from django.db import migrations, models


def backfill_partition_numbers(apps, schema_editor):
    PartitionEntry = apps.get_model("layouts", "PartitionEntry")
    for entry in PartitionEntry.objects.all().order_by("layout_id", "order"):
        if not entry.partition_number:
            entry.partition_number = entry.order
            entry.save(update_fields=["partition_number"])


class Migration(migrations.Migration):

    dependencies = [
        ("layouts", "0003_partitionentry_lvm_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="partitionentry",
            name="partition_number",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_partition_numbers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="partitionentry",
            name="partition_number",
            field=models.PositiveIntegerField(blank=True),
        ),
        migrations.AlterUniqueTogether(
            name="partitionentry",
            unique_together={("layout", "order"), ("layout", "partition_number")},
        ),
    ]
