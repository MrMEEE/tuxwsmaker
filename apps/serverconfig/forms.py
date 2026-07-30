from __future__ import annotations

from django import forms

from .models import ServerConfiguration


class ServerConfigurationForm(forms.ModelForm):
    rhn_username = forms.CharField(required=False, label="Red Hat username")
    rhn_password = forms.CharField(
        required=False,
        label="Red Hat password",
        widget=forms.PasswordInput(render_value=False),
    )
    selected_image_url = forms.CharField(required=False)
    selected_iso_url = forms.CharField(required=False)
    selected_iso_os_id = forms.CharField(required=False)

    class Meta:
        model = ServerConfiguration
        fields = [
            "concurrent_builds",
            "artifact_retention_days",
            "enable_artifact_compression",
            "use_redhat_subscription",
        ]
