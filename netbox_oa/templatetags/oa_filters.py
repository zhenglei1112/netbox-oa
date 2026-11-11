from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django import template
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from netbox_oa.choices import WorkflowStatus
from netbox_oa.time_display import format_duration

register = template.Library()


@register.filter
def activity_initial(value):
    """Single-character node labels for the compact approval sidebar."""
    name = str(value or "")
    for keyword, initial in (
        ("生成付款", "申"), ("复核", "复"), ("部门", "部"), ("公司", "司"),
        ("上传", "传"), ("用印", "印"), ("申请", "申"), ("发起", "申"),
        ("付款", "款"), ("修正", "修"), ("补正", "修"),
    ):
        if keyword in name:
            return initial
    return name[:1]


@register.filter
def oa_datetime(value):
    """Display timestamps to seconds, preserving date-only snapshots as dates."""
    if not value:
        return value
    parsed = value
    if isinstance(value, str):
        try:
            parsed = parse_date(value) if len(value) == 10 else parse_datetime(value)
        except ValueError:
            return value
    if isinstance(parsed, datetime):
        if timezone.is_aware(parsed):
            parsed = timezone.localtime(parsed)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(parsed, date):
        return parsed.strftime("%Y-%m-%d")
    return value


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
    return oa_datetime(value)


@register.filter
def duration_hours(value):
    """Display stored hour quantities as days, hours and minutes."""
    try:
        hours = Decimal(str(value))
        if not hours.is_finite():
            return value
        return format_duration(hours * 60)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return value
