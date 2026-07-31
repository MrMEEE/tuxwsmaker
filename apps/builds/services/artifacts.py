from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from apps.builds.models import BuildArtifact, BuildDefinition


class ArtifactExportError(RuntimeError):
    pass


def _write_pxe_tree(*, iso_path: Path, out_dir: Path) -> Path:
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


def _run_checked(args: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
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
                target = output_dir / f"partition-{number:02d}.img"
                actual_target = target.with_suffix(target.suffix + ".gz") if compress else target

                raw_handle.seek(start)
                remaining = size
                if compress:
                    writer = gzip.open(actual_target, "wb", compresslevel=1)
                else:
                    writer = actual_target.open("wb")
                try:
                    while remaining > 0:
                        chunk = raw_handle.read(min(8 * 1024 * 1024, remaining))
                        if not chunk:
                            break
                        writer.write(chunk)
                        remaining -= len(chunk)
                finally:
                    writer.close()

                partition_manifest.append(
                    {
                        **partition,
                        "file_name": actual_target.name,
                        "sha256": _sha256_of_file(actual_target),
                        "compressed": compress,
                    }
                )

        manifest = {
            "build_id": build.id,
            "build_name": build.name,
            "disk_image": str(qcow2_disk_path),
            "boot_mode": build.machine_config.boot_mode,
            "table_type": table["table_type"],
            "disk_size_bytes": table["disk_size_bytes"],
            "partitions": partition_manifest,
            "layout_entries": [
                {
                    "order": entry.order,
                    "name": entry.name,
                    "entry_role": entry.entry_role,
                    "mount_point": entry.mount_point,
                    "filesystem": entry.filesystem,
                    "luks_enabled": entry.luks_enabled,
                    "luks_name": entry.luks_name,
                    "volume_group": entry.volume_group,
                    "logical_volume": entry.logical_volume,
                }
                for entry in build.partition_layout.entries.order_by("order")
            ],
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


def _export_usb_image(*, qcow2_path: Path, output_path: Path) -> None:
    if shutil.which("qemu-img") is None:
        raise ArtifactExportError("qemu-img is required to export USB images")

    cmd = [
        "qemu-img",
        "convert",
        "-f",
        "qcow2",
        "-O",
        "raw",
        str(qcow2_path),
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ArtifactExportError(
            f"qemu-img convert failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )


def _write_pxe_bundle(*, build: BuildDefinition, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_pxe_tree(iso_path=Path(build.iso_image.iso_file.path), out_dir=out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        {
            "build_id": build.id,
            "build_name": build.name,
            "os": str(build.operating_system),
            "iso": str(build.iso_image),
            "partition_layout": str(build.partition_layout),
            "machine_boot_mode": build.machine_config.boot_mode,
        }
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
    if build.output_pxe:
        pxe_path = _write_pxe_bundle(build=build, out_dir=build_dir / "pxe")
        created.append((BuildArtifact.TYPE_PXE, pxe_path))

    if build.output_usb_img:
        usb_path = build_dir / "usb_image.img"
        _export_usb_image(qcow2_path=qcow2_disk_path, output_path=usb_path)
        created.append((BuildArtifact.TYPE_USB, usb_path))

    for artifact_type, file_path in created:
        compressed = False
        actual_path = file_path
        checksum_path = file_path / "manifest.json" if file_path.is_dir() else file_path

        if compress:
            if file_path.is_file():
                actual_path = _gzip_file(file_path)
                checksum_path = actual_path
                compressed = True

        BuildArtifact.objects.create(
            build=build,
            artifact_type=artifact_type,
            file_path=str(actual_path),
            sha256=_sha256_of_file(checksum_path),
            compressed=compressed,
        )
