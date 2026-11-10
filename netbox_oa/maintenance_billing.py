"""Contract-priced settlement snapshots. Monetary calculations only run on the server."""

from copy import deepcopy
from datetime import date, datetime
from datetime import timezone as datetime_timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

from django.apps import apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .maintenance_contracts import purchase_contracts
from .models import MaintenanceDocument
from .services import _allowed, _require_actor, _save

CENT = Decimal("0.01")
LIMIT = Decimal("999999999999.99")
METHODS = {"fixed": "固定单价", "tiered": "按规格分档", "duration": "首班次及超时"}
UNITS = {"fault": "次", "shift": "班次", "device": "台", "km": "公里", "piece": "个"}


def number(value, label, *, positive=False):
    if isinstance(value, bool) or len(str(value)) > 32:
        raise ValidationError(f"{label}格式不正确。")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{label}须为有效数字。") from None
    if (not result.is_finite() or result < 0 or result > LIMIT
            or result.as_tuple().exponent < -6 or (positive and not result)):
        raise ValidationError(f"{label}须为有限的{'正' if positive else '非负'}数，最多6位小数。")
    return result


def money(value):
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def schedule_for(order, actor):
    contract = purchase_contracts(actor).filter(pk=order.contract_id).first()
    if contract is None:
        raise PermissionDenied("无权查看工单所属合同的计费规则。")
    schedule = getattr(contract, "maintenance_pricing", {}) or {}
    if not isinstance(schedule, dict) or schedule.get("schema_version") != 1:
        raise ValidationError("所属合同尚未配置有效的维护收费标准，请先维护合同计费规则。")
    if schedule.get("kind") != "framework" or schedule.get("currency") != "CNY":
        raise ValidationError("目前仅支持人民币框架代维收费标准。")
    return schedule


def catalogue(schedule):
    rules = {}
    for group in ("services", "materials"):
        items = schedule.get(group, [])
        if not isinstance(items, list) or len(items) > 200:
            raise ValidationError("合同收费清单格式无效。")
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("code"), str)
                    or not isinstance(item.get("name"), str) or not item["code"].strip()
                    or not item["name"].strip() or not isinstance(item.get("unit"), str)
                    or item["unit"] not in UNITS):
                raise ValidationError("合同收费条目的编码、名称或单位无效。")
            key = f"{group}:{item['code']}"
            if key in rules:
                raise ValidationError("合同收费项目编码重复，请先修正合同。")
            rules[key] = {**deepcopy(item), "method": item.get("method", "fixed"),
                          "rule_key": key, "group": group}
    return rules


def configured(order):
    contract = apps.get_model("netbox_contract", "Contract").objects.filter(
        pk=order.contract_id
    ).first()
    return bool(contract and getattr(contract, "maintenance_pricing", {})) or (
        order.documents.filter(material="settlement", removed_at__isnull=True,
                               replacement_documents__isnull=True).exclude(billing_data={}).exists()
    )


def service_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return timezone.localtime(parsed)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError("请填写有效的服务开始和结束时间。") from None


def calculate(schedule, lines):
    if not isinstance(lines, list) or not 1 <= len(lines) <= 1000:
        raise ValidationError("每份工单须填写1至1000条计费明细。")
    rules = catalogue(schedule)
    result = []
    total = Decimal(0)
    for index, line in enumerate(lines, 1):
        try:
            if (not isinstance(line, dict) or not isinstance(line.get("rule_key"), str)
                    or line["rule_key"] not in rules):
                raise ValidationError("请选择所属合同中的计费条目。")
            rule = rules[line["rule_key"]]
            start, end = service_time(line.get("started_at")), service_time(line.get("ended_at"))
            if end.astimezone(datetime_timezone.utc) < start.astimezone(datetime_timezone.utc):
                raise ValidationError("服务结束时间不得早于开始时间。")
            effective = schedule.get("effective", {})
            for key, actual, lower in (("start_date", start.date(), True),
                                       ("end_date", end.date(), False)):
                boundary = effective.get(key)
                if boundary:
                    try:
                        boundary = date.fromisoformat(boundary)
                    except (ValueError, TypeError):
                        raise ValidationError("合同生效日期配置无效。") from None
                    if (lower and actual < boundary) or (not lower and actual > boundary):
                        raise ValidationError("服务时间超出合同收费标准生效范围。")
            quantity = number(line.get("quantity", "1"), "数量", positive=True)
            deduction = number(line.get("deduction", "0"), "扣费金额")
            if deduction.as_tuple().exponent < -2:
                raise ValidationError("扣费金额最多保留2位小数。")
            method = rule["method"]
            tier = None
            tier_index = ""
            hours = Decimal(0)
            if method == "fixed":
                unit_price = number(rule.get("unit_price"), "合同单价")
            elif method == "tiered":
                if rule.get("boundaries_confirmed") is not True:
                    raise ValidationError("合同分档边界尚未确认，不能自动计费。")
                matches = []
                tiers = rule.get("tiers", [])
                if not isinstance(tiers, list) or len(tiers) > 100:
                    raise ValidationError("合同分档配置无效。")
                if "tier_index" in line:
                    selection = line["tier_index"]
                    if (not isinstance(selection, str)
                            or selection not in {str(i) for i in range(len(tiers))}):
                        raise ValidationError("请选择所属合同中的规格档位。")
                    specification = None
                else:
                    # Older saved details contain a numeric specification.
                    specification = number(line.get("specification"), "规格值")
                for candidate_index, candidate in enumerate(tiers):
                    if (not isinstance(candidate, dict)
                            or type(candidate.get("min_inclusive")) is not bool
                            or type(candidate.get("max_inclusive")) is not bool):
                        raise ValidationError("合同分档边界配置无效。")
                    low = number(candidate.get("min"), "档位下限")
                    high = (number(candidate["max"], "档位上限")
                            if candidate.get("max") is not None else None)
                    if ((specification is None and str(candidate_index) == selection)
                            or (specification is not None
                                and (specification > low or (specification == low
                                                 and candidate.get("min_inclusive")))
                            and (high is None or specification < high
                                 or (specification == high and candidate.get("max_inclusive"))))):
                        matches.append((str(candidate_index), candidate))
                if len(matches) != 1:
                    raise ValidationError("规格值须唯一匹配一个已确认的合同档位。")
                tier_index, tier = matches[0]
                tier = deepcopy(tier)
                unit_price = number(tier.get("unit_price"), "合同档位单价")
            elif method == "duration":
                elapsed = (end.astimezone(datetime_timezone.utc)
                           - start.astimezone(datetime_timezone.utc))
                hours = (Decimal(elapsed.days * 86400 + elapsed.seconds)
                         + Decimal(elapsed.microseconds) / 1000000) / 3600
                if not hours:
                    raise ValidationError("按时长计费的服务结束时间须晚于开始时间。")
                base_hours = number(rule.get("base_hours"), "首班次时长", positive=True)
                base_price = number(rule.get("base_price"), "首班次价格")
                overtime_price = number(rule.get("overtime_price"), "超时单价")
                overtime = max(Decimal(0), hours - base_hours)
                if overtime and rule.get("overtime_rounding") not in {"pro_rata", "ceil_hour"}:
                    raise ValidationError("合同尚未明确超时进位规则，不能计算超时费用。")
                if rule.get("overtime_rounding") == "ceil_hour":
                    overtime = overtime.to_integral_value(rounding=ROUND_CEILING)
                unit_price = base_price + overtime * overtime_price
            else:
                raise ValidationError("不支持该合同计费方式。")
            if unit_price * quantity > LIMIT:
                raise ValidationError("金额超出允许范围。")
            gross = (unit_price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
            if gross > LIMIT or deduction > gross:
                raise ValidationError("金额超出范围，或扣费金额大于服务价格。")
            net = gross - deduction
            total += net
            if total > LIMIT:
                raise ValidationError("结算总金额超出允许范围。")
            result.append({
                "rule_key": line["rule_key"], "rule": deepcopy(rule), "name": rule["name"],
                "unit": UNITS.get(rule.get("unit"), rule.get("unit", "")),
                "method_label": METHODS[method], "quantity": str(quantity),
                "started_at": start.isoformat(), "ended_at": end.isoformat(),
                "specification": str(line.get("specification", "")), "tier": tier,
                "tier_index": tier_index,
                "hours": str(hours), "unit_price": str(unit_price), "gross": money(gross),
                "deduction": money(deduction), "net": money(net),
            })
        except ValidationError as exc:
            raise ValidationError([f"第{index}条：{message}" for message in exc.messages]) from None
    return {"schema_version": 1, "currency": "CNY", "price_basis": schedule.get("price_basis"),
            "lines": result, "total": money(total)}


def billing_documents(order):
    """Current fee sheets are independent of scan uploads and submission evidence."""
    return order.documents.filter(
        material="settlement", kind="billing", removed_at__isnull=True,
        replacement_documents__isnull=True,
    )


def summary(order, *, require_complete=False):
    documents = list(billing_documents(order).order_by("item_number", "pk"))
    total = Decimal(0)
    snapshots = []
    for doc in documents:
        if not doc.billing_data:
            if require_complete:
                raise ValidationError(f"请先维护服务结算单第{doc.item_number}份的计费明细。")
            continue
        total += number(doc.billing_data.get("total"), "结算单汇总金额")
        snapshots.append({"document_id": doc.pk, "item_number": doc.item_number,
                          "version": doc.version, "billing_data": deepcopy(doc.billing_data)})
    if require_complete and not documents:
        raise ValidationError("请维护结算费用明细。费用维护无需上传或关联扫描件。")
    if total > LIMIT:
        raise ValidationError("验收汇总金额超出允许范围。")
    return {"total": money(total), "documents": snapshots}


def check_access(document, actor):
    from .maintenance_services import task_is_assigned

    order = document.order
    _allowed(type(order), actor, "view", order.pk)
    if document.pk:
        _allowed(type(document), actor, "view", document.pk)
    _require_actor(actor, MaintenanceDocument, "add")
    task = order.tasks.filter(status="pending").first()
    if (order.status != "active" or document.material != "settlement" or not task
            or order.steps_snapshot[task.position]["key"] != "acceptance_apply"
            or document.kind not in {"billing", "scan"}
            or (document.pk and not order.documents.filter(
                pk=document.pk, removed_at__isnull=True, replacement_documents__isnull=True
            ).exists())):
        raise ValidationError("仅验收申请环节可维护当前有效的结算费用明细。")
    _allowed(type(task), actor, "change", task.pk)
    if not task_is_assigned(task, actor):
        raise PermissionDenied("只有当前验收申请办理人可以维护计费明细。")
    return task


@transaction.atomic
def save_billing(pk, actor, lines):
    original = MaintenanceDocument.objects.select_related("order").get(pk=pk)
    return _save_billing(original, actor, lines)


@transaction.atomic
def create_billing(order, actor, lines):
    return _save_billing(MaintenanceDocument(
        order=order, material="settlement", kind="billing", original_name="结算费用明细"
    ), actor, lines)


def _save_billing(original, actor, lines):
    from .maintenance_services import _begin_task, _event, _task

    task = check_access(original, actor)
    order, task = _task(task.pk, actor)
    original.order = order
    check_access(original, actor)
    existing = billing_documents(order).first()
    if existing:
        existing.order = order
        check_access(existing, actor)
        original = existing
    data = calculate(schedule_for(order, actor), lines)
    data["contract_id"] = order.contract_id
    data["contract_number"] = order.contract_number
    if original.pk and original.kind == "billing":
        document = original
        document.snapshot()
        document.billing_data = data
    else:
        document = MaintenanceDocument(
            order=order, material="settlement", kind="billing", state="electronic",
            version=(order.documents.filter(material="settlement").aggregate(n=Max("version"))["n"]
                     or 0) + 1,
            item_number=1, original_name="结算费用明细",
            uploaded_by=actor, uploaded_task=task, billing_data=data,
        )
    document._changelog_message = "保存结算费用明细（更新当前记录）"
    _save(document)
    _allowed(MaintenanceDocument, actor, "add", document.pk)
    _begin_task(task, actor)
    _event(order, actor, "edit", "维护服务结算单计费明细", task,
           document_id=document.pk,
           item_number=document.item_number, total=data["total"])
    return document


@transaction.atomic
def remove_billing(pk, actor):
    raise ValidationError("一份工单仅保留一份费用明细，请维护明细中的计费条目，不支持整份删除。")
