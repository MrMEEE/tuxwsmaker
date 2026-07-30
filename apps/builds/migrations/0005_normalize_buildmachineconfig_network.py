# Generated manually to normalize existing build machine config networks.

from django.db import migrations


def forwards(apps, schema_editor):
	BuildMachineConfig = apps.get_model("builds", "BuildMachineConfig")
	BuildMachineConfig.objects.all().update(libvirt_network="wsbuildnet")


def backwards(apps, schema_editor):
	BuildMachineConfig = apps.get_model("builds", "BuildMachineConfig")
	BuildMachineConfig.objects.all().update(libvirt_network="default")


class Migration(migrations.Migration):

	dependencies = [
		("builds", "0004_buildplaybookselection_builddefinition_playbooks"),
	]

	operations = [
		migrations.RunPython(forwards, backwards),
	]