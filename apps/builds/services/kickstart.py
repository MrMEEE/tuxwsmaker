from __future__ import annotations

from pathlib import Path

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
) -> dict[str, str]:
    bios = (
        "DEFAULT tuxwsmaker\n"
        "PROMPT 0\n"
        "TIMEOUT 20\n"
        "LABEL tuxwsmaker\n"
        f"  KERNEL /{kernel_rel_path}\n"
        f"  APPEND initrd=/{initrd_rel_path} inst.ks={kickstart_url} inst.repo={install_source_url} inst.stage2={install_source_url} console=ttyS0 ip=dhcp\n"
    )
    efi = (
        "set timeout=2\n"
        "set default=0\n"
        "menuentry 'TuxWSMaker Build' {\n"
        f"  linuxefi /{kernel_rel_path} initrd=/{initrd_rel_path} inst.ks={kickstart_url} inst.repo={install_source_url} inst.stage2={install_source_url} console=ttyS0 ip=dhcp\n"
        f"  initrdefi /{initrd_rel_path}\n"
        "}\n"
    )
    return {
        "bios": bios,
        "efi": efi,
    }
