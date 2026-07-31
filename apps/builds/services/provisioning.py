from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


class ProvisioningError(RuntimeError):
    pass


class AnsibleProvisioner:
    """Runs post-kickstart guest configuration with ansible-playbook."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def wait_for_ssh(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        timeout_seconds: int = 600,
    ) -> None:
        end = time.time() + timeout_seconds
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-i",
            private_key_path,
            f"{user}@{host}",
            "true",
        ]
        while time.time() < end:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                return
            time.sleep(5)
        raise ProvisioningError("Timed out waiting for SSH login with generated key")

    def run_remote_command(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        command: str,
        timeout_seconds: int = 600,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        ssh_cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-i",
            private_key_path,
            f"{user}@{host}",
            "bash",
            "-lc",
            shlex.quote(command),
        ]
        return subprocess.run(
            ssh_cmd,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )

    def upload_file(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        local_path: Path,
        remote_path: str,
        timeout_seconds: int = 600,
    ) -> None:
        scp_cmd = [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-i",
            private_key_path,
            str(local_path),
            f"{user}@{host}:{remote_path}",
        ]
        proc = subprocess.run(scp_cmd, capture_output=True, text=True, check=False, timeout=timeout_seconds)
        if proc.returncode != 0:
            raise ProvisioningError(f"scp failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")

    def wait_for_dnsmasq_lease(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        mac_address: str,
        progress_cb: Callable[[str, str], None] | None = None,
        lease_path: str = "/var/lib/misc/dnsmasq.leases",
        timeout_seconds: int = 1200,
    ) -> str:
        end = time.time() + timeout_seconds
        mac_address = mac_address.lower()
        mac_pattern = re.compile(rf"\b{re.escape(mac_address)}\b")
        ipv4_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        http_request_pattern = re.compile(r'"(?:GET|HEAD)\s+([^\s]+)\s+HTTP/[^\"]+"')
        seen_lines: set[str] = set()
        saw_dhcp_request = False
        command = f"if [ -f {shlex.quote(lease_path)} ]; then cat {shlex.quote(lease_path)}; fi"
        while time.time() < end:
            proc = self.run_remote_command(
                host=host,
                user=user,
                private_key_path=private_key_path,
                command=command,
                timeout_seconds=20,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 3 and mac_pattern.search(line):
                        ip_address = parts[2]
                        if ip_address:
                            if progress_cb is not None:
                                progress_cb("network", f"Builder dnsmasq assigned {ip_address} to MAC {mac_address}")
                            return ip_address

            journal_proc = self.run_remote_command(
                host=host,
                user=user,
                private_key_path=private_key_path,
                command="journalctl -u dnsmasq --no-pager -o cat --since '10 minutes ago' 2>/dev/null || true",
                timeout_seconds=20,
            )
            for line in (journal_proc.stdout or "").splitlines():
                normalized_line = line.strip()
                if not normalized_line or normalized_line in seen_lines:
                    continue
                seen_lines.add(normalized_line)
                if not mac_pattern.search(normalized_line):
                    continue

                if progress_cb is not None and not saw_dhcp_request and (
                    "DHCPDISCOVER" in normalized_line or "DHCPREQUEST" in normalized_line
                ):
                    progress_cb("network", f"Builder dnsmasq saw DHCP traffic from MAC {mac_address}")
                    saw_dhcp_request = True

                if "DHCPACK" in normalized_line or "DHCPOFFER" in normalized_line:
                    ip_match = ipv4_pattern.search(normalized_line)
                    if ip_match:
                        ip_address = ip_match.group(0)
                        if progress_cb is not None:
                            progress_cb("network", f"Builder dnsmasq assigned {ip_address} to MAC {mac_address}")
                        return ip_address
            time.sleep(5)
        raise ProvisioningError(f"Timed out waiting for DHCP lease for MAC {mac_address}")

    def wait_for_guest_boot_progress(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        mac_address: str,
        ssh_user: str,
        ssh_private_key_path: str,
        kickstart_url: str,
        install_source_url: str,
        progress_cb: Callable[[str, str], None] | None = None,
        lease_path: str = "/var/lib/misc/dnsmasq.leases",
        http_access_log_path: str = "/var/log/httpd/access_log",
        timeout_seconds: int = 1200,
        ssh_probe_timeout_seconds: int = 60,
    ) -> str:
        end = time.time() + timeout_seconds
        mac_address = mac_address.lower()
        mac_slug = mac_address.replace(":", "-")
        lease_line_pattern = re.compile(
            r"^\s*\d+\s+([0-9a-f:]{17})\s+((?:\d{1,3}\.){3}\d{1,3})\s+",
            re.IGNORECASE,
        )
        mac_pattern = re.compile(rf"\b{re.escape(mac_address)}\b")
        ipv4_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        http_request_pattern = re.compile(r'"(?:GET|HEAD)\s+([^\s]+)\s+HTTP/[^\"]+"')
        seen_lines: set[str] = set()
        current_ip = ""
        initial_request_logged = False
        initial_ip_logged = False
        bootloader_logged = False
        kernel_logged = False
        initrd_logged = False
        kickstart_logged = False
        install_source_logged = False
        reboot_request_logged = False
        reboot_ip_logged = False
        last_ssh_wait_log_at = 0.0
        last_ssh_wait_ip = ""

        kickstart_path = urlparse(kickstart_url).path or kickstart_url
        install_source_path = urlparse(install_source_url).path or install_source_url

        def emit(stage: str, message: str) -> None:
            if progress_cb is not None:
                progress_cb(stage, message)

        def safe_remote(command: str, *, timeout: int = 20) -> str:
            # SSH/journal reads can occasionally time out under load; treat as transient.
            try:
                proc = self.run_remote_command(
                    host=host,
                    user=user,
                    private_key_path=private_key_path,
                    command=command,
                    timeout_seconds=timeout,
                )
            except Exception:
                return ""
            if proc.returncode != 0:
                return ""
            return proc.stdout or ""

        def observe_dhcp(line: str) -> None:
            nonlocal current_ip, initial_request_logged, initial_ip_logged, reboot_request_logged, reboot_ip_logged
            lease_match = lease_line_pattern.match(line)
            if lease_match:
                lease_mac = lease_match.group(1).lower()
                lease_ip = lease_match.group(2)
                if lease_mac == mac_address:
                    current_ip = lease_ip
                    if not initial_ip_logged:
                        emit("network", f"VM {mac_address} received IP address {current_ip}")
                        initial_ip_logged = True
                return

            if not mac_pattern.search(line):
                return
            upper_line = line.upper()
            if "DHCPDISCOVER" in upper_line or "DHCPREQUEST" in upper_line:
                if not initial_request_logged:
                    emit("network", f"VM {mac_address} requested an IP from builder dnsmasq")
                    initial_request_logged = True
                elif initial_ip_logged and initrd_logged and not reboot_request_logged:
                    emit("network", f"VM {mac_address} requested an IP again after installation")
                    reboot_request_logged = True
            if "DHCPACK" in upper_line or "DHCPOFFER" in upper_line:
                ip_match = ipv4_pattern.search(line)
                if not ip_match:
                    return
                current_ip = ip_match.group(0)
                if not initial_ip_logged:
                    emit("network", f"VM {mac_address} received IP address {current_ip}")
                    initial_ip_logged = True
                elif not reboot_ip_logged:
                    emit("network", f"VM {mac_address} received IP address {current_ip} after reboot")
                    reboot_ip_logged = True

        def observe_tftp(line: str) -> None:
            nonlocal bootloader_logged, kernel_logged, initrd_logged
            lower_line = line.lower()
            if not mac_pattern.search(line):
                if mac_slug not in lower_line and (not current_ip or current_ip not in line):
                    return
            if "tftp" not in lower_line and "sent " not in lower_line and "file " not in lower_line:
                return
            if not bootloader_logged and any(marker in lower_line for marker in ("pxelinux.0", "grubx64.efi", "grubia32.efi", "bootx64.efi", "bootia32.efi")):
                if "pxelinux.0" in lower_line:
                    emit("pxe", f"VM {mac_address} requested bootloader pxelinux.0")
                elif "grubia32.efi" in lower_line or "bootia32.efi" in lower_line:
                    emit("pxe", f"VM {mac_address} requested bootloader grubia32.efi")
                else:
                    emit("pxe", f"VM {mac_address} requested bootloader grubx64.efi")
                bootloader_logged = True
            if not kernel_logged and "vmlinuz" in lower_line:
                emit("pxe", f"VM {mac_address} requested kernel vmlinuz")
                kernel_logged = True
            if not initrd_logged and "initrd.img" in lower_line:
                emit("pxe", f"VM {mac_address} requested initrd initrd.img")
                initrd_logged = True

        def observe_http(line: str) -> None:
            nonlocal kickstart_logged, install_source_logged
            if not current_ip or current_ip not in line:
                return
            req_match = http_request_pattern.search(line)
            if req_match is None:
                return
            request_path = req_match.group(1)
            install_source_root = install_source_path.rstrip("/")
            kickstart_root = kickstart_path.rstrip("/")
            if not kickstart_logged and kickstart_path and kickstart_path in line:
                if request_path == kickstart_root or request_path.startswith(kickstart_root + "/"):
                    emit("kickstart", f"VM {mac_address} requested kickstart file {kickstart_path}")
                    kickstart_logged = True
            if (
                not install_source_logged
                and install_source_path
                and (request_path == install_source_root or request_path.startswith(install_source_root + "/"))
            ):
                emit("install", f"VM {mac_address} requested installation source {install_source_path}")
                install_source_logged = True

        lease_command = f"if [ -f {shlex.quote(lease_path)} ]; then cat {shlex.quote(lease_path)}; fi"
        http_command = f"if [ -f {shlex.quote(http_access_log_path)} ]; then tail -n 2000 {shlex.quote(http_access_log_path)}; fi"
        dnsmasq_command = "journalctl -u dnsmasq --no-pager -o short-iso -n 300 2>/dev/null || true"

        emit("network", f"Monitoring builder DHCP/TFTP/HTTP activity for MAC {mac_address}")

        while time.time() < end:
            lease_output = safe_remote(lease_command, timeout=20)
            for line in lease_output.splitlines():
                normalized_line = line.strip()
                if normalized_line:
                    observe_dhcp(normalized_line)

            dnsmasq_output = safe_remote(dnsmasq_command, timeout=20)
            for line in dnsmasq_output.splitlines():
                normalized_line = line.strip()
                if normalized_line and normalized_line not in seen_lines:
                    seen_lines.add(normalized_line)
                    observe_dhcp(normalized_line)
                    observe_tftp(normalized_line)

            http_output = safe_remote(http_command, timeout=20)
            for line in http_output.splitlines():
                normalized_line = line.strip()
                if normalized_line and normalized_line not in seen_lines:
                    seen_lines.add(normalized_line)
                    observe_http(normalized_line)

            if current_ip:
                now = time.time()
                if current_ip != last_ssh_wait_ip or (now - last_ssh_wait_log_at) >= 30:
                    emit("ssh", f"Waiting for SSH access on {ssh_user}@{current_ip}")
                    last_ssh_wait_ip = current_ip
                    last_ssh_wait_log_at = now

                ssh_cmd = [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-i",
                    ssh_private_key_path,
                    f"{ssh_user}@{current_ip}",
                    "true",
                ]
                try:
                    ssh_proc = subprocess.run(
                        ssh_cmd,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=max(20, ssh_probe_timeout_seconds),
                    )
                except subprocess.TimeoutExpired:
                    time.sleep(5)
                    continue
                if ssh_proc.returncode == 0:
                    emit("ssh", "Build VM SSH login is ready")
                    return current_ip

            time.sleep(5)

        raise ProvisioningError(f"Timed out waiting for guest boot progress and SSH login for MAC {mac_address}")

    def configure_guest(
        self,
        *,
        host: str,
        playbook_path: str,
        user: str = "root",
        private_key_path: str,
        working_dir: Path | None = None,
    ) -> None:
        cwd = working_dir or self.project_root
        requested_path = Path(playbook_path)
        resolved_playbook = requested_path if requested_path.is_absolute() else (cwd / requested_path)

        file_kind = self._classify_ansible_file(resolved_playbook)

        if file_kind == "task_list":
            wrapped_proc = self._run_task_list_wrapper(
                host=host,
                user=user,
                private_key_path=private_key_path,
                task_list_path=resolved_playbook,
                working_dir=cwd,
            )
            if wrapped_proc.returncode != 0:
                raise ProvisioningError(
                    "ansible task-list wrapper failed: "
                    f"{wrapped_proc.stderr.strip() or wrapped_proc.stdout.strip()}"
                )
            return

        proc = self._run_ansible_playbook(
            host=host,
            user=user,
            private_key_path=private_key_path,
            playbook_path=resolved_playbook,
            working_dir=cwd,
        )
        if proc.returncode == 0:
            return

        # Unknown/mixed structures get one fallback wrapper attempt for compatibility.
        if file_kind == "playbook":
            raise ProvisioningError(
                f"ansible-playbook failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

        wrapped_proc = self._run_task_list_wrapper(
            host=host,
            user=user,
            private_key_path=private_key_path,
            task_list_path=resolved_playbook,
            working_dir=cwd,
        )
        if wrapped_proc.returncode != 0:
            raise ProvisioningError(
                "ansible-playbook failed as playbook and task-list wrapper. "
                f"Direct error: {proc.stderr.strip() or proc.stdout.strip()} | "
                f"Wrapper error: {wrapped_proc.stderr.strip() or wrapped_proc.stdout.strip()}"
            )

    def _run_task_list_wrapper(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        task_list_path: Path,
        working_dir: Path,
    ) -> subprocess.CompletedProcess:

        wrapper_text = (
            "---\n"
            "- name: TuxWSMaker Task List Wrapper\n"
            "  hosts: template_vm\n"
            "  gather_facts: true\n"
            "  pre_tasks:\n"
            "    - name: Gather network facts\n"
            "      ansible.builtin.setup:\n"
            "        gather_subset:\n"
            "          - min\n"
            "          - network\n"
            "\n"
            "    - name: Normalize default IPv4 fact key\n"
            "      ansible.builtin.set_fact:\n"
            "        ansible_facts: \"{{ ansible_facts | combine({'default_ipv4': (ansible_facts.ansible_default_ipv4 | default(ansible_default_ipv4 | default({})))}, recursive=True) }}\"\n"
            "      when:\n"
            "        - ansible_facts.default_ipv4 is not defined\n"
            "        - ansible_facts.ansible_default_ipv4 is defined or ansible_default_ipv4 is defined\n"
            "  tasks:\n"
            "    - name: Include task list\n"
            f"      ansible.builtin.include_tasks: {task_list_path.as_posix()}\n"
        )

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yml", delete=False) as wrapper_file:
            wrapper_file.write(wrapper_text)
            wrapper_path = Path(wrapper_file.name)

        try:
            wrapped_proc = self._run_ansible_playbook(
                host=host,
                user=user,
                private_key_path=private_key_path,
                playbook_path=wrapper_path,
                working_dir=working_dir,
            )
        finally:
            wrapper_path.unlink(missing_ok=True)
        return wrapped_proc

    def _run_ansible_playbook(
        self,
        *,
        host: str,
        user: str,
        private_key_path: str,
        playbook_path: Path,
        working_dir: Path,
    ) -> subprocess.CompletedProcess:
        inventory_text = (
            "[all]\n"
            f"template_vm ansible_host={host}\n"
            "[template_vm]\n"
            "template_vm\n"
            "[target_vm]\n"
            "template_vm\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".ini", delete=False) as inventory_file:
            inventory_file.write(inventory_text)
            inventory_path = Path(inventory_file.name)

        try:
            cmd = [
                "ansible-playbook",
                "-i",
                str(inventory_path),
                "-u",
                user,
                "--private-key",
                private_key_path,
                str(playbook_path),
            ]
            return subprocess.run(
                cmd,
                cwd=working_dir,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            inventory_path.unlink(missing_ok=True)

    def _classify_ansible_file(self, path: Path) -> str:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return "unknown"

        lines = content.splitlines()

        first_content = ""
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            first_content = line
            break

        if not first_content:
            return "unknown"
        if not first_content.startswith("-"):
            return "unknown"

        # A real playbook contains play-level hosts/import_playbook keys.
        if re.search(r"(?m)^\s*hosts\s*:", content):
            return "playbook"
        if re.search(r"(?m)^\s*-\s*import_playbook\s*:", content):
            return "playbook"

        # Task files typically start with list items and omit play-level hosts.
        if re.search(r"(?m)^\s*-\s*name\s*:", content):
            return "task_list"
        return "unknown"
