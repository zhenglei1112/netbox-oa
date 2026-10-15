"""Rename workflow labels without changing any instance's execution rules."""

from django.db import migrations


STEP_NAMES = {
    "order_fill": "发起工单",
    "order_review": "工单初审",
    "order_final": "工单部门审核",
    "report_fill": "服务报告",
    "report_department_seal": "服务报告用印部门审核",
    "report_senior_seal": "服务报告用印公司审批",
    "report_seal": "服务报告用印",
    "settlement_fill": "服务结算单",
    "settlement_department_seal": "服务结算单用印部门审核",
    "settlement_senior_seal": "服务结算单用印公司审批",
    "settlement_seal": "服务结算单用印",
    "payment_plan": "生成付款",
    "payment_review": "付款部门审核",
    "payment_eip": "付款",
}


def rename_steps(apps, schema_editor):
    alias = schema_editor.connection.alias
    orders = apps.get_model("netbox_oa", "MaintenanceOrder").objects.using(alias)
    tasks = apps.get_model("netbox_oa", "MaintenanceTask").objects.using(alias)
    for order in orders.exclude(steps_snapshot=[]).iterator():
        changed = False
        for position, node in enumerate(order.steps_snapshot):
            name = STEP_NAMES.get(node.get("key"))
            if name and node.get("name") != name:
                old_name = node.get("name")
                node["name"] = name
                changed = True
                tasks.filter(order_id=order.pk, position=position, name=old_name).update(name=name)
        if changed:
            orders.filter(pk=order.pk).update(steps_snapshot=order.steps_snapshot)


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0014_payment_application_stage")]
    operations = [migrations.RunPython(rename_steps, migrations.RunPython.noop)]
