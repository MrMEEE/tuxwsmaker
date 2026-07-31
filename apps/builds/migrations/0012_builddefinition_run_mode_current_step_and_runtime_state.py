from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("builds", "0011_boot_mode_bios_or_uefi"),
    ]

    operations = [
        migrations.AddField(
            model_name="builddefinition",
            name="current_step",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("vm_shell", "Create VM Shell"),
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
        migrations.AddField(
            model_name="builddefinition",
            name="run_mode",
            field=models.CharField(
                choices=[("auto", "Automatic"), ("manual", "Manual")],
                default="auto",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="builddefinition",
            name="runtime_state",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
