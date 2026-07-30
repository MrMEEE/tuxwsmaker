from django import forms

from apps.builds.models import SSHKey
from .models import PlaybookBranch, PlaybookRepository


class PlaybookRepositoryForm(forms.ModelForm):
    default_branch = forms.ChoiceField(choices=[("main", "main")])
    ssh_key = forms.ModelChoiceField(
        queryset=SSHKey.objects.none(),
        required=False,
        empty_label="No SSH key",
        label="SSH private key",
    )
    api_key = forms.CharField(
        required=False,
        label="API key / token",
        widget=forms.PasswordInput(render_value=False),
        help_text="Used for HTTPS repositories. Leave blank to keep the current value when editing.",
    )

    class Meta:
        model = PlaybookRepository
        fields = ["name", "repo_url", "default_branch", "ssh_key"]

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        ssh_keys = SSHKey.objects.filter(scope=SSHKey.SCOPE_USER).select_related("owner").order_by("owner__username", "name")
        if self.user and getattr(self.user, "is_authenticated", False):
            ssh_keys = ssh_keys.filter(owner=self.user)
        else:
            ssh_keys = ssh_keys.none()
        self.fields["ssh_key"].queryset = ssh_keys

        branch_names = set()
        if self.instance and self.instance.pk:
            branch_names.update(
                PlaybookBranch.objects.filter(repository=self.instance)
                .order_by("name")
                .values_list("name", flat=True)
            )

        branch_names.add(self.initial.get("default_branch") or "")
        branch_names.add(self.data.get("default_branch") or "")
        branch_names.add("main")
        branch_names = {name for name in branch_names if name}

        choices = [(name, name) for name in sorted(branch_names)]
        self.fields["default_branch"].choices = choices

    def clean(self):
        cleaned = super().clean()
        repo_url = (cleaned.get("repo_url") or "").strip()
        if repo_url.startswith("http"):
            cleaned["ssh_key"] = None
        elif "@" in repo_url:
            cleaned["api_key"] = ""
        return cleaned

    def save(self, commit: bool = True):
        instance: PlaybookRepository = super().save(commit=False)
        ssh_key = self.cleaned_data.get("ssh_key")
        if ssh_key is not None or not self.instance.pk:
            instance.ssh_key = ssh_key

        api_key = (self.cleaned_data.get("api_key") or "").strip()
        if api_key:
            instance.set_api_key(api_key)

        if commit:
            instance.save()
            self.save_m2m()
        return instance
