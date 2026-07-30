from django.db import migrations, models


def backfill_iso_os_version(apps, schema_editor):
	ISOImage = apps.get_model("catalog", "ISOImage")
	for iso in ISOImage.objects.all().iterator():
		if not iso.os_version:
			iso.os_version = iso.version
			iso.save(update_fields=["os_version"])


class Migration(migrations.Migration):

	dependencies = [
		("catalog", "0001_initial"),
	]

	operations = [
		migrations.AddField(
			model_name="isoimage",
			name="os_version",
			field=models.CharField(default="", max_length=64),
			preserve_default=False,
		),
		migrations.RunPython(backfill_iso_os_version, migrations.RunPython.noop),
	]