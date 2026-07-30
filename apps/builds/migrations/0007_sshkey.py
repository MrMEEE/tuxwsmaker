from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

	dependencies = [
		("builds", "0006_alter_buildmachineconfig_libvirt_network"),
	]

	operations = [
		migrations.CreateModel(
			name="SSHKey",
			fields=[
				("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
				("name", models.CharField(max_length=120)),
				("private_key_encrypted", models.TextField(blank=True)),
				("public_key", models.TextField(blank=True)),
				("created_at", models.DateTimeField(auto_now_add=True)),
				("updated_at", models.DateTimeField(auto_now=True)),
				(
					"build",
					models.ForeignKey(
						blank=True,
						null=True,
						on_delete=django.db.models.deletion.CASCADE,
						related_name="ssh_keys",
						to="builds.builddefinition",
					),
				),
			],
			options={
				"ordering": ["-created_at"],
				"unique_together": (("build", "name"),),
			},
		),
	]
