from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.afterburners.models import AfterburnerProfile
from apps.builds.forms import BuildDefinitionForm
from apps.builds.models import BuildDefinition, BuildMachineConfig
from apps.builds.services.kickstart import calculate_layout_disk_size_gib
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout
from apps.repositories.models import PackageRepository, RedHatRepositoryCatalog
from apps.serverconfig.models import ServerConfiguration


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


class BuildDefinitionFormAfterburnerTests(TestCase):
    def setUp(self):
        self.os_obj = OperatingSystem.objects.create(name="RHEL", family=OperatingSystem.FAMILY_RHEL)
        self.layout = PartitionLayout.objects.create(name="form-layout", table_type=PartitionLayout.TABLE_MBR)
        self.cfg = BuildMachineConfig.objects.create(name="form-cfg", boot_mode=BuildMachineConfig.BOOT_BIOS)
        self.iso = ISOImage.objects.create(operating_system=self.os_obj, version="10.0", iso_file="isos/form.iso")

    def _form_data(self):
        return {
            "name": "form-build",
            "operating_system": str(self.os_obj.id),
            "iso_image": str(self.iso.id),
            "partition_layout": str(self.layout.id),
            "machine_config": str(self.cfg.id),
            "output_pxe": "on",
            "output_usb_img": "on",
        }

    def test_accepts_valid_afterburner_order_payload(self):
        first = AfterburnerProfile.objects.create(name="A")
        second = AfterburnerProfile.objects.create(name="B")
        data = self._form_data()
        data["afterburner_order_json"] = json.dumps([
            {"id": second.id},
            {"id": first.id},
        ])

        form = BuildDefinitionForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(form.cleaned_data["ordered_afterburner_ids"], [second.id, first.id])

    def test_rejects_unknown_afterburner_id_in_order_payload(self):
        data = self._form_data()
        data["afterburner_order_json"] = '[{"id": 999999}]'

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("Unknown afterburner IDs", form.errors.as_text())

    def test_accepts_valid_repository_payload(self):
        repo = PackageRepository.objects.create(
            name="repo-a",
            family=PackageRepository.FAMILY_RPM,
            enabled=True,
            base_url="https://repo.example.invalid/rpm",
            rpm_repoid="repo-a",
        )
        data = self._form_data()
        data["repository_order_json"] = json.dumps([
            {"id": repo.id, "during_build": True, "before_afterburner": True},
        ])

        form = BuildDefinitionForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(
            form.cleaned_data["ordered_repository_payload"],
            [{"id": repo.id, "during_build": True, "before_afterburner": True}],
        )

    def test_rejects_repository_with_no_phase_selected(self):
        repo = PackageRepository.objects.create(
            name="repo-a",
            family=PackageRepository.FAMILY_RPM,
            enabled=True,
            base_url="https://repo.example.invalid/rpm",
            rpm_repoid="repo-a",
        )
        data = self._form_data()
        data["repository_order_json"] = json.dumps([
            {"id": repo.id, "during_build": False, "before_afterburner": False},
        ])

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("at least one phase", form.errors.as_text())

    def test_rejects_repository_with_wrong_family(self):
        repo = PackageRepository.objects.create(
            name="repo-a",
            family=PackageRepository.FAMILY_DEB,
            enabled=True,
            base_url="https://repo.example.invalid/deb",
            deb_suite="bookworm",
            deb_components="main",
        )
        data = self._form_data()
        data["repository_order_json"] = json.dumps([
            {"id": repo.id, "during_build": True, "before_afterburner": False},
        ])

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("must match the build OS family", form.errors.as_text())

    def test_requires_rhsm_auth_mode_when_rhsm_repositories_selected(self):
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-baseos-rpms",
            name="RHEL 10 BaseOS",
        )
        data = self._form_data()
        data["rhsm_repositories"] = [str(rh_repo.id)]

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("Select an RHSM authentication mode", form.errors.as_text())

    def test_accepts_rhsm_username_password_mode_with_selected_repositories(self):
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-appstream-rpms",
            name="RHEL 10 AppStream",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_USERPASS,
                "rhsm_username": "rh-user",
                "rhsm_password": "secret-pass",
                "rhsm_repositories": [str(rh_repo.id)],
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_accepts_rhsm_configuration_credentials_mode_with_selected_repositories(self):
        cfg = ServerConfiguration.get_solo()
        cfg.rhn_username = "server-rh-user"
        cfg.set_rhn_password("server-rh-pass")
        cfg.save()
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-baseos-rpms",
            name="RHEL 10 BaseOS",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_CONFIG,
                "rhsm_repositories": [str(rh_repo.id)],
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_accepts_rhsm_repository_order_payload(self):
        cfg = ServerConfiguration.get_solo()
        cfg.rhn_username = "server-rh-user"
        cfg.set_rhn_password("server-rh-pass")
        cfg.save()
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-baseos-rpms",
            name="RHEL 10 BaseOS",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_CONFIG,
                "rhsm_repository_order_json": json.dumps(
                    [
                        {
                            "id": rh_repo.id,
                            "during_build": True,
                            "before_afterburner": True,
                        }
                    ]
                ),
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(
            form.cleaned_data["ordered_rhsm_repository_payload"],
            [{"id": rh_repo.id, "during_build": True, "before_afterburner": True}],
        )

    def test_rejects_rhsm_repository_order_payload_without_phase(self):
        cfg = ServerConfiguration.get_solo()
        cfg.rhn_username = "server-rh-user"
        cfg.set_rhn_password("server-rh-pass")
        cfg.save()
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-baseos-rpms",
            name="RHEL 10 BaseOS",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_CONFIG,
                "rhsm_repository_order_json": json.dumps(
                    [
                        {
                            "id": rh_repo.id,
                            "during_build": False,
                            "before_afterburner": False,
                        }
                    ]
                ),
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("Each attached RHSM repository must be enabled for at least one phase", form.errors.as_text())

    def test_rejects_rhsm_configuration_credentials_mode_without_server_credentials(self):
        cfg = ServerConfiguration.get_solo()
        cfg.rhn_username = ""
        cfg.clear_rhn_password()
        cfg.save()
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-baseos-rpms",
            name="RHEL 10 BaseOS",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_CONFIG,
                "rhsm_repositories": [str(rh_repo.id)],
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("Server configuration RHSM credentials are required", form.errors.as_text())

    def test_rejects_rhsm_repository_with_mismatched_iso_major(self):
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=9,
            architecture="x86_64",
            repo_id="rhel-9-baseos-rpms",
            name="RHEL 9 BaseOS",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_USERPASS,
                "rhsm_username": "rh-user",
                "rhsm_password": "secret-pass",
                "rhsm_repositories": [str(rh_repo.id)],
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertIn("must match ISO major version", form.errors.as_text())

    def test_persists_rhsm_account_info_on_build_configuration(self):
        rh_repo = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-baseos-rpms",
            name="RHEL 10 BaseOS",
        )
        data = self._form_data()
        data.update(
            {
                "rhsm_auth_mode": BuildDefinition.RHSM_AUTH_USERPASS,
                "rhsm_username": "rh-user",
                "rhsm_password": "secret-pass",
                "rhsm_repositories": [str(rh_repo.id)],
            }
        )

        form = BuildDefinitionForm(data=data)

        self.assertTrue(form.is_valid(), form.errors.as_text())
        build = form.save(commit=False)
        build.save()

        self.assertEqual(build.rhsm_username, "rh-user")
        self.assertTrue(build.has_rhsm_password())
        self.assertEqual(build.get_rhsm_password(), "secret-pass")
