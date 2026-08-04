from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
import subprocess
import tarfile
import tempfile
from pathlib import Path

from apps.workers.tasks import run_build_definition
from apps.workers.tasks import build_task_cache_key
from apps.workers.tasks import rerun_build_playbooks
from apps.workers.tasks import run_build_step
from apps.serverconfig.models import ServerConfiguration
from apps.realtime.events import publish_event
from config.celery import app as celery_app

from .forms import BuildDefinitionForm, BuildMachineConfigForm, UserSSHKeyForm
from .models import BuildArtifact, BuildDefinition, BuildLogEntry, BuildMachineConfig, BuildPlaybookSelection, SSHKey
from .services.builder import BuilderVMManager
from .services.provisioning import AnsibleProvisioner
from .services.virtualization import LibvirtVMManager


def _is_modal_request(request) -> bool:
	return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("X-Modal-Request") == "1"


def _persist_build_playbook_order(build: BuildDefinition, ordered_ids: list[int]) -> None:
	BuildPlaybookSelection.objects.filter(build=build).delete()
	if not ordered_ids:
		return
	BuildPlaybookSelection.objects.bulk_create(
		[
			BuildPlaybookSelection(build=build, playbook_id=playbook_id, order=index + 1)
			for index, playbook_id in enumerate(ordered_ids)
		]
	)


def _extract_last_known_vm_ip(build: BuildDefinition) -> str:
	for entry in BuildLogEntry.objects.filter(build=build, stage="network").order_by("-created_at", "-id"):
		msg = (entry.message or "").strip()
		if "Build VM is reachable at " in msg:
			return msg.rsplit(" ", 1)[-1].strip()
		if "Build VM obtained IP address " in msg:
			return msg.rsplit(" ", 1)[-1].strip()
	return ""


def _probe_vm_ssh_ready(build: BuildDefinition) -> tuple[bool, bool, str]:
	vm_name = f"build-{build.id}"
	state = dict(build.runtime_state or {})
	try:
		vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
		vm_exists = vm_manager.domain_exists(vm_name)
		if not vm_exists:
			return False, False, ""
		vm_running = vm_manager.domain_is_active(vm_name)
		if not vm_running:
			return True, False, ""
	except Exception:
		return False, False, ""

	ip_address = str(state.get("build_ip_address") or "").strip()
	vm_mac_address = str(state.get("vm_mac_address") or "").strip()
	try:
		if not ip_address:
			ip_address = vm_manager.current_ipv4(
				domain_name=vm_name,
				network_name=BuildMachineConfig.FIXED_LIBVIRT_NETWORK,
				mac_address=vm_mac_address,
			) or ""
	except Exception:
		if not ip_address:
			ip_address = ""
	if not ip_address:
		ip_address = _extract_last_known_vm_ip(build)
	if not ip_address:
		return True, False, ""

	build_key = SSHKey.objects.filter(
		scope=SSHKey.SCOPE_IMAGE_BUILD,
		build=build,
		name="build",
	).first()
	if build_key is None or not build_key.has_keypair():
		return True, False, ip_address

	private_key_text = build_key.get_private_key().strip()
	if not private_key_text:
		return True, False, ip_address

	with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=f"build-{build.id}-", delete=False) as key_file:
		key_file.write(private_key_text + "\n")
		key_path = Path(key_file.name)
	key_path.chmod(0o600)
	try:
		ssh_user = getattr(settings, "BUILD_VM_SSH_USER", "root")
		cmd = [
			"ssh",
			"-o",
			"BatchMode=yes",
			"-o",
			"StrictHostKeyChecking=no",
			"-o",
			"UserKnownHostsFile=/dev/null",
			"-i",
			str(key_path),
			f"{ssh_user}@{ip_address}",
			"true",
		]
		proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=5)
		return True, proc.returncode == 0, ip_address
	except (subprocess.TimeoutExpired, OSError):
		return True, False, ip_address
	finally:
		key_path.unlink(missing_ok=True)


def _manual_step_items(build: BuildDefinition) -> list[dict[str, object]]:
	items: list[dict[str, object]] = []
	next_step = build.next_manual_step()
	for step, label in BuildDefinition.STEP_CHOICES:
		if step == BuildDefinition.STEP_PENDING:
			continue
		completed = build.has_completed_step(step)
		enabled = build.can_run_manual_step(step)
		items.append(
			{
				"slug": step,
				"label": label,
				"completed": completed,
				"enabled": enabled,
				"is_next": step == next_step and not completed,
			}
		)
	return items


def _task_is_active(task_id: str) -> bool:
	if not task_id:
		return False
	try:
		state = str(celery_app.AsyncResult(task_id).state or "").upper()
	except Exception:
		return False
	return state in {"PENDING", "RECEIVED", "STARTED", "RETRY"}


def _set_build_active_task(build: BuildDefinition, task_id: str) -> None:
	state = dict(build.runtime_state or {})
	state["active_task_id"] = task_id
	build.runtime_state = state
	build.save(update_fields=["runtime_state", "updated_at"])


def _recover_stale_build_state(build: BuildDefinition) -> bool:
	if build.status not in {BuildDefinition.STATUS_RUNNING, BuildDefinition.STATUS_QUEUED}:
		return False
	state = dict(build.runtime_state or {})
	task_id = str(state.get("active_task_id") or cache.get(build_task_cache_key(build.id)) or "").strip()
	if _task_is_active(task_id):
		return False
	state.pop("active_task_id", None)
	build.runtime_state = state
	build.status = BuildDefinition.STATUS_FAILED
	build.save(update_fields=["status", "runtime_state", "updated_at"])
	message = f"Recovered stale build state after task exited unexpectedly during {build.get_current_step_display()}"
	BuildLogEntry.objects.create(build=build, stage="error", message=message)
	publish_event(
		"builds",
		"failed",
		{"build_id": build.id, "status": build.status, "current_step": build.current_step, "error": message},
	)
	return True


def _cleanup_builder_boot_files_for_build(build: BuildDefinition) -> None:
	state = dict(build.runtime_state or {})
	builder_ip = str(state.get("builder_ip") or "").strip()
	builder_user = str(state.get("builder_ssh_user") or getattr(settings, "BUILDER_VM_SSH_USER", "root")).strip() or "root"
	tftp_root = str(state.get("tftp_root") or "/var/lib/tftpboot").strip() or "/var/lib/tftpboot"
	vm_mac = str(state.get("vm_mac_address") or "").strip().lower()

	if not builder_ip:
		return

	builder_manager = BuilderVMManager()
	key_pair = builder_manager.ensure_access_keypair()
	provisioner = AnsibleProvisioner(project_root=Path(__file__).resolve().parents[2])
	try:
		mac_slug = vm_mac.replace(":", "-") if vm_mac else ""
		cleanup_parts = [
			"set +e",
			f"mkdir -p {tftp_root}/builds {tftp_root}/build-state {tftp_root}/pxelinux.cfg {tftp_root}/efi64 {tftp_root}/efi32 /var/www/html/isos /var/www/html/kickstarts",
			f"if mountpoint -q /var/www/html/isos/build-{build.id}; then umount /var/www/html/isos/build-{build.id} || umount -l /var/www/html/isos/build-{build.id} || true; fi",
			f"rm -rf /var/www/html/isos/build-{build.id}",
			f"rm -f /var/www/html/kickstarts/build-{build.id}.cfg",
			f"rm -rf {tftp_root}/builds/build-{build.id}",
			f"rm -f {tftp_root}/build-state/build-{build.id}.mac",
		]
		if mac_slug:
			cleanup_parts.append(
				f"rm -f {tftp_root}/pxelinux.cfg/01-{mac_slug} {tftp_root}/grub.cfg-01-{mac_slug} {tftp_root}/grub.cfg-{mac_slug} "
				f"{tftp_root}/efi64/grub.cfg-01-{mac_slug} {tftp_root}/efi64/grub.cfg-{mac_slug} "
				f"{tftp_root}/efi32/grub.cfg-01-{mac_slug} {tftp_root}/efi32/grub.cfg-{mac_slug}"
			)
		cleanup_parts.append("restorecon -RF /var/www/html/isos /var/www/html/kickstarts >/dev/null 2>&1 || true")
		cleanup_cmd = "; ".join(cleanup_parts)
		provisioner.run_remote_command(
			host=builder_ip,
			user=builder_user,
			private_key_path=str(key_pair.private_key_path),
			command=cleanup_cmd,
			timeout_seconds=45,
		)
	except Exception:
		# Best-effort cleanup: cancellation should still proceed even if builder is unreachable.
		pass
	finally:
		key_pair.cleanup_private()


class BuildListView(LoginRequiredMixin, ListView):
	model = BuildDefinition
	template_name = "builds/build_list.html"
	context_object_name = "items"


class BuildDetailView(LoginRequiredMixin, DetailView):
	model = BuildDefinition
	template_name = "builds/build_detail.html"
	context_object_name = "build"

	def get_queryset(self):
		return BuildDefinition.objects.select_related(
			"machine_config", "iso_image", "operating_system", "partition_layout"
		).prefetch_related("artifacts", "logs")

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		build = self.object
		if _recover_stale_build_state(build):
			build.refresh_from_db()
		vm_exists, vm_ssh_ready, vm_ip = _probe_vm_ssh_ready(build)
		context["build_vm_exists"] = vm_exists
		context["build_vm_ssh_ready"] = vm_ssh_ready
		context["build_vm_ip"] = vm_ip
		context["current_step_display"] = build.get_current_step_display()
		context["manual_steps"] = _manual_step_items(build)
		context["show_full_build"] = build.status not in {
			BuildDefinition.STATUS_RUNNING,
			BuildDefinition.STATUS_QUEUED,
		}
		has_playbooks = bool(build.playbook_path) or build.ordered_playbook_selections().exists()
		context["show_rerun_playbooks"] = vm_exists and vm_ssh_ready and has_playbooks and build.status not in {
			BuildDefinition.STATUS_RUNNING,
			BuildDefinition.STATUS_QUEUED,
		}
		context["show_cancel_build"] = vm_exists or build.status in {
			BuildDefinition.STATUS_RUNNING,
			BuildDefinition.STATUS_QUEUED,
		}
		return context


class BuildCreateView(LoginRequiredMixin, CreateView):
	model = BuildDefinition
	form_class = BuildDefinitionForm
	template_name = "builds/build_form.html"
	success_url = reverse_lazy("builds:build-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["builds/build_form_fragment.html"]
		return [self.template_name]

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["modal_mode"] = _is_modal_request(self.request)
		return context

	def form_invalid(self, form):
		if _is_modal_request(self.request):
			return self.render_to_response(self.get_context_data(form=form), status=400)
		return super().form_invalid(form)

	def form_valid(self, form):
		response = super().form_valid(form)
		_persist_build_playbook_order(self.object, form.cleaned_data.get("ordered_playbook_ids", []))
		publish_event("builds", "created", {"build_id": self.object.id, "status": self.object.status})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "build_id": self.object.id, "message": "Build saved"})
		return response


class BuildUpdateView(LoginRequiredMixin, UpdateView):
	model = BuildDefinition
	form_class = BuildDefinitionForm
	template_name = "builds/build_form.html"
	success_url = reverse_lazy("builds:build-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["builds/build_form_fragment.html"]
		return [self.template_name]

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["modal_mode"] = _is_modal_request(self.request)
		return context

	def form_invalid(self, form):
		if _is_modal_request(self.request):
			return self.render_to_response(self.get_context_data(form=form), status=400)
		return super().form_invalid(form)

	def form_valid(self, form):
		response = super().form_valid(form)
		_persist_build_playbook_order(self.object, form.cleaned_data.get("ordered_playbook_ids", []))
		publish_event("builds", "updated", {"build_id": self.object.id, "status": self.object.status})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "build_id": self.object.id, "message": "Build saved"})
		return response


class BuildDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk):
		obj = get_object_or_404(BuildDefinition, pk=pk)
		deleted_id = obj.id
		obj.delete()
		publish_event("builds", "deleted", {"build_id": deleted_id})
		messages.success(request, "Build definition deleted")
		return redirect("builds:build-list")


class BuildQueueView(LoginRequiredMixin, View):
	def post(self, request, pk):
		build = get_object_or_404(BuildDefinition, pk=pk)
		if _recover_stale_build_state(build):
			build.refresh_from_db()
		if build.status in {BuildDefinition.STATUS_RUNNING, BuildDefinition.STATUS_QUEUED}:
			messages.warning(request, "Build is already queued or running")
			return redirect("builds:build-detail", pk=build.pk)

		active_count = BuildDefinition.objects.filter(
			status__in=[BuildDefinition.STATUS_QUEUED, BuildDefinition.STATUS_RUNNING]
		).count()
		limit = ServerConfiguration.get_concurrency_limit()
		if active_count >= limit:
			messages.error(request, f"Concurrency limit reached ({limit}). Try again later.")
			return redirect("builds:build-detail", pk=build.pk)

		build.status = BuildDefinition.STATUS_QUEUED
		build.started_by = request.user
		build.save(update_fields=["status", "started_by", "updated_at"])
		publish_event("builds", "queued", {"build_id": build.id, "status": build.status})
		BuildLogEntry.objects.create(build=build, stage="queued", message="Build queued")
		task = run_build_definition.delay(build.id)
		_set_build_active_task(build, task.id)
		cache.set(build_task_cache_key(build.id), task.id, timeout=6 * 60 * 60)
		messages.success(request, "Build queued")
		return redirect("builds:build-detail", pk=build.pk)


class BuildCancelView(LoginRequiredMixin, View):
	def post(self, request, pk):
		build = get_object_or_404(BuildDefinition.objects.select_related("machine_config"), pk=pk)
		vm_name = f"build-{build.id}"

		task_id = cache.get(build_task_cache_key(build.id))
		if task_id:
			celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
			cache.delete(build_task_cache_key(build.id))

		try:
			vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
			vm_manager.remove_domain(name=vm_name, disk_path=f"{Path(settings.ARTIFACT_ROOT) / 'disks' / (vm_name + '.qcow2')}")
		except Exception:
			pass

		_cleanup_builder_boot_files_for_build(build)

		build.status = BuildDefinition.STATUS_DRAFT
		build.current_step = BuildDefinition.STEP_PENDING
		build.runtime_state = {"last_completed_step": BuildDefinition.STEP_PENDING}
		build.save(update_fields=["status", "current_step", "runtime_state", "updated_at"])
		BuildLogEntry.objects.create(build=build, stage="cancel", message="Build cancelled by user")
		publish_event("builds", "cancelled", {"build_id": build.id, "status": build.status})
		messages.success(request, "Build cancelled")
		return redirect("builds:build-detail", pk=build.pk)


class BuildRunStepView(LoginRequiredMixin, View):
	def post(self, request, pk, step):
		build = get_object_or_404(BuildDefinition.objects.select_related("machine_config"), pk=pk)
		if _recover_stale_build_state(build):
			build.refresh_from_db()

		if step not in BuildDefinition.STEP_SEQUENCE:
			messages.error(request, "Unknown build step")
			return redirect("builds:build-detail", pk=build.pk)
		if build.status in {BuildDefinition.STATUS_RUNNING, BuildDefinition.STATUS_QUEUED}:
			messages.error(request, "Cannot run a manual step while the build is queued or running")
			return redirect("builds:build-detail", pk=build.pk)
		if not build.can_run_manual_step(step):
			messages.error(request, "That step is not available yet. Run the previous step first.")
			return redirect("builds:build-detail", pk=build.pk)

		task = run_build_step.delay(build.id, step)
		_set_build_active_task(build, task.id)
		cache.set(build_task_cache_key(build.id), task.id, timeout=6 * 60 * 60)
		messages.success(request, f"Queued manual step: {dict(BuildDefinition.STEP_CHOICES).get(step, step)}")
		return redirect("builds:build-detail", pk=build.pk)


class BuildRerunPlaybooksView(LoginRequiredMixin, View):
	def post(self, request, pk):
		build = get_object_or_404(BuildDefinition.objects.select_related("machine_config"), pk=pk)
		if _recover_stale_build_state(build):
			build.refresh_from_db()

		if build.status in {BuildDefinition.STATUS_RUNNING, BuildDefinition.STATUS_QUEUED}:
			messages.error(request, "Cannot re-run playbooks while build is queued or running")
			return redirect("builds:build-detail", pk=build.pk)

		vm_exists, vm_ssh_ready, vm_ip = _probe_vm_ssh_ready(build)
		if not vm_exists:
			messages.error(request, "Build VM is not running")
			return redirect("builds:build-detail", pk=build.pk)
		if not vm_ssh_ready:
			messages.error(request, "Build VM SSH login is not ready")
			return redirect("builds:build-detail", pk=build.pk)

		task = rerun_build_playbooks.delay(build.id, vm_ip)
		_set_build_active_task(build, task.id)
		cache.set(build_task_cache_key(build.id), task.id, timeout=6 * 60 * 60)
		messages.success(request, "Playbook re-run queued")
		return redirect("builds:build-detail", pk=build.pk)


class BuildConfigListView(LoginRequiredMixin, ListView):
	model = BuildMachineConfig
	template_name = "builds/build_config_list.html"
	context_object_name = "items"


class BuildConfigCreateView(LoginRequiredMixin, CreateView):
	model = BuildMachineConfig
	form_class = BuildMachineConfigForm
	template_name = "builds/build_config_form.html"
	success_url = reverse_lazy("builds:build-config-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["builds/build_config_form_fragment.html"]
		return [self.template_name]

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["modal_mode"] = _is_modal_request(self.request)
		return context

	def form_invalid(self, form):
		if _is_modal_request(self.request):
			return self.render_to_response(self.get_context_data(form=form), status=400)
		return super().form_invalid(form)

	def form_valid(self, form):
		form.instance.libvirt_network = BuildMachineConfig.FIXED_LIBVIRT_NETWORK
		response = super().form_valid(form)
		publish_event("build-config", "created", {"config_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "config_id": self.object.id, "message": "Config saved"})
		return response


class BuildConfigUpdateView(LoginRequiredMixin, UpdateView):
	model = BuildMachineConfig
	form_class = BuildMachineConfigForm
	template_name = "builds/build_config_form.html"
	success_url = reverse_lazy("builds:build-config-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["builds/build_config_form_fragment.html"]
		return [self.template_name]

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["modal_mode"] = _is_modal_request(self.request)
		return context

	def form_invalid(self, form):
		if _is_modal_request(self.request):
			return self.render_to_response(self.get_context_data(form=form), status=400)
		return super().form_invalid(form)

	def form_valid(self, form):
		form.instance.libvirt_network = BuildMachineConfig.FIXED_LIBVIRT_NETWORK
		response = super().form_valid(form)
		publish_event("build-config", "updated", {"config_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "config_id": self.object.id, "message": "Config saved"})
		return response


class BuildConfigDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk):
		obj = get_object_or_404(BuildMachineConfig, pk=pk)
		deleted_id = obj.id
		obj.delete()
		publish_event("build-config", "deleted", {"config_id": deleted_id})
		messages.success(request, "Build machine configuration deleted")
		return redirect("builds:build-config-list")


class BuildArtifactDownloadView(LoginRequiredMixin, View):
	def get(self, request, pk, artifact_id):
		build = get_object_or_404(BuildDefinition, pk=pk)
		artifact = get_object_or_404(BuildArtifact, pk=artifact_id, build=build)

		artifact_root = Path(settings.ARTIFACT_ROOT).resolve()
		artifact_path = Path(artifact.file_path).resolve()

		if artifact_root not in artifact_path.parents and artifact_root != artifact_path:
			raise Http404("Artifact path is outside configured artifact root")

		if artifact_path.is_file():
			return FileResponse(
				artifact_path.open("rb"),
				as_attachment=True,
				filename=artifact_path.name,
			)

		if artifact_path.is_dir():
			tar_path = artifact_path.with_name(f"{artifact_path.name}.tar.gz")
			if not tar_path.exists():
				with tarfile.open(tar_path, mode="w:gz") as tar:
					tar.add(artifact_path, arcname=artifact_path.name)
			return FileResponse(
				tar_path.open("rb"),
				as_attachment=True,
				filename=f"{artifact_path.name}.tar.gz",
			)

		raise Http404("Artifact path does not exist")


class SSHKeyListView(LoginRequiredMixin, ListView):
	model = SSHKey
	template_name = "builds/ssh_key_list.html"
	context_object_name = "items"

	def get_queryset(self):
		return SSHKey.objects.select_related("build", "owner")


class SSHKeyCreateView(LoginRequiredMixin, CreateView):
	model = SSHKey
	form_class = UserSSHKeyForm
	template_name = "builds/ssh_key_form.html"
	success_url = reverse_lazy("builds:ssh-key-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["builds/ssh_key_form_fragment.html"]
		return [self.template_name]

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["modal_mode"] = _is_modal_request(self.request)
		return context

	def form_invalid(self, form):
		if _is_modal_request(self.request):
			return self.render_to_response(self.get_context_data(form=form), status=400)
		messages.error(self.request, form.errors.as_text())
		return self.render_to_response(self.get_context_data(form=form))

	def form_valid(self, form):
		self.object = form.save(owner=self.request.user)
		key = self.object
		messages.success(self.request, f"User SSH key '{key.name}' saved")
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "key_id": self.object.id, "message": "User SSH key saved"})
		return redirect(self.success_url)


class SSHKeyDownloadView(LoginRequiredMixin, View):
	def get(self, request, pk, key_type):
		key = get_object_or_404(SSHKey.objects.select_related("build"), pk=pk)
		if key_type == "private":
			content = key.get_private_key()
			suffix = "private"
		else:
			content = key.public_key.strip()
			suffix = "public"

		if not content:
			messages.error(request, f"Selected SSH key has no {suffix} material stored")
			return redirect("builds:ssh-key-list")

		owner = f"build-{key.build_id}" if key.build_id else "builder"
		filename = f"{owner}-{key.name}-{suffix}.txt"
		response = HttpResponse(content + "\n", content_type="text/plain")
		response["Content-Disposition"] = f'attachment; filename="{filename}"'
		return response


class SSHKeyDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk):
		key = get_object_or_404(SSHKey.objects.select_related("owner", "build"), pk=pk)
		if key.scope != SSHKey.SCOPE_USER or key.owner_id != request.user.id:
			raise PermissionDenied("Only user-managed SSH keys can be deleted")
		key.delete()
		messages.success(request, "SSH key deleted")
		return redirect("builds:ssh-key-list")

