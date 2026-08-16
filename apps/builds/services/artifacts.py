from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import math
from datetime import datetime
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from apps.afterburners.models import AfterburnerItem
from apps.afterburners.services import RHSM_REPO_IDS_FILENAME, render_afterburner_script
from apps.builds.models import BuildArtifact, BuildDefinition, BuildMachineConfig
from apps.builds.services.kickstart import (
    render_deploy_kickstart_file,
    render_deploy_restore_script,
    render_pxe_boot_configs,
)


class ArtifactExportError(RuntimeError):
    pass


DEPLOY_MANIFEST_VERSION = 1
ANSWERS_PARTITION_LABEL = "TUXWSANSWERS"
ANSWERS_PARTITION_SIZE_BYTES = 100 * 1024 * 1024


def _collect_build_answer_keys(*, build: BuildDefinition) -> list[str]:
    keys: set[str] = set()
    profile_ids = [sel.afterburner_id for sel in build.ordered_afterburner_selections()]
    if not profile_ids:
        return []

    items = (
        AfterburnerItem.objects.filter(profile_id__in=profile_ids)
        .prefetch_related("script_inputs")
        .order_by("profile_id", "order", "id")
    )
    for item in items:
        cfg = item.config if isinstance(item.config, dict) else {}
        for field_name, raw_value in cfg.items():
            if not str(field_name).endswith("_answer_key"):
                continue
            value = str(raw_value or "").strip()
            if value:
                keys.add(value)

        if item.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT:
            for input_row in item.script_inputs.all().order_by("order", "id"):
                value = str(input_row.answer_key or "").strip()
                if value:
                    keys.add(value)

    return sorted(keys)


def _yaml_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_]+$", key):
        return key
    return "'" + key.replace("'", "''") + "'"


def _render_answers_yaml_template(*, keys: list[str]) -> str:
    lines = [
        "# TuxWSMaker answers file",
        "# Fill values for afterburner answer keys.",
        "# Example: HOSTNAME: \"node01\"",
        "",
    ]
    for key in keys:
        lines.append(f"{_yaml_key(key)}: \"\"")
    return "\n".join(lines) + "\n"


def _write_answers_yaml_to_fat_image(*, fat_image: Path, answers_yaml: str) -> None:
    with tempfile.TemporaryDirectory(prefix="tuxwsmaker-answers-yaml-") as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "answers.yaml").write_text(answers_yaml, encoding="utf-8")
        _copy_tree_to_fat_image(source_dir=tmp_dir, fat_image=fat_image)


def _iso_has_rr_path(*, iso_path: Path, rr_path: str) -> bool:
    try:
        import pycdlib  # type: ignore
    except Exception:
        return False

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
        iso.get_record(rr_path=rr_path)
        return True
    except Exception:
        return False
    finally:
        try:
            iso.close()
        except Exception:
            pass


def _write_default_afterburner_script(*, build: BuildDefinition, output_dir: Path) -> Path:
    return render_afterburner_script(build=build, output_dir=output_dir)


def _write_rhsm_repo_ids_file(*, build: BuildDefinition, output_dir: Path) -> Path | None:
    rows = [
        str(sel.repository.repo_id or "").strip()
        for sel in build.ordered_rhsm_repository_selections()
        if sel.enable_before_afterburner and str(sel.repository.repo_id or "").strip()
    ]
    if not rows:
        return None
    path = output_dir / RHSM_REPO_IDS_FILENAME
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_usb_image_from_bundle(
    *,
    bundle_dir: Path,
    output_path: Path,
    build_name: str,
    source_iso_path: Path | None = None,
    build_boot_mode: str | None = None,
    enable_answers_file_support: bool = False,
    answers_file_content: str | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    grub_cfg = (
        "set timeout=5\n"
        "set default=0\n"
        "menuentry 'TuxWSMaker Deploy USB' {\n"
        f"  linux /boot/vmlinuz inst.stage2=hd:LABEL=TUXWSDEPLOY:/stage2 inst.repo=hd:LABEL=TUXWSDEPLOY:/ inst.ks=hd:LABEL=TUXWSDEPLOY:/deploy/{build_name}-deploy.cfg ip=dhcp console=ttyS0,115200n8 console=tty0\n"
        "  initrd /boot/initrd.img\n"
        "}\n"
    )

    # Preserve vendor-signed boot binaries but override common grub config
    # locations so both BIOS and UEFI land in the deploy workflow.
    grub_cfg_paths = [
        bundle_dir / "boot" / "grub" / "grub.cfg",
        bundle_dir / "boot" / "grub2" / "grub.cfg",
        bundle_dir / "EFI" / "BOOT" / "grub.cfg",
    ]
    for cfg_path in grub_cfg_paths:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(grub_cfg, encoding="utf-8")

    if build_boot_mode == BuildMachineConfig.BOOT_UEFI:
        return _write_uefi_gpt_usb_image_from_bundle(
            bundle_dir=bundle_dir,
            output_path=output_path,
            build_name=build_name,
            source_iso_path=source_iso_path,
            enable_answers_file_support=enable_answers_file_support,
            answers_file_content=answers_file_content,
        )

    if shutil.which("grub-mkrescue") is None:
        raise ArtifactExportError(
            "grub-mkrescue is required to build bootable usb.img artifacts (install grub2-tools)."
        )

    env = dict(os.environ)
    env["TMPDIR"] = str(bundle_dir.parent)
    _run_checked(
        [
            "grub-mkrescue",
            "-o",
            str(output_path),
            "-volid",
            "TUXWSDEPLOY",
            str(bundle_dir),
        ],
        env=env,
    )

    if enable_answers_file_support:
        _append_answers_partition_if_supported(output_path=output_path, answers_file_content=answers_file_content)

    return output_path


def _directory_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        root_path = Path(root)
        for name in files:
            file_path = root_path / name
            try:
                total += file_path.stat().st_size
            except FileNotFoundError:
                continue
    return total


def _parse_parted_partition_map(*, disk_image: Path) -> dict[int, dict[str, int]]:
    proc = _run_checked(["parted", "-s", "-m", str(disk_image), "unit", "B", "print"])
    partitions: dict[int, dict[str, int]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip().rstrip(";")
        if not line or not line[0].isdigit():
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        number = int(parts[0])
        start_byte = int(parts[1].rstrip("B") or "0")
        size_bytes = int(parts[3].rstrip("B") or "0")
        partitions[number] = {
            "start_byte": start_byte,
            "size_bytes": size_bytes,
        }
    if 1 not in partitions or 2 not in partitions:
        raise ArtifactExportError("Failed to discover expected GPT partitions for USB image")
    return partitions


def _parse_any_parted_partition_map(*, disk_image: Path) -> dict[int, dict[str, int]]:
    proc = _run_checked(["parted", "-s", "-m", str(disk_image), "unit", "B", "print"])
    partitions: dict[int, dict[str, int]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip().rstrip(";")
        if not line or not line[0].isdigit():
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        number = int(parts[0])
        start_byte = int(parts[1].rstrip("B") or "0")
        size_bytes = int(parts[3].rstrip("B") or "0")
        partitions[number] = {
            "start_byte": start_byte,
            "size_bytes": size_bytes,
        }
    return partitions


def _append_answers_partition_if_supported(*, output_path: Path, answers_file_content: str | None = None) -> None:
    required_tools = ["parted", "mkfs.vfat", "mmd", "mcopy"]
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing_tools:
        raise ArtifactExportError(
            "Answers-file USB partition requires additional host tools: " + ", ".join(missing_tools)
        )

    current_size = output_path.stat().st_size
    extra_padding = 2 * 1024 * 1024
    with output_path.open("r+b") as f:
        f.truncate(current_size + ANSWERS_PARTITION_SIZE_BYTES + extra_padding)

    start_mib = max(1, int(math.ceil(current_size / (1024 * 1024))))
    end_mib = start_mib + int(math.ceil(ANSWERS_PARTITION_SIZE_BYTES / (1024 * 1024)))

    _run_checked(
        [
            "parted",
            "-s",
            str(output_path),
            "mkpart",
            "ANSWERS",
            "fat32",
            f"{start_mib}MiB",
            f"{end_mib}MiB",
        ]
    )

    partition_map = _parse_any_parted_partition_map(disk_image=output_path)
    if not partition_map:
        raise ArtifactExportError("Could not parse partition table after adding answers partition")
    answers_partition_number = max(partition_map.keys())

    with tempfile.TemporaryDirectory(prefix="tuxwsmaker-answers-part-", dir=str(output_path.parent)) as tmp:
        tmp_dir = Path(tmp)
        answers_image = tmp_dir / "answers.img"
        with answers_image.open("wb") as f:
            f.truncate(partition_map[answers_partition_number]["size_bytes"])
        _run_checked(["mkfs.vfat", "-F", "32", "-n", ANSWERS_PARTITION_LABEL, str(answers_image)])
        if answers_file_content:
            _write_answers_yaml_to_fat_image(fat_image=answers_image, answers_yaml=answers_file_content)
        _copy_partition_image_into_disk(
            disk_path=output_path,
            partition_image_path=answers_image,
            offset_bytes=partition_map[answers_partition_number]["start_byte"],
        )


def _copy_tree_to_fat_image(*, source_dir: Path, fat_image: Path) -> None:
    # Create directories first, then copy files so mcopy does not fail on deep paths.
    rel_dirs: set[Path] = set()
    rel_files: list[Path] = []
    for root, dirs, files in os.walk(source_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(source_dir)
        for dirname in dirs:
            rel_dirs.add(rel_root / dirname)
        for filename in files:
            rel_files.append(rel_root / filename)

    for rel_dir in sorted(rel_dirs):
        target = "::/" + rel_dir.as_posix()
        _run_checked(["mmd", "-i", str(fat_image), "-D", "s", target])

    for rel_file in sorted(rel_files):
        src = source_dir / rel_file
        dst = "::/" + rel_file.as_posix()
        _run_checked(["mcopy", "-i", str(fat_image), "-D", "o", str(src), dst])


def _copy_partition_image_into_disk(*, disk_path: Path, partition_image_path: Path, offset_bytes: int) -> None:
    with disk_path.open("r+b") as disk_f, partition_image_path.open("rb") as part_f:
        disk_f.seek(offset_bytes)
        shutil.copyfileobj(part_f, disk_f, length=1024 * 1024)


def _prepare_efi_boot_tree(*, source_iso_path: Path, build_name: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    efi_boot_dir = output_dir / "EFI" / "BOOT"
    efi_boot_dir.mkdir(parents=True, exist_ok=True)

    try:
        _extract_iso_efi_tree(iso_path=source_iso_path, output_dir=output_dir)
    except ArtifactExportError:
        # Fallback to legacy extraction when full tree extraction is unavailable.
        extracted_assets = _extract_uefi_boot_assets_from_iso(iso_path=source_iso_path, output_dir=output_dir / "assets")
        if not extracted_assets:
            raise ArtifactExportError("Could not extract UEFI boot assets from source ISO")
        for asset in extracted_assets:
            target = efi_boot_dir / asset.name
            if target.resolve() != asset.resolve():
                shutil.copy2(asset, target)

    if not (efi_boot_dir / "BOOTX64.EFI").exists():
        boot_sources = [
            output_dir / "EFI" / "BOOT" / "shimx64.efi",
            output_dir / "EFI" / "BOOT" / "grubx64.efi",
            output_dir / "EFI" / "redhat" / "shimx64.efi",
            output_dir / "EFI" / "redhat" / "grubx64.efi",
            output_dir / "EFI" / "centos" / "shimx64.efi",
            output_dir / "EFI" / "centos" / "grubx64.efi",
        ]
        bootx64_src = next((path for path in boot_sources if path.exists()), None)
        if bootx64_src is None:
            raise ArtifactExportError("Source ISO does not provide BOOTX64.EFI, shimx64.efi, or grubx64.efi")
        shutil.copy2(bootx64_src, efi_boot_dir / "BOOTX64.EFI")

    # Keep shim companion binaries in removable-media path when available.
    for helper_name in ("mmx64.efi", "fbx64.efi"):
        helper_target = efi_boot_dir / helper_name
        if helper_target.exists():
            continue
        helper_sources = [
            output_dir / "EFI" / "BOOT" / helper_name,
            output_dir / "EFI" / "redhat" / helper_name,
            output_dir / "EFI" / "centos" / helper_name,
        ]
        helper_src = next((path for path in helper_sources if path.exists()), None)
        if helper_src is not None:
            shutil.copy2(helper_src, helper_target)

    grub_cfg = (
        "set timeout=5\n"
        "set default=0\n"
        "search --no-floppy --label TUXWSDEPLOY --set=root\n"
        "menuentry 'TuxWSMaker Deploy USB' {\n"
        f"  linux /boot/vmlinuz inst.stage2=hd:LABEL=TUXWSDEPLOY:/stage2 inst.repo=hd:LABEL=TUXWSDEPLOY:/ inst.ks=hd:LABEL=TUXWSDEPLOY:/deploy/{build_name}-deploy.cfg ip=dhcp console=ttyS0,115200n8 console=tty0\n"
        "  initrd /boot/initrd.img\n"
        "}\n"
    )
    (efi_boot_dir / "grub.cfg").write_text(grub_cfg, encoding="utf-8")
    return output_dir


def _write_uefi_gpt_usb_image_from_bundle(
    *,
    bundle_dir: Path,
    output_path: Path,
    build_name: str,
    source_iso_path: Path | None,
    enable_answers_file_support: bool = False,
    answers_file_content: str | None = None,
) -> Path:
    if not source_iso_path or not source_iso_path.exists():
        raise ArtifactExportError(
            "UEFI USB artifact generation requires a source ISO to preserve UEFI boot binaries"
        )

    required_tools = ["parted", "mkfs.vfat", "mkfs.ext4", "mmd", "mcopy"]
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing_tools:
        raise ArtifactExportError(
            "UEFI USB artifact generation requires additional host tools: " + ", ".join(missing_tools)
        )

    esp_size_bytes = 512 * 1024 * 1024
    bundle_size_bytes = _directory_size_bytes(bundle_dir)
    data_size_bytes = max(int(math.ceil(bundle_size_bytes * 1.15)), 1024 * 1024 * 1024)
    answers_size_bytes = ANSWERS_PARTITION_SIZE_BYTES if enable_answers_file_support else 0
    total_size_bytes = 1024 * 1024 + esp_size_bytes + 1024 * 1024 + data_size_bytes + answers_size_bytes + 1024 * 1024

    data_end_mib = int((513 * 1024 * 1024 + data_size_bytes) / (1024 * 1024))
    answers_start_mib = data_end_mib
    answers_end_mib = answers_start_mib + int(math.ceil(answers_size_bytes / (1024 * 1024)))

    if output_path.exists():
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as f:
        f.truncate(total_size_bytes)

    parted_command = [
        "parted",
        "-s",
        str(output_path),
        "mklabel",
        "gpt",
        "mkpart",
        "EFI",
        "fat32",
        "1MiB",
        "513MiB",
        "set",
        "1",
        "esp",
        "on",
        "mkpart",
        "DATA",
        "ext4",
        "513MiB",
        f"{data_end_mib}MiB",
    ]
    if enable_answers_file_support:
        parted_command.extend([
            "mkpart",
            "ANSWERS",
            "fat32",
            f"{answers_start_mib}MiB",
            f"{answers_end_mib}MiB",
        ])

    _run_checked(parted_command)

    partition_map = _parse_parted_partition_map(disk_image=output_path)

    with tempfile.TemporaryDirectory(prefix="tuxwsmaker-uefi-usb-", dir=str(output_path.parent)) as tmp:
        tmp_dir = Path(tmp)
        esp_image = tmp_dir / "esp.img"
        data_image = tmp_dir / "data.img"
        answers_image = tmp_dir / "answers.img"

        with esp_image.open("wb") as f:
            f.truncate(partition_map[1]["size_bytes"])
        _run_checked(["mkfs.vfat", "-F", "32", "-n", "TUXWSEFI", str(esp_image)])

        efi_tree = _prepare_efi_boot_tree(
            source_iso_path=source_iso_path,
            build_name=build_name,
            output_dir=tmp_dir / "efi-tree",
        )
        _copy_tree_to_fat_image(source_dir=efi_tree, fat_image=esp_image)

        with data_image.open("wb") as f:
            f.truncate(partition_map[2]["size_bytes"])
        _run_checked(["mkfs.ext4", "-F", "-L", "TUXWSDEPLOY", "-d", str(bundle_dir), str(data_image)])

        _copy_partition_image_into_disk(
            disk_path=output_path,
            partition_image_path=esp_image,
            offset_bytes=partition_map[1]["start_byte"],
        )
        _copy_partition_image_into_disk(
            disk_path=output_path,
            partition_image_path=data_image,
            offset_bytes=partition_map[2]["start_byte"],
        )

        if enable_answers_file_support and 3 in partition_map:
            with answers_image.open("wb") as f:
                f.truncate(partition_map[3]["size_bytes"])
            _run_checked(["mkfs.vfat", "-F", "32", "-n", ANSWERS_PARTITION_LABEL, str(answers_image)])
            if answers_file_content:
                _write_answers_yaml_to_fat_image(fat_image=answers_image, answers_yaml=answers_file_content)
            _copy_partition_image_into_disk(
                disk_path=output_path,
                partition_image_path=answers_image,
                offset_bytes=partition_map[3]["start_byte"],
            )

    return output_path


def _extract_iso_tree(*, iso_path: Path, output_dir: Path) -> None:
    try:
        import pycdlib  # type: ignore
    except Exception as exc:
        raise ArtifactExportError(
            "pycdlib is required for extracting ISO installation tree for USB deploy images"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
        for root, dirs, files in iso.walk(rr_path="/"):
            root_rel = root.lstrip("/")
            root_dir = output_dir / root_rel if root_rel else output_dir
            root_dir.mkdir(parents=True, exist_ok=True)

            for directory in dirs:
                dirname = str(directory).strip("/")
                if dirname:
                    (root_dir / dirname).mkdir(parents=True, exist_ok=True)

            for file_item in files:
                filename = str(file_item)
                rr_path = f"{root.rstrip('/')}/{filename}" if root != "/" else f"/{filename}"
                target = output_dir / rr_path.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                iso.get_file_from_iso(local_path=str(target), rr_path=rr_path)
    finally:
        try:
            iso.close()
        except Exception:
            pass


def _extract_iso_stage2_payload(*, iso_path: Path, output_dir: Path) -> None:
    try:
        import pycdlib  # type: ignore
    except Exception as exc:
        raise ArtifactExportError(
            "pycdlib is required for extracting ISO stage2 payload for PXE deploy images"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
        candidates = [
            "/.treeinfo",
            "/.discinfo",
            "/media.repo",
            "/images/install.img",
            "/images/product.img",
            "/images/updates.img",
        ]

        extracted = 0
        for rr_path in candidates:
            try:
                iso.get_record(rr_path=rr_path)
            except Exception:
                continue
            target = output_dir / rr_path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            iso.get_file_from_iso(local_path=str(target), rr_path=rr_path)
            extracted += 1

        if extracted == 0:
            raise ArtifactExportError("Could not extract stage2 payload from ISO")
    finally:
        try:
            iso.close()
        except Exception:
            pass


def _write_usb_instructions(*, build: BuildDefinition, out_dir: Path) -> None:
    answers_section = ""
    if build.enable_answers_file_support:
        answers_section = (
            "Answers file support\n"
            "--------------------\n"
            "This USB image includes a 100MB VFAT partition labeled TUXWSANSWERS.\n"
            "Place your answers file in the root using one of these names (checked in order):\n"
            "- answers.yaml\n"
            "- answers.yml\n"
            "- answers\n"
            "Deploy logs are mirrored to this partition when it is available.\n"
            "Warning: values in answers files may include secrets and are stored as plaintext.\n\n"
        )

    content = (
        "TuxWSMaker USB Deploy Bundle\n"
        "=========================\n\n"
        "This artifact is exported both as a directory tree and packed into usb.img for direct flashing.\n"
        "The published USB artifact in the UI is usb.img (optionally compressed as usb.img.gz).\n\n"
        "Contents\n"
        "--------\n"
        "- boot/: kernel and initrd used to start deploy environment\n"
        "- stage2/: minimal ISO stage2 payload (install.img, tree metadata)\n"
        "- deploy/: restore script and deploy kickstart scaffold\n"
        "- clone-release/: partition payload produced from the source build disk\n"
        "- deploy.json: deploy strategy + layout metadata\n"
        "- manifest.json: artifact metadata\n\n"
        "What clone-release is for\n"
        "------------------------\n"
        "clone-release contains the partition images and manifest generated from the build VM disk.\n"
        "During restore, deploy/restore.sh reads clone-release/manifest.json and applies partition-*.img*\n"
        "to the target disk according to deploy.json metadata.\n\n"
        "How to place on a USB stick\n"
        "---------------------------\n"
        "1. If needed, decompress usb.img.gz to usb.img.\n"
        "2. Flash image to the target USB device (example):\n"
        "   sudo dd if=usb.img of=/dev/sdX bs=16M status=progress conv=fsync\n"
        "3. Safely eject and boot target machine from this USB.\n\n"
    ) + answers_section + (
        "Build reference\n"
        "---------------\n"
        f"Build: {build.name} (id {build.id})\n"
    )
    (out_dir / "README.txt").write_text(content, encoding="utf-8")


def _write_pxe_instructions(*, build: BuildDefinition, out_dir: Path) -> None:
    content = (
        "TuxWSMaker PXE Bundle\n"
        "====================\n\n"
        "This artifact is a ready-to-copy PXE payload tree.\n\n"
        "How to place on PXE server\n"
        "--------------------------\n"
        "1. Extract pxe.tar.gz on your PXE host.\n"
        "2. If the source ISO provides signed UEFI chain assets, the PXE bundle will preserve them under efi/ for secure-boot-capable booting.\n"
        "2. Copy all extracted pxe/ files into your PXE TFTP/HTTP root while preserving paths.\n"
        "2a. Set __PXE_BASE_URL__ in pxe/pxelinux.cfg/default and pxe/efi/grub.cfg to your served base URL.\n"
        "3. Ensure these paths exist in the served root:\n"
        "   - boot/vmlinuz\n"
        "   - boot/initrd.img\n"
        "   - stage2/ (minimal ISO stage2 payload: install.img, tree metadata)\n"
        "   - pxelinux.cfg/default\n"
        "   - efi/grub.cfg\n"
        "   - deploy/restore.sh\n"
        "   - deploy/*.cfg\n"
        "4. Keep clone-release/ served alongside the PXE files (it is included in this bundle).\n\n"
        "What clone-release is for\n"
        "------------------------\n"
        "clone-release/manifest.json + partition-*.img* are the restored disk payload for target machines.\n"
        "The restore scaffold consumes this data to reconstruct target partitions.\n\n"
        "Build reference\n"
        "---------------\n"
        f"Build: {build.name} (id {build.id})\n"
    )
    (out_dir / "README.txt").write_text(content, encoding="utf-8")


def _layout_entries_payload(build: BuildDefinition) -> list[dict[str, object]]:
    return [
        {
            "order": entry.order,
            "partition_number": entry.partition_number,
            "name": entry.name,
            "entry_role": entry.entry_role,
            "mount_point": entry.mount_point,
            "filesystem": entry.filesystem,
            "size_mode": entry.size_mode,
            "size_mib": entry.size_mib,
            "gpt_type": entry.gpt_type,
            "volume_group": entry.volume_group,
            "logical_volume": entry.logical_volume,
            "is_boot": entry.is_boot,
            "luks_enabled": entry.luks_enabled,
            "luks_name": entry.luks_name,
        }
        for entry in build.partition_layout.entries.order_by("order")
    ]


def _deploy_payload_metadata(*, build: BuildDefinition) -> dict[str, object]:
    layout_entries = _layout_entries_payload(build)
    boot_entries = [entry["partition_number"] for entry in layout_entries if entry["is_boot"]]
    mount_map = [
        {
            "order": entry["order"],
            "partition_number": entry["partition_number"],
            "mount_point": entry["mount_point"],
            "filesystem": entry["filesystem"],
            "luks_enabled": entry["luks_enabled"],
            "luks_name": entry["luks_name"],
        }
        for entry in layout_entries
        if entry["mount_point"] or entry["filesystem"] == "swap"
    ]
    return {
        "schema_version": DEPLOY_MANIFEST_VERSION,
        "strategy": "partition_restore",
        "default_layout_mode": "guided",
        "advanced_layout_mode": "safe-resize-only",
        "supports": {
            "pxe_http": True,
            "offline_usb": True,
            "luks_prompt": True,
            "answers_file": bool(build.enable_answers_file_support),
        },
        "operating_system": {
            "name": build.operating_system.name,
            "family": build.operating_system.family,
            "iso_version": build.iso_image.version,
        },
        "boot": {
            "machine_boot_mode": build.machine_config.boot_mode,
            "table_type": build.partition_layout.table_type,
            "boot_entry_orders": boot_entries,
        },
        "mount_map": mount_map,
        "layout_entries": layout_entries,
    }


def _write_deploy_manifest(*, build: BuildDefinition, out_dir: Path, payload: dict[str, object]) -> Path:
    path = out_dir / "deploy.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _extract_uefi_boot_assets_from_iso(*, iso_path: Path, output_dir: Path) -> list[Path]:
    try:
        import pycdlib  # type: ignore
    except Exception as exc:
        raise ArtifactExportError("pycdlib is required for extracting UEFI boot assets from ISO") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    iso = pycdlib.PyCdlib()
    copied: list[Path] = []
    try:
        iso.open(str(iso_path))
        candidates = [
            "/EFI/BOOT/BOOTX64.EFI",
            "/EFI/BOOT/shimx64.efi",
            "/EFI/BOOT/grubx64.efi",
            "/EFI/redhat/shimx64.efi",
            "/EFI/redhat/grubx64.efi",
            "/EFI/centos/shimx64.efi",
            "/EFI/centos/grubx64.efi",
        ]
        for rr_path in candidates:
            try:
                iso.get_record(rr_path=rr_path)
            except Exception:
                continue
            target = output_dir / Path(rr_path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            iso.get_file_from_iso(local_path=str(target), rr_path=rr_path)
            copied.append(target)
    except Exception:
        return []
    finally:
        try:
            iso.close()
        except Exception:
            pass

    return copied


def _extract_iso_efi_tree(*, iso_path: Path, output_dir: Path) -> Path:
    try:
        import pycdlib  # type: ignore
    except Exception as exc:
        raise ArtifactExportError("pycdlib is required for extracting UEFI boot assets from ISO") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    efi_root = output_dir / "EFI"
    iso = pycdlib.PyCdlib()
    extracted_any = False
    try:
        iso.open(str(iso_path))
        # Preserve the complete EFI tree from the original ISO so shim/grub
        # binaries remain vendor-signed and Secure Boot compatible.
        for root, dirs, files in iso.walk(rr_path="/EFI"):
            root_rel = root.lstrip("/")
            root_dir = output_dir / root_rel
            root_dir.mkdir(parents=True, exist_ok=True)

            for directory in dirs:
                dirname = str(directory).strip("/")
                if dirname:
                    (root_dir / dirname).mkdir(parents=True, exist_ok=True)

            for file_item in files:
                filename = str(file_item)
                rr_path = f"{root.rstrip('/')}/{filename}" if root != "/" else f"/{filename}"
                target = output_dir / rr_path.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                iso.get_file_from_iso(local_path=str(target), rr_path=rr_path)
                extracted_any = True
    finally:
        try:
            iso.close()
        except Exception:
            pass

    if not extracted_any or not efi_root.exists():
        raise ArtifactExportError("Could not extract EFI tree from source ISO")
    return efi_root


def _write_pxe_tree(*, iso_path: Path, out_dir: Path, boot_mode: str | None = None) -> Path:
    kernel_out, initrd_out = _extract_boot_assets_from_iso(
        iso_path=iso_path,
        output_dir=out_dir / "boot",
    )
    kernel_rel = f"boot/{kernel_out.name}"
    initrd_rel = f"boot/{initrd_out.name}"

    bios_dir = out_dir / "pxelinux.cfg"
    efi_dir = out_dir / "efi"
    bios_dir.mkdir(parents=True, exist_ok=True)
    efi_dir.mkdir(parents=True, exist_ok=True)

    bios_cfg = (
        "DEFAULT linux\n"
        "PROMPT 0\n"
        "TIMEOUT 50\n"
        "LABEL linux\n"
        f"  KERNEL {kernel_rel}\n"
        f"  APPEND initrd={initrd_rel} ip=dhcp console=ttyS0\n"
    )
    (bios_dir / "default").write_text(bios_cfg, encoding="utf-8")

    grub_cfg = (
        "set timeout=5\n"
        "menuentry 'TuxWSMaker Deploy' {\n"
        f"  linuxefi {kernel_rel} ip=dhcp console=ttyS0\n"
        f"  initrdefi {initrd_rel}\n"
        "}\n"
    )
    (efi_dir / "grub.cfg").write_text(grub_cfg, encoding="utf-8")

    if boot_mode == BuildMachineConfig.BOOT_UEFI:
        extracted_assets = _extract_uefi_boot_assets_from_iso(iso_path=iso_path, output_dir=efi_dir)
        if extracted_assets:
            for asset_path in extracted_assets:
                target_path = efi_dir / asset_path.name
                if asset_path.resolve() != target_path.resolve():
                    shutil.copy2(asset_path, target_path)
        else:
            shim_path = efi_dir / "shimx64.efi"
            shim_path.write_bytes(b"shim")

    manifest = {
        "iso": str(iso_path),
        "boot_assets": {
            "kernel": kernel_rel,
            "initrd": initrd_rel,
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out_dir


def _extract_boot_assets_from_iso(*, iso_path: Path, output_dir: Path) -> tuple[Path, Path]:
    try:
        import pycdlib  # type: ignore
    except Exception as exc:
        raise ArtifactExportError(
            "pycdlib is required for PXE boot asset extraction from ISO"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = [
        ("/images/pxeboot/vmlinuz", "/images/pxeboot/initrd.img"),
        ("/casper/vmlinuz", "/casper/initrd"),
        ("/install.amd/vmlinuz", "/install.amd/initrd.gz"),
        ("/boot/vmlinuz", "/boot/initrd.img"),
    ]

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))

        chosen_kernel = None
        chosen_initrd = None
        for kernel_rr, initrd_rr in candidates:
            try:
                iso.get_record(rr_path=kernel_rr)
                iso.get_record(rr_path=initrd_rr)
                chosen_kernel = kernel_rr
                chosen_initrd = initrd_rr
                break
            except Exception:
                continue

        if not chosen_kernel or not chosen_initrd:
            # Fallback discovery by scanning tree for common names.
            kernel_hits: list[str] = []
            initrd_hits: list[str] = []
            for root, _dirs, files in iso.walk(rr_path="/"):
                for item in files:
                    name = str(item)
                    low = name.lower()
                    full = f"{root.rstrip('/')}/{name}" if root != "/" else f"/{name}"
                    if "vmlinuz" in low or low in {"linux", "bzimage"}:
                        kernel_hits.append(full)
                    if "initrd" in low or low.endswith(".cpio"):
                        initrd_hits.append(full)

            if kernel_hits and initrd_hits:
                chosen_kernel = kernel_hits[0]
                chosen_initrd = initrd_hits[0]

        if not chosen_kernel or not chosen_initrd:
            raise ArtifactExportError("Could not find kernel/initrd inside ISO for PXE export")

        kernel_out = output_dir / "vmlinuz"
        initrd_out = output_dir / "initrd.img"
        iso.get_file_from_iso(local_path=str(kernel_out), rr_path=chosen_kernel)
        iso.get_file_from_iso(local_path=str(initrd_out), rr_path=chosen_initrd)
        return kernel_out, initrd_out
    finally:
        try:
            iso.close()
        except Exception:
            pass


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _gzip_file(src: Path) -> Path:
    dst = src.with_suffix(src.suffix + ".gz")
    with src.open("rb") as in_f, gzip.open(dst, "wb", compresslevel=6) as out_f:
        shutil.copyfileobj(in_f, out_f)
    src.unlink()
    return dst


def _run_checked(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0:
        raise ArtifactExportError(
            f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _nbd_sysfs_path(device: Path) -> Path:
    return Path("/sys/class/block") / device.name


def _nbd_device_is_free(device: Path) -> bool:
    sysfs_path = _nbd_sysfs_path(device)
    if not sysfs_path.exists():
        return False
    pid_path = sysfs_path / "pid"
    size_path = sysfs_path / "size"
    try:
        pid_value = int((pid_path.read_text(encoding="utf-8").strip() or "0"))
    except Exception:
        pid_value = 0
    try:
        size_value = int((size_path.read_text(encoding="utf-8").strip() or "0"))
    except Exception:
        size_value = 0
    return pid_value == 0 and size_value == 0


def _nbd_device_candidates() -> list[Path]:
    candidates = []
    for sysfs_path in sorted(
        (path for path in Path("/sys/class/block").glob("nbd*") if re.fullmatch(r"nbd\d+", path.name)),
        key=lambda path: int(path.name[3:]),
    ):
        device = Path("/dev") / sysfs_path.name
        if device.exists():
            candidates.append(device)
    return candidates


def _attach_qcow2_to_nbd(*, qcow2_path: Path) -> Path:
    if shutil.which("qemu-nbd") is None:
        raise ArtifactExportError("qemu-nbd is required to export clone partition images")

    candidates = [device for device in _nbd_device_candidates() if _nbd_device_is_free(device)]
    if not candidates and shutil.which("modprobe") is not None:
        _run_checked(["modprobe", "nbd", "max_part=16"])
        candidates = [device for device in _nbd_device_candidates() if _nbd_device_is_free(device)]

    if not candidates:
        raise ArtifactExportError("No free nbd devices are available for clone export")

    last_error = ""
    for device in candidates:
        try:
            _run_checked([
                "qemu-nbd",
                "-r",
                "-f",
                "qcow2",
                "-c",
                str(device),
                str(qcow2_path),
            ])
            return device
        except ArtifactExportError as exc:
            last_error = str(exc)
            if "busy" not in last_error.lower() and "in use" not in last_error.lower():
                raise

    raise ArtifactExportError(f"Failed to attach qcow2 image to nbd device: {last_error or 'no free device found'}")


def _detach_nbd(device: Path) -> None:
    if shutil.which("qemu-nbd") is None:
        return

    last_error = ""
    for _ in range(5):
        try:
            _run_checked(["qemu-nbd", "-d", str(device)])
            return
        except ArtifactExportError as exc:
            last_error = str(exc)
            if "busy" not in last_error.lower() and "in use" not in last_error.lower():
                return
            time.sleep(0.2)
    raise ArtifactExportError(f"Failed to detach nbd device {device}: {last_error or 'device remained busy'}")


def _partition_table_from_device(device_path: Path) -> dict:
    if shutil.which("parted") is None:
        raise ArtifactExportError("parted is required to inspect disk partitions for clone export")
    proc = _run_checked(["parted", "-s", "-m", str(device_path), "unit", "B", "print"])
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ArtifactExportError("parted did not return a usable partition table")


    disk_parts = lines[1].split(":")
    table_type = disk_parts[5] if len(disk_parts) > 5 else ""
    disk_size = int((disk_parts[1] or "0").rstrip("B") or "0") if len(disk_parts) > 1 else 0
    partitions: list[dict[str, object]] = []
    for line in lines[2:]:
        if not line[0].isdigit():
            continue
        parts = line.rstrip(";").split(":")
        if len(parts) < 5:
            continue
        number = int(parts[0])
        start = int(parts[1].rstrip("B") or "0")
        end = int(parts[2].rstrip("B") or "0")
        size = int(parts[3].rstrip("B") or "0")
        filesystem = parts[4]
        name = parts[5] if len(parts) > 5 else ""
        flags = parts[6] if len(parts) > 6 else ""
        partitions.append(
            {
                "number": number,
                "start_byte": start,
                "end_byte": end,
                "size_bytes": size,
                "filesystem": filesystem,
                "name": name,
                "flags": flags,
            }
        )

    if not partitions:
        raise ArtifactExportError("No partitions were discovered in the build disk image")

    return {
        "table_type": table_type,
        "disk_size_bytes": disk_size,
        "partitions": partitions,
    }


def dump_clone_partitions(
    *,
    build: BuildDefinition,
    qcow2_disk_path: Path,
    output_dir: Path,
    compress: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    sparse_chunk_size = 1024 * 1024
    nbd_device = _attach_qcow2_to_nbd(qcow2_path=qcow2_disk_path)

    try:
        table = _partition_table_from_device(nbd_device)
        partition_manifest: list[dict[str, object]] = []

        with nbd_device.open("rb") as raw_handle:
            for partition in table["partitions"]:
                number = int(partition["number"])
                start = int(partition["start_byte"])
                size = int(partition["size_bytes"])
                raw_handle.seek(start)
                remaining = size
                extents: list[dict[str, int]] = []
                sparse_target = output_dir / f"partition-{number:02d}.sdat"
                actual_target = sparse_target.with_suffix(sparse_target.suffix + ".gz") if compress else sparse_target
                uncompressed_target = sparse_target
                data_offset = 0

                writer = uncompressed_target.open("wb")
                try:
                    partition_offset = 0
                    while remaining > 0:
                        chunk = raw_handle.read(min(sparse_chunk_size, remaining))
                        if not chunk:
                            break
                        if any(chunk):
                            writer.write(chunk)
                            extents.append(
                                {
                                    "partition_offset": partition_offset,
                                    "data_offset": data_offset,
                                    "length": len(chunk),
                                }
                            )
                            data_offset += len(chunk)
                        remaining -= len(chunk)
                        partition_offset += len(chunk)
                finally:
                    writer.close()

                if compress:
                    with uncompressed_target.open("rb") as in_f, gzip.open(actual_target, "wb", compresslevel=1) as out_f:
                        shutil.copyfileobj(in_f, out_f)
                    uncompressed_target.unlink(missing_ok=True)

                extents_path = output_dir / f"partition-{number:02d}.extents.json"
                extents_path.write_text(json.dumps(extents, separators=(",", ":")), encoding="utf-8")

                partition_manifest.append(
                    {
                        **partition,
                        "file_name": actual_target.name,
                        "extents_file": extents_path.name,
                        "payload_format": "sparse-extents-v1",
                        "sparse_chunk_size": sparse_chunk_size,
                        "payload_size_bytes": data_offset,
                        "sha256": _sha256_of_file(actual_target),
                        "compressed": compress,
                    }
                )

        deploy = _deploy_payload_metadata(build=build)
        manifest = {
            "schema_version": DEPLOY_MANIFEST_VERSION,
            "build_id": build.id,
            "build_name": build.name,
            "operating_system": {
                "name": build.operating_system.name,
                "family": build.operating_system.family,
                "iso": str(build.iso_image),
            },
            "disk_image": str(qcow2_disk_path),
            "boot_mode": build.machine_config.boot_mode,
            "table_type": table["table_type"],
            "disk_size_bytes": table["disk_size_bytes"],
            "partitions": partition_manifest,
            "layout_entries": deploy["layout_entries"],
            "deploy": deploy,
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return output_dir
    finally:
        _detach_nbd(nbd_device)


def save_clone_release(*, dump_dir: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, mode="w:gz") as tar:
        tar.add(dump_dir, arcname=dump_dir.name)
    return output_path


def _archive_directory(*, source_dir: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, mode="w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)
    return output_path


def _embed_clone_payload(*, source_dir: Path, out_dir: Path) -> None:
    if not source_dir.exists():
        return
    target_dir = out_dir / "clone-release"
    target_dir.mkdir(parents=True, exist_ok=True)
    for child in source_dir.iterdir():
        target_path = target_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target_path, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target_path)


def _export_usb_image(*, qcow2_path: Path, output_path: Path) -> None:
    # Legacy compatibility shim retained for tests and callers that still expect a raw image export.
    # The active artifact path now writes an offline deploy bundle instead.
    output_path.mkdir(parents=True, exist_ok=True)


def _write_usb_bundle(*, build: BuildDefinition, out_dir: Path, clone_payload_dir: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    boot_dir = out_dir / "boot"
    boot_dir.mkdir(parents=True, exist_ok=True)
    kernel_out, initrd_out = _extract_boot_assets_from_iso(
        iso_path=Path(build.iso_image.iso_file.path),
        output_dir=boot_dir,
    )
    kernel_dst = boot_dir / "vmlinuz"
    initrd_dst = boot_dir / "initrd.img"
    if kernel_out.resolve() != kernel_dst.resolve():
        shutil.copy2(kernel_out, kernel_dst)
    if initrd_out.resolve() != initrd_dst.resolve():
        shutil.copy2(initrd_out, initrd_dst)

    _extract_iso_stage2_payload(iso_path=Path(build.iso_image.iso_file.path), output_dir=out_dir / "stage2")

    deploy_dir = out_dir / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    render_deploy_restore_script(output_dir=deploy_dir, os_family=build.operating_system.family, build=build)
    _write_rhsm_repo_ids_file(build=build, output_dir=deploy_dir)
    _write_default_afterburner_script(build=build, output_dir=deploy_dir)
    render_deploy_kickstart_file(
        output_dir=deploy_dir,
        vm_name=build.name,
        restore_script_url="file:///run/install/repo/deploy/restore.sh",
        deploy_manifest_url="file:///run/install/repo/deploy.json",
        clone_manifest_url="file:///run/install/repo/clone-release/manifest.json",
    )

    deploy_payload = _deploy_payload_metadata(build=build)
    deploy_payload.update(
        {
            "artifact_type": BuildArtifact.TYPE_USB,
            "payload_delivery": "offline_usb",
            "payload_hint": {
                "clone_manifest": "clone-release/manifest.json",
                "partition_glob": "clone-release/partition-*.img*",
            },
            "scaffold": {
                "restore_script": "deploy/restore.sh",
                "deploy_kickstart": f"deploy/{build.name}-deploy.cfg",
            },
        }
    )
    _write_deploy_manifest(build=build, out_dir=out_dir, payload=deploy_payload)

    if clone_payload_dir is not None:
        _embed_clone_payload(source_dir=clone_payload_dir, out_dir=out_dir)

    manifest = {
        "build_id": build.id,
        "build_name": build.name,
        "os": str(build.operating_system),
        "os_family": build.operating_system.family,
        "iso": str(build.iso_image),
        "partition_layout": str(build.partition_layout),
        "machine_boot_mode": build.machine_config.boot_mode,
        "deploy_manifest": "deploy.json",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_usb_instructions(build=build, out_dir=out_dir)
    return out_dir


def _artifact_release_metadata(*, build: BuildDefinition, generation: int | None = None) -> tuple[str, str]:
    build_date = build.created_at.strftime("%Y-%m-%d") if build.created_at else "unknown"
    build_number = build.id
    generation_suffix = f"-{generation}" if generation is not None else ""
    group = f"{build_date}-build-{build_number}{generation_suffix}"
    label = f"{build_date} (build {build_number}{generation_suffix})"
    return group, label


def _write_pxe_bundle(*, build: BuildDefinition, out_dir: Path, clone_payload_dir: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_pxe_tree(
        iso_path=Path(build.iso_image.iso_file.path),
        out_dir=out_dir,
        boot_mode=build.machine_config.boot_mode,
    )
    _extract_iso_stage2_payload(iso_path=Path(build.iso_image.iso_file.path), output_dir=out_dir / "stage2")

    pxe_boot_cfgs = render_pxe_boot_configs(
        vm_name=build.name,
        kernel_rel_path="boot/vmlinuz",
        initrd_rel_path="boot/initrd.img",
        kickstart_url=f"__PXE_BASE_URL__/deploy/{build.name}-deploy.cfg",
        install_source_url="__PXE_BASE_URL__",
        stage2_source_url="__PXE_BASE_URL__/stage2",
    )
    (out_dir / "pxelinux.cfg" / "default").write_text(pxe_boot_cfgs["bios"], encoding="utf-8")
    (out_dir / "efi" / "grub.cfg").write_text(pxe_boot_cfgs["efi"], encoding="utf-8")

    deploy_dir = out_dir / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    render_deploy_restore_script(output_dir=deploy_dir, os_family=build.operating_system.family, build=build)
    _write_rhsm_repo_ids_file(build=build, output_dir=deploy_dir)
    _write_default_afterburner_script(build=build, output_dir=deploy_dir)
    render_deploy_kickstart_file(
        output_dir=deploy_dir,
        vm_name=build.name,
        restore_script_url="file:///run/install/repo/deploy/restore.sh",
        deploy_manifest_url="file:///run/install/repo/deploy.json",
        clone_manifest_url="file:///run/install/repo/clone-release/manifest.json",
    )
    deploy_payload = _deploy_payload_metadata(build=build)
    deploy_payload.update(
        {
            "artifact_type": BuildArtifact.TYPE_PXE,
            "payload_delivery": "pxe_http",
            "payload_hint": {
                "clone_manifest": "clone-release/manifest.json",
                "partition_glob": "clone-release/partition-*.img*",
            },
            "scaffold": {
                "restore_script": "deploy/restore.sh",
                "deploy_kickstart": f"deploy/{build.name}-deploy.cfg",
            },
        }
    )
    _write_deploy_manifest(build=build, out_dir=out_dir, payload=deploy_payload)
    if clone_payload_dir is not None:
        _embed_clone_payload(source_dir=clone_payload_dir, out_dir=out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        {
            "build_id": build.id,
            "build_name": build.name,
            "os": str(build.operating_system),
            "os_family": build.operating_system.family,
            "iso": str(build.iso_image),
            "partition_layout": str(build.partition_layout),
            "machine_boot_mode": build.machine_config.boot_mode,
            "deploy_manifest": "deploy.json",
        }
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_pxe_instructions(build=build, out_dir=out_dir)
    return out_dir


def prepare_iso_pxe_assets(*, iso_path: Path) -> Path:
    iso_path = iso_path.resolve()
    pxe_dir = iso_path.with_name(f"{iso_path.stem}.pxe")
    if pxe_dir.exists():
        shutil.rmtree(pxe_dir)
    return _write_pxe_tree(iso_path=iso_path, out_dir=pxe_dir)


def remove_iso_pxe_assets(*, iso_path: Path) -> None:
    pxe_dir = iso_path.resolve().with_name(f"{iso_path.resolve().stem}.pxe")
    shutil.rmtree(pxe_dir, ignore_errors=True)


def generate_artifacts(
    *,
    build: BuildDefinition,
    root: Path,
    qcow2_disk_path: Path,
    compress: bool,
) -> None:
    build_dir = root / f"build-{build.id}"
    build_dir.mkdir(parents=True, exist_ok=True)

    created = []
    answers_yaml_content: str | None = None
    if bool(build.enable_answers_file_support):
        answers_yaml_content = _render_answers_yaml_template(keys=_collect_build_answer_keys(build=build))
    existing_batches = BuildArtifact.objects.filter(build=build).values_list("release_group", flat=True)
    generation = 1
    while f"{build.created_at.strftime('%Y-%m-%d') if build.created_at else 'unknown'}-build-{build.id}-{generation}" in existing_batches:
        generation += 1
    release_group, release_label = _artifact_release_metadata(build=build, generation=generation)
    if build.output_usb_img:
        usb_bundle_path = build_dir / "usb"
        _write_usb_bundle(
            build=build,
            out_dir=usb_bundle_path,
            clone_payload_dir=build_dir / "clone-release",
        )
        usb_image_path = build_dir / "usb.img"
        _write_usb_image_from_bundle(
            bundle_dir=usb_bundle_path,
            output_path=usb_image_path,
            build_name=build.name,
            source_iso_path=Path(build.iso_image.iso_file.path),
            build_boot_mode=build.machine_config.boot_mode,
            **(
                {
                    "enable_answers_file_support": True,
                    "answers_file_content": answers_yaml_content,
                }
                if bool(build.enable_answers_file_support)
                else {}
            ),
        )
        created.append((BuildArtifact.TYPE_USB, usb_image_path))

    if build.output_pxe:
        pxe_dir = _write_pxe_bundle(
            build=build,
            out_dir=build_dir / "pxe",
            clone_payload_dir=build_dir / "clone-release",
        )
        pxe_tar = _archive_directory(source_dir=pxe_dir, output_path=build_dir / "pxe.tar.gz")
        created.append((BuildArtifact.TYPE_PXE, pxe_tar))

    for artifact_type, file_path in created:
        compressed = False
        actual_path = file_path
        checksum_path = file_path / "manifest.json" if file_path.is_dir() else file_path

        if compress and artifact_type != BuildArtifact.TYPE_USB:
            if file_path.is_file():
                file_name = file_path.name.lower()
                already_compressed = file_name.endswith((".gz", ".tgz", ".xz", ".bz2", ".zip"))
                if not already_compressed:
                    actual_path = _gzip_file(file_path)
                    checksum_path = actual_path
                    compressed = True

        BuildArtifact.objects.create(
            build=build,
            artifact_type=artifact_type,
            file_path=str(actual_path),
            sha256=_sha256_of_file(checksum_path),
            compressed=compressed,
            release_group=release_group,
            release_label=release_label,
        )
