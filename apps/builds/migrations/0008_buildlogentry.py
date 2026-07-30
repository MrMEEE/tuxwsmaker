from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

	dependencies = [
		("builds", "0007_sshkey"),
	]

	operations = [
		migrations.CreateModel(
			name="BuildLogEntry",
			fields=[
				("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
				("stage", models.CharField(blank=True, max_length=64)),
				("message", models.TextField()),
				("created_at", models.DateTimeField(auto_now_add=True)),
				(
					"build",
					models.ForeignKey(
						on_delete=django.db.models.deletion.CASCADE,
						related_name="logs",
						to="builds.builddefinition",
					),
				),
			],
			options={
				"ordering": ["created_at", "id"],
			},
		),
	]
