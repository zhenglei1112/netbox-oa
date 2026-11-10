"""Company-authored sections of the supplied maintenance document templates."""

from django.utils import timezone

COMPANY_FORMS = {"order": "维护服务工单", "acceptance": "工单验收单"}
# Previously generated report forms remain available for historical viewing.
PRINTABLE_FORMS = (*COMPANY_FORMS, "report")
ACCEPTANCE_SIGNING_NODES = {
    "acceptance_apply", "acceptance_review", "acceptance_department",
}
ORDER_SIGNING_NODES = {"order_correction", "order_review", "order_final"}
TASK_SIGNING_NODES = ORDER_SIGNING_NODES | ACCEPTANCE_SIGNING_NODES
SERVICE_EFFECTS = (("excellent", "优"), ("good", "良"), ("qualified", "合格"),
                   ("unqualified", "不合格"))
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


def company_form_initial(order):
    return {
        **{key: getattr(order, key) for key in ORDER_FORM_FIELDS},
        "company_name": "中交信通网络科技有限公司",
        "requested_at": timezone.localtime(order.created).strftime("%Y-%m-%dT%H:%M"),
        "applicant": order.requester.get_full_name() or order.requester.get_username(),
    }
