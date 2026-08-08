from django.test import TestCase

from apps.packages.models import PackageItem, PackageList
from apps.builds.models import BuildDefinition, BuildMachineConfig
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout
from apps.workers.tasks import _collect_selected_package_group_log_entries, _collect_selected_packages_for_build


def _make_build(os_family="rhel", suffix=""):
    family = OperatingSystem.FAMILY_RHEL if os_family == "rhel" else OperatingSystem.FAMILY_DEBIAN
    os_obj = OperatingSystem.objects.create(name=f"OS-{os_family}{suffix}", family=family)
    iso = ISOImage.objects.create(operating_system=os_obj, version=f"1.0{suffix}", iso_file=f"isos/test{suffix}.iso")
    layout = PartitionLayout.objects.create(name=f"layout{suffix}")
    PartitionEntry.objects.create(
        layout=layout,
        order=1,
        name="root",
        mount_point="/",
        filesystem="xfs",
        size_mode=PartitionEntry.SIZE_REMAINDER,
    )
    cfg = BuildMachineConfig.objects.create(name=f"cfg{suffix}", boot_mode="bios")
    build = BuildDefinition.objects.create(
        name=f"build-{os_family}{suffix}",
        operating_system=os_obj,
        iso_image=iso,
        partition_layout=layout,
        machine_config=cfg,
    )
    return build


class CollectPackagesTest(TestCase):
    def test_regular_packages_go_to_installs(self):
        build = _make_build("rhel", "-reg")
        pl = PackageList.objects.create(name="base", distro_family=PackageList.DISTRO_RHEL)
        PackageItem.objects.create(package_list=pl, package_name="vim")
        PackageItem.objects.create(package_list=pl, package_name="curl")
        build.package_lists.add(pl)
        groups, installs, removes, skipped = _collect_selected_packages_for_build(build)
        self.assertEqual(installs, ["curl", "vim"])
        self.assertEqual(groups, [])
        self.assertEqual(removes, [])
        self.assertEqual(skipped, [])

    def test_at_prefix_goes_to_groups(self):
        build = _make_build("rhel", "-grp")
        pl = PackageList.objects.create(name="grp", distro_family=PackageList.DISTRO_RHEL)
        PackageItem.objects.create(package_list=pl, package_name="@gnome-desktop")
        PackageItem.objects.create(package_list=pl, package_name="@workstation-product-environment")
        build.package_lists.add(pl)
        groups, installs, removes, _ = _collect_selected_packages_for_build(build)
        self.assertIn("gnome-desktop", groups)
        self.assertIn("workstation-product-environment", groups)
        self.assertEqual(installs, [])
        self.assertEqual(removes, [])

    def test_environment_group_prefix_is_preserved(self):
        build = _make_build("rhel", "-env")
        pl = PackageList.objects.create(name="env", distro_family=PackageList.DISTRO_RHEL)
        PackageItem.objects.create(package_list=pl, package_name="@^minimal-environment")
        build.package_lists.add(pl)
        groups, installs, removes, _ = _collect_selected_packages_for_build(build)
        self.assertEqual(groups, ["minimal-environment"])
        self.assertEqual(installs, [])
        self.assertEqual(removes, [])

    def test_minus_prefix_goes_to_removes(self):
        build = _make_build("rhel", "-rem")
        pl = PackageList.objects.create(name="trim", distro_family=PackageList.DISTRO_RHEL)
        PackageItem.objects.create(package_list=pl, package_name="-bind")
        PackageItem.objects.create(package_list=pl, package_name="-telnet")
        build.package_lists.add(pl)
        groups, installs, removes, _ = _collect_selected_packages_for_build(build)
        self.assertEqual(removes, ["bind", "telnet"])
        self.assertEqual(installs, [])
        self.assertEqual(groups, [])

    def test_mixed_entries(self):
        build = _make_build("rhel", "-mix")
        pl = PackageList.objects.create(name="mixed", distro_family=PackageList.DISTRO_ALL)
        PackageItem.objects.create(package_list=pl, package_name="@gnome-desktop")
        PackageItem.objects.create(package_list=pl, package_name="vim")
        PackageItem.objects.create(package_list=pl, package_name="-bind")
        build.package_lists.add(pl)
        groups, installs, removes, _ = _collect_selected_packages_for_build(build)
        self.assertEqual(groups, ["gnome-desktop"])
        self.assertEqual(installs, ["vim"])
        self.assertEqual(removes, ["bind"])

    def test_incompatible_distro_family_skipped(self):
        build = _make_build("rhel", "-inc")
        pl_debian = PackageList.objects.create(name="deb", distro_family=PackageList.DISTRO_DEBIAN)
        PackageItem.objects.create(package_list=pl_debian, package_name="nginx")
        build.package_lists.add(pl_debian)
        groups, installs, removes, skipped = _collect_selected_packages_for_build(build)
        self.assertIn("deb", skipped)
        self.assertEqual(installs, [])

    def test_quoted_group_name_with_spaces(self):
        build = _make_build("rhel", "-qgrp")
        pl = PackageList.objects.create(name="qgrp", distro_family=PackageList.DISTRO_RHEL)
        PackageItem.objects.create(package_list=pl, package_name='"@KDE Plasma Workspaces"')
        PackageItem.objects.create(package_list=pl, package_name="'@GNOME Desktop'")
        PackageItem.objects.create(package_list=pl, package_name="@gnome-desktop")
        build.package_lists.add(pl)
        groups, installs, removes, _ = _collect_selected_packages_for_build(build)
        self.assertIn("KDE Plasma Workspaces", groups)
        self.assertIn("GNOME Desktop", groups)
        self.assertIn("gnome-desktop", groups)
        self.assertEqual(installs, [])
        self.assertEqual(removes, [])

    def test_group_log_entries_show_raw_and_normalized_names(self):
        build = _make_build("rhel", "-log")
        pl = PackageList.objects.create(name="log", distro_family=PackageList.DISTRO_RHEL)
        PackageItem.objects.create(package_list=pl, package_name="@gnome-desktop")
        PackageItem.objects.create(package_list=pl, package_name="@^minimal-environment")
        build.package_lists.add(pl)

        entries = _collect_selected_package_group_log_entries(build)

        self.assertCountEqual(
            entries,
            ["@gnome-desktop -> gnome-desktop", "@^minimal-environment -> minimal-environment"],
        )

    def test_deduplication_across_lists(self):
        build = _make_build("rhel", "-dup")
        pl1 = PackageList.objects.create(name="l1", distro_family=PackageList.DISTRO_ALL)
        pl2 = PackageList.objects.create(name="l2", distro_family=PackageList.DISTRO_ALL)
        PackageItem.objects.create(package_list=pl1, package_name="vim")
        PackageItem.objects.create(package_list=pl2, package_name="vim")
        build.package_lists.add(pl1, pl2)
        _, installs, _, _ = _collect_selected_packages_for_build(build)
        self.assertEqual(installs.count("vim"), 1)
