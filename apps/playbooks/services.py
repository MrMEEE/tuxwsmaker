from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.builds.models import SSHKey

from .models import Playbook, PlaybookBranch, PlaybookRepository


class PlaybookSyncError(RuntimeError):
    pass


def _run_git(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise PlaybookSyncError("git is required for playbook repository operations")

    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env=run_env,
    )
    if proc.returncode != 0:
        raise PlaybookSyncError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc


def _repo_cache_root() -> Path:
    root = Path(settings.MEDIA_ROOT) / "playbook_repos"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repo_workdir(repository: PlaybookRepository, branch: str) -> Path:
    safe_branch = branch.replace("/", "_")
    return _repo_cache_root() / f"repo-{repository.id}-{safe_branch}"


def _repo_workdir_for_url(repo_url: str, branch: str) -> Path:
    safe_branch = branch.replace("/", "_")
    digest = hashlib.sha1(repo_url.encode("utf-8")).hexdigest()[:12]
    return _repo_cache_root() / f"probe-{digest}-{safe_branch}"


def _repo_auth_mode(repo_url: str) -> str:
    repo_url = repo_url.strip()
    if repo_url.startswith("http://") or repo_url.startswith("https://"):
        return "https"
    if "@" in repo_url:
        return "ssh"
    return "auto"


def _repo_uses_ssh(repo_url: str) -> bool:
    return _repo_auth_mode(repo_url) == "ssh"


def _ssh_command_for_repo(ssh_key: SSHKey | None = None) -> tuple[str, list[Path]]:
    base_parts = [
        "ssh",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-F",
        "/dev/null",
    ]
    cleanup: list[Path] = []
    if ssh_key and ssh_key.get_private_key():
        key_file = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        key_file.write(ssh_key.get_private_key().strip() + "\n")
        key_file.close()
        key_path = Path(key_file.name)
        key_path.chmod(0o600)
        cleanup.append(key_path)
        return " ".join(base_parts[:1] + ["-i", shlex.quote(str(key_path))] + base_parts[1:]), cleanup
    return " ".join(base_parts), cleanup


def _git_env_for_repo(
    repo_url: str,
    *,
    ssh_key: SSHKey | None = None,
    api_key: str = "",
) -> tuple[dict[str, str], list[Path]]:
    env: dict[str, str] = {}
    cleanup: list[Path] = []
    mode = _repo_auth_mode(repo_url)
    if mode == "ssh" or (mode == "auto" and ssh_key and ssh_key.get_private_key()):
        if ssh_key and ssh_key.get_private_key():
            ssh_command, ssh_cleanup = _ssh_command_for_repo(ssh_key)
            env["GIT_SSH_COMMAND"] = ssh_command
            cleanup.extend(ssh_cleanup)
        else:
            env["GIT_SSH_COMMAND"], ssh_cleanup = _ssh_command_for_repo(None)
            cleanup.extend(ssh_cleanup)
        env["GIT_TERMINAL_PROMPT"] = "0"
    elif mode == "https" or (mode == "auto" and api_key):
        if api_key:
            askpass = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
            askpass.write(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) echo oauth2 ;;\n"
                "  *) echo '" + api_key.replace("'", "'\\''") + "' ;;\n"
                "esac\n"
            )
            askpass.close()
            askpass_path = Path(askpass.name)
            askpass_path.chmod(0o700)
            cleanup.append(askpass_path)
            env["GIT_ASKPASS"] = str(askpass_path)
            env["GIT_TERMINAL_PROMPT"] = "0"
    return env, cleanup


def _cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _list_remote_branches(repo_url: str, fallback: str = "main") -> list[str]:
    proc = _run_git(["ls-remote", "--heads", repo_url])
    branches: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if ref.startswith("refs/heads/"):
            branches.append(ref.replace("refs/heads/", "", 1))

    if not branches:
        branches = [fallback]
    return sorted(set(branches))


def _checkout_repository_url(
    repo_url: str,
    branch: str,
    *,
    ssh_key: SSHKey | None = None,
    api_key: str = "",
    force_refresh: bool = False,
) -> Path:
    target = _repo_workdir_for_url(repo_url, branch)
    if target.exists() and (target / ".git").exists() and not force_refresh:
        return target

    git_env, cleanup_paths = _git_env_for_repo(repo_url, ssh_key=ssh_key, api_key=api_key)

    if target.exists() and (target / ".git").exists():
        try:
            _run_git(["fetch", "--all", "--prune"], cwd=target, env=git_env)
            _run_git(["checkout", branch], cwd=target, env=git_env)
            _run_git(["reset", "--hard", f"origin/{branch}"], cwd=target, env=git_env)
            return target
        finally:
            _cleanup_paths(cleanup_paths)

    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(["clone", "--depth", "1", "--branch", branch, repo_url, str(target)], env=git_env)
        return target
    finally:
        _cleanup_paths(cleanup_paths)


def _build_tree(root: Path, relative: Path, max_nodes: int, counter: list[int]) -> dict:
    node = {"name": relative.name or "/", "type": "dir", "children": []}
    if counter[0] >= max_nodes:
        return node

    try:
        entries = sorted((root / relative).iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception:
        return node

    for entry in entries:
        # Hide dotfiles and hidden folders (.git, .github, .gitignore, etc.) in tree view.
        if entry.name.startswith("."):
            continue
        if counter[0] >= max_nodes:
            break
        counter[0] += 1
        rel = relative / entry.name
        if entry.is_dir():
            node["children"].append(_build_tree(root, rel, max_nodes, counter))
        else:
            node["children"].append({"name": entry.name, "type": "file"})
    return node


def inspect_repository(
    repo_url: str,
    preferred_branch: str | None = None,
    *,
    ssh_key: SSHKey | None = None,
    api_key: str = "",
) -> dict:
    repo_url = repo_url.strip()
    if not repo_url:
        raise PlaybookSyncError("Repository URL is required")

    branches = _list_remote_branches(repo_url)
    branch = preferred_branch.strip() if preferred_branch else ""
    if branch not in branches:
        branch = branches[0]

    workdir = _checkout_repository_url(repo_url, branch, ssh_key=ssh_key, api_key=api_key)
    tree = _build_tree(workdir, Path("."), max_nodes=800, counter=[0])
    return {
        "branches": branches,
        "selected_branch": branch,
        "tree": tree,
    }


def sync_branches(repository: PlaybookRepository) -> list[str]:
    branches = _list_remote_branches(repository.repo_url, fallback=repository.default_branch)
    PlaybookBranch.objects.filter(repository=repository).exclude(name__in=branches).delete()
    for branch in branches:
        PlaybookBranch.objects.update_or_create(
            repository=repository,
            name=branch,
            defaults={"is_default": branch == repository.default_branch},
        )

    repository.last_branch_sync_at = timezone.now()
    repository.save(update_fields=["last_branch_sync_at", "updated_at"])
    return branches


def checkout_repository(repository: PlaybookRepository, branch: str) -> Path:
    target = _repo_workdir(repository, branch)
    if target.exists() and (target / ".git").exists():
        git_env, cleanup_paths = _git_env_for_repo(
            repository.repo_url,
            ssh_key=repository.ssh_key if repository.ssh_key and repository.ssh_key.scope == SSHKey.SCOPE_USER else None,
            api_key=repository.get_api_key(),
        )
        try:
            _run_git(["fetch", "--all", "--prune"], cwd=target, env=git_env)
            _run_git(["checkout", branch], cwd=target, env=git_env)
            _run_git(["reset", "--hard", f"origin/{branch}"], cwd=target, env=git_env)
            return target
        finally:
            _cleanup_paths(cleanup_paths)
        return target

    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    git_env, cleanup_paths = _git_env_for_repo(
        repository.repo_url,
        ssh_key=repository.ssh_key if repository.ssh_key and repository.ssh_key.scope == SSHKey.SCOPE_USER else None,
        api_key=repository.get_api_key(),
    )
    try:
        _run_git(["clone", "--depth", "1", "--branch", branch, repository.repo_url, str(target)], env=git_env)
        return target
    finally:
        _cleanup_paths(cleanup_paths)


def sync_playbooks(repository: PlaybookRepository, branch: str) -> list[Playbook]:
    workdir = checkout_repository(repository, branch)
    candidates: list[str] = []
    for path in workdir.rglob("*.yml"):
        rel = path.relative_to(workdir).as_posix()
        if "/roles/" in rel or rel.startswith("roles/"):
            continue
        candidates.append(rel)
    for path in workdir.rglob("*.yaml"):
        rel = path.relative_to(workdir).as_posix()
        if "/roles/" in rel or rel.startswith("roles/"):
            continue
        candidates.append(rel)

    unique_paths = sorted(set(candidates))
    Playbook.objects.filter(repository=repository, branch=branch).exclude(path__in=unique_paths).delete()

    items: list[Playbook] = []
    for rel in unique_paths:
        pb, _ = Playbook.objects.update_or_create(
            repository=repository,
            branch=branch,
            path=rel,
            defaults={"display_name": rel, "is_active": True},
        )
        items.append(pb)

    repository.last_playbook_sync_at = timezone.now()
    repository.save(update_fields=["last_playbook_sync_at", "updated_at"])
    return items
