from __future__ import annotations

import base64
import secrets
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.db import transaction

from apps.builds.models import BuildArtifact, BuildDefinition, BuildLogEntry, BuildMachineConfig, SSHKey
from apps.builds.services.artifacts import (
    ArtifactExportError,
    dump_clone_partitions,
    generate_artifacts,
    prepare_iso_pxe_assets,
)
from apps.builds.services.builder import BuilderError, BuilderVMManager
from apps.builds.services.kickstart import calculate_layout_disk_size_gib, render_kickstart_file, render_pxe_boot_configs
from apps.builds.services.provisioning import AnsibleProvisioner, ProvisioningError
from apps.builds.services.ssh_keys import SSHKeyError, generate_build_ssh_keypair
from apps.builds.services.virtualization import LibvirtVMManager, VMDefinition, VirtualizationError
from apps.playbooks.services import PlaybookSyncError, checkout_repository, checkout_repository_url
from apps.packages.models import PackageList
from apps.repositories.services import render_repository_activation_snippet, render_repository_cleanup_snippet
from apps.realtime.events import publish_event
from apps.serverconfig.models import ServerConfiguration
from config.celery import app as celery_app


BUILD_TASK_CACHE_TTL_SECONDS = 6 * 60 * 60


def build_task_cache_key(build_id: int) -> str:
    return f"builds:active-task:{build_id}"


def _task_state(task_id: str) -> str:
    if not task_id:
        return ""
    try:
        return str(celery_app.AsyncResult(task_id).state or "").upper()
    except Exception:
        return ""


def reconcile_stale_build_states_on_startup() -> int:
    recovered = 0
    try:
        stuck_builds = BuildDefinition.objects.filter(
            status__in=[BuildDefinition.STATUS_RUNNING, BuildDefinition.STATUS_QUEUED]
        ).only("id", "status", "current_step", "runtime_state", "updated_at")
    except DatabaseError:
        return 0

    for build in stuck_builds:
        state = dict(build.runtime_state or {})
        runtime_task_id = str(state.get("active_task_id") or "").strip()
        cache_task_id = str(cache.get(build_task_cache_key(build.id)) or "").strip()
        task_id = runtime_task_id or cache_task_id
        task_state = _task_state(task_id)
        if task_state in {"RECEIVED", "STARTED", "RETRY"}:
            continue
        if task_state == "PENDING" and task_id and cache_task_id == task_id:
            # Task is still known in cache and likely queued; leave it alone.
            continue

        state.pop("active_task_id", None)
        build.runtime_state = state
        build.status = BuildDefinition.STATUS_FAILED
        build.save(update_fields=["status", "runtime_state", "updated_at"])

        message = f"Recovered stale build state after service restart during {build.get_current_step_display()}"
        BuildLogEntry.objects.create(build=build, stage="error", message=message)
        publish_event(
            "builds",
            "failed",
            {
                "build_id": build.id,
                "status": build.status,
                "current_step": build.current_step,
                "error": message,
            },
        )
        recovered += 1

    return recovered


def _append_build_log(*, build: BuildDefinition, stage: str, message: str) -> None:
    BuildLogEntry.objects.create(build=build, stage=stage, message=message)
    publish_event("builds", "log", {"build_id": build.id, "stage": stage, "message": message})


def _resolve_effective_boot_mode(*, build: BuildDefinition) -> str:
    return build.machine_config.boot_mode


def _validate_boot_layout_compatibility(*, build: BuildDefinition, effective_boot_mode: str) -> None:
    table_type = build.partition_layout.table_type
    entries = list(build.partition_layout.entries.all())

    if effective_boot_mode == BuildMachineConfig.BOOT_UEFI:
        if table_type != "gpt":
            raise ProvisioningError("UEFI machine config requires a GPT partition table")
        has_esp = any(
            (entry.mount_point or "").strip() == "/boot/efi" or entry.filesystem == "efi"
            for entry in entries
        )
        if not has_esp:
            raise ProvisioningError("UEFI machine config requires an EFI system partition mounted at /boot/efi")
        return

    if effective_boot_mode == BuildMachineConfig.BOOT_BIOS and table_type != "mbr":
        raise ProvisioningError("BIOS machine config requires an MBR partition table")


def _generate_mac_address() -> str:
    raw = secrets.token_bytes(3)
    return "52:54:00:%02x:%02x:%02x" % tuple(raw)


def _artifact_root() -> Path:
    return Path(settings.ARTIFACT_ROOT)


def _vm_name(build: BuildDefinition) -> str:
    return f"build-{build.id}"


def _disk_path(build: BuildDefinition) -> Path:
    return _artifact_root() / "disks" / f"{_vm_name(build)}.qcow2"


def _unique_disk_path(build: BuildDefinition) -> Path:
    disks_dir = _artifact_root() / "disks"
    vm_name = _vm_name(build)
    for _ in range(8):
        candidate = disks_dir / f"{vm_name}-{secrets.token_hex(4)}.qcow2"
        if not candidate.exists():
            return candidate
    return disks_dir / f"{vm_name}-{secrets.token_hex(8)}.qcow2"


def _runtime_state(build: BuildDefinition) -> dict:
    return dict(build.runtime_state or {})


def _save_runtime_state(build: BuildDefinition, **updates) -> None:
    state = _runtime_state(build)
    state.update(updates)
    build.runtime_state = state
    build.save(update_fields=["runtime_state", "updated_at"])


def _set_active_task_id(build: BuildDefinition, task_id: str) -> None:
    if not task_id:
        return
    _save_runtime_state(build, active_task_id=task_id)


def _clear_active_task_id(build: BuildDefinition, task_id: str) -> None:
    if not task_id:
        return
    state = _runtime_state(build)
    if str(state.get("active_task_id") or "") == task_id:
        state.pop("active_task_id", None)
        build.runtime_state = state
        build.save(update_fields=["runtime_state", "updated_at"])


def _begin_step(build: BuildDefinition, *, step: str) -> None:
    build.current_step = step
    build.save(update_fields=["current_step", "updated_at"])
    publish_event(
        "builds",
        "updated",
        {
            "build_id": build.id,
            "status": build.status,
            "current_step": build.current_step,
            "run_mode": build.run_mode,
        },
    )


def _require_runtime_value(build: BuildDefinition, key: str) -> str:
    value = str(_runtime_state(build).get(key, "") or "").strip()
    if not value:
        raise ProvisioningError(f"Build runtime state is missing '{key}'. Run the previous step first.")
    return value


def _set_build_running(build: BuildDefinition, *, run_mode: str, reset: bool) -> None:
    with transaction.atomic():
        if reset:
            BuildLogEntry.objects.filter(build=build).delete()
            build.runtime_state = {"last_completed_step": BuildDefinition.STEP_PENDING}
            build.current_step = BuildDefinition.STEP_PENDING
        build.status = BuildDefinition.STATUS_RUNNING
        build.run_mode = run_mode
        build.save(update_fields=["status", "run_mode", "current_step", "runtime_state", "updated_at"])
    publish_event("builds", "running", {"build_id": build.id, "status": build.status, "run_mode": run_mode})


def _complete_step(build: BuildDefinition, *, step: str, keep_running: bool) -> None:
    state = _runtime_state(build)
    state["last_completed_step"] = step
    build.runtime_state = state
    build.current_step = step
    if keep_running:
        build.status = BuildDefinition.STATUS_RUNNING
    elif step == BuildDefinition.STEP_SAVE_RELEASE:
        build.status = BuildDefinition.STATUS_SUCCEEDED
    else:
        build.status = BuildDefinition.STATUS_DRAFT
    build.save(update_fields=["current_step", "runtime_state", "status", "updated_at"])
    event_name = "succeeded" if build.status == BuildDefinition.STATUS_SUCCEEDED else "updated"
    publish_event(
        "builds",
        event_name,
        {
            "build_id": build.id,
            "status": build.status,
            "current_step": build.current_step,
            "run_mode": build.run_mode,
        },
    )


def _fail_build(build: BuildDefinition, *, step: str, exc: Exception) -> None:
    _append_build_log(build=build, stage="error", message=str(exc))
    build.status = BuildDefinition.STATUS_FAILED
    build.current_step = step
    build.save(update_fields=["status", "current_step", "updated_at"])
    publish_event(
        "builds",
        "failed",
        {
            "build_id": build.id,
            "status": build.status,
            "current_step": build.current_step,
            "error": str(exc),
        },
    )


def _run_remote_checked(
    *,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    command: str,
    timeout_seconds: int,
    input_text: str | None = None,
) -> str:
    proc = provisioner.run_remote_command(
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=timeout_seconds,
        input_text=input_text,
    )
    if proc.returncode != 0:
        raise ProvisioningError(
            f"Builder remote command failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout or ""


def _append_remote_command_log(*, build: BuildDefinition, label: str, proc: subprocess.CompletedProcess) -> None:
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    _append_build_log(build=build, stage="packages", message=f"{label} finished (rc={proc.returncode})")
    if stdout:
        _append_build_log(build=build, stage="packages", message=f"{label} stdout: {stdout[:2000]}")
    if stderr:
        _append_build_log(build=build, stage="packages", message=f"{label} stderr: {stderr[:2000]}")


def _run_remote_logged(
    *,
    build: BuildDefinition,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    command: str,
    timeout_seconds: int,
    label: str,
    input_text: str | None = None,
) -> str:
    _append_build_log(build=build, stage="packages", message=f"{label}: starting")
    proc = provisioner.run_remote_command(
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=timeout_seconds,
        input_text=input_text,
    )
    _append_remote_command_log(build=build, label=label, proc=proc)
    if proc.returncode != 0:
        raise ProvisioningError(f"{label} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout or ""


def _ensure_builder_pxe_bootloader(
    *,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    timeout_seconds: int,
) -> str:
    network_gateway = str(getattr(settings, "BUILDER_LIBVIRT_NETWORK_GATEWAY", "192.168.200.1"))
    builder_dns_ip = str(getattr(settings, "BUILDER_VM_STATIC_IPV4", "192.168.200.10"))
    command = (
        "tftp_root=; "
        "if [ -f /etc/dnsmasq.d/wsbuildnet.conf ]; then "
        "  tftp_root=$(awk -F= '/^[[:space:]]*tftp-root=/{print $2; exit}' /etc/dnsmasq.d/wsbuildnet.conf | tr -d '[:space:]'); "
        "fi; "
        "if [ -z \"$tftp_root\" ]; then tftp_root=/var/lib/tftpboot; fi; "
        "if [ ! -d \"$tftp_root\" ] && [ -d /tftpboot ]; then tftp_root=/tftpboot; fi; "
        "dnf -y install dnsmasq >/dev/null 2>&1 || true; "
        "if ! command -v dnsmasq >/dev/null 2>&1; then "
        "  local_iso=$(find /var/www/html/isos -maxdepth 1 -mindepth 1 -type d -name 'build-*' 2>/dev/null | head -n 1); "
        "  if [ -n \"$local_iso\" ]; then "
        "    dnf --disablerepo='*' --repofrompath=baseos,file://$local_iso/BaseOS --repofrompath=appstream,file://$local_iso/AppStream --setopt=baseos.gpgcheck=0 --setopt=appstream.gpgcheck=0 -y install dnsmasq syslinux-tftpboot syslinux grub2-efi-x64 shim-x64 >/dev/null 2>&1 || true; "
        "  fi; "
        "fi; "
        "if ! command -v dnsmasq >/dev/null 2>&1; then echo 'dnsmasq is not installed on builder VM and no enabled repositories are available; provision builder packages or enable repos.' >&2; exit 1; fi; "
        "mkdir -p \"$tftp_root\"/pxelinux.cfg \"$tftp_root\"/builds \"$tftp_root\"/build-state /var/www/html/kickstarts; "
        "if [ ! -f \"$tftp_root/pxelinux.0\" ]; then "
        "  dnf -y install syslinux-tftpboot syslinux grub2-efi-x64 shim-x64 >/dev/null 2>&1 || true; "
        "  pxe_src=; if [ -f /tftpboot/pxelinux.0 ]; then pxe_src=/tftpboot/pxelinux.0; fi; "
        "  if [ -z \"$pxe_src\" ]; then pxe_src=$(find /usr/share /usr/lib /tftpboot -type f -name pxelinux.0 2>/dev/null | head -n 1); fi; "
        "  if [ -n \"$pxe_src\" ]; then cp -f \"$pxe_src\" \"$tftp_root/pxelinux.0\"; fi; "
        "fi; "
        "for mod in ldlinux.c32 libcom32.c32 libutil.c32; do "
        "  if [ ! -f \"$tftp_root/$mod\" ]; then "
        "    mod_src=; if [ -f /tftpboot/$mod ]; then mod_src=/tftpboot/$mod; fi; "
        "    if [ -z \"$mod_src\" ]; then mod_src=$(find /usr/share /usr/lib /tftpboot -type f -name \"$mod\" 2>/dev/null | head -n 1); fi; "
        "    if [ -n \"$mod_src\" ]; then cp -f \"$mod_src\" \"$tftp_root/$mod\"; fi; "
        "  fi; "
        "done; "
        "mkdir -p \"$tftp_root\"/efi64 \"$tftp_root\"/efi32; "
        "efi64_src=; "
        "for candidate in /tftpboot/efi64/grubnetx64.efi /usr/lib/grub/x86_64-efi/grubnetx64.efi /usr/share/grub/x86_64-efi/grubnetx64.efi /boot/efi/EFI/redhat/grubx64.efi /usr/lib/grub/x86_64-efi/grubx64.efi /usr/share/grub/x86_64-efi/grubx64.efi /tftpboot/efi64/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI; do "
        "  if [ -f \"$candidate\" ]; then efi64_src=$candidate; break; fi; "
        "done; "
        "if [ -n \"$efi64_src\" ]; then cp -f \"$efi64_src\" \"$tftp_root/efi64/grubx64.efi\"; fi; "
        "if [ -f /tftpboot/efi32/grubia32.efi ]; then cp -f /tftpboot/efi32/grubia32.efi \"$tftp_root/efi32/grubia32.efi\"; fi; "
        "if [ ! -f \"$tftp_root/efi64/grubx64.efi\" ]; then "
        "  efi64_src=$(find /usr/lib /usr/share /boot /tftpboot -maxdepth 8 -type f \\( -name grubnetx64.efi -o -name grubx64.efi -o -name BOOTX64.EFI -o -name bootx64.efi \\) 2>/dev/null | grep -E '/(grubnetx64\\.efi|grubx64\\.efi|BOOTX64\\.EFI|bootx64\\.efi)$' | head -n 1); "
        "  if [ -n \"$efi64_src\" ]; then cp -f \"$efi64_src\" \"$tftp_root/efi64/grubx64.efi\"; fi; "
        "fi; "
        "if [ ! -f \"$tftp_root/efi32/grubia32.efi\" ]; then "
        "  efi32_src=$(find /usr/lib /usr/share /boot /tftpboot -maxdepth 8 -type f \\( -name grubia32.efi -o -name BOOTIA32.EFI -o -name bootia32.efi \\) 2>/dev/null | head -n 1); "
        "  if [ -n \"$efi32_src\" ]; then cp -f \"$efi32_src\" \"$tftp_root/efi32/grubia32.efi\"; fi; "
        "fi; "
        "if [ ! -f \"$tftp_root/grubx64.efi\" ] && [ -f \"$tftp_root/efi64/grubx64.efi\" ]; then ln -sfn efi64/grubx64.efi \"$tftp_root/grubx64.efi\"; fi; "
        "if [ ! -f \"$tftp_root/grubia32.efi\" ] && [ -f \"$tftp_root/efi32/grubia32.efi\" ]; then ln -sfn efi32/grubia32.efi \"$tftp_root/grubia32.efi\"; fi; "
        "if [ -f \"$tftp_root/efi64/grubx64.efi\" ]; then chmod 0644 \"$tftp_root/efi64/grubx64.efi\"; fi; "
        "if [ -f \"$tftp_root/efi32/grubia32.efi\" ]; then chmod 0644 \"$tftp_root/efi32/grubia32.efi\"; fi; "
        "if [ -f \"$tftp_root/grubx64.efi\" ]; then chmod 0644 \"$tftp_root/grubx64.efi\" || true; fi; "
        "if [ -f \"$tftp_root/grubia32.efi\" ]; then chmod 0644 \"$tftp_root/grubia32.efi\" || true; fi; "
        "restorecon -RF \"$tftp_root\" >/dev/null 2>&1 || true; "
        "if [ -f /etc/dnsmasq.d/wsbuildnet.conf ] && grep -q 'dhcp-boot=tag:efi64' /etc/dnsmasq.d/wsbuildnet.conf && [ ! -f \"$tftp_root/efi64/grubx64.efi\" ]; then "
        "  echo 'UEFI boot is enabled but grubx64.efi is missing from TFTP root' >&2; "
        "  exit 1; "
        "fi; "
        "if [ ! -f /etc/dnsmasq.d/wsbuildnet.conf ]; then "
        f"  iface=$(ip -o -4 addr show | awk '$4 ~ /^" + network_gateway.rsplit('.',1)[0].replace('.', '\\.') + "\\./ {{print $2; exit}}'); "
        f"  if [ -z \"$iface\" ]; then iface=$(ip route get {network_gateway} | awk 'BEGIN {{for (i=1; i<=NF; i++) if ($i == \"dev\") {{print $(i+1); exit}}}}'); fi; "
        "  if [ -z \"$iface\" ]; then iface=$(ip -o -4 route show to default | awk '{print $5; exit}'); fi; "
        "  mkdir -p /etc/dnsmasq.d; "
        "  cat > /etc/dnsmasq.d/wsbuildnet.conf <<'EOF'\n"
        "bind-interfaces\n"
        "domain=wsbuildnet\n"
        "expand-hosts\n"
        "local=/wsbuildnet/\n"
        "enable-tftp\n"
        "tftp-root=/var/lib/tftpboot\n"
        "dhcp-match=set:efi64,option:client-arch,7\n"
        "dhcp-match=set:efi32,option:client-arch,6\n"
        "dhcp-boot=tag:efi64,efi64/grubx64.efi\n"
        "dhcp-boot=tag:efi32,efi32/grubia32.efi\n"
        "dhcp-boot=pxelinux.0\n"
        "dhcp-authoritative\n"
        "dhcp-range=192.168.200.100,192.168.200.254,255.255.255.0,12h\n"
        f"dhcp-option=option:router,{network_gateway}\n"
        f"dhcp-option=option:dns-server,{builder_dns_ip}\n"
        "log-dhcp\n"
        "EOF\n"
        "  if [ -n \"$iface\" ]; then echo \"interface=$iface\" | cat - /etc/dnsmasq.d/wsbuildnet.conf > /etc/dnsmasq.d/wsbuildnet.conf.tmp && mv /etc/dnsmasq.d/wsbuildnet.conf.tmp /etc/dnsmasq.d/wsbuildnet.conf; fi; "
        "fi; "
        "if [ -f /etc/dnsmasq.d/wsbuildnet.conf ]; then "
        "  sed -i 's#dhcp-boot=tag:efi64,grubx64.efi#dhcp-boot=tag:efi64,efi64/grubx64.efi#g' /etc/dnsmasq.d/wsbuildnet.conf; "
        "  sed -i 's#dhcp-boot=tag:efi32,grubia32.efi#dhcp-boot=tag:efi32,efi32/grubia32.efi#g' /etc/dnsmasq.d/wsbuildnet.conf; "
        "  sed -i '/^log-tftp$/d' /etc/dnsmasq.d/wsbuildnet.conf; "
        "  grep -q '^log-dhcp$' /etc/dnsmasq.d/wsbuildnet.conf || echo 'log-dhcp' >> /etc/dnsmasq.d/wsbuildnet.conf; "
        "  systemctl enable --now dnsmasq >/dev/null 2>&1 || true; "
        "  systemctl restart dnsmasq >/dev/null 2>&1 || true; "
        "fi; "
        "test -f \"$tftp_root/pxelinux.0\"; "
        "printf '%s' \"$tftp_root\""
    )
    output = _run_remote_checked(
        provisioner=provisioner,
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=timeout_seconds,
    )
    return output.strip() or "/var/lib/tftpboot"


def _cleanup_builder_bootstrap_assets(
    *,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    tftp_root: str,
    build_id: int,
    timeout_seconds: int,
) -> None:
    state_file = f"{tftp_root}/build-state/build-{build_id}.mac"
    build_root = f"{tftp_root}/builds/build-{build_id}"
    cleanup_command = (
        "mkdir -p /var/www/html/isos /var/www/html/kickstarts; "
        f"old_mac=; if [ -f {state_file} ]; then old_mac=$(cat {state_file} | tr -d '[:space:]'); fi; "
        f"rm -rf {build_root}; "
        "if [ -n \"$old_mac\" ]; then "
        "old_slug=$(printf '%s' \"$old_mac\" | tr '[:upper:]' '[:lower:]' | tr ':' '-'); "
        f"rm -f {tftp_root}/pxelinux.cfg/01-$old_slug {tftp_root}/grub.cfg-01-$old_slug {tftp_root}/grub.cfg-$old_slug {tftp_root}/efi64/grub.cfg-01-$old_slug {tftp_root}/efi64/grub.cfg-$old_slug {tftp_root}/efi64/grub.cfg {tftp_root}/efi32/grub.cfg-01-$old_slug {tftp_root}/efi32/grub.cfg-$old_slug {tftp_root}/efi32/grub.cfg; "
        "fi; "
        f"mkdir -p {tftp_root}/builds {tftp_root}/build-state {tftp_root}/pxelinux.cfg {tftp_root}/efi64 {tftp_root}/efi32 /var/www/html/kickstarts"
    )
    _run_remote_checked(
        provisioner=provisioner,
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=cleanup_command,
        timeout_seconds=timeout_seconds,
    )


def _publish_builder_iso_http_source(
    *,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    timeout_seconds: int,
    build_id: int,
    iso_guest_path: str,
) -> str:
    iso_url = f"http://{host}/isos/build-{build_id}"
    mount_path = f"/var/www/html/isos/build-{build_id}"
    quoted_iso_guest_path = shlex.quote(iso_guest_path)
    command = (
        "mkdir -p /var/www/html/isos; "
        f"iso_src={quoted_iso_guest_path}; "
        "if [ ! -f \"$iso_src\" ]; then "
        "  mkdir -p /mnt/buildisos; "
        "  if ! mountpoint -q /mnt/buildisos; then "
        "    mount -t virtiofs buildisos /mnt/buildisos >/dev/null 2>&1 || mount -t fuse.buildisos buildisos /mnt/buildisos >/dev/null 2>&1 || true; "
        "  fi; "
        "fi; "
        "if [ ! -f \"$iso_src\" ]; then "
        "  alt_src=\"/buildisos/${iso_src#/mnt/buildisos/}\"; "
        "  if [ -f \"$alt_src\" ]; then iso_src=\"$alt_src\"; fi; "
        "fi; "
        "if [ ! -f \"$iso_src\" ]; then "
        "  iso_name=$(basename \"$iso_src\"); "
        "  probe=$(find /mnt/buildisos /buildisos /mnt -maxdepth 6 -type f -name \"$iso_name\" 2>/dev/null | head -n 1 || true); "
        "  if [ -n \"$probe\" ]; then iso_src=\"$probe\"; fi; "
        "fi; "
        "if [ ! -f \"$iso_src\" ]; then "
        "  echo \"ISO source is not visible in builder guest: $iso_src\" >&2; "
        "  exit 1; "
        "fi; "
        f"mkdir -p {mount_path}; "
        f"if mountpoint -q {mount_path}; then umount {mount_path} || umount -l {mount_path} || true; fi; "
        f"if ! mount -o loop,ro,context=system_u:object_r:httpd_sys_content_t:s0 \"$iso_src\" {mount_path}; then "
        f"  mount -o loop,ro \"$iso_src\" {mount_path}; "
        "fi; "
        f"if [ ! -f {mount_path}/.treeinfo ]; then "
        f"  if [ ! -f {mount_path}/images/install.img ] && [ ! -f {mount_path}/LiveOS/squashfs.img ]; then "
        "    echo 'Mounted ISO is missing expected installer metadata/content' >&2; "
        "    exit 1; "
        "  fi; "
        "fi; "
        "restorecon -RF /var/www/html/isos /var/www/html/kickstarts >/dev/null 2>&1 || true; "
        "dnf -y install httpd >/dev/null 2>&1 || true; "
        "if ! command -v httpd >/dev/null 2>&1; then "
        f"  dnf --disablerepo='*' --repofrompath=baseos,file://{mount_path}/BaseOS --repofrompath=appstream,file://{mount_path}/AppStream --setopt=baseos.gpgcheck=0 --setopt=appstream.gpgcheck=0 -y install httpd >/dev/null 2>&1 || true; "
        "fi; "
        "if ! command -v httpd >/dev/null 2>&1; then "
        "  echo 'httpd is not installed on builder VM; cannot publish HTTP install source' >&2; "
        "  exit 1; "
        "fi; "
        "systemctl enable --now httpd >/dev/null 2>&1 || true; "
        "systemctl restart httpd >/dev/null 2>&1 || true; "
        "if command -v firewall-cmd >/dev/null 2>&1; then firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true; firewall-cmd --reload >/dev/null 2>&1 || true; fi; "
        "if ! systemctl is-active --quiet httpd; then echo 'httpd service is not active after setup' >&2; exit 1; fi"
    )
    _run_remote_checked(
        provisioner=provisioner,
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=timeout_seconds,
    )
    return iso_url


def _build_timeout_seconds(build: BuildDefinition) -> int:
    return build.machine_config.kickstart_timeout_minutes * 60


def _builder_ready_timeout_seconds(build: BuildDefinition) -> int:
    configured = int(getattr(settings, "BUILDER_READY_TIMEOUT_SECONDS", 180))
    return max(30, min(_build_timeout_seconds(build), configured))


def _builder_session(build: BuildDefinition, *, ensure_iso_shared: bool) -> tuple[BuilderVMManager, AnsibleProvisioner, str, str, Path]:
    builder_manager = BuilderVMManager()
    builder_timeout = _builder_ready_timeout_seconds(build)
    try:
        if ensure_iso_shared:
            builder_manager.ensure_iso_shared(Path(build.iso_image.iso_file.path))
        _append_build_log(build=build, stage="builder", message="Ensuring builder VM exists")
        builder_manager.ensure_builder_vm()
        _append_build_log(build=build, stage="builder", message="Checking whether builder VM is running")
        if not builder_manager.builder_vm_running():
            _append_build_log(build=build, stage="builder", message="Builder VM is not running; starting it")
            builder_manager.start_builder_vm()
        else:
            _append_build_log(build=build, stage="builder", message="Builder VM is already running")
        _append_build_log(build=build, stage="builder", message="Waiting for builder DHCP/IP address")
        builder_ip = builder_manager.wait_for_ipv4(timeout_seconds=builder_timeout)
        _append_build_log(build=build, stage="builder", message=f"Builder VM IP detected: {builder_ip}")
        builder_access = builder_manager.ensure_access_keypair()
        builder_ssh_user = getattr(settings, "BUILDER_VM_SSH_USER", "root")
        provisioner = AnsibleProvisioner(project_root=Path(__file__).resolve().parents[2])
        _append_build_log(build=build, stage="ssh", message=f"Waiting for builder SSH on {builder_ssh_user}@{builder_ip}")
        provisioner.wait_for_ssh(
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_access.private_key_path),
            timeout_seconds=builder_timeout,
        )
        _append_build_log(build=build, stage="ssh", message="Applying builder SSH performance profile (disable reverse DNS/GSSAPI)")
        _run_remote_checked(
            provisioner=provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_access.private_key_path),
            command=(
                "mkdir -p /etc/ssh/sshd_config.d; "
                "cat > /etc/ssh/sshd_config.d/99-tuxwsmaker-fast-ssh.conf <<'EOF'\n"
                "UseDNS no\n"
                "GSSAPIAuthentication no\n"
                "EOF\n"
                "if command -v sshd >/dev/null 2>&1; then sshd -t; fi; "
                "systemctl reload sshd >/dev/null 2>&1 || systemctl reload ssh >/dev/null 2>&1 || true"
            ),
            timeout_seconds=120,
        )
        return builder_manager, provisioner, builder_ip, builder_ssh_user, builder_access.private_key_path
    except BuilderError as exc:
        raise ProvisioningError(f"Builder VM setup failed: {exc}") from exc


def _materialize_build_keypair(build: BuildDefinition):
    key_dir = _artifact_root() / "keys"
    return generate_build_ssh_keypair(build.id, key_dir)


def _resolve_ip_for_existing_vm(build: BuildDefinition) -> str:
    state = _runtime_state(build)
    vm_name = str(state.get("vm_name") or _vm_name(build))
    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    ip_address = str(state.get("build_ip_address") or "").strip()
    if ip_address:
        return ip_address
    ip_address = vm_manager.current_ipv4(domain_name=vm_name, network_name=BuildMachineConfig.FIXED_LIBVIRT_NETWORK) or ""
    if ip_address:
        _save_runtime_state(build, build_ip_address=ip_address)
        return ip_address
    ip_address = vm_manager.wait_for_ipv4(
        domain_name=vm_name,
        network_name=BuildMachineConfig.FIXED_LIBVIRT_NETWORK,
        timeout_seconds=120,
        mac_address=str(state.get("vm_mac_address") or ""),
    ) or ""
    if not ip_address:
        raise ProvisioningError(f"Could not determine IP address for running VM {vm_name}")
    _save_runtime_state(build, build_ip_address=ip_address)
    return ip_address


def _run_selected_playbooks(
    *,
    build: BuildDefinition,
    provisioner: AnsibleProvisioner,
    ip_address: str,
    ssh_user: str,
    private_key_path: str,
) -> None:
    ordered = list(build.ordered_playbook_selections())
    if ordered:
        _append_build_log(build=build, stage="playbooks", message=f"Running {len(ordered)} assigned playbook(s)")
        for selection in ordered:
            repo = selection.playbook.repository
            branch = selection.playbook.branch
            repo_checkout = checkout_repository(repo, branch)
            _append_build_log(
                build=build,
                stage="playbooks",
                message=f"Running playbook {selection.playbook.repository.name} [{branch}] {selection.playbook.path}",
            )
            provisioner.configure_guest(
                host=ip_address,
                playbook_path=selection.playbook.path,
                user=ssh_user,
                private_key_path=private_key_path,
                working_dir=repo_checkout,
            )
        return

    if build.playbook_path:
        repo_url = (build.playbook_repo or "").strip()
        branch = (build.playbook_branch or "main").strip() or "main"
        if repo_url:
            repo_checkout = checkout_repository_url(repo_url, branch)
            _append_build_log(
                build=build,
                stage="playbooks",
                message=f"Running fallback playbook {build.playbook_path} from {repo_url} [{branch}]",
            )
            provisioner.configure_guest(
                host=ip_address,
                playbook_path=build.playbook_path,
                user=ssh_user,
                private_key_path=private_key_path,
                working_dir=repo_checkout,
            )
            return

        _append_build_log(build=build, stage="playbooks", message=f"Running fallback playbook {build.playbook_path}")
        provisioner.configure_guest(
            host=ip_address,
            playbook_path=build.playbook_path,
            user=ssh_user,
            private_key_path=private_key_path,
        )


def _collect_selected_packages_for_build(
    build: BuildDefinition,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (groups, installs, removes, skipped_lists).

    Package name prefixes:
      @name  → group install  (dnf group install / tasksel on Debian)
    @^name → environment group install on RHEL-family guests
      -name  → remove after install
      name   → regular install
    """
    os_family = str(build.operating_system.family or "").strip().lower()
    allowed_families = {PackageList.DISTRO_ALL}
    if os_family == "rhel":
        allowed_families.add(PackageList.DISTRO_RHEL)
    elif os_family == "debian":
        allowed_families.add(PackageList.DISTRO_DEBIAN)

    groups: list[str] = []
    installs: list[str] = []
    removes: list[str] = []
    seen: set[str] = set()
    skipped_lists: list[str] = []
    selected_lists = build.package_lists.prefetch_related("items").order_by("name")
    for package_list in selected_lists:
        if package_list.distro_family not in allowed_families:
            skipped_lists.append(package_list.name)
            continue
        for item in package_list.items.all():
            raw_pkg = str(item.package_name or "").strip().strip('"').strip("'")
            if len(raw_pkg) >= 2 and raw_pkg[0] in ('"', "'") and raw_pkg[-1] == raw_pkg[0]:
                raw_pkg = raw_pkg[1:-1].strip()
            if not raw_pkg or raw_pkg in seen:
                continue
            seen.add(raw_pkg)
            if raw_pkg.startswith("@^"):
                groups.append(raw_pkg[2:])
            elif raw_pkg.startswith("@"):
                groups.append(raw_pkg[1:])
            elif raw_pkg.startswith("-"):
                removes.append(raw_pkg[1:])
            else:
                installs.append(raw_pkg)
    return groups, installs, removes, skipped_lists


def _collect_selected_package_group_log_entries(build: BuildDefinition) -> list[str]:
    os_family = str(build.operating_system.family or "").strip().lower()
    allowed_families = {PackageList.DISTRO_ALL}
    if os_family == "rhel":
        allowed_families.add(PackageList.DISTRO_RHEL)
    elif os_family == "debian":
        allowed_families.add(PackageList.DISTRO_DEBIAN)

    entries: list[str] = []
    seen: set[str] = set()
    selected_lists = build.package_lists.prefetch_related("items").order_by("name")
    for package_list in selected_lists:
        if package_list.distro_family not in allowed_families:
            continue
        for item in package_list.items.all():
            raw_pkg = str(item.package_name or "").strip().strip('"').strip("'")
            if len(raw_pkg) >= 2 and raw_pkg[0] in ('"', "'") and raw_pkg[-1] == raw_pkg[0]:
                raw_pkg = raw_pkg[1:-1].strip()
            if not raw_pkg or raw_pkg in seen:
                continue
            if raw_pkg.startswith("@^"):
                entries.append(f"{raw_pkg} -> {raw_pkg[2:]}")
                seen.add(raw_pkg)
            elif raw_pkg.startswith("@"):
                entries.append(f"{raw_pkg} -> {raw_pkg[1:]}")
                seen.add(raw_pkg)
    return entries


def _rhsm_selected_repo_ids(build: BuildDefinition, *, phase: str = "during_build") -> list[str]:
    selections = list(build.ordered_rhsm_repository_selections())
    if phase == "before_afterburner":
        filtered = [sel for sel in selections if sel.enable_before_afterburner]
    else:
        filtered = [sel for sel in selections if sel.enable_during_build]

    repo_ids = [
        str(sel.repository.repo_id or "").strip()
        for sel in filtered
        if str(sel.repository.repo_id or "").strip()
    ]
    if repo_ids:
        return repo_ids

    # Backward-compatible fallback for legacy builds that still use direct M2M only.
    return [
        str(repo.repo_id or "").strip()
        for repo in build.rhsm_repositories.all().order_by("rhel_major", "repo_id")
        if str(repo.repo_id or "").strip()
    ]


def _build_phase_selected_rpm_repo_ids(build: BuildDefinition) -> list[str]:
    repo_ids: list[str] = []
    seen: set[str] = set()
    for sel in build.ordered_repository_selections():
        if not sel.enable_during_build:
            continue
        repo = sel.repository
        if not repo.enabled or repo.family != "rpm":
            continue
        repo_id = str(repo.effective_rpm_repoid() or "").strip()
        if not repo_id or repo_id in seen:
            continue
        seen.add(repo_id)
        repo_ids.append(repo_id)
    return repo_ids


def _validate_and_scope_rpm_repositories(
    *,
    provisioner: AnsibleProvisioner,
    ip_address: str,
    ssh_user: str,
    private_key_path: str,
    allowed_repo_ids: list[str],
) -> None:
    if not allowed_repo_ids:
        return

    quoted_allowed = " ".join(shlex.quote(repo_id) for repo_id in allowed_repo_ids)
    command = (
        "PKG_MGR=''; "
        "if command -v dnf >/dev/null 2>&1; then PKG_MGR='dnf'; "
        "elif command -v yum >/dev/null 2>&1; then PKG_MGR='yum'; fi; "
        "if [[ -z \"$PKG_MGR\" ]]; then echo 'dnf/yum is not available on guest' >&2; exit 1; fi; "
        "declare -a ALLOWED_REPOS=(" + quoted_allowed + "); "
        "declare -A ALLOWED_SET=(); "
        "for repo_id in \"${ALLOWED_REPOS[@]}\"; do ALLOWED_SET[$repo_id]=1; done; "
        "for repo_id in \"${ALLOWED_REPOS[@]}\"; do "
        "  if ! $PKG_MGR -q repolist --all \"$repo_id\" >/dev/null 2>&1; then "
        "    echo \"Required repository '$repo_id' is not configured on guest\" >&2; "
        "    exit 1; "
        "  fi; "
        "done; "
        "enabled_ids=$($PKG_MGR -q repolist --enabled 2>/dev/null | awk 'BEGIN {body=0} $1==\"repo\" && $2==\"id\" {body=1; next} body && NF>=1 {print $1}'); "
        "unexpected=''; "
        "if [[ -n \"${enabled_ids:-}\" ]]; then "
        "  while IFS= read -r repo_id; do "
        "    [[ -n \"$repo_id\" ]] || continue; "
        "    if [[ -z \"${ALLOWED_SET[$repo_id]:-}\" ]]; then "
        "      unexpected=\"${unexpected}${unexpected:+, }$repo_id\"; "
        "    fi; "
        "  done <<< \"$enabled_ids\"; "
        "fi; "
        "if [[ -n \"$unexpected\" ]]; then "
        "  echo \"Unexpected enabled repositories detected: $unexpected\" >&2; "
        "  exit 1; "
        "fi"
    )
    _run_remote_checked(
        provisioner=provisioner,
        host=ip_address,
        user=ssh_user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=300,
    )


def _step_install_packages(build: BuildDefinition) -> dict[str, str]:
    vm_name = str(_runtime_state(build).get("vm_name") or _vm_name(build))
    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    if not vm_manager.domain_exists(vm_name) or not vm_manager.domain_is_active(vm_name):
        raise ProvisioningError(f"Build VM {vm_name} is not running")

    ip_address = _resolve_ip_for_existing_vm(build)
    key_pair = _materialize_build_keypair(build)
    try:
        ssh_user = str(_runtime_state(build).get("build_ssh_user") or getattr(settings, "BUILD_VM_SSH_USER", "root"))
        provisioner = AnsibleProvisioner(project_root=Path(__file__).resolve().parents[2])
        _append_build_log(build=build, stage="ssh", message=f"Waiting for SSH access on {ssh_user}@{ip_address}")
        provisioner.wait_for_ssh(
            host=ip_address,
            user=ssh_user,
            private_key_path=str(key_pair.private_key_path),
            timeout_seconds=180,
        )
        _append_build_log(build=build, stage="ssh", message="Build VM SSH login is ready")

        os_family = str(build.operating_system.family or "").strip().lower()
        rhsm_repo_ids = _rhsm_selected_repo_ids(build, phase="during_build")
        custom_repo_ids = _build_phase_selected_rpm_repo_ids(build) if os_family == "rhel" else []
        allowed_repo_ids = list(dict.fromkeys(custom_repo_ids + rhsm_repo_ids)) if os_family == "rhel" else []
        rhsm_registered = False
        try:
            if rhsm_repo_ids:
                if os_family != "rhel":
                    raise ProvisioningError("RHSM repositories can only be used on RHEL builds")
                _append_build_log(build=build, stage="rhsm", message="Preparing RHSM registration for package installation")
                registration_mode = str(build.rhsm_auth_mode or BuildDefinition.RHSM_AUTH_NONE).strip()
                if registration_mode == BuildDefinition.RHSM_AUTH_CONFIG:
                    cfg = ServerConfiguration.get_solo()
                    username = str(cfg.rhn_username or "").strip()
                    password = str(cfg.get_rhn_password() or "").strip()
                    if not username or not password:
                        raise ProvisioningError("Server configuration RHSM credentials are required for configuration-credentials mode")
                    register_command = (
                        "if ! command -v subscription-manager >/dev/null 2>&1; then "
                        "echo 'subscription-manager is not available on guest' >&2; exit 1; fi; "
                        f"subscription-manager register --force --username {shlex.quote(username)} --password {shlex.quote(password)}"
                    )
                elif registration_mode == BuildDefinition.RHSM_AUTH_USERPASS:
                    username = str(build.rhsm_username or "").strip()
                    password = str(build.get_rhsm_password() or "").strip()
                    if not username or not password:
                        raise ProvisioningError("RHSM username/password mode requires both username and password")
                    register_command = (
                        "if ! command -v subscription-manager >/dev/null 2>&1; then "
                        "echo 'subscription-manager is not available on guest' >&2; exit 1; fi; "
                        f"subscription-manager register --force --username {shlex.quote(username)} --password {shlex.quote(password)}"
                    )
                elif registration_mode == BuildDefinition.RHSM_AUTH_ACTIVATION_KEY:
                    org_id = str(build.rhsm_org_id or "").strip()
                    activation_key = str(build.get_rhsm_activation_key() or "").strip()
                    if not org_id or not activation_key:
                        raise ProvisioningError("RHSM activation-key mode requires both org ID and activation key")
                    register_command = (
                        "if ! command -v subscription-manager >/dev/null 2>&1; then "
                        "echo 'subscription-manager is not available on guest' >&2; exit 1; fi; "
                        f"subscription-manager register --force --org {shlex.quote(org_id)} --activationkey {shlex.quote(activation_key)}"
                    )
                else:
                    raise ProvisioningError("RHSM authentication mode must be configured when RHSM repositories are selected")

                _run_remote_checked(
                    provisioner=provisioner,
                    host=ip_address,
                    user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                    command=register_command,
                    timeout_seconds=300,
                )
                rhsm_registered = True
                _append_build_log(build=build, stage="rhsm", message="RHSM registration completed")

                enable_command = "subscription-manager repos " + " ".join(
                    f"--enable={shlex.quote(repo_id)}" for repo_id in rhsm_repo_ids
                )
                _run_remote_checked(
                    provisioner=provisioner,
                    host=ip_address,
                    user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                    command=enable_command,
                    timeout_seconds=300,
                )
                _append_build_log(
                    build=build,
                    stage="rhsm",
                    message=f"Enabled RHSM repositories: {', '.join(rhsm_repo_ids)}",
                )

            groups, installs, removes, skipped_lists = _collect_selected_packages_for_build(build)
            if skipped_lists:
                _append_build_log(
                    build=build,
                    stage="packages",
                    message=f"Skipping incompatible package lists for {build.operating_system.family}: {', '.join(skipped_lists)}",
                )
            if not groups and not installs and not removes:
                _append_build_log(build=build, stage="packages", message="No package list entries selected for installation")
                _save_runtime_state(build, build_ip_address=ip_address)
                return {"ip": ip_address, "packages_installed": "0"}

            total_count = len(groups) + len(installs) + len(removes)
            parts = []
            if groups:
                parts.append(f"{len(groups)} group(s)")
            if installs:
                parts.append(f"{len(installs)} package(s)")
            if removes:
                parts.append(f"{len(removes)} removal(s)")
            _append_build_log(build=build, stage="packages", message=f"Processing {', '.join(parts)} from selected package lists")
            group_log_entries = _collect_selected_package_group_log_entries(build)
            if groups:
                if group_log_entries:
                    _append_build_log(
                        build=build,
                        stage="packages",
                        message=f"Package groups selected (raw -> normalized): {', '.join(group_log_entries)}",
                    )
                else:
                    _append_build_log(build=build, stage="packages", message=f"Package groups selected: {', '.join(groups)}")
            if installs:
                _append_build_log(build=build, stage="packages", message=f"Packages selected: {', '.join(installs)}")
            if removes:
                _append_build_log(build=build, stage="packages", message=f"Packages selected for removal: {', '.join(removes)}")

            if os_family == "debian":
                cmd_parts = [
                    "if ! command -v apt-get >/dev/null 2>&1; then "
                    "echo 'apt-get is not available on guest' >&2; exit 1; fi; "
                    "export DEBIAN_FRONTEND=noninteractive; "
                    "apt-get update -y",
                ]
                if groups:
                    for g in groups:
                        cmd_parts.append(f"echo 'Skipping package group {shlex.quote(g)}: groups are not supported on Debian/Ubuntu' >&2 || true")
                if installs:
                    quoted_installs = " ".join(shlex.quote(p) for p in installs)
                    cmd_parts.append(f"apt-get install -y --no-install-recommends {quoted_installs}")
                if removes:
                    quoted_removes = " ".join(shlex.quote(p) for p in removes)
                    cmd_parts.append(f"apt-get remove -y {quoted_removes}")
                install_command = "; ".join(cmd_parts)
            else:
                if os_family == "rhel" and not allowed_repo_ids:
                    raise ProvisioningError(
                        "Install Packages requires at least one enabled repository in build repositories or RHSM repositories"
                    )
                repo_scope = "--disablerepo='*' " + " ".join(
                    f"--enablerepo={shlex.quote(repo_id)}" for repo_id in allowed_repo_ids
                )
                pkg_mgr_prefix = (
                    "PKG_MGR=''; "
                    "if command -v dnf >/dev/null 2>&1; then PKG_MGR='dnf'; "
                    "elif command -v yum >/dev/null 2>&1; then PKG_MGR='yum'; fi; "
                    "if [[ -z \"$PKG_MGR\" ]]; then echo 'dnf/yum is not available on guest' >&2; exit 1; fi"
                )
                cmd_parts = [
                    pkg_mgr_prefix,
                    f"$PKG_MGR -y {repo_scope} --setopt=skip_if_unavailable=True makecache || true",
                ]
                install_command = "; ".join(cmd_parts)

            _append_build_log(build=build, stage="packages", message=f"Running package operations on guest")
            _run_build_phase_repositories(
                build=build,
                provisioner=provisioner,
                ip_address=ip_address,
                ssh_user=ssh_user,
                private_key_path=str(key_pair.private_key_path),
            )
            try:
                if os_family == "rhel":
                    _append_build_log(
                        build=build,
                        stage="repositories",
                        message=f"Validating repository scope for Install Packages: {', '.join(allowed_repo_ids) if allowed_repo_ids else 'none'}",
                    )
                    _validate_and_scope_rpm_repositories(
                        provisioner=provisioner,
                        ip_address=ip_address,
                        ssh_user=ssh_user,
                        private_key_path=str(key_pair.private_key_path),
                        allowed_repo_ids=allowed_repo_ids,
                    )
                if os_family == "rhel":
                    pkg_mgr_prefix = (
                        "PKG_MGR=''; "
                        "if command -v dnf >/dev/null 2>&1; then PKG_MGR='dnf'; "
                        "elif command -v yum >/dev/null 2>&1; then PKG_MGR='yum'; fi; "
                        "if [[ -z \"$PKG_MGR\" ]]; then echo 'dnf/yum is not available on guest' >&2; exit 1; fi"
                    )
                    if groups:
                        quoted_groups = " ".join(shlex.quote(g) for g in groups)
                        group_command = (
                            f"{pkg_mgr_prefix}; "
                            f"$PKG_MGR -y {repo_scope} --setopt=skip_if_unavailable=True makecache || true; "
                            f"for group_name in {quoted_groups}; do "
                            f"  if ! $PKG_MGR -q {repo_scope} --setopt=skip_if_unavailable=True group info \"$group_name\" >/dev/null 2>&1; then "
                            f"    echo 'Package group is unavailable, aborting: ' \"$group_name\" >&2; "
                            f"    exit 1; "
                            f"  fi; "
                            f"  echo 'Installing package group: ' \"$group_name\"; "
                            f"  $PKG_MGR -y {repo_scope} --setopt=skip_if_unavailable=True group install \"$group_name\"; "
                            f"done"
                        )
                        _run_remote_logged(
                            build=build,
                            provisioner=provisioner,
                            host=ip_address,
                            user=ssh_user,
                            private_key_path=str(key_pair.private_key_path),
                            command=group_command,
                            timeout_seconds=1200,
                            label="group install",
                        )
                    if installs:
                        quoted_installs = " ".join(shlex.quote(p) for p in installs)
                        install_packages_command = (
                            f"{pkg_mgr_prefix}; "
                            f"$PKG_MGR -y {repo_scope} --setopt=skip_if_unavailable=True makecache || true; "
                            f"$PKG_MGR -y {repo_scope} --setopt=skip_if_unavailable=True install {quoted_installs}"
                        )
                        _run_remote_logged(
                            build=build,
                            provisioner=provisioner,
                            host=ip_address,
                            user=ssh_user,
                            private_key_path=str(key_pair.private_key_path),
                            command=install_packages_command,
                            timeout_seconds=1200,
                            label="package install",
                        )
                    if removes:
                        quoted_removes = " ".join(shlex.quote(p) for p in removes)
                        remove_packages_command = (
                            f"{pkg_mgr_prefix}; "
                            f"$PKG_MGR -y {repo_scope} --setopt=skip_if_unavailable=True remove {quoted_removes}"
                        )
                        _run_remote_logged(
                            build=build,
                            provisioner=provisioner,
                            host=ip_address,
                            user=ssh_user,
                            private_key_path=str(key_pair.private_key_path),
                            command=remove_packages_command,
                            timeout_seconds=1200,
                            label="package removal",
                        )
                else:
                    _run_remote_logged(
                        build=build,
                        provisioner=provisioner,
                        host=ip_address,
                        user=ssh_user,
                        private_key_path=str(key_pair.private_key_path),
                        command=install_command,
                        timeout_seconds=1200,
                        label="package install",
                    )
            finally:
                _cleanup_build_phase_repositories(
                    build=build,
                    provisioner=provisioner,
                    ip_address=ip_address,
                    ssh_user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                )
            _append_build_log(build=build, stage="packages", message="Package operations completed")
            _save_runtime_state(build, build_ip_address=ip_address)
            return {"ip": ip_address, "packages_installed": str(total_count)}
        finally:
            if rhsm_repo_ids:
                _run_remote_checked(
                    provisioner=provisioner,
                    host=ip_address,
                    user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                    command="subscription-manager repos " + " ".join(
                        f"--disable={shlex.quote(repo_id)}" for repo_id in rhsm_repo_ids
                    ) + " || true",
                    timeout_seconds=180,
                )
            if rhsm_registered:
                _run_remote_checked(
                    provisioner=provisioner,
                    host=ip_address,
                    user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                    command="subscription-manager unregister >/dev/null 2>&1 || true; subscription-manager clean >/dev/null 2>&1 || true",
                    timeout_seconds=120,
                )
    finally:
        key_pair.cleanup_private()


def _run_build_phase_repositories(
    *,
    build: BuildDefinition,
    provisioner: AnsibleProvisioner,
    ip_address: str,
    ssh_user: str,
    private_key_path: str,
) -> None:
    selections = [sel for sel in build.ordered_repository_selections() if sel.enable_during_build]
    snippet = render_repository_activation_snippet(
        selections=selections,
        os_family=build.operating_system.family,
        root_expression='""',
        phase_label="build",
    )
    if not snippet:
        return
    _append_build_log(build=build, stage="repositories", message="Activating temporary repositories for build phase")
    _run_remote_checked(
        provisioner=provisioner,
        host=ip_address,
        user=ssh_user,
        private_key_path=private_key_path,
        command=snippet,
        timeout_seconds=300,
    )


def _cleanup_build_phase_repositories(
    *,
    build: BuildDefinition,
    provisioner: AnsibleProvisioner,
    ip_address: str,
    ssh_user: str,
    private_key_path: str,
) -> None:
    selections = [sel for sel in build.ordered_repository_selections() if sel.enable_during_build]
    snippet = render_repository_cleanup_snippet(
        selections=selections,
        os_family=build.operating_system.family,
        root_expression='""',
        phase_label="build",
    )
    if not snippet:
        return
    _append_build_log(build=build, stage="repositories", message="Cleaning up temporary repositories after build phase")
    _run_remote_checked(
        provisioner=provisioner,
        host=ip_address,
        user=ssh_user,
        private_key_path=private_key_path,
        command=snippet,
        timeout_seconds=300,
    )


def _step_create_vm_shell(build: BuildDefinition) -> dict[str, str]:
    timeout_seconds = _build_timeout_seconds(build)
    effective_boot_mode = _resolve_effective_boot_mode(build=build)
    _validate_boot_layout_compatibility(build=build, effective_boot_mode=effective_boot_mode)
    _append_build_log(build=build, stage="start", message="Preparing build environment")
    _append_build_log(build=build, stage="vm", message=f"Effective boot mode: {effective_boot_mode}")
    _append_build_log(build=build, stage="builder", message="Ensuring builder VM is available")

    builder_manager, builder_provisioner, builder_ip, builder_ssh_user, builder_key_path = _builder_session(
        build,
        ensure_iso_shared=True,
    )
    _append_build_log(build=build, stage="builder", message=f"Builder VM ready at {builder_ip}")
    _append_build_log(build=build, stage="builder", message="Builder SSH access is ready")

    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    vm_name = _vm_name(build)
    vm_mac_address = _generate_mac_address()
    ssh_user = getattr(settings, "BUILD_VM_SSH_USER", "root")
    artifact_root = _artifact_root()
    kickstart_dir = artifact_root / "kickstarts"
    disk_path = _unique_disk_path(build)
    key_pair = _materialize_build_keypair(build)
    try:
        _append_build_log(build=build, stage="ssh", message="Generated guest SSH keypair")
        kickstart_path = render_kickstart_file(
            output_dir=kickstart_dir,
            vm_name=vm_name,
            ssh_public_key=key_pair.public_key,
            partition_layout=build.partition_layout,
        )
        _append_build_log(build=build, stage="kickstart", message=f"Rendered kickstart file {kickstart_path.name}")
        pxe_assets_dir = prepare_iso_pxe_assets(iso_path=Path(build.iso_image.iso_file.path))
        _append_build_log(build=build, stage="pxe", message=f"Prepared PXE assets from {build.iso_image.iso_file.name}")

        iso_path = Path(build.iso_image.iso_file.path).resolve()
        shared_iso_root = Path(builder_manager.definition.shared_iso_dir).resolve()
        try:
            iso_rel = iso_path.relative_to(shared_iso_root)
        except ValueError as exc:
            raise ProvisioningError(
                f"ISO is outside shared builder path ({shared_iso_root}): {iso_path}"
            ) from exc
        iso_guest_path = f"/mnt/buildisos/{iso_rel.as_posix()}"
        install_source_url = _publish_builder_iso_http_source(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            timeout_seconds=timeout_seconds,
            build_id=build.id,
            iso_guest_path=iso_guest_path,
        )
        _append_build_log(build=build, stage="pxe", message=f"Published ISO source URL {install_source_url}")

        _append_build_log(build=build, stage="pxe", message="Ensuring builder TFTP bootloader files")
        tftp_root = _ensure_builder_pxe_bootloader(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            timeout_seconds=timeout_seconds,
        )
        _append_build_log(build=build, stage="pxe", message=f"Using TFTP root {tftp_root}")

        _cleanup_builder_bootstrap_assets(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            tftp_root=tftp_root,
            build_id=build.id,
            timeout_seconds=timeout_seconds,
        )

        remote_build_root = f"{tftp_root}/builds/{vm_name}"
        remote_kickstart_path = f"/var/www/html/kickstarts/{kickstart_path.name}"
        remote_boot_kernel = f"{remote_build_root}/vmlinuz"
        remote_boot_initrd = f"{remote_build_root}/initrd.img"
        mac_slug = vm_mac_address.lower().replace(':', '-')
        remote_bios_config = f"{tftp_root}/pxelinux.cfg/01-{mac_slug}"
        remote_efi_config = f"{tftp_root}/grub.cfg-01-{mac_slug}"
        remote_efi_config_alt = f"{tftp_root}/grub.cfg-{mac_slug}"
        remote_efi_default = f"{tftp_root}/grub.cfg"
        remote_efi64_config = f"{tftp_root}/efi64/grub.cfg-01-{mac_slug}"
        remote_efi64_config_alt = f"{tftp_root}/efi64/grub.cfg-{mac_slug}"
        remote_efi64_default = f"{tftp_root}/efi64/grub.cfg"
        remote_efi32_config = f"{tftp_root}/efi32/grub.cfg-01-{mac_slug}"
        remote_efi32_config_alt = f"{tftp_root}/efi32/grub.cfg-{mac_slug}"
        remote_efi32_default = f"{tftp_root}/efi32/grub.cfg"

        _run_remote_checked(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            command=f"mkdir -p {remote_build_root} {tftp_root}/pxelinux.cfg {tftp_root}/efi64 {tftp_root}/efi32 /var/www/html/kickstarts",
            timeout_seconds=timeout_seconds,
        )
        builder_provisioner.upload_file(
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            local_path=kickstart_path,
            remote_path=remote_kickstart_path,
            timeout_seconds=timeout_seconds,
        )
        _run_remote_checked(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            command=(
                "mkdir -p /var/www/html/kickstarts /var/www/html/isos; "
                "restorecon -RF /var/www/html/kickstarts /var/www/html/isos >/dev/null 2>&1 || true; "
                f"restorecon -v {remote_kickstart_path} >/dev/null 2>&1 || chcon -t httpd_sys_content_t {remote_kickstart_path} >/dev/null 2>&1 || true"
            ),
            timeout_seconds=timeout_seconds,
        )
        builder_provisioner.upload_file(
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            local_path=pxe_assets_dir / "boot" / "vmlinuz",
            remote_path=remote_boot_kernel,
            timeout_seconds=timeout_seconds,
        )
        builder_provisioner.upload_file(
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            local_path=pxe_assets_dir / "boot" / "initrd.img",
            remote_path=remote_boot_initrd,
            timeout_seconds=timeout_seconds,
        )
        boot_configs = render_pxe_boot_configs(
            vm_name=vm_name,
            kernel_rel_path=f"builds/{vm_name}/vmlinuz",
            initrd_rel_path=f"builds/{vm_name}/initrd.img",
            kickstart_url=f"http://{builder_ip}/kickstarts/{kickstart_path.name}",
            install_source_url=install_source_url,
        )
        boot_config_targets = [
            (remote_bios_config, boot_configs["bios"]),
            (remote_efi_config, boot_configs["efi"]),
            (remote_efi_config_alt, boot_configs["efi"]),
            (remote_efi_default, boot_configs["efi"]),
            (remote_efi64_config, boot_configs["efi"]),
            (remote_efi64_config_alt, boot_configs["efi"]),
            (remote_efi64_default, boot_configs["efi"]),
            (remote_efi32_config, boot_configs["efi"]),
            (remote_efi32_config_alt, boot_configs["efi"]),
            (remote_efi32_default, boot_configs["efi"]),
        ]
        write_commands = ["set -euo pipefail"]
        for remote_path, content in boot_config_targets:
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            write_commands.append(
                f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}"
            )
        _run_remote_checked(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            command="; ".join(write_commands),
            timeout_seconds=timeout_seconds,
        )
        _run_remote_checked(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            command=f"printf '%s\\n' {vm_mac_address} > {tftp_root}/build-state/build-{build.id}.mac",
            timeout_seconds=timeout_seconds,
        )
        _append_build_log(build=build, stage="pxe", message=f"Published MAC-specific PXE config for {vm_mac_address}")

        vm_definition = VMDefinition(
            name=vm_name,
            memory_mib=build.machine_config.memory_mib,
            vcpus=build.machine_config.cpu,
            disk_gib=calculate_layout_disk_size_gib(build.partition_layout),
            network_name=BuildMachineConfig.FIXED_LIBVIRT_NETWORK,
            iso_path=build.iso_image.iso_file.path,
            kickstart_path=str(kickstart_path),
            domain_xml="",
            ssh_public_key=key_pair.public_key,
            mac_address=vm_mac_address,
            disk_path=str(disk_path),
            boot_mode=effective_boot_mode,
        )
        _append_build_log(build=build, stage="vm", message=f"Defining PXE boot VM {vm_name} with MAC {vm_mac_address}")
        vm_manager.ensure_domain(vm_definition, replace_existing=True, start_domain=False)
        _append_build_log(build=build, stage="vm", message="Build VM shell is prepared and ready for Install OS")
        _save_runtime_state(
            build,
            builder_ip=builder_ip,
            builder_ssh_user=builder_ssh_user,
            tftp_root=tftp_root,
            vm_name=vm_name,
            vm_mac_address=vm_mac_address,
            disk_path=str(disk_path),
            effective_boot_mode=effective_boot_mode,
            kickstart_name=kickstart_path.name,
            install_source_url=install_source_url,
            build_ssh_user=ssh_user,
            build_ip_address="",
            partition_dump_dir="",
            clone_release_path="",
        )
        return {"vm": vm_name, "mac": vm_mac_address}
    finally:
        key_pair.cleanup_private()


def _step_install_os(build: BuildDefinition) -> dict[str, str]:
    timeout_seconds = _build_timeout_seconds(build)
    builder_manager, builder_provisioner, builder_ip, builder_ssh_user, builder_key_path = _builder_session(
        build,
        ensure_iso_shared=False,
    )
    state = _runtime_state(build)
    vm_name = str(state.get("vm_name") or _vm_name(build))
    vm_mac_address = str(state.get("vm_mac_address") or "").strip()
    tftp_root = str(state.get("tftp_root") or "/var/lib/tftpboot").strip() or "/var/lib/tftpboot"
    install_source_url = _require_runtime_value(build, "install_source_url")
    kickstart_name = _require_runtime_value(build, "kickstart_name")
    ssh_user = str(state.get("build_ssh_user") or getattr(settings, "BUILD_VM_SSH_USER", "root"))
    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    if not vm_manager.domain_exists(vm_name):
        raise ProvisioningError(f"Build VM {vm_name} is not defined. Run Create VM first.")
    if not vm_manager.domain_is_active(vm_name):
        _append_build_log(build=build, stage="vm", message=f"Starting build VM {vm_name} for OS installation")
        vm_manager.start_domain(vm_name)
    else:
        _append_build_log(build=build, stage="vm", message=f"Build VM {vm_name} is already running")
    _append_build_log(build=build, stage="network", message=f"Watching builder dnsmasq for MAC {vm_mac_address}")
    key_pair = _materialize_build_keypair(build)
    try:
        kickstart_dir = _artifact_root() / "kickstarts"
        refreshed_kickstart_path = render_kickstart_file(
            output_dir=kickstart_dir,
            vm_name=vm_name,
            ssh_public_key=key_pair.public_key,
            partition_layout=build.partition_layout,
        )
        remote_kickstart_path = f"/var/www/html/kickstarts/{kickstart_name}"
        _run_remote_checked(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            command="mkdir -p /var/www/html/kickstarts",
            timeout_seconds=timeout_seconds,
        )
        builder_provisioner.upload_file(
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            local_path=refreshed_kickstart_path,
            remote_path=remote_kickstart_path,
            timeout_seconds=timeout_seconds,
        )
        _run_remote_checked(
            provisioner=builder_provisioner,
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            command=(
                f"restorecon -v {remote_kickstart_path} >/dev/null 2>&1 "
                f"|| chcon -t httpd_sys_content_t {remote_kickstart_path} >/dev/null 2>&1 || true"
            ),
            timeout_seconds=timeout_seconds,
        )
        _append_build_log(build=build, stage="kickstart", message=f"Refreshed kickstart file {kickstart_name}")
        logvol_lines = [
            line.strip()
            for line in refreshed_kickstart_path.read_text(encoding="utf-8").splitlines()
            if line.lstrip().startswith("logvol ")
        ]
        if logvol_lines:
            _append_build_log(
                build=build,
                stage="kickstart",
                message=f"Kickstart logvol lines: {' | '.join(logvol_lines)}",
            )

        ip_address = builder_provisioner.wait_for_guest_boot_progress(
            host=builder_ip,
            user=builder_ssh_user,
            private_key_path=str(builder_key_path),
            mac_address=vm_mac_address,
            ssh_user=ssh_user,
            ssh_private_key_path=str(key_pair.private_key_path),
            kickstart_url=f"http://{builder_ip}/kickstarts/{kickstart_name}",
            install_source_url=install_source_url,
            progress_cb=lambda stage, message: _append_build_log(build=build, stage=stage, message=message),
            timeout_seconds=timeout_seconds,
        )
        _append_build_log(build=build, stage="network", message=f"Build VM is reachable at {ip_address}")
        _save_runtime_state(build, builder_ip=builder_ip, build_ip_address=ip_address)
        return {"ip": ip_address}
    finally:
        key_pair.cleanup_private()


def _step_run_playbooks(build: BuildDefinition) -> dict[str, str]:
    vm_name = str(_runtime_state(build).get("vm_name") or _vm_name(build))
    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    if not vm_manager.domain_exists(vm_name) or not vm_manager.domain_is_active(vm_name):
        raise ProvisioningError(f"Build VM {vm_name} is not running")

    ip_address = _resolve_ip_for_existing_vm(build)
    key_pair = _materialize_build_keypair(build)
    try:
        ssh_user = str(_runtime_state(build).get("build_ssh_user") or getattr(settings, "BUILD_VM_SSH_USER", "root"))
        provisioner = AnsibleProvisioner(project_root=Path(__file__).resolve().parents[2])
        _append_build_log(build=build, stage="ssh", message=f"Waiting for SSH access on {ssh_user}@{ip_address}")
        provisioner.wait_for_ssh(
            host=ip_address,
            user=ssh_user,
            private_key_path=str(key_pair.private_key_path),
            timeout_seconds=180,
        )
        _append_build_log(build=build, stage="ssh", message="Build VM SSH login is ready")
        _run_build_phase_repositories(
            build=build,
            provisioner=provisioner,
            ip_address=ip_address,
            ssh_user=ssh_user,
            private_key_path=str(key_pair.private_key_path),
        )
        try:
            _run_selected_playbooks(
                build=build,
                provisioner=provisioner,
                ip_address=ip_address,
                ssh_user=ssh_user,
                private_key_path=str(key_pair.private_key_path),
            )
        finally:
            _cleanup_build_phase_repositories(
                build=build,
                provisioner=provisioner,
                ip_address=ip_address,
                ssh_user=ssh_user,
                private_key_path=str(key_pair.private_key_path),
            )
        _save_runtime_state(build, build_ip_address=ip_address)
        return {"ip": ip_address}
    finally:
        key_pair.cleanup_private()


def _step_shutdown(build: BuildDefinition) -> dict[str, str]:
    vm_name = str(_runtime_state(build).get("vm_name") or _vm_name(build))
    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    _append_build_log(build=build, stage="shutdown", message="Shutting down build VM")
    vm_manager.shutdown_and_wait(vm_name, timeout_seconds=_build_timeout_seconds(build))
    return {"vm": vm_name}


def _step_dump_partitions(build: BuildDefinition) -> dict[str, str]:
    artifact_root = _artifact_root()
    build_dir = artifact_root / f"build-{build.id}"
    dump_dir = build_dir / "clone-release"
    if dump_dir.exists():
        import shutil
        shutil.rmtree(dump_dir)
    _append_build_log(build=build, stage="artifacts", message="Dumping build partitions to clone workspace")
    dump_clone_partitions(
        build=build,
        qcow2_disk_path=Path(_require_runtime_value(build, "disk_path")),
        output_dir=dump_dir,
        compress=ServerConfiguration.compression_enabled(),
    )
    _save_runtime_state(build, partition_dump_dir=str(dump_dir), clone_release_path="")
    return {"partition_dump_dir": str(dump_dir)}


def _step_save_release(build: BuildDefinition) -> dict[str, str]:
    artifact_root = _artifact_root()
    build_dir = artifact_root / f"build-{build.id}"
    _require_runtime_value(build, "partition_dump_dir")
    build.artifacts.all().delete()
    _append_build_log(build=build, stage="artifacts", message="Generating build artifacts")
    generate_artifacts(
        build=build,
        root=artifact_root,
        qcow2_disk_path=Path(_require_runtime_value(build, "disk_path")),
        compress=ServerConfiguration.compression_enabled(),
    )
    _append_build_log(build=build, stage="artifacts", message="Skipping standalone clone-release.tar.gz; publishing PXE/USB artifacts only")

    # Space cleanup: keep published artifacts, remove intermediate staging trees and stale legacy outputs.
    shutil.rmtree(build_dir / "pxe", ignore_errors=True)
    shutil.rmtree(build_dir / "usb", ignore_errors=True)
    for stale_name in (
        "pxe.tar.gz.gz",
        "usb_image.img",
        "usb_image.img.gz",
    ):
        stale_path = build_dir / stale_name
        if stale_path.exists():
            stale_path.unlink(missing_ok=True)

    _save_runtime_state(build, clone_release_path="")
    _append_build_log(build=build, stage="done", message="Build completed successfully")
    return {"clone_release_path": ""}


def _execute_step(build: BuildDefinition, step: str) -> dict[str, str]:
    if step == BuildDefinition.STEP_VM_SHELL:
        return _step_create_vm_shell(build)
    if step == BuildDefinition.STEP_INSTALL_OS:
        return _step_install_os(build)
    if step == BuildDefinition.STEP_INSTALL_PACKAGES:
        return _step_install_packages(build)
    if step == BuildDefinition.STEP_RUN_PLAYBOOKS:
        return _step_run_playbooks(build)
    if step == BuildDefinition.STEP_SHUTDOWN:
        return _step_shutdown(build)
    if step == BuildDefinition.STEP_DUMP_PARTITIONS:
        return _step_dump_partitions(build)
    if step == BuildDefinition.STEP_SAVE_RELEASE:
        return _step_save_release(build)
    if step == BuildDefinition.STEP_CLEANUP:
        vm_name = str(_runtime_state(build).get("vm_name") or _vm_name(build))
        disk_path = str(_runtime_state(build).get("disk_path") or _disk_path(build))
        vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
        vm_manager.remove_domain(name=vm_name, disk_path=disk_path)
        build.status = BuildDefinition.STATUS_DRAFT
        build.current_step = BuildDefinition.STEP_PENDING
        build.runtime_state = {
            "last_completed_step": BuildDefinition.STEP_PENDING,
            "vm_name": None,
            "build_ip_address": None,
            "partition_dump_dir": None,
            "clone_release_path": None,
        }
        build.save(update_fields=["status", "current_step", "runtime_state", "updated_at"])
        return {"status": BuildDefinition.STATUS_DRAFT}
    raise ProvisioningError(f"Unknown build step '{step}'")


def _load_build(build_id: int) -> BuildDefinition:
    return BuildDefinition.objects.select_related(
        "machine_config", "iso_image", "operating_system", "partition_layout"
    ).get(pk=build_id)


@shared_task(bind=True)
def run_build_definition(self, build_id: int) -> dict[str, str]:
    task_id = str(getattr(self.request, "id", "") or "")
    if task_id:
        cache.set(build_task_cache_key(build_id), task_id, timeout=BUILD_TASK_CACHE_TTL_SECONDS)

    build = _load_build(build_id)
    _set_build_running(build, run_mode=BuildDefinition.RUN_MODE_AUTO, reset=True)
    _set_active_task_id(build, task_id)
    result: dict[str, str] = {}
    current_step = BuildDefinition.STEP_PENDING
    try:
        for step in BuildDefinition.STEP_SEQUENCE:
            current_step = step
            build.refresh_from_db()
            _begin_step(build, step=step)
            result.update(_execute_step(build, step))
            build.refresh_from_db()
            _complete_step(build, step=step, keep_running=step != BuildDefinition.STEP_SAVE_RELEASE)
        return {"status": build.status, **result}
    except (VirtualizationError, ProvisioningError, SSHKeyError, ArtifactExportError, PlaybookSyncError, subprocess.TimeoutExpired) as exc:
        _fail_build(build, step=current_step, exc=exc)
        raise RuntimeError(str(exc)) from exc
    finally:
        build.refresh_from_db()
        _clear_active_task_id(build, task_id)
        if task_id and cache.get(build_task_cache_key(build_id)) == task_id:
            cache.delete(build_task_cache_key(build_id))


@shared_task(bind=True)
def run_build_step(self, build_id: int, step: str) -> dict[str, str]:
    task_id = str(getattr(self.request, "id", "") or "")
    if task_id:
        cache.set(build_task_cache_key(build_id), task_id, timeout=BUILD_TASK_CACHE_TTL_SECONDS)

    build = _load_build(build_id)
    if not build.can_run_manual_step(step):
        raise RuntimeError(f"Step '{step}' is not available for this build right now")

    _set_build_running(build, run_mode=BuildDefinition.RUN_MODE_MANUAL, reset=step == BuildDefinition.STEP_VM_SHELL)
    _set_active_task_id(build, task_id)
    _begin_step(build, step=step)
    _append_build_log(build=build, stage="manual", message=f"Running manual step: {dict(BuildDefinition.STEP_CHOICES).get(step, step)}")
    try:
        result = _execute_step(build, step)
        build.refresh_from_db()
        if step != BuildDefinition.STEP_CLEANUP:
            _complete_step(build, step=step, keep_running=False)
        return {"status": build.status, "step": step, **result}
    except (VirtualizationError, ProvisioningError, SSHKeyError, ArtifactExportError, PlaybookSyncError, subprocess.TimeoutExpired) as exc:
        _fail_build(build, step=step, exc=exc)
        raise RuntimeError(str(exc)) from exc
    finally:
        build.refresh_from_db()
        _clear_active_task_id(build, task_id)
        if task_id and cache.get(build_task_cache_key(build_id)) == task_id:
            cache.delete(build_task_cache_key(build_id))


@shared_task(bind=True)
def rerun_build_playbooks(self, build_id: int, ip_address: str = "") -> dict[str, str]:
    task_id = str(getattr(self.request, "id", "") or "")
    if task_id:
        cache.set(build_task_cache_key(build_id), task_id, timeout=BUILD_TASK_CACHE_TTL_SECONDS)

    build = _load_build(build_id)
    _set_active_task_id(build, task_id)
    vm_name = str(_runtime_state(build).get("vm_name") or _vm_name(build))
    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)

    try:
        if not vm_manager.domain_exists(vm_name) or not vm_manager.domain_is_active(vm_name):
            raise ProvisioningError(f"Build VM {vm_name} is not running")

        effective_ip = (ip_address or "").strip() or _resolve_ip_for_existing_vm(build)
        key_pair = _materialize_build_keypair(build)
        try:
            ssh_user = str(_runtime_state(build).get("build_ssh_user") or getattr(settings, "BUILD_VM_SSH_USER", "root"))
            provisioner = AnsibleProvisioner(project_root=Path(__file__).resolve().parents[2])
            _append_build_log(build=build, stage="playbooks", message=f"Re-running playbooks on {effective_ip}")
            _append_build_log(build=build, stage="ssh", message=f"Waiting for SSH access on {ssh_user}@{effective_ip}")
            provisioner.wait_for_ssh(
                host=effective_ip,
                user=ssh_user,
                private_key_path=str(key_pair.private_key_path),
                timeout_seconds=180,
            )
            _append_build_log(build=build, stage="ssh", message="Build VM SSH login is ready")
            _run_build_phase_repositories(
                build=build,
                provisioner=provisioner,
                ip_address=effective_ip,
                ssh_user=ssh_user,
                private_key_path=str(key_pair.private_key_path),
            )
            try:
                _run_selected_playbooks(
                    build=build,
                    provisioner=provisioner,
                    ip_address=effective_ip,
                    ssh_user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                )
            finally:
                _cleanup_build_phase_repositories(
                    build=build,
                    provisioner=provisioner,
                    ip_address=effective_ip,
                    ssh_user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                )
            _append_build_log(build=build, stage="playbooks", message="Playbook re-run completed successfully")
            _save_runtime_state(build, build_ip_address=effective_ip)
            return {"status": "ok", "ip": effective_ip, "vm": vm_name}
        finally:
            key_pair.cleanup_private()
    except (ProvisioningError, SSHKeyError, PlaybookSyncError, subprocess.TimeoutExpired) as exc:
        _append_build_log(build=build, stage="error", message=str(exc))
        raise RuntimeError(str(exc)) from exc
    finally:
        build.refresh_from_db()
        _clear_active_task_id(build, task_id)
        if task_id and cache.get(build_task_cache_key(build_id)) == task_id:
            cache.delete(build_task_cache_key(build_id))
