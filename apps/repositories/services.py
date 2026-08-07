from __future__ import annotations

import shlex
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .models import PackageRepository


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _normalize_repo_family(os_or_repo_family: str) -> str:
    value = str(os_or_repo_family or "").strip().lower()
    if value in {PackageRepository.FAMILY_DEB, PackageRepository.FAMILY_RPM}:
        return value
    return {
        "debian": PackageRepository.FAMILY_DEB,
        "rhel": PackageRepository.FAMILY_RPM,
    }.get(value, value)


def _inject_auth_into_url(repo: PackageRepository) -> str:
    base_url = str(repo.base_url or "").strip()
    secret = repo.get_secret()
    if not base_url or not secret:
        return base_url
    parts = urlsplit(base_url)
    if not parts.scheme or repo.auth_type == PackageRepository.AUTH_NONE:
        return base_url

    username = repo.username.strip() if repo.auth_type == PackageRepository.AUTH_BASIC else (repo.username.strip() or "token")
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]
    auth_netloc = f"{username}:{secret}@{netloc}"
    return urlunsplit((parts.scheme, auth_netloc, parts.path, parts.query, parts.fragment))


def _normalize_rpm_source_url(source_url: str) -> str:
    """Normalize common mis-pasted RPM metadata endpoints back to source endpoints."""
    raw = str(source_url or "").strip()
    if not raw:
        return raw

    parts = urlsplit(raw)
    path_lower = parts.path.lower()
    normalized_path = parts.path

    metalink_suffix = "/metalink/repodata/repomd.xml"
    mirrorlist_suffix = "/mirrorlist/repodata/repomd.xml"
    if path_lower.endswith(metalink_suffix):
        normalized_path = parts.path[: -len("/repodata/repomd.xml")]
    elif path_lower.endswith(mirrorlist_suffix):
        normalized_path = parts.path[: -len("/repodata/repomd.xml")]

    if normalized_path != parts.path:
        return urlunsplit((parts.scheme, parts.netloc, normalized_path, parts.query, parts.fragment))
    return raw


def _rpm_source_key_and_value(source_url: str) -> tuple[str, str]:
    normalized = _normalize_rpm_source_url(source_url)
    parts = urlsplit(normalized)
    path_lower = parts.path.lower().rstrip("/")

    query_keys = set(parse_qs(parts.query, keep_blank_values=True).keys())
    if path_lower.endswith("metalink"):
        return "metalink", normalized
    if path_lower.endswith("mirrorlist"):
        return "mirrorlist", normalized
    if "metalink" in query_keys:
        return "metalink", normalized
    if "mirrorlist" in query_keys:
        return "mirrorlist", normalized
    return "baseurl", normalized


def render_repository_preview(repo: PackageRepository) -> str:
    if repo.family == PackageRepository.FAMILY_DEB:
        signed_by = ""
        if repo.signing_mode != PackageRepository.SIGNING_NONE:
            signed_by = f" [signed-by=/etc/apt/keyrings/tuxwsmaker-repo-{repo.id}.asc]"
        return f"deb{signed_by} {_inject_auth_into_url(repo)} {repo.deb_suite} {repo.deb_components}".strip()

    source_key, source_value = _rpm_source_key_and_value(repo.base_url)
    lines = [
        f"[{repo.effective_rpm_repoid()}]",
        f"name={repo.name}",
        f"{source_key}={source_value}",
        f"enabled={'1' if repo.enabled else '0'}",
    ]
    if repo.signing_mode != PackageRepository.SIGNING_NONE:
        lines.append("gpgcheck=1")
        lines.append(f"gpgkey=file:///etc/pki/rpm-gpg/TUXWSMAKER-REPO-{repo.id}")
    else:
        lines.append("gpgcheck=0")
    secret = repo.get_secret()
    if repo.auth_type == PackageRepository.AUTH_BASIC and repo.username and secret:
        lines.append(f"username={repo.username}")
        lines.append(f"password={secret}")
    elif repo.auth_type == PackageRepository.AUTH_TOKEN and secret:
        lines.append(f"password={secret}")
    return "\n".join(lines)


def _matching_repositories(selections, os_family: str) -> list[PackageRepository]:
    repo_family = _normalize_repo_family(os_family)
    matched: list[PackageRepository] = []
    seen: set[int] = set()
    for selection in selections:
        repo = selection.repository if hasattr(selection, "repository") else selection
        if not repo.enabled or repo.family != repo_family or repo.id in seen:
            continue
        matched.append(repo)
        seen.add(repo.id)
    return matched


def render_repository_activation_snippet(*, selections, os_family: str, root_expression: str, phase_label: str) -> str:
    repo_family = _normalize_repo_family(os_family)
    repos = _matching_repositories(selections, os_family)
    if not repos:
        return ""

    lines = [
        f'echo "[repositories] Activating temporary repositories for {phase_label}"',
        f"REPO_ROOT={root_expression}",
        'repo_path() { printf "%s%s" "$REPO_ROOT" "$1"; }',
        'repo_exec() { if [[ -n "$REPO_ROOT" ]]; then chroot "$REPO_ROOT" /usr/bin/env "$@"; else "$@"; fi; }',
    ]

    if repo_family == PackageRepository.FAMILY_DEB:
        lines.append('mkdir -p "$(repo_path /etc/apt/sources.list.d)" "$(repo_path /etc/apt/keyrings)"')
        for repo in repos:
            repo_file = f"/etc/apt/sources.list.d/tuxwsmaker-repo-{repo.id}.list"
            signed_by = ""
            if repo.signing_mode != PackageRepository.SIGNING_NONE:
                signed_by = f" [signed-by=/etc/apt/keyrings/tuxwsmaker-repo-{repo.id}.asc]"
            lines.append(f'cat > "$(repo_path { _shell_quote(repo_file) })" <<\'EOF_REPO_{repo.id}\'')
            lines.append(f"deb{signed_by} {_inject_auth_into_url(repo)} {repo.deb_suite} {repo.deb_components}".strip())
            lines.append(f"EOF_REPO_{repo.id}")
            if repo.signing_mode == PackageRepository.SIGNING_URL and repo.gpg_key_url:
                key_path = f"/etc/apt/keyrings/tuxwsmaker-repo-{repo.id}.asc"
                lines.append(f'curl -fsSL {_shell_quote(repo.gpg_key_url)} -o "$(repo_path { _shell_quote(key_path) })"')
            elif repo.signing_mode == PackageRepository.SIGNING_INLINE and repo.gpg_key_inline:
                key_path = f"/etc/apt/keyrings/tuxwsmaker-repo-{repo.id}.asc"
                lines.append(f'cat > "$(repo_path { _shell_quote(key_path) })" <<\'EOF_KEY_{repo.id}\'')
                lines.append(repo.gpg_key_inline.rstrip())
                lines.append(f"EOF_KEY_{repo.id}")
        lines.append("repo_exec apt-get update")
    else:
        lines.append('mkdir -p "$(repo_path /etc/yum.repos.d)" "$(repo_path /etc/pki/rpm-gpg)"')
        for repo in repos:
            repo_file = f"/etc/yum.repos.d/tuxwsmaker-repo-{repo.id}.repo"
            lines.append(f'cat > "$(repo_path { _shell_quote(repo_file) })" <<\'EOF_REPO_{repo.id}\'')
            lines.append(render_repository_preview(repo))
            lines.append(f"EOF_REPO_{repo.id}")
            if repo.signing_mode == PackageRepository.SIGNING_URL and repo.gpg_key_url:
                key_path = f"/etc/pki/rpm-gpg/TUXWSMAKER-REPO-{repo.id}"
                lines.append(f'curl -fsSL {_shell_quote(repo.gpg_key_url)} -o "$(repo_path { _shell_quote(key_path) })"')
            elif repo.signing_mode == PackageRepository.SIGNING_INLINE and repo.gpg_key_inline:
                key_path = f"/etc/pki/rpm-gpg/TUXWSMAKER-REPO-{repo.id}"
                lines.append(f'cat > "$(repo_path { _shell_quote(key_path) })" <<\'EOF_KEY_{repo.id}\'')
                lines.append(repo.gpg_key_inline.rstrip())
                lines.append(f"EOF_KEY_{repo.id}")
        lines.append(
            'if repo_exec command -v dnf >/dev/null 2>&1; then '
            'repo_exec dnf -y --setopt=skip_if_unavailable=True makecache || '
            'echo "[repositories] warning: dnf makecache failed; continuing"; '
            'else repo_exec yum -y --setopt=skip_if_unavailable=True makecache || '
            'echo "[repositories] warning: yum makecache failed; continuing"; fi'
        )

    return "\n".join(lines) + "\n"


def render_repository_cleanup_snippet(*, selections, os_family: str, root_expression: str, phase_label: str) -> str:
    repo_family = _normalize_repo_family(os_family)
    repos = _matching_repositories(selections, os_family)
    if not repos:
        return ""

    lines = [
        f'echo "[repositories] Cleaning up temporary repositories for {phase_label}"',
        f"REPO_ROOT={root_expression}",
        'repo_path() { printf "%s%s" "$REPO_ROOT" "$1"; }',
        'repo_exec() { if [[ -n "$REPO_ROOT" ]]; then chroot "$REPO_ROOT" /usr/bin/env "$@"; else "$@"; fi; }',
    ]

    for repo in repos:
        if repo_family == PackageRepository.FAMILY_DEB:
            lines.append(f'rm -f "$(repo_path {_shell_quote(f"/etc/apt/sources.list.d/tuxwsmaker-repo-{repo.id}.list")})"')
            if repo.signing_mode != PackageRepository.SIGNING_NONE:
                lines.append(f'rm -f "$(repo_path {_shell_quote(f"/etc/apt/keyrings/tuxwsmaker-repo-{repo.id}.asc")})"')
        else:
            lines.append(f'rm -f "$(repo_path {_shell_quote(f"/etc/yum.repos.d/tuxwsmaker-repo-{repo.id}.repo")})"')
            if repo.signing_mode != PackageRepository.SIGNING_NONE:
                lines.append(f'rm -f "$(repo_path {_shell_quote(f"/etc/pki/rpm-gpg/TUXWSMAKER-REPO-{repo.id}")})"')

    if repo_family == PackageRepository.FAMILY_DEB:
        lines.append("repo_exec apt-get clean || true")
    else:
        lines.append('if repo_exec command -v dnf >/dev/null 2>&1; then repo_exec dnf clean all || true; else repo_exec yum clean all || true; fi')

    return "\n".join(lines) + "\n"