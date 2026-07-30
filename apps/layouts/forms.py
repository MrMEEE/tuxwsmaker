from __future__ import annotations

from django import forms

from .models import PartitionEntry, PartitionLayout


class PartitionLayoutForm(forms.ModelForm):
    class Meta:
        model = PartitionLayout
        fields = ["name", "description"]


class PartitionEntryForm(forms.ModelForm):
    class Meta:
        model = PartitionEntry
        fields = [
            "order",
            "name",
            "entry_role",
            "mount_point",
            "filesystem",
            "size_mode",
            "size_mib",
            "gpt_type",
            "volume_group",
            "logical_volume",
            "is_boot",
            "luks_enabled",
            "luks_name",
        ]
        widgets = {
            "entry_role": forms.Select(choices=PartitionEntry.ROLE_CHOICES),
            "filesystem": forms.Select(choices=PartitionEntry.FILESYSTEM_CHOICES),
            "gpt_type": forms.Select(choices=PartitionEntry.GPT_TYPE_CHOICES),
        }

    def clean(self):
        cleaned = super().clean()
        size_mode = cleaned.get("size_mode")
        size_mib = cleaned.get("size_mib")
        entry_role = cleaned.get("entry_role")
        mount_point = cleaned.get("mount_point")
        filesystem = cleaned.get("filesystem")
        volume_group = cleaned.get("volume_group")
        logical_volume = cleaned.get("logical_volume")
        luks_enabled = cleaned.get("luks_enabled")
        luks_name = cleaned.get("luks_name")

        if size_mode == PartitionEntry.SIZE_FIXED and not size_mib:
            raise forms.ValidationError("Fixed-size entries require size_mib")
        if size_mode == PartitionEntry.SIZE_REMAINDER and size_mib:
            raise forms.ValidationError("Remainder entries must not set size_mib")
        if luks_enabled and not luks_name:
            raise forms.ValidationError("LUKS-enabled entries require luks_name")
        if entry_role == PartitionEntry.ROLE_LV and not volume_group:
            raise forms.ValidationError("LVM logical volumes require volume_group")
        if entry_role == PartitionEntry.ROLE_LV and not logical_volume:
            raise forms.ValidationError("LVM logical volumes require logical_volume")
        if entry_role != PartitionEntry.ROLE_LV and logical_volume:
            raise forms.ValidationError("logical_volume is only valid for LVM logical volumes")
        if entry_role == PartitionEntry.ROLE_PV and mount_point:
            raise forms.ValidationError("LVM physical volumes must not have mount_point")
        if entry_role == PartitionEntry.ROLE_PV and filesystem != "none":
            raise forms.ValidationError("LVM physical volumes must use filesystem 'none'")

        return cleaned


class YAMLFallbackForm(forms.ModelForm):
    class Meta:
        model = PartitionLayout
        fields = ["yaml_fallback"]
        widgets = {
            "yaml_fallback": forms.Textarea(attrs={"rows": 16, "placeholder": "key: value"}),
        }
