from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.playbooks.models import PlaybookRepository


class PlaybookInspectViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="playbooks-user", password="secret", is_local=True)
        self.client.login(username="playbooks-user", password="secret")

    @patch("apps.playbooks.views.inspect_repository")
    def test_repo_inspect_accepts_get_for_tree_refresh(self, mock_inspect):
        mock_inspect.return_value = {
            "branches": ["main"],
            "selected_branch": "main",
            "tree": {"name": "/", "type": "dir", "children": []},
        }

        response = self.client.get(
            reverse("playbooks:repo-inspect"),
            {
                "repo_url": "https://example.invalid/repo.git",
                "branch": "main",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ok"], True)
        mock_inspect.assert_called_once_with(
            repo_url="https://example.invalid/repo.git",
            preferred_branch="main",
            ssh_key=None,
            api_key="",
        )


class PlaybookRepositoryRefreshViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="playbooks-refresh", password="secret", is_local=True)
        self.client.login(username="playbooks-refresh", password="secret")
        self.repo = PlaybookRepository.objects.create(
            name="Repo Refresh",
            repo_url="https://example.invalid/repo-refresh.git",
            default_branch="main",
        )

    @patch("apps.playbooks.views.sync_playbooks")
    @patch("apps.playbooks.views.sync_branches")
    def test_refresh_view_runs_branch_and_playbook_sync(self, mock_sync_branches, mock_sync_playbooks):
        mock_sync_branches.return_value = ["main", "dev"]
        mock_sync_playbooks.return_value = [object(), object()]

        response = self.client.post(
            reverse("playbooks:repo-refresh", args=[self.repo.pk]),
            {"branch": "main"},
        )

        self.assertEqual(response.status_code, 302)
        mock_sync_branches.assert_called_once_with(self.repo)
        mock_sync_playbooks.assert_called_once_with(self.repo, "main")
