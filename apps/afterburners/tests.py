from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.afterburners.forms import AfterburnerItemForm
from apps.afterburners.models import AfterburnerItem, AfterburnerProfile, AfterburnerScriptInput
from apps.afterburners.services import RHSM_REPO_IDS_FILENAME, _build_item_snippet
from apps.afterburners.views import _custom_script_example_lines


class AfterburnerScriptInputModelTests(TestCase):
    def setUp(self):
        self.profile = AfterburnerProfile.objects.create(name="base", description="")
        self.custom_item = AfterburnerItem.objects.create(
            profile=self.profile,
            order=1,
            name="custom",
            item_type=AfterburnerItem.TYPE_CUSTOM_SCRIPT,
            config={"script_body": "echo ok"},
        )

    def test_key_is_normalized_to_uppercase(self):
        row = AfterburnerScriptInput(
            item=self.custom_item,
            order=1,
            key="team_name",
            label="Team",
            input_type=AfterburnerScriptInput.TYPE_STRING,
        )

        row.full_clean()
        self.assertEqual(row.key, "TEAM_NAME")

    def test_invalid_env_key_is_rejected(self):
        row = AfterburnerScriptInput(
            item=self.custom_item,
            order=1,
            key="1bad-key",
            label="Bad",
            input_type=AfterburnerScriptInput.TYPE_STRING,
        )

        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_select_requires_non_empty_string_options(self):
        row = AfterburnerScriptInput(
            item=self.custom_item,
            order=1,
            key="MODE",
            label="Mode",
            input_type=AfterburnerScriptInput.TYPE_SELECT,
            select_options=[],
        )

        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_script_input_disallowed_for_non_custom_items(self):
        normal_item = AfterburnerItem.objects.create(
            profile=self.profile,
            order=2,
            name="hostname",
            item_type=AfterburnerItem.TYPE_HOSTNAME,
            config={},
        )
        row = AfterburnerScriptInput(
            item=normal_item,
            order=1,
            key="HOST",
            label="Host",
            input_type=AfterburnerScriptInput.TYPE_STRING,
        )

        with self.assertRaises(ValidationError):
            row.full_clean()

    def test_non_select_input_clears_select_options(self):
        row = AfterburnerScriptInput(
            item=self.custom_item,
            order=1,
            key="RETRIES",
            label="Retries",
            input_type=AfterburnerScriptInput.TYPE_INT,
            select_options=["1", "2"],
        )

        row.full_clean()
        self.assertEqual(row.select_options, [])


class AfterburnerItemFormTests(TestCase):
    def setUp(self):
        self.profile = AfterburnerProfile.objects.create(name="base-form", description="")

    def test_luks_autodetect_allows_blank_device(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_LUKS_ROTATE,
                "luks_autodetect": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertTrue(item.config["autodetect"])
        self.assertEqual(item.config["device"], "")

    def test_luks_requires_device_or_autodetect(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_LUKS_ROTATE,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Provide a device or enable autodetect", form.errors.as_text())

    def test_wait_for_enter_requires_message(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_WAIT_FOR_ENTER,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Please enter a message to show the user", form.errors.as_text())

    def test_wait_for_enter_saves_message(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_WAIT_FOR_ENTER,
                "wait_message": "Press Enter to continue",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config["message"], "Press Enter to continue")

    def test_bootloader_password_saves_default_grub_user(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_BOOTLOADER_PASSWORD,
                "bootloader_user": "admin",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config["grub_user"], "admin")

    def test_local_user_supports_preset_groups(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_LOCAL_USER,
                "local_user_groups": "wheel, developers, qa",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config["groups"], "wheel, developers, qa")
        self.assertFalse(item.config["prompt_groups"])

    def test_local_user_supports_prompt_for_groups(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_LOCAL_USER,
                "local_user_prompt_groups": "on",
                "local_user_groups": "wheel",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertTrue(item.config["prompt_groups"])
        self.assertEqual(item.config["groups"], "wheel")

    def test_tpm_integration_requires_device_or_autodetect(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_TPM_INTEGRATION,
                "tpm_hash": "sha256",
                "tpm_pcr_bank": "sha256",
                "tpm_key": "ecc",
                "tpm_pcr_ids": [],
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Provide a device or enable autodetect", form.errors.as_text())

    def test_tpm_integration_saves_config(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_TPM_INTEGRATION,
                "tpm_device": "/dev/sda3",
                "tpm_hash": "sha384",
                "tpm_pcr_bank": "sha256",
                "tpm_key": "rsa",
                "tpm_pcr_ids": ["7", "11"],
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config["device"], "/dev/sda3")
        self.assertEqual(item.config["hash"], "sha384")
        self.assertEqual(item.config["pcr_bank"], "sha256")
        self.assertEqual(item.config["key"], "rsa")
        self.assertEqual(item.config["pcr_ids"], ["7", "11"])

    def test_tpm_integration_allows_empty_pcr_ids(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_TPM_INTEGRATION,
                "tpm_device": "/dev/sda3",
                "tpm_hash": "sha256",
                "tpm_pcr_bank": "sha256",
                "tpm_key": "ecc",
                "tpm_pcr_ids": [],
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config["pcr_ids"], [])

    def test_redhat_registration_requires_prompt_or_preset_credentials(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_REDHAT_REGISTRATION,
                "rhsm_prompt_credentials": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Provide preset credentials or enable prompting for credentials", form.errors.as_text())

    def test_redhat_registration_accepts_userpass_preset(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_REDHAT_REGISTRATION,
                "rhsm_username": "rh-user",
                "rhsm_password": "secret",
                "rhsm_repo_ids": "rhel-10-baseos-rpms,rhel-10-appstream-rpms",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config["username"], "rh-user")
        self.assertEqual(item.config["repo_ids"], "rhel-10-baseos-rpms,rhel-10-appstream-rpms")

    def test_redhat_registration_accepts_prompt_mode(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_REDHAT_REGISTRATION,
                "rhsm_prompt_credentials": "on",
                "rhsm_prompt_repositories": "on",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_custom_script_run_mode_defaults_to_non_chroot(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_CUSTOM_SCRIPT,
                "custom_name": "Update Packages",
                "script_body": "echo ok",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config.get("run_mode"), "non_chroot")

    def test_custom_script_run_mode_can_be_chroot(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_CUSTOM_SCRIPT,
                "custom_name": "Update Packages",
                "script_body": "echo ok",
                "script_run_mode": "chroot",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        item = form.save(commit=False)
        item.profile = self.profile
        item.order = 1
        item.save()
        self.assertEqual(item.config.get("run_mode"), "chroot")

    def test_custom_script_questions_json_builds_input_payload(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_CUSTOM_SCRIPT,
                "custom_name": "Update Packages",
                "script_body": "#!/bin/bash\necho ok\n",
                "script_questions_json": json.dumps([
                    {
                        "name": "Repo channel",
                        "question": "Which channel should be used?",
                        "env_var": "REPO_CHANNEL",
                    }
                ]),
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        payload = form.cleaned_data["item_script_inputs_payload"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["description"], "Repo channel")
        self.assertEqual(payload[0]["label"], "Which channel should be used?")
        self.assertEqual(payload[0]["key"], "REPO_CHANNEL")

    def test_custom_script_questions_json_requires_required_fields(self):
        form = AfterburnerItemForm(
            data={
                "item_type": AfterburnerItem.TYPE_CUSTOM_SCRIPT,
                "custom_name": "Update Packages",
                "script_body": "echo ok",
                "script_questions_json": json.dumps([
                    {
                        "name": "",
                        "question": "",
                        "env_var": "",
                    }
                ]),
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Question 1: Name is required", form.errors.as_text())


class AfterburnerRedHatRegistrationSnippetTests(TestCase):
    def test_snippet_prompts_for_credentials_when_enabled(self):
        profile = AfterburnerProfile.objects.create(name="rhsm", description="")
        item = AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="rhsm registration",
            item_type=AfterburnerItem.TYPE_REDHAT_REGISTRATION,
            config={
                "prompt_credentials": True,
                "prompt_repositories": True,
                "repo_ids": "rhel-10-baseos-rpms",
            },
        )

        snippet = _build_item_snippet(item)
        self.assertIn("prompt_bool RHSM_USE_ACTIVATION_KEY", snippet)
        self.assertIn("while true; do", snippet)
        self.assertIn("Red Hat registration failed. Try again.", snippet)
        self.assertIn("if run_chroot subscription-manager register --force --org", snippet)
        self.assertIn("if run_chroot subscription-manager register --force --username", snippet)
        self.assertIn("prompt_password RHSM_PASSWORD", snippet)
        self.assertIn("prompt_text RHSM_REPO_IDS_USER", snippet)
        self.assertIn("subscription-manager repos --enable=", snippet)
        self.assertIn(f"RHSM_REPO_FILE=/run/install/repo/deploy/{RHSM_REPO_IDS_FILENAME}", snippet)

    def test_snippet_uses_preset_credentials_when_not_prompting(self):
        profile = AfterburnerProfile.objects.create(name="rhsm-preset", description="")
        item = AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="rhsm preset",
            item_type=AfterburnerItem.TYPE_REDHAT_REGISTRATION,
            config={
                "username": "preset-user",
                "password": "preset-pass",
                "prompt_credentials": False,
                "prompt_repositories": False,
                "repo_ids": "rhel-10-baseos-rpms",
            },
        )

        snippet = _build_item_snippet(item)
        self.assertIn("register --force --username", snippet)
        self.assertIn("RHSM_REPO_IDS_CONFIG=rhel-10-baseos-rpms", snippet)
        self.assertIn("RHSM_REPO_IDS_MERGED", snippet)


class AfterburnerLocalUserSnippetTests(TestCase):
    def test_snippet_uses_prompted_groups_when_enabled(self):
        profile = AfterburnerProfile.objects.create(name="local-user-groups", description="")
        item = AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="local user",
            item_type=AfterburnerItem.TYPE_LOCAL_USER,
            config={
                "groups": "wheel,developers",
                "prompt_groups": True,
            },
        )

        snippet = _build_item_snippet(item)
        self.assertIn("prompt_text LOCAL_USER_GROUPS", snippet)
        self.assertIn("run_chroot groupadd -f", snippet)
        self.assertIn("run_chroot usermod -aG \"$LOCAL_USER_GROUP\"", snippet)

    def test_snippet_uses_preset_groups_when_prompt_disabled(self):
        profile = AfterburnerProfile.objects.create(name="local-user-static-groups", description="")
        item = AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="local user",
            item_type=AfterburnerItem.TYPE_LOCAL_USER,
            config={
                "groups": "ops,qa",
                "prompt_groups": False,
            },
        )

        snippet = _build_item_snippet(item)
        self.assertIn("LOCAL_USER_GROUPS=ops,qa", snippet)
        self.assertIn("LOCAL_USER_GROUP_SEEN", snippet)


class AfterburnerCustomScriptExampleTests(TestCase):
    def test_password_inputs_use_secret_safe_example_lines(self):
        profile = AfterburnerProfile.objects.create(name="example", description="")
        item = AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="custom",
            item_type=AfterburnerItem.TYPE_CUSTOM_SCRIPT,
            config={"script_body": "echo ok"},
        )
        AfterburnerScriptInput.objects.create(
            item=item,
            order=1,
            key="API_TOKEN",
            label="API token",
            input_type=AfterburnerScriptInput.TYPE_PASSWORD,
        )
        AfterburnerScriptInput.objects.create(
            item=item,
            order=2,
            key="ENVIRONMENT",
            label="Environment",
            input_type=AfterburnerScriptInput.TYPE_SELECT,
            select_options=["dev", "prod"],
        )

        lines = _custom_script_example_lines(item)

        self.assertIn("# API_TOKEN is a secret. Do not print or log it.", lines)
        self.assertIn(': "${API_TOKEN:?API_TOKEN is required}"', lines)
        self.assertIn('echo "Using ${ENVIRONMENT}"', lines)
        self.assertNotIn('echo "Using ${API_TOKEN}"', lines)


class AfterburnerCustomScriptRunModeSnippetTests(TestCase):
    def test_chroot_custom_script_uses_run_chroot(self):
        profile = AfterburnerProfile.objects.create(name="snippet-mode", description="")
        item = AfterburnerItem.objects.create(
            profile=profile,
            order=1,
            name="custom",
            item_type=AfterburnerItem.TYPE_CUSTOM_SCRIPT,
            config={"script_body": "echo ok", "run_mode": "chroot"},
        )

        snippet = _build_item_snippet(item)

        self.assertIn("run_chroot bash /tmp/tuxws-afterburner-custom.sh", snippet)
        self.assertIn("$TARGET_ROOT/tmp/tuxws-afterburner-custom.sh", snippet)


class AfterburnerOrderingViewsTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="tester", password="secret")
        self.client.force_login(self.user)

        self.profile = AfterburnerProfile.objects.create(name="order-profile", description="")
        self.item_a = AfterburnerItem.objects.create(
            profile=self.profile,
            order=1,
            name="A",
            item_type=AfterburnerItem.TYPE_HOSTNAME,
            config={},
        )
        self.item_b = AfterburnerItem.objects.create(
            profile=self.profile,
            order=2,
            name="B",
            item_type=AfterburnerItem.TYPE_LOCAL_USER,
            config={},
        )
        self.item_c = AfterburnerItem.objects.create(
            profile=self.profile,
            order=3,
            name="C",
            item_type=AfterburnerItem.TYPE_WAIT_FOR_ENTER,
            config={"message": "continue"},
        )
        self.custom_item = AfterburnerItem.objects.create(
            profile=self.profile,
            order=4,
            name="Custom",
            item_type=AfterburnerItem.TYPE_CUSTOM_SCRIPT,
            config={"script_body": "echo ok"},
        )
        self.input_a = AfterburnerScriptInput.objects.create(
            item=self.custom_item,
            order=1,
            key="ENVIRONMENT",
            label="Environment",
            input_type=AfterburnerScriptInput.TYPE_STRING,
        )
        self.input_b = AfterburnerScriptInput.objects.create(
            item=self.custom_item,
            order=2,
            key="TEAM",
            label="Team",
            input_type=AfterburnerScriptInput.TYPE_STRING,
        )

    def test_move_view_swaps_items_without_unique_constraint_failure(self):
        response = self.client.post(reverse("afterburners:item-move", args=[self.item_b.id, "up"]))

        self.assertEqual(response.status_code, 302)
        ordered_ids = list(
            AfterburnerItem.objects.filter(profile=self.profile).order_by("order", "id").values_list("id", flat=True)
        )
        self.assertEqual(ordered_ids, [self.item_b.id, self.item_a.id, self.item_c.id, self.custom_item.id])

    def test_reorder_view_applies_drag_drop_order(self):
        response = self.client.post(
            reverse("afterburners:item-reorder", args=[self.profile.id]),
            {"ordered_ids": f"{self.item_c.id},{self.item_a.id},{self.item_b.id},{self.custom_item.id}"},
        )

        self.assertEqual(response.status_code, 302)
        ordered_ids = list(
            AfterburnerItem.objects.filter(profile=self.profile).order_by("order", "id").values_list("id", flat=True)
        )
        self.assertEqual(ordered_ids, [self.item_c.id, self.item_a.id, self.item_b.id, self.custom_item.id])

    def test_script_input_reorder_view_applies_drag_drop_order(self):
        response = self.client.post(
            reverse("afterburners:input-reorder", args=[self.custom_item.id]),
            {"ordered_ids": f"{self.input_b.id},{self.input_a.id}"},
        )

        self.assertEqual(response.status_code, 302)
        ordered_ids = list(
            AfterburnerScriptInput.objects.filter(item=self.custom_item)
            .order_by("order", "id")
            .values_list("id", flat=True)
        )
        self.assertEqual(ordered_ids, [self.input_b.id, self.input_a.id])
