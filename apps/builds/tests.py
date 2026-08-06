from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from django.core.cache import cache
import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch

from apps.builds.models import BuildArtifact, BuildDefinition, BuildMachineConfig, SSHKey
from apps.builds.services.artifacts import generate_artifacts
from apps.builds.services.kickstart import render_kickstart_file
from apps.builds.views import _probe_vm_ssh_ready, _recover_stale_build_state
from apps.workers.tasks import _execute_step, build_task_cache_key, reconcile_stale_build_states_on_startup
from apps.catalog.models import ISOImage, OperatingSystem
from apps.layouts.models import PartitionEntry, PartitionLayout
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
		self.assertFalse(self.build.can_run_manual_step(BuildDefinition.STEP_INSTALL_PACKAGES))
		self.assertFalse(self.build.can_run_manual_step(BuildDefinition.STEP_RUN_PLAYBOOKS))

		self.build.current_step = BuildDefinition.STEP_INSTALL_OS
		self.build.runtime_state = {"last_completed_step": BuildDefinition.STEP_INSTALL_OS}
		self.assertEqual(self.build.next_manual_step(), BuildDefinition.STEP_INSTALL_PACKAGES)
		self.assertTrue(self.build.can_run_manual_step(BuildDefinition.STEP_INSTALL_PACKAGES))
		self.assertFalse(self.build.can_run_manual_step(BuildDefinition.STEP_RUN_PLAYBOOKS))

	def test_cleanup_step_is_available_after_release(self):
		self.build.runtime_state = {"last_completed_step": BuildDefinition.STEP_SAVE_RELEASE}
		self.assertEqual(self.build.next_manual_step(), BuildDefinition.STEP_CLEANUP)
		self.assertTrue(self.build.can_run_manual_step(BuildDefinition.STEP_CLEANUP))

	@patch("apps.workers.tasks.LibvirtVMManager.remove_domain")
	def test_cleanup_step_resets_manual_build_to_start(self, mock_remove_domain):
		self.build.status = BuildDefinition.STATUS_DRAFT
		self.build.current_step = BuildDefinition.STEP_SAVE_RELEASE
		self.build.runtime_state = {
			"last_completed_step": BuildDefinition.STEP_SAVE_RELEASE,
			"vm_name": "build-vm",
		}
		self.build.save(update_fields=["status", "current_step", "runtime_state", "updated_at"])

		result = _execute_step(self.build, BuildDefinition.STEP_CLEANUP)

		self.build.refresh_from_db()
		self.assertEqual(result["status"], BuildDefinition.STATUS_DRAFT)
		self.assertEqual(self.build.current_step, BuildDefinition.STEP_PENDING)
		self.assertEqual(self.build.runtime_state.get("last_completed_step"), BuildDefinition.STEP_PENDING)
		self.assertEqual(self.build.runtime_state.get("vm_name"), None)
		mock_remove_domain.assert_called_once_with(name="build-vm", disk_path="")

	@patch("apps.builds.services.artifacts._write_usb_image_from_bundle")
	@patch("apps.builds.services.artifacts._extract_iso_stage2_payload")
	@patch("apps.builds.services.artifacts._extract_boot_assets_from_iso")
	def test_generate_artifacts_preserves_multiple_release_batches(self, mock_extract, _mock_stage2, mock_write_image):
		root = Path("/tmp/tuxwsmaker-test-multi-release")
		shutil.rmtree(root, ignore_errors=True)
		root.mkdir(parents=True, exist_ok=True)

		kernel = root / "fake-vmlinuz"
		initrd = root / "fake-initrd"
		kernel.write_text("k", encoding="utf-8")
		initrd.write_text("i", encoding="utf-8")
		mock_extract.return_value = (kernel, initrd)

		def fake_usb_image(*, bundle_dir, output_path, build_name, source_iso_path=None, build_boot_mode=None):
			output_path.write_bytes(b"usb-image")
			return output_path

		mock_write_image.side_effect = fake_usb_image

		generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)
		generate_artifacts(build=self.build, root=root, qcow2_disk_path=root / "disk.qcow2", compress=False)

		artifacts = list(BuildArtifact.objects.filter(build=self.build).order_by("created_at"))
		self.assertEqual(len(artifacts), 4)
		self.assertEqual(len({artifact.release_group for artifact in artifacts}), 2)
		self.assertEqual(len({artifact.release_label for artifact in artifacts}), 2)

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

	def test_startup_reconcile_marks_stale_running_build_failed(self):
		self.build.status = BuildDefinition.STATUS_RUNNING
		self.build.current_step = BuildDefinition.STEP_SAVE_RELEASE
		self.build.runtime_state = {"active_task_id": "missing-task-id"}
		self.build.save(update_fields=["status", "current_step", "runtime_state", "updated_at"])
		cache.delete(build_task_cache_key(self.build.id))

		recovered_count = reconcile_stale_build_states_on_startup()

		self.build.refresh_from_db()
		self.assertEqual(recovered_count, 1)
		self.assertEqual(self.build.status, BuildDefinition.STATUS_FAILED)
		self.assertEqual(self.build.runtime_state.get("active_task_id"), None)


class BuildVmSshProbeTests(TestCase):
	def setUp(self):
		os_obj = OperatingSystem.objects.create(name="RHEL-PROBE", family=OperatingSystem.FAMILY_RHEL)
		layout = PartitionLayout.objects.create(name="layout-probe")
		cfg = BuildMachineConfig.objects.create(name="cfg-probe")
		iso = ISOImage.objects.create(operating_system=os_obj, version="10.7", iso_file="isos/probe.iso")

		self.build = BuildDefinition.objects.create(
			name="build-probe",
			operating_system=os_obj,
			iso_image=iso,
			partition_layout=layout,
			machine_config=cfg,
		)

		key = SSHKey(
			scope=SSHKey.SCOPE_IMAGE_BUILD,
			build=self.build,
			name="build",
		)
		key.set_keypair(private_key="-----BEGIN PRIVATE KEY-----\nprobe\n-----END PRIVATE KEY-----", public_key="ssh-rsa AAAA probe")
		key.save()

	@patch("apps.builds.views.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=5))
	@patch("apps.builds.views.LibvirtVMManager")
	def test_probe_returns_not_ready_on_ssh_timeout(self, mock_vm_manager_cls, _mock_run):
		vm_manager = mock_vm_manager_cls.return_value
		vm_manager.domain_exists.return_value = True
		vm_manager.domain_is_active.return_value = True
		vm_manager.current_ipv4.return_value = "192.168.200.100"

		vm_exists, vm_ssh_ready, vm_ip = _probe_vm_ssh_ready(self.build)

		self.assertTrue(vm_exists)
		self.assertFalse(vm_ssh_ready)
		self.assertEqual(vm_ip, "192.168.200.100")

	@patch("apps.builds.views.subprocess.run", side_effect=OSError("ssh unavailable"))
	@patch("apps.builds.views.LibvirtVMManager")
	def test_probe_returns_not_ready_on_ssh_oserror(self, mock_vm_manager_cls, _mock_run):
		vm_manager = mock_vm_manager_cls.return_value
		vm_manager.domain_exists.return_value = True
		vm_manager.domain_is_active.return_value = True
		vm_manager.current_ipv4.return_value = "192.168.200.101"

		vm_exists, vm_ssh_ready, vm_ip = _probe_vm_ssh_ready(self.build)

		self.assertTrue(vm_exists)
		self.assertFalse(vm_ssh_ready)
		self.assertEqual(vm_ip, "192.168.200.101")

	@patch("apps.builds.views.subprocess.run")
	@patch("apps.builds.views.LibvirtVMManager")
	def test_probe_prefers_runtime_state_ip_over_dhcp_lookup(self, mock_vm_manager_cls, mock_run):
		self.build.runtime_state = {
			"build_ip_address": "192.168.200.10",
			"vm_mac_address": "52:54:00:c8:00:0b",
		}
		self.build.save(update_fields=["runtime_state", "updated_at"])

		vm_manager = mock_vm_manager_cls.return_value
		vm_manager.domain_exists.return_value = True
		vm_manager.domain_is_active.return_value = True
		vm_manager.current_ipv4.return_value = "192.168.200.100"

		mock_run.return_value = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr="")

		vm_exists, vm_ssh_ready, vm_ip = _probe_vm_ssh_ready(self.build)

		self.assertTrue(vm_exists)
		self.assertTrue(vm_ssh_ready)
		self.assertEqual(vm_ip, "192.168.200.10")
		vm_manager.current_ipv4.assert_not_called()


class KickstartRenderingTests(TestCase):
	def test_render_kickstart_includes_grow_for_lv_missing_size(self):
		layout = PartitionLayout.objects.create(name="layout-kickstart-lv-size")
		PartitionEntry.objects.create(
			layout=layout,
			order=1,
			name="pv0",
			entry_role=PartitionEntry.ROLE_PV,
			filesystem="none",
			size_mode=PartitionEntry.SIZE_FIXED,
			size_mib=8192,
			volume_group="vg0",
		)
		# Intentionally skip size_mib to emulate legacy rows that bypassed full_clean.
		PartitionEntry.objects.create(
			layout=layout,
			order=2,
			name="lvroot",
			entry_role=PartitionEntry.ROLE_LV,
			mount_point="/",
			filesystem="xfs",
			size_mode=PartitionEntry.SIZE_FIXED,
			size_mib=None,
			volume_group="vg0",
			logical_volume="root",
		)

		out_dir = Path("/tmp/tuxwsmaker-test-kickstart-lv-size")
		shutil.rmtree(out_dir, ignore_errors=True)
		path = render_kickstart_file(
			output_dir=out_dir,
			vm_name="vm-lv-size",
			ssh_public_key="ssh-ed25519 AAAA test",
			partition_layout=layout,
		)
		content = path.read_text(encoding="utf-8")

		self.assertIn("volgroup vg0 pv.01", content)
		self.assertIn("logvol / --vgname=vg0 --name=root --size=1 --grow --fstype=xfs", content)
