from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path


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
        lease_path: str = "/var/lib/misc/dnsmasq.leases",
        timeout_seconds: int = 1200,
    ) -> str:
        end = time.time() + timeout_seconds
        mac_address = mac_address.lower()
        pattern = re.compile(rf"\b{re.escape(mac_address)}\b")
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
                    if len(parts) >= 3 and pattern.search(line):
                        ip_address = parts[2]
                        if ip_address:
                            return ip_address
            time.sleep(5)
        raise ProvisioningError(f"Timed out waiting for DHCP lease for MAC {mac_address}")

    def configure_guest(
        self,
        *,
        host: str,
        playbook_path: str,
        user: str = "root",
        private_key_path: str,
        working_dir: Path | None = None,
    ) -> None:
        cmd = [
            "ansible-playbook",
            "-i",
            f"{host},",
            "-u",
            user,
            "--private-key",
            private_key_path,
            playbook_path,
        ]
        proc = subprocess.run(
            cmd,
            cwd=working_dir or self.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ProvisioningError(
                f"ansible-playbook failed ({proc.returncode}): {proc.stderr.strip()}"
            )
