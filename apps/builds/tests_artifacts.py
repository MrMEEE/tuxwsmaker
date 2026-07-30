from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from apps.builds.models import BuildDefinition, BuildMachineConfig
from apps.builds.services.artifacts import generate_artifacts
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionLayout


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

    @patch("apps.builds.services.artifacts._export_usb_image")
    @patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
    def test_pxe_bundle_uses_extracted_boot_assets(self, mock_extract, _mock_usb):
        root = Path("/tmp/tuxwsmaker-test-artifacts")
        root.mkdir(parents=True, exist_ok=True)

        kernel = root / "fake-vmlinuz"
        initrd = root / "fake-initrd"
        kernel.write_text("k", encoding="utf-8")
        initrd.write_text("i", encoding="utf-8")
        mock_extract.return_value = (kernel, initrd)

        generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

        pxe_dir = root / f"build-{self.build.id}" / "pxe"
        bios = (pxe_dir / "pxelinux.cfg" / "default").read_text(encoding="utf-8")
        grub = (pxe_dir / "efi" / "grub.cfg").read_text(encoding="utf-8")

        self.assertIn("boot/fake-vmlinuz", bios)
        self.assertIn("boot/fake-initrd", bios)
        self.assertIn("boot/fake-vmlinuz", grub)
        self.assertIn("boot/fake-initrd", grub)
