from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.repositories.forms import PackageRepositoryForm
from apps.repositories.models import PackageRepository
from apps.repositories.services import render_repository_preview
from apps.repositories.tasks import _parse_repolist_output, _summarize_rhsm_discovery_error


class PackageRepositoryFormTests(TestCase):
    def test_deb_repository_requires_suite_and_components(self):
        form = PackageRepositoryForm(
            data={
                "name": "deb-repo",
                "family": PackageRepository.FAMILY_DEB,
                "enabled": "on",
                "base_url": "https://repo.example.invalid/deb",
                "auth_type": PackageRepository.AUTH_NONE,
                "signing_mode": PackageRepository.SIGNING_NONE,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Suite / distribution is required", form.errors.as_text())

    def test_rpm_repository_requires_repoid(self):
        form = PackageRepositoryForm(
            data={
                "name": "rpm-repo",
                "family": PackageRepository.FAMILY_RPM,
                "enabled": "on",
                "base_url": "https://repo.example.invalid/rpm",
                "auth_type": PackageRepository.AUTH_NONE,
                "signing_mode": PackageRepository.SIGNING_NONE,
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Repository ID is required", form.errors.as_text())

    def test_deb_repository_preview_includes_signed_by_when_signing_enabled(self):
        form = PackageRepositoryForm(
            data={
                "name": "deb-repo",
                "family": PackageRepository.FAMILY_DEB,
                "enabled": "on",
                "base_url": "https://repo.example.invalid/deb",
                "auth_type": PackageRepository.AUTH_NONE,
                "signing_mode": PackageRepository.SIGNING_URL,
                "gpg_key_url": "https://repo.example.invalid/key.asc",
                "deb_suite": "jammy",
                "deb_components": "main universe",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        repo = form.save()
        preview = render_repository_preview(repo)
        self.assertIn("signed-by=/etc/apt/keyrings/tuxwsmaker-repo-", preview)
        self.assertIn("jammy main universe", preview)

    def test_rpm_repository_preview_uses_metalink_directive(self):
        form = PackageRepositoryForm(
            data={
                "name": "rpm-epel",
                "family": PackageRepository.FAMILY_RPM,
                "enabled": "on",
                "base_url": "https://mirrors.fedoraproject.org/metalink?repo=epel-10&arch=x86_64",
                "auth_type": PackageRepository.AUTH_NONE,
                "signing_mode": PackageRepository.SIGNING_NONE,
                "rpm_repoid": "epel-10",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        repo = form.save()
        preview = render_repository_preview(repo)
        self.assertIn("metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-10&arch=x86_64", preview)
        self.assertNotIn("baseurl=", preview)

    def test_rpm_repository_preview_normalizes_metalink_repodata_endpoint(self):
        form = PackageRepositoryForm(
            data={
                "name": "rpm-epel-alt",
                "family": PackageRepository.FAMILY_RPM,
                "enabled": "on",
                "base_url": "https://mirrors.fedoraproject.org/metalink/repodata/repomd.xml?repo=epel-z-10&arch=x86_64",
                "auth_type": PackageRepository.AUTH_NONE,
                "signing_mode": PackageRepository.SIGNING_NONE,
                "rpm_repoid": "epel-z-10",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_text())
        repo = form.save()
        preview = render_repository_preview(repo)
        self.assertIn("metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-z-10&arch=x86_64", preview)
        self.assertNotIn("/metalink/repodata/repomd.xml", preview)


class RHSMRepoDiscoveryParserTests(TestCase):
    def test_parse_repolist_verbose_extracts_source_metadata(self):
        sample = """
Repo-id      : rhel-10-baseos-rpms
Repo-name    : Red Hat Enterprise Linux 10 for x86_64 - BaseOS (RPMs)
Repo-status  : enabled
Repo-metalink: https://cdn.redhat.com/content/dist/rhel10/$releasever/x86_64/baseos/os

Repo-id      : rhel-10-appstream-rpms
Repo-name    : Red Hat Enterprise Linux 10 for x86_64 - AppStream (RPMs)
Repo-status  : disabled
Repo-baseurl : https://cdn.redhat.com/content/dist/rhel10/$releasever/x86_64/appstream/os
"""
        repos = _parse_repolist_output(sample)

        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]["repo_id"], "rhel-10-baseos-rpms")
        self.assertEqual(repos[0]["source_type"], "metalink")
        self.assertIn("cdn.redhat.com", repos[0]["source_url"])
        self.assertTrue(repos[0]["enabled_by_default"])

        self.assertEqual(repos[1]["repo_id"], "rhel-10-appstream-rpms")
        self.assertEqual(repos[1]["source_type"], "baseurl")
        self.assertIn("appstream", repos[1]["source_url"])
        self.assertFalse(repos[1]["enabled_by_default"])

    def test_parse_repolist_table_output_with_trailing_status(self):
        sample = """
repo id                                                        repo name                                                                 status
rhel-10-for-x86_64-baseos-rpms                                Red Hat Enterprise Linux 10 for x86_64 - BaseOS (RPMs)                    enabled
rhel-10-for-x86_64-appstream-rpms                             Red Hat Enterprise Linux 10 for x86_64 - AppStream (RPMs)                 disabled
"""
        repos = _parse_repolist_output(sample)

        self.assertEqual(len(repos), 2)
        self.assertEqual(repos[0]["repo_id"], "rhel-10-for-x86_64-baseos-rpms")
        self.assertTrue(repos[0]["enabled_by_default"])
        self.assertIn("BaseOS", repos[0]["name"])
        self.assertEqual(repos[1]["repo_id"], "rhel-10-for-x86_64-appstream-rpms")
        self.assertFalse(repos[1]["enabled_by_default"])


class RHSMRepositorySyncViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="sync-user", password="secret")
        self.client.force_login(self.user)

    @patch("apps.repositories.views.sync_rhsm_repository_catalog")
    def test_sync_view_passes_selected_major(self, sync_mock):
        sync_mock.return_value = {"created": 0, "updated": 0, "warnings": {}, "errors": {}}

        response = self.client.post(
            reverse("repositories:rhsm-sync"),
            {"rhel_major": "10"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_called_once_with(versions_override=[10])

    @patch("apps.repositories.views.sync_rhsm_repository_catalog")
    def test_sync_view_ignores_invalid_major(self, sync_mock):
        sync_mock.return_value = {"created": 0, "updated": 0, "warnings": {}, "errors": {}}

        response = self.client.post(
            reverse("repositories:rhsm-sync"),
            {"rhel_major": "not-an-int"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_called_once_with(versions_override=None)


class RHSMDiscoveryWarningTests(TestCase):
    def test_summarize_rhsm_discovery_error_prefers_credential_message(self):
        message = (
            "Warning: Permanently added '192.168.200.10' (ED25519) to the list of known hosts.\n"
            "RHSM_DISCOVERY credentials are not configured"
        )

        self.assertEqual(
            _summarize_rhsm_discovery_error(RuntimeError(message)),
            "RHSM discovery credentials are not configured in server configuration",
        )

    def test_summarize_rhsm_discovery_error_reports_invalid_credentials(self):
        message = (
            "Warning: Permanently added '192.168.200.10' (ED25519) to the list of known hosts.\n"
            "Invalid username or password. To create a login, please visit https://www.redhat.com/wapps/ugc/register.html "
            "(HTTP error code 401: Unauthorized)"
        )

        self.assertEqual(
            _summarize_rhsm_discovery_error(RuntimeError(message)),
            "Server configuration RHSM credentials were rejected (invalid username or password)",
        )
