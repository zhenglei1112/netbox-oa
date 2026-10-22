from django.db import migrations


def add_payment_verification(apps, schema_editor):
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(
        schema_editor.connection.alias
    ).filter(status__in=("draft", "active"))
    for order in orders.iterator():
        changed = False
        for step in order.steps_snapshot:
            if step.get("key") != "payment_eip":
                continue
            required = step.setdefault("required", [])
            if not any(material == "verification" for material, state in required):
                required.append(["verification", "electronic"])
                changed = True
        if changed:
            orders.filter(pk=order.pk).update(steps_snapshot=order.steps_snapshot)


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0023_document_removed_at")]

    operations = [
        migrations.RunPython(add_payment_verification, migrations.RunPython.noop),
    ]
