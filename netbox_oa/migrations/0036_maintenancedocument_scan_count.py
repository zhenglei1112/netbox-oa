from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0035_alter_maintenancedocument_material")]

    operations = [
        migrations.AddField(
            model_name="maintenancedocument",
            name="scan_count",
            field=models.PositiveBigIntegerField(
                default=0, editable=False, verbose_name="扫描次数"
            ),
        ),
    ]
