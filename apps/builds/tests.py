from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from django.core.cache import cache
from pathlib import Path
from unittest.mock import patch

from apps.builds.models import BuildArtifact, BuildDefinition, BuildMachineConfig
from apps.builds.views import _recover_stale_build_state
from apps.workers.tasks import build_task_cache_key
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionLayout
from apps.serverconfig.models import ServerConfiguration


class BuildQueueLimitTests(TestCase):
	def setUp(self):
		cache.clear()
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="tester", password="secret", is_local=True)
		self.client.login(username="tester", password="secret")

		cfg = ServerConfiguration.get_solo()
		cfg.concurrent_builds = 1
		cfg.save()

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


class BuildManualStepTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(username="manual", password="secret", is_local=True)
		self.client.login(username="manual", password="secret")

		os_obj = OperatingSystem.objects.create(name="RHEL-MANUAL", family=OperatingSystem.FAMILY_RHEL)
		layout = PartitionLayout.objects.create(name="layout-manual")
		cfg = BuildMachineConfig.objects.create(name="cfg-manual")
		iso = ISOImage.objects.create(operating_system=os_obj, version="10.5", iso_file="isos/manual.iso")

		self.build = BuildDefinition.objects.create(
			name="build-manual",
			operating_system=os_obj,
			iso_image=iso,
			partition_layout=layout,
			machine_config=cfg,
		)

	def test_build_step_methods_follow_sequence(self):
		self.assertEqual(self.build.next_manual_step(), BuildDefinition.STEP_VM_SHELL)
		self.assertTrue(self.build.can_run_manual_step(BuildDefinition.STEP_VM_SHELL))
		self.assertFalse(self.build.can_run_manual_step(BuildDefinition.STEP_INSTALL_OS))

		self.build.current_step = BuildDefinition.STEP_VM_SHELL
		self.build.runtime_state = {"last_completed_step": BuildDefinition.STEP_VM_SHELL}
		self.assertEqual(self.build.next_manual_step(), BuildDefinition.STEP_INSTALL_OS)
		self.assertTrue(self.build.can_run_manual_step(BuildDefinition.STEP_INSTALL_OS))
		self.assertFalse(self.build.can_run_manual_step(BuildDefinition.STEP_RUN_PLAYBOOKS))

	@patch("apps.builds.views.run_build_step.delay")
	def test_manual_step_route_rejects_missing_prerequisite(self, mock_delay):
		response = self.client.post(
			reverse("builds:build-run-step", args=[self.build.pk, BuildDefinition.STEP_INSTALL_OS])
		)
		self.assertEqual(response.status_code, 302)
		mock_delay.assert_not_called()

	@patch("apps.builds.views.run_build_step.delay")
	def test_manual_step_route_queues_allowed_step(self, mock_delay):
		mock_delay.return_value.id = "task-1"
		response = self.client.post(
			reverse("builds:build-run-step", args=[self.build.pk, BuildDefinition.STEP_VM_SHELL])
		)
		self.assertEqual(response.status_code, 302)
		mock_delay.assert_called_once_with(self.build.id, BuildDefinition.STEP_VM_SHELL)

	def test_recover_stale_running_state_marks_build_failed(self):
		self.build.status = BuildDefinition.STATUS_RUNNING
		self.build.run_mode = BuildDefinition.RUN_MODE_MANUAL
		self.build.current_step = BuildDefinition.STEP_VM_SHELL
		self.build.save(update_fields=["status", "run_mode", "current_step", "updated_at"])
		cache.delete(build_task_cache_key(self.build.id))

		recovered = _recover_stale_build_state(self.build)

		self.assertTrue(recovered)
		self.build.refresh_from_db()
		self.assertEqual(self.build.status, BuildDefinition.STATUS_FAILED)
