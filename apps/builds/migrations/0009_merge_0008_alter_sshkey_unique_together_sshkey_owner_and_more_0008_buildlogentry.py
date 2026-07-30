from django.db import migrations


class Migration(migrations.Migration):

	dependencies = [
		("builds", "0008_alter_sshkey_unique_together_sshkey_owner_and_more"),
		("builds", "0008_buildlogentry"),
	]

	operations = []
