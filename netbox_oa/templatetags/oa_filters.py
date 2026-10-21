from django import template
from django.utils import timezone
from django.utils.dateparse import parse_date

from netbox_oa.choices import WorkflowStatus

register = template.Library()


@register.filter
def chinese_date(value, include_time=False):
    """Format a stored form date without changing its snapshot or wall time."""
    if not value:
        return value
    text = str(value)
    try:
        day = parse_date(text[:10])
    except ValueError:
        return value
    if day is None:
        return value
    result = f"{day.year}年{day.month}月{day.day}日"
    if include_time and len(text) >= 16:
        result += " " + text[11:16]
    return result

STATUS_DISPLAY_MAP = {
    WorkflowStatus.DRAFT.value: "草稿",
    WorkflowStatus.PENDING.value: "待审批",
    WorkflowStatus.APPROVED.value: "已通过",
    WorkflowStatus.REJECTED.value: "已拒绝",
    WorkflowStatus.CANCELLED.value: "已撤销",
}


@register.filter
def status_label(value):
    if not value:
        return "—"
    return STATUS_DISPLAY_MAP.get(str(value), value)


@register.filter
def activity_time(value):
    if not value:
        return "—"
    local = timezone.localtime(value)
    today = timezone.localdate()
    if local.date() == today:
        return local.strftime("今天 %H:%M")
    if local.year == today.year:
        return local.strftime("%m-%d %H:%M")
    return local.strftime("%Y-%m-%d %H:%M")
