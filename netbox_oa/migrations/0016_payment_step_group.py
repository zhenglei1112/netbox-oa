"""Move payment generation/review into payment without changing task progress."""

from django.db import migrations


def move_steps(apps, schema_editor, stage):
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(
        schema_editor.connection.alias
    )
    for order in orders.exclude(steps_snapshot=[]).iterator():
        changed = False
        for node in order.steps_snapshot:
            if node.get("key") in {"payment_plan", "payment_review"} and node.get("stage") != stage:
                node["stage"] = stage
                changed = True
        if changed:
            fields = {"steps_snapshot": order.steps_snapshot}
            if 0 <= order.current_position < len(order.steps_snapshot):
                current = order.steps_snapshot[order.current_position]
                if current.get("key") in {"payment_plan", "payment_review"}:
                    fields["stage"] = stage
            orders.filter(pk=order.pk).update(**fields)


def forwards(apps, schema_editor):
    move_steps(apps, schema_editor, "payment")


def backwards(apps, schema_editor):
    move_steps(apps, schema_editor, "settlement")


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0015_maintenance_step_names")]
    operations = [migrations.RunPython(forwards, backwards)]
