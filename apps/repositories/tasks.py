from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone

from celery import shared_task
from django.conf import settings
from django.db import transaction

from apps.builds.services.builder import BuilderVMManager
from apps.builds.services.provisioning import AnsibleProvisioner
from apps.serverconfig.models import ServerConfiguration

from .models import RedHatRepositoryCatalog


_REPO_LINE_RE = re.compile(r"^(?P<repo_id>\S+)\s+(?P<status>enabled|disabled|status)\s*(?P<name>.*)$", re.IGNORECASE)
_REPO_TABLE_LINE_RE = re.compile(r"^(?P<repo_id>\S+)\s+(?P<name>.+?)\s+(?P<status>enabled|disabled|status)$", re.IGNORECASE)
_KV_LINE_RE = re.compile(r"^(?P<key>Repo-[^:]+)\s*:\s*(?P<value>.*)$")


def _parse_repolist_output(raw: str) -> list[dict[str, object]]:
    repos: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        repo_id = str(current.get("repo_id") or "").strip()
        if not repo_id:
            current = None
            return
        repos.append(
            {
                "repo_id": repo_id,
                "name": str(current.get("name") or "").strip(),
                "enabled_by_default": bool(current.get("enabled_by_default")),
                "source_type": str(current.get("source_type") or RedHatRepositoryCatalog.SOURCE_BASEURL),
                "source_url": str(current.get("source_url") or "").strip(),
            }
        )
        current = None

    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            flush_current()
            continue
        low = stripped.lower()
        if low.startswith("repo id") or low.startswith("last metadata") or low.startswith("loaded plugins") or low.startswith("total packages"):
            continue

        kv_match = _KV_LINE_RE.match(stripped)
        if kv_match:
            key = kv_match.group("key").strip().lower()
            value = kv_match.group("value").strip()
            if key == "repo-id":
                flush_current()
                current = {"repo_id": value, "name": "", "enabled_by_default": False}
                continue
            if not current:
                continue
            if key == "repo-name":
                current["name"] = value
            elif key == "repo-status":
                current["enabled_by_default"] = value.lower().startswith("enabled")
            elif key == "repo-baseurl":
                current["source_type"] = RedHatRepositoryCatalog.SOURCE_BASEURL
                current["source_url"] = value.split()[0].strip()
            elif key == "repo-metalink":
                current["source_type"] = RedHatRepositoryCatalog.SOURCE_METALINK
                current["source_url"] = value.split()[0].strip()
            elif key == "repo-mirrorlist":
                current["source_type"] = RedHatRepositoryCatalog.SOURCE_MIRRORLIST
                current["source_url"] = value.split()[0].strip()
            continue

        match = _REPO_LINE_RE.match(stripped)
        if match:
            repo_id = match.group("repo_id").strip()
            if repo_id in {"repolist:", "repo-id"}:
                continue
            status = match.group("status").strip().lower()
            name = match.group("name").strip()
            repos.append(
                {
                    "repo_id": repo_id,
                    "name": name,
                    "enabled_by_default": status == "enabled",
                    "source_type": RedHatRepositoryCatalog.SOURCE_BASEURL,
                    "source_url": "",
                }
            )
            continue

        table_match = _REPO_TABLE_LINE_RE.match(stripped)
        if not table_match:
            continue
        repo_id = table_match.group("repo_id").strip()
        if repo_id in {"repolist:", "repo-id"}:
            continue
        status = table_match.group("status").strip().lower()
        name = table_match.group("name").strip()
        repos.append(
            {
                "repo_id": repo_id,
                "name": name,
                "enabled_by_default": status == "enabled",
                "source_type": RedHatRepositoryCatalog.SOURCE_BASEURL,
                "source_url": "",
            }
        )
    flush_current()
    return repos


def _discover_repos_via_container_rhsm(
    *,
    provisioner: AnsibleProvisioner,
    host: str,
    user: str,
    private_key_path: str,
    rhel_major: int,
    arch: str,
    username: str,
    password: str,
    org_id: str,
    activation_key: str,
) -> list[dict[str, object]]:
    image = f"registry.access.redhat.com/ubi{int(rhel_major)}/ubi:latest"
    env_username = shlex.quote(f"RHSM_USERNAME={username}")
    env_password = shlex.quote(f"RHSM_PASSWORD={password}")
    env_org_id = shlex.quote(f"RHSM_ORG_ID={org_id}")
    env_activation_key = shlex.quote(f"RHSM_ACTIVATION_KEY={activation_key}")

    inner_script = (
        "set -euo pipefail; "
        "cleanup() { "
        "subscription-manager unregister >/dev/null 2>&1 || true; "
        "subscription-manager clean >/dev/null 2>&1 || true; "
        "}; "
        "trap cleanup EXIT; "
        "if ! command -v subscription-manager >/dev/null 2>&1; then "
        "echo 'subscription-manager is not available in container' >&2; exit 20; fi; "
        "if [[ -n \"${RHSM_ORG_ID:-}\" && -n \"${RHSM_ACTIVATION_KEY:-}\" ]]; then "
        "subscription-manager register --force --org \"$RHSM_ORG_ID\" --activationkey \"$RHSM_ACTIVATION_KEY\" >/dev/null; "
        "elif [[ -n \"${RHSM_USERNAME:-}\" && -n \"${RHSM_PASSWORD:-}\" ]]; then "
        "if ! subscription-manager register --force --username \"$RHSM_USERNAME\" --password \"$RHSM_PASSWORD\" >/dev/null 2>&1; then "
        "subscription-manager register --username \"$RHSM_USERNAME\" --password \"$RHSM_PASSWORD\" >/dev/null; "
        "fi; "
        "else "
        "echo 'RHSM_DISCOVERY credentials are not configured' >&2; exit 21; "
        "fi; "
        "subscription-manager release --unset >/dev/null 2>&1 || true; "
        f"subscription-manager release --set={int(rhel_major)} >/dev/null 2>&1 || true; "
        "if command -v dnf >/dev/null 2>&1; then dnf repolist --all; "
        "elif command -v yum >/dev/null 2>&1; then yum repolist all; "
        "else exit 3; fi"
    )

    command = (
        "set -euo pipefail; "
        "runtime=''; "
        "if command -v podman >/dev/null 2>&1; then runtime='podman'; "
        "elif command -v docker >/dev/null 2>&1; then runtime='docker'; fi; "
        "if [[ -z \"$runtime\" ]]; then echo 'no container runtime available' >&2; exit 2; fi; "
        f"$runtime pull -q {shlex.quote(image)} >/dev/null 2>&1 || true; "
        f"$runtime run --rm --platform {shlex.quote(f'linux/{arch}')} "
        f"-e {env_username} -e {env_password} -e {env_org_id} -e {env_activation_key} "
        f"{shlex.quote(image)} sh -lc {shlex.quote(inner_script)}"
    )
    proc = provisioner.run_remote_command(
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"Failed RHSM container discovery for RHEL {rhel_major}")
    return _parse_repolist_output(proc.stdout)


def _discover_repos_on_builder(*, provisioner: AnsibleProvisioner, host: str, user: str, private_key_path: str, rhel_major: int, arch: str) -> list[dict[str, object]]:
    image = f"registry.access.redhat.com/ubi{int(rhel_major)}/ubi:latest"
    command = (
        "set -euo pipefail; "
        "runtime=''; "
        "if command -v podman >/dev/null 2>&1; then runtime='podman'; "
        "elif command -v docker >/dev/null 2>&1; then runtime='docker'; fi; "
        "if [[ -z \"$runtime\" ]]; then echo 'no container runtime available' >&2; exit 2; fi; "
        f"$runtime pull -q {shlex.quote(image)} >/dev/null 2>&1 || true; "
        f"$runtime run --rm --platform {shlex.quote(f'linux/{arch}') } {shlex.quote(image)} sh -lc "
        + shlex.quote(
            "if command -v dnf >/dev/null 2>&1; then dnf repolist --all; "
            "elif command -v yum >/dev/null 2>&1; then yum repolist all; "
            "else exit 3; fi"
        )
    )
    proc = provisioner.run_remote_command(
        host=host,
        user=user,
        private_key_path=private_key_path,
        command=command,
        timeout_seconds=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"Failed to discover repositories for RHEL {rhel_major}")
    return _parse_repolist_output(proc.stdout)


def _summarize_rhsm_discovery_error(exc: Exception) -> str:
    raw_message = str(exc).strip()
    if not raw_message:
        return "RHSM discovery failed"

    lines = [line.strip() for line in raw_message.splitlines() if line.strip()]
    if not lines:
        return "RHSM discovery failed"

    for line in reversed(lines):
        lower = line.lower()
        if "rhsm_discovery credentials are not configured" in lower:
            return "RHSM discovery credentials are not configured in server configuration"
        if "invalid username or password" in lower or "http error code 401" in lower:
            return "Server configuration RHSM credentials were rejected (invalid username or password)"
        if "subscription-manager" in lower or "rhsm" in lower:
            return line

    return lines[-1]


@shared_task(name="repositories.sync_rhsm_repository_catalog")
def sync_rhsm_repository_catalog(*, versions_override: list[int] | None = None) -> dict[str, object]:
    versions_raw = versions_override if versions_override is not None else getattr(settings, "RHSM_DISCOVERY_RHEL_VERSIONS", [8, 9, 10])
    versions: list[int] = []
    for value in versions_raw:
        try:
            version = int(value)
            if version > 0:
                versions.append(version)
        except (TypeError, ValueError):
            continue
    versions = sorted(set(versions))
    if not versions:
        versions = [10]
    arch = str(getattr(settings, "RHSM_DISCOVERY_ARCH", "x86_64") or "x86_64").strip() or "x86_64"
    cfg = ServerConfiguration.get_solo()
    discovery_username = str(cfg.rhn_username or "").strip() or str(getattr(settings, "RHSM_DISCOVERY_USERNAME", "") or "").strip()
    discovery_password = cfg.get_rhn_password() or str(getattr(settings, "RHSM_DISCOVERY_PASSWORD", "") or "").strip()
    discovery_org_id = str(getattr(settings, "RHSM_DISCOVERY_ORG_ID", "") or "").strip()
    discovery_activation_key = str(getattr(settings, "RHSM_DISCOVERY_ACTIVATION_KEY", "") or "").strip()
    has_discovery_creds = bool(
        (discovery_org_id and discovery_activation_key)
        or (discovery_username and discovery_password)
    )

    builder_manager = BuilderVMManager()
    builder_manager.ensure_builder_vm()
    if not builder_manager.builder_vm_running():
        builder_manager.start_builder_vm()
    builder_ip = builder_manager.wait_for_ipv4(timeout_seconds=300)
    builder_access = builder_manager.ensure_access_keypair()
    builder_ssh_user = getattr(settings, "BUILDER_VM_SSH_USER", "root")

    provisioner = AnsibleProvisioner(project_root=getattr(settings, "BASE_DIR"))
    provisioner.wait_for_ssh(
        host=builder_ip,
        user=builder_ssh_user,
        private_key_path=str(builder_access.private_key_path),
        timeout_seconds=300,
    )

    now = datetime.now(timezone.utc)
    discovered_rows: list[tuple[int, dict[str, object]]] = []
    errors: dict[int, str] = {}
    warnings: dict[int, str] = {}
    discovery_mode: dict[int, str] = {}

    for version in versions:
        try:
            try:
                repos = _discover_repos_via_container_rhsm(
                    provisioner=provisioner,
                    host=builder_ip,
                    user=builder_ssh_user,
                    private_key_path=str(builder_access.private_key_path),
                    rhel_major=version,
                    arch=arch,
                    username=discovery_username,
                    password=discovery_password,
                    org_id=discovery_org_id,
                    activation_key=discovery_activation_key,
                )
                discovery_mode[version] = "rhsm"
            except Exception as subman_exc:
                if has_discovery_creds:
                    reason = _summarize_rhsm_discovery_error(subman_exc)
                    raise RuntimeError(f"RHSM discovery failed with server configuration credentials: {reason}") from subman_exc
                repos = _discover_repos_on_builder(
                    provisioner=provisioner,
                    host=builder_ip,
                    user=builder_ssh_user,
                    private_key_path=str(builder_access.private_key_path),
                    rhel_major=version,
                    arch=arch,
                )
                discovery_mode[version] = "ubi-fallback"
                warnings[version] = f"RHSM discovery unavailable; fell back to UBI container repositories: {_summarize_rhsm_discovery_error(subman_exc)}"
            for repo in repos:
                discovered_rows.append((version, repo))
        except Exception as exc:  # pragma: no cover - best effort for periodic sync
            errors[version] = str(exc)

    created = 0
    updated = 0
    with transaction.atomic():
        seen_keys: set[tuple[int, str, str]] = set()
        for version, repo in discovered_rows:
            repo_id = str(repo.get("repo_id") or "").strip()
            if not repo_id:
                continue
            key = (version, arch, repo_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            obj, was_created = RedHatRepositoryCatalog.objects.get_or_create(
                rhel_major=version,
                architecture=arch,
                repo_id=repo_id,
                defaults={
                    "name": str(repo.get("name") or "").strip(),
                    "source_type": str(repo.get("source_type") or RedHatRepositoryCatalog.SOURCE_BASEURL),
                    "source_url": str(repo.get("source_url") or "").strip(),
                    "enabled_by_default": bool(repo.get("enabled_by_default")),
                    "last_synced": now,
                },
            )
            if was_created:
                created += 1
                continue
            changed = False
            new_name = str(repo.get("name") or "").strip()
            if obj.name != new_name:
                obj.name = new_name
                changed = True
            new_source_type = str(repo.get("source_type") or RedHatRepositoryCatalog.SOURCE_BASEURL)
            if obj.source_type != new_source_type:
                obj.source_type = new_source_type
                changed = True
            new_source_url = str(repo.get("source_url") or "").strip()
            if obj.source_url != new_source_url:
                obj.source_url = new_source_url
                changed = True
            new_default = bool(repo.get("enabled_by_default"))
            if obj.enabled_by_default != new_default:
                obj.enabled_by_default = new_default
                changed = True
            obj.last_synced = now
            changed = True
            if changed:
                obj.save(update_fields=["name", "source_type", "source_url", "enabled_by_default", "last_synced", "updated_at"])
                updated += 1

    catalog = list(
        RedHatRepositoryCatalog.objects.filter(rhel_major__in=versions, architecture=arch)
        .order_by("rhel_major", "repo_id")
        .values("id", "rhel_major", "architecture", "repo_id", "name", "enabled_by_default", "source_type", "source_url")
    )

    return {
        "builder": builder_ip,
        "arch": arch,
        "versions": versions,
        "created": created,
        "updated": updated,
        "catalog": catalog,
        "warnings": warnings,
        "errors": errors,
        "discovery_mode": discovery_mode,
    }
