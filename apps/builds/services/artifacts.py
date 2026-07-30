from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
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
