from __future__ import annotations

import json
import re

from django import forms
from django.core.exceptions import ValidationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_ssh_private_key

from apps.afterburners.models import AfterburnerProfile
from apps.catalog.models import ISOImage
from apps.builds.models import BuildDefinition, SSHKey
from apps.layouts.models import PartitionLayout
from apps.playbooks.models import Playbook
from apps.repositories.models import PackageRepository, RedHatRepositoryCatalog
from apps.serverconfig.models import ServerConfiguration

from .models import BuildMachineConfig, BuildPlaybookSelection, BuildRepositorySelection, BuildRhsmRepositorySelection


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
    afterburner_order_json = forms.CharField(required=False, widget=forms.HiddenInput())
    repository_order_json = forms.CharField(required=False, widget=forms.HiddenInput())
    rhsm_repository_order_json = forms.CharField(required=False, widget=forms.HiddenInput())
    rhsm_password = forms.CharField(
        required=False,
        label="Red Hat password",
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank while editing to keep the current password.",
    )
    rhsm_activation_key = forms.CharField(
        required=False,
        label="Red Hat activation key",
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank while editing to keep the current activation key.",
    )

    class Meta:
        model = BuildDefinition
        fields = [
            "name",
            "operating_system",
            "iso_image",
            "partition_layout",
            "machine_config",
            "package_lists",
            "rhsm_auth_mode",
            "rhsm_username",
            "rhsm_org_id",
            "rhsm_repositories",
            "output_pxe",
            "output_usb_img",
            "enable_answers_file_support",
        ]
        widgets = {
            "package_lists": forms.CheckboxSelectMultiple,
            "rhsm_repositories": forms.SelectMultiple(attrs={"size": 8}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        iso_qs = ISOImage.objects.select_related("operating_system").order_by(
            "operating_system__name", "version"
        )
        self.fields["iso_image"].queryset = iso_qs
        self.available_playbooks = Playbook.objects.select_related("repository").filter(is_active=True)
        self.available_afterburners = AfterburnerProfile.objects.order_by("name")
        self.available_repositories = PackageRepository.objects.order_by("name")
        rhsm_repo_qs = RedHatRepositoryCatalog.objects.order_by("rhel_major", "repo_id")
        self.fields["rhsm_repositories"].queryset = rhsm_repo_qs
        self.fields["rhsm_auth_mode"].required = False
        self.fields["rhsm_auth_mode"].label = "RHSM auth mode"
        self.fields["rhsm_username"].required = False
        self.fields["rhsm_org_id"].required = False
        self.fields["enable_answers_file_support"].label = "Enable answers-file support"

        self.iso_major_map = {}
        for iso_item in iso_qs:
            match = re.search(r"(\d+)", str(iso_item.version or ""))
            self.iso_major_map[str(iso_item.id)] = int(match.group(1)) if match else None

        self.rhsm_repo_meta = {
            str(repo.id): {
                "rhel_major": int(repo.rhel_major),
                "architecture": str(repo.architecture or "").strip() or "x86_64",
            }
            for repo in rhsm_repo_qs
        }
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

            ordered_afterburners = list(
                self.instance.afterburner_selections.select_related("afterburner").order_by("order")
            )
            afterburner_payload = [
                {
                    "id": sel.afterburner_id,
                    "label": sel.afterburner.name,
                }
                for sel in ordered_afterburners
            ]
            self.initial["afterburner_order_json"] = json.dumps(afterburner_payload)

            ordered_repositories = list(
                BuildRepositorySelection.objects.filter(build=self.instance)
                .select_related("repository")
                .order_by("order")
            )
            repository_payload = [
                {
                    "id": sel.repository_id,
                    "label": sel.repository.name,
                    "during_build": sel.enable_during_build,
                    "before_afterburner": sel.enable_before_afterburner,
                }
                for sel in ordered_repositories
            ]
            self.initial["repository_order_json"] = json.dumps(repository_payload)

            ordered_rhsm_repositories = list(
                BuildRhsmRepositorySelection.objects.filter(build=self.instance)
                .select_related("repository")
                .order_by("order")
            )
            if ordered_rhsm_repositories:
                rhsm_repository_payload = [
                    {
                        "id": sel.repository_id,
                        "label": f"RHEL {sel.repository.rhel_major} {sel.repository.architecture}: {sel.repository.repo_id}",
                        "during_build": sel.enable_during_build,
                        "before_afterburner": sel.enable_before_afterburner,
                    }
                    for sel in ordered_rhsm_repositories
                ]
            else:
                rhsm_repository_payload = [
                    {
                        "id": repo.id,
                        "label": f"RHEL {repo.rhel_major} {repo.architecture}: {repo.repo_id}",
                        "during_build": True,
                        "before_afterburner": False,
                    }
                    for repo in self.instance.rhsm_repositories.order_by("rhel_major", "repo_id")
                ]
            self.initial["rhsm_repository_order_json"] = json.dumps(rhsm_repository_payload)

    def clean(self):
        cleaned = super().clean()
        os_obj = cleaned.get("operating_system")
        iso = cleaned.get("iso_image")
        if os_obj and iso and iso.operating_system_id != os_obj.id:
            raise forms.ValidationError("Selected ISO does not belong to selected OS")
        if not cleaned.get("output_pxe") and not cleaned.get("output_usb_img"):
            raise forms.ValidationError("Enable at least one output type")

        os_family = str(getattr(os_obj, "family", "") or "").strip().lower()
        rhsm_auth_mode = str(cleaned.get("rhsm_auth_mode") or BuildDefinition.RHSM_AUTH_NONE).strip()
        rhsm_username = str(cleaned.get("rhsm_username") or "").strip()
        rhsm_org_id = str(cleaned.get("rhsm_org_id") or "").strip()
        rhsm_password = str(cleaned.get("rhsm_password") or "").strip()
        rhsm_activation_key = str(cleaned.get("rhsm_activation_key") or "").strip()
        rhsm_repos = cleaned.get("rhsm_repositories")
        iso_major = None
        if iso:
            match = re.search(r"(\d+)", str(iso.version or ""))
            if match:
                iso_major = int(match.group(1))

        rhsm_repository_json = (cleaned.get("rhsm_repository_order_json") or "").strip()
        cleaned["ordered_rhsm_repository_payload"] = []
        has_rhsm_repos = False
        rhsm_repo_objects: list[RedHatRepositoryCatalog] = []

        if rhsm_repository_json:
            try:
                rhsm_payload = json.loads(rhsm_repository_json)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(f"Invalid RHSM repository ordering payload: {exc}")
            if not isinstance(rhsm_payload, list):
                raise forms.ValidationError("RHSM repository ordering payload must be a list")

            rows: list[dict[str, object]] = []
            ids: list[int] = []
            for item in rhsm_payload:
                if not isinstance(item, dict) or "id" not in item:
                    raise forms.ValidationError("RHSM repository ordering payload is malformed")
                try:
                    repo_id = int(item["id"])
                except (TypeError, ValueError):
                    raise forms.ValidationError("RHSM repository ordering payload contains invalid IDs")
                during_build = bool(item.get("during_build"))
                before_afterburner = bool(item.get("before_afterburner"))
                if not during_build and not before_afterburner:
                    raise forms.ValidationError("Each attached RHSM repository must be enabled for at least one phase")
                rows.append(
                    {
                        "id": repo_id,
                        "during_build": during_build,
                        "before_afterburner": before_afterburner,
                    }
                )
                ids.append(repo_id)

            if len(ids) != len(set(ids)):
                raise forms.ValidationError("RHSM repository ordering payload contains duplicate repositories")

            rhsm_repo_qs = RedHatRepositoryCatalog.objects.filter(id__in=ids)
            existing = set(rhsm_repo_qs.values_list("id", flat=True))
            missing = [str(v) for v in ids if v not in existing]
            if missing:
                raise forms.ValidationError(f"Unknown RHSM repository IDs in ordering payload: {', '.join(missing)}")

            by_id = {repo.id: repo for repo in rhsm_repo_qs}
            rhsm_repo_objects = [by_id[item_id] for item_id in ids if item_id in by_id]
            cleaned["ordered_rhsm_repository_payload"] = rows
            has_rhsm_repos = bool(rows)
        elif rhsm_repos is not None:
            rhsm_repo_objects = list(rhsm_repos)
            has_rhsm_repos = bool(rhsm_repo_objects)

        if os_family != "rhel":
            if rhsm_auth_mode != BuildDefinition.RHSM_AUTH_NONE:
                self.add_error("rhsm_auth_mode", "RHSM authentication is only available for RHEL builds")
            if has_rhsm_repos:
                self.add_error("rhsm_repositories", "RHSM repositories can only be selected for RHEL builds")
        elif has_rhsm_repos and rhsm_auth_mode == BuildDefinition.RHSM_AUTH_NONE:
            self.add_error("rhsm_auth_mode", "Select an RHSM authentication mode when RHSM repositories are selected")

        if os_family == "rhel" and has_rhsm_repos and iso_major is not None:
            mismatched = [
                repo.repo_id
                for repo in rhsm_repo_objects
                if int(repo.rhel_major) != int(iso_major)
            ]
            if mismatched:
                self.add_error(
                    "rhsm_repository_order_json",
                    f"Selected RHSM repositories must match ISO major version {iso_major}: {', '.join(mismatched)}",
                )

        if rhsm_auth_mode == BuildDefinition.RHSM_AUTH_USERPASS:
            if not rhsm_username:
                self.add_error("rhsm_username", "Username is required for RHSM username/password mode")
            if not rhsm_password and not self.instance.has_rhsm_password():
                self.add_error("rhsm_password", "Password is required for RHSM username/password mode")
        elif rhsm_auth_mode == BuildDefinition.RHSM_AUTH_CONFIG and has_rhsm_repos:
            cfg = ServerConfiguration.get_solo()
            if not str(cfg.rhn_username or "").strip() or not str(cfg.get_rhn_password() or "").strip():
                self.add_error(
                    "rhsm_auth_mode",
                    "Server configuration RHSM credentials are required for this auth mode",
                )
        elif rhsm_auth_mode == BuildDefinition.RHSM_AUTH_ACTIVATION_KEY:
            if not rhsm_org_id:
                self.add_error("rhsm_org_id", "Org ID is required for RHSM activation-key mode")
            if not rhsm_activation_key and not self.instance.has_rhsm_activation_key():
                self.add_error("rhsm_activation_key", "Activation key is required for RHSM activation-key mode")

        order_json = (cleaned.get("playbook_order_json") or "").strip()
        cleaned["ordered_playbook_ids"] = []
        cleaned["ordered_playbook_payload"] = []
        if order_json:
            try:
                payload = json.loads(order_json)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(f"Invalid playbook ordering payload: {exc}")
            if not isinstance(payload, list):
                raise forms.ValidationError("Playbook ordering payload must be a list")

            ids: list[int] = []
            rows: list[dict[str, object]] = []
            for item in payload:
                if not isinstance(item, dict) or "id" not in item:
                    raise forms.ValidationError("Playbook ordering payload is malformed")
                try:
                    playbook_id = int(item["id"])
                except (TypeError, ValueError):
                    raise forms.ValidationError("Playbook ordering payload contains invalid IDs")
                ids.append(playbook_id)
                rows.append({"id": playbook_id, "run_mode": BuildPlaybookSelection.RUN_MODE_NON_CHROOT})

            existing = set(Playbook.objects.filter(id__in=ids, is_active=True).values_list("id", flat=True))
            missing = [str(v) for v in ids if v not in existing]
            if missing:
                raise forms.ValidationError(f"Unknown playbook IDs in ordering payload: {', '.join(missing)}")

            cleaned["ordered_playbook_ids"] = ids
            cleaned["ordered_playbook_payload"] = rows

        afterburner_json = (cleaned.get("afterburner_order_json") or "").strip()
        cleaned["ordered_afterburner_ids"] = []
        if afterburner_json:
            try:
                payload = json.loads(afterburner_json)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(f"Invalid afterburner ordering payload: {exc}")
            if not isinstance(payload, list):
                raise forms.ValidationError("Afterburner ordering payload must be a list")

            ids: list[int] = []
            for item in payload:
                if not isinstance(item, dict) or "id" not in item:
                    raise forms.ValidationError("Afterburner ordering payload is malformed")
                try:
                    ids.append(int(item["id"]))
                except (TypeError, ValueError):
                    raise forms.ValidationError("Afterburner ordering payload contains invalid IDs")

            existing = set(AfterburnerProfile.objects.filter(id__in=ids).values_list("id", flat=True))
            missing = [str(v) for v in ids if v not in existing]
            if missing:
                raise forms.ValidationError(f"Unknown afterburner IDs in ordering payload: {', '.join(missing)}")

            cleaned["ordered_afterburner_ids"] = ids

        repository_json = (cleaned.get("repository_order_json") or "").strip()
        cleaned["ordered_repository_payload"] = []
        if repository_json:
            try:
                payload = json.loads(repository_json)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(f"Invalid repository ordering payload: {exc}")
            if not isinstance(payload, list):
                raise forms.ValidationError("Repository ordering payload must be a list")

            rows: list[dict[str, object]] = []
            ids: list[int] = []
            for item in payload:
                if not isinstance(item, dict) or "id" not in item:
                    raise forms.ValidationError("Repository ordering payload is malformed")
                try:
                    repo_id = int(item["id"])
                except (TypeError, ValueError):
                    raise forms.ValidationError("Repository ordering payload contains invalid IDs")
                during_build = bool(item.get("during_build"))
                before_afterburner = bool(item.get("before_afterburner"))
                if not during_build and not before_afterburner:
                    raise forms.ValidationError("Each attached repository must be enabled for at least one phase")
                rows.append(
                    {
                        "id": repo_id,
                        "during_build": during_build,
                        "before_afterburner": before_afterburner,
                    }
                )
                ids.append(repo_id)

            if len(ids) != len(set(ids)):
                raise forms.ValidationError("Repository ordering payload contains duplicate repositories")

            existing = set(PackageRepository.objects.filter(id__in=ids).values_list("id", flat=True))
            missing = [str(v) for v in ids if v not in existing]
            if missing:
                raise forms.ValidationError(f"Unknown repository IDs in ordering payload: {', '.join(missing)}")

            if os_obj:
                expected_repo_family = {
                    "rhel": PackageRepository.FAMILY_RPM,
                    "debian": PackageRepository.FAMILY_DEB,
                }.get(str(os_obj.family or "").strip(), "")
                incompatible = list(
                    PackageRepository.objects.filter(id__in=ids).exclude(family=expected_repo_family).values_list("name", flat=True)
                )
                if incompatible:
                    raise forms.ValidationError(
                        f"Attached repositories must match the build OS family ({os_obj.family}): {', '.join(incompatible)}"
                    )

            cleaned["ordered_repository_payload"] = rows
        return cleaned

    def save(self, commit: bool = True):
        instance: BuildDefinition = super().save(commit=False)
        mode = str(self.cleaned_data.get("rhsm_auth_mode") or BuildDefinition.RHSM_AUTH_NONE).strip()
        rhsm_password = str(self.cleaned_data.get("rhsm_password") or "").strip()
        rhsm_activation_key = str(self.cleaned_data.get("rhsm_activation_key") or "").strip()

        if mode == BuildDefinition.RHSM_AUTH_USERPASS:
            if rhsm_password:
                instance.set_rhsm_password(rhsm_password)
            instance.rhsm_org_id = ""
            instance.rhsm_activation_key_encrypted = ""
        elif mode == BuildDefinition.RHSM_AUTH_ACTIVATION_KEY:
            if rhsm_activation_key:
                instance.set_rhsm_activation_key(rhsm_activation_key)
            instance.rhsm_username = ""
            instance.rhsm_password_encrypted = ""
        else:
            instance.rhsm_username = ""
            instance.rhsm_org_id = ""
            instance.rhsm_password_encrypted = ""
            instance.rhsm_activation_key_encrypted = ""

        if commit:
            instance.save()
            self.save_m2m()
        return instance
