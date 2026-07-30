from __future__ import annotations

from pathlib import Path

from django import forms

from .models import ISOImage, ISOVariable, OSVariable, OperatingSystem


class OperatingSystemForm(forms.ModelForm):
    class Meta:
        model = OperatingSystem
        fields = ["name", "family", "description"]


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiISOUploadForm(forms.Form):
    files = forms.FileField(required=False, widget=MultiFileInput(attrs={"accept": ".iso"}))
    versions = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Optional, one version per line"}),
        help_text="If empty, version names are derived from ISO filenames.",
    )
    os_versions = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Optional, one OS version per line"}),
        help_text="If empty, OS versions are derived from the version values.",
    )

    def clean(self):
        cleaned = super().clean()
        files = []
        if hasattr(self, "files"):
            # Browsers usually submit repeated "files" fields for multi-select,
            # but some clients use "files[]". Accept both.
            files = self.files.getlist("files") or self.files.getlist("files[]")
            if not files:
                files = [f for f in self.files.values() if f]
        if not files:
            raise forms.ValidationError("Select one or more ISO files")

        versions_raw = cleaned.get("versions", "")
        versions = [line.strip() for line in versions_raw.splitlines() if line.strip()]
        if versions and len(versions) != len(files):
            raise forms.ValidationError("Version line count must match number of uploaded files")

        derived = versions or [Path(f.name).stem for f in files]
        cleaned["resolved_versions"] = derived

        os_versions_raw = cleaned.get("os_versions", "")
        os_versions = [line.strip() for line in os_versions_raw.splitlines() if line.strip()]
        if os_versions and len(os_versions) != len(files):
            raise forms.ValidationError("OS version line count must match number of uploaded files")
        cleaned["resolved_os_versions"] = os_versions or derived
        return cleaned


class VariableBulkForm(forms.Form):
    data = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8, "placeholder": "key=value"}),
        help_text="One key=value pair per line.",
    )

    @staticmethod
    def parse(text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for idx, line in enumerate(text.splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue
            if "=" not in raw:
                raise forms.ValidationError(f"Line {idx} must use key=value format")
            key, value = raw.split("=", 1)
            key = key.strip()
            if not key:
                raise forms.ValidationError(f"Line {idx} has empty key")
            result[key] = value.strip()
        return result


class ISOVariableForm(forms.ModelForm):
    class Meta:
        model = ISOVariable
        fields = ["key", "value"]


class OSVariableForm(forms.ModelForm):
    class Meta:
        model = OSVariable
        fields = ["key", "value"]


class ISOQuickEditForm(forms.ModelForm):
    class Meta:
        model = ISOImage
        fields = ["version", "os_version", "is_active"]
