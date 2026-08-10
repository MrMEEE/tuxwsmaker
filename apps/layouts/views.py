from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View
import json
import yaml

from apps.realtime.events import publish_event

from .forms import PartitionEntryForm, PartitionLayoutForm, YAMLFallbackForm
from .models import PartitionEntry
from .models import PartitionLayout


def _is_modal_request(request) -> bool:
	return request.GET.get("modal") == "1" or request.POST.get("modal") == "1" or request.headers.get("X-Modal-Request") == "1"


def _to_bool(value):
	if isinstance(value, bool):
		return value
	if value is None:
		return False
	return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_table_type(raw_value):
	val = str(raw_value or "").strip().lower()
	if val in {PartitionLayout.TABLE_GPT, PartitionLayout.TABLE_MBR}:
		return val
	return PartitionLayout.TABLE_GPT


def _normalize_yaml_entries(parsed_layout):
	if not isinstance(parsed_layout, dict):
		raise ValueError("YAML root must be a mapping")

	table_type = _normalize_table_type(parsed_layout.get("table_type"))
	entries = parsed_layout.get("entries")
	if entries is None:
		entries = []
	if not isinstance(entries, list):
		raise ValueError("entries must be a list")

	normalized = []
	for idx, item in enumerate(entries, start=1):
		if not isinstance(item, dict):
			raise ValueError(f"entries[{idx}] must be a mapping")

		size_mode = str(item.get("size_mode") or PartitionEntry.SIZE_FIXED).strip().lower()
		if size_mode not in {PartitionEntry.SIZE_FIXED, PartitionEntry.SIZE_REMAINDER}:
			raise ValueError(f"entries[{idx}] has invalid size_mode")

		size_mib = item.get("size_mib")
		if size_mode == PartitionEntry.SIZE_FIXED:
			try:
				size_mib = int(size_mib)
			except (TypeError, ValueError):
				raise ValueError(f"entries[{idx}] fixed size requires integer size_mib")
		else:
			size_mib = None

		partition_number = int(item.get("partition_number", item.get("number", idx)) or idx)
		normalized.append(
			{
				"order": idx,
				"partition_number": partition_number,
				"name": str(item.get("name", "")).strip(),
				"entry_role": str(item.get("role", item.get("entry_role", PartitionEntry.ROLE_STANDARD))).strip().lower(),
				"mount_point": str(item.get("mount_point", "")).strip(),
				"filesystem": str(item.get("filesystem", "ext4")).strip().lower(),
				"size_mode": size_mode,
				"size_mib": size_mib,
				"gpt_type": str(item.get("type_code", item.get("gpt_type", ""))).strip(),
				"volume_group": str(item.get("volume_group", "")).strip(),
				"logical_volume": str(item.get("logical_volume", "")).strip(),
				"is_boot": _to_bool(item.get("bootable", item.get("is_boot", False))),
				"luks_enabled": _to_bool(item.get("luks_enabled", False)),
				"luks_name": str(item.get("luks_name", "")).strip(),
			}
		)

	return table_type, normalized


def _render_layout_yaml(layout, entries):
	payload = {
		"table_type": layout.table_type,
		"entries": [],
	}
	for entry in entries:
		payload["entries"].append(
			{
				"order": entry.order,
				"partition_number": entry.partition_number,
				"name": entry.name,
				"role": entry.entry_role,
				"mount_point": entry.mount_point,
				"filesystem": entry.filesystem,
				"size_mode": entry.size_mode,
				"size_mib": entry.size_mib if entry.size_mode == PartitionEntry.SIZE_FIXED else None,
				"type_code": entry.gpt_type,
				"volume_group": entry.volume_group,
				"logical_volume": entry.logical_volume,
				"bootable": entry.is_boot,
				"luks_enabled": entry.luks_enabled,
				"luks_name": entry.luks_name,
			}
		)
	return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)


def _sync_layout_yaml(layout):
	entries = list(layout.entries.order_by("order"))
	layout.yaml_fallback = _render_layout_yaml(layout, entries)
	layout.save(update_fields=["yaml_fallback", "updated_at"])


class PartitionLayoutListView(LoginRequiredMixin, ListView):
	model = PartitionLayout
	template_name = "layouts/layout_list.html"
	context_object_name = "items"


class PartitionLayoutCreateView(LoginRequiredMixin, CreateView):
	model = PartitionLayout
	form_class = PartitionLayoutForm
	template_name = "layouts/layout_form.html"
	success_url = reverse_lazy("layouts:layout-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["layouts/layout_form_fragment.html"]
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
		publish_event("layouts", "created", {"layout_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "layout_id": self.object.id, "message": "Partition layout saved"})
		return response


class PartitionLayoutUpdateView(LoginRequiredMixin, UpdateView):
	model = PartitionLayout
	form_class = PartitionLayoutForm
	template_name = "layouts/layout_form.html"
	success_url = reverse_lazy("layouts:layout-list")

	def get_template_names(self):
		if _is_modal_request(self.request):
			return ["layouts/layout_form_fragment.html"]
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
		publish_event("layouts", "updated", {"layout_id": self.object.id})
		if _is_modal_request(self.request):
			return JsonResponse({"ok": True, "layout_id": self.object.id, "message": "Partition layout saved"})
		return response


class PartitionLayoutDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk):
		obj = get_object_or_404(PartitionLayout, pk=pk)
		deleted_id = obj.id
		obj.delete()
		publish_event("layouts", "deleted", {"layout_id": deleted_id})
		messages.success(request, "Partition layout deleted")
		return redirect("layouts:layout-list")


class PartitionLayoutDetailView(LoginRequiredMixin, DetailView):
	model = PartitionLayout
	template_name = "layouts/layout_detail.html"
	context_object_name = "layout"

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context["entry_form"] = PartitionEntryForm()
		context["yaml_form"] = YAMLFallbackForm(instance=self.object)
		context["table_type_choices"] = PartitionLayout.TABLE_CHOICES
		context["filesystem_choices"] = json.dumps([{"value": v, "label": l} for v, l in PartitionEntry.FILESYSTEM_CHOICES])
		context["entry_role_choices"] = json.dumps([{"value": v, "label": l} for v, l in PartitionEntry.ROLE_CHOICES])
		context["gpt_type_choices"] = json.dumps([{"value": v, "label": l} for v, l in PartitionEntry.GPT_TYPE_CHOICES])
		context["mbr_type_choices"] = json.dumps([{"value": v, "label": l} for v, l in PartitionEntry.MBR_TYPE_CHOICES])
		return context


class PartitionEntryCreateView(LoginRequiredMixin, View):
	def post(self, request, pk):
		layout = get_object_or_404(PartitionLayout, pk=pk)
		form = PartitionEntryForm(request.POST)
		if form.is_valid():
			entry = form.save(commit=False)
			entry.layout = layout
			entry.save()
			_sync_layout_yaml(layout)
			publish_event("layouts", "entry-created", {"layout_id": layout.id, "entry_id": entry.id})
			messages.success(request, "Partition entry added")
		else:
			messages.error(request, form.errors.as_text())
		return redirect("layouts:layout-detail", pk=layout.pk)


class PartitionEntryUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk, entry_id):
		layout = get_object_or_404(PartitionLayout, pk=pk)
		entry = get_object_or_404(PartitionEntry, pk=entry_id, layout=layout)
		form = PartitionEntryForm(request.POST, instance=entry)
		if form.is_valid():
			form.save()
			_sync_layout_yaml(layout)
			publish_event("layouts", "entry-updated", {"layout_id": layout.id, "entry_id": entry.id})
			messages.success(request, "Partition entry updated")
		else:
			messages.error(request, form.errors.as_text())
		return redirect("layouts:layout-detail", pk=layout.pk)


class PartitionEntryDeleteView(LoginRequiredMixin, View):
	def post(self, request, pk, entry_id):
		layout = get_object_or_404(PartitionLayout, pk=pk)
		entry = get_object_or_404(PartitionEntry, pk=entry_id, layout=layout)
		deleted_entry_id = entry.id
		entry.delete()
		_sync_layout_yaml(layout)
		publish_event("layouts", "entry-deleted", {"layout_id": layout.id, "entry_id": deleted_entry_id})
		messages.success(request, "Partition entry deleted")
		return redirect("layouts:layout-detail", pk=layout.pk)


class PartitionYAMLUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk):
		layout = get_object_or_404(PartitionLayout, pk=pk)
		form = YAMLFallbackForm(request.POST, instance=layout)
		if form.is_valid():
			yaml_text = form.cleaned_data.get("yaml_fallback", "")
			try:
				parsed = yaml.safe_load(yaml_text) or {}
				table_type, new_entries_data = _normalize_yaml_entries(parsed)
			except Exception as exc:
				messages.error(request, f"YAML parse failed: {exc}")
				return redirect("layouts:layout-detail", pk=layout.pk)

			new_entries = []
			for idx, item in enumerate(new_entries_data, start=1):
				entry = PartitionEntry(layout=layout, **item)
				try:
					entry.full_clean(validate_unique=False)
				except Exception as exc:
					messages.error(request, f"YAML entry {idx} is invalid: {exc}")
					return redirect("layouts:layout-detail", pk=layout.pk)
				new_entries.append(entry)

			with transaction.atomic():
				layout.table_type = table_type
				layout.yaml_fallback = yaml_text
				layout.save(update_fields=["table_type", "yaml_fallback", "updated_at"])
				layout.entries.all().delete()
				PartitionEntry.objects.bulk_create(new_entries)

			publish_event("layouts", "yaml-updated", {"layout_id": layout.id, "count": len(new_entries)})
			messages.success(request, "YAML updated and partition table synchronized")
		else:
			messages.error(request, form.errors.as_text())
		return redirect("layouts:layout-detail", pk=layout.pk)


class PartitionYAMLPreviewView(LoginRequiredMixin, View):
	def post(self, request, pk):
		layout = get_object_or_404(PartitionLayout, pk=pk)
		yaml_text = request.POST.get("yaml_fallback", "")
		try:
			parsed = yaml.safe_load(yaml_text) or {}
			table_type, new_entries_data = _normalize_yaml_entries(parsed)
		except Exception as exc:
			return JsonResponse({"ok": False, "error": str(exc)}, status=400)

		validated_entries = []
		for idx, item in enumerate(new_entries_data, start=1):
			entry = PartitionEntry(layout=layout, **item)
			try:
				entry.full_clean(validate_unique=False)
			except Exception as exc:
				return JsonResponse({"ok": False, "error": f"Entry {idx} is invalid: {exc}"}, status=400)

			validated_entries.append(
				{
					"order": entry.order,
					"name": entry.name,
					"entry_role": entry.entry_role,
					"mount_point": entry.mount_point,
					"filesystem": entry.filesystem,
					"size_mode": entry.size_mode,
					"size_mib": entry.size_mib,
					"gpt_type": entry.gpt_type,
					"volume_group": entry.volume_group,
					"logical_volume": entry.logical_volume,
					"is_boot": entry.is_boot,
					"luks_enabled": entry.luks_enabled,
					"luks_name": entry.luks_name,
				}
			)

		return JsonResponse({"ok": True, "table_type": table_type, "entries": validated_entries})


class PartitionEntriesBulkUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk):
		layout = get_object_or_404(PartitionLayout, pk=pk)
		table_type = _normalize_table_type(request.POST.get("table_type", layout.table_type))
		raw_entries = request.POST.get("entries_json", "")
		if not raw_entries:
			messages.error(request, "No partition data was submitted")
			return redirect("layouts:layout-detail", pk=layout.pk)

		try:
			parsed = json.loads(raw_entries)
		except json.JSONDecodeError:
			messages.error(request, "Invalid partition JSON payload")
			return redirect("layouts:layout-detail", pk=layout.pk)

		if not isinstance(parsed, list):
			messages.error(request, "Partition payload must be a list")
			return redirect("layouts:layout-detail", pk=layout.pk)

		new_entries = []
		for idx, item in enumerate(parsed, start=1):
			if not isinstance(item, dict):
				messages.error(request, f"Invalid entry payload at index {idx}")
				return redirect("layouts:layout-detail", pk=layout.pk)

			size_mode = item.get("size_mode", PartitionEntry.SIZE_FIXED)
			size_mib = item.get("size_mib")
			if size_mode == PartitionEntry.SIZE_FIXED:
				try:
					size_mib = int(size_mib)
				except (TypeError, ValueError):
					messages.error(request, f"Fixed partition at index {idx} requires integer size_mib")
					return redirect("layouts:layout-detail", pk=layout.pk)
			elif size_mode == PartitionEntry.SIZE_REMAINDER:
				size_mib = None
			else:
				messages.error(request, f"Invalid size mode at index {idx}")
				return redirect("layouts:layout-detail", pk=layout.pk)

			entry = PartitionEntry(
				layout=layout,
				order=idx,
				partition_number=int(item.get("partition_number", item.get("number", idx)) or idx),
				name=str(item.get("name", "")).strip(),
				entry_role=str(item.get("entry_role", PartitionEntry.ROLE_STANDARD)).strip().lower(),
				mount_point=str(item.get("mount_point", "")).strip(),
				filesystem=str(item.get("filesystem", "")).strip(),
				size_mode=size_mode,
				size_mib=size_mib,
				gpt_type=str(item.get("gpt_type", "")).strip(),
				volume_group=str(item.get("volume_group", "")).strip(),
				logical_volume=str(item.get("logical_volume", "")).strip(),
				luks_enabled=bool(item.get("luks_enabled", False)),
				luks_name=str(item.get("luks_name", "")).strip(),
			)
			try:
				entry.full_clean(validate_unique=False)
			except Exception as exc:
				messages.error(request, f"Entry {idx} is invalid: {exc}")
				return redirect("layouts:layout-detail", pk=layout.pk)
			new_entries.append(entry)

		with transaction.atomic():
			layout.table_type = table_type
			layout.save(update_fields=["table_type", "updated_at"])
			layout.entries.all().delete()
			PartitionEntry.objects.bulk_create(new_entries)
			created_entries = list(layout.entries.order_by("order"))
			layout.yaml_fallback = _render_layout_yaml(layout, created_entries)
			layout.save(update_fields=["yaml_fallback", "updated_at"])

		publish_event("layouts", "bulk-updated", {"layout_id": layout.id, "count": len(new_entries), "table_type": table_type})
		messages.success(request, "Partition layout updated")
		return redirect("layouts:layout-detail", pk=layout.pk)
