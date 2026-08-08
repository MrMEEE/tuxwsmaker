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
            # Allow spaces in group names (@Group Name) and quoted entries ("@Group Name").
            bare = value.strip('"').strip("'")
            if " " in value and not bare.startswith("@"):
                raise forms.ValidationError(
                    f"Line {idx} contains spaces; use one package name per line "
                    f"(group names with spaces must start with @ e.g. @KDE Plasma Workspaces)"
                )
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
        return values
