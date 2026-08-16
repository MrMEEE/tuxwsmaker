from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from apps.catalog.models import OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout
from apps.repositories.services import render_repository_activation_snippet, render_repository_cleanup_snippet


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
    size_bits.extend(["--size=1", "--grow"])

  # Guard against legacy/invalid records that have no explicit size mode payload.
  # Kickstart requires a base sizing flag; --grow alone is not accepted for logvol.
  if not size_bits:
    size_bits.extend(["--size=1", "--grow"])

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
  non_lv_entries = [entry for entry in entries if entry.entry_role != PartitionEntry.ROLE_LV]
  lv_entries = [entry for entry in entries if entry.entry_role == PartitionEntry.ROLE_LV]

  for entry in non_lv_entries:
    lines.append(_render_partition_command(entry))

  vg_to_pvs: dict[str, list[str]] = {}
  for entry in non_lv_entries:
    if entry.entry_role != PartitionEntry.ROLE_PV:
      continue
    vg_name = str(entry.volume_group or "").strip()
    if not vg_name:
      continue
    vg_to_pvs.setdefault(vg_name, []).append(f"pv.{entry.order:02d}")

  for vg_name, pv_names in vg_to_pvs.items():
    lines.append(f"volgroup {vg_name} {' '.join(pv_names)}")

  for entry in lv_entries:
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


def render_deploy_restore_script(*, output_dir: Path, os_family: str, build=None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "restore.sh"

    finish_step = "status 'RHEL-family finish adapter: restore path complete'"
    if os_family == OperatingSystem.FAMILY_DEBIAN:
        finish_step = "status 'Debian-family finish adapter: restore path complete'"

    repo_setup = ""
    repo_cleanup = ""
    if build is not None:
      selections = [sel for sel in build.ordered_repository_selections() if sel.enable_before_afterburner]
      repo_setup = render_repository_activation_snippet(
        selections=selections,
        os_family=os_family,
        root_expression='"$MOUNT_ROOT"',
        phase_label="deploy/afterburner",
      )
      repo_cleanup = render_repository_cleanup_snippet(
        selections=selections,
        os_family=os_family,
        root_expression='"$MOUNT_ROOT"',
        phase_label="deploy/afterburner",
      )

    content = dedent(
        """#!/usr/bin/env bash
      set -Eeuo pipefail

DEPLOY_ROOT="${DEPLOY_ROOT:-/run/tuxwsmaker}"
DEPLOY_MANIFEST_URL="${DEPLOY_MANIFEST_URL:-file:///run/install/repo/deploy.json}"
CLONE_MANIFEST_URL="${CLONE_MANIFEST_URL:-file:///run/install/repo/clone-release/manifest.json}"
WORK_DIR="/tmp/tuxwsmaker-deploy"
MOUNT_ROOT="/mnt/sysimage"
TARGET_HOSTNAME="${TARGET_HOSTNAME:-}"
DEFAULT_LUKS_PASSWORD="${DEFAULT_LUKS_PASSWORD:-tuxwsmaker}"
RESTORE_LOG="${RESTORE_LOG:-/tmp/tuxwsmaker-restore.log}"
ANSWERS_SUPPORT="__ANSWERS_SUPPORT__"
ANSWERS_MOUNT=""
ANSWERS_FILE=""
RESTORE_LOG_MIRROR=""
CURRENT_PHASE="bootstrap"
INPUT_TTY="${DEPLOY_INPUT_TTY:-}"

# Keep restore output single-path to avoid garbled duplicate lines when caller
# already mirrors stdout/stderr to /dev/console.
exec > >(tee -a "$RESTORE_LOG") 2>&1
if [[ -z "$INPUT_TTY" && -c /dev/tty ]]; then
  INPUT_TTY="/dev/tty"
fi
if [[ -z "$INPUT_TTY" && -c /dev/console ]]; then
  INPUT_TTY="/dev/console"
fi
if [[ -n "$INPUT_TTY" && -c "$INPUT_TTY" && -r "$INPUT_TTY" ]]; then
  exec < "$INPUT_TTY"
else
  INPUT_TTY=""
fi
# Some installer TTY paths can have ONLCR disabled, which renders each newline
# without carriage return and causes staircase-like indented output.
if [[ -t 1 ]] && command -v stty >/dev/null 2>&1; then
  stty sane echo icrnl onlcr 2>/dev/null || true
fi

hold_on_error() {
  local exit_code="$1"
  sync_answers_restore_log || true
  echo
  echo "[deploy] Restore failed with exit code ${exit_code}" >&2
  echo "[deploy] Failing phase: ${CURRENT_PHASE}" >&2
  echo "[deploy] Command: ${BASH_COMMAND:-unknown}" >&2
  echo "[deploy] Line: ${BASH_LINENO[0]:-unknown}" >&2
  echo "[deploy] Detailed log: ${RESTORE_LOG}" >&2
  echo "[deploy] Press Enter to continue, or wait 15 minutes for automatic timeout" >&2
  if [[ -n "$INPUT_TTY" ]]; then
    read -r -t 900 _ < "$INPUT_TTY" || true
  else
    read -r -t 900 _ || true
  fi
}

setup_answers_support() {
  [[ "$ANSWERS_SUPPORT" == "yes" ]] || return 0

  local mount_root="/run/tuxwsmaker-answers"
  local by_label="/dev/disk/by-label/TUXWSANSWERS"
  local part_dev=""

  if [[ -e "$by_label" ]]; then
    part_dev="$by_label"
  else
    while IFS= read -r dev_path; do
      [[ -n "$dev_path" ]] || continue
      if [[ "$(blkid -s LABEL -o value "$dev_path" 2>/dev/null || true)" == "TUXWSANSWERS" ]]; then
        part_dev="$dev_path"
        break
      fi
    done < <(lsblk -rpn -o PATH,TYPE | awk '$2 == "part" {print $1}')
  fi

  if [[ -z "$part_dev" ]]; then
    status "Answers partition label TUXWSANSWERS not found; continuing without answers file"
    return 0
  fi

  mkdir -p "$mount_root"
  if ! mount -t vfat "$part_dev" "$mount_root" >/dev/null 2>&1; then
    status "Could not mount answers partition at $part_dev; continuing without answers file"
    return 0
  fi

  ANSWERS_MOUNT="$mount_root"
  RESTORE_LOG_MIRROR="$ANSWERS_MOUNT/deploy-restore.log"

  if [[ -f "$ANSWERS_MOUNT/answers.yaml" ]]; then
    ANSWERS_FILE="$ANSWERS_MOUNT/answers.yaml"
  elif [[ -f "$ANSWERS_MOUNT/answers.yml" ]]; then
    ANSWERS_FILE="$ANSWERS_MOUNT/answers.yml"
  elif [[ -f "$ANSWERS_MOUNT/answers" ]]; then
    ANSWERS_FILE="$ANSWERS_MOUNT/answers"
  fi

  if [[ -n "$ANSWERS_FILE" ]]; then
    status "Answers file detected: $ANSWERS_FILE"
  else
    status "Answers partition mounted but no answers file found (answers.yaml, answers.yml, answers)"
  fi
}

sync_answers_restore_log() {
  [[ -n "$RESTORE_LOG_MIRROR" ]] || return 0
  [[ -f "$RESTORE_LOG" ]] || return 0
  cp -f "$RESTORE_LOG" "$RESTORE_LOG_MIRROR" >/dev/null 2>&1 || true
}

prompt_read_line() {
  local prompt_text="$1"
  local timeout_seconds="$2"
  local out_var="$3"
  local line=""

  if [[ -n "$INPUT_TTY" ]]; then
    printf '%s' "$prompt_text" > "$INPUT_TTY"
    IFS= read -r -t "$timeout_seconds" line < "$INPUT_TTY" || true
    printf '\r\n' > "$INPUT_TTY" || true
  else
    printf '%s' "$prompt_text" >&2
    IFS= read -r -t "$timeout_seconds" line || true
    printf '\r\n' >&2 || true
  fi

  line="${line//$'\r'/}"
  line="${line#${line%%[![:space:]]*}}"
  line="${line%${line##*[![:space:]]}}"
  printf -v "$out_var" '%s' "$line"
}

prompt_write() {
  local message="$1"
  if [[ -n "$INPUT_TTY" ]]; then
    printf '%s\n' "$message" > "$INPUT_TTY"
  else
    printf '%s\n' "$message" >&2
  fi
}

on_error() {
  local exit_code=$?
  hold_on_error "$exit_code"
  exit "$exit_code"
}

trap on_error ERR

mkdir -p "$DEPLOY_ROOT" "$WORK_DIR"

status() {
  local msg="$1"
  echo "[deploy] $msg"
}

prompt_clear_target_disk() {
  local prompt="[deploy] Existing partitions detected on ${TARGET_DEV}. Clear disk and continue? [yes/no] (default: no): "
  local reply=""
  while true; do
    prompt_read_line "$prompt" 300 reply
    reply="${reply:-no}"
    case "${reply,,}" in
      y|yes|true|1)
        return 0
        ;;
      n|no|false|0|"")
        return 1
        ;;
      *)
        status "Please answer yes or no"
        ;;
    esac
  done
}

release_target_disk_usage() {
  status "Releasing mounts, swaps, and mappings on $TARGET_DEV"

  if command -v lsblk >/dev/null 2>&1; then
    while read -r mount_point; do
      [[ -n "${mount_point:-}" ]] || continue
      [[ "$mount_point" == "/run/install/repo" ]] && continue
      umount -lf "$mount_point" >/dev/null 2>&1 || true
    done < <(lsblk -nr -o MOUNTPOINT "$TARGET_DEV" | awk 'NF')
  fi

  while read -r swap_dev; do
    [[ -n "${swap_dev:-}" ]] || continue
    swapoff "$swap_dev" >/dev/null 2>&1 || true
  done < <(lsblk -nr -o PATH,FSTYPE "$TARGET_DEV" | awk '$2=="swap" {print $1}')

  while read -r crypt_name; do
    [[ -n "${crypt_name:-}" ]] || continue
    cryptsetup close "$crypt_name" >/dev/null 2>&1 || true
  done < <(lsblk -nr -o NAME,TYPE "$TARGET_DEV" | awk '$2=="crypt" {print $1}')

  if command -v vgchange >/dev/null 2>&1; then
    vgchange -an >/dev/null 2>&1 || true
  fi

  udevadm settle >/dev/null 2>&1 || true
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
  sync_answers_restore_log || true
  if [[ -n "$ANSWERS_MOUNT" ]]; then
    umount -lf "$ANSWERS_MOUNT" >/dev/null 2>&1 || true
  fi
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

resolve_lv_path() {
  local vg_name="$1"
  local lv_name="$2"
  local mapper_vg mapper_lv mapper_path

  if command -v dmsetup >/dev/null 2>&1; then
    dmsetup mknodes >/dev/null 2>&1 || true
  fi
  udevadm settle >/dev/null 2>&1 || true

  if [[ -n "$vg_name" && -n "$lv_name" && -b "/dev/$vg_name/$lv_name" ]]; then
    printf '/dev/%s/%s' "$vg_name" "$lv_name"
    return 0
  fi

  if [[ -n "$vg_name" && -n "$lv_name" ]]; then
    mapper_vg="${vg_name//-/--}"
    mapper_lv="${lv_name//-/--}"
    mapper_path="/dev/mapper/${mapper_vg}-${mapper_lv}"
    if [[ -b "$mapper_path" ]]; then
      printf '%s' "$mapper_path"
      return 0
    fi
  fi

  if ! command -v lvs >/dev/null 2>&1; then
    return 1
  fi

  local exact_path
  exact_path="$(lvs --noheadings -o vg_name,lv_name,lv_path --separator='|' 2>/dev/null | awk -F'|' -v want_vg="$vg_name" -v want_lv="$lv_name" '
    function trim(v) { sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v); return v }
    {
      vg=trim($1); lv=trim($2); path=trim($3)
      if (want_vg != "" && want_lv != "" && vg == want_vg && lv == want_lv) {
        print path
        exit
      }
    }
  ')"
  if [[ -n "$exact_path" && -b "$exact_path" ]]; then
    printf '%s' "$exact_path"
    return 0
  fi
  if [[ -n "$exact_path" && -e "$exact_path" ]]; then
    printf '%s' "$exact_path"
    return 0
  fi

  local lv_only_path
  lv_only_path="$(lvs --noheadings -o lv_name,lv_path --separator='|' 2>/dev/null | awk -F'|' -v want_lv="$lv_name" '
    function trim(v) { sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v); return v }
    {
      lv=trim($1); path=trim($2)
      if (want_lv != "" && lv == want_lv) {
        count += 1
        found = path
      }
    }
    END {
      if (count == 1) {
        print found
      }
    }
  ')"
  if [[ -n "$lv_only_path" && -b "$lv_only_path" ]]; then
    printf '%s' "$lv_only_path"
    return 0
  fi
  if [[ -n "$lv_only_path" && -e "$lv_only_path" ]]; then
    printf '%s' "$lv_only_path"
    return 0
  fi

  if [[ -n "$vg_name" && -n "$lv_name" ]]; then
    mapper_vg="${vg_name//-/--}"
    mapper_lv="${lv_name//-/--}"
    mapper_path="/dev/mapper/${mapper_vg}-${mapper_lv}"
    if lvs --noheadings "$vg_name/$lv_name" >/dev/null 2>&1; then
      printf '%s' "$mapper_path"
      return 0
    fi
  fi

  return 1
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

REQUIRED_SECTORS="$(python3 - "$WORK_DIR/clone-manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    clone = json.load(f)

sector = 512
partitions = sorted(clone.get("partitions", []), key=lambda p: int(p.get("number") or 0))
starts = [max(0, int(p.get("start_byte") or 0) // sector) for p in partitions]
sizes = [max(1, int(p.get("size_bytes") or 0) // sector) for p in partitions]
default_total = max((s + z) for s, z in zip(starts, sizes)) if partitions else 0
required = max(default_total, int((clone.get("disk_size_bytes") or 0) // sector))
print(required)
PY
)"
prompt_for_install_disk() {
  local override="${TARGET_DISK:-}"
  if [[ -n "$override" ]]; then
    printf '%s\n' "$override"
    return 0
  fi

  local -a disk_rows=()
  while IFS= read -r row; do
    [[ -n "$row" ]] || continue
    local dev type bytes sectors
    dev="$(printf '%s\n' "$row" | awk '{print $1}')"
    type="$(printf '%s\n' "$row" | awk '{print $2}')"

    [[ "$dev" == /dev/* ]] || continue
    [[ "$type" == "disk" ]] || continue

    bytes="$(blockdev --getsize64 "$dev" 2>/dev/null || lsblk -b -dn -o SIZE "$dev" 2>/dev/null | awk '{print $1}')"
    bytes="${bytes//[[:space:]]/}"
    if ! [[ "$bytes" =~ ^[0-9]+$ ]] || [[ "$bytes" -le 0 ]]; then
      continue
    fi
    sectors="$((bytes / 512))"
    disk_rows+=("${dev}|${bytes}|${sectors}")
  done < <(lsblk -b -dn -o PATH,TYPE,SIZE 2>/dev/null)

  if [[ ${#disk_rows[@]} -eq 0 ]]; then
    echo "[deploy] Could not find target disk large enough for restore (required sectors: $REQUIRED_SECTORS)" >&2
    return 1
  fi

  if [[ ${#disk_rows[@]} -eq 1 ]]; then
    local only_disk only_sectors
    only_disk="${disk_rows[0]%%|*}"
    only_sectors="${disk_rows[0]##*|}"
    if (( only_sectors < REQUIRED_SECTORS )); then
      echo "[deploy] Could not find target disk large enough for restore (required sectors: $REQUIRED_SECTORS)" >&2
      return 1
    fi
    printf '%s\n' "$only_disk"
    return 0
  fi

  local choice=""
  while true; do
    prompt_write "[deploy] Multiple candidate disks detected for restore. Choose the target disk:"
    local index=1
    for entry in "${disk_rows[@]}"; do
      local disk_name disk_bytes disk_sectors disk_note
      disk_name="${entry%%|*}"
      disk_bytes="${entry#*|}"
      disk_bytes="${disk_bytes%%|*}"
      disk_sectors="${entry##*|}"
      disk_note=""
      if (( disk_sectors < REQUIRED_SECTORS )); then
        disk_note=" [too small]"
      fi
      prompt_write "[deploy]   ${index}) ${disk_name} (${disk_bytes} bytes, ${disk_sectors} sectors)${disk_note}"
      index=$((index + 1))
    done

    prompt_read_line "[deploy] Select a disk number: " 300 choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#disk_rows[@]} )); then
      local selected_disk selected_sectors
      selected_disk="${disk_rows[choice - 1]%%|*}"
      selected_sectors="${disk_rows[choice - 1]##*|}"
      if (( selected_sectors < REQUIRED_SECTORS )); then
        prompt_write "[deploy] Selected disk is too small for restore (required sectors: $REQUIRED_SECTORS). Choose another disk."
        continue
      fi
      printf '%s\n' "$selected_disk"
      return 0
    fi
    prompt_write "[deploy] Invalid choice. Please enter one of the numbered options."
  done
}

TARGET_DISK="$(prompt_for_install_disk)"
TARGET_DISK="${TARGET_DISK// /}"
TARGET_DISK="${TARGET_DISK#/dev/}"
if [[ -z "$TARGET_DISK" ]]; then
  echo "[deploy] Could not find target disk large enough for restore (required sectors: $REQUIRED_SECTORS)" >&2
  exit 1
fi
TARGET_DEV="/dev/$TARGET_DISK"
status "Target disk: $TARGET_DEV"

TARGET_SECTORS="$(blockdev --getsz "$TARGET_DEV" 2>/dev/null || lsblk -b -dn -o SIZE "$TARGET_DEV" 2>/dev/null | awk '{print int($1 / 512)}')"
TARGET_SECTORS="${TARGET_SECTORS//[[:space:]]/}"
if ! [[ "${TARGET_SECTORS:-}" =~ ^[0-9]+$ ]] || [[ "$TARGET_SECTORS" -le 0 ]]; then
  echo "[deploy] Could not determine target disk size in sectors for $TARGET_DEV" >&2
  exit 1
fi
status "Target disk sectors: $TARGET_SECTORS"

CURRENT_PHASE="partition-layout-generation"
python3 - "$WORK_DIR/deploy.json" "$WORK_DIR/clone-manifest.json" "$WORK_DIR/sfdisk.layout" "$WORK_DIR/parts.map" "$TARGET_SECTORS" <<'PY'
import json
import sys

deploy_path, clone_path, sfdisk_path, map_path, target_sectors_raw = sys.argv[1:6]
with open(deploy_path, encoding="utf-8") as f:
    deploy = json.load(f)
with open(clone_path, encoding="utf-8") as f:
    clone = json.load(f)

try:
  target_sectors = max(0, int(target_sectors_raw or 0))
except ValueError as exc:
  raise SystemExit(f"Invalid target sector count: {target_sectors_raw}") from exc

table_type = str(clone.get("table_type") or deploy.get("boot", {}).get("table_type") or "gpt").lower()
layout_entries = sorted(
  deploy.get("layout_entries", []),
  key=lambda e: int(e.get("partition_number") or e.get("order") or 0),
)
layout_by_number = {
  int(e.get("partition_number") or e.get("order") or 0): e
  for e in layout_entries
  if e.get("partition_number") is not None or e.get("order") is not None
}
partitions = sorted(clone.get("partitions", []), key=lambda p: int(p.get("number") or 0))
if not partitions:
    raise SystemExit("No partitions found in clone manifest")

sector = 512
sfdisk_lines = [f"label: {table_type}", "unit: sectors"]
map_lines = []
starts = [max(0, int(p.get("start_byte") or 0) // sector) for p in partitions]
sizes = [max(1, int(p.get("size_bytes") or 0) // sector) for p in partitions]
default_total = max((s + z) for s, z in zip(starts, sizes))
disk_total = max(default_total, int((clone.get("disk_size_bytes") or 0) // sector), target_sectors)
usable_sector_limit = disk_total
if table_type == "gpt" and disk_total > 34:
  # GPT reserves the final sectors for the backup header/table.
  # For exclusive-end arithmetic we cap at (last_usable + 1) = total - 33.
  usable_sector_limit = disk_total - 33
for idx, part in enumerate(partitions):
    number = int(part["number"])
    start_sector = starts[idx]
    size_sector = sizes[idx]
    entry = layout_by_number.get(number, {})
    size_mode = str(entry.get("size_mode") or "fixed").strip().lower()

    if size_mode == "remainder":
      next_start = starts[idx + 1] if idx + 1 < len(starts) else usable_sector_limit
      next_start = min(next_start, usable_sector_limit)
      available = max(1, next_start - start_sector)
      if available < size_sector:
          raise SystemExit(f"Target disk too small for remainder partition {number}")
      size_sector = available

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
          str(entry.get("entry_role") or ""),
      str(entry.get("volume_group") or ""),
      str(entry.get("logical_volume") or ""),
          str(entry.get("size_mode") or "fixed"),
    ]))

with open(sfdisk_path, "w", encoding="utf-8") as f:
    f.write("\\n".join(sfdisk_lines) + "\\n")
with open(map_path, "w", encoding="utf-8") as f:
    f.write("\\n".join(map_lines) + "\\n")
PY

CURRENT_PHASE="partition-table-creation"
status "Creating target partition table"

if lsblk -nr -o TYPE "$TARGET_DEV" 2>/dev/null | grep -q '^part$'; then
  status "Target disk currently has partitions"
  if ! prompt_clear_target_disk; then
    status "Operator declined disk clear; aborting restore"
    exit 1
  fi

  release_target_disk_usage
fi

wipefs -a -f -q "$TARGET_DEV" || true
if command -v sgdisk >/dev/null 2>&1; then
  sgdisk --zap-all "$TARGET_DEV" >/dev/null 2>&1 || true
fi
dd if=/dev/zero of="$TARGET_DEV" bs=1M count=16 conv=fsync status=none || true

sfdisk --force --wipe always --wipe-partitions always "$TARGET_DEV" < "$WORK_DIR/sfdisk.layout"
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

restore_partition_payload_to() {
  local restore_target="$1"

  status "[$PART_INDEX/$TOTAL_PARTS] Restoring partition $number ($file_name) to $restore_target"
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
      return 0
    fi
    if [[ "${payload_format:-raw}" == "sparse-extents-v1" && -n "${extents_path:-}" ]]; then
      status "[$PART_INDEX/$TOTAL_PARTS] Applying sparse extents for partition $number"
      compressed_flag=0
      [[ "${compressed:-False}" == "True" || "${compressed:-false}" == "true" ]] && compressed_flag=1
      apply_sparse_extents_file "$image_path" "$extents_path" "$restore_target" "$compressed_flag"
    elif [[ "$image_path" == *.gz ]]; then
      status "[$PART_INDEX/$TOTAL_PARTS] Writing sparse blocks for partition $number"
      gzip -dc "$image_path" | sparse_restore_stream "$restore_target" 65536
    else
      status "[$PART_INDEX/$TOTAL_PARTS] Writing sparse blocks for partition $number"
      write_sparse_blocks "$image_path" "$restore_target" 65536
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
      apply_sparse_extents_file "$tmp_payload" "$tmp_extents" "$restore_target" "$compressed_flag"
      rm -f "$tmp_payload" "$tmp_extents"
    elif [[ "$image_url" == *.gz ]]; then
      curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$image_url" | gzip -dc | sparse_restore_stream "$restore_target" 65536
    else
      curl --fail --show-error --location --connect-timeout 10 --max-time 120 "$image_url" | sparse_restore_stream "$restore_target" 65536
    fi
  fi
}

CURRENT_PHASE="partition-restore"
while IFS='|' read -r number file_name extents_file payload_format compressed mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
  [[ -z "${number:-}" ]] && continue
  PART_INDEX=$((PART_INDEX + 1))
  if [[ -z "${file_name:-}" ]]; then
    echo "[deploy] Missing file_name for partition $number in parts.map" >&2
    exit 1
  fi

  part_dev=$(raw_part "$TARGET_DISK" "$number")
  image_url="$CLONE_BASE/$file_name"
  if [[ "$mount_point" == "swap" || "$fs_type" == "swap" ]]; then
    luks_enabled="False"
    luks_name=""
  fi
  status "[$PART_INDEX/$TOTAL_PARTS] Partition metadata: mount=${mount_point:-none} fs=${fs_type:-none} role=${entry_role:-none} luks=${luks_enabled:-False} name=${luks_name:-none}"
  echo "$number|$part_dev|$mount_point|$fs_type|$luks_enabled|$luks_name|$entry_role|$volume_group|$logical_volume|$size_mode" >> "$WORK_DIR/part-dev.map"

  # Partition payloads are captured from raw block devices. Even for encrypted
  # volumes, restore must write back to the raw partition device.
  restore_target="$part_dev"

  restore_partition_payload_to "$restore_target"

  if [[ "$luks_enabled" == "True" ]]; then
    map_name="${luks_name:-luks-${number}}"
    if ! cryptsetup isLuks "$part_dev" >/dev/null 2>&1; then
      status "[$PART_INDEX/$TOTAL_PARTS] No LUKS header detected on $part_dev; bootstrapping LUKS and replaying payload"
      if [[ -z "${DEFAULT_LUKS_PASSWORD:-}" ]]; then
        echo "[deploy] DEFAULT_LUKS_PASSWORD is empty; cannot bootstrap LUKS for $part_dev" >&2
        exit 1
      fi
      printf '%s' "$DEFAULT_LUKS_PASSWORD" | cryptsetup luksFormat --type luks2 --batch-mode "$part_dev" -
      printf '%s' "$DEFAULT_LUKS_PASSWORD" | cryptsetup open "$part_dev" "$map_name" -
      restore_partition_payload_to "/dev/mapper/$map_name"
    fi
  fi

  status "[$PART_INDEX/$TOTAL_PARTS] Partition $number restore completed"
done < "$WORK_DIR/parts.map"

CURRENT_PHASE="filesystem-and-boot-repair"
sync
partprobe "$TARGET_DEV" || true
if command -v sgdisk >/dev/null 2>&1; then
  # On larger restore targets, relocate backup GPT header/table to the actual end.
  sgdisk -e "$TARGET_DEV" >/dev/null 2>&1 || true
fi
partprobe "$TARGET_DEV" || true
udevadm settle || true

while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
  [[ -z "${number:-}" ]] && continue
  [[ "$luks_enabled" == "True" ]] || continue
  map_name="${luks_name:-luks-${number}}"
  if ! cryptsetup isLuks "$part_dev" >/dev/null 2>&1; then
    echo "[deploy] Expected LUKS container but did not detect one on $part_dev" >&2
    exit 1
  fi
  if ! cryptsetup status "$map_name" >/dev/null 2>&1; then
    if [[ -z "${DEFAULT_LUKS_PASSWORD:-}" ]]; then
      echo "[deploy] DEFAULT_LUKS_PASSWORD is empty; cannot open LUKS mapping for $part_dev" >&2
      exit 1
    fi
    printf '%s' "$DEFAULT_LUKS_PASSWORD" | cryptsetup open "$part_dev" "$map_name" -
  fi
done < "$WORK_DIR/part-dev.map"

status "Rescanning and activating LVM volumes"
if command -v dmsetup >/dev/null 2>&1; then
  dmsetup mknodes >/dev/null 2>&1 || true
fi
if command -v pvscan >/dev/null 2>&1; then
  pvscan --cache >/dev/null 2>&1 || true
fi
if command -v vgscan >/dev/null 2>&1; then
  vgscan --mknodes >/dev/null 2>&1 || true
fi
if command -v lvscan >/dev/null 2>&1; then
  lvscan >/dev/null 2>&1 || true
fi
if command -v vgchange >/dev/null 2>&1; then
  vgchange -ay --sysinit >/dev/null 2>&1 || vgchange -ay >/dev/null 2>&1 || true
fi
if command -v lvchange >/dev/null 2>&1; then
  lvchange -ay >/dev/null 2>&1 || true
fi

status "Expanding remainder PV containers"
while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
  [[ "$entry_role" == "pv" && "$size_mode" == "remainder" ]] || continue
  pv_target="$part_dev"
  if [[ "$luks_enabled" == "True" ]]; then
    map_name="${luks_name:-luks-${number}}"
    if cryptsetup status "$map_name" >/dev/null 2>&1; then
      cryptsetup resize "$map_name" >/dev/null 2>&1 || true
      pv_target="/dev/mapper/$map_name"
    else
      status "WARNING: LUKS mapping $map_name is not active; skipping PV resize for partition $number"
      continue
    fi
  fi
  if command -v pvresize >/dev/null 2>&1 && [[ -n "$pv_target" ]]; then
    pvresize "$pv_target" >/dev/null 2>&1 || true
  fi
done < "$WORK_DIR/part-dev.map"

CURRENT_PHASE="lv-layout-map"
status "Merging LV layout metadata into restore device map"
python3 - "$WORK_DIR/deploy.json" "$WORK_DIR/part-dev.map" <<'PY'
import json
import sys

deploy_path, map_path = sys.argv[1:3]

with open(deploy_path, encoding="utf-8") as deploy_file:
  deploy = json.load(deploy_file)

existing_numbers = set()
try:
  with open(map_path, encoding="utf-8") as map_file:
    for raw in map_file:
      raw = raw.strip()
      if not raw:
        continue
      number = raw.split("|", 1)[0].strip()
      if number.isdigit():
        existing_numbers.add(int(number))
except FileNotFoundError:
  pass

append_rows = []
for entry in sorted(
    deploy.get("layout_entries", []),
    key=lambda e: int(e.get("partition_number") or e.get("order") or 0),
):
  if str(entry.get("entry_role") or "").strip().lower() != "lv":
    continue
  number = int(entry.get("partition_number") or entry.get("order") or 0)
  if number <= 0 or number in existing_numbers:
    continue
  append_rows.append("|".join([
    str(number),
    "",
    str(entry.get("mount_point") or ""),
    str(entry.get("filesystem") or ""),
    str(entry.get("luks_enabled") or False),
    str(entry.get("luks_name") or ""),
    str(entry.get("entry_role") or ""),
    str(entry.get("volume_group") or ""),
    str(entry.get("logical_volume") or ""),
    str(entry.get("size_mode") or "fixed"),
  ]))

if append_rows:
  with open(map_path, "a", encoding="utf-8") as map_file:
    map_file.write("\\n".join(append_rows) + "\\n")
  print(f"[deploy] Added {len(append_rows)} LV entries to part-dev map")
else:
  print("[deploy] No additional LV entries needed in part-dev map")
PY

while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
  [[ "$entry_role" == "lv" && "$size_mode" == "remainder" ]] || continue
  [[ -n "${volume_group:-}" && -n "${logical_volume:-}" ]] || continue
  lv_path="$(resolve_lv_path "$volume_group" "$logical_volume" || true)"
  [[ -n "$lv_path" ]] || continue
  if command -v lvdisplay >/dev/null 2>&1 && lvdisplay "$lv_path" >/dev/null 2>&1; then
    lvextend -l +100%FREE "$lv_path" >/dev/null 2>&1 || true
  fi
done < "$WORK_DIR/part-dev.map"

status "Mounting restored filesystems"
: > "$WORK_DIR/mounted.paths"
ROOT_DEV=""
ROOT_ENTRY_FOUND=0
while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
  [[ -z "${number:-}" ]] && continue
  if [[ "$mount_point" == "/" ]]; then
    ROOT_ENTRY_FOUND=1
    if [[ "$entry_role" == "lv" && -n "${volume_group:-}" && -n "${logical_volume:-}" ]]; then
      ROOT_DEV="$(resolve_lv_path "$volume_group" "$logical_volume" || true)"
      if [[ -z "$ROOT_DEV" ]]; then
        status "Could not resolve LV path for root mount: ${volume_group}/${logical_volume}"
      fi
    else
      ROOT_DEV="$part_dev"
    fi
    break
  fi
done < "$WORK_DIR/part-dev.map"

if [[ "$ROOT_ENTRY_FOUND" != "1" ]]; then
  status "ERROR: Root mountpoint not found in deploy metadata"
  if [[ -f "$WORK_DIR/part-dev.map" ]]; then
    status "Current part-dev map contents:"
    cat "$WORK_DIR/part-dev.map" || true
  fi
  if command -v lvs >/dev/null 2>&1; then
    status "Available logical volumes:"
    lvs -o vg_name,lv_name,lv_path || true
  fi
  exit 1
elif [[ -z "$ROOT_DEV" ]]; then
  status "ERROR: Root device could not be resolved from deploy metadata"
  if [[ -f "$WORK_DIR/part-dev.map" ]]; then
    status "Current part-dev map contents:"
    cat "$WORK_DIR/part-dev.map" || true
  fi
  if command -v lvs >/dev/null 2>&1; then
    status "Available logical volumes:"
    lvs -o vg_name,lv_name,lv_path || true
  fi
  if command -v ls >/dev/null 2>&1; then
    status "Current device-mapper entries:"
    ls -l /dev/mapper || true
  fi
  exit 1
else
  CURRENT_PHASE="root-mount"
  if [[ ! -b "$ROOT_DEV" ]]; then
    status "Root device $ROOT_DEV is not a block device"
    if command -v lvs >/dev/null 2>&1; then
      status "Available logical volumes:"
      lvs -o vg_name,lv_name,lv_path || true
    fi
    if command -v lsblk >/dev/null 2>&1; then
      status "Available block devices:"
      lsblk -f || true
    fi
    exit 1
  fi
  mkdir -p "$MOUNT_ROOT"
  _fstype=$(blkid -o value -s TYPE "$ROOT_DEV" 2>/dev/null || true)
  if echo "$_fstype" | grep -qE '^ext[234]$'; then
    fsck -y "$ROOT_DEV" >/dev/null 2>&1 || true
  elif [[ "$_fstype" == "xfs" ]] && command -v xfs_repair >/dev/null 2>&1; then
    xfs_repair "$ROOT_DEV" >/dev/null 2>&1 || xfs_repair -L "$ROOT_DEV" >/dev/null 2>&1 || true
  fi
  if ! mount "$ROOT_DEV" "$MOUNT_ROOT"; then
    status "ERROR: Could not mount root device $ROOT_DEV on $MOUNT_ROOT"
    dmesg | tail -5 || true
    exit 1
  fi
  echo "$MOUNT_ROOT" >> "$WORK_DIR/mounted.paths"

  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
    [[ -z "${number:-}" ]] && continue
    [[ -z "${mount_point:-}" ]] && continue
    [[ "$mount_point" == "/" || "$mount_point" == "swap" ]] && continue
    if [[ "$entry_role" == "lv" && -n "${volume_group:-}" && -n "${logical_volume:-}" ]]; then
      source_dev="$(resolve_lv_path "$volume_group" "$logical_volume" || true)"
      if [[ -z "$source_dev" ]]; then
        status "Could not resolve LV path for ${volume_group}/${logical_volume}; skipping mount of $mount_point"
        continue
      fi
    else
      source_dev="$part_dev"
    fi
    [[ "$entry_role" == "pv" ]] && continue
    target="$MOUNT_ROOT$mount_point"
    mkdir -p "$target"
    _fstype=$(blkid -o value -s TYPE "$source_dev" 2>/dev/null || true)
    if echo "$_fstype" | grep -qE '^ext[234]$'; then
      fsck -y "$source_dev" >/dev/null 2>&1 || true
    elif [[ "$_fstype" == "xfs" ]] && command -v xfs_repair >/dev/null 2>&1; then
      xfs_repair "$source_dev" >/dev/null 2>&1 || xfs_repair -L "$source_dev" >/dev/null 2>&1 || true
    fi
    if mount "$source_dev" "$target"; then
      echo "$target" >> "$WORK_DIR/mounted.paths"
    else
      status "WARNING: Could not mount $source_dev on $target — skipping"
      dmesg | tail -3 2>/dev/null || true
    fi
  done < "$WORK_DIR/part-dev.map"

  CURRENT_PHASE="filesystem-grow"
  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
    [[ "$size_mode" == "remainder" ]] || continue
    [[ -n "${mount_point:-}" && "$mount_point" != "swap" ]] || continue
    if [[ "$entry_role" == "lv" && -n "${volume_group:-}" && -n "${logical_volume:-}" ]]; then
      source_dev="$(resolve_lv_path "$volume_group" "$logical_volume" || true)"
      if [[ -z "$source_dev" ]]; then
        status "Could not resolve LV path for ${volume_group}/${logical_volume}; skipping grow for $mount_point"
        continue
      fi
    else
      source_dev="$part_dev"
    fi

    grow_target="$MOUNT_ROOT"
    if [[ "$mount_point" != "/" ]]; then
      grow_target="$MOUNT_ROOT$mount_point"
    fi

    case "$fs_type" in
      xfs)
        command -v xfs_growfs >/dev/null 2>&1 && xfs_growfs "$grow_target" >/dev/null 2>&1 || true
        ;;
      ext2|ext3|ext4)
        command -v resize2fs >/dev/null 2>&1 && resize2fs "$source_dev" >/dev/null 2>&1 || true
        ;;
      btrfs)
        command -v btrfs >/dev/null 2>&1 && btrfs filesystem resize max "$grow_target" >/dev/null 2>&1 || true
        ;;
    esac
  done < "$WORK_DIR/part-dev.map"

  CURRENT_PHASE="fstab-rebuild"
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

  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
    [[ "$mount_point" == "swap" || "$fs_type" == "swap" ]] || continue
    swap_uuid=$(blkid -s UUID -o value "$part_dev" 2>/dev/null || true)
    swap_opts="defaults,nofail,x-systemd.device-timeout=10s"
    if [[ -n "$swap_uuid" ]]; then
      echo "UUID=$swap_uuid none swap $swap_opts 0 0" >> "$MOUNT_ROOT/etc/fstab"
    else
      echo "$part_dev none swap $swap_opts 0 0" >> "$MOUNT_ROOT/etc/fstab"
    fi
  done < "$WORK_DIR/part-dev.map"

  CURRENT_PHASE="crypttab-rebuild"
  status "Rebuilding /etc/crypttab"
  : > "$MOUNT_ROOT/etc/crypttab"
  while IFS='|' read -r number part_dev mount_point fs_type luks_enabled luks_name entry_role volume_group logical_volume size_mode; do
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
    CURRENT_PHASE="hostname-setup"
    status "Setting hostname to $TARGET_HOSTNAME"
    echo -n "$TARGET_HOSTNAME" > "$MOUNT_ROOT/etc/hostname"
  fi

  CURRENT_PHASE="bind-mount-runtime"
  for bind_path in /dev /dev/pts /proc /sys /sys/firmware/efi/efivars /run; do
    mkdir -p "$MOUNT_ROOT$bind_path"
    mount --bind "$bind_path" "$MOUNT_ROOT$bind_path" || true
  done

  CURRENT_PHASE="bootloader-repair"
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
  if command -v grub2-install >/dev/null 2>&1 && [[ -d /usr/lib/grub/x86_64-efi || -d /usr/lib/grub2/x86_64-efi ]]; then
    grub2-install "$TARGET_DEV" && touch /tmp/tuxwsmaker-grub-install.ok || true
  elif command -v grub-install >/dev/null 2>&1 && [[ -d /usr/lib/grub/x86_64-efi || -d /usr/lib/grub2/x86_64-efi ]]; then
    grub-install "$TARGET_DEV" && touch /tmp/tuxwsmaker-grub-install.ok || true
  else
    echo "UEFI grub-install prerequisites not found in chroot; skipping explicit grub-install" >&2
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
    CURRENT_PHASE="uefi-boot-entry-refresh"
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

  setup_answers_support

  AFTERBURNER_SRC="/run/install/repo/deploy/afterburner.sh"
  AFTERBURNER_RUN="$WORK_DIR/afterburner.sh"
  TARGET_RESOLV_BACKUP="$WORK_DIR/target-resolv.conf.before-afterburner"
  TARGET_RESOLV_INJECTED=0
  if [[ -f "$AFTERBURNER_SRC" ]]; then
    CURRENT_PHASE="afterburner"
    if [[ -f /etc/resolv.conf ]]; then
      mkdir -p "$MOUNT_ROOT/etc"
      if [[ -e "$MOUNT_ROOT/etc/resolv.conf" || -L "$MOUNT_ROOT/etc/resolv.conf" ]]; then
        cp -a "$MOUNT_ROOT/etc/resolv.conf" "$TARGET_RESOLV_BACKUP" 2>/dev/null || true
      fi
      cp -L /etc/resolv.conf "$MOUNT_ROOT/etc/resolv.conf" 2>/dev/null || true
      TARGET_RESOLV_INJECTED=1
      status "Injected live installer resolver into target (overwriting build-time DNS)"
    fi
__REPO_SETUP__
    status "Running afterburner in restore context"
    cp "$AFTERBURNER_SRC" "$AFTERBURNER_RUN"
    chmod +x "$AFTERBURNER_RUN"
    set +e
    MOUNT_ROOT="$MOUNT_ROOT" TARGET_DEV="$TARGET_DEV" WORK_DIR="$WORK_DIR" ANSWERS_FILE="$ANSWERS_FILE" bash "$AFTERBURNER_RUN"
    afterburner_exit=$?
    set -e
__REPO_CLEANUP__
    if [[ -e "$TARGET_RESOLV_BACKUP" || -L "$TARGET_RESOLV_BACKUP" ]]; then
      cp -a "$TARGET_RESOLV_BACKUP" "$MOUNT_ROOT/etc/resolv.conf" 2>/dev/null || true
    elif [[ "$TARGET_RESOLV_INJECTED" == "1" ]]; then
      rm -f "$MOUNT_ROOT/etc/resolv.conf" 2>/dev/null || true
    fi
    rm -f "$AFTERBURNER_RUN" "$TARGET_RESOLV_BACKUP" 2>/dev/null || true
    if [[ $afterburner_exit -ne 0 ]]; then
      status "Afterburner failed"
      exit $afterburner_exit
    fi
    status "Afterburner completed"
  else
    status "No afterburner script found at /run/install/repo/deploy/afterburner.sh; skipping"
  fi
fi

CURRENT_PHASE="completed"
sync_answers_restore_log || true
status "Restore completed successfully"
__FINISH_STEP__
"""
    ).lstrip()

    content = content.replace("__FINISH_STEP__", finish_step)
    content = content.replace("__REPO_SETUP__", repo_setup.rstrip() + ("\n" if repo_setup else ""))
    content = content.replace("__REPO_CLEANUP__", repo_cleanup.rstrip() + ("\n" if repo_cleanup else ""))
    content = content.replace("__ANSWERS_SUPPORT__", "yes" if build is not None and bool(build.enable_answers_file_support) else "no")
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
set -Eeuo pipefail
PRE_INPUT_TTY="${{DEPLOY_INPUT_TTY:-/dev/tty2}}"
if [ ! -c "$PRE_INPUT_TTY" ]; then
  PRE_INPUT_TTY=""
fi
if [ -z "$PRE_INPUT_TTY" ] && [ -c /dev/tty ]; then
  PRE_INPUT_TTY="/dev/tty"
fi
if [ -z "$PRE_INPUT_TTY" ] && [ -c /dev/console ]; then
  PRE_INPUT_TTY="/dev/console"
fi
if [ -c /dev/console ]; then
  echo "[deploy-pre] Interactive terminal selected: ${{PRE_INPUT_TTY:-none}}" > /dev/console 2>/dev/null || true
fi
if [ -n "$PRE_INPUT_TTY" ] && [ -c "$PRE_INPUT_TTY" ]; then
  exec < "$PRE_INPUT_TTY" > "$PRE_INPUT_TTY" 2>&1
fi
export DEPLOY_INPUT_TTY="$PRE_INPUT_TTY"
if [ -t 1 ] && command -v stty >/dev/null 2>&1; then
  stty sane echo icrnl onlcr 2>/dev/null || true
fi
pre_hold_on_error() {{
  local exit_code="$1"
  echo "[deploy-pre] ERROR: pre-script failed with exit code ${{exit_code}}" >&2
  echo "[deploy-pre] Command: ${{BASH_COMMAND:-unknown}}" >&2
  echo "[deploy-pre] Line: ${{BASH_LINENO[0]:-unknown}}" >&2
  echo "[deploy-pre] Press Enter to continue, or wait 15 minutes for automatic timeout" >&2
  read -r -t 900 _ || true
}}
pre_on_error() {{
  local exit_code=$?
  pre_hold_on_error "$exit_code"
  exit "$exit_code"
}}
trap pre_on_error ERR
echo "[deploy-pre] Starting at $(date -Is)"
export DEPLOY_MANIFEST_URL={deploy_manifest_url}
export CLONE_MANIFEST_URL={clone_manifest_url}
if [ ! -f /run/install/repo/deploy/restore.sh ]; then
    echo "Missing local restore script in /run/install/repo/deploy/restore.sh" >&2
    exit 1
fi
cp /run/install/repo/deploy/restore.sh /tmp/restore.sh
chmod +x /tmp/restore.sh
restore_rc=0
if command -v tmux >/dev/null 2>&1 && tmux display-message -p '#S' >/dev/null 2>&1; then
  RESTORE_WAIT_KEY="tuxwsmaker-restore-done-$$"
  RESTORE_RC_FILE="/tmp/tuxwsmaker-restore.rc"
  cat > /tmp/tuxwsmaker-restore-tmux-runner.sh <<'TMUX_RESTORE'
#!/usr/bin/env bash
set +e
bash /tmp/restore.sh
rc=$?
echo "$rc" > "$1"
tmux wait-for -S "$2"
if [[ "$rc" -ne 0 ]]; then
  echo
  echo "[deploy-pre] Restore session complete (rc=$rc). Press Enter to continue, or wait 15 minutes for automatic timeout"
  read -r -t 900 _ || true
fi
exit "$rc"
TMUX_RESTORE
  chmod +x /tmp/tuxwsmaker-restore-tmux-runner.sh
  rm -f "$RESTORE_RC_FILE"
  tmux new-window -d -n restore-io "/tmp/tuxwsmaker-restore-tmux-runner.sh '$RESTORE_RC_FILE' '$RESTORE_WAIT_KEY'"
  tmux select-window -t restore-io || true
  tmux wait-for "$RESTORE_WAIT_KEY"
  restore_rc=$(cat "$RESTORE_RC_FILE" 2>/dev/null || echo 1)
else
  bash /tmp/restore.sh || restore_rc=$?
fi

if [[ "$restore_rc" -ne 0 ]]; then
  trap - ERR
  echo "[deploy-pre] ERROR: restore.sh failed with exit code ${{restore_rc}}" >&2
  if [ -f /tmp/tuxwsmaker-restore.log ]; then
    echo "[deploy-pre] ---- restore log tail ----" >&2
    tail -n 200 /tmp/tuxwsmaker-restore.log >&2 || true
    echo "[deploy-pre] ---- end restore log tail ----" >&2
  fi
  echo "[deploy-pre] Press Enter to continue, or wait 15 minutes for automatic timeout" >&2
  read -r -t 900 _ || true
  exit "${{restore_rc}}"
fi
echo "[deploy-pre] Restore script finished at $(date -Is)"
cat <<'EOF'
================================================================
Restore complete.

Restore looks complete. The system will reboot automatically.
================================================================
EOF
echo "[deploy-pre] Restore complete; rebooting automatically"
trap - ERR
sync
reboot -f || systemctl reboot -f || reboot || poweroff -f || halt -f
%end
"""
    path.write_text(content, encoding="utf-8")
    return path
