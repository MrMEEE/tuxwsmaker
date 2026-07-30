from django.db import models


class PackageList(models.Model):
	DISTRO_ALL = "all"
	DISTRO_RHEL = "rhel"
	DISTRO_DEBIAN = "debian"
	DISTRO_CHOICES = [
		(DISTRO_ALL, "All"),
		(DISTRO_RHEL, "RHEL/Fedora family"),
		(DISTRO_DEBIAN, "Debian/Ubuntu family"),
	]

	name = models.CharField(max_length=120, unique=True)
	description = models.TextField(blank=True)
	distro_family = models.CharField(max_length=16, choices=DISTRO_CHOICES, default=DISTRO_ALL)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["name"]

	def __str__(self) -> str:
		return self.name


class PackageItem(models.Model):
	package_list = models.ForeignKey(PackageList, on_delete=models.CASCADE, related_name="items")
	package_name = models.CharField(max_length=200)

	class Meta:
		unique_together = ("package_list", "package_name")
		ordering = ["package_name"]

	def __str__(self) -> str:
		return f"{self.package_list.name}:{self.package_name}"

# Create your models here.
