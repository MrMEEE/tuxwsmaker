from __future__ import annotations

from django import forms

from .models import PackageList


class PackageListForm(forms.ModelForm):
    class Meta:
        model = PackageList
        fields = ["name", "description", "distro_family"]


class PackageItemsBulkForm(forms.Form):
    data = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "placeholder": "vim\ncurl\npython3"}),
        help_text="One package name per line.",
    )

    @staticmethod
    def parse(text: str) -> list[str]:
        values: list[str] = []
        seen = set()
        for idx, raw in enumerate(text.splitlines(), start=1):
            value = raw.strip()
            if not value:
                continue
            if " " in value:
                raise forms.ValidationError(f"Line {idx} contains spaces; use one package name per line")
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values
