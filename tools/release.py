#!/usr/bin/env python3
"""Release manager for tuxwsmaker."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "version.txt"


class ReleaseError(RuntimeError):
    pass


def run(cmd: list[str], capture: bool = True, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, capture_output=capture, check=check)


def parse_version(s: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", s.strip())
    if not m:
        raise ReleaseError(f"Invalid version format: {s!r} (expected X.Y.Z)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def fmt(v: tuple[int, int, int]) -> str:
    return f"{v[0]}.{v[1]}.{v[2]}"


def bump(version: str, mode: str) -> str:
    major, minor, patch = parse_version(version)
    if mode == "major":
        return fmt((major + 1, 0, 0))
    if mode == "minor":
        return fmt((major, minor + 1, 0))
    return fmt((major, minor, patch + 1))


def current_branch() -> str:
    return run(["git", "branch", "--show-current"]).stdout.strip()


def ensure_clean_tree() -> None:
    status = run(["git", "status", "--porcelain"]).stdout.strip()
    if status:
        raise ReleaseError("Working tree is not clean. Commit or stash changes first.")


def ensure_tag_free(tag: str) -> None:
    exists = run(["git", "tag", "-l", tag]).stdout.strip()
    if exists:
        raise ReleaseError(f"Tag {tag} already exists")


def main() -> None:
    parser = argparse.ArgumentParser(description="tuxwsmaker release manager")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--major", action="store_true")
    group.add_argument("--minor", action="store_true")
    group.add_argument("--patch", action="store_true")
    group.add_argument("--version", metavar="X.Y.Z")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode = "patch"
    if args.major:
        mode = "major"
    elif args.minor:
        mode = "minor"

    branch = current_branch()
    if branch not in {"main", "master"}:
        raise ReleaseError(f"Releases must run from main/master, current branch is {branch}")

    if not VERSION_FILE.exists():
        raise ReleaseError("version.txt does not exist")

    current = VERSION_FILE.read_text(encoding="utf-8").strip()
    target = args.version or bump(current, mode)
    parse_version(target)

    tag = f"v{target}"
    ensure_tag_free(tag)

    print(f"Current version: {current}")
    print(f"Target version : {target}")

    if args.dry_run:
        print("Dry run complete.")
        return

    ensure_clean_tree()

    VERSION_FILE.write_text(f"{target}\n", encoding="utf-8")

    run(["git", "add", "version.txt"])
    run(["git", "commit", "-m", f"chore: release {target}"], capture=False)
    run(["git", "tag", "-a", tag, "-m", f"Release {target}"], capture=False)
    run(["git", "push", "origin", "HEAD"], capture=False)
    run(["git", "push", "origin", tag], capture=False)

    print(f"Release {target} complete.")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
