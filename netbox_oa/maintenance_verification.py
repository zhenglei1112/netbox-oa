"""The deliberately small, public projection of a printed document."""

from .maintenance_materials import COMPANY_FORMS, service_effect_label

PHASES = ("工单阶段", "验收阶段", "待付款", "已完成")


def verification_context(document):
    order = document.order
    data = document.form_data
    submission = order.events.filter(action="start").select_related("actor").order_by(
        "created", "pk"
    ).first()
    submitter = submission.actor if submission else order.requester
    index = 3 if order.status == "completed" else {
        "ordering": 0, "acceptance": 1, "payment": 2,
    }.get(order.stage, 0)
    status = {
        "draft": "未提交", "terminated": "已终止", "cancelled": "已撤销",
    }.get(order.status, PHASES[index])
    active = order.status in {"active", "completed"}
    return {
        "number": data.get("number") or order.number,
        "material_label": COMPANY_FORMS[document.material],
        "submitter": submitter.get_full_name().strip() or submitter.get_username(),
        "submitted_at": submission.created if submission else None,
        "company_name": data.get("supplier") or "—",
        "province": data.get("service_region") or "—",
        "location": data.get("service_location") or "—",
        "requirements": data.get("requirements") or "—",
        "status_label": status,
        "inactive": not active,
        "phases": [
            {
                "label": label,
                "number": position + 1,
                "done": order.status != "draft" and (
                    position < index or order.status == "completed"
                ),
                "current": active and position == index,
            }
            for position, label in enumerate(PHASES)
        ],
        "scan_count": document.scan_count,
        "is_latest": not order.documents.filter(
            material=document.material, kind="form", removed_at__isnull=True,
            version__gt=document.version,
        ).exists(),
        "acceptance_result": (
            service_effect_label(data.get("service_effect"))
            if document.material == "acceptance" else ""
        ),
    }
