"""Company-authored sections of the supplied maintenance document templates."""

from django.utils import timezone

COMPANY_FORMS = {"order": "维护服务工单", "acceptance": "工单服务验收单"}
# Previously generated report forms remain available for historical viewing.
PRINTABLE_FORMS = (*COMPANY_FORMS, "report")
ACCEPTANCE_SIGNING_NODES = {
    "acceptance_apply", "acceptance_review", "acceptance_department",
}
ORDER_SIGNING_NODES = {"order_correction", "order_review", "order_final"}
TASK_SIGNING_NODES = ORDER_SIGNING_NODES | ACCEPTANCE_SIGNING_NODES
SERVICE_EFFECTS = (("passed", "通过"), ("failed", "不通过"))
SERVICE_ITEMS = (
    ("fiber", "光缆故障处置"),
    ("construction", "施工盯防"),
    ("cutover", "割接盯防"),
    ("air", "空调保养"),
    ("inspection", "巡检服务"),
    ("power", "应急供电"),
)
ORDER_FORM_FIELDS = (
    "contract_id",
    "contract_number",
    "contract_type_snapshot",
    "supplier",
    "service_region",
    "service_region_ids",
    "service_location",
    "service_type",
    "contact_phone",
    "requirements",
)


def service_effect_label(value):
    # Historical signed forms retain their original assessment.
    labels = {"excellent": "优", "good": "良", "qualified": "合格", "unqualified": "不合格"}
    return dict(SERVICE_EFFECTS).get(value, labels.get(value, "—"))


def service_type_values(value):
    """Read both current selections and immutable single-choice snapshots."""
    if isinstance(value, str):
        return [value] if value else []
    return value if isinstance(value, list) else []


def service_type_label(value):
    labels = dict(SERVICE_ITEMS)
    return "、".join(labels.get(key, key) for key in service_type_values(value)) or "—"


def company_form_initial(order):
    return {
        **{key: getattr(order, key) for key in ORDER_FORM_FIELDS},
        "service_type": service_type_values(order.service_type),
        "company_name": "中交信通网络科技有限公司",
        "requested_at": timezone.localtime(order.created).strftime("%Y-%m-%dT%H:%M"),
        "applicant": order.requester.get_full_name() or order.requester.get_username(),
    }
