"""Company-authored sections of the supplied maintenance document templates."""

from django.utils import timezone

COMPANY_FORMS = {"order": "维护服务工单"}
# Previously generated report forms remain available for historical viewing.
PRINTABLE_FORMS = (*COMPANY_FORMS, "report")
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
