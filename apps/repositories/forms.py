from __future__ import annotations

from django import forms

from .models import PackageRepository


class PackageRepositoryForm(forms.ModelForm):
    secret = forms.CharField(
        required=False,
        label="Password / token",
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank when editing to keep the current secret.",
    )

    class Meta:
        model = PackageRepository
        fields = [
            "name",
            "family",
            "enabled",
            "base_url",
            "auth_type",
            "username",
            "signing_mode",
            "gpg_key_url",
            "gpg_key_inline",
            "deb_suite",
            "deb_components",
            "rpm_repoid",
        ]
        widgets = {
            "gpg_key_inline": forms.Textarea(attrs={"rows": 6}),
        }

    def clean(self):
        cleaned = super().clean()
        family = str(cleaned.get("family") or "").strip()
        auth_type = str(cleaned.get("auth_type") or PackageRepository.AUTH_NONE).strip()
        signing_mode = str(cleaned.get("signing_mode") or PackageRepository.SIGNING_NONE).strip()

        username = str(cleaned.get("username") or "").strip()
        base_url = str(cleaned.get("base_url") or "").strip()
        deb_suite = str(cleaned.get("deb_suite") or "").strip()
        deb_components = str(cleaned.get("deb_components") or "").strip()
        rpm_repoid = str(cleaned.get("rpm_repoid") or "").strip()
        gpg_key_url = str(cleaned.get("gpg_key_url") or "").strip()
        gpg_key_inline = str(cleaned.get("gpg_key_inline") or "").strip()
        secret = str(cleaned.get("secret") or "").strip()

        if not base_url:
            self.add_error("base_url", "Base URL is required")

        if auth_type == PackageRepository.AUTH_BASIC and not username:
            self.add_error("username", "Username is required for basic authentication")
        if auth_type == PackageRepository.AUTH_BASIC and not secret and not self.instance.has_secret():
            self.add_error("secret", "Password is required for basic authentication")
        if auth_type == PackageRepository.AUTH_TOKEN and not secret and not self.instance.has_secret():
            self.add_error("secret", "Token is required for token authentication")
        if auth_type == PackageRepository.AUTH_NONE:
            cleaned["username"] = ""

        if signing_mode == PackageRepository.SIGNING_URL and not gpg_key_url:
            self.add_error("gpg_key_url", "GPG key URL is required for URL signing mode")
        if signing_mode == PackageRepository.SIGNING_INLINE and not gpg_key_inline:
            self.add_error("gpg_key_inline", "Inline GPG key is required for inline signing mode")
        if signing_mode == PackageRepository.SIGNING_NONE:
            cleaned["gpg_key_url"] = ""
            cleaned["gpg_key_inline"] = ""

        if family == PackageRepository.FAMILY_DEB:
            if not deb_suite:
                self.add_error("deb_suite", "Suite / distribution is required for Deb repositories")
            if not deb_components:
                self.add_error("deb_components", "Components are required for Deb repositories")
            cleaned["rpm_repoid"] = ""
        elif family == PackageRepository.FAMILY_RPM:
            if not rpm_repoid:
                self.add_error("rpm_repoid", "Repository ID is required for RPM repositories")
            cleaned["deb_suite"] = ""
            cleaned["deb_components"] = ""

        return cleaned

    def save(self, commit: bool = True):
        instance: PackageRepository = super().save(commit=False)
        secret = str(self.cleaned_data.get("secret") or "").strip()
        if secret:
            instance.set_secret(secret)
        if commit:
            instance.save()
        return instance