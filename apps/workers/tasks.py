from __future__ import annotations

import secrets
import shlex
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from apps.builds.models import BuildDefinition, BuildLogEntry
from apps.builds.models import BuildMachineConfig
from apps.builds.services.builder import BuilderVMManager
from apps.builds.services.artifacts import ArtifactExportError, generate_artifacts
from apps.builds.services.kickstart import calculate_layout_disk_size_gib, render_kickstart_file, render_pxe_boot_configs
from apps.builds.services.provisioning import AnsibleProvisioner, ProvisioningError
from apps.builds.services.ssh_keys import SSHKeyError, generate_build_ssh_keypair
from apps.builds.services.virtualization import LibvirtVMManager, VMDefinition, VirtualizationError
from apps.builds.services.artifacts import prepare_iso_pxe_assets
from apps.playbooks.services import PlaybookSyncError, checkout_repository
from apps.realtime.events import publish_event
from apps.serverconfig.models import ServerConfiguration


BUILD_TASK_CACHE_TTL_SECONDS = 6 * 60 * 60


def build_task_cache_key(build_id: int) -> str:
    return f"builds:active-task:{build_id}"


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


def _ensure_builder_pxe_bootloader(
    *,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    timeout_seconds: int,
) -> str:
    command = (
        "tftp_root=; "
        "if [ -f /etc/dnsmasq.d/wsbuildnet.conf ]; then "
        "  tftp_root=$(awk -F= '/^[[:space:]]*tftp-root=/{print $2; exit}' /etc/dnsmasq.d/wsbuildnet.conf | tr -d '[:space:]'); "
        "fi; "
        "if [ -z \"$tftp_root\" ]; then tftp_root=/var/lib/tftpboot; fi; "
        "if [ ! -d \"$tftp_root\" ] && [ -d /tftpboot ]; then tftp_root=/tftpboot; fi; "
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
        "mkdir -p \"$tftp_root/efi64\" \"$tftp_root/efi32\"; "
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
        "if [ -f /etc/dnsmasq.d/wsbuildnet.conf ]; then "
        "  sed -i 's#dhcp-boot=tag:efi64,grubx64.efi#dhcp-boot=tag:efi64,efi64/grubx64.efi#g' /etc/dnsmasq.d/wsbuildnet.conf; "
        "  sed -i 's#dhcp-boot=tag:efi32,grubia32.efi#dhcp-boot=tag:efi32,efi32/grubia32.efi#g' /etc/dnsmasq.d/wsbuildnet.conf; "
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
    kickstart_file = f"/var/www/html/kickstarts/build-{build_id}.cfg"
    iso_mount_path = f"/var/www/html/isos/build-{build_id}"
    iso_symlink_path = f"/var/www/html/isos/build-{build_id}.iso"
    cleanup_command = (
        "mkdir -p /var/www/html/isos; "
        f"if mountpoint -q {iso_mount_path}; then umount {iso_mount_path} || umount -l {iso_mount_path} || true; fi; "
        f"rm -f {iso_symlink_path}; "
        f"rm -rf {iso_mount_path}; "
        f"old_mac=; if [ -f {state_file} ]; then old_mac=$(cat {state_file} | tr -d '[:space:]'); fi; "
        f"rm -rf {build_root} {kickstart_file}; "
        "if [ -n \"$old_mac\" ]; then "
        "old_slug=$(printf '%s' \"$old_mac\" | tr '[:upper:]' '[:lower:]' | tr ':' '-'); "
        f"rm -f {tftp_root}/pxelinux.cfg/01-$old_slug {tftp_root}/grub.cfg-01-$old_slug {tftp_root}/grub.cfg-$old_slug; "
        "fi; "
        f"mkdir -p {tftp_root}/builds {tftp_root}/build-state {tftp_root}/pxelinux.cfg /var/www/html/kickstarts"
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
        "fi"
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


@shared_task(bind=True)
def run_build_definition(self, build_id: int) -> dict[str, str]:
    task_id = str(getattr(self.request, "id", "") or "")
    if task_id:
        cache.set(build_task_cache_key(build_id), task_id, timeout=BUILD_TASK_CACHE_TTL_SECONDS)

    build = BuildDefinition.objects.select_related(
        "machine_config", "iso_image", "operating_system"
    ).get(pk=build_id)

    effective_boot_mode = _resolve_effective_boot_mode(build=build)
    _validate_boot_layout_compatibility(build=build, effective_boot_mode=effective_boot_mode)

    timeout_seconds = build.machine_config.kickstart_timeout_minutes * 60

    with transaction.atomic():
        build.status = BuildDefinition.STATUS_RUNNING
        build.save(update_fields=["status", "updated_at"])
        BuildLogEntry.objects.filter(build=build).delete()
    publish_event("builds", "running", {"build_id": build.id, "status": build.status})
    _append_build_log(build=build, stage="start", message="Build worker started")
    _append_build_log(build=build, stage="vm", message=f"Effective boot mode: {effective_boot_mode}")

    builder_manager = BuilderVMManager()
    builder_manager.ensure_iso_shared(Path(build.iso_image.iso_file.path))
    builder_manager.ensure_builder_vm()
    builder_ip = builder_manager.wait_for_ipv4(timeout_seconds=timeout_seconds)
    builder_access = builder_manager.ensure_access_keypair()
    builder_ssh_user = getattr(settings, "BUILDER_VM_SSH_USER", "root")
    builder_provisioner = AnsibleProvisioner(project_root=Path(__file__).resolve().parents[2])

    _append_build_log(build=build, stage="builder", message=f"Builder VM ready at {builder_ip}")

    builder_provisioner.wait_for_ssh(
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        timeout_seconds=timeout_seconds,
    )

    _append_build_log(build=build, stage="pxe", message="Ensuring builder TFTP bootloader files")
    tftp_root = _ensure_builder_pxe_bootloader(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        timeout_seconds=timeout_seconds,
    )
    _append_build_log(build=build, stage="pxe", message=f"Using TFTP root {tftp_root}")

    _cleanup_builder_bootstrap_assets(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        tftp_root=tftp_root,
        build_id=build.id,
        timeout_seconds=timeout_seconds,
    )

    vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
    vm_name = f"build-{build.id}"
    vm_mac_address = _generate_mac_address()
    ssh_user = getattr(settings, "BUILD_VM_SSH_USER", "root")
    artifact_root = Path(settings.ARTIFACT_ROOT)
    key_dir = artifact_root / "keys"
    kickstart_dir = artifact_root / "kickstarts"
    disk_dir = artifact_root / "disks"
    key_pair = generate_build_ssh_keypair(build.id, key_dir)
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
        private_key_path=str(builder_access.private_key_path),
        timeout_seconds=timeout_seconds,
        build_id=build.id,
        iso_guest_path=iso_guest_path,
    )
    _append_build_log(build=build, stage="pxe", message=f"Published ISO source URL {install_source_url}")

    disk_path = disk_dir / f"{vm_name}.qcow2"

    remote_build_root = f"{tftp_root}/builds/{vm_name}"
    remote_kickstart_path = f"/var/www/html/kickstarts/{kickstart_path.name}"
    remote_boot_kernel = f"{remote_build_root}/vmlinuz"
    remote_boot_initrd = f"{remote_build_root}/initrd.img"
    mac_slug = vm_mac_address.lower().replace(':', '-')
    remote_bios_config = f"{tftp_root}/pxelinux.cfg/01-{mac_slug}"
    remote_efi_config = f"{tftp_root}/grub.cfg-01-{mac_slug}"
    remote_efi_config_alt = f"{tftp_root}/grub.cfg-{mac_slug}"
    remote_efi_default = f"{tftp_root}/grub.cfg"

    _run_remote_checked(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        command=f"mkdir -p {remote_build_root} {tftp_root}/pxelinux.cfg /var/www/html/kickstarts",
        timeout_seconds=timeout_seconds,
    )
    builder_provisioner.upload_file(
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        local_path=kickstart_path,
        remote_path=remote_kickstart_path,
        timeout_seconds=timeout_seconds,
    )
    builder_provisioner.upload_file(
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        local_path=pxe_assets_dir / "boot" / "vmlinuz",
        remote_path=remote_boot_kernel,
        timeout_seconds=timeout_seconds,
    )
    builder_provisioner.upload_file(
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
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
    _run_remote_checked(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        command=f"cat > {remote_bios_config}",
        timeout_seconds=timeout_seconds,
        input_text=boot_configs["bios"],
    )
    _run_remote_checked(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        command=f"cat > {remote_efi_config}",
        timeout_seconds=timeout_seconds,
        input_text=boot_configs["efi"],
    )
    _run_remote_checked(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        command=f"cat > {remote_efi_config_alt}",
        timeout_seconds=timeout_seconds,
        input_text=boot_configs["efi"],
    )
    _run_remote_checked(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        command=f"cat > {remote_efi_default}",
        timeout_seconds=timeout_seconds,
        input_text=boot_configs["efi"],
    )
    _run_remote_checked(
        provisioner=builder_provisioner,
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
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

    try:
        _append_build_log(build=build, stage="vm", message=f"Defining PXE boot VM {vm_name} with MAC {vm_mac_address}")
        vm_manager.ensure_domain(vm_definition, replace_existing=True)
        vm_manager.start_domain(vm_name)
        _append_build_log(build=build, stage="vm", message="Build VM started and waiting for DHCP lease")

        try:
            ip_address = builder_provisioner.wait_for_dnsmasq_lease(
                host=builder_ip,
                user=builder_ssh_user,
                private_key_path=str(builder_access.private_key_path),
                mac_address=vm_mac_address,
                timeout_seconds=timeout_seconds,
            )
        except ProvisioningError:
            ip_address = vm_manager.wait_for_ipv4(
                domain_name=vm_name,
                network_name=BuildMachineConfig.FIXED_LIBVIRT_NETWORK,
                timeout_seconds=timeout_seconds,
            )

        _append_build_log(build=build, stage="network", message=f"Build VM obtained IP address {ip_address}")
        if not ip_address:
            raise VirtualizationError("Timed out waiting for VM IP after kickstart")

        builder_provisioner.wait_for_ssh(
            host=ip_address,
            user=ssh_user,
            private_key_path=str(key_pair.private_key_path),
            timeout_seconds=timeout_seconds,
        )
        _append_build_log(build=build, stage="ssh", message="Build VM SSH login is ready")

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
                builder_provisioner.configure_guest(
                    host=ip_address,
                    playbook_path=selection.playbook.path,
                    user=ssh_user,
                    private_key_path=str(key_pair.private_key_path),
                    working_dir=repo_checkout,
                )
        elif build.playbook_path:
            _append_build_log(build=build, stage="playbooks", message=f"Running fallback playbook {build.playbook_path}")
            builder_provisioner.configure_guest(
                host=ip_address,
                playbook_path=build.playbook_path,
                user=ssh_user,
                private_key_path=str(key_pair.private_key_path),
            )

        _append_build_log(build=build, stage="shutdown", message="Waiting for installed VM to shut down and reboot")
        vm_manager.shutdown_and_wait(
            vm_name,
            timeout_seconds=timeout_seconds,
        )

        build.artifacts.all().delete()
        _append_build_log(build=build, stage="artifacts", message="Generating build artifacts")
        generate_artifacts(
            build=build,
            root=artifact_root,
            qcow2_disk_path=disk_path,
            compress=ServerConfiguration.compression_enabled(),
        )

        build.status = BuildDefinition.STATUS_SUCCEEDED
        build.save(update_fields=["status", "updated_at"])
        _append_build_log(build=build, stage="done", message="Build completed successfully")
        publish_event(
            "builds",
            "succeeded",
            {
                "build_id": build.id,
                "status": build.status,
                "artifact_count": build.artifacts.count(),
            },
        )
        return {"status": build.status, "vm": vm_name, "ip": ip_address}
    except (VirtualizationError, ProvisioningError, SSHKeyError, ArtifactExportError, PlaybookSyncError) as exc:
        _append_build_log(build=build, stage="error", message=str(exc))
        build.status = BuildDefinition.STATUS_FAILED
        build.save(update_fields=["status", "updated_at"])
        publish_event(
            "builds",
            "failed",
            {"build_id": build.id, "status": build.status, "error": str(exc)},
        )
        raise RuntimeError(str(exc)) from exc
    finally:
        key_pair.cleanup_private()
        if task_id and cache.get(build_task_cache_key(build_id)) == task_id:
            cache.delete(build_task_cache_key(build_id))
