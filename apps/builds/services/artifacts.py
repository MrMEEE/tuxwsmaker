from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from datetime import datetime
import subprocess
import tarfile
import tempfile
from pathlib import Path

from apps.afterburners.services import render_afterburner_script
from apps.builds.models import BuildArtifact, BuildDefinition, BuildMachineConfig
from apps.builds.services.kickstart import (
    render_deploy_kickstart_file,
    render_deploy_restore_script,
    render_pxe_boot_configs,
)


class ArtifactExportError(RuntimeError):
    pass


DEPLOY_MANIFEST_VERSION = 1


def _write_default_afterburner_script(*, build: BuildDefinition, output_dir: Path) -> Path:
    return render_afterburner_script(build=build, output_dir=output_dir)


def _write_usb_image_from_bundle(
    *,
    bundle_dir: Path,
    output_path: Path,
    build_name: str,
    source_iso_path: Path | None = None,
    build_boot_mode: str | None = None,
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
        if not source_iso_path or not source_iso_path.exists():
            raise ArtifactExportError(
                "UEFI Secure Boot artifact generation requires a source ISO to preserve the signed UEFI boot chain"
            )
        if shutil.which("xorriso") is None:
            raise ArtifactExportError(
                "UEFI Secure Boot artifact generation requires xorriso to preserve the signed UEFI boot chain"
            )

        try:
            cmd = [
                "xorriso",
                "-indev",
                str(source_iso_path),
                "-outdev",
                str(output_path),
                "-boot_image",
                "any",
                "replay",
                "-volid",
                "TUXWSDEPLOY",
                "-map",
                str(bundle_dir),
                "/",
            ]

            # Keep the signed UEFI boot chain from the source ISO but drop large
            # distro package trees so usb.img remains close to payload size.
            if "rhel" in source_iso_path.name.lower():
                cmd.extend([
                    "-rm_r",
                    "/BaseOS",
                    "/AppStream",
                    "--",
                ])

            cmd.append("-commit")
            _run_checked(cmd)
            return output_path
        except Exception as exc:
            raise ArtifactExportError(
                f"UEFI Secure Boot artifact generation requires a signed-boot-chain-preserving ISO overlay, but xorriso failed: {exc}"
            ) from exc

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
    boot_entries = [entry["order"] for entry in layout_entries if entry["is_boot"]]
    mount_map = [
        {
            "order": entry["order"],
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


def _convert_qcow2_to_raw(*, qcow2_path: Path, raw_path: Path) -> None:
    if shutil.which("qemu-img") is None:
        raise ArtifactExportError("qemu-img is required to export clone partition images")
    _run_checked([
        "qemu-img",
        "convert",
        "-f",
        "qcow2",
        "-O",
        "raw",
        "-S",
        "4k",
        str(qcow2_path),
        str(raw_path),
    ])


def _partition_table_from_raw(raw_path: Path) -> dict:
    if shutil.which("parted") is None:
        raise ArtifactExportError("parted is required to inspect disk partitions for clone export")
    proc = _run_checked(["parted", "-s", "-m", str(raw_path), "unit", "B", "print"])
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
    with tempfile.NamedTemporaryFile(prefix=f"build-{build.id}-", suffix=".raw", delete=False) as raw_tmp:
        raw_path = Path(raw_tmp.name)

    try:
        _convert_qcow2_to_raw(qcow2_path=qcow2_disk_path, raw_path=raw_path)
        table = _partition_table_from_raw(raw_path)
        partition_manifest: list[dict[str, object]] = []

        with raw_path.open("rb") as raw_handle:
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
        raw_path.unlink(missing_ok=True)


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
    existing_batches = BuildArtifact.objects.filter(build=build).values_list("release_group", flat=True)
    generation = 1
    while f"{build.created_at.strftime('%Y-%m-%d') if build.created_at else 'unknown'}-build-{build.id}-{generation}" in existing_batches:
        generation += 1
    release_group, release_label = _artifact_release_metadata(build=build, generation=generation)
    if build.output_pxe:
        pxe_dir = _write_pxe_bundle(
            build=build,
            out_dir=build_dir / "pxe",
            clone_payload_dir=build_dir / "clone-release",
        )
        pxe_tar = _archive_directory(source_dir=pxe_dir, output_path=build_dir / "pxe.tar.gz")
        created.append((BuildArtifact.TYPE_PXE, pxe_tar))

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
        )
        created.append((BuildArtifact.TYPE_USB, usb_image_path))

    for artifact_type, file_path in created:
        compressed = False
        actual_path = file_path
        checksum_path = file_path / "manifest.json" if file_path.is_dir() else file_path

        if compress:
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
