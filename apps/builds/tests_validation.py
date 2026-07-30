from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.builds.models import BuildDefinition, BuildMachineConfig
from apps.builds.services.kickstart import calculate_layout_disk_size_gib
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout


class BuildValidationTests(TestCase):
    def test_build_machine_config_forces_fixed_network(self):
        cfg = BuildMachineConfig.objects.create(name="cfg", libvirt_network="not-allowed")
        cfg.refresh_from_db()
        self.assertEqual(cfg.libvirt_network, BuildMachineConfig.FIXED_LIBVIRT_NETWORK)

    def test_iso_must_belong_to_selected_os(self):
        os1 = OperatingSystem.objects.create(name="RHEL", family=OperatingSystem.FAMILY_RHEL)
        os2 = OperatingSystem.objects.create(name="Ubuntu", family=OperatingSystem.FAMILY_DEBIAN)
        layout = PartitionLayout.objects.create(name="layout")
        cfg = BuildMachineConfig.objects.create(name="cfg")
        iso = ISOImage.objects.create(operating_system=os1, version="10.0", iso_file="isos/test.iso")

        build = BuildDefinition(
            name="mismatch",
            operating_system=os2,
            iso_image=iso,
            partition_layout=layout,
            machine_config=cfg,
            output_pxe=True,
            output_usb_img=False,
        )

        with self.assertRaises(ValidationError):
            build.clean()

    def test_at_least_one_output_required(self):
        os1 = OperatingSystem.objects.create(name="RHEL", family=OperatingSystem.FAMILY_RHEL)
        layout = PartitionLayout.objects.create(name="layout")
        cfg = BuildMachineConfig.objects.create(name="cfg")
        iso = ISOImage.objects.create(operating_system=os1, version="10.0", iso_file="isos/test.iso")

        build = BuildDefinition(
            name="no-output",
            operating_system=os1,
            iso_image=iso,
            partition_layout=layout,
            machine_config=cfg,
            output_pxe=False,
            output_usb_img=False,
        )

        with self.assertRaises(ValidationError):
            build.clean()

    def test_layout_disk_size_comes_from_partition_entries(self):
        layout = PartitionLayout.objects.create(name="disk-layout")
        PartitionEntry.objects.create(
            layout=layout,
            order=1,
            name="boot",
            mount_point="/boot",
            filesystem="ext4",
            size_mode=PartitionEntry.SIZE_FIXED,
            size_mib=1024,
        )
        PartitionEntry.objects.create(
            layout=layout,
            order=2,
            name="root",
            mount_point="/",
            filesystem="xfs",
            size_mode=PartitionEntry.SIZE_REMAINDER,
        )

        self.assertEqual(calculate_layout_disk_size_gib(layout), 12)
