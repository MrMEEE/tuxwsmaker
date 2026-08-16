from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("builds", "0022_buildplaybookselection_run_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="builddefinition",
            name="enable_answers_file_support",
            field=models.BooleanField(default=False),
        ),
    ]
