from __future__ import annotations

import ipaddress
import json

from django import forms
from django.core.exceptions import ValidationError

from .models import AfterburnerItem, AfterburnerProfile, AfterburnerScriptInput


class AfterburnerProfileForm(forms.ModelForm):
    class Meta:
        model = AfterburnerProfile
        fields = ["name", "description"]


class AfterburnerItemForm(forms.ModelForm):
    CUSTOM_SCRIPT_RUN_MODE_NON_CHROOT = "non_chroot"
    CUSTOM_SCRIPT_RUN_MODE_CHROOT = "chroot"
    CUSTOM_SCRIPT_RUN_MODE_CHOICES = [
        (CUSTOM_SCRIPT_RUN_MODE_NON_CHROOT, "Non-chroot"),
        (CUSTOM_SCRIPT_RUN_MODE_CHROOT, "Chroot (target system)"),
    ]
    TPM_HASH_CHOICES = [
        ("sha1", "sha1"),
        ("sha256", "sha256"),
        ("sha384", "sha384"),
        ("sha512", "sha512"),
    ]
    TPM_KEY_CHOICES = [
        ("rsa", "rsa"),
        ("ecc", "ecc"),
    ]
    TPM_PCR_ID_CHOICES = [(str(v), str(v)) for v in range(0, 16)]

    ad_domain = forms.CharField(required=False, label="Default AD domain")
    ad_computer_ou = forms.CharField(required=False, label="Default AD computer OU")
    ad_join_user = forms.CharField(required=False, label="Default AD join user")

    static_interface = forms.CharField(required=False, label="Default interface")
    static_ip_address = forms.GenericIPAddressField(required=False, protocol="IPv4", label="Default IPv4 address")
    static_prefix = forms.ChoiceField(
        required=False,
        label="Default prefix",
        choices=[(str(v), str(v)) for v in range(1, 33)],
        initial="24",
    )
    static_gateway = forms.GenericIPAddressField(required=False, protocol="IPv4", label="Default gateway")
    static_dns = forms.CharField(required=False, label="Default DNS servers")

    luks_device = forms.CharField(required=False, label="Default LUKS block device")
    luks_autodetect = forms.BooleanField(
        required=False,
        label="Autodetect LUKS containers",
        help_text="Scan the target system for LUKS containers and prompt for each one.",
    )

    tpm_device = forms.CharField(required=False, label="Default LUKS block device")
    tpm_autodetect = forms.BooleanField(
        required=False,
        label="Autodetect LUKS containers",
        help_text="Scan the target system for LUKS containers and bind each one to TPM.",
    )
    tpm_hash = forms.ChoiceField(
        required=False,
        label="TPM hash",
        choices=TPM_HASH_CHOICES,
        initial="sha256",
    )
    tpm_pcr_bank = forms.ChoiceField(
        required=False,
        label="TPM PCR bank",
        choices=TPM_HASH_CHOICES,
        initial="sha256",
    )
    tpm_key = forms.ChoiceField(
        required=False,
        label="TPM key",
        choices=TPM_KEY_CHOICES,
        initial="ecc",
    )
    tpm_pcr_ids = forms.MultipleChoiceField(
        required=False,
        label="TPM PCR IDs",
        choices=TPM_PCR_ID_CHOICES,
        initial=[],
        widget=forms.SelectMultiple(attrs={"size": 8}),
        help_text="Add zero or more PCR IDs to bind in the TPM2 policy.",
    )

    bootloader_user = forms.CharField(required=False, label="Default GRUB username")
    rhsm_username = forms.CharField(required=False, label="Red Hat username")
    rhsm_password = forms.CharField(required=False, label="Red Hat password", widget=forms.PasswordInput(render_value=True))
    rhsm_org_id = forms.CharField(required=False, label="Organization ID")
    rhsm_activation_key = forms.CharField(required=False, label="Activation key")
    rhsm_repo_ids = forms.CharField(required=False, label="Repository IDs (comma-separated)")
    rhsm_prompt_credentials = forms.BooleanField(
        required=False,
        label="Ask for registration credentials during afterburner",
    )
    rhsm_prompt_repositories = forms.BooleanField(
        required=False,
        label="Ask for repository IDs during afterburner",
    )
    wait_message = forms.CharField(required=False, label="Message to show")
    script_body = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        label="Custom script body",
        help_text="Runs inside the restored system during deployment. Use the input keys as environment variables inside your script. Example: echo \"Deploying ${ENVIRONMENT}\"; useradd -m \"${USERNAME}\"",
    )
    custom_name = forms.CharField(
        required=False,
        label="Display name",
        help_text="Shown in the afterburner list for this custom script item.",
    )
    script_questions_json = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
        label="Questions",
    )
    script_run_mode = forms.ChoiceField(
        required=False,
        label="Run mode",
        choices=CUSTOM_SCRIPT_RUN_MODE_CHOICES,
        initial=CUSTOM_SCRIPT_RUN_MODE_NON_CHROOT,
    )

    class Meta:
        model = AfterburnerItem
        fields = ["item_type"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item_type"].widget.attrs.update({"class": "form-select"})
        for key, field in self.fields.items():
            if key == "item_type":
                continue
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("class", "form-control")
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs.setdefault("class", "form-select")
            else:
                field.widget.attrs.setdefault("class", "form-control")

        if self.instance and self.instance.pk:
            cfg = dict(self.instance.config or {})
            self.initial["ad_domain"] = str(cfg.get("domain") or "")
            self.initial["ad_computer_ou"] = str(cfg.get("computer_ou") or "")
            self.initial["ad_join_user"] = str(cfg.get("join_user") or "")

            self.initial["static_interface"] = str(cfg.get("interface") or "")
            self.initial["static_ip_address"] = str(cfg.get("ip_address") or "")
            self.initial["static_prefix"] = str(cfg.get("prefix") or "24")
            self.initial["static_gateway"] = str(cfg.get("gateway") or "")
            self.initial["static_dns"] = str(cfg.get("dns") or "")

            self.initial["luks_device"] = str(cfg.get("device") or "")
            self.initial["luks_autodetect"] = bool(cfg.get("autodetect") or False)

            self.initial["tpm_device"] = str(cfg.get("device") or "")
            self.initial["tpm_autodetect"] = bool(cfg.get("autodetect") or False)

            legacy_profile = str(cfg.get("policy_profile") or "").strip()
            if legacy_profile == "pcr7_11_sha256_ecc":
                tpm_hash = "sha256"
                tpm_key = "ecc"
                tpm_pcr_bank = "sha256"
                tpm_pcr_ids = ["7", "11"]
            elif legacy_profile == "pcr7_sha256_ecc":
                tpm_hash = "sha256"
                tpm_key = "ecc"
                tpm_pcr_bank = "sha256"
                tpm_pcr_ids = ["7"]
            else:
                tpm_hash = str(cfg.get("hash") or "sha256")
                tpm_key = str(cfg.get("key") or "ecc")
                tpm_pcr_bank = str(cfg.get("pcr_bank") or "sha256")
                raw_ids = cfg.get("pcr_ids") or []
                if isinstance(raw_ids, str):
                    tpm_pcr_ids = [v.strip() for v in raw_ids.split(",") if v.strip()]
                elif isinstance(raw_ids, list):
                    tpm_pcr_ids = [str(v).strip() for v in raw_ids if str(v).strip()]
                else:
                    tpm_pcr_ids = []

            self.initial["tpm_hash"] = tpm_hash
            self.initial["tpm_key"] = tpm_key
            self.initial["tpm_pcr_bank"] = tpm_pcr_bank
            self.initial["tpm_pcr_ids"] = tpm_pcr_ids

            self.initial["bootloader_user"] = str(cfg.get("grub_user") or "")
            self.initial["rhsm_username"] = str(cfg.get("username") or "")
            self.initial["rhsm_password"] = str(cfg.get("password") or "")
            self.initial["rhsm_org_id"] = str(cfg.get("org_id") or "")
            self.initial["rhsm_activation_key"] = str(cfg.get("activation_key") or "")
            self.initial["rhsm_repo_ids"] = str(cfg.get("repo_ids") or "")
            self.initial["rhsm_prompt_credentials"] = bool(cfg.get("prompt_credentials") or False)
            self.initial["rhsm_prompt_repositories"] = bool(cfg.get("prompt_repositories") or False)
            self.initial["wait_message"] = str(cfg.get("message") or "")
            self.initial["script_body"] = str(cfg.get("script_body") or "")
            if self.instance.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT:
                default_name = dict(AfterburnerItem.TYPE_CHOICES).get(AfterburnerItem.TYPE_CUSTOM_SCRIPT, "Custom script")
                self.initial["custom_name"] = self.instance.name if self.instance.name != default_name else ""
                self.initial["script_run_mode"] = str(cfg.get("run_mode") or self.CUSTOM_SCRIPT_RUN_MODE_NON_CHROOT)
                lines: list[str] = []
                payload: list[dict[str, str]] = []
                for row in self.instance.script_inputs.all().order_by("order", "id"):
                    lines.append(row.key)
                    payload.append(
                        {
                            "name": str(row.description or row.label or "").strip(),
                            "question": str(row.label or "").strip(),
                            "env_var": str(row.key or "").strip(),
                        }
                    )
                self.initial["script_questions_json"] = json.dumps(payload)

    def _parse_script_questions(self, raw_payload: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        raw = (raw_payload or "").strip()
        if not raw:
            return rows
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid questions payload: {exc.msg}") from exc

        if not isinstance(parsed, list):
            raise ValidationError("Invalid questions payload: expected a list")

        seen_env_vars: set[str] = set()
        for idx, entry in enumerate(parsed, start=1):
            if not isinstance(entry, dict):
                raise ValidationError(f"Question {idx}: invalid entry")

            name = str(entry.get("name") or "").strip()
            label = str(entry.get("question") or "").strip()
            key = str(entry.get("env_var") or "").strip().upper()

            if not name:
                raise ValidationError(f"Question {idx}: Name is required")
            if not label:
                raise ValidationError(f"Question {idx}: Question is required")
            if not key:
                raise ValidationError(f"Question {idx}: Environment variable name is required")
            if key in seen_env_vars:
                raise ValidationError(f"Question {idx}: Duplicate environment variable '{key}'")
            seen_env_vars.add(key)

            probe = AfterburnerScriptInput(
                item=self.instance if self.instance and self.instance.pk else AfterburnerItem(item_type=AfterburnerItem.TYPE_CUSTOM_SCRIPT),
                order=len(rows) + 1,
                key=key,
                label=label,
                input_type=AfterburnerScriptInput.TYPE_STRING,
                required=False,
                default_value="",
                select_options=[],
                description=name,
            )
            probe.item.item_type = AfterburnerItem.TYPE_CUSTOM_SCRIPT
            probe.clean()
            rows.append(
                {
                    "order": len(rows) + 1,
                    "key": probe.key,
                    "label": label,
                    "input_type": AfterburnerScriptInput.TYPE_STRING,
                    "required": False,
                    "default_value": "",
                    "select_options": [],
                    "description": name,
                }
            )
        return rows

    def clean(self):
        cleaned = super().clean()
        item_type = cleaned.get("item_type")
        config: dict[str, str] = {}

        if item_type == AfterburnerItem.TYPE_AD_JOIN:
            domain = str(cleaned.get("ad_domain") or "").strip()
            computer_ou = str(cleaned.get("ad_computer_ou") or "").strip()
            join_user = str(cleaned.get("ad_join_user") or "").strip()
            if computer_ou and not domain:
                self.add_error("ad_domain", "Domain is required when a computer OU is provided")
            if join_user and not domain:
                self.add_error("ad_domain", "Domain is required when a join user is provided")
            config = {
                "domain": domain,
                "computer_ou": computer_ou,
                "join_user": join_user,
            }
        elif item_type == AfterburnerItem.TYPE_STATIC_IP:
            interface = str(cleaned.get("static_interface") or "").strip()
            ip_address = str(cleaned.get("static_ip_address") or "").strip()
            prefix = str(cleaned.get("static_prefix") or "24").strip() or "24"
            gateway = str(cleaned.get("static_gateway") or "").strip()
            dns_raw = str(cleaned.get("static_dns") or "").strip()

            if any([ip_address, gateway, dns_raw]) and not interface:
                self.add_error("static_interface", "Interface is required when static network values are set")

            if gateway and not ip_address:
                self.add_error("static_ip_address", "IP address is required when a gateway is provided")

            if ip_address:
                prefix_int = int(prefix)
                if prefix_int < 1 or prefix_int > 32:
                    self.add_error("static_prefix", "Prefix must be between 1 and 32")

            if dns_raw:
                dns_values = [v.strip() for v in dns_raw.split(",") if v.strip()]
                invalid_dns = []
                for value in dns_values:
                    try:
                        ipaddress.ip_address(value)
                    except ValueError:
                        invalid_dns.append(value)
                if invalid_dns:
                    self.add_error("static_dns", f"Invalid DNS address(es): {', '.join(invalid_dns)}")

            config = {
                "interface": interface,
                "ip_address": ip_address,
                "prefix": prefix,
                "gateway": gateway,
                "dns": dns_raw,
            }
        elif item_type == AfterburnerItem.TYPE_LUKS_ROTATE:
            device = str(cleaned.get("luks_device") or "").strip()
            autodetect = bool(cleaned.get("luks_autodetect"))
            if device and not device.startswith("/dev/"):
                self.add_error("luks_device", "LUKS device should be a /dev/... path")
            if not autodetect and not device:
                self.add_error("luks_device", "Provide a device or enable autodetect")
            config = {
                "device": device,
                "autodetect": autodetect,
            }
        elif item_type == AfterburnerItem.TYPE_BOOTLOADER_PASSWORD:
            grub_user = str(cleaned.get("bootloader_user") or "").strip()
            config = {
                "grub_user": grub_user,
            }
        elif item_type == AfterburnerItem.TYPE_REDHAT_REGISTRATION:
            username = str(cleaned.get("rhsm_username") or "").strip()
            password = str(cleaned.get("rhsm_password") or "").strip()
            org_id = str(cleaned.get("rhsm_org_id") or "").strip()
            activation_key = str(cleaned.get("rhsm_activation_key") or "").strip()
            repo_ids = str(cleaned.get("rhsm_repo_ids") or "").strip()
            prompt_credentials = bool(cleaned.get("rhsm_prompt_credentials"))
            prompt_repositories = bool(cleaned.get("rhsm_prompt_repositories"))

            using_userpass = bool(username or password)
            using_activation_key = bool(org_id or activation_key)

            if using_userpass and (not username or not password):
                self.add_error("rhsm_username", "Username and password are required when using user/password mode")
                self.add_error("rhsm_password", "Username and password are required when using user/password mode")

            if using_activation_key and (not org_id or not activation_key):
                self.add_error("rhsm_org_id", "Org ID and activation key are required when using activation key mode")
                self.add_error("rhsm_activation_key", "Org ID and activation key are required when using activation key mode")

            if using_userpass and using_activation_key:
                self.add_error("rhsm_username", "Choose only one preset mode: username/password OR activation key/org")

            if not prompt_credentials and not using_userpass and not using_activation_key:
                self.add_error(
                    "rhsm_prompt_credentials",
                    "Provide preset credentials or enable prompting for credentials",
                )

            config = {
                "username": username,
                "password": password,
                "org_id": org_id,
                "activation_key": activation_key,
                "repo_ids": repo_ids,
                "prompt_credentials": prompt_credentials,
                "prompt_repositories": prompt_repositories,
            }
        elif item_type == AfterburnerItem.TYPE_TPM_INTEGRATION:
            device = str(cleaned.get("tpm_device") or "").strip()
            autodetect = bool(cleaned.get("tpm_autodetect"))
            tpm_hash = str(cleaned.get("tpm_hash") or "sha256").strip()
            tpm_pcr_bank = str(cleaned.get("tpm_pcr_bank") or "sha256").strip()
            tpm_key = str(cleaned.get("tpm_key") or "ecc").strip()
            raw_pcr_ids = [str(v).strip() for v in (cleaned.get("tpm_pcr_ids") or []) if str(v).strip()]
            pcr_ids = list(dict.fromkeys(raw_pcr_ids))

            hash_values = {value for value, _label in self.TPM_HASH_CHOICES}
            key_values = {value for value, _label in self.TPM_KEY_CHOICES}
            pcr_values = {value for value, _label in self.TPM_PCR_ID_CHOICES}

            if device and not device.startswith("/dev/"):
                self.add_error("tpm_device", "LUKS device should be a /dev/... path")
            if not autodetect and not device:
                self.add_error("tpm_device", "Provide a device or enable autodetect")
            if tpm_hash not in hash_values:
                self.add_error("tpm_hash", "Invalid TPM hash value")
            if tpm_pcr_bank not in hash_values:
                self.add_error("tpm_pcr_bank", "Invalid TPM PCR bank value")
            if tpm_key not in key_values:
                self.add_error("tpm_key", "Invalid TPM key value")
            if any(value not in pcr_values for value in pcr_ids):
                self.add_error("tpm_pcr_ids", "One or more PCR IDs are invalid")
            config = {
                "device": device,
                "autodetect": autodetect,
                "hash": tpm_hash,
                "pcr_bank": tpm_pcr_bank,
                "key": tpm_key,
                "pcr_ids": pcr_ids,
            }
        elif item_type == AfterburnerItem.TYPE_WAIT_FOR_ENTER:
            message = str(cleaned.get("wait_message") or "").strip()
            if not message:
                self.add_error("wait_message", "Please enter a message to show the user")
            config = {
                "message": message,
            }
        elif item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT:
            body = str(cleaned.get("script_body") or "")
            body = body.replace("\r\n", "\n").replace("\r", "\n").rstrip()
            run_mode = str(cleaned.get("script_run_mode") or self.CUSTOM_SCRIPT_RUN_MODE_NON_CHROOT).strip()
            allowed_modes = {value for value, _label in self.CUSTOM_SCRIPT_RUN_MODE_CHOICES}
            if run_mode not in allowed_modes:
                self.add_error("script_run_mode", "Invalid custom script run mode")
                run_mode = self.CUSTOM_SCRIPT_RUN_MODE_NON_CHROOT
            config = {
                "script_body": body,
                "run_mode": run_mode,
            }
            cleaned["item_custom_name"] = str(cleaned.get("custom_name") or "").strip()
            cleaned["item_script_inputs_payload"] = self._parse_script_questions(str(cleaned.get("script_questions_json") or ""))

        cleaned["item_config"] = config
        return cleaned

    def save(self, commit: bool = True):
        obj: AfterburnerItem = super().save(commit=False)
        display = dict(AfterburnerItem.TYPE_CHOICES).get(obj.item_type, obj.item_type)
        if obj.item_type == AfterburnerItem.TYPE_CUSTOM_SCRIPT:
            custom_name = str(self.cleaned_data.get("item_custom_name") or "").strip()
            obj.name = custom_name or display
        else:
            obj.name = display
        obj.description = ""
        obj.config = self.cleaned_data.get("item_config") or {}
        if commit:
            obj.save()
        return obj


class AfterburnerScriptInputForm(forms.ModelForm):
    select_options_csv = forms.CharField(
        required=False,
        help_text="Comma-separated values used when input type is Select",
        label="Select options",
    )

    class Meta:
        model = AfterburnerScriptInput
        fields = ["order", "key", "label", "input_type", "required", "default_value", "description"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.select_options:
            self.initial["select_options_csv"] = ", ".join(self.instance.select_options)

    def clean_select_options_csv(self):
        raw = (self.cleaned_data.get("select_options_csv") or "").strip()
        if not raw:
            return []
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            return []
        return values

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("input_type") == AfterburnerScriptInput.TYPE_SELECT and not cleaned.get("select_options_csv"):
            raise ValidationError("Select input type requires at least one option")
        return cleaned

    def save(self, commit: bool = True):
        obj: AfterburnerScriptInput = super().save(commit=False)
        obj.select_options = self.cleaned_data.get("select_options_csv") or []
        if commit:
            obj.save()
        return obj
