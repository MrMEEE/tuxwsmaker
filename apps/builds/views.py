from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
import tarfile
import tempfile
from pathlib import Path

from apps.workers.tasks import run_build_definition
from apps.workers.tasks import build_task_cache_key
from apps.serverconfig.models import ServerConfiguration
from apps.realtime.events import publish_event
from config.celery import app as celery_app

from .forms import BuildDefinitionForm, BuildMachineConfigForm, UserSSHKeyForm
from .models import BuildArtifact, BuildDefinition, BuildLogEntry, BuildMachineConfig, BuildPlaybookSelection, SSHKey
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
		vm_name = f"build-{build.id}"
		vm_exists = False
		try:
			vm_manager = LibvirtVMManager(uri=build.machine_config.hypervisor_uri)
			vm_exists = vm_manager.domain_exists(vm_name)
		except Exception:
			vm_exists = False
		context["build_vm_exists"] = vm_exists
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

		build.status = BuildDefinition.STATUS_DRAFT
		build.save(update_fields=["status", "updated_at"])
		BuildLogEntry.objects.create(build=build, stage="cancel", message="Build cancelled by user")
		publish_event("builds", "cancelled", {"build_id": build.id, "status": build.status})
		messages.success(request, "Build cancelled")
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
			tmp = tempfile.NamedTemporaryFile(prefix=f"build-{build.id}-", suffix=".tar.gz", delete=False)
			tmp.close()
			tar_path = Path(tmp.name)
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

