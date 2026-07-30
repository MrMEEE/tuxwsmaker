from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("layouts", "0002_layout_table_type_and_filesystem_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="partitionentry",
            name="entry_role",
            field=models.CharField(
                choices=[
                    ("standard", "Standard partition"),
                    ("pv", "LVM physical volume (PV)"),
                    ("lv", "LVM logical volume (LV)"),
                ],
                default="standard",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="partitionentry",
            name="logical_volume",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="partitionentry",
            name="volume_group",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
