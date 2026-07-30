from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("layouts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="partitionlayout",
            name="table_type",
            field=models.CharField(
                choices=[("gpt", "GPT"), ("mbr", "MBR")],
                default="gpt",
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="partitionentry",
            name="filesystem",
            field=models.CharField(
                choices=[
                    ("ext2", "ext2"),
                    ("ext3", "ext3"),
                    ("ext4", "ext4"),
                    ("xfs", "xfs"),
                    ("btrfs", "btrfs"),
                    ("fat32", "fat32"),
                    ("efi", "efi"),
                    ("swap", "swap"),
                    ("none", "none (raw/unformatted)"),
                ],
                default="ext4",
                max_length=32,
            ),
        ),
    ]
