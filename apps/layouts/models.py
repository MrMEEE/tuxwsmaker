from django.db import models
from django.core.exceptions import ValidationError


class PartitionLayout(models.Model):
	TABLE_GPT = "gpt"
	TABLE_MBR = "mbr"
	TABLE_CHOICES = [(TABLE_GPT, "GPT"), (TABLE_MBR, "MBR")]

	name = models.CharField(max_length=120, unique=True)
	description = models.TextField(blank=True)
	table_type = models.CharField(max_length=8, choices=TABLE_CHOICES, default=TABLE_GPT)
	yaml_fallback = models.TextField(blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]

	def __str__(self) -> str:
		return self.name


class PartitionEntry(models.Model):
	SIZE_FIXED = "fixed"
	SIZE_REMAINDER = "remainder"
	SIZE_CHOICES = [(SIZE_FIXED, "Fixed MiB"), (SIZE_REMAINDER, "Use remainder")]
	ROLE_STANDARD = "standard"
	ROLE_PV = "pv"
	ROLE_LV = "lv"
	ROLE_CHOICES = [
		(ROLE_STANDARD, "Standard partition"),
		(ROLE_PV, "LVM physical volume (PV)"),
		(ROLE_LV, "LVM logical volume (LV)"),
	]
	FILESYSTEM_CHOICES = [
		("ext2", "ext2"),
		("ext3", "ext3"),
		("ext4", "ext4"),
		("xfs", "xfs"),
		("btrfs", "btrfs"),
		("fat32", "fat32"),
		("efi", "efi"),
		("swap", "swap"),
		("none", "none (raw/unformatted)"),
	]
	GPT_TYPE_CHOICES = [
		("", "Default"),
		("c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "EFI System Partition"),
		("0fc63daf-8483-4772-8e79-3d69d8477de4", "Linux filesystem"),
		("e6d6d379-f507-44c2-a23c-238f2a3df928", "Linux LVM"),
		("0657fd6d-a4ab-43c4-84e5-0933c84b4f4f", "Linux swap"),
		("8308", "Linux reserved"),
	]
	MBR_TYPE_CHOICES = [
		("", "Default"),
		("0x83", "Linux"),
		("0x82", "Linux swap"),
		("0x8e", "Linux LVM"),
		("0xef", "EFI (FAT)"),
		("0x07", "NTFS/exFAT"),
	]

	layout = models.ForeignKey(PartitionLayout, on_delete=models.CASCADE, related_name="entries")
	order = models.PositiveIntegerField()
	name = models.CharField(max_length=120)
	entry_role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_STANDARD)
	mount_point = models.CharField(max_length=120, blank=True)
	filesystem = models.CharField(max_length=32, choices=FILESYSTEM_CHOICES, default="ext4")
	size_mode = models.CharField(max_length=16, choices=SIZE_CHOICES, default=SIZE_FIXED)
	size_mib = models.PositiveIntegerField(null=True, blank=True)
	gpt_type = models.CharField(max_length=64, blank=True)
	volume_group = models.CharField(max_length=64, blank=True)
	logical_volume = models.CharField(max_length=64, blank=True)
	is_boot = models.BooleanField(default=False)
	luks_enabled = models.BooleanField(default=False)
	luks_name = models.CharField(max_length=64, blank=True)

	class Meta:
		ordering = ["order"]
		unique_together = ("layout", "order")

	def clean(self) -> None:
		if self.size_mode == self.SIZE_FIXED and not self.size_mib:
			raise ValidationError("size_mib is required for fixed-size partitions")
		if self.size_mode == self.SIZE_REMAINDER and self.size_mib:
			raise ValidationError("size_mib must be empty when using remainder mode")
		if self.luks_enabled:
			if self.entry_role != self.ROLE_PV:
				raise ValidationError("LUKS can only be enabled on the encrypted PV container")
			if self.filesystem != "none":
				raise ValidationError("Encrypted PV containers must use filesystem 'none'")
			if self.mount_point:
				raise ValidationError("Encrypted PV containers must not have a mount_point")
			if not self.luks_name:
				raise ValidationError("luks_name is required when LUKS is enabled")
		if self.entry_role == self.ROLE_LV:
			if self.luks_enabled:
				raise ValidationError("LVM logical volumes must not be marked LUKS-enabled")
			if not self.volume_group:
				raise ValidationError("volume_group is required for LVM logical volumes")
			if not self.logical_volume:
				raise ValidationError("logical_volume is required for LVM logical volumes")
		if self.entry_role != self.ROLE_LV and self.logical_volume:
			raise ValidationError("logical_volume can only be set for LVM logical volumes")
		if self.entry_role == self.ROLE_PV and self.mount_point:
			raise ValidationError("mount_point must be empty for LVM physical volumes")
		if self.entry_role == self.ROLE_PV and self.filesystem != "none":
			raise ValidationError("filesystem must be 'none' for LVM physical volumes")

	def __str__(self) -> str:
		return f"{self.layout.name}:{self.order}:{self.mount_point or self.name}"

# Create your models here.
