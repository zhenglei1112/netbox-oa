from django.db import migrations


def remove_obsolete_payment_requirements(apps, schema_editor):
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(
        schema_editor.connection.alias
    ).filter(status__in=("draft", "active"))
    for order in orders.iterator():
        changed = False
        for step in order.steps_snapshot:
            required = step.get("required", [])
            remaining = [pair for pair in required if pair[0] not in {"contract", "verification"}]
            if remaining != required:
                step["required"] = remaining
                changed = True
        if changed:
            orders.filter(pk=order.pk).update(steps_snapshot=order.steps_snapshot)


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0027_alter_maintenancedocument_material_and_more")]

    operations = [
        migrations.RunPython(remove_obsolete_payment_requirements, migrations.RunPython.noop),
    ]
