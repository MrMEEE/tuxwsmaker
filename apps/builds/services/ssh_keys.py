from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from apps.builds.models import SSHKey


class SSHKeyError(RuntimeError):
    pass


@dataclass
class SSHKeyPair:
    private_key_path: Path
    public_key: str

    def cleanup_private(self) -> None:
        if self.private_key_path.exists():
            self.private_key_path.unlink()


def generate_ssh_keypair_text(*, comment: str) -> tuple[str, str]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_key_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8").strip()
    public_key_text = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("utf-8").strip()
    if comment:
        public_key_text = f"{public_key_text} {comment}".strip()
    return private_key_text, public_key_text


def _materialize_private_key(private_key_text: str, *, prefix: str) -> Path:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=prefix, delete=False) as key_file:
        key_file.write(private_key_text.strip() + "\n")
        private_key_path = Path(key_file.name)
    private_key_path.chmod(0o600)
    return private_key_path


def _store_keypair(*, key: SSHKey, private_key_text: str, public_key_text: str) -> SSHKey:
    key.set_keypair(private_key=private_key_text, public_key=public_key_text)
    key.save()
    return key


def ensure_named_ssh_keypair(name: str, output_dir: Path) -> SSHKeyPair:
    key, created = SSHKey.objects.get_or_create(scope=SSHKey.SCOPE_BUILDER, build=None, owner=None, name=name)
    if created or not key.has_keypair():
        private_key_text, public_key_text = generate_ssh_keypair_text(comment=f"tuxwsmaker-{name}")
        _store_keypair(key=key, private_key_text=private_key_text, public_key_text=public_key_text)
    private_key_path = _materialize_private_key(key.get_private_key(), prefix=f"{name}-")
    return SSHKeyPair(private_key_path=private_key_path, public_key=key.public_key)


def generate_build_ssh_keypair(build_id: int, output_dir: Path) -> SSHKeyPair:
    build_key, _created = SSHKey.objects.get_or_create(
        scope=SSHKey.SCOPE_IMAGE_BUILD,
        build_id=build_id,
        owner=None,
        name="build",
    )
    if not build_key.has_keypair():
        private_key_text, public_key_text = generate_ssh_keypair_text(comment=f"tuxwsmaker-build-{build_id}")
        _store_keypair(key=build_key, private_key_text=private_key_text, public_key_text=public_key_text)
    private_key_path = _materialize_private_key(build_key.get_private_key(), prefix=f"build-{build_id}-")
    return SSHKeyPair(private_key_path=private_key_path, public_key=build_key.public_key)
