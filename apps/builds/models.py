import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


class BuildMachineConfig(models.Model):
	FIXED_LIBVIRT_NETWORK = "wsbuildnet"
	BOOT_UEFI = "uefi"
	BOOT_BIOS = "bios"
	BOOT_MODE_CHOICES = [
		(BOOT_UEFI, "UEFI"),
		(BOOT_BIOS, "BIOS"),
	]

	name = models.CharField(max_length=120, unique=True)
	cpu = models.PositiveIntegerField(default=4)
	memory_mib = models.PositiveIntegerField(default=8192)
	hypervisor_uri = models.CharField(max_length=200, default="qemu:///system")
	libvirt_network = models.CharField(max_length=120, default=FIXED_LIBVIRT_NETWORK)
	kickstart_timeout_minutes = models.PositiveIntegerField(default=20)
	boot_mode = models.CharField(max_length=8, choices=BOOT_MODE_CHOICES, default=BOOT_UEFI)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]

	def __str__(self) -> str:
		return self.name

	def clean(self) -> None:
		self.libvirt_network = self.FIXED_LIBVIRT_NETWORK

	def save(self, *args, **kwargs):
		self.libvirt_network = self.FIXED_LIBVIRT_NETWORK
		return super().save(*args, **kwargs)


class BuildDefinition(models.Model):
	STATUS_DRAFT = "draft"
	STATUS_QUEUED = "queued"
	STATUS_RUNNING = "running"
	STATUS_FAILED = "failed"
	STATUS_SUCCEEDED = "succeeded"
	STATUS_CHOICES = [
		(STATUS_DRAFT, "Draft"),
		(STATUS_QUEUED, "Queued"),
		(STATUS_RUNNING, "Running"),
		(STATUS_FAILED, "Failed"),
		(STATUS_SUCCEEDED, "Succeeded"),
	]
	RUN_MODE_AUTO = "auto"
	RUN_MODE_MANUAL = "manual"
	RUN_MODE_CHOICES = [
		(RUN_MODE_AUTO, "Automatic"),
		(RUN_MODE_MANUAL, "Manual"),
	]
	RHSM_AUTH_NONE = "none"
	RHSM_AUTH_USERPASS = "userpass"
	RHSM_AUTH_ACTIVATION_KEY = "activation_key"
	RHSM_AUTH_CHOICES = [
		(RHSM_AUTH_NONE, "None"),
		(RHSM_AUTH_USERPASS, "Red Hat username/password"),
		(RHSM_AUTH_ACTIVATION_KEY, "Activation key + org ID"),
	]
	STEP_PENDING = "pending"
	STEP_VM_SHELL = "vm_shell"
	STEP_INSTALL_OS = "install_os"
	STEP_INSTALL_PACKAGES = "install_packages"
	STEP_RUN_PLAYBOOKS = "run_playbooks"
	STEP_SHUTDOWN = "shutdown"
	STEP_DUMP_PARTITIONS = "dump_partitions"
	STEP_SAVE_RELEASE = "save_release"
	STEP_CLEANUP = "cleanup"
	STEP_CHOICES = [
		(STEP_PENDING, "Pending"),
		(STEP_VM_SHELL, "Create VM"),
		(STEP_INSTALL_OS, "Install OS"),
		(STEP_INSTALL_PACKAGES, "Install Packages"),
		(STEP_RUN_PLAYBOOKS, "Run Playbooks"),
		(STEP_SHUTDOWN, "Shutdown"),
		(STEP_DUMP_PARTITIONS, "Dump Partitions"),
		(STEP_SAVE_RELEASE, "Save Release"),
		(STEP_CLEANUP, "Cleanup"),
	]
	STEP_SEQUENCE = [
		STEP_VM_SHELL,
		STEP_INSTALL_OS,
		STEP_INSTALL_PACKAGES,
		STEP_RUN_PLAYBOOKS,
		STEP_SHUTDOWN,
		STEP_DUMP_PARTITIONS,
		STEP_SAVE_RELEASE,
		STEP_CLEANUP,
	]

	name = models.CharField(max_length=120, unique=True)
	operating_system = models.ForeignKey("catalog.OperatingSystem", on_delete=models.PROTECT)
	iso_image = models.ForeignKey("catalog.ISOImage", on_delete=models.PROTECT)
	partition_layout = models.ForeignKey("layouts.PartitionLayout", on_delete=models.PROTECT)
	machine_config = models.ForeignKey(BuildMachineConfig, on_delete=models.PROTECT)
	package_lists = models.ManyToManyField("packages.PackageList", blank=True)
	rhsm_repositories = models.ManyToManyField("repositories.RedHatRepositoryCatalog", blank=True)
	playbooks = models.ManyToManyField(
		"playbooks.Playbook",
		blank=True,
		through="BuildPlaybookSelection",
		related_name="build_definitions",
	)
	afterburners = models.ManyToManyField(
		"afterburners.AfterburnerProfile",
		blank=True,
		through="BuildAfterburnerSelection",
		related_name="build_definitions",
	)
	playbook_repo = models.CharField(max_length=255, blank=True)
	playbook_branch = models.CharField(max_length=120, default="main")
	playbook_path = models.CharField(max_length=255, blank=True)
	rhsm_auth_mode = models.CharField(max_length=24, choices=RHSM_AUTH_CHOICES, default=RHSM_AUTH_NONE)
	rhsm_username = models.CharField(max_length=120, blank=True)
	rhsm_password_encrypted = models.TextField(blank=True)
	rhsm_org_id = models.CharField(max_length=120, blank=True)
	rhsm_activation_key_encrypted = models.TextField(blank=True)
	output_pxe = models.BooleanField(default=True)
	output_usb_img = models.BooleanField(default=True)
	status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
	run_mode = models.CharField(max_length=16, choices=RUN_MODE_CHOICES, default=RUN_MODE_AUTO)
	current_step = models.CharField(max_length=32, choices=STEP_CHOICES, default=STEP_PENDING)
	runtime_state = models.JSONField(default=dict, blank=True)
	started_by = models.ForeignKey("users.User", null=True, blank=True, on_delete=models.SET_NULL)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["-updated_at"]

	def __str__(self) -> str:
		return self.name

	def clean(self) -> None:
		if not self.output_pxe and not self.output_usb_img:
			raise ValidationError("At least one output type must be enabled")
		if self.iso_image_id and self.operating_system_id:
			if self.iso_image.operating_system_id != self.operating_system_id:
				raise ValidationError("ISO image must belong to selected operating system")
		if self.partition_layout_id and self.machine_config_id:
			boot_mode = self.machine_config.boot_mode
			table_type = self.partition_layout.table_type
			entries = list(self.partition_layout.entries.all())

			if boot_mode == BuildMachineConfig.BOOT_UEFI and table_type != "gpt":
				raise ValidationError("UEFI machine config requires a GPT partition table")
			if boot_mode == BuildMachineConfig.BOOT_BIOS and table_type != "mbr":
				raise ValidationError("BIOS machine config requires an MBR partition table")
			if boot_mode == BuildMachineConfig.BOOT_UEFI:
				has_esp = any(
					(entry.mount_point or "").strip() == "/boot/efi" or entry.filesystem == "efi"
					for entry in entries
				)
				if not has_esp:
					raise ValidationError("UEFI machine config requires an EFI system partition mounted at /boot/efi")

	def ordered_playbook_selections(self):
		return self.playbook_selections.select_related("playbook", "playbook__repository").order_by("order")

	def ordered_repository_selections(self):
		return self.repository_selections.select_related("repository").order_by("order")

	def ordered_afterburner_selections(self):
		return self.afterburner_selections.select_related("afterburner").order_by("order")

	def set_rhsm_password(self, secret: str) -> None:
		self.rhsm_password_encrypted = _fernet_from_secret().encrypt(secret.encode("utf-8")).decode("utf-8")

	def get_rhsm_password(self) -> str:
		if not self.rhsm_password_encrypted:
			return ""
		try:
			return _fernet_from_secret().decrypt(self.rhsm_password_encrypted.encode("utf-8")).decode("utf-8")
		except InvalidToken:
			return ""

	def has_rhsm_password(self) -> bool:
		return bool(self.rhsm_password_encrypted)

	def set_rhsm_activation_key(self, secret: str) -> None:
		self.rhsm_activation_key_encrypted = _fernet_from_secret().encrypt(secret.encode("utf-8")).decode("utf-8")

	def get_rhsm_activation_key(self) -> str:
		if not self.rhsm_activation_key_encrypted:
			return ""
		try:
			return _fernet_from_secret().decrypt(self.rhsm_activation_key_encrypted.encode("utf-8")).decode("utf-8")
		except InvalidToken:
			return ""

	def has_rhsm_activation_key(self) -> bool:
		return bool(self.rhsm_activation_key_encrypted)

	def next_manual_step(self) -> str:
		last_completed = str((self.runtime_state or {}).get("last_completed_step") or self.STEP_PENDING)
		if last_completed not in self.STEP_SEQUENCE:
			return self.STEP_VM_SHELL
		index = self.STEP_SEQUENCE.index(last_completed) + 1
		if index >= len(self.STEP_SEQUENCE):
			return self.STEP_SAVE_RELEASE
		return self.STEP_SEQUENCE[index]

	def has_completed_step(self, step: str) -> bool:
		if step == self.STEP_PENDING:
			return True
		last_completed = str((self.runtime_state or {}).get("last_completed_step") or self.STEP_PENDING)
		if last_completed not in self.STEP_SEQUENCE:
			return False
		return self.STEP_SEQUENCE.index(last_completed) >= self.STEP_SEQUENCE.index(step)

	def can_run_manual_step(self, step: str) -> bool:
		if step not in self.STEP_SEQUENCE:
			return False
		if self.status in {self.STATUS_QUEUED, self.STATUS_RUNNING}:
			return False
		if step == self.STEP_VM_SHELL:
			return True
		previous_step = self.STEP_SEQUENCE[self.STEP_SEQUENCE.index(step) - 1]
		return self.has_completed_step(previous_step)


class BuildArtifact(models.Model):
	TYPE_PXE = "pxe"
	TYPE_USB = "usb"
	TYPE_CLONE = "clone"
	TYPE_CHOICES = [(TYPE_PXE, "PXE bundle"), (TYPE_USB, "USB image"), (TYPE_CLONE, "Clone release")]

	build = models.ForeignKey(BuildDefinition, on_delete=models.CASCADE, related_name="artifacts")
	artifact_type = models.CharField(max_length=8, choices=TYPE_CHOICES)
	file_path = models.CharField(max_length=500)
	sha256 = models.CharField(max_length=64)
	compressed = models.BooleanField(default=False)
	release_group = models.CharField(max_length=64, default="default", blank=True)
	release_label = models.CharField(max_length=64, default="latest", blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["-created_at"]

	def __str__(self) -> str:
		return f"{self.build.name}:{self.artifact_type}"


class BuildLogEntry(models.Model):
	build = models.ForeignKey(BuildDefinition, on_delete=models.CASCADE, related_name="logs")
	stage = models.CharField(max_length=64, blank=True)
	message = models.TextField()
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["created_at", "id"]

	def __str__(self) -> str:
		prefix = f"{self.stage}: " if self.stage else ""
		return f"{self.build.name}:{prefix}{self.message[:80]}"


class BuildPlaybookSelection(models.Model):
	build = models.ForeignKey(
		BuildDefinition,
		on_delete=models.CASCADE,
		related_name="playbook_selections",
	)
	playbook = models.ForeignKey(
		"playbooks.Playbook",
		on_delete=models.CASCADE,
		related_name="build_selections",
	)
	order = models.PositiveIntegerField(default=1)

	class Meta:
		ordering = ["order", "id"]
		unique_together = (
			("build", "playbook"),
			("build", "order"),
		)

	def __str__(self) -> str:
		return f"{self.build.name}:{self.order}:{self.playbook.path}"


class BuildAfterburnerSelection(models.Model):
	build = models.ForeignKey(
		BuildDefinition,
		on_delete=models.CASCADE,
		related_name="afterburner_selections",
	)
	afterburner = models.ForeignKey(
		"afterburners.AfterburnerProfile",
		on_delete=models.CASCADE,
		related_name="build_selections",
	)
	order = models.PositiveIntegerField(default=1)

	class Meta:
		ordering = ["order", "id"]
		unique_together = (
			("build", "afterburner"),
			("build", "order"),
		)

	def __str__(self) -> str:
		return f"{self.build.name}:{self.order}:{self.afterburner.name}"


class BuildRepositorySelection(models.Model):
	build = models.ForeignKey(
		BuildDefinition,
		on_delete=models.CASCADE,
		related_name="repository_selections",
	)
	repository = models.ForeignKey(
		"repositories.PackageRepository",
		on_delete=models.CASCADE,
		related_name="build_selections",
	)
	order = models.PositiveIntegerField(default=1)
	enable_during_build = models.BooleanField(default=False)
	enable_before_afterburner = models.BooleanField(default=False)

	class Meta:
		ordering = ["order", "id"]
		unique_together = (
			("build", "repository"),
			("build", "order"),
		)

	def __str__(self) -> str:
		return f"{self.build.name}:{self.order}:{self.repository.name}"


def _fernet_from_secret() -> Fernet:
	digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
	return Fernet(base64.urlsafe_b64encode(digest))


class SSHKey(models.Model):
	SCOPE_BUILDER = "builder"
	SCOPE_IMAGE_BUILD = "image_build"
	SCOPE_USER = "user"
	SCOPE_CHOICES = [
		(SCOPE_BUILDER, "Builder"),
		(SCOPE_IMAGE_BUILD, "Image Build"),
		(SCOPE_USER, "User"),
	]

	scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default=SCOPE_BUILDER)
	name = models.CharField(max_length=120)
	build = models.ForeignKey(BuildDefinition, null=True, blank=True, on_delete=models.CASCADE, related_name="ssh_keys")
	owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="ssh_keys")
	private_key_encrypted = models.TextField(blank=True)
	public_key = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["-created_at"]
		unique_together = (
			("build", "name"),
			("owner", "scope", "name"),
		)

	def __str__(self) -> str:
		if self.scope == self.SCOPE_USER and self.owner_id:
			owner = f"user-{self.owner_id}"
		elif self.scope == self.SCOPE_IMAGE_BUILD and self.build_id:
			owner = f"build-{self.build_id}"
		else:
			owner = self.scope
		return f"{owner}:{self.name}"

	def clean(self) -> None:
		if self.scope == self.SCOPE_USER:
			self.build = None
		elif self.scope == self.SCOPE_BUILDER:
			self.build = None
			self.owner = None
		elif self.scope == self.SCOPE_IMAGE_BUILD and not self.build_id:
			raise ValidationError("Image Build SSH keys must be linked to a build")

	def set_keypair(self, *, private_key: str, public_key: str) -> None:
		self.private_key_encrypted = _fernet_from_secret().encrypt(private_key.encode("utf-8")).decode("utf-8")
		self.public_key = public_key.strip()

	def get_private_key(self) -> str:
		if not self.private_key_encrypted:
			return ""
		try:
			return _fernet_from_secret().decrypt(self.private_key_encrypted.encode("utf-8")).decode("utf-8")
		except InvalidToken:
			return ""

	def has_keypair(self) -> bool:
		return bool(self.private_key_encrypted and self.public_key)
