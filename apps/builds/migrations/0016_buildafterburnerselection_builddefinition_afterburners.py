from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("afterburners", "0001_initial"),
        ("builds", "0015_buildartifact_release_group_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="BuildAfterburnerSelection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField(default=1)),
                (
                    "afterburner",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="build_selections", to="afterburners.afterburnerprofile"),
                ),
                (
                    "build",
                    models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="afterburner_selections", to="builds.builddefinition"),
                ),
            ],
            options={
                "ordering": ["order", "id"],
                "unique_together": {("build", "afterburner"), ("build", "order")},
            },
        ),
        migrations.AddField(
            model_name="builddefinition",
            name="afterburners",
            field=models.ManyToManyField(blank=True, related_name="build_definitions", through="builds.BuildAfterburnerSelection", to="afterburners.afterburnerprofile"),
        ),
    ]
