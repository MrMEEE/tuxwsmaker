from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("builds", "0021_buildrhsmrepositoryselection"),
    ]

    operations = [
        migrations.AddField(
            model_name="buildplaybookselection",
            name="run_mode",
            field=models.CharField(
                choices=[("non_chroot", "Non-chroot"), ("chroot", "Chroot")],
                default="non_chroot",
                max_length=16,
            ),
        ),
    ]
