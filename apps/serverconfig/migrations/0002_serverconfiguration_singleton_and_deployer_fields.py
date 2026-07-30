from django.db import migrations, models


def normalize_singleton(apps, schema_editor):
    ServerConfiguration = apps.get_model("serverconfig", "ServerConfiguration")

    first = ServerConfiguration.objects.order_by("id").first()
    if first is None:
        ServerConfiguration.objects.create(
            id=1,
            name="default",
            concurrent_builds=2,
            artifact_retention_days=30,
            enable_artifact_compression=True,
            notes="",
        )
        return

    data = {
        "concurrent_builds": first.concurrent_builds,
        "artifact_retention_days": first.artifact_retention_days,
        "enable_artifact_compression": first.enable_artifact_compression,
        "deployer_base_image_path": getattr(first, "deployer_base_image_path", ""),
        "deployer_image_label": getattr(first, "deployer_image_label", ""),
        "deployer_image_source_url": getattr(first, "deployer_image_source_url", ""),
        "notes": first.notes,
    }

    ServerConfiguration.objects.exclude(pk=first.pk).delete()
    if first.pk != 1:
        first.delete()
        ServerConfiguration.objects.create(id=1, name="default", **data)
    else:
        for field, value in data.items():
            setattr(first, field, value)
        first.name = "default"
        first.save()


class Migration(migrations.Migration):

    dependencies = [
        ("serverconfig", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="serverconfiguration",
            name="deployer_base_image_path",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="serverconfiguration",
            name="deployer_image_label",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="serverconfiguration",
            name="deployer_image_source_url",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.RunPython(normalize_singleton, migrations.RunPython.noop),
    ]
