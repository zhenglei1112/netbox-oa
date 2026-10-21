from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0022_material_item_revisions")]

    operations = [
        migrations.AddField(
            model_name="maintenancedocument",
            name="removed_at",
            field=models.DateTimeField(null=True, blank=True, editable=False),
        ),
    ]
