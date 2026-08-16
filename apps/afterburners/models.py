from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import models


_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class AfterburnerProfile(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class AfterburnerItem(models.Model):
    TYPE_HOSTNAME = "hostname"
    TYPE_LOCAL_USER = "local_user"
    TYPE_AD_JOIN = "ad_join"
    TYPE_STATIC_IP = "static_ip"
    TYPE_LUKS_ROTATE = "luks_rotate"
    TYPE_BOOTLOADER_PASSWORD = "bootloader_password"
    TYPE_TPM_INTEGRATION = "tpm_integration"
    TYPE_REDHAT_REGISTRATION = "redhat_registration"
    TYPE_WAIT_FOR_ENTER = "wait_for_enter"
    TYPE_CUSTOM_SCRIPT = "custom_script"
    TYPE_CHOICES = [
        (TYPE_HOSTNAME, "Hostname"),
        (TYPE_LOCAL_USER, "Local user"),
        (TYPE_AD_JOIN, "AD join"),
        (TYPE_STATIC_IP, "Static IP"),
        (TYPE_LUKS_ROTATE, "Set LUKS password"),
        (TYPE_BOOTLOADER_PASSWORD, "Set Bootloader password"),
        (TYPE_TPM_INTEGRATION, "TPM integration"),
        (TYPE_REDHAT_REGISTRATION, "Red Hat registration"),
        (TYPE_WAIT_FOR_ENTER, "Wait for Enter"),
        (TYPE_CUSTOM_SCRIPT, "Custom script"),
    ]

    profile = models.ForeignKey(AfterburnerProfile, on_delete=models.CASCADE, related_name="items")
    order = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    item_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        unique_together = (("profile", "order"),)

    def __str__(self) -> str:
        return f"{self.profile.name}:{self.order}:{self.name}"

    def clean(self) -> None:
        if not isinstance(self.config, dict):
            raise ValidationError("Item config must be a JSON object")


class AfterburnerScriptInput(models.Model):
    TYPE_STRING = "string"
    TYPE_PASSWORD = "password"
    TYPE_BOOL = "bool"
    TYPE_INT = "int"
    TYPE_SELECT = "select"
    TYPE_CHOICES = [
        (TYPE_STRING, "String"),
        (TYPE_PASSWORD, "Password"),
        (TYPE_BOOL, "Boolean"),
        (TYPE_INT, "Integer"),
        (TYPE_SELECT, "Select"),
    ]

    item = models.ForeignKey(AfterburnerItem, on_delete=models.CASCADE, related_name="script_inputs")
    order = models.PositiveIntegerField(default=1)
    key = models.CharField(max_length=64)
    label = models.CharField(max_length=120)
    input_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=TYPE_STRING)
    required = models.BooleanField(default=False)
    default_value = models.CharField(max_length=255, blank=True)
    answer_key = models.CharField(max_length=120, blank=True)
    select_options = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "id"]
        unique_together = (("item", "order"), ("item", "key"))

    def __str__(self) -> str:
        return f"{self.item.name}:{self.key}"

    def clean(self) -> None:
        key = (self.key or "").strip().upper()
        self.key = key
        if not _ENV_VAR_RE.match(key):
            raise ValidationError({"key": "Input key must be a valid shell env var name (e.g. TEAM_NAME)"})

        if self.item.item_type != AfterburnerItem.TYPE_CUSTOM_SCRIPT:
            raise ValidationError("Script inputs are only allowed on custom script afterburner items")

        if self.input_type == self.TYPE_SELECT:
            if (
                not isinstance(self.select_options, list)
                or not self.select_options
                or not all(isinstance(v, str) and v for v in self.select_options)
            ):
                raise ValidationError({"select_options": "Select inputs require a non-empty list of string options"})
        else:
            if self.select_options:
                self.select_options = []
