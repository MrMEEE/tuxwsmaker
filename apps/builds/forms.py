from __future__ import annotations

import json

from django import forms
from django.core.exceptions import ValidationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_ssh_private_key

from apps.catalog.models import ISOImage
from apps.builds.models import SSHKey
from apps.layouts.models import PartitionLayout
from apps.playbooks.models import Playbook

from .models import BuildDefinition, BuildMachineConfig, BuildPlaybookSelection


class BuildMachineConfigForm(forms.ModelForm):
    class Meta:
        model = BuildMachineConfig
        fields = [
            "name",
            "cpu",
            "memory_mib",
            "boot_mode",
            "hypervisor_uri",
            "kickstart_timeout_minutes",
        ]


class UserSSHKeyForm(forms.ModelForm):
    private_key = forms.CharField(
        label="Private key",
        widget=forms.Textarea(attrs={"rows": 6}),
        required=False,
        help_text="Paste an OpenSSH or PEM private key, or leave blank and use Generate new keypair."
    )
    generate_keypair = forms.BooleanField(required=False, initial=False, label="Generate new keypair")

    class Meta:
        model = SSHKey
        fields = ["name"]

    def clean(self):
        cleaned = super().clean()
        private_key = (cleaned.get("private_key") or "").strip()
        generate_keypair = bool(cleaned.get("generate_keypair"))
        if not private_key and not generate_keypair:
            raise forms.ValidationError("Provide a private key or choose Generate new keypair")
        if private_key:
            self._parse_private_key(private_key)
        return cleaned

    def _parse_private_key(self, private_key: str):
        private_key_bytes = private_key.encode("utf-8")
        loaders = (
            load_ssh_private_key,
            load_pem_private_key,
        )
        last_error: Exception | None = None
        for loader in loaders:
            try:
                return loader(private_key_bytes, password=None)
            except ValueError as exc:
                last_error = exc
        raise ValidationError("Enter a valid OpenSSH or PEM private key") from last_error

    def save(self, *, owner, commit: bool = True):
        instance: SSHKey = super().save(commit=False)
        instance.scope = SSHKey.SCOPE_USER
        instance.owner = owner
        private_key = (self.cleaned_data.get("private_key") or "").strip()
        if self.cleaned_data.get("generate_keypair") or not private_key:
            private_key_obj = ed25519.Ed25519PrivateKey.generate()
            private_key = private_key_obj.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8").strip()
            public_key = private_key_obj.public_key().public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            ).decode("utf-8").strip()
        else:
            private_obj = self._parse_private_key(private_key)
            public_key = private_obj.public_key().public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            ).decode("utf-8").strip()
        instance.set_keypair(private_key=private_key, public_key=public_key)
        if commit:
            instance.save()
        return instance


class BuildDefinitionForm(forms.ModelForm):
    playbook_order_json = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = BuildDefinition
        fields = [
            "name",
            "operating_system",
            "iso_image",
            "partition_layout",
            "machine_config",
            "package_lists",
            "output_pxe",
            "output_usb_img",
        ]
        widgets = {
            "package_lists": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["iso_image"].queryset = ISOImage.objects.select_related("operating_system").order_by(
            "operating_system__name", "version"
        )
        self.available_playbooks = Playbook.objects.select_related("repository").filter(is_active=True)
        self.machine_boot_modes = {
            str(item[0]): item[1]
            for item in BuildMachineConfig.objects.values_list("id", "boot_mode")
        }
        self.layout_meta = {}
        for layout in PartitionLayout.objects.prefetch_related("entries").all():
            has_esp = any(
                (entry.mount_point or "").strip() == "/boot/efi" or entry.filesystem == "efi"
                for entry in layout.entries.all()
            )
            self.layout_meta[str(layout.id)] = {
                "table_type": layout.table_type,
                "has_esp": has_esp,
            }

        if self.instance and self.instance.pk:
            ordered = list(
                BuildPlaybookSelection.objects.filter(build=self.instance)
                .select_related("playbook")
                .order_by("order")
            )
            payload = [
                {
                    "id": sel.playbook_id,
                    "label": f"{sel.playbook.repository.name} [{sel.playbook.branch}] {sel.playbook.path}",
                }
                for sel in ordered
            ]
            self.initial["playbook_order_json"] = json.dumps(payload)

    def clean(self):
        cleaned = super().clean()
        os_obj = cleaned.get("operating_system")
        iso = cleaned.get("iso_image")
        if os_obj and iso and iso.operating_system_id != os_obj.id:
            raise forms.ValidationError("Selected ISO does not belong to selected OS")
        if not cleaned.get("output_pxe") and not cleaned.get("output_usb_img"):
            raise forms.ValidationError("Enable at least one output type")

        order_json = (cleaned.get("playbook_order_json") or "").strip()
        cleaned["ordered_playbook_ids"] = []
        if order_json:
            try:
                payload = json.loads(order_json)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(f"Invalid playbook ordering payload: {exc}")
            if not isinstance(payload, list):
                raise forms.ValidationError("Playbook ordering payload must be a list")

            ids: list[int] = []
            for item in payload:
                if not isinstance(item, dict) or "id" not in item:
                    raise forms.ValidationError("Playbook ordering payload is malformed")
                try:
                    ids.append(int(item["id"]))
                except (TypeError, ValueError):
                    raise forms.ValidationError("Playbook ordering payload contains invalid IDs")

            existing = set(Playbook.objects.filter(id__in=ids, is_active=True).values_list("id", flat=True))
            missing = [str(v) for v in ids if v not in existing]
            if missing:
                raise forms.ValidationError(f"Unknown playbook IDs in ordering payload: {', '.join(missing)}")

            cleaned["ordered_playbook_ids"] = ids
        return cleaned
