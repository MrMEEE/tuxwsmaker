from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("builds", "0013_alter_buildartifact_artifact_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="builddefinition",
            name="current_step",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("vm_shell", "Create VM"),
                    ("install_os", "Install OS"),
                    ("run_playbooks", "Run Playbooks"),
                    ("shutdown", "Shutdown"),
                    ("dump_partitions", "Dump Partitions"),
                    ("save_release", "Save Release"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
    ]
