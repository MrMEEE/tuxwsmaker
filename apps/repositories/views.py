from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from apps.realtime.events import publish_event

from .forms import PackageRepositoryForm
from .models import PackageRepository
from .services import render_repository_preview
from .tasks import sync_rhsm_repository_catalog


def _is_modal_request(request) -> bool:
    return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("X-Modal-Request") == "1"

class PackageRepositoryListView(LoginRequiredMixin, ListView):
    model = PackageRepository
    template_name = "repositories/repo_list.html"
    context_object_name = "items"


class PackageRepositoryCreateView(LoginRequiredMixin, CreateView):
    model = PackageRepository
    form_class = PackageRepositoryForm
    template_name = "repositories/repo_form.html"
    success_url = reverse_lazy("repositories:repo-list")

    def get_template_names(self):
        if _is_modal_request(self.request):
            return ["repositories/repo_form_fragment.html"]
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
        publish_event("repositories", "created", {"repository_id": self.object.id})
        if _is_modal_request(self.request):
            return JsonResponse({"ok": True, "repository_id": self.object.id, "message": "Repository saved"})
        return response


class PackageRepositoryUpdateView(LoginRequiredMixin, UpdateView):
    model = PackageRepository
    form_class = PackageRepositoryForm
    template_name = "repositories/repo_form.html"
    success_url = reverse_lazy("repositories:repo-list")

    def get_template_names(self):
        if _is_modal_request(self.request):
            return ["repositories/repo_form_fragment.html"]
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
        publish_event("repositories", "updated", {"repository_id": self.object.id})
        if _is_modal_request(self.request):
            return JsonResponse({"ok": True, "repository_id": self.object.id, "message": "Repository saved"})
        return response


class PackageRepositoryDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(PackageRepository, pk=pk)
        deleted_id = obj.id
        obj.delete()
        publish_event("repositories", "deleted", {"repository_id": deleted_id})
        messages.success(request, "Repository deleted")
        return redirect("repositories:repo-list")


class PackageRepositoryDetailView(LoginRequiredMixin, DetailView):
    model = PackageRepository
    template_name = "repositories/repo_detail.html"
    context_object_name = "repo"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["preview"] = render_repository_preview(self.object)
        return context


class RHSMRepositorySyncView(LoginRequiredMixin, View):
    def post(self, request):
        requested_major_raw = str(request.POST.get("rhel_major") or "").strip()
        versions_override = None
        if requested_major_raw:
            try:
                requested_major = int(requested_major_raw)
                if requested_major > 0:
                    versions_override = [requested_major]
            except ValueError:
                versions_override = None

        result = sync_rhsm_repository_catalog(versions_override=versions_override)
        if _is_modal_request(request) or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "result": result})
        messages.success(request, "RHSM repository catalog synchronized")
        return redirect("builds:build-list")