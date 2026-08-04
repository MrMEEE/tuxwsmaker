from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from apps.catalog.models import OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout


def calculate_layout_disk_size_gib(layout: PartitionLayout) -> int:
    entries = list(layout.entries.order_by("order"))
    fixed_mib = 0
    has_remainder = False
    for entry in entries:
        if entry.size_mode == PartitionEntry.SIZE_FIXED and entry.size_mib:
            fixed_mib += int(entry.size_mib)
        elif entry.size_mode == PartitionEntry.SIZE_REMAINDER:
            has_remainder = True

    minimum_mib = max(fixed_mib, 8192)
    if has_remainder:
        minimum_mib += 4096
    return max(1, (minimum_mib + 1023) // 1024)


def _render_partition_command(entry: PartitionEntry) -> str:
    size_bits = []
    if entry.size_mode == PartitionEntry.SIZE_FIXED and entry.size_mib:
        size_bits.append(f"--size={entry.size_mib}")
    elif entry.size_mode == PartitionEntry.SIZE_REMAINDER:
        size_bits.append("--grow")

    if entry.gpt_type:
        size_bits.append(f"--type={entry.gpt_type}")
    if entry.is_boot:
        size_bits.append("--asprimary")

    if entry.entry_role == PartitionEntry.ROLE_PV:
        return f"part pv.{entry.order:02d} {' '.join(size_bits)}".strip()

    if entry.entry_role == PartitionEntry.ROLE_LV:
        base = [
            f"logvol {entry.mount_point or '/'}",
            f"--vgname={entry.volume_group}",
            f"--name={entry.logical_volume}",
        ]
        base.extend(size_bits)
        if entry.filesystem != "none":
            base.append(f"--fstype={entry.filesystem}")
        return " ".join(base).strip()

    mount_point = entry.mount_point.strip() or "/"
    if entry.filesystem == "swap" or mount_point == "swap":
        mount_point = "swap"
    line = [f"part {mount_point}"]
    line.extend(size_bits)
    if entry.filesystem and entry.filesystem != "none" and mount_point != "swap":
        line.append(f"--fstype={entry.filesystem}")
    return " ".join(line).strip()


def _render_partition_section(layout: PartitionLayout) -> str:
    entries = list(layout.entries.order_by("order"))
    if not entries:
        return "autopart --type=lvm"

    lines = ["clearpart --all --initlabel"]
    for entry in entries:
        lines.append(_render_partition_command(entry))
    return "\n".join(lines)


def render_kickstart_file(*, output_dir: Path, vm_name: str, ssh_public_key: str, partition_layout: PartitionLayout) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{vm_name}.cfg"

    partition_section = _render_partition_section(partition_layout)
    content = f"""#version=RHEL9
text
reboot
lang en_US.UTF-8
keyboard us
timezone UTC --utc
rootpw --lock
selinux --enforcing
firewall --enabled --service=ssh
services --enabled=sshd,NetworkManager
bootloader --location=mbr
zerombr
{partition_section}

%packages
@^minimal-environment
openssh-server
%end

%post --log=/root/ks-post.log
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'EOF'
{ssh_public_key}
EOF
chmod 600 /root/.ssh/authorized_keys
systemctl enable sshd
%end
"""
    path.write_text(content, encoding="utf-8")
    return path


def render_pxe_boot_configs(
    *,
    vm_name: str,
    kernel_rel_path: str,
    initrd_rel_path: str,
    kickstart_url: str,
    install_source_url: str,
    stage2_source_url: str | None = None,
) -> dict[str, str]:
    stage2_url = stage2_source_url or install_source_url
    common_args = (
        f"inst.ks={kickstart_url} "
        f"inst.repo={install_source_url} "
        f"inst.stage2={stage2_url} "
        "ip=dhcp console=ttyS0,115200n8 console=tty0"
    )
    bios = (
        "DEFAULT tuxwsmaker\n"
        "PROMPT 0\n"
        "TIMEOUT 20\n"
        "LABEL tuxwsmaker\n"
        f"  KERNEL /{kernel_rel_path}\n"
        f"  APPEND initrd=/{initrd_rel_path} {common_args}\n"
    )
    efi = (
        "set timeout=2\n"
        "set default=0\n"
        "menuentry 'TuxWSMaker Build' {\n"
        f"  linuxefi /{kernel_rel_path} initrd=/{initrd_rel_path} {common_args}\n"
        f"  initrdefi /{initrd_rel_path}\n"
        "}\n"
    )
    return {
        "bios": bios,
        "efi": efi,
    }


def render_deploy_restore_script(*, output_dir: Path, os_family: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "restore.sh"

    finish_step = "status 'RHEL-family finish adapter: restore path complete'"
    if os_family == OperatingSystem.FAMILY_DEBIAN:
        finish_step = "status 'Debian-family finish adapter: restore path complete'"

    content = dedent(
        """#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="${DEPLOY_ROOT:-/run/tuxwsmaker}"
DEPLOY_MANIFEST_URL="${DEPLOY_MANIFEST_URL:-file:///run/install/repo/deploy.json}"
CLONE_MANIFEST_URL="${CLONE_MANIFEST_URL:-file:///run/install/repo/clone-release/manifest.json}"
WORK_DIR="/tmp/tuxwsmaker-deploy"
MOUNT_ROOT="/mnt/sysimage"
TARGET_HOSTNAME="${TARGET_HOSTNAME:-}"

mkdir -p "$DEPLOY_ROOT" "$WORK_DIR"

status() {
  local msg="$1"
  echo "[deploy] $msg"
}

write_sparse_blocks() {
  local source_path="$1"
  local target_path="$2"
  local block_size="${3:-65536}"

  if command -v blkdiscard >/dev/null 2>&1; then
    blkdiscard -f "$target_path" >/dev/null 2>&1 || true
  fi

  python3 - "$source_path" "$target_path" "$block_size" <<'PY'
import sys

source_path, target_path, block_size = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(source_path, 'rb') as source_handle, open(target_path, 'r+b', buffering=0) as target_handle:
    offset = 0
    while True:
        chunk = source_handle.read(block_size)
        if not chunk:
            break
        if any(chunk):
            target_handle.seek(offset)
            target_handle.write(chunk)
        offset += len(chunk)
PY
}

apply_sparse_extents_file() {
  local payload_path="$1"
  local extents_path="$2"
  local target_path="$3"
  local compressed_flag="$4"

  if command -v blkdiscard >/dev/null 2>&1; then
    blkdiscard -f "$target_path" >/dev/null 2>&1 || true
  fi

  python3 - "$payload_path" "$extents_path" "$target_path" "$compressed_flag" <<'PY'
import gzip
import json
import sys

payload_path, extents_path, target_path, compressed_flag = sys.argv[1:5]
with open(extents_path, encoding="utf-8") as extents_file:
    extents = json.load(extents_file)

opener = gzip.open if compressed_flag == "1" else open
with opener(payload_path, "rb") as payload_handle, open(target_path, "r+b", buffering=0) as target_handle:
    for extent in extents:
        length = int(extent.get("length") or 0)
        if length <= 0:
            continue
        part_offset = int(extent.get("partition_offset") or 0)
        chunk = payload_handle.read(length)
        if len(chunk) != length:
            raise RuntimeError(f"Sparse payload truncated for {payload_path}: expected {length} bytes, got {len(chunk)}")
        target_handle.seek(part_offset)
        target_handle.write(chunk)
PY
}

sparse_restore_stream() {
  local target_path="$1"
  local block_size="${2:-65536}"

  if command -v blkdiscard >/dev/null 2>&1; then
    blkdiscard -f "$target_path" >/dev/null 2>&1 || true
  fi

  python3 -c 'import sys
target_path = sys.argv[1]
block_size = int(sys.argv[2])
with open(target_path, "r+b", buffering=0) as target_handle:
    offset = 0
    stdin = sys.stdin.buffer
    while True:
        chunk = stdin.read(block_size)
        if not chunk:
            break
        if any(chunk):
            target_handle.seek(offset)
            target_handle.write(chunk)
        offset += len(chunk)' "$target_path" "$block_size"
}

cleanup_mounts() {
  if [[ -f "$WORK_DIR/mounted.paths" ]]; then
    tac "$WORK_DIR/mounted.paths" | while read -r mp; do
      [[ -z "${mp:-}" ]] && continue
      umount -lf "$mp" >/dev/null 2>&1 || true
    done
  fi
  for bind_path in /run /sys/firmware/efi/efivars /sys /proc /dev/pts /dev; do
    umount -lf "$MOUNT_ROOT$bind_path" >/dev/null 2>&1 || true
  done
  umount -lf "$MOUNT_ROOT" >/dev/null 2>&1 || true
}
trap cleanup_mounts EXIT

fetch_file() {
  local url="$1"
  local dst="$2"
  if [[ "$url" == file://* ]]; then
    cp "${url#file://}" "$dst"
  else
    curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$url" -o "$dst"
  fi
}

raw_part() {
  local disk="$1"
  local n="$2"
  if [[ "$disk" == nvme* ]]; then
    printf '/dev/%sp%s' "$disk" "$n"
  else
    printf '/dev/%s%s' "$disk" "$n"
  fi
}

status "Starting restore"
status "Timestamp: $(date -Is)"
status "deploy manifest: $DEPLOY_MANIFEST_URL"
status "clone manifest: $CLONE_MANIFEST_URL"

fetch_file "$DEPLOY_MANIFEST_URL" "$WORK_DIR/deploy.json"
fetch_file "$CLONE_MANIFEST_URL" "$WORK_DIR/clone-manifest.json"
status "Manifests downloaded"

SOURCE_BOOT_MODE="$(python3 - "$WORK_DIR/deploy.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as f:
  deploy = json.load(f)

mode = str((deploy.get('boot') or {}).get('machine_boot_mode') or '').strip().lower()
print(mode)
PY
)"
status "Source image boot mode: ${SOURCE_BOOT_MODE:-unknown}"

TARGET_DISK="${TARGET_DISK:-$(lsblk -b -dn -o NAME,TYPE,SIZE | awk '$2=="disk"{print $1" "$3}' | sort -k2 -nr | head -n1 | awk '{print $1}') }"
TARGET_DISK="${TARGET_DISK// /}"
if [[ -z "$TARGET_DISK" ]]; then
  echo "[deploy] Could not find target disk" >&2
  exit 1
fi
TARGET_DEV="/dev/$TARGET_DISK"
status "Target disk: $TARGET_DEV"

python3 - "$WORK_DIR/deploy.json" "$WORK_DIR/clone-manifest.json" "$WORK_DIR/sfdisk.layout" "$WORK_DIR/parts.map" <<'PY'
import json
import sys

deploy_path, clone_path, sfdisk_path, map_path = sys.argv[1:5]
with open(deploy_path, encoding="utf-8") as f:
    deploy = json.load(f)
with open(clone_path, encoding="utf-8") as f:
    clone = json.load(f)

table_type = str(clone.get("table_type") or deploy.get("boot", {}).get("table_type") or "gpt").lower()
layout_entries = sorted(deploy.get("layout_entries", []), key=lambda e: int(e.get("order") or 0))
layout_by_order = {int(e.get("order")): e for e in layout_entries if e.get("order") is not None}
partitions = sorted(clone.get("partitions", []), key=lambda p: int(p.get("number") or 0))
if not partitions:
    raise SystemExit("No partitions found in clone manifest")

sector = 512
sfdisk_lines = [f"label: {table_type}", "unit: sectors"]
map_lines = []
for part in partitions:
    number = int(part["number"])
    start_sector = max(0, int(part.get("start_byte") or 0) // sector)
    size_sector = max(1, int(part.get("size_bytes") or 0) // sector)
    entry = layout_by_order.get(number, {})

    opts = [f"start={start_sector}", f"size={size_sector}"]
    gpt_type = str(entry.get("gpt_type") or "").strip()
    flags = str(part.get("flags") or "").lower()
    if gpt_type:
        opts.append(f"type={gpt_type}")
    elif table_type == "gpt" and "esp" in flags:
        opts.append("type=c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
    elif table_type == "gpt" and ("bios_grub" in flags or "bios grub" in flags):
      opts.append("type=21686148-6449-6e6f-744e-656564454649")
    elif table_type != "gpt" and "boot" in flags:
      opts.append("bootable")

    name = str(part.get("name") or entry.get("name") or "").strip().replace('"', "")
    if name:
        opts.append(f'name="{name}"')

    sfdisk_lines.append(", ".join(opts))
    map_lines.append("|".join([
        str(number),
        str(part.get("file_name") or ""),
      str(part.get("extents_file") or ""),
      str(part.get("payload_format") or "raw"),
      str(part.get("compressed") or False),
        str(entry.get("mount_point") or ""),
        str(entry.get("filesystem") or ""),
        str(entry.get("luks_enabled") or False),
        str(entry.get("luks_name") or ""),
    ]))

with open(sfdisk_path, "w", encoding="utf-8") as f:
    f.write("\\n".join(sfdisk_lines) + "\\n")
with open(map_path, "w", encoding="utf-8") as f:
    f.write("\\n".join(map_lines) + "\\n")
PY

wipefs -a -f -q "$TARGET_DEV" || true
if command -v sgdisk >/dev/null 2>&1; then
  sgdisk --zap-all "$TARGET_DEV" >/dev/null 2>&1 || true
fi
dd if=/dev/zero of="$TARGET_DEV" bs=1M count=16 conv=fsync status=none || true

sfdisk --wipe always --wipe-partitions always "$TARGET_DEV" < "$WORK_DIR/sfdisk.layout"
partprobe "$TARGET_DEV" || true
udevadm settle || true
status "Partition table created"

if [[ "$CLONE_MANIFEST_URL" == file://* ]]; then
  CLONE_BASE="file://$(dirname "${CLONE_MANIFEST_URL#file://}")"
else
  CLONE_BASE="${CLONE_MANIFEST_URL%/*}"
fi

: > "$WORK_DIR/part-dev.map"
TOTAL_PARTS=$(grep -c '^[0-9]' "$WORK_DIR/parts.map" || true)
PART_INDEX=0
while IFS='|' read -r number file_name extents_file payload_format compressed mount_point fs_type luks_enabled luks_name; do
  [[ -z "${number:-}" ]] && continue
  PART_INDEX=$((PART_INDEX + 1))
  if [[ -z "${file_name:-}" ]]; then
    echo "[deploy] Missing file_name for partition $number in parts.map" >&2
    exit 1
  fi

  part_dev=$(raw_part "$TARGET_DISK" "$number")
  image_url="$CLONE_BASE/$file_name"
  echo "$number|$part_dev|$mount_point|$fs_type|$luks_enabled|$luks_name" >> "$WORK_DIR/part-dev.map"

  status "[$PART_INDEX/$TOTAL_PARTS] Restoring partition $number ($file_name) to $part_dev"
  if [[ "$image_url" == file://* ]]; then
    image_path="${image_url#file://}"
    extents_path=""
    if [[ -n "${extents_file:-}" ]]; then
      extents_url="$CLONE_BASE/$extents_file"
      extents_path="${extents_url#file://}"
    fi
    image_size=$(stat -c%s "$image_path" 2>/dev/null || echo 0)
    if [[ "$image_size" -eq 0 ]]; then
      status "[$PART_INDEX/$TOTAL_PARTS] Partition $number is empty; skipping write"
      continue
    fi
    if [[ "${payload_format:-raw}" == "sparse-extents-v1" && -n "${extents_path:-}" ]]; then
      status "[$PART_INDEX/$TOTAL_PARTS] Applying sparse extents for partition $number"
      compressed_flag=0
      [[ "${compressed:-False}" == "True" || "${compressed:-false}" == "true" ]] && compressed_flag=1
      apply_sparse_extents_file "$image_path" "$extents_path" "$part_dev" "$compressed_flag"
    elif [[ "$image_path" == *.gz ]]; then
      status "[$PART_INDEX/$TOTAL_PARTS] Writing sparse blocks for partition $number"
      gzip -dc "$image_path" | sparse_restore_stream "$part_dev" 65536
    else
      status "[$PART_INDEX/$TOTAL_PARTS] Writing sparse blocks for partition $number"
      write_sparse_blocks "$image_path" "$part_dev" 65536
    fi
  else
    status "[$PART_INDEX/$TOTAL_PARTS] Streaming remote partition image for $number"
    if [[ "${payload_format:-raw}" == "sparse-extents-v1" && -n "${extents_file:-}" ]]; then
      tmp_payload="$WORK_DIR/partition-${number}.payload"
      tmp_extents="$WORK_DIR/partition-${number}.extents.json"
      curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$image_url" -o "$tmp_payload"
      curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$CLONE_BASE/$extents_file" -o "$tmp_extents"
      compressed_flag=0
      [[ "${compressed:-False}" == "True" || "${compressed:-false}" == "true" ]] && compressed_flag=1
      apply_sparse_extents_file "$tmp_payload" "$tmp_extents" "$part_dev" "$compressed_flag"
      rm -f "$tmp_payload" "$tmp_extents"
    elif [[ "$image_url" == *.gz ]]; then
      curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$image_url" | gzip -dc | sparse_restore_stream "$part_dev" 65536
    else
      curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$image_url" | sparse_restore_stream "$part_dev" 65536
    fi
  fi
  status "[$PART_INDEX/$TOTAL_PARTS] Partition $number restore completed"
done < "$WORK_DIR/parts.map"

sync
partprobe "$TARGET_DEV" || true
udevadm settle || true
if command -v vgchange >/dev/null 2>&1; then
  vgchange -ay >/dev/null 2>&1 || true
fi

status "Mounting restored filesystems"
: > "$WORK_DIR/mounted.paths"
ROOT_DEV=""
while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name; do
  [[ -z "${number:-}" ]] && continue
  if [[ "$mount_point" == "/" ]]; then
    ROOT_DEV="$part_dev"
    break
  fi
done < "$WORK_DIR/part-dev.map"

if [[ -z "$ROOT_DEV" ]]; then
  status "Root mountpoint not found in deploy metadata; skipping chroot actions"
else
  mkdir -p "$MOUNT_ROOT"
  mount "$ROOT_DEV" "$MOUNT_ROOT"
  echo "$MOUNT_ROOT" >> "$WORK_DIR/mounted.paths"

  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name; do
    [[ -z "${number:-}" ]] && continue
    [[ -z "${mount_point:-}" ]] && continue
    [[ "$mount_point" == "/" || "$mount_point" == "swap" ]] && continue
    target="$MOUNT_ROOT$mount_point"
    mkdir -p "$target"
    if mount "$part_dev" "$target"; then
      echo "$target" >> "$WORK_DIR/mounted.paths"
    else
      status "Could not mount $part_dev on $target (continuing)"
    fi
  done < "$WORK_DIR/part-dev.map"

  status "Rebuilding /etc/fstab"
  : > "$MOUNT_ROOT/etc/fstab"
  while read -r mp; do
    [[ -z "${mp:-}" ]] && continue
    src=$(findmnt -n -o SOURCE --target "$mp" 2>/dev/null || true)
    fstype=$(findmnt -n -o FSTYPE --target "$mp" 2>/dev/null || true)
    [[ -z "${src:-}" || -z "${fstype:-}" ]] && continue

    if [[ "$mp" == "$MOUNT_ROOT" ]]; then
      fs_mp="/"
    else
      fs_mp="${mp#$MOUNT_ROOT}"
    fi
    [[ -z "${fs_mp:-}" ]] && continue

    uuid=$(blkid -s UUID -o value "$src" 2>/dev/null || true)
    [[ -n "$uuid" ]] && devref="UUID=$uuid" || devref="$src"

    opts="defaults"
    [[ "$fstype" == "vfat" ]] && opts="umask=0077,shortname=winnt"
    printf "%-32s %-16s %-7s %-22s 0 0\\n" "$devref" "$fs_mp" "$fstype" "$opts" >> "$MOUNT_ROOT/etc/fstab"
  done < "$WORK_DIR/mounted.paths"

  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name; do
    [[ "$mount_point" == "swap" || "$fs_type" == "swap" ]] || continue
    swap_uuid=$(blkid -s UUID -o value "$part_dev" 2>/dev/null || true)
    if [[ -n "$swap_uuid" ]]; then
      echo "UUID=$swap_uuid none swap defaults 0 0" >> "$MOUNT_ROOT/etc/fstab"
    else
      echo "$part_dev none swap defaults 0 0" >> "$MOUNT_ROOT/etc/fstab"
    fi
  done < "$WORK_DIR/part-dev.map"

  status "Rebuilding /etc/crypttab"
  : > "$MOUNT_ROOT/etc/crypttab"
  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name; do
    [[ "$luks_enabled" == "True" ]] || continue
    if cryptsetup isLuks "$part_dev" >/dev/null 2>&1; then
      luks_uuid=$(cryptsetup luksUUID "$part_dev" 2>/dev/null || true)
      map_name="${luks_name:-luks-${number}}"
      if [[ -n "$luks_uuid" ]]; then
        echo "$map_name UUID=$luks_uuid none luks,discard,x-initrd.attach" >> "$MOUNT_ROOT/etc/crypttab"
      fi
    fi
  done < "$WORK_DIR/part-dev.map"

  if [[ -n "$TARGET_HOSTNAME" ]]; then
    status "Setting hostname to $TARGET_HOSTNAME"
    echo -n "$TARGET_HOSTNAME" > "$MOUNT_ROOT/etc/hostname"
  fi

  for bind_path in /dev /dev/pts /proc /sys /sys/firmware/efi/efivars /run; do
    mkdir -p "$MOUNT_ROOT$bind_path"
    mount --bind "$bind_path" "$MOUNT_ROOT$bind_path" || true
  done

  status "Repairing bootloader and initramfs in chroot"
  BOOT_MODE="bios"
  if [[ -d /sys/firmware/efi ]]; then
    BOOT_MODE="uefi"
  fi
  status "Boot mode detected: $BOOT_MODE"

  if [[ -n "${SOURCE_BOOT_MODE:-}" && "$SOURCE_BOOT_MODE" != "$BOOT_MODE" ]]; then
    status "ERROR: Source image boot mode ($SOURCE_BOOT_MODE) does not match target firmware mode ($BOOT_MODE)"
    status "ERROR: Rebuild the source image with matching boot mode before deploy restore"
    exit 1
  fi

  chroot "$MOUNT_ROOT" /usr/bin/env TARGET_DEV="$TARGET_DEV" BOOT_MODE="$BOOT_MODE" /bin/bash <<'CHROOT'
set -euo pipefail
if [[ "${BOOT_MODE:-bios}" == "bios" ]]; then
  if command -v grub2-install >/dev/null 2>&1 && [[ -x /usr/lib/grub/i386-pc/modinfo.sh ]]; then
    grub2-install "$TARGET_DEV" && touch /tmp/tuxwsmaker-grub-install.ok || true
  elif command -v grub-install >/dev/null 2>&1 && [[ -d /usr/lib/grub/i386-pc ]]; then
    grub-install "$TARGET_DEV" && touch /tmp/tuxwsmaker-grub-install.ok || true
  fi
else
  if command -v grub2-install >/dev/null 2>&1; then
    grub2-install "$TARGET_DEV" && touch /tmp/tuxwsmaker-grub-install.ok || true
  elif command -v grub-install >/dev/null 2>&1; then
    grub-install "$TARGET_DEV" && touch /tmp/tuxwsmaker-grub-install.ok || true
  fi
fi

if [[ -d /boot/grub2 ]] && command -v grub2-mkconfig >/dev/null 2>&1; then
  grub2-mkconfig -o /boot/grub2/grub.cfg || true
fi
if command -v update-grub >/dev/null 2>&1; then
  update-grub || true
fi
if command -v dracut >/dev/null 2>&1; then
  dracut -f --regenerate-all || true
elif command -v update-initramfs >/dev/null 2>&1; then
  update-initramfs -u -k all || true
fi
CHROOT

  if [[ "$BOOT_MODE" == "bios" && ! -f "$MOUNT_ROOT/tmp/tuxwsmaker-grub-install.ok" ]]; then
    status "Chroot BIOS grub install unavailable; attempting installer-environment fallback"
    if command -v grub2-install >/dev/null 2>&1 && [[ -d "$MOUNT_ROOT/boot/grub2" ]]; then
      grub2-install --boot-directory="$MOUNT_ROOT/boot" "$TARGET_DEV" || true
    elif command -v grub-install >/dev/null 2>&1 && [[ -d "$MOUNT_ROOT/boot/grub" || -d "$MOUNT_ROOT/boot/grub2" ]]; then
      grub-install --boot-directory="$MOUNT_ROOT/boot" "$TARGET_DEV" || true
    fi
  elif [[ -f "$MOUNT_ROOT/tmp/tuxwsmaker-grub-install.ok" ]]; then
    status "Bootloader install completed inside chroot"
  fi
  rm -f "$MOUNT_ROOT/tmp/tuxwsmaker-grub-install.ok" || true

  if [[ "$BOOT_MODE" == "bios" ]]; then
    status "Verifying BIOS bootloader on $TARGET_DEV"
    mbr_sig=$(dd if="$TARGET_DEV" bs=1 skip=510 count=2 2>/dev/null | od -An -tx1 | tr -d ' \n')
    if [[ "$mbr_sig" != "55aa" ]]; then
      status "ERROR: Missing MBR boot signature (expected 55aa, got ${mbr_sig:-none})"
      exit 1
    fi
    if dd if="$TARGET_DEV" bs=440 count=1 2>/dev/null | cmp -s - /dev/zero; then
      status "ERROR: MBR boot code area is empty after restore"
      exit 1
    fi
    status "BIOS bootloader verification passed"
  fi

  EFI_NUM=$(awk -F'|' '$3=="/boot/efi" {print $1; exit}' "$WORK_DIR/part-dev.map")
  if [[ -d /sys/firmware/efi && -n "$EFI_NUM" ]] && command -v efibootmgr >/dev/null 2>&1; then
    status "Refreshing UEFI boot entry"
    efi_loader="\\EFI\\BOOT\\BOOTX64.EFI"
    for cand in \
      "$MOUNT_ROOT/boot/efi/EFI/redhat/shimx64.efi|\\EFI\\redhat\\shimx64.efi" \
      "$MOUNT_ROOT/boot/efi/EFI/centos/shimx64.efi|\\EFI\\centos\\shimx64.efi" \
      "$MOUNT_ROOT/boot/efi/EFI/rocky/shimx64.efi|\\EFI\\rocky\\shimx64.efi" \
      "$MOUNT_ROOT/boot/efi/EFI/almalinux/shimx64.efi|\\EFI\\almalinux\\shimx64.efi" \
      "$MOUNT_ROOT/boot/efi/EFI/debian/grubx64.efi|\\EFI\\debian\\grubx64.efi" \
      "$MOUNT_ROOT/boot/efi/EFI/ubuntu/grubx64.efi|\\EFI\\ubuntu\\grubx64.efi"; do
      host_path="${cand%%|*}"
      loader_path="${cand##*|}"
      if [[ -f "$host_path" ]]; then
        efi_loader="$loader_path"
        break
      fi
    done
    efibootmgr -q -w -c -d "$TARGET_DEV" -p "$EFI_NUM" -L "TuxWSMaker Restored" -l "$efi_loader" || true
  fi

  AFTERBURNER="/run/install/repo/deploy/afterburner.sh"
  if [[ -f "$AFTERBURNER" ]]; then
    status "Running afterburner inside restored system"
    cp "$AFTERBURNER" "$MOUNT_ROOT/root/afterburner.sh"
    chmod +x "$MOUNT_ROOT/root/afterburner.sh"
    chroot "$MOUNT_ROOT" /bin/bash /root/afterburner.sh || true
  else
    status "No afterburner script found at /run/install/repo/deploy/afterburner.sh; skipping"
  fi
fi

status "Restore completed successfully"
__FINISH_STEP__
"""
    ).lstrip()

    content = content.replace("__FINISH_STEP__", finish_step)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def render_deploy_kickstart_file(
    *,
    output_dir: Path,
    vm_name: str,
    restore_script_url: str,
    deploy_manifest_url: str,
    clone_manifest_url: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{vm_name}-deploy.cfg"
    content = f"""#version=RHEL9
text
reboot
lang en_US.UTF-8
keyboard us
timezone UTC --utc
network --bootproto=dhcp
skipx
firewall --disabled
selinux --permissive

%pre --erroronfail --log=/tmp/deploy-pre.log
if [ -c /dev/console ]; then
  exec > >(tee /dev/console) 2>&1
fi
echo "[deploy-pre] Starting at $(date -Is)"
export DEPLOY_MANIFEST_URL={deploy_manifest_url}
export CLONE_MANIFEST_URL={clone_manifest_url}
if [ ! -f /run/install/repo/deploy/restore.sh ]; then
    echo "Missing local restore script in /run/install/repo/deploy/restore.sh" >&2
    exit 1
fi
cp /run/install/repo/deploy/restore.sh /tmp/restore.sh
chmod +x /tmp/restore.sh
bash /tmp/restore.sh
echo "[deploy-pre] Restore script finished at $(date -Is)"
cat <<'EOF'
================================================================
Restore complete.

Press Enter to power off. Then remove install media and boot from disk.
================================================================
EOF
echo "[deploy-pre] Restore complete, press enter to power off"
if ! read -r _ < /dev/console; then
  echo "[deploy-pre] tty input unavailable; pausing 120 seconds before power off"
  sleep 120
fi
sync
poweroff -f || halt -f
%end
"""
    path.write_text(content, encoding="utf-8")
    return path
