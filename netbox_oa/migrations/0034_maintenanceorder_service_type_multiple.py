import json

from django.db import migrations, models

import netbox_oa.validators


def prepare_service_types(apps, schema_editor):
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(
        schema_editor.connection.alias
    )
    for order in orders.only("pk", "service_type").iterator():
        # Encode before changing the column type: PostgreSQL requires valid JSON.
        orders.filter(pk=order.pk).update(
            service_type=json.dumps([order.service_type] if order.service_type else [])
        )


def restore_service_type(apps, schema_editor):
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(
        schema_editor.connection.alias
    )
    for order in orders.only("pk", "service_type").iterator():
        values = json.loads(order.service_type)
        if len(values) > 1:
            raise ValueError("存在多选服务项目，无法无损回退为单选字段。")
        orders.filter(pk=order.pk).update(service_type=values[0] if values else "")


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_oa", "0033_maintenancedocument_electronic_seal_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="maintenanceorder",
            name="service_type",
            field=models.TextField(verbose_name="服务项目"),
        ),
        migrations.RunPython(prepare_service_types, restore_service_type),
        migrations.AlterField(
            model_name="maintenanceorder",
            name="service_type",
            field=models.JSONField(
                default=list,
                validators=[netbox_oa.validators.validate_service_types],
                verbose_name="服务项目",
            ),
        ),
    ]
