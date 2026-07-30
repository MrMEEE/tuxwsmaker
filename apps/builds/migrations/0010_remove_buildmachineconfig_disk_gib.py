from django.db import migrations


class Migration(migrations.Migration):

	dependencies = [
		("builds", "0009_merge_0008_alter_sshkey_unique_together_sshkey_owner_and_more_0008_buildlogentry"),
	]

	operations = [
		migrations.RemoveField(
			model_name="buildmachineconfig",
			name="disk_gib",
		),
	]