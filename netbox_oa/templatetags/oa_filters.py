from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from netbox_oa.choices import WorkflowStatus
from netbox_oa.time_display import format_duration

register = template.Library()


@register.filter
def service_type_badges(value):
    from netbox_oa.maintenance_presentation import service_type_badges as render_badges

    return render_badges(value)


def _money_amount(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and abs(amount) < Decimal("10000000000000000"):
            amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if abs(amount) < Decimal("10000000000000000"):
                return amount if amount else Decimal("0.00")
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


@register.filter
def cny_number(value):
    """Display yuan with a currency symbol, thousands separators and two decimals."""
    amount = _money_amount(value)
    return value if amount is None else f"￥{amount:,.2f}"


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

@register.filter
def billing_service_period(billing):
    """Display the date range across saved billing lines, ignoring missing dates."""
    billing = billing or {}
    documents = billing.get("documents", [{"billing_data": billing}])
    starts, ends = [], []
    for document in documents:
        for line in document.get("billing_data", {}).get("lines", []):
            for key, dates in (("started_at", starts), ("ended_at", ends)):
                value = line.get(key)
                if value:
                    try:
                        day = parse_date(str(value)[:10])
                    except ValueError:
                        continue
                    if day:
                        dates.append(day)
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    if start and end and start != end:
        return f"{chinese_date(start)}至{chinese_date(end)}"
    return chinese_date(start or end) or ""


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
