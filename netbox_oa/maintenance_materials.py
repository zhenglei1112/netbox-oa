"""Company-authored sections of the supplied maintenance document templates."""

from django.utils import timezone

COMPANY_FORMS = {"order": "维护服务工单", "report": "服务报告（甲方部分）"}
SERVICE_ITEMS = (
    ("fiber", "光缆故障处置"),
    ("construction", "施工盯防"),
    ("cutover", "割接盯防"),
    ("air", "空调保养"),
    ("inspection", "巡检服务"),
    ("power", "应急供电"),
)
ORDER_FORM_FIELDS = (
    "contract_number",
    "supplier",
    "service_region",
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
