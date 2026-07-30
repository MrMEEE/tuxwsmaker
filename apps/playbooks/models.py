import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

from apps.builds.models import SSHKey


def _fernet_from_secret() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class PlaybookRepository(models.Model):
    name = models.CharField(max_length=120, unique=True)
    repo_url = models.CharField(max_length=500, unique=True)
    default_branch = models.CharField(max_length=120, default="main")
    ssh_key = models.ForeignKey(
        SSHKey,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="playbook_repositories",
    )
    api_key_encrypted = models.TextField(blank=True)
    last_branch_sync_at = models.DateTimeField(null=True, blank=True)
    last_playbook_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def set_api_key(self, api_key: str) -> None:
        self.api_key_encrypted = _fernet_from_secret().encrypt(api_key.encode("utf-8")).decode("utf-8")

    def get_api_key(self) -> str:
        if not self.api_key_encrypted:
            return ""
        try:
            return _fernet_from_secret().decrypt(self.api_key_encrypted.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return ""

    def has_api_key(self) -> bool:
        return bool(self.api_key_encrypted)


class PlaybookBranch(models.Model):
    repository = models.ForeignKey(
        PlaybookRepository,
        on_delete=models.CASCADE,
        related_name="branches",
    )
    name = models.CharField(max_length=120)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        unique_together = ("repository", "name")

    def __str__(self) -> str:
        return f"{self.repository.name}:{self.name}"


class Playbook(models.Model):
    repository = models.ForeignKey(
        PlaybookRepository,
        on_delete=models.CASCADE,
        related_name="playbooks",
    )
    branch = models.CharField(max_length=120)
    path = models.CharField(max_length=500)
    display_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["repository__name", "branch", "path"]
        unique_together = ("repository", "branch", "path")

    def __str__(self) -> str:
        return f"{self.repository.name}:{self.branch}:{self.path}"
