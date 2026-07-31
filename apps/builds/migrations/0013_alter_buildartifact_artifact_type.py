from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("builds", "0012_builddefinition_run_mode_current_step_and_runtime_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="buildartifact",
            name="artifact_type",
            field=models.CharField(
                choices=[("pxe", "PXE bundle"), ("usb", "USB image"), ("clone", "Clone release")],
                max_length=8,
            ),
        ),
    ]
