from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from textwrap import dedent
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from django.conf import settings
from apps.serverconfig.models import ServerConfiguration
from .ssh_keys import SSHKeyError, generate_ssh_keypair_text


class BuilderError(RuntimeError):
	pass


@dataclass
class BuilderVMDefinition:
	name: str
	memory_mib: int
	vcpus: int
	disk_gib: int
	base_image_path: str
	shared_iso_dir: str
	network_name: str
	network_gateway: str = ""
	static_ipv4: str = ""
	network_prefix: int = 24
	nic_mac: str = ""
	disk_path: str = ""
	domain_xml: str = ""


@dataclass
class BuilderAccessKeyPair:
	private_key_text: str
	public_key: str
	private_key_path: Path

	def cleanup_private(self) -> None:
		if self.private_key_path.exists():
			self.private_key_path.unlink()


class BuilderVMManager:
	@staticmethod
	def _progress(progress_cb: Callable[[str, str], None] | None, stage: str, message: str) -> None:
		if progress_cb is not None:
			progress_cb(stage, message)

	def __init__(self, uri: str | None = None) -> None:
		server_cfg = ServerConfiguration.get_effective()
		configured_base_image = server_cfg.builder_base_image_path if server_cfg else ""
		self.uri = uri or getattr(settings, "BUILDER_HYPERVISOR_URI", "qemu:///system")
		self.definition = BuilderVMDefinition(
			name=getattr(settings, "BUILDER_VM_NAME", "tuxwsmaker-builder"),
			memory_mib=getattr(settings, "BUILDER_VM_MEMORY_MIB", 8192),
			vcpus=getattr(settings, "BUILDER_VM_VCPUS", 4),
			disk_gib=getattr(settings, "BUILDER_VM_DISK_GIB", 80),
			base_image_path=configured_base_image or getattr(settings, "BUILDER_BASE_IMAGE_PATH", ""),
			shared_iso_dir=str(getattr(settings, "BUILDER_SHARED_ISO_ROOT", Path(settings.ISO_UPLOAD_ROOT))),
			network_name=getattr(settings, "BUILDER_LIBVIRT_NETWORK", "wsbuildnet"),
			network_gateway=str(getattr(settings, "BUILDER_LIBVIRT_NETWORK_GATEWAY", "192.168.200.1")),
			static_ipv4=str(getattr(settings, "BUILDER_VM_STATIC_IPV4", "192.168.200.10")),
			network_prefix=int(getattr(settings, "BUILDER_VM_STATIC_PREFIX", 24)),
			nic_mac=str(getattr(settings, "BUILDER_VM_NIC_MAC", "52:54:00:c8:00:0a")),
			disk_path=str(getattr(settings, "BUILDER_VM_DISK_PATH", Path(getattr(settings, "BUILDER_STORAGE_POOL_PATH", Path("/var/lib/libvirt/images"))) / f"{getattr(settings, 'BUILDER_VM_NAME', 'tuxwsmaker-builder')}.qcow2")),
		)

	def _connect(self):
		try:
			import libvirt  # type: ignore
		except Exception as exc:
			raise BuilderError("libvirt Python bindings are required for builder VM management") from exc

		conn = libvirt.open(self.uri)
		if conn is None:
			raise BuilderError(f"Failed to connect to hypervisor at {self.uri}")
		return conn

	def ensure_access_keypair(self, *, force_new: bool = False):
		cfg = ServerConfiguration.get_effective()
		private_key_text = cfg.get_builder_ssh_private_key()
		public_key = cfg.get_builder_ssh_public_key()
		if force_new or not private_key_text or not public_key:
			try:
				private_key_text, public_key = generate_ssh_keypair_text(comment="tuxwsmaker-builder-vm")
			except Exception as exc:
				raise BuilderError(f"Failed to generate builder SSH keypair: {exc}") from exc
			cfg.set_builder_ssh_keypair(private_key=private_key_text, public_key=public_key)
			cfg.save()

		with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as key_file:
			key_file.write(private_key_text + "\n")
			private_key_path = Path(key_file.name)

		private_key_path.chmod(0o600)
		return BuilderAccessKeyPair(
			private_key_text=private_key_text,
			public_key=public_key,
			private_key_path=private_key_path,
		)

	def builder_vm_exists(self) -> bool:
		# Avoid noisy libvirt "Domain not found" logs by checking with virsh first.
		via_virsh = self._builder_vm_exists_via_virsh()
		if via_virsh is not None:
			return via_virsh
		try:
			conn = self._connect()
			try:
				conn.lookupByName(self.definition.name)
				return True
			finally:
				conn.close()
		except Exception:
			return False

	def _builder_vm_exists_via_virsh(self) -> bool | None:
		env = dict(os.environ)
		env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
		cmd = ["virsh", "--connect", self.uri, "dominfo", self.definition.name]
		try:
			proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
		except FileNotFoundError:
			return None
		if proc.returncode == 0:
			return True

		combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
		if "no domain with matching name" in combined or "domain not found" in combined:
			return False

		# For connection/auth failures, fall back to libvirt API lookup.
		return None

	def remove_builder_vm(self, progress_cb: Callable[[str, str], None] | None = None) -> None:
		self._progress(progress_cb, "cleanup", "Removing existing builder VM and disk")
		disk_path = Path(self.definition.disk_path)
		conn = self._connect()
		try:
			try:
				domain = conn.lookupByName(self.definition.name)
				if domain.isActive() == 1:
					self._progress(progress_cb, "cleanup", "Stopping running builder VM")
					domain.destroy()
				self._progress(progress_cb, "cleanup", "Undefining builder VM")
				domain.undefine()
			except Exception:
				pass
		finally:
			conn.close()

		if disk_path.exists():
			self._progress(progress_cb, "cleanup", f"Deleting builder disk {disk_path}")
			disk_path.unlink()

	def recreate_builder_vm(self, progress_cb: Callable[[str, str], None] | None = None) -> None:
		self.remove_builder_vm(progress_cb=progress_cb)
		self.ensure_builder_vm(progress_cb=progress_cb)

	def ensure_builder_vm(self, progress_cb: Callable[[str, str], None] | None = None) -> None:
		self._progress(progress_cb, "network", "Ensuring builder libvirt network")
		self._ensure_builder_network()
		self._progress(progress_cb, "storage", "Ensuring default libvirt storage pool")
		self._ensure_default_storage_pool()
		shared_iso_dir = Path(self.definition.shared_iso_dir)
		shared_iso_dir.mkdir(parents=True, exist_ok=True)

		if not self.definition.base_image_path:
			return

		disk_path = Path(self.definition.disk_path)
		disk_path.parent.mkdir(parents=True, exist_ok=True)
		self._progress(progress_cb, "ssh", "Ensuring builder access SSH keypair")
		key_pair = self.ensure_access_keypair()
		if not disk_path.exists():
			self._progress(progress_cb, "disk", f"Creating overlay disk at {disk_path}")
			self._create_overlay_disk(disk_path=disk_path)
			self._progress(progress_cb, "disk", "Injecting SSH key into builder disk")
			self._inject_ssh_key(disk_path=disk_path, public_key=key_pair.public_key)
			self._progress(progress_cb, "network", "Injecting static network configuration")
			self._inject_static_network_config(disk_path=disk_path)

		conn = self._connect()
		try:
			self._ensure_default_storage_pool(conn=conn)
			try:
				domain = conn.lookupByName(self.definition.name)
				if domain.isActive() == 0:
					self._progress(progress_cb, "vm", "Starting existing builder VM")
					domain.create()
				else:
					self._progress(progress_cb, "vm", "Builder VM already exists and is running")
				return
			except Exception:
				pass

			if shutil.which("virt-install") is None:
				raise BuilderError("virt-install is required to create the builder VM")

			filesystem_arg = self._filesystem_mapping_arg(shared_iso_dir)
			self._progress(progress_cb, "vm", "Creating builder VM with virt-install")

			cmd = [
				"virt-install",
				"--name",
				self.definition.name,
				"--osinfo",
				"rhel10-unknown",
				"--memory",
				str(self.definition.memory_mib),
				"--memorybacking",
				"source.type=memfd,access.mode=shared",
				"--vcpus",
				str(self.definition.vcpus),
				"--import",
				"--disk",
				f"path={disk_path},format=qcow2,bus=virtio",
				"--network",
				f"network={self.definition.network_name},model=virtio,mac={self.definition.nic_mac}",
				"--filesystem",
				filesystem_arg,
				"--graphics",
				"none",
				"--console",
				"pty,target_type=serial",
				"--noautoconsole",
			]
			env = dict(os.environ)
			env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
			proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
			if proc.returncode != 0:
				error_text = (proc.stderr or proc.stdout or "").strip()
				if "ModuleNotFoundError: No module named 'gi'" in error_text:
					raise BuilderError(
						"virt-install failed because Python GI bindings are missing. Install system packages providing gi/virt-install (for example: python3-gi, gir1.2-libosinfo-1.0, virt-install/virt-manager), then retry."
					)
				raise BuilderError(
					f"virt-install failed ({proc.returncode}): {error_text}"
				)
			self._progress(progress_cb, "vm", "Builder VM created successfully")
		finally:
			conn.close()

	def start_builder_vm(self, progress_cb: Callable[[str, str], None] | None = None) -> None:
		conn = self._connect()
		try:
			domain = conn.lookupByName(self.definition.name)
			if domain.isActive() == 0:
				self._progress(progress_cb, "vm", "Starting existing builder VM")
				domain.create()
			else:
				self._progress(progress_cb, "vm", "Builder VM already exists and is running")
		finally:
			conn.close()

	def builder_vm_running(self) -> bool:
		conn = self._connect()
		try:
			domain = conn.lookupByName(self.definition.name)
			return domain.isActive() == 1
		except Exception:
			return False
		finally:
			conn.close()

	def _filesystem_mapping_arg(self, shared_iso_dir: Path) -> str:
		virtiofsd_bin = shutil.which("virtiofsd")
		if virtiofsd_bin is None:
			for candidate in ("/usr/libexec/virtiofsd", "/usr/lib/qemu/virtiofsd"):
				if Path(candidate).exists():
					virtiofsd_bin = candidate
					break
		if virtiofsd_bin is not None:
			return f"source={shared_iso_dir},target=buildisos,driver.type=virtiofs"
		# Compatibility fallback when host lacks virtiofsd; guest-side auto-mount may be unavailable.
		return f"type=mount,source={shared_iso_dir},target=buildisos,accessmode=mapped"

	def _inject_ssh_key(self, *, disk_path: Path, public_key: str) -> None:
		if shutil.which("virt-customize") is None:
			raise BuilderError("virt-customize is required to inject SSH keys into the builder VM disk")
		if shutil.which("supermin") is None:
			raise BuilderError("supermin is required by virt-customize. Install guestfs-tools/supermin on the host")

		with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as key_file:
			key_file.write(public_key + "\n")
			key_file_path = Path(key_file.name)

		try:
			cmd = [
				"virt-customize",
				"-a",
				str(disk_path),
				"--ssh-inject",
				f"root:file:{key_file_path}",
				"--selinux-relabel",
			]
			self._run_virt_customize(cmd, "SSH key inject")
		finally:
			key_file_path.unlink(missing_ok=True)

	def _inject_static_network_config(self, *, disk_path: Path) -> None:
		if shutil.which("virt-customize") is None:
			raise BuilderError("virt-customize is required to inject static network settings into the builder VM disk")
		if shutil.which("supermin") is None:
			raise BuilderError("supermin is required by virt-customize. Install guestfs-tools/supermin on the host")

		nmconnection = dedent(
			f"""
			[connection]
			id=tuxwsmaker-static
			type=ethernet
			autoconnect=true

			[ethernet]
			mac-address={self.definition.nic_mac}

			[ipv4]
			method=manual
			address1={self.definition.static_ipv4}/{self.definition.network_prefix},{self.definition.network_gateway}
			dns={self.definition.network_gateway};1.1.1.1;

			[ipv6]
			method=ignore
			"""
		).strip() + "\n"

		with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as cfg_file:
			cfg_file.write(nmconnection)
			cfg_file_path = Path(cfg_file.name)

		try:
			cmd = [
				"virt-customize",
				"-a",
				str(disk_path),
				"--upload",
				f"{cfg_file_path}:/etc/NetworkManager/system-connections/tuxwsmaker-static.nmconnection",
				"--run-command",
				"chmod 600 /etc/NetworkManager/system-connections/tuxwsmaker-static.nmconnection",
				"--run-command",
				"chown root:root /etc/NetworkManager/system-connections/tuxwsmaker-static.nmconnection",
				"--selinux-relabel",
			]
			self._run_virt_customize(cmd, "static network inject")
		finally:
			cfg_file_path.unlink(missing_ok=True)

	def _run_virt_customize(self, cmd: list[str], operation: str) -> None:
		env = dict(os.environ)
		env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
		env.setdefault("LIBGUESTFS_BACKEND", "direct")
		proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
		if proc.returncode != 0:
			error_text = (proc.stderr or proc.stdout or "").strip()
			if "supermin exited with error status 1" in error_text:
				raise BuilderError(
					f"virt-customize {operation} failed: libguestfs/supermin host setup is incomplete. "
					"Install guestfs-tools and supermin on the host, then run libguestfs-test-tool to verify before retrying. "
					f"Raw error: {error_text}"
				)
			raise BuilderError(
				f"virt-customize {operation} failed ({proc.returncode}): {error_text}"
			)

	def _ensure_builder_network(self, conn=None) -> None:
		network_name = self.definition.network_name
		bridge_name = getattr(settings, "BUILDER_LIBVIRT_BRIDGE_NAME", "virbrwsbld")
		network_gateway = getattr(settings, "BUILDER_LIBVIRT_NETWORK_GATEWAY", "192.168.200.1")
		network_netmask = getattr(settings, "BUILDER_LIBVIRT_NETWORK_NETMASK", "255.255.255.0")
		enable_dhcp = bool(getattr(settings, "BUILDER_LIBVIRT_ENABLE_DHCP", False))
		enable_dns = bool(getattr(settings, "BUILDER_LIBVIRT_ENABLE_DNS", False))
		dhcp_start = getattr(settings, "BUILDER_LIBVIRT_DHCP_START", "192.168.200.100")
		dhcp_end = getattr(settings, "BUILDER_LIBVIRT_DHCP_END", "192.168.200.254")

		if conn is None:
			conn = self._connect()
			close_conn = True
		else:
			close_conn = False

		try:
			try:
				network = conn.networkLookupByName(network_name)
				if network.isActive() == 0:
					try:
						network.create()
					except Exception as exc:
						raise self._network_start_error(network_name, network_gateway, exc) from exc
				try:
					network.setAutostart(True)
				except Exception as exc:
					raise BuilderError(f"Failed to set autostart for libvirt network {network_name}: {exc}") from exc
				return
			except Exception:
				pass

			dhcp_xml = ""
			if enable_dhcp:
				dhcp_xml = dedent(
					f"""
					<dhcp>
					  <range start='{dhcp_start}' end='{dhcp_end}'/>
					</dhcp>
					"""
				).strip()

			dns_xml = ""
			if not enable_dns:
				dns_xml = "<dns enable='no'/>"

			xml = dedent(
				f"""
				<network>
				  <name>{network_name}</name>
				  <forward mode='nat'/>
				  {dns_xml}
				  <bridge name='{bridge_name}' stp='on' delay='0'/>
				  <ip address='{network_gateway}' netmask='{network_netmask}'>
				    {dhcp_xml}
				  </ip>
				</network>
				"""
			).strip()

			try:
				network = conn.networkDefineXML(xml)
			except Exception as exc:
				raise BuilderError(f"Failed to define libvirt network {network_name}: {exc}") from exc
			if network is None:
				raise BuilderError(f"Failed to define libvirt network {network_name}")
			try:
				network.create()
			except Exception as exc:
				raise self._network_start_error(network_name, network_gateway, exc) from exc
			try:
				network.setAutostart(True)
			except Exception as exc:
				raise BuilderError(f"Failed to set autostart for libvirt network {network_name}: {exc}") from exc
		finally:
			if close_conn:
				conn.close()

	def _network_start_error(self, network_name: str, network_gateway: str, exc: Exception) -> BuilderError:
		message = str(exc)
		if "Address already in use" in message:
			return BuilderError(
				f"Failed to start libvirt network {network_name}: gateway address {network_gateway} is already in use. "
				"A host DNS service may already be bound to port 53. Keep BUILDER_LIBVIRT_ENABLE_DNS=False (default), then destroy+undefine wsbuildnet so it is recreated without libvirt dnsmasq."
			)
		return BuilderError(f"Failed to start libvirt network {network_name}: {message}")

	def _ensure_default_storage_pool(self, conn=None) -> None:
		pool_name = getattr(settings, "BUILDER_STORAGE_POOL_NAME", "default")
		pool_path = Path(getattr(settings, "BUILDER_STORAGE_POOL_PATH", "/var/lib/libvirt/images"))
		pool_path.mkdir(parents=True, exist_ok=True)

		if conn is None:
			conn = self._connect()
			close_conn = True
		else:
			close_conn = False

		try:
			try:
				pool = conn.storagePoolLookupByName(pool_name)
				if pool.isActive() == 0:
					pool.create(0)
				pool.setAutostart(True)
				return
			except Exception:
				pass

			xml = dedent(
				f"""
				<pool type='dir'>
				  <name>{pool_name}</name>
				  <target>
				    <path>{pool_path}</path>
				  </target>
				</pool>
				"""
			).strip()
			pool = conn.storagePoolDefineXML(xml)
			if pool is None:
				raise BuilderError(f"Failed to define libvirt storage pool {pool_name}")
			pool.build(0)
			pool.create(0)
			pool.setAutostart(True)
		finally:
			if close_conn:
				conn.close()

	def ensure_iso_shared(self, iso_path: Path) -> Path:
		shared_iso_dir = Path(self.definition.shared_iso_dir).resolve()
		shared_iso_dir.mkdir(parents=True, exist_ok=True)
		resolved_iso = iso_path.resolve()
		if not resolved_iso.exists():
			raise BuilderError(f"ISO not found: {resolved_iso}")

		try:
			resolved_iso.relative_to(shared_iso_dir)
		except ValueError as exc:
			raise BuilderError(
				f"ISO is outside mapped host ISO folder ({shared_iso_dir}): {resolved_iso}"
			) from exc
		return resolved_iso

	def wait_for_ipv4(self, timeout_seconds: int = 900, progress_cb: Callable[[str, str], None] | None = None) -> str:
		bootstrap_ip = str(getattr(settings, "BUILDER_VM_BOOTSTRAP_IP", "")).strip()
		if bootstrap_ip:
			self._progress(progress_cb, "network", f"Configured builder bootstrap IP is {bootstrap_ip}")
			if self._is_tcp_port_open(bootstrap_ip, 22, timeout_seconds=2):
				self._progress(progress_cb, "network", f"Bootstrap IP {bootstrap_ip} is reachable on SSH")
				return bootstrap_ip
			self._progress(
				progress_cb,
				"network",
				f"Bootstrap IP {bootstrap_ip} is not reachable yet; discovering IP via libvirt lease/ARP",
			)

		conn = self._connect()
		try:
			self._progress(progress_cb, "network", "Waiting for builder VM IPv4 address")
			network = conn.networkLookupByName(self.definition.network_name)
			end = time.time() + timeout_seconds
			while time.time() < end:
				try:
					leases = network.DHCPLeases() or []
				except Exception:
					leases = []

				for lease in leases:
					if not isinstance(lease, dict):
						continue
					hostname = lease.get("hostname") or ""
					ipaddr = lease.get("ipaddr")
					if hostname == self.definition.name and ipaddr:
						return ipaddr

				ip_from_arp = self._lookup_ipv4_via_virsh_arp()
				if ip_from_arp:
					return ip_from_arp
				time.sleep(5)
		finally:
			conn.close()

		extra_hint = ""
		if bootstrap_ip:
			extra_hint = f" Configured bootstrap IP {bootstrap_ip} was not reachable and no dynamic address was discovered."
		raise BuilderError(
			f"Timed out waiting for IPv4 address for builder VM {self.definition.name} on network {self.definition.network_name}.{extra_hint}"
		)

	def _is_tcp_port_open(self, host: str, port: int, timeout_seconds: float = 2.0) -> bool:
		try:
			with socket.create_connection((host, port), timeout=timeout_seconds):
				return True
		except OSError:
			return False

	def _lookup_ipv4_via_virsh_arp(self) -> str:
		env = dict(os.environ)
		env["PATH"] = f"/usr/bin:/bin:{env.get('PATH', '')}"
		cmd = [
			"virsh",
			"--connect",
			self.uri,
			"domifaddr",
			self.definition.name,
			"--source",
			"arp",
		]
		proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
		if proc.returncode != 0:
			return ""

		for line in (proc.stdout or "").splitlines():
			if "/" not in line:
				continue
			parts = line.split()
			for part in parts:
				if "/" not in part:
					continue
				ip = part.split("/", 1)[0].strip()
				if ip and ":" not in ip:
					return ip
		return ""

	def provision_builder_vm(
		self,
		*,
		rhn_username: str,
		rhn_password: str,
		use_redhat_subscription: bool = True,
		progress_cb: Callable[[str, str], None] | None = None,
	) -> str:
		self._progress(progress_cb, "provision", "Starting builder VM provisioning")
		key_pair = self.ensure_access_keypair()
		ip_address = self.wait_for_ipv4(timeout_seconds=900, progress_cb=progress_cb)
		ssh_user = getattr(settings, "BUILDER_VM_SSH_USER", "root")

		def run_remote(command: str, *, timeout_seconds: int = 600) -> None:
			remote_command = f"bash -lc {shlex.quote(command)}"
			ssh_cmd = [
				"ssh",
				"-o",
				"BatchMode=yes",
				"-o",
				"StrictHostKeyChecking=no",
				"-o",
				"UserKnownHostsFile=/dev/null",
				"-i",
				str(key_pair.private_key_path),
				f"{ssh_user}@{ip_address}",
				remote_command,
			]
			proc = subprocess.run(
				ssh_cmd,
				capture_output=True,
				text=True,
				check=False,
				timeout=timeout_seconds,
			)
			if proc.returncode != 0:
				raise BuilderError(
					f"Builder provisioning step failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
				)

		wait_end = time.time() + 600
		self._progress(progress_cb, "ssh", f"Waiting for SSH readiness on {ip_address}")
		while time.time() < wait_end:
			try:
				run_remote("true", timeout_seconds=20)
				break
			except BuilderError:
				time.sleep(5)
		else:
			raise BuilderError(f"Timed out waiting for SSH readiness on builder VM {ip_address}")

		self._progress(progress_cb, "mount", "Mounting shared ISO directory inside builder VM")
		try:
			run_remote(
				"mkdir -p /mnt/buildisos /usr/local/sbin /etc/systemd/system && "
				"cat > /usr/local/sbin/mount-buildisos.sh <<'EOF'\n"
				"#!/usr/bin/env bash\n"
				"set -euo pipefail\n"
				"mkdir -p /mnt/buildisos\n"
				"if mountpoint -q /mnt/buildisos; then exit 0; fi\n"
				"if mount -t fuse.buildisos buildisos /mnt/buildisos 2>/dev/null; then exit 0; fi\n"
				"if mount -t virtiofs buildisos /mnt/buildisos 2>/dev/null; then exit 0; fi\n"
				"echo 'buildisos mount is unavailable on this guest/host combination' >&2\n"
				"exit 0\n"
				"EOF\n"
				"chmod 0755 /usr/local/sbin/mount-buildisos.sh && "
				"cat > /etc/systemd/system/buildisos-mount.service <<'EOF'\n"
				"[Unit]\n"
				"Description=Mount shared builder ISO directory\n"
				"After=multi-user.target\n"
				"\n"
				"[Service]\n"
				"Type=oneshot\n"
				"ExecStart=/usr/local/sbin/mount-buildisos.sh\n"
				"RemainAfterExit=yes\n"
				"\n"
				"[Install]\n"
				"WantedBy=multi-user.target\n"
				"EOF\n"
				"systemctl daemon-reload && systemctl enable --now buildisos-mount.service"
			)
		except BuilderError as exc:
			self._progress(
				progress_cb,
				"mount-warning",
				f"Shared ISO auto-mount not available on this host setup ({exc}). Provisioning will continue; PXE assets are prepared on the host.",
			)

		if use_redhat_subscription:
			quoted_user = shlex.quote(rhn_username)
			quoted_password = shlex.quote(rhn_password)

			self._progress(progress_cb, "rhn", "Ensuring subscription-manager is installed")
			run_remote(
				"if ! command -v subscription-manager >/dev/null 2>&1; then "
				"dnf -y install subscription-manager; "
				"fi"
			)
			self._progress(progress_cb, "rhn", "Registering builder VM with RHN if needed")
			run_remote(
				"if ! subscription-manager identity >/dev/null 2>&1; then "
				f"subscription-manager register --username {quoted_user} --password {quoted_password} --force; "
				"fi"
			)
		else:
			self._progress(
				progress_cb,
				"rhn",
				"Skipping subscription-manager registration (Use Red Hat subscription is disabled)",
			)
		self._progress(progress_cb, "packages", "Installing required builder packages and services")
		run_remote(
			"dnf -y install "
			"qemu-kvm libvirt virt-install python3-libvirt python3-lxml tftp-server httpd libvirt-nss dnsmasq qemu-img firewalld "
			"syslinux-tftpboot grub2-efi-x64 shim-x64"
		)
		self._progress(progress_cb, "packages", "Configuring dnsmasq for builder DHCP and DNS")
		run_remote(
			"iface=$(ip -o -4 addr show | awk '$4 ~ /^192\\.168\\.200\\.10\\// {print $2; exit}'); "
			"if [ -z \"$iface\" ]; then iface=$(ip route get 192.168.200.10 | awk 'BEGIN {for (i=1; i<=NF; i++) if ($i == \"dev\") {print $(i+1); exit}}'); fi; "
			"mkdir -p /var/lib/tftpboot/pxelinux.cfg /var/lib/tftpboot/builds /var/www/html/kickstarts; "
			"pxe_src=; if [ -f /tftpboot/pxelinux.0 ]; then pxe_src=/tftpboot/pxelinux.0; fi; "
			"if [ -z \"$pxe_src\" ]; then pxe_src=$(find /usr/share /usr/lib /tftpboot -maxdepth 6 -type f -name pxelinux.0 2>/dev/null | head -n 1); fi; "
			"if [ -z \"$pxe_src\" ]; then dnf -y install syslinux syslinux-tftpboot >/dev/null 2>&1 || true; fi; "
			"if [ -z \"$pxe_src\" ]; then pxe_src=$(find /usr/share /usr/lib /tftpboot -maxdepth 6 -type f -name pxelinux.0 2>/dev/null | head -n 1); fi; "
			"if [ -n \"$pxe_src\" ]; then cp -f \"$pxe_src\" /var/lib/tftpboot/pxelinux.0; fi; "
			"for mod in ldlinux.c32 libcom32.c32 libutil.c32; do "
			"  mod_src=; if [ -f /tftpboot/$mod ]; then mod_src=/tftpboot/$mod; fi; "
			"  if [ -z \"$mod_src\" ]; then mod_src=$(find /usr/share /usr/lib /tftpboot -maxdepth 7 -type f -name \"$mod\" 2>/dev/null | head -n 1); fi; "
			"  if [ -n \"$mod_src\" ]; then cp -f \"$mod_src\" /var/lib/tftpboot/; fi; "
			"done; "
			"mkdir -p /var/lib/tftpboot/efi64 /var/lib/tftpboot/efi32; "
			"efi64_src=; "
			"for candidate in /tftpboot/efi64/grubnetx64.efi /usr/lib/grub/x86_64-efi/grubnetx64.efi /usr/share/grub/x86_64-efi/grubnetx64.efi /boot/efi/EFI/redhat/grubx64.efi /usr/lib/grub/x86_64-efi/grubx64.efi /usr/share/grub/x86_64-efi/grubx64.efi /tftpboot/efi64/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI; do "
			"  if [ -f \"$candidate\" ]; then efi64_src=$candidate; break; fi; "
			"done; "
			"if [ -n \"$efi64_src\" ]; then cp -f \"$efi64_src\" /var/lib/tftpboot/efi64/grubx64.efi; fi; "
			"if [ -f /tftpboot/efi32/grubia32.efi ]; then cp -f /tftpboot/efi32/grubia32.efi /var/lib/tftpboot/efi32/grubia32.efi; fi; "
			"if [ ! -f /var/lib/tftpboot/efi64/grubx64.efi ]; then "
			"  efi64_src=$(find /usr/lib /usr/share /boot /tftpboot -maxdepth 8 -type f \\( -name grubnetx64.efi -o -name grubx64.efi -o -name BOOTX64.EFI -o -name bootx64.efi \\) 2>/dev/null | grep -E '/(grubnetx64\\.efi|grubx64\\.efi|BOOTX64\\.EFI|bootx64\\.efi)$' | head -n 1); "
			"  if [ -n \"$efi64_src\" ]; then cp -f \"$efi64_src\" /var/lib/tftpboot/efi64/grubx64.efi; fi; "
			"fi; "
			"if [ ! -f /var/lib/tftpboot/efi32/grubia32.efi ]; then "
			"  efi32_src=$(find /usr/lib /usr/share /boot /tftpboot -maxdepth 8 -type f \\( -name grubia32.efi -o -name BOOTIA32.EFI -o -name bootia32.efi \\) 2>/dev/null | head -n 1); "
			"  if [ -n \"$efi32_src\" ]; then cp -f \"$efi32_src\" /var/lib/tftpboot/efi32/grubia32.efi; fi; "
			"fi; "
			"if [ ! -f /var/lib/tftpboot/grubx64.efi ] && [ -f /var/lib/tftpboot/efi64/grubx64.efi ]; then ln -sfn efi64/grubx64.efi /var/lib/tftpboot/grubx64.efi; fi; "
			"if [ ! -f /var/lib/tftpboot/grubia32.efi ] && [ -f /var/lib/tftpboot/efi32/grubia32.efi ]; then ln -sfn efi32/grubia32.efi /var/lib/tftpboot/grubia32.efi; fi; "
			"if [ -f /var/lib/tftpboot/efi64/grubx64.efi ]; then chmod 0644 /var/lib/tftpboot/efi64/grubx64.efi; fi; "
			"if [ -f /var/lib/tftpboot/efi32/grubia32.efi ]; then chmod 0644 /var/lib/tftpboot/efi32/grubia32.efi; fi; "
			"if [ -f /var/lib/tftpboot/grubx64.efi ]; then chmod 0644 /var/lib/tftpboot/grubx64.efi || true; fi; "
			"if [ -f /var/lib/tftpboot/grubia32.efi ]; then chmod 0644 /var/lib/tftpboot/grubia32.efi || true; fi; "
			"restorecon -RF /var/lib/tftpboot >/dev/null 2>&1 || true; "
			"if [ ! -f /var/lib/tftpboot/efi64/grubx64.efi ]; then echo 'grubx64.efi is missing under /var/lib/tftpboot/efi64 after package setup' >&2; exit 1; fi; "
			"if [ ! -f /var/lib/tftpboot/pxelinux.0 ]; then echo 'pxelinux.0 is missing under /var/lib/tftpboot after package setup' >&2; exit 1; fi; "
			"cat > /etc/dnsmasq.d/wsbuildnet.conf <<EOF\n"
			"interface=$iface\n"
			"bind-interfaces\n"
			"domain=wsbuildnet\n"
			"expand-hosts\n"
			"local=/wsbuildnet/\n"
			"enable-tftp\n"
			"tftp-root=/var/lib/tftpboot\n"
			"dhcp-match=set:efi64,option:client-arch,7\n"
			"dhcp-match=set:efi32,option:client-arch,6\n"
				"log-dhcp\n"
				"log-queries\n"
			"dhcp-boot=tag:efi64,efi64/grubx64.efi\n"
			"dhcp-boot=tag:efi32,efi32/grubia32.efi\n"
			"dhcp-boot=pxelinux.0\n"
			"dhcp-authoritative\n"
			"dhcp-range=192.168.200.100,192.168.200.254,255.255.255.0,12h\n"
			"dhcp-option=option:router,192.168.200.1\n"
			"dhcp-option=option:dns-server,192.168.200.10\n"
			"EOF\n"
			"chmod 644 /etc/dnsmasq.d/wsbuildnet.conf"
		)
		self._progress(progress_cb, "services", "Enabling and starting core services")
		run_remote("systemctl enable --now libvirtd firewalld httpd dnsmasq tftp.socket")
		run_remote("systemctl restart dnsmasq")
		run_remote("firewall-cmd --permanent --add-service=http || true")
		run_remote("firewall-cmd --permanent --add-service=tftp || true")
		run_remote("firewall-cmd --permanent --add-service=dns || true")
		run_remote("firewall-cmd --permanent --add-service=dhcp || true")
		run_remote("firewall-cmd --reload || true")
		self._progress(progress_cb, "done", "Builder provisioning completed")

		return f"{ip_address} (ssh key: {key_pair.private_key_path})"

	def _create_overlay_disk(self, *, disk_path: Path) -> None:
		if shutil.which("qemu-img") is None:
			raise BuilderError("qemu-img is required to create the builder VM disk")

		base_image = Path(self.definition.base_image_path)
		if not base_image.exists():
			raise BuilderError(f"Builder base image not found: {base_image}")

		cmd = [
			"qemu-img",
			"create",
			"-f",
			"qcow2",
			"-b",
			str(base_image),
			"-F",
			"qcow2",
			str(disk_path),
		]
		proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
		if proc.returncode != 0:
			raise BuilderError(
				f"qemu-img overlay failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
			)
