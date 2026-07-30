from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("serverconfig", "0002_serverconfiguration_singleton_and_deployer_fields"),
    ]

    operations = [
        migrations.RenameField(
            model_name="serverconfiguration",
            old_name="deployer_base_image_path",
            new_name="builder_base_image_path",
        ),
        migrations.RenameField(
            model_name="serverconfiguration",
            old_name="deployer_image_label",
            new_name="builder_image_label",
        ),
        migrations.RenameField(
            model_name="serverconfiguration",
            old_name="deployer_image_source_url",
            new_name="builder_image_source_url",
        ),
    ]
