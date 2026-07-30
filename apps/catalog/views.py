import logging
import os
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django import forms
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie

from apps.realtime.events import publish_event
from apps.builds.services.artifacts import prepare_iso_pxe_assets, remove_iso_pxe_assets

from .forms import ISOQuickEditForm, MultiISOUploadForm, OperatingSystemForm, VariableBulkForm
from .models import ISOImage, ISOVariable, OSVariable
from .models import OperatingSystem


def _is_modal_request(request) -> bool:
	return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("X-Modal-Request") == "1"


logger = logging.getLogger(__name__)
class OSListView(LoginRequiredMixin, ListView):
	model = OperatingSystem
	template_name = "catalog/os_list.html"
	context_object_name = "items"


class OSCreateView(LoginRequiredMixin, CreateView):
	model = OperatingSystem
	form_class = OperatingSystemForm
	template_name = "catalog/os_form.html"
	success_url = reverse_lazy("catalog:os-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["catalog/os_form_fragment.html"]
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
		publish_event("catalog", "created", {"os_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "os_id": self.object.id, "message": "Operating system saved"})
		return response


class OSUpdateView(LoginRequiredMixin, UpdateView):
	model = OperatingSystem
	form_class = OperatingSystemForm
	template_name = "catalog/os_form.html"
	success_url = reverse_lazy("catalog:os-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["catalog/os_form_fragment.html"]
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
		publish_event("catalog", "updated", {"os_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "os_id": self.object.id, "message": "Operating system saved"})
		return response


class OSDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk):
		obj = get_object_or_404(OperatingSystem, pk=pk)
		deleted_id = obj.id
		obj.delete()
		publish_event("catalog", "deleted", {"os_id": deleted_id})
		messages.success(request, "Operating system deleted")
		return redirect("catalog:os-list")


@method_decorator(ensure_csrf_cookie, name="dispatch")
class OSDetailView(LoginRequiredMixin, DetailView):
	model = OperatingSystem
	template_name = "catalog/os_detail.html"
	context_object_name = "os_item"

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["upload_form"] = MultiISOUploadForm()
		context["os_var_form"] = VariableBulkForm(
			initial={
				"data": "\n".join(f"{v.key}={v.value}" for v in self.object.variables.all()),
			}
		)
		return context


@method_decorator(csrf_protect, name="dispatch")
class OSVariableUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk):
		os_obj = get_object_or_404(OperatingSystem, pk=pk)
		form = VariableBulkForm(request.POST)
		if not form.is_valid():
			messages.error(request, form.errors.as_text())
			return redirect("catalog:os-detail", pk=os_obj.pk)

		try:
			pairs = VariableBulkForm.parse(form.cleaned_data["data"])
		except forms.ValidationError as exc:
			messages.error(request, str(exc))
			return redirect("catalog:os-detail", pk=os_obj.pk)
		os_obj.variables.all().delete()
		OSVariable.objects.bulk_create(
			[OSVariable(operating_system=os_obj, key=key, value=value) for key, value in pairs.items()]
		)
		publish_event("catalog", "variables-updated", {"os_id": os_obj.id})
		messages.success(request, "OS variables updated")
		return redirect("catalog:os-detail", pk=os_obj.pk)


@method_decorator(csrf_protect, name="dispatch")
class ISOUploadView(LoginRequiredMixin, View):
	def post(self, request, pk):
		os_obj = get_object_or_404(OperatingSystem, pk=pk)
		wants_json = request.headers.get("x-requested-with") == "XMLHttpRequest"
		logger.info(
			"iso_upload_start os_id=%s user=%s content_length=%s transfer_encoding=%s content_type=%s ajax=%s upload_mode=%s hinted_file_count=%s hinted_bytes=%s file_keys=%s",
			os_obj.id,
			request.user.username,
			request.META.get("CONTENT_LENGTH", ""),
			request.META.get("HTTP_TRANSFER_ENCODING", ""),
			request.META.get("CONTENT_TYPE", ""),
			wants_json,
			request.headers.get("X-Upload-Mode", ""),
			request.headers.get("X-Upload-File-Count", ""),
			request.headers.get("X-Upload-Bytes", ""),
			list(request.FILES.keys()),
		)

		def _error(message: str, status: int = 400):
			if wants_json:
				return JsonResponse({"ok": False, "error": message}, status=status)
			messages.error(request, message)
			return redirect("catalog:os-detail", pk=os_obj.pk)

		def _success(message: str):
			if wants_json:
				return JsonResponse({"ok": True, "message": message})
			messages.success(request, message)
			return redirect("catalog:os-detail", pk=os_obj.pk)

		files = request.FILES.getlist("files") or request.FILES.getlist("files[]")
		if not files:
			files = [f for f in request.FILES.values() if f]

		if not files:
			keys = ", ".join(request.FILES.keys()) or "none"
			logger.warning("iso_upload_no_files os_id=%s user=%s request_file_keys=%s", os_obj.id, request.user.username, keys)
			return _error(f"Select one or more ISO files (received upload fields: {keys})")

		versions_raw = request.POST.get("versions", "")
		versions_input = [line.strip() for line in versions_raw.splitlines() if line.strip()]
		if versions_input and len(versions_input) != len(files):
			logger.warning(
				"iso_upload_version_mismatch os_id=%s user=%s files=%s versions=%s",
				os_obj.id,
				request.user.username,
				len(files),
				len(versions_input),
			)
			return _error("Version line count must match number of uploaded files")

		versions = versions_input or [Path(file_obj.name).stem for file_obj in files]
		os_versions_raw = request.POST.get("os_versions", "")
		os_versions_input = [line.strip() for line in os_versions_raw.splitlines() if line.strip()]
		if os_versions_input and len(os_versions_input) != len(files):
			logger.warning(
				"iso_upload_os_version_mismatch os_id=%s user=%s files=%s os_versions=%s",
				os_obj.id,
				request.user.username,
				len(files),
				len(os_versions_input),
			)
			return _error("OS version line count must match number of uploaded files")

		os_versions = os_versions_input or versions

		uploaded = 0
		for file_obj, version, os_version in zip(files, versions, os_versions):
			try:
				logger.info(
					"iso_upload_saving os_id=%s user=%s filename=%s bytes=%s version=%s os_version=%s",
					os_obj.id,
					request.user.username,
					file_obj.name,
					getattr(file_obj, "size", ""),
					version,
					os_version,
				)
				iso_obj = ISOImage.objects.create(
					operating_system=os_obj,
					version=version,
					os_version=os_version,
					iso_file=file_obj,
				)
				prepare_iso_pxe_assets(iso_path=Path(iso_obj.iso_file.path))
				uploaded += 1
			except IntegrityError:
				logger.warning(
					"iso_upload_duplicate os_id=%s user=%s version=%s",
					os_obj.id,
					request.user.username,
					version,
				)
				if not wants_json:
					messages.warning(request, f"Skipped duplicate version: {version}")

		logger.info("iso_upload_done os_id=%s user=%s uploaded=%s", os_obj.id, request.user.username, uploaded)

		publish_event("catalog", "iso-uploaded", {"os_id": os_obj.id, "count": uploaded})
		return _success(f"Uploaded {uploaded} ISO file(s)")


@method_decorator(csrf_protect, name="dispatch")
class ISOUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk, iso_id):
		os_obj = get_object_or_404(OperatingSystem, pk=pk)
		iso = get_object_or_404(ISOImage, pk=iso_id, operating_system=os_obj)
		form = ISOQuickEditForm(request.POST, instance=iso)
		if not form.is_valid():
			messages.error(request, form.errors.as_text())
			return redirect("catalog:os-detail", pk=os_obj.pk)

		try:
			form.save()
		except IntegrityError:
			messages.error(request, f"Duplicate version: {form.cleaned_data['version']}")
			return redirect("catalog:os-detail", pk=os_obj.pk)

		publish_event("catalog", "iso-updated", {"os_id": os_obj.id, "iso_id": iso.id})
		messages.success(request, f"Updated ISO {iso.version}")
		return redirect("catalog:os-detail", pk=os_obj.pk)


@method_decorator(csrf_protect, name="dispatch")
class ISODeleteView(LoginRequiredMixin, View):
	def post(self, request, pk, iso_id):
		os_obj = get_object_or_404(OperatingSystem, pk=pk)
		iso = get_object_or_404(ISOImage, pk=iso_id, operating_system=os_obj)
		deleted_iso_id = iso.id
		remove_iso_pxe_assets(iso_path=Path(iso.iso_file.path))
		iso.iso_file.delete(save=False)
		iso.delete()
		publish_event("catalog", "iso-deleted", {"os_id": os_obj.id, "iso_id": deleted_iso_id})
		messages.success(request, "ISO deleted")
		return redirect("catalog:os-detail", pk=os_obj.pk)


@method_decorator(csrf_protect, name="dispatch")
class ISOVariableUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk, iso_id):
		os_obj = get_object_or_404(OperatingSystem, pk=pk)
		iso = get_object_or_404(ISOImage, pk=iso_id, operating_system=os_obj)
		form = VariableBulkForm(request.POST)
		if not form.is_valid():
			messages.error(request, form.errors.as_text())
			return redirect("catalog:os-detail", pk=os_obj.pk)

		try:
			pairs = VariableBulkForm.parse(form.cleaned_data["data"])
		except forms.ValidationError as exc:
			messages.error(request, str(exc))
			return redirect("catalog:os-detail", pk=os_obj.pk)
		iso.variables.all().delete()
		ISOVariable.objects.bulk_create(
			[ISOVariable(iso=iso, key=key, value=value) for key, value in pairs.items()]
		)
		publish_event("catalog", "iso-variables-updated", {"os_id": os_obj.id, "iso_id": iso.id})
		messages.success(request, f"Variables updated for ISO {iso.version}")
		return redirect("catalog:os-detail", pk=os_obj.pk)
