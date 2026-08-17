from __future__ import annotations

from django.db import transaction
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView, View

from .forms import AfterburnerItemForm, AfterburnerProfileForm, AfterburnerScriptInputForm
from .models import AfterburnerItem, AfterburnerProfile, AfterburnerScriptInput


def _sync_custom_script_inputs(*, item: AfterburnerItem, payload: list[dict[str, object]]) -> None:
    if item.item_type != AfterburnerItem.TYPE_CUSTOM_SCRIPT:
        return
    rows = payload or []
    with transaction.atomic():
        item.script_inputs.all().delete()
        if not rows:
            return
        AfterburnerScriptInput.objects.bulk_create(
            [
                AfterburnerScriptInput(
                    item=item,
                    order=int(row.get("order") or idx + 1),
                    key=str(row.get("key") or "").strip().upper(),
                    label=str(row.get("label") or "").strip(),
                    input_type=str(row.get("input_type") or AfterburnerScriptInput.TYPE_STRING).strip(),
                    required=bool(row.get("required")),
                    default_value=str(row.get("default_value") or ""),
                    answer_key=str(row.get("answer_key") or "").strip(),
                    select_options=list(row.get("select_options") or []),
                    description=str(row.get("description") or "").strip(),
                )
                for idx, row in enumerate(rows)
            ]
        )


def _sync_shared_luks_tpm_answer_keys(*, item: AfterburnerItem) -> None:
    if item.item_type not in {AfterburnerItem.TYPE_LUKS_ROTATE, AfterburnerItem.TYPE_TPM_INTEGRATION}:
        return

    cfg = dict(item.config or {})
    shared_autodetect_key = str(cfg.get("luks_autodetect_answer_key") or cfg.get("tpm_autodetect_answer_key") or "").strip()
    shared_password_key = str(cfg.get("luks_new_password_answer_key") or cfg.get("tpm_password_answer_key") or "").strip()

    target_items = list(
        AfterburnerItem.objects.filter(
            profile_id=item.profile_id,
            item_type__in=[AfterburnerItem.TYPE_LUKS_ROTATE, AfterburnerItem.TYPE_TPM_INTEGRATION],
        )
    )
    to_update: list[AfterburnerItem] = []
    for target in target_items:
        target_cfg = dict(target.config or {})
        target_cfg["luks_autodetect_answer_key"] = shared_autodetect_key
        target_cfg["luks_new_password_answer_key"] = shared_password_key
        target_cfg.pop("tpm_autodetect_answer_key", None)
        target_cfg.pop("tpm_password_answer_key", None)
        if target_cfg != (target.config or {}):
            target.config = target_cfg
            to_update.append(target)

    if to_update:
        AfterburnerItem.objects.bulk_update(to_update, ["config"])


def _custom_script_example_lines(item: AfterburnerItem | None) -> list[str]:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
    ]
    if item is None:
        lines.extend([
            "# Add custom inputs to expose environment variables here.",
            'echo "Deploying ${ENVIRONMENT}"',
        ])
        return lines

    inputs = list(item.script_inputs.all().order_by("order", "id"))
    if not inputs:
        lines.extend([
            "# Add custom inputs to expose environment variables here.",
            'echo "Running custom script"',
        ])
        return lines

    lines.append("# Custom inputs are available as environment variables:")
    for input_row in inputs:
        lines.append(f"#   {input_row.key}")
    lines.append("")

    for input_row in inputs:
        if input_row.input_type == AfterburnerScriptInput.TYPE_PASSWORD:
            lines.append(f"# {input_row.key} is a secret. Do not print or log it.")
            lines.append(f': "${{{input_row.key}:?{input_row.key} is required}}"')
        else:
            lines.append(f'echo "Using ${{{input_row.key}}}"')
        lines.append("")
    return lines


def _resequence_orders(model_cls, objects: list) -> None:
    if not objects:
        return

    max_order = max((obj.order or 0) for obj in objects)
    temp_base = max_order + len(objects) + 100

    with transaction.atomic():
        for idx, obj in enumerate(objects, start=1):
            obj.order = temp_base + idx
        model_cls.objects.bulk_update(objects, ["order"])

        for idx, obj in enumerate(objects, start=1):
            obj.order = idx
        model_cls.objects.bulk_update(objects, ["order"])


class AfterburnerProfileListView(LoginRequiredMixin, ListView):
    model = AfterburnerProfile
    template_name = "afterburners/profile_list.html"
    context_object_name = "items"


class AfterburnerProfileDetailView(LoginRequiredMixin, DetailView):
    model = AfterburnerProfile
    template_name = "afterburners/profile_detail.html"
    context_object_name = "profile"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["item_create_form"] = kwargs.get("item_create_form") or AfterburnerItemForm(prefix="create")
        items = list(context["profile"].items.all().order_by("order", "id"))
        for item in items:
            item.edit_form = AfterburnerItemForm(instance=item, prefix=f"item-{item.id}")
            item.custom_script_example_lines = _custom_script_example_lines(item) if item.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT else []
        context["ordered_items"] = items
        context["item_create_script_example_lines"] = _custom_script_example_lines(None)
        return context


class AfterburnerProfileCreateView(LoginRequiredMixin, CreateView):
    model = AfterburnerProfile
    form_class = AfterburnerProfileForm
    template_name = "afterburners/profile_form.html"
    success_url = reverse_lazy("afterburners:profile-list")


class AfterburnerProfileUpdateView(LoginRequiredMixin, UpdateView):
    model = AfterburnerProfile
    form_class = AfterburnerProfileForm
    template_name = "afterburners/profile_form.html"
    success_url = reverse_lazy("afterburners:profile-list")


class AfterburnerProfileDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        profile = get_object_or_404(AfterburnerProfile, pk=pk)
        profile.delete()
        messages.success(request, "Afterburner deleted")
        return redirect("afterburners:profile-list")


class AfterburnerItemCreateView(LoginRequiredMixin, CreateView):
    model = AfterburnerItem
    form_class = AfterburnerItemForm
    template_name = "afterburners/item_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.profile = get_object_or_404(AfterburnerProfile, pk=self.kwargs["profile_id"])
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        form.instance.profile = self.profile
        max_order = self.profile.items.order_by("-order").values_list("order", flat=True).first() or 0
        form.instance.order = max_order + 1
        response = super().form_valid(form)
        _sync_shared_luks_tpm_answer_keys(item=self.object)
        _sync_custom_script_inputs(
            item=self.object,
            payload=form.cleaned_data.get("item_script_inputs_payload") or [],
        )
        messages.success(self.request, "Afterburner item added")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["profile_id"] = self.profile.id
        context["custom_script_example_lines"] = _custom_script_example_lines(None)
        return context

    def get_success_url(self):
        return reverse_lazy("afterburners:profile-detail", kwargs={"pk": self.profile.id})


class AfterburnerItemUpdateView(LoginRequiredMixin, UpdateView):
    model = AfterburnerItem
    form_class = AfterburnerItemForm
    template_name = "afterburners/item_form.html"

    def get_success_url(self):
        return reverse_lazy("afterburners:profile-detail", kwargs={"pk": self.object.profile_id})

    def form_valid(self, form):
        response = super().form_valid(form)
        _sync_shared_luks_tpm_answer_keys(item=self.object)
        _sync_custom_script_inputs(
            item=self.object,
            payload=form.cleaned_data.get("item_script_inputs_payload") or [],
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["profile_id"] = self.object.profile_id
        context["custom_script_example_lines"] = _custom_script_example_lines(self.object if self.object.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT else None)
        return context


class AfterburnerItemInlineCreateView(LoginRequiredMixin, View):
    def post(self, request, profile_id):
        profile = get_object_or_404(AfterburnerProfile, pk=profile_id)
        form = AfterburnerItemForm(request.POST, prefix="create")
        if form.is_valid():
            item = form.save(commit=False)
            item.profile = profile
            max_order = profile.items.order_by("-order").values_list("order", flat=True).first() or 0
            item.order = max_order + 1
            item.save()
            _sync_shared_luks_tpm_answer_keys(item=item)
            _sync_custom_script_inputs(
                item=item,
                payload=form.cleaned_data.get("item_script_inputs_payload") or [],
            )
            messages.success(request, "Afterburner item added")
        else:
            errors = "; ".join(form.errors.get("__all__", []))
            if not errors:
                errors = "; ".join(
                    f"{field}: {', '.join(msgs)}"
                    for field, msgs in form.errors.items()
                    if field != "__all__"
                )
            messages.error(request, errors or "Unable to add item")
        return redirect("afterburners:profile-detail", pk=profile.id)


class AfterburnerItemInlineUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        item = get_object_or_404(AfterburnerItem, pk=pk)
        form = AfterburnerItemForm(request.POST, instance=item, prefix=f"item-{item.id}")
        if form.is_valid():
            item = form.save()
            _sync_shared_luks_tpm_answer_keys(item=item)
            _sync_custom_script_inputs(
                item=item,
                payload=form.cleaned_data.get("item_script_inputs_payload") or [],
            )
            messages.success(request, "Afterburner item updated")
        else:
            errors = "; ".join(form.errors.get("__all__", []))
            if not errors:
                errors = "; ".join(
                    f"{field}: {', '.join(msgs)}"
                    for field, msgs in form.errors.items()
                    if field != "__all__"
                )
            messages.error(request, errors or "Unable to update item")
        return redirect("afterburners:profile-detail", pk=item.profile_id)


class AfterburnerItemDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        item = get_object_or_404(AfterburnerItem, pk=pk)
        profile_id = item.profile_id
        item.delete()
        messages.success(request, "Afterburner item deleted")
        return redirect("afterburners:profile-detail", pk=profile_id)


class AfterburnerItemMoveView(LoginRequiredMixin, View):
    def post(self, request, pk, direction):
        item = get_object_or_404(AfterburnerItem, pk=pk)
        siblings = list(AfterburnerItem.objects.filter(profile=item.profile).order_by("order", "id"))
        idx = next((i for i, v in enumerate(siblings) if v.id == item.id), None)
        if idx is None:
            return redirect("afterburners:profile-detail", pk=item.profile_id)

        target = idx - 1 if direction == "up" else idx + 1
        if target < 0 or target >= len(siblings):
            return redirect("afterburners:profile-detail", pk=item.profile_id)

        siblings[idx], siblings[target] = siblings[target], siblings[idx]
        _resequence_orders(AfterburnerItem, siblings)
        return redirect("afterburners:profile-detail", pk=item.profile_id)


class AfterburnerItemReorderView(LoginRequiredMixin, View):
    def post(self, request, profile_id):
        profile = get_object_or_404(AfterburnerProfile, pk=profile_id)
        siblings = list(AfterburnerItem.objects.filter(profile=profile).order_by("order", "id"))

        raw_ordered_ids = (request.POST.get("ordered_ids") or "").strip()
        try:
            ordered_ids = [int(v) for v in raw_ordered_ids.split(",") if v.strip()]
        except ValueError:
            messages.error(request, "Invalid reorder payload")
            return redirect("afterburners:profile-detail", pk=profile.id)

        current_ids = [obj.id for obj in siblings]
        if not ordered_ids or len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
            messages.error(request, "Unable to apply item order")
            return redirect("afterburners:profile-detail", pk=profile.id)

        by_id = {obj.id: obj for obj in siblings}
        reordered = [by_id[item_id] for item_id in ordered_ids]
        _resequence_orders(AfterburnerItem, reordered)
        return redirect("afterburners:profile-detail", pk=profile.id)


class ScriptInputCreateView(LoginRequiredMixin, CreateView):
    model = AfterburnerScriptInput
    form_class = AfterburnerScriptInputForm
    template_name = "afterburners/input_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.item = get_object_or_404(AfterburnerItem, pk=self.kwargs["item_id"])
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        form.instance.item = self.item
        response = super().form_valid(form)
        messages.success(self.request, "Custom input added")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["profile_id"] = self.item.profile_id
        return context

    def get_success_url(self):
        return reverse_lazy("afterburners:profile-detail", kwargs={"pk": self.item.profile_id})


class ScriptInputUpdateView(LoginRequiredMixin, UpdateView):
    model = AfterburnerScriptInput
    form_class = AfterburnerScriptInputForm
    template_name = "afterburners/input_form.html"

    def get_success_url(self):
        return reverse_lazy("afterburners:profile-detail", kwargs={"pk": self.object.item.profile_id})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["profile_id"] = self.object.item.profile_id
        return context


class ScriptInputDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        row = get_object_or_404(AfterburnerScriptInput, pk=pk)
        profile_id = row.item.profile_id
        row.delete()
        messages.success(request, "Custom input deleted")
        return redirect("afterburners:profile-detail", pk=profile_id)


class ScriptInputReorderView(LoginRequiredMixin, View):
    def post(self, request, item_id):
        item = get_object_or_404(AfterburnerItem, pk=item_id)
        siblings = list(AfterburnerScriptInput.objects.filter(item=item).order_by("order", "id"))

        raw_ordered_ids = (request.POST.get("ordered_ids") or "").strip()
        try:
            ordered_ids = [int(v) for v in raw_ordered_ids.split(",") if v.strip()]
        except ValueError:
            messages.error(request, "Invalid input reorder payload")
            return redirect("afterburners:profile-detail", pk=item.profile_id)

        current_ids = [obj.id for obj in siblings]
        if not ordered_ids or len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
            messages.error(request, "Unable to apply script input order")
            return redirect("afterburners:profile-detail", pk=item.profile_id)

        by_id = {obj.id: obj for obj in siblings}
        reordered = [by_id[input_id] for input_id in ordered_ids]
        _resequence_orders(AfterburnerScriptInput, reordered)
        return redirect("afterburners:profile-detail", pk=item.profile_id)


class ScriptInputMoveView(LoginRequiredMixin, View):
    def post(self, request, pk, direction):
        row = get_object_or_404(AfterburnerScriptInput, pk=pk)
        siblings = list(AfterburnerScriptInput.objects.filter(item=row.item).order_by("order", "id"))
        idx = next((i for i, v in enumerate(siblings) if v.id == row.id), None)
        if idx is None:
            return redirect("afterburners:profile-detail", pk=row.item.profile_id)

        target = idx - 1 if direction == "up" else idx + 1
        if target < 0 or target >= len(siblings):
            return redirect("afterburners:profile-detail", pk=row.item.profile_id)

        siblings[idx], siblings[target] = siblings[target], siblings[idx]
        _resequence_orders(AfterburnerScriptInput, siblings)
        return redirect("afterburners:profile-detail", pk=row.item.profile_id)
