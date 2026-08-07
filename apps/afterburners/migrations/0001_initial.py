from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AfterburnerProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="AfterburnerItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField(default=1)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                (
                    "item_type",
                    models.CharField(
                        choices=[
                            ("hostname", "Hostname"),
                            ("local_user", "Local user"),
                            ("ad_join", "AD join"),
                            ("static_ip", "Static IP"),
                            ("luks_rotate", "Set LUKS password"),
                            ("custom_script", "Custom script"),
                        ],
                        max_length=32,
                    ),
                ),
                ("config", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "profile",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="items", to="afterburners.afterburnerprofile"),
                ),
            ],
            options={"ordering": ["order", "id"], "unique_together": {("profile", "order")}},
        ),
        migrations.CreateModel(
            name="AfterburnerScriptInput",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField(default=1)),
                ("key", models.CharField(max_length=64)),
                ("label", models.CharField(max_length=120)),
                (
                    "input_type",
                    models.CharField(
                        choices=[
                            ("string", "String"),
                            ("password", "Password"),
                            ("bool", "Boolean"),
                            ("int", "Integer"),
                            ("select", "Select"),
                        ],
                        default="string",
                        max_length=16,
                    ),
                ),
                ("required", models.BooleanField(default=False)),
                ("default_value", models.CharField(blank=True, max_length=255)),
                ("select_options", models.JSONField(blank=True, default=list)),
                ("description", models.TextField(blank=True)),
                (
                    "item",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="script_inputs", to="afterburners.afterburneritem"),
                ),
            ],
            options={"ordering": ["order", "id"], "unique_together": {("item", "order"), ("item", "key")}},
        ),
    ]
