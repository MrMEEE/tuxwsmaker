from django.db import migrations, models


def migrate_boot_mode_both_to_uefi(apps, schema_editor):
    BuildMachineConfig = apps.get_model("builds", "BuildMachineConfig")
    BuildMachineConfig.objects.filter(boot_mode="both").update(boot_mode="uefi")


class Migration(migrations.Migration):

    dependencies = [
        ("builds", "0010_remove_buildmachineconfig_disk_gib"),
    ]

    operations = [
        migrations.RunPython(migrate_boot_mode_both_to_uefi, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="buildmachineconfig",
            name="boot_mode",
            field=models.CharField(
                choices=[("uefi", "UEFI"), ("bios", "BIOS")],
                default="uefi",
                max_length=8,
            ),
        ),
    ]
