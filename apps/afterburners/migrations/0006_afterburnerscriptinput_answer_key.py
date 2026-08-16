from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("afterburners", "0005_alter_afterburneritem_item_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="afterburnerscriptinput",
            name="answer_key",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
