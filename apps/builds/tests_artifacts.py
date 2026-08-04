from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from apps.builds.models import BuildDefinition, BuildMachineConfig
from apps.builds.services.artifacts import (
    ArtifactExportError,
    dump_clone_partitions,
    generate_artifacts,
    _write_usb_image_from_bundle,
)
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout


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
        self.assertIn("sfdisk --wipe always --wipe-partitions always", restore_script)
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
        self.assertIn("exec > >(tee /dev/console) 2>&1", deploy_kickstart)
        self.assertNotIn('DEPLOY_TTY="/dev/tty6"', deploy_kickstart)
        self.assertNotIn("chvt 6", deploy_kickstart)
        self.assertIn("read -r _ < /dev/console", deploy_kickstart)
        self.assertIn("bash /tmp/restore.sh", deploy_kickstart)
        self.assertNotIn("%packages", deploy_kickstart)
        self.assertIn("Missing local restore script", deploy_kickstart)
        self.assertNotIn("curl -fsSL", deploy_kickstart)
        self.assertIn("How to place on PXE server", pxe_readme)
        self.assertIn("What clone-release is for", pxe_readme)
        self.assertIn("Set __PXE_BASE_URL__", pxe_readme)
        self.assertTrue((pxe_dir / "efi" / "shimx64.efi").exists())
        self.assertIn("signed UEFI chain assets", pxe_readme)
        self.assertNotIn("__PXE_REPO_URL__", pxe_readme)
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
