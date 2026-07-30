from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class VirtualizationError(RuntimeError):
    pass


@dataclass
class VMDefinition:
    name: str
    memory_mib: int
    vcpus: int
    disk_gib: int
    network_name: str
    iso_path: str
    kickstart_path: str
    domain_xml: str
    ssh_public_key: str = ""
    mac_address: str = ""
    disk_path: str = ""
    boot_mode: str = "bios"


class LibvirtVMManager:
    """Manages builder VMs via libvirt Python bindings."""

    UEFI_CODE_CANDIDATES = (
        "/usr/share/OVMF/OVMF_CODE_4M.fd",
        "/usr/share/OVMF/OVMF_CODE.fd",
        "/usr/share/qemu/OVMF.fd",
        "/usr/share/ovmf/OVMF.fd",
    )
    UEFI_VARS_CANDIDATES = (
        "/usr/share/OVMF/OVMF_VARS_4M.fd",
        "/usr/share/OVMF/OVMF_VARS.fd",
    )

    def __init__(self, uri: str = "qemu:///system") -> None:
        self.uri = uri

    def _connect(self):
        try:
            import libvirt  # type: ignore
        except Exception as exc:
            raise VirtualizationError(
                "libvirt Python bindings are required for VM lifecycle management"
            ) from exc

        conn = libvirt.open(self.uri)
        if conn is None:
            raise VirtualizationError(f"Failed to connect to hypervisor at {self.uri}")
        return conn

    def ensure_domain(self, vm: VMDefinition, replace_existing: bool = False) -> None:
        conn = self._connect()
        try:
            try:
                domain = conn.lookupByName(vm.name)
                if replace_existing:
                    if domain.isActive() == 1:
                        domain.destroy()
                    self._undefine_domain(domain)
                    if vm.disk_path:
                        disk = Path(vm.disk_path)
                        if disk.exists():
                            disk.unlink()
                else:
                    return
            except Exception:
                pass

            if not vm.domain_xml.strip():
                if vm.kickstart_path.strip() and vm.disk_path.strip():
                    self._install_domain_with_virt_install(vm)
                    return
                raise VirtualizationError(
                    "Provide domain_xml or kickstart_path+disk_path for new domains"
                )
            conn.defineXML(vm.domain_xml)
        finally:
            conn.close()

    def domain_exists(self, name: str) -> bool:
        conn = self._connect()
        try:
            try:
                conn.lookupByName(name)
                return True
            except Exception:
                return False
        finally:
            conn.close()

    def domain_is_active(self, name: str) -> bool:
        conn = self._connect()
        try:
            try:
                domain = conn.lookupByName(name)
            except Exception:
                return False
            return domain.isActive() == 1
        finally:
            conn.close()

    def current_ipv4(
        self,
        *,
        domain_name: str,
        network_name: str,
        mac_address: str = "",
    ) -> Optional[str]:
        conn = self._connect()
        try:
            network = conn.networkLookupByName(network_name)
            mac_address = (mac_address or "").strip().lower()
            try:
                leases = network.DHCPLeases() or []
            except Exception:
                leases = []

            for lease in leases:
                if not isinstance(lease, dict):
                    continue
                hostname = (lease.get("hostname") or "").strip()
                ipaddr = lease.get("ipaddr")
                lease_mac = (lease.get("mac") or lease.get("macaddr") or "").strip().lower()
                if mac_address and lease_mac == mac_address and ipaddr:
                    return ipaddr
                if hostname == domain_name and ipaddr:
                    return ipaddr
            return None
        finally:
            conn.close()

    def remove_domain(self, *, name: str, disk_path: str = "") -> None:
        conn = self._connect()
        try:
            try:
                domain = conn.lookupByName(name)
                if domain.isActive() == 1:
                    domain.destroy()
                self._undefine_domain(domain)
            except Exception:
                pass
        finally:
            conn.close()

        if disk_path:
            disk = Path(disk_path)
            if disk.exists():
                disk.unlink()

    def _undefine_domain(self, domain) -> None:
        try:
            domain.undefine()
            return
        except Exception as exc:
            # UEFI guests require undefine flags to remove associated NVRAM.
            message = str(exc).lower()
            if "nvram" not in message and "managed save" not in message and "snapshot" not in message:
                raise

        try:
            import libvirt  # type: ignore
        except Exception as exc:
            raise VirtualizationError("libvirt Python bindings are required for VM lifecycle management") from exc

        flags = 0
        flags |= int(getattr(libvirt, "VIR_DOMAIN_UNDEFINE_NVRAM", 0))
        flags |= int(getattr(libvirt, "VIR_DOMAIN_UNDEFINE_MANAGED_SAVE", 0))
        flags |= int(getattr(libvirt, "VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA", 0))

        if not hasattr(domain, "undefineFlags") or flags == 0:
            raise VirtualizationError("Domain requires flagged undefine (NVRAM/managed state), but libvirt flags are unavailable")

        domain.undefineFlags(flags)

    def _install_domain_with_virt_install(self, vm: VMDefinition) -> None:
        if shutil.which("virt-install") is None:
            raise VirtualizationError("virt-install is required to create domains from kickstart")

        disk_path = Path(vm.disk_path)
        disk_path.parent.mkdir(parents=True, exist_ok=True)

        if not disk_path.exists():
            self._create_qcow2_disk(disk_path=disk_path, size_gib=vm.disk_gib)

        if vm.boot_mode == "uefi":
            code_path, vars_path = self._resolve_uefi_firmware()
            boot_arg = (
                f"loader={code_path},"
                "loader.readonly=yes,"
                "loader.type=pflash,"
                "loader.secure=no,"
                f"nvram.template={vars_path}"
            )
        else:
            boot_arg = ""

        cmd = [
            "virt-install",
            "--name",
            vm.name,
            "--osinfo",
            "linux2024",
            "--memory",
            str(vm.memory_mib),
            "--vcpus",
            str(vm.vcpus),
            "--disk",
            f"path={disk_path},format=qcow2,bus=virtio",
            "--network",
            f"network={vm.network_name},model=virtio{',mac=' + vm.mac_address if vm.mac_address else ''}",
            "--graphics",
            "none",
            "--console",
            "pty,target_type=serial",
            "--pxe",
            "--noautoconsole",
            "--wait",
            "0",
        ]
        if boot_arg:
            cmd.extend(["--boot", boot_arg])
        env = dict(os.environ)
        env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        if proc.returncode != 0:
            raise VirtualizationError(
                f"virt-install failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

    def _resolve_uefi_firmware(self) -> tuple[str, str]:
        code_path = next((path for path in self.UEFI_CODE_CANDIDATES if Path(path).exists()), "")
        vars_path = next((path for path in self.UEFI_VARS_CANDIDATES if Path(path).exists()), "")
        if not code_path or not vars_path:
            raise VirtualizationError(
                "Could not find a usable generic OVMF firmware pair on the host. "
                "Expected one of: "
                f"code={', '.join(self.UEFI_CODE_CANDIDATES)} vars={', '.join(self.UEFI_VARS_CANDIDATES)}"
            )
        return code_path, vars_path

    def _create_qcow2_disk(self, *, disk_path: Path, size_gib: int) -> None:
        if shutil.which("qemu-img") is None:
            raise VirtualizationError("qemu-img is required to create VM disks")
        cmd = ["qemu-img", "create", "-f", "qcow2", str(disk_path), f"{size_gib}G"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise VirtualizationError(
                f"qemu-img failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )

    def start_domain(self, name: str) -> None:
        conn = self._connect()
        try:
            domain = conn.lookupByName(name)
            if domain.isActive() == 0:
                domain.create()
        finally:
            conn.close()

    def stop_domain(self, name: str) -> None:
        conn = self._connect()
        try:
            domain = conn.lookupByName(name)
            if domain.isActive() == 1:
                domain.shutdown()
        finally:
            conn.close()

    def shutdown_and_wait(self, name: str, timeout_seconds: int = 180) -> None:
        conn = self._connect()
        try:
            domain = conn.lookupByName(name)
            if domain.isActive() == 0:
                return

            domain.shutdown()
            end = time.time() + timeout_seconds
            while time.time() < end:
                if domain.isActive() == 0:
                    return
                time.sleep(2)

            domain.destroy()
        finally:
            conn.close()

    def wait_for_ipv4(
        self,
        domain_name: str,
        network_name: str,
        timeout_seconds: int = 1200,
        mac_address: str = "",
    ) -> Optional[str]:
        conn = self._connect()
        try:
            network = conn.networkLookupByName(network_name)
            end = time.time() + timeout_seconds
            mac_address = (mac_address or "").strip().lower()
            while time.time() < end:
                try:
                    leases = network.DHCPLeases() or []
                except Exception:
                    leases = []

                for lease in leases:
                    if not isinstance(lease, dict):
                        continue
                    hostname = (lease.get("hostname") or "").strip()
                    ipaddr = lease.get("ipaddr")
                    lease_mac = (lease.get("mac") or lease.get("macaddr") or "").strip().lower()
                    if mac_address and lease_mac == mac_address and ipaddr:
                        return ipaddr
                    if hostname == domain_name and ipaddr:
                        return ipaddr
                time.sleep(5)
            return None
        finally:
            conn.close()
