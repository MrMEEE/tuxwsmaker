from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from apps.realtime.events import publish_event

from .forms import PackageItemsBulkForm, PackageListForm
from .models import PackageItem
from .models import PackageList


def _is_modal_request(request) -> bool:
	return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("X-Modal-Request") == "1"


class PackageListView(LoginRequiredMixin, ListView):
	model = PackageList
	template_name = "packages/package_list.html"
	context_object_name = "items"


class PackageListCreateView(LoginRequiredMixin, CreateView):
	model = PackageList
	form_class = PackageListForm
	template_name = "packages/package_form.html"
	success_url = reverse_lazy("packages:package-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["packages/package_form_fragment.html"]
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
		publish_event("packages", "created", {"package_list_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "package_list_id": self.object.id, "message": "Package list saved"})
		return response


class PackageListUpdateView(LoginRequiredMixin, UpdateView):
	model = PackageList
	form_class = PackageListForm
	template_name = "packages/package_form.html"
	success_url = reverse_lazy("packages:package-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["packages/package_form_fragment.html"]
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
		publish_event("packages", "updated", {"package_list_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "package_list_id": self.object.id, "message": "Package list saved"})
		return response


class PackageListDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk):
		obj = get_object_or_404(PackageList, pk=pk)
		deleted_id = obj.id
		obj.delete()
		publish_event("packages", "deleted", {"package_list_id": deleted_id})
		messages.success(request, "Package list deleted")
		return redirect("packages:package-list")


class PackageListDetailView(LoginRequiredMixin, DetailView):
	model = PackageList
	template_name = "packages/package_detail.html"
	context_object_name = "package_list"

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["bulk_form"] = PackageItemsBulkForm(
			initial={
				"data": "\n".join(item.package_name for item in self.object.items.all()),
			}
		)
		return context


class PackageItemsUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk):
		obj = get_object_or_404(PackageList, pk=pk)
		form = PackageItemsBulkForm(request.POST)
		if not form.is_valid():
			messages.error(request, form.errors.as_text())
			return redirect("packages:package-detail", pk=obj.pk)

		try:
			packages = PackageItemsBulkForm.parse(form.cleaned_data["data"])
		except forms.ValidationError as exc:
			messages.error(request, str(exc))
			return redirect("packages:package-detail", pk=obj.pk)

		obj.items.all().delete()
		PackageItem.objects.bulk_create([PackageItem(package_list=obj, package_name=pkg) for pkg in packages])
		publish_event("packages", "items-updated", {"package_list_id": obj.id, "count": len(packages)})
		messages.success(request, "Package items updated")
		return redirect("packages:package-detail", pk=obj.pk)
