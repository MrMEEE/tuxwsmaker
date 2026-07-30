from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
	is_local = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	def __str__(self) -> str:
		return self.username


class UserRole(models.Model):
	ROLE_ADMIN = "admin"
	ROLE_OPERATOR = "operator"
	ROLE_VIEWER = "viewer"
	ROLE_CHOICES = [
		(ROLE_ADMIN, "Admin"),
		(ROLE_OPERATOR, "Operator"),
		(ROLE_VIEWER, "Viewer"),
	]

	user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="roles")
	role = models.CharField(max_length=20, choices=ROLE_CHOICES)

	class Meta:
		unique_together = ("user", "role")

	def __str__(self) -> str:
		return f"{self.user.username}:{self.role}"


def _fernet_from_secret() -> Fernet:
	digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
	return Fernet(base64.urlsafe_b64encode(digest))


class LDAPSource(models.Model):
	PROTOCOL_LDAP = "ldap"
	PROTOCOL_LDAPS = "ldaps"
	PROTOCOL_CHOICES = [(PROTOCOL_LDAP, "LDAP"), (PROTOCOL_LDAPS, "LDAPS")]

	GROUP_MEMBERSHIP_AD = "ad"
	GROUP_MEMBERSHIP_POSIX = "posix"
	GROUP_MEMBERSHIP_CHOICES = [
		(GROUP_MEMBERSHIP_AD, "Active Directory memberOf"),
		(GROUP_MEMBERSHIP_POSIX, "POSIX memberUid"),
	]

	name = models.CharField(max_length=128, unique=True)
	hostname = models.CharField(max_length=255)
	port = models.PositiveIntegerField(default=389)
	protocol = models.CharField(max_length=8, choices=PROTOCOL_CHOICES, default=PROTOCOL_LDAP)
	bind_dn = models.CharField(max_length=255, blank=True)
	bind_password_encrypted = models.TextField(blank=True)
	base_dn = models.CharField(max_length=255)
	group_base_dn = models.CharField(max_length=255, blank=True)
	group_membership = models.CharField(max_length=16, choices=GROUP_MEMBERSHIP_CHOICES, default=GROUP_MEMBERSHIP_AD)
	attr_username = models.CharField(max_length=64, default="uid")
	attr_first_name = models.CharField(max_length=64, default="givenName")
	attr_last_name = models.CharField(max_length=64, default="sn")
	attr_email = models.CharField(max_length=64, default="mail")
	is_active = models.BooleanField(default=False)

	def server_uri(self) -> str:
		return f"{self.protocol}://{self.hostname}:{self.port}"

	def set_bind_password(self, plain: str) -> None:
		if not plain:
			self.bind_password_encrypted = ""
			return
		self.bind_password_encrypted = _fernet_from_secret().encrypt(plain.encode("utf-8")).decode("utf-8")

	def get_bind_password(self) -> str:
		if not self.bind_password_encrypted:
			return ""
		try:
			return _fernet_from_secret().decrypt(self.bind_password_encrypted.encode("utf-8")).decode("utf-8")
		except InvalidToken:
			return ""

	def __str__(self) -> str:
		return self.name


class LDAPGroupMapping(models.Model):
	source = models.ForeignKey(LDAPSource, on_delete=models.CASCADE, related_name="group_mappings")
	ldap_group_dn = models.CharField(max_length=255)
	role = models.CharField(max_length=20, choices=UserRole.ROLE_CHOICES)

	class Meta:
		unique_together = ("source", "ldap_group_dn", "role")

	def __str__(self) -> str:
		return f"{self.source.name}:{self.ldap_group_dn}->{self.role}"

# Create your models here.
