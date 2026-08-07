from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _fernet_from_secret() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class PackageRepository(models.Model):
    FAMILY_DEB = "deb"
    FAMILY_RPM = "rpm"
    FAMILY_CHOICES = [
        (FAMILY_DEB, "Debian / Ubuntu"),
        (FAMILY_RPM, "RHEL / Fedora"),
    ]

    AUTH_NONE = "none"
    AUTH_BASIC = "basic"
    AUTH_TOKEN = "token"
    AUTH_CHOICES = [
        (AUTH_NONE, "None"),
        (AUTH_BASIC, "Username + password"),
        (AUTH_TOKEN, "Token"),
    ]

    SIGNING_NONE = "none"
    SIGNING_URL = "url"
    SIGNING_INLINE = "inline"
    SIGNING_CHOICES = [
        (SIGNING_NONE, "None"),
        (SIGNING_URL, "GPG key URL"),
        (SIGNING_INLINE, "Inline GPG key"),
    ]

    name = models.CharField(max_length=120, unique=True)
    family = models.CharField(max_length=8, choices=FAMILY_CHOICES)
    enabled = models.BooleanField(default=True)
    base_url = models.CharField(max_length=500)
    auth_type = models.CharField(max_length=16, choices=AUTH_CHOICES, default=AUTH_NONE)
    username = models.CharField(max_length=120, blank=True)
    secret_encrypted = models.TextField(blank=True)
    signing_mode = models.CharField(max_length=16, choices=SIGNING_CHOICES, default=SIGNING_NONE)
    gpg_key_url = models.CharField(max_length=500, blank=True)
    gpg_key_inline = models.TextField(blank=True)
    deb_suite = models.CharField(max_length=120, blank=True)
    deb_components = models.CharField(max_length=255, blank=True)
    rpm_repoid = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def set_secret(self, secret: str) -> None:
        self.secret_encrypted = _fernet_from_secret().encrypt(secret.encode("utf-8")).decode("utf-8")

    def get_secret(self) -> str:
        if not self.secret_encrypted:
            return ""
        try:
            return _fernet_from_secret().decrypt(self.secret_encrypted.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            return ""

    def has_secret(self) -> bool:
        return bool(self.secret_encrypted)

    def effective_rpm_repoid(self) -> str:
        repoid = (self.rpm_repoid or "").strip()
        if repoid:
            return repoid
        return self.name.lower().replace(" ", "-")


class RedHatRepositoryCatalog(models.Model):
    SOURCE_BASEURL = "baseurl"
    SOURCE_METALINK = "metalink"
    SOURCE_MIRRORLIST = "mirrorlist"
    SOURCE_CHOICES = [
        (SOURCE_BASEURL, "baseurl"),
        (SOURCE_METALINK, "metalink"),
        (SOURCE_MIRRORLIST, "mirrorlist"),
    ]

    rhel_major = models.PositiveIntegerField()
    architecture = models.CharField(max_length=32, default="x86_64")
    repo_id = models.CharField(max_length=255)
    name = models.CharField(max_length=500, blank=True)
    source_type = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_BASEURL)
    source_url = models.CharField(max_length=500, blank=True)
    enabled_by_default = models.BooleanField(default=False)
    last_synced = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rhel_major", "repo_id"]
        unique_together = ("rhel_major", "architecture", "repo_id")

    def __str__(self) -> str:
        return f"RHEL {self.rhel_major} {self.architecture}: {self.repo_id}"