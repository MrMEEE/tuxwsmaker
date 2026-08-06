import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

from apps.builds.models import SSHKey


class ServerConfiguration(models.Model):
	SINGLETON_PK = 1
	DEFAULT_NAME = "default"

	name = models.CharField(max_length=120, unique=True)
	concurrent_builds = models.PositiveIntegerField(default=2)
	artifact_retention_days = models.PositiveIntegerField(default=30)
	enable_artifact_compression = models.BooleanField(default=True)
	use_redhat_subscription = models.BooleanField(default=True)
	rhn_username = models.CharField(max_length=120, blank=True)
	rhn_password_encrypted = models.TextField(blank=True)
	builder_base_image_path = models.CharField(max_length=500, blank=True)
	builder_image_label = models.CharField(max_length=255, blank=True)
	builder_image_source_url = models.CharField(max_length=500, blank=True)
	notes = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]

	def __str__(self) -> str:
		return self.name

	def save(self, *args, **kwargs):
		self.pk = self.SINGLETON_PK
		self.name = self.DEFAULT_NAME
		kwargs.pop("force_insert", None)
		return super().save(*args, **kwargs)

	@classmethod
	def get_solo(cls) -> "ServerConfiguration":
		cfg = cls.objects.filter(pk=cls.SINGLETON_PK).first()
		if cfg:
			return cfg
		first = cls.objects.order_by("id").first()
		if first:
			first.pk = cls.SINGLETON_PK
			first.name = cls.DEFAULT_NAME
			first.save()
			return first
		return cls.objects.create(pk=cls.SINGLETON_PK, name=cls.DEFAULT_NAME)

	@classmethod
	def get_effective(cls) -> "ServerConfiguration":
		return cls.get_solo()

	def set_builder_ssh_keypair(self, *, private_key: str, public_key: str) -> None:
		key, _created = SSHKey.objects.get_or_create(scope=SSHKey.SCOPE_BUILDER, build=None, owner=None, name="builder-vm")
		key.set_keypair(private_key=private_key, public_key=public_key)
		key.save()

	def get_builder_ssh_private_key(self) -> str:
		key = SSHKey.objects.filter(scope=SSHKey.SCOPE_BUILDER, build=None, owner=None, name="builder-vm").first()
		if not key:
			return ""
		return key.get_private_key()

	def get_builder_ssh_public_key(self) -> str:
		key = SSHKey.objects.filter(scope=SSHKey.SCOPE_BUILDER, build=None, owner=None, name="builder-vm").first()
		if not key:
			return ""
		return key.public_key.strip()

	def has_builder_ssh_keypair(self) -> bool:
		key = SSHKey.objects.filter(scope=SSHKey.SCOPE_BUILDER, build=None, owner=None, name="builder-vm").first()
		return bool(key and key.has_keypair())

	def set_rhn_password(self, secret: str) -> None:
		self.rhn_password_encrypted = _fernet_from_secret().encrypt(secret.encode("utf-8")).decode("utf-8")

	def get_rhn_password(self) -> str:
		if not self.rhn_password_encrypted:
			return ""
		try:
			return _fernet_from_secret().decrypt(self.rhn_password_encrypted.encode("utf-8")).decode("utf-8")
		except InvalidToken:
			return ""

	def clear_rhn_password(self) -> None:
		self.rhn_password_encrypted = ""

	def has_rhn_password(self) -> bool:
		return bool(self.rhn_password_encrypted)

	@classmethod
	def get_concurrency_limit(cls) -> int:
		cfg = cls.get_effective()
		return max(1, cfg.concurrent_builds)

	@classmethod
	def compression_enabled(cls) -> bool:
		cfg = cls.get_effective()
		return bool(cfg.enable_artifact_compression)


class BuilderProgressEvent(models.Model):
	STAGE_CHOICES = [
		("queued", "Queued"),
		("start", "Start"),
		("progress", "Progress"),
		("iso", "ISO"),
		("cleanup", "Cleanup"),
		("done", "Done"),
		("error", "Error"),
	]

	run_id = models.CharField(max_length=64, db_index=True, blank=True)
	stage = models.CharField(max_length=32, choices=STAGE_CHOICES, default="progress")
	message = models.TextField()
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["created_at", "id"]

	def __str__(self) -> str:
		return f"{self.stage}: {self.message[:80]}"


def _fernet_from_secret() -> Fernet:
	digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
	return Fernet(base64.urlsafe_b64encode(digest))

# Create your models here.
