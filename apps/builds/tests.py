from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from pathlib import Path

from apps.builds.models import BuildArtifact, BuildDefinition, BuildMachineConfig
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionLayout
from apps.serverconfig.models import ServerConfiguration


class BuildQueueLimitTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="tester", password="secret", is_local=True)
		self.client.login(username="tester", password="secret")

		ServerConfiguration.objects.create(name="default", concurrent_builds=1)

		os_obj = OperatingSystem.objects.create(name="RHEL", family=OperatingSystem.FAMILY_RHEL)
		layout = PartitionLayout.objects.create(name="standard")
		cfg = BuildMachineConfig.objects.create(name="default")

		iso1 = ISOImage.objects.create(operating_system=os_obj, version="10.1", iso_file="isos/one.iso")
		iso2 = ISOImage.objects.create(operating_system=os_obj, version="10.2", iso_file="isos/two.iso")

		self.active_build = BuildDefinition.objects.create(
			name="active",
			operating_system=os_obj,
			iso_image=iso1,
			partition_layout=layout,
			machine_config=cfg,
			status=BuildDefinition.STATUS_RUNNING,
		)
		self.blocked_build = BuildDefinition.objects.create(
			name="blocked",
			operating_system=os_obj,
			iso_image=iso2,
			partition_layout=layout,
			machine_config=cfg,
			status=BuildDefinition.STATUS_DRAFT,
		)

	def test_ui_queue_respects_concurrency_limit(self):
		response = self.client.post(reverse("builds:build-queue", args=[self.blocked_build.pk]))
		self.blocked_build.refresh_from_db()
		self.assertEqual(response.status_code, 302)
		self.assertEqual(self.blocked_build.status, BuildDefinition.STATUS_DRAFT)

	def test_api_queue_respects_concurrency_limit(self):
		response = self.client.post(reverse("queue-build", args=[self.blocked_build.pk]))
		self.blocked_build.refresh_from_db()
		self.assertEqual(response.status_code, 429)
		self.assertEqual(self.blocked_build.status, BuildDefinition.STATUS_DRAFT)


class BuildArtifactDownloadTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="dluser", password="secret", is_local=True)
		self.client.login(username="dluser", password="secret")

		os_obj = OperatingSystem.objects.create(name="RHEL-DL", family=OperatingSystem.FAMILY_RHEL)
		layout = PartitionLayout.objects.create(name="layout-dl")
		cfg = BuildMachineConfig.objects.create(name="cfg-dl")
		iso = ISOImage.objects.create(operating_system=os_obj, version="10.9", iso_file="isos/dl.iso")

		self.build = BuildDefinition.objects.create(
			name="build-dl",
			operating_system=os_obj,
			iso_image=iso,
			partition_layout=layout,
			machine_config=cfg,
		)

		artifact_root = Path(settings.ARTIFACT_ROOT)
		artifact_root.mkdir(parents=True, exist_ok=True)

		file_path = artifact_root / "test-file.img"
		file_path.write_bytes(b"artifact")
		self.file_artifact = BuildArtifact.objects.create(
			build=self.build,
			artifact_type=BuildArtifact.TYPE_USB,
			file_path=str(file_path),
			sha256="x",
		)

		dir_path = artifact_root / "test-pxe"
		dir_path.mkdir(parents=True, exist_ok=True)
		(dir_path / "manifest.json").write_text("{}", encoding="utf-8")
		self.dir_artifact = BuildArtifact.objects.create(
			build=self.build,
			artifact_type=BuildArtifact.TYPE_PXE,
			file_path=str(dir_path),
			sha256="y",
		)

	def test_download_file_artifact(self):
		response = self.client.get(
			reverse("builds:artifact-download", args=[self.build.pk, self.file_artifact.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn("attachment", response["Content-Disposition"])

	def test_download_directory_artifact(self):
		response = self.client.get(
			reverse("builds:artifact-download", args=[self.build.pk, self.dir_artifact.pk])
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn(".tar.gz", response["Content-Disposition"])
