from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from apps.afterburners.models import AfterburnerItem, AfterburnerProfile, AfterburnerScriptInput
from apps.builds.models import BuildDefinition, BuildMachineConfig
from apps.builds.services.artifacts import (
    ArtifactExportError,
    dump_clone_partitions,
    generate_artifacts,
    _write_usb_image_from_bundle,
)
from apps.builds.services.kickstart import render_deploy_restore_script
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout
from apps.repositories.models import PackageRepository, RedHatRepositoryCatalog


class ArtifactGenerationTests(TestCase):
    def setUp(self):
        os_obj = OperatingSystem.objects.create(name="RHEL-ART", family=OperatingSystem.FAMILY_RHEL)
        layout = PartitionLayout.objects.create(name="layout-art")
        cfg = BuildMachineConfig.objects.create(name="cfg-art")
        iso = ISOImage.objects.create(operating_system=os_obj, version="10.3", iso_file="isos/art.iso")

        self.build = BuildDefinition.objects.create(
            name="build-art",
            operating_system=os_obj,
            iso_image=iso,
            partition_layout=layout,
            machine_config=cfg,
            output_pxe=True,
            output_usb_img=False,
        )

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_afterburner_script_renders_ordered_profiles(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        first_profile = AfterburnerProfile.objects.create(name="20-hostname")
        second_profile = AfterburnerProfile.objects.create(name="10-custom")

        AfterburnerItem.objects.create(
            profile=first_profile,
            order=1,
            name="set hostname",
            item_type=AfterburnerItem.TYPE_HOSTNAME,
            config={"default_hostname": "lab-node"},
        )
        custom_item = AfterburnerItem.objects.create(
            profile=second_profile,
            order=1,
            name="custom script",
            item_type=AfterburnerItem.TYPE_CUSTOM_SCRIPT,
            config={"script_body": "echo custom"},
        )
        AfterburnerScriptInput.objects.create(
            item=custom_item,
            order=1,
            key="ENVIRONMENT",
            label="Environment",
            input_type=AfterburnerScriptInput.TYPE_SELECT,
            select_options=["dev", "prod"],
        )
        AfterburnerScriptInput.objects.create(
            item=custom_item,
            order=2,
            key="RETRIES",
            label="Retries",
            input_type=AfterburnerScriptInput.TYPE_INT,
            required=True,
        )

        self.build.afterburner_selections.create(afterburner=second_profile, order=1)
        self.build.afterburner_selections.create(afterburner=first_profile, order=2)

        root = Path("/tmp/tuxwsmaker-test-afterburner-render")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        script = (root / f"build-{self.build.id}" / "pxe" / "deploy" / "afterburner.sh").read_text(encoding="utf-8")

        first_idx = script.index("--- Profile: 10-custom ---")
        second_idx = script.index("--- Profile: 20-hostname ---")
        self.assertLess(first_idx, second_idx)
        self.assertIn('case "${ENVIRONMENT}" in', script)
        self.assertIn("dev) ;;", script)
        self.assertIn("prod) ;;", script)
        self.assertIn("ENVIRONMENT must be one of: dev, prod", script)
        self.assertIn('RETRIES must be an integer', script)
        self.assertIn('RETRIES is required', script)
        self.assertIn('env ENVIRONMENT="${ENVIRONMENT:-}" RETRIES="${RETRIES:-}" bash /tmp/tuxws-afterburner-custom.sh', script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_afterburner_script_includes_luks_autodetect_flow(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        profile = AfterburnerProfile.objects.create(name="30-luks")
        AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="rotate luks",
            item_type=AfterburnerItem.TYPE_LUKS_ROTATE,
            config={"autodetect": True, "device": "/dev/sda3"},
        )
        self.build.afterburner_selections.create(afterburner=profile, order=1)

        root = Path("/tmp/tuxwsmaker-test-afterburner-luks")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        script = (root / f"build-{self.build.id}" / "pxe" / "deploy" / "afterburner.sh").read_text(encoding="utf-8")
        self.assertIn("discover_luks_devices", script)
        self.assertIn('"$TARGET_ROOT/etc/crypttab"', script)
        self.assertIn("resolve_crypttab_source", script)
        self.assertIn("LUKS autodetect is ambiguous", script)
        self.assertIn("LUKS_AUTODETECT_OUTPUT", script)
        self.assertIn("rotate_luks_container", script)
        self.assertIn("LUKS_TARGETS", script)
        self.assertIn("cryptsetup isLuks", script)
        self.assertIn("for LUKS_DEV in \"${LUKS_TARGETS[@]}\"", script)
        self.assertIn("DEFAULT_LUKS_PASSWORD", script)
        self.assertIn("test-passphrase", script)
        self.assertIn("Confirm new LUKS password for", script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_afterburner_script_includes_wait_for_enter_prompt(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        profile = AfterburnerProfile.objects.create(name="40-wait")
        AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="wait",
            item_type=AfterburnerItem.TYPE_WAIT_FOR_ENTER,
            config={"message": "Press Enter to continue"},
        )
        self.build.afterburner_selections.create(afterburner=profile, order=1)

        root = Path("/tmp/tuxwsmaker-test-afterburner-wait")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        script = (root / f"build-{self.build.id}" / "pxe" / "deploy" / "afterburner.sh").read_text(encoding="utf-8")
        self.assertIn("wait for enter", script)
        self.assertIn("Press Enter to continue", script)
        self.assertIn("read -r _ < /dev/console || true", script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_afterburner_script_includes_bootloader_password_prompt(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        profile = AfterburnerProfile.objects.create(name="50-grub")
        AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="set grub password",
            item_type=AfterburnerItem.TYPE_BOOTLOADER_PASSWORD,
            config={"grub_user": "admin"},
        )
        self.build.afterburner_selections.create(afterburner=profile, order=1)

        root = Path("/tmp/tuxwsmaker-test-afterburner-grub")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        script = (root / f"build-{self.build.id}" / "pxe" / "deploy" / "afterburner.sh").read_text(encoding="utf-8")
        self.assertIn('prompt_text GRUB_USER "GRUB superuser"', script)
        self.assertIn('GRUB_MKPASSWD_BIN', script)
        self.assertIn('grub2-mkpasswd-pbkdf2', script)
        self.assertIn('password_pbkdf2 $GRUB_USER $GRUB_PW_HASH', script)
        self.assertIn('chmod 600 "$GRUB_USER_CFG"', script)

    def test_deploy_restore_script_includes_temporary_repository_hooks_before_afterburner(self):
        repo = PackageRepository.objects.create(
            name="repo-a",
            family=PackageRepository.FAMILY_RPM,
            enabled=True,
            base_url="https://repo.example.invalid/rpm",
            rpm_repoid="repo-a",
        )
        self.build.repository_selections.create(
            repository=repo,
            order=1,
            enable_during_build=False,
            enable_before_afterburner=True,
        )

        root = Path("/tmp/tuxwsmaker-test-restore-repos")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        path = render_deploy_restore_script(output_dir=root, os_family=self.build.operating_system.family, build=self.build)
        script = path.read_text(encoding="utf-8")

        self.assertIn("Activating temporary repositories for deploy/afterburner", script)
        self.assertIn("tuxwsmaker-repo-", script)
        self.assertIn("Cleaning up temporary repositories for deploy/afterburner", script)

    def test_deploy_restore_script_marks_swap_entries_nofail(self):
        layout = PartitionLayout.objects.create(name="swap-layout")
        PartitionEntry.objects.create(
            layout=layout,
            order=1,
            name="root",
            mount_point="/",
            filesystem="xfs",
            size_mode=PartitionEntry.SIZE_REMAINDER,
        )
        PartitionEntry.objects.create(
            layout=layout,
            order=2,
            name="swap",
            mount_point="swap",
            filesystem="swap",
            size_mode=PartitionEntry.SIZE_FIXED,
            size_mib=2048,
        )

        build = BuildDefinition.objects.create(
            name="build-swap",
            operating_system=self.build.operating_system,
            iso_image=self.build.iso_image,
            partition_layout=layout,
            machine_config=self.build.machine_config,
        )

        root = Path("/tmp/tuxwsmaker-test-restore-swap")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        path = render_deploy_restore_script(output_dir=root, os_family=build.operating_system.family, build=build)
        script = path.read_text(encoding="utf-8")

        self.assertIn("nofail,x-systemd.device-timeout=10s", script)

    def test_deploy_restore_script_never_opens_swap_as_luks(self):
        layout = PartitionLayout.objects.create(name="swap-luks-guard")
        PartitionEntry.objects.create(
            layout=layout,
            order=1,
            name="root",
            mount_point="/",
            filesystem="xfs",
            size_mode=PartitionEntry.SIZE_REMAINDER,
        )
        PartitionEntry.objects.create(
            layout=layout,
            order=2,
            name="swap",
            mount_point="swap",
            filesystem="swap",
            size_mode=PartitionEntry.SIZE_FIXED,
            size_mib=2048,
            luks_enabled=True,
            luks_name="cryptswap",
        )

        build = BuildDefinition.objects.create(
            name="build-swap-luks-guard",
            operating_system=self.build.operating_system,
            iso_image=self.build.iso_image,
            partition_layout=layout,
            machine_config=self.build.machine_config,
        )

        root = Path("/tmp/tuxwsmaker-test-restore-swap-luks-guard")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        path = render_deploy_restore_script(output_dir=root, os_family=build.operating_system.family, build=build)
        script = path.read_text(encoding="utf-8")

        self.assertIn('if [[ "$mount_point" == "swap" || "$fs_type" == "swap" ]]; then', script)
        self.assertIn('luks_enabled="False"', script)
        self.assertIn('luks_name=""', script)

    def test_deploy_restore_script_logs_partition_metadata(self):
        layout = PartitionLayout.objects.create(name="partition-log-layout")
        PartitionEntry.objects.create(
            layout=layout,
            order=1,
            name="root",
            mount_point="/",
            filesystem="xfs",
            size_mode=PartitionEntry.SIZE_REMAINDER,
        )

        build = BuildDefinition.objects.create(
            name="build-partition-log",
            operating_system=self.build.operating_system,
            iso_image=self.build.iso_image,
            partition_layout=layout,
            machine_config=self.build.machine_config,
        )

        root = Path("/tmp/tuxwsmaker-test-restore-partition-log")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        path = render_deploy_restore_script(output_dir=root, os_family=build.operating_system.family, build=build)
        script = path.read_text(encoding="utf-8")

        self.assertIn("Partition metadata:", script)
        self.assertIn("mount=${mount_point:-none}", script)
        self.assertIn("fs=${fs_type:-none}", script)
        self.assertIn("luks=${luks_enabled:-False}", script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_bundle_persists_rhsm_repo_ids_for_afterburner_restore(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        profile = AfterburnerProfile.objects.create(name="70-rhsm")
        AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="rh registration",
            item_type=AfterburnerItem.TYPE_REDHAT_REGISTRATION,
            config={
                "username": "rh-user",
                "password": "rh-pass",
                "repo_ids": "",
            },
        )
        self.build.afterburner_selections.create(afterburner=profile, order=1)

        rhsm_repo_a = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-for-x86_64-baseos-rpms",
            name="BaseOS",
        )
        rhsm_repo_b = RedHatRepositoryCatalog.objects.create(
            rhel_major=10,
            architecture="x86_64",
            repo_id="rhel-10-for-x86_64-appstream-rpms",
            name="AppStream",
        )
        self.build.rhsm_repository_selections.create(
            repository=rhsm_repo_a,
            order=1,
            enable_during_build=False,
            enable_before_afterburner=True,
        )
        self.build.rhsm_repository_selections.create(
            repository=rhsm_repo_b,
            order=2,
            enable_during_build=False,
            enable_before_afterburner=True,
        )

        root = Path("/tmp/tuxwsmaker-test-rhsm-repo-payload")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        deploy_root = root / f"build-{self.build.id}" / "pxe" / "deploy"
        repo_file = deploy_root / "rhsm-repositories.txt"
        self.assertTrue(repo_file.exists())
        self.assertEqual(
            repo_file.read_text(encoding="utf-8"),
            "rhel-10-for-x86_64-baseos-rpms\nrhel-10-for-x86_64-appstream-rpms\n",
        )

        script = (deploy_root / "afterburner.sh").read_text(encoding="utf-8")
        self.assertIn("RHSM_REPO_FILE=/run/install/repo/deploy/rhsm-repositories.txt", script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_afterburner_script_includes_tpm_integration_prompt(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        profile = AfterburnerProfile.objects.create(name="60-tpm")
        AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="tpm",
            item_type=AfterburnerItem.TYPE_TPM_INTEGRATION,
            config={
                "device": "/dev/sda3",
                "autodetect": True,
                "hash": "sha256",
                "pcr_bank": "sha256",
                "key": "ecc",
                "pcr_ids": ["7"],
            },
        )
        self.build.afterburner_selections.create(afterburner=profile, order=1)

        root = Path("/tmp/tuxwsmaker-test-afterburner-tpm")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        script = (root / f"build-{self.build.id}" / "pxe" / "deploy" / "afterburner.sh").read_text(encoding="utf-8")
        self.assertIn('TPM2_POLICY_B64=', script)
        self.assertIn('TPM2_POLICY="$(printf %s "$TPM2_POLICY_B64" | base64 -d)"', script)
        self.assertIn("discover_tpm_luks_devices", script)
        self.assertIn('"$TARGET_ROOT/etc/crypttab"', script)
        self.assertIn("TPM LUKS autodetect is ambiguous", script)
        self.assertIn("TPM_AUTODETECT_OUTPUT", script)
        self.assertIn('clevis luks bind -y -k - -d "$container_dev" tpm2 "$TPM2_POLICY"', script)
        self.assertIn('clevis luks list -d "$container_dev"', script)
        self.assertIn('cryptsetup luksRemoveKey "$container_dev" -', script)
        self.assertIn('run_chroot dracut -q -f --regenerate-all', script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_afterburner_script_omits_pcr_ids_when_none_selected(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        profile = AfterburnerProfile.objects.create(name="61-tpm-no-pcr")
        AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="tpm",
            item_type=AfterburnerItem.TYPE_TPM_INTEGRATION,
            config={
                "device": "/dev/sda3",
                "hash": "sha256",
                "pcr_bank": "sha256",
                "key": "ecc",
                "pcr_ids": [],
            },
        )
        self.build.afterburner_selections.create(afterburner=profile, order=1)

        root = Path("/tmp/tuxwsmaker-test-afterburner-tpm-no-pcr")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        script = (root / f"build-{self.build.id}" / "pxe" / "deploy" / "afterburner.sh").read_text(encoding="utf-8")
        self.assertIn('TPM2_POLICY_B64=', script)
        self.assertIn('TPM2_POLICY="$(printf %s "$TPM2_POLICY_B64" | base64 -d)"', script)
        self.assertNotIn('"pcr_ids":', script)

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_bundle_uses_extracted_boot_assets(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        root = Path("/tmp/tuxwsmaker-test-artifacts")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)
        shim_path = root / "shimx64.efi"
        shim_path.write_bytes(b"shim")
        mock_uefi.return_value = [shim_path]

        clone_payload_dir = root / f"build-{self.build.id}" / "clone-release"
        clone_payload_dir.mkdir(parents=True, exist_ok=True)
        (clone_payload_dir / "manifest.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        (clone_payload_dir / "partition-01.img").write_bytes(b"payload")

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        pxe_dir = root / f"build-{self.build.id}" / "pxe"
        bios = (pxe_dir / "pxelinux.cfg" / "default").read_text(encoding="utf-8")
        grub = (pxe_dir / "efi" / "grub.cfg").read_text(encoding="utf-8")
        deploy = json.loads((pxe_dir / "deploy.json").read_text(encoding="utf-8"))
        manifest = json.loads((pxe_dir / "manifest.json").read_text(encoding="utf-8"))
        restore_script = (pxe_dir / "deploy" / "restore.sh").read_text(encoding="utf-8")
        deploy_kickstart = (pxe_dir / "deploy" / f"{self.build.name}-deploy.cfg").read_text(encoding="utf-8")
        pxe_readme = (pxe_dir / "README.txt").read_text(encoding="utf-8")

        self.assertIn("/boot/vmlinuz", bios)
        self.assertIn("/boot/initrd.img", bios)
        self.assertIn("/boot/vmlinuz", grub)
        self.assertIn("/boot/initrd.img", grub)
        self.assertIn("inst.stage2=__PXE_BASE_URL__/stage2", bios)
        self.assertIn("inst.repo=__PXE_BASE_URL__", bios)
        self.assertIn(f"inst.ks=__PXE_BASE_URL__/deploy/{self.build.name}-deploy.cfg", bios)
        self.assertIn("inst.stage2=__PXE_BASE_URL__/stage2", grub)
        self.assertEqual(deploy["artifact_type"], "pxe")
        self.assertEqual(deploy["operating_system"]["family"], OperatingSystem.FAMILY_RHEL)
        self.assertEqual(deploy["payload_delivery"], "pxe_http")
        self.assertEqual(deploy["scaffold"]["restore_script"], "deploy/restore.sh")
        self.assertEqual(manifest["deploy_manifest"], "deploy.json")
        self.assertEqual(manifest["os_family"], OperatingSystem.FAMILY_RHEL)
        self.assertIn("RHEL-family finish adapter", restore_script)
        self.assertIn("sfdisk --force --wipe always --wipe-partitions always", restore_script)
        self.assertIn("Restoring partition", restore_script)
        self.assertIn("status()", restore_script)
        self.assertIn("[$PART_INDEX/$TOTAL_PARTS]", restore_script)
        self.assertIn("Partition $number is empty; skipping write", restore_script)
        self.assertIn("grub2-install", restore_script)
        self.assertIn("Verifying BIOS bootloader", restore_script)
        self.assertIn("Missing MBR boot signature", restore_script)
        self.assertIn("MBR boot code area is empty", restore_script)
        self.assertIn("Source image boot mode", restore_script)
        self.assertIn("does not match target firmware mode", restore_script)
        self.assertIn("afterburner.sh", restore_script)
        self.assertIn("DEPLOY_MANIFEST_URL=file:///run/install/repo/deploy.json", deploy_kickstart)
        self.assertIn("%pre --erroronfail", deploy_kickstart)
        self.assertIn("%pre --erroronfail --log=/tmp/deploy-pre.log", deploy_kickstart)
        self.assertIn('exec < "$PRE_INPUT_TTY" > "$PRE_INPUT_TTY" 2>&1', deploy_kickstart)
        self.assertNotIn('DEPLOY_TTY="/dev/tty6"', deploy_kickstart)
        self.assertNotIn("chvt 6", deploy_kickstart)
        self.assertIn("read -r -t 900 _ || true", deploy_kickstart)
        self.assertIn("bash /tmp/restore.sh", deploy_kickstart)
        self.assertIn("Restore looks complete. The system will reboot automatically.", deploy_kickstart)
        self.assertIn('reboot -f || systemctl reboot -f || reboot || poweroff -f || halt -f', deploy_kickstart)
        self.assertNotIn("waiting for console confirmation before poweroff", deploy_kickstart)
        self.assertNotIn("%packages", deploy_kickstart)
        self.assertIn("Missing local restore script", deploy_kickstart)
        self.assertNotIn("curl -fsSL", deploy_kickstart)
        self.assertIn("How to place on PXE server", pxe_readme)
        self.assertIn("What clone-release is for", pxe_readme)
        self.assertIn("Set __PXE_BASE_URL__", pxe_readme)
        self.assertTrue((pxe_dir / "efi" / "shimx64.efi").exists())
        self.assertIn("signed UEFI chain assets", pxe_readme)
        self.assertNotIn("__PXE_REPO_URL__", pxe_readme)
        self.assertTrue((pxe_dir / "deploy" / "afterburner.sh").exists())
        self.assertTrue((pxe_dir / "clone-release" / "manifest.json").exists())
        self.assertTrue((pxe_dir / "clone-release" / "partition-01.img").exists())

    @patch("apps.builds.services.artifacts._extract_uefi_boot_assets_from_iso")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_packs_uefi_assets_without_self_copying(self, mock_extract, _mock_usb, _mock_iso_tree, mock_uefi):
        self.build.machine_config.boot_mode = BuildMachineConfig.BOOT_UEFI
        self.build.machine_config.save(update_fields=["boot_mode"])

        root = Path("/tmp/tuxwsmaker-test-artifacts-uefi")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)

        efi_dir = root / "efi-assets"
        efi_dir.mkdir(parents=True, exist_ok=True)
        shim_path = efi_dir / "BOOTX64.EFI"
        shim_path.write_bytes(b"uefi")
        mock_uefi.return_value = [shim_path]

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        pxe_dir = root / f"build-{self.build.id}" / "pxe"
        self.assertTrue((pxe_dir / "efi" / "BOOTX64.EFI").exists())

    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_restore_scaffold_uses_debian_adapter_when_os_family_is_debian(self, mock_extract, _mock_usb, _mock_iso_tree):
        self.build.operating_system.family = OperatingSystem.FAMILY_DEBIAN
        self.build.operating_system.save(update_fields=["family"])

        root = Path("/tmp/tuxwsmaker-test-artifacts-debian")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        pxe_dir = root / f"build-{self.build.id}" / "pxe"
        restore_script = (pxe_dir / "deploy" / "restore.sh").read_text(encoding="utf-8")
        deploy = json.loads((pxe_dir / "deploy.json").read_text(encoding="utf-8"))

        self.assertIn("Debian-family finish adapter", restore_script)
        self.assertEqual(deploy["operating_system"]["family"], OperatingSystem.FAMILY_DEBIAN)

    @patch("apps.builds.services.artifacts._write_usb_image_from_bundle")
    @patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_usb_bundle_embeds_clone_payload_without_builder_dependency(self, mock_extract, _mock_stage2, mock_write_image):
        self.build.output_usb_img = True
        self.build.save(update_fields=["output_usb_img"])

        root = Path("/tmp/tuxwsmaker-test-usb-bundle")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)

        def fake_usb_image(*, bundle_dir, output_path, build_name, source_iso_path=None, build_boot_mode=None):
            output_path.write_bytes(b"usb-image")
            grub_dir = bundle_dir / "boot" / "grub"
            grub_dir.mkdir(parents=True, exist_ok=True)
            (grub_dir / "grub.cfg").write_text(
                f"linux /boot/vmlinuz inst.stage2=hd:LABEL=TUXWSDEPLOY:/stage2 inst.repo=hd:LABEL=TUXWSDEPLOY:/ inst.ks=hd:LABEL=TUXWSDEPLOY:/deploy/{build_name}-deploy.cfg\n",
                encoding="utf-8",
            )
            return output_path

        mock_write_image.side_effect = fake_usb_image

        clone_payload_dir = root / f"build-{self.build.id}" / "clone-release"
        clone_payload_dir.mkdir(parents=True, exist_ok=True)
        (clone_payload_dir / "manifest.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        (clone_payload_dir / "partition-01.img").write_bytes(b"payload")

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        usb_dir = root / f"build-{self.build.id}" / "usb"
        usb_img = root / f"build-{self.build.id}" / "usb.img"
        self.assertTrue((usb_dir / "boot" / "vmlinuz").exists())
        self.assertTrue((usb_dir / "boot" / "initrd.img").exists())
        self.assertTrue((usb_dir / "clone-release" / "manifest.json").exists())
        self.assertTrue((usb_dir / "clone-release" / "partition-01.img").exists())
        self.assertTrue((usb_dir / "deploy" / "restore.sh").exists())
        self.assertTrue((usb_dir / "deploy" / "afterburner.sh").exists())
        usb_grub = (usb_dir / "boot" / "grub" / "grub.cfg").read_text(encoding="utf-8")
        self.assertTrue(usb_img.exists())

        deploy = json.loads((usb_dir / "deploy.json").read_text(encoding="utf-8"))
        usb_readme = (usb_dir / "README.txt").read_text(encoding="utf-8")
        self.assertEqual(deploy["artifact_type"], "usb")
        self.assertEqual(deploy["payload_delivery"], "offline_usb")
        self.assertEqual(deploy["payload_hint"]["clone_manifest"], "clone-release/manifest.json")
        self.assertEqual(deploy["scaffold"]["restore_script"], "deploy/restore.sh")
        self.assertNotIn("http://builder", json.dumps(deploy))
        self.assertIn("inst.stage2=hd:LABEL=TUXWSDEPLOY:/stage2", usb_grub)
        self.assertIn("inst.repo=hd:LABEL=TUXWSDEPLOY:/", usb_grub)
        self.assertIn("inst.ks=hd:LABEL=TUXWSDEPLOY:/deploy/", usb_grub)
        self.assertIn("If needed, decompress usb.img.gz to usb.img", usb_readme)
        self.assertNotIn("__USB_REPO_URL__", usb_readme)
        self.assertIn("What clone-release is for", usb_readme)

        deploy_kickstart = (usb_dir / "deploy" / f"{self.build.name}-deploy.cfg").read_text(encoding="utf-8")
        self.assertNotIn("http://builder", deploy_kickstart)

    @patch("apps.builds.services.artifacts._run_checked")
    @patch("apps.builds.services.artifacts.shutil.which")
    def test_write_usb_image_fails_clearly_for_uefi_without_preservation_tool(self, mock_which, mock_run_checked):
        mock_which.return_value = None

        root = Path("/tmp/tuxwsmaker-test-usb-uefi-error")
        shutil.rmtree(root, ignore_errors=True)
        bundle_dir = root / "usb"
        (bundle_dir / "boot").mkdir(parents=True, exist_ok=True)
        output_path = root / "usb.img"
        source_iso_path = root / "rhel-10.2-x86_64-dvd.iso"
        source_iso_path.write_bytes(b"iso")

        with self.assertRaisesRegex(ArtifactExportError, "requires xorriso"):
            _write_usb_image_from_bundle(
                bundle_dir=bundle_dir,
                output_path=output_path,
                build_name=self.build.name,
                source_iso_path=source_iso_path,
                build_boot_mode="uefi",
            )

        mock_run_checked.assert_not_called()

    @patch("apps.builds.services.artifacts._partition_table_from_raw")
    @patch("apps.builds.services.artifacts._convert_qcow2_to_raw")
    def test_dump_clone_partitions_writes_deploy_metadata(self, mock_convert, mock_table):
        root = Path("/tmp/tuxwsmaker-test-clone-release")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        PartitionEntry.objects.create(
            layout=self.build.partition_layout,
            order=1,
            name="EFI",
            mount_point="/boot/efi",
            filesystem="efi",
            size_mode=PartitionEntry.SIZE_FIXED,
            size_mib=512,
            is_boot=True,
        )
        PartitionEntry.objects.create(
            layout=self.build.partition_layout,
            order=2,
            name="root",
            mount_point="/",
            filesystem="xfs",
            size_mode=PartitionEntry.SIZE_REMAINDER,
            luks_enabled=True,
            luks_name="rootfs",
        )

        def fake_convert(*, qcow2_path, raw_path):
            raw_path.write_bytes(b"ABCDEFGH")

        mock_convert.side_effect = fake_convert
        mock_table.return_value = {
            "table_type": "gpt",
            "disk_size_bytes": 8,
            "partitions": [
                {"number": 1, "start_byte": 0, "end_byte": 3, "size_bytes": 4, "filesystem": "fat32", "name": "EFI", "flags": "boot,esp"},
                {"number": 2, "start_byte": 4, "end_byte": 7, "size_bytes": 4, "filesystem": "xfs", "name": "root", "flags": ""},
            ],
        }

        dump_clone_partitions(
            build=self.build,
            qcow2_disk_path=root / "disk.qcow2",
            output_dir=root / "clone-release",
            compress=False,
        )

        manifest = json.loads((root / "clone-release" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["operating_system"]["family"], OperatingSystem.FAMILY_RHEL)
        self.assertEqual(manifest["deploy"]["strategy"], "partition_restore")
        self.assertEqual(manifest["deploy"]["advanced_layout_mode"], "safe-resize-only")
        self.assertEqual(manifest["deploy"]["boot"]["boot_entry_orders"], [1])
        self.assertEqual(manifest["deploy"]["mount_map"][0]["mount_point"], "/boot/efi")
        self.assertTrue(manifest["deploy"]["mount_map"][1]["luks_enabled"])
        self.assertEqual(manifest["partitions"][0]["payload_format"], "sparse-extents-v1")
        self.assertIn("extents_file", manifest["partitions"][0])
        self.assertIn("payload_size_bytes", manifest["partitions"][0])

    def test_render_deploy_restore_script_handles_encrypted_pv_and_lv_mounts(self):
        root = Path("/tmp/tuxwsmaker-test-restore-script-lvm")
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)

        render_deploy_restore_script(output_dir=root / "deploy", os_family=OperatingSystem.FAMILY_RHEL)
        script = (root / "deploy" / "restore.sh").read_text(encoding="utf-8")

        self.assertIn('[[ "$luks_enabled" == "True" ]]', script)
        self.assertIn('printf \'%s\' "$DEFAULT_LUKS_PASSWORD" | cryptsetup open "$part_dev" "$map_name" -', script)
        self.assertIn('ROOT_DEV="$(resolve_lv_path "$volume_group" "$logical_volume" || true)"', script)
        self.assertIn('source_dev="$(resolve_lv_path "$volume_group" "$logical_volume" || true)"', script)
        self.assertIn('resolve_lv_path() {', script)
        self.assertIn('str(entry.get("size_mode") or "fixed")', script)
        self.assertIn('DEFAULT_LUKS_PASSWORD="${DEFAULT_LUKS_PASSWORD:-tuxwsmaker}"', script)
        self.assertIn('cryptsetup luksFormat --type luks2 --batch-mode "$part_dev" -', script)
        self.assertIn('restore_target="/dev/mapper/$map_name"', script)
        self.assertIn('[[ "$entry_role" == "lv" && "$size_mode" == "remainder" ]]', script)
        self.assertIn('lvextend -l +100%FREE "$lv_path"', script)
        self.assertIn('case "$fs_type" in', script)
        self.assertIn('xfs_growfs "$grow_target"', script)

    @patch("apps.builds.services.artifacts._run_checked")
    @patch("apps.builds.services.artifacts.shutil.which")
    def test_write_usb_image_uses_xorriso_replay_for_uefi(self, mock_which, mock_run_checked):
        mock_which.side_effect = lambda tool: "/usr/bin/xorriso" if tool == "xorriso" else "/usr/bin/grub-mkrescue"

        root = Path("/tmp/tuxwsmaker-test-usb-uefi-xorriso")
        shutil.rmtree(root, ignore_errors=True)
        bundle_dir = root / "usb"
        (bundle_dir / "boot").mkdir(parents=True, exist_ok=True)
        output_path = root / "usb.img"
        source_iso_path = root / "rhel-10.2-x86_64-dvd.iso"
        source_iso_path.write_bytes(b"iso")

        _write_usb_image_from_bundle(
            bundle_dir=bundle_dir,
            output_path=output_path,
            build_name=self.build.name,
            source_iso_path=source_iso_path,
            build_boot_mode="uefi",
        )

        self.assertTrue((bundle_dir / "boot" / "grub" / "grub.cfg").exists())
        self.assertTrue((bundle_dir / "boot" / "grub2" / "grub.cfg").exists())
        self.assertTrue((bundle_dir / "EFI" / "BOOT" / "grub.cfg").exists())
        efi_grub_cfg = (bundle_dir / "EFI" / "BOOT" / "grub.cfg").read_text(encoding="utf-8")
        self.assertIn("menuentry 'TuxWSMaker Deploy USB'", efi_grub_cfg)
        self.assertIn("inst.ks=hd:LABEL=TUXWSDEPLOY:/deploy/", efi_grub_cfg)

        cmd = mock_run_checked.call_args.args[0]
        self.assertEqual(cmd[0], "xorriso")
        self.assertIn("-boot_image", cmd)
        boot_index = cmd.index("-boot_image")
        self.assertEqual(cmd[boot_index + 1], "any")
        self.assertEqual(cmd[boot_index + 2], "replay")
        self.assertIn("-volid", cmd)
        self.assertIn("TUXWSDEPLOY", cmd)
        self.assertIn("-map", cmd)
        self.assertIn(str(bundle_dir), cmd)
        self.assertIn("-rm_r", cmd)
        self.assertIn("/BaseOS", cmd)
        self.assertIn("/AppStream", cmd)
        self.assertIn("--", cmd)

    @patch("apps.builds.services.artifacts._run_checked")
    @patch("apps.builds.services.artifacts.shutil.which")
    def test_write_usb_image_uses_expected_volume_label(self, mock_which, mock_run_checked):
        mock_which.return_value = "/usr/bin/grub-mkrescue"

        root = Path("/tmp/tuxwsmaker-test-usb-label")
        shutil.rmtree(root, ignore_errors=True)
        bundle_dir = root / "usb"
        (bundle_dir / "boot").mkdir(parents=True, exist_ok=True)
        output_path = root / "usb.img"

        _write_usb_image_from_bundle(
            bundle_dir=bundle_dir,
            output_path=output_path,
            build_name=self.build.name,
            build_boot_mode="bios",
        )

        cmd = mock_run_checked.call_args.args[0]
        self.assertIn("-volid", cmd)
        self.assertIn("TUXWSDEPLOY", cmd)
