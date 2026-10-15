from django.db import migrations
from django.utils import timezone


def backfill_order_forms(apps, schema_editor):
    alias = schema_editor.connection.alias
    orders = apps.get_model("netbox_oa", "MaintenanceOrder")
    documents = apps.get_model("netbox_oa", "MaintenanceDocument")
    for order in (
        orders.objects.using(alias)
        .filter(status__in=("draft", "active"), stage="ordering")
        .select_related("requester")
        .iterator()
    ):
        if documents.objects.using(alias).filter(order_id=order.pk, material="order").exists():
            continue
        if order.status == "active":
            steps = order.steps_snapshot
            if not steps or order.current_position >= len(steps):
                continue
            if steps[order.current_position]["key"] not in {"number", "order_fill"}:
                continue
        if not all(
            (
                order.contract_number,
                order.supplier,
                order.service_region,
                order.service_location,
                order.service_type,
                order.requirements,
            )
        ):
            continue
        requested_at = timezone.localtime(order.created or timezone.now())
        requester = order.requester
        applicant = f"{requester.first_name} {requester.last_name}".strip() or requester.username
        data = {
            key: getattr(order, key)
            for key in (
                "contract_id",
                "contract_number",
                "supplier",
                "service_region",
                "service_location",
                "service_type",
                "contact_phone",
                "requirements",
            )
        }
        data.update(
            number=order.number,
            company_name="中交信通网络科技有限公司",
            requested_at=requested_at.isoformat(),
            applicant=applicant,
            signature="",
            signature_date="",
        )
        documents.objects.using(alias).create(
            order_id=order.pk,
            material="order",
            version=1,
            kind="form",
            form_data=data,
            state="electronic",
            original_name="维护服务工单 v1",
            uploaded_by_id=order.requester_id,
        )


class Migration(migrations.Migration):
    dependencies = [("netbox_oa", "0008_normalize_maintenance_flow_name")]
    operations = [migrations.RunPython(backfill_order_forms, migrations.RunPython.noop)]
