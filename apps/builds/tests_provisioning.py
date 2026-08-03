from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps.builds.services.provisioning import AnsibleProvisioner


class AnsibleProvisionerClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provisioner = AnsibleProvisioner(project_root=Path.cwd())

    def test_classifies_task_list_with_yaml_document_marker(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yml", delete=False) as handle:
            handle.write("---\n- name: Example task\n  debug:\n    msg: hello\n")
            path = Path(handle.name)

        try:
            self.assertEqual(self.provisioner._classify_ansible_file(path), "task_list")
        finally:
            path.unlink(missing_ok=True)

    def test_classifies_playbook_with_yaml_document_marker(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yml", delete=False) as handle:
            handle.write("---\n- hosts: all\n  tasks:\n    - name: Example play\n")
            path = Path(handle.name)

        try:
            self.assertEqual(self.provisioner._classify_ansible_file(path), "playbook")
        finally:
            path.unlink(missing_ok=True)
