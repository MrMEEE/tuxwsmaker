from django.test import TestCase

from apps.layouts.forms import PartitionEntryForm
from apps.layouts.models import PartitionEntry, PartitionLayout


class PartitionEntrySchemaTests(TestCase):
	def setUp(self):
		self.layout = PartitionLayout.objects.create(name="layout")

	def test_luks_must_be_on_pv_container(self):
		form = PartitionEntryForm(
			data={
				"order": 1,
				"name": "root",
				"entry_role": PartitionEntry.ROLE_STANDARD,
				"mount_point": "/",
				"filesystem": "xfs",
				"size_mode": PartitionEntry.SIZE_FIXED,
				"size_mib": 1024,
				"luks_enabled": "on",
				"luks_name": "cryptsys",
			}
		)

		self.assertFalse(form.is_valid())
		self.assertIn("LUKS can only be enabled on the encrypted PV container", form.errors.as_text())

	def test_encrypted_pv_container_is_allowed(self):
		form = PartitionEntryForm(
			data={
				"order": 1,
				"partition_number": 7,
				"name": "crypt container",
				"entry_role": PartitionEntry.ROLE_PV,
				"mount_point": "",
				"filesystem": "none",
				"size_mode": PartitionEntry.SIZE_FIXED,
				"size_mib": 8192,
				"volume_group": "cryptsys",
				"logical_volume": "",
				"luks_enabled": "on",
				"luks_name": "cryptsys",
			}
		)

		self.assertTrue(form.is_valid(), form.errors.as_text())
		entry = form.save(commit=False)
		self.assertEqual(entry.partition_number, 7)

	def test_partition_number_defaults_to_order(self):
		entry = PartitionEntry(layout=self.layout, order=2, name="root")
		entry.save()

		self.assertEqual(entry.partition_number, 2)
