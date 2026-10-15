"""Assign the three submission steps to the order's requester."""

from django.db import migrations


KEYS = {"report_fill", "settlement_fill", "payment_plan"}


def assign_requester(apps, schema_editor):
    alias = schema_editor.connection.alias
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(alias)
    tasks = apps.get_model("netbox_oa", "MaintenanceTask").objects.using(alias)
    for order in orders.exclude(steps_snapshot=[]).iterator():
        positions = []
        for position, node in enumerate(order.steps_snapshot):
            if node.get("key") in KEYS:
                node["role"] = "requester"
                node["group_id"] = None
                node.pop("user_id", None)
                positions.append(position)
        if positions:
            orders.filter(pk=order.pk).update(steps_snapshot=order.steps_snapshot)
            if order.status == "active":
                tasks.filter(order_id=order.pk, position__in=positions, status="pending").update(
                    assigned_user_id=order.requester_id, assigned_group_id=None
                )


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0016_payment_step_group")]
    operations = [migrations.RunPython(assign_requester, migrations.RunPython.noop)]
