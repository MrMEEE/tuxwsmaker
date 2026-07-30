from django.db import models


class OperatingSystem(models.Model):
	FAMILY_RHEL = "rhel"
	FAMILY_DEBIAN = "debian"
	FAMILY_CHOICES = [
		(FAMILY_RHEL, "RHEL/Fedora family"),
		(FAMILY_DEBIAN, "Debian/Ubuntu family"),
	]

	name = models.CharField(max_length=120)
	description = models.TextField(blank=True)
	family = models.CharField(max_length=16, choices=FAMILY_CHOICES, default=FAMILY_RHEL)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]
		unique_together = ("name", "family")

	def __str__(self) -> str:
		return f"{self.name} ({self.family})"


class OSVariable(models.Model):
	operating_system = models.ForeignKey(OperatingSystem, on_delete=models.CASCADE, related_name="variables")
	key = models.CharField(max_length=120)
	value = models.TextField()

	class Meta:
		unique_together = ("operating_system", "key")
		ordering = ["key"]

	def __str__(self) -> str:
		return f"{self.operating_system.name}:{self.key}"


class ISOImage(models.Model):
	operating_system = models.ForeignKey(OperatingSystem, on_delete=models.CASCADE, related_name="isos")
	version = models.CharField(max_length=64)
	os_version = models.CharField(max_length=64)
	iso_file = models.FileField(upload_to="isos/")
	sha256 = models.CharField(max_length=64, blank=True)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["operating_system__name", "version"]
		unique_together = ("operating_system", "version")

	def __str__(self) -> str:
		return f"{self.operating_system.name} {self.version}"

	def effective_variables(self) -> dict[str, str]:
		values = {item.key: item.value for item in self.operating_system.variables.all()}
		values.update({item.key: item.value for item in self.variables.all()})
		return values


class ISOVariable(models.Model):
	iso = models.ForeignKey(ISOImage, on_delete=models.CASCADE, related_name="variables")
	key = models.CharField(max_length=120)
	value = models.TextField()

	class Meta:
		unique_together = ("iso", "key")
		ordering = ["key"]

	def __str__(self) -> str:
		return f"{self.iso}:{self.key}"

# Create your models here.
