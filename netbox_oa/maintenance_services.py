"""Permission-checked, transactional maintenance lifecycle operations."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from users.models import Group

from .maintenance_flow import MATERIALS, ROLES, build_steps
from .maintenance_materials import COMPANY_FORMS, ORDER_FORM_FIELDS
from .models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceLink,
    MaintenanceOrder,
    MaintenancePolicy,
    MaintenanceRole,
    MaintenanceTask,
)
from .services import _allowed, _require_actor, _save, _snapshot, _transition


def _event(order, actor, action, description, task=None, **details):
    return MaintenanceEvent.objects.create(
        order=order, actor=actor, task=task, action=action, description=description, details=details
    )


def _order(pk, actor, action="change"):
    _allowed(MaintenanceOrder, actor, action, pk)
    order = MaintenanceOrder.objects.select_for_update().get(pk=pk)
    _snapshot(order)
    return order


def task_is_assigned(task, actor):
    if actor.is_superuser:
        return True
    if task.assigned_user_id:
        return task.assigned_user_id == actor.pk
    return task.assigned_group_id and actor.groups.filter(pk=task.assigned_group_id).exists()


def _task(pk, actor):
    _allowed(MaintenanceTask, actor, "change", pk)
    order_id = MaintenanceTask.objects.values_list("order_id", flat=True).get(pk=pk)
    _allowed(MaintenanceOrder, actor, "view", order_id)
    order = MaintenanceOrder.objects.select_for_update().get(pk=order_id)
    task = MaintenanceTask.objects.select_for_update().get(pk=pk)
    if (
        order.status != "active"
        or task.status != "pending"
        or task.position != order.current_position
    ):
        raise ValidationError("任务已处理或流程不在当前节点，请刷新页面。")
    if not task_is_assigned(task, actor):
        raise PermissionDenied("任务已分配给其他人员，或您不属于当前经办组。")
    _snapshot(order)
    _snapshot(task)
    return order, task


def _new_task(order):
    node = order.steps_snapshot[order.current_position]
    task = MaintenanceTask(
        order=order,
        position=order.current_position,
        name=node["name"],
        kind=node["kind"],
        assigned_group_id=node.get("group_id"),
        assigned_user_id=node.get("user_id")
        or (order.owner_id if node["role"] == "owner" else None),
        deadline=timezone.now() + timedelta(hours=node["sla_hours"]),
        is_countersign=node["kind"] == "countersign",
    )
    _save(task)
    return task


@transaction.atomic
def save_policy(*, actor, name, groups, sla_hours=24, warning_hours=4, enabled=True, pk=None):
    if pk:
        _allowed(MaintenancePolicy, actor, "change", pk)
        policy = MaintenancePolicy.objects.select_for_update().get(pk=pk)
        _snapshot(policy)
    else:
        _require_actor(actor, MaintenancePolicy, "add")
        policy = MaintenancePolicy()
    if set(groups) != {role for role, _ in ROLES}:
        raise ValidationError("请配置全部七种经办角色。")
    ids = set(groups.values())
    if len(list(Group.objects.select_for_update().filter(pk__in=ids))) != len(ids):
        raise ValidationError("经办组不存在。")
    policy.name, policy.sla_hours, policy.warning_hours = name, sla_hours, warning_hours
    policy.enabled = enabled
    _save(policy)
    if (
        not MaintenancePolicy.objects.restrict(actor, "change" if pk else "add")
        .filter(pk=policy.pk)
        .exists()
    ):
        raise PermissionDenied("流程配置不符合对象权限约束。")
    for role, group_id in groups.items():
        MaintenanceRole.objects.update_or_create(
            policy=policy, role=role, defaults={"group_id": group_id}
        )
    return policy


@transaction.atomic
def create_order(*, actor, policy, **data):
    _require_actor(actor, MaintenanceOrder, "add")
    _allowed(MaintenancePolicy, actor, "view", policy.pk)
    policy = MaintenancePolicy.objects.select_for_update().get(pk=policy.pk)
    if not policy.enabled:
        raise ValidationError("流程配置已停用。")
    owner = data.pop("owner", None) or actor
    if not owner.is_active:
        raise ValidationError("线路主管账号未启用。")
    order = MaintenanceOrder(
        policy=policy, requester=actor, owner=owner, number=uuid4().hex, **data
    )
    _save(order)
    order.number = f"GD-{timezone.localdate().year}-{order.pk:06d}"
    _save(order)
    if not MaintenanceOrder.objects.restrict(actor, "add").filter(pk=order.pk).exists():
        raise PermissionDenied("新工单不符合新增权限约束。")
    _event(order, actor, "create", "创建维护服务工单")
    return order


@transaction.atomic
def update_order(*, pk, actor, **data):
    order = _order(pk, actor)
    if order.status == "draft":
        if actor.pk not in {order.owner_id, order.requester_id} and not actor.is_superuser:
            raise PermissionDenied("只有申请人或线路主管可以修改草稿。")
    elif order.status == "active":
        current = order.tasks.get(status="pending")
        if order.steps_snapshot[current.position]["key"] != "order_fill":
            raise ValidationError("已提交审核的工单资料需要退回填报节点才能修改。")
        _allowed(MaintenanceTask, actor, "change", current.pk)
        if not task_is_assigned(current, actor):
            raise PermissionDenied("只有当前填报人员可以修改工单资料。")
    else:
        raise ValidationError("工单已结束，不能修改。")
    allowed = {
        "title",
        "service_type",
        "priority",
        "supplier",
        "contract_number",
        "service_region",
        "service_location",
        "contact_phone",
        "requirements",
        "fault_reference",
    }
    if set(data) - allowed:
        raise ValidationError("只能修改工单业务信息。")
    for key, value in data.items():
        setattr(order, key, value)
    _save(order)
    _allowed(MaintenanceOrder, actor, "change", order.pk)
    previous = order.documents.filter(material="order", kind="form").first()
    if previous and set(data) & set(ORDER_FORM_FIELDS):
        _require_actor(actor, MaintenanceDocument, "add")
        form_data = dict(previous.form_data)
        form_data.update({key: getattr(order, key) for key in ORDER_FORM_FIELDS})
        _create_company_document(order, actor, "order", form_data)
    _event(order, actor, "edit", "修改工单资料", fields=sorted(data))
    return order


@transaction.atomic
def start_order(*, pk, actor):
    order = _order(pk, actor)
    if order.owner_id != actor.pk and order.requester_id != actor.pk and not actor.is_superuser:
        raise PermissionDenied("只有申请人或线路主管可以发起流程。")
    policy = MaintenancePolicy.objects.select_for_update().get(pk=order.policy_id)
    if not policy.enabled:
        raise ValidationError("流程配置已停用。")
    policy.full_clean()
    groups = dict(policy.roles.values_list("role", "group_id"))
    if set(groups) != {role for role, _ in ROLES}:
        raise ValidationError("请先配置全部经办角色。")
    ids = set(groups.values())
    if len(list(Group.objects.select_for_update().filter(pk__in=ids))) != len(ids):
        raise ValidationError("经办组不存在。")
    order.steps_snapshot = build_steps(groups, policy.sla_hours)
    order.warning_hours = policy.warning_hours
    _transition(order, "start", actor, "发起维护流程")
    _save(order)
    order.reserved_groups.add(*ids)
    _new_task(order)
    _event(order, actor, "start", "发起四阶段维护流程")
    return order


def _materials(order, node):
    ids = {}
    for material, state in node["required"]:
        document = order.documents.filter(material=material).order_by("-version").first()
        rank = {"electronic": 0, "awaiting_paper": 0, "paper": 1, "sealed": 2}
        if document and document.kind == "form" and material == "report":
            raise ValidationError("服务报告甲方表单不能替代乙方报告，请上传乙方完成的服务报告。")
        if not document or rank[document.state] < rank[state]:
            label = dict(MATERIALS)[material]
            if material == "order" and state == "electronic":
                raise ValidationError("请先填写并保存维护服务工单表单。")
            raise ValidationError(
                f"请上传当前版本的{label}，材料状态至少为{dict(MaintenanceDocument._meta.get_field('state').choices)[state]}。"
            )
        ids[material] = document.pk
    return ids


def _comment(comment, required=False):
    if len(comment) > 4000 or (required and not comment.strip()):
        raise ValidationError("请填写处理意见，最多 4000 字符。")


@transaction.atomic
def complete_task(
    *,
    pk,
    actor,
    comment="",
    external_reference="",
    payment_amount=None,
    period_start=None,
    period_end=None,
):
    _comment(comment)
    order, task = _task(pk, actor)
    node = order.steps_snapshot[task.position]
    evidence = _materials(order, node)
    for material, baseline in task.evidence.get("correction_baseline", {}).items():
        latest = order.documents.filter(material=material).order_by("-version").first()
        revised = latest.source_document if latest and latest.kind == "scan" else latest
        if not revised or revised.version <= baseline:
            raise ValidationError("退回补正后必须保存修订表单或上传修订附件，不能直接提交原材料。")
    if task.kind == "external" and not external_reference.strip():
        raise ValidationError("外部流程节点必须登记 EIP 或财务业务单号。")
    if len(external_reference) > 200:
        raise ValidationError("外部单号不能超过 200 字符。")
    if node["key"] == "payment_plan":
        if payment_amount is None or period_start is None or period_end is None:
            raise ValidationError("请填写付款金额及周期起止日期。")
        order.payment_amount, order.period_start, order.period_end = (
            payment_amount,
            period_start,
            period_end,
        )
        order.full_clean()
    if node["key"] in {"paid", "receipt", "issue", "finance_handover"}:
        _comment(comment, required=True)
    if node["key"].endswith("_mail"):
        _comment(comment, required=True)
    if node["key"] == "receipt" and order.payment_status != "paid":
        raise ValidationError("必须先确认实际付款。")
    _transition(task, "complete", actor, comment)
    task.decided_by, task.completed_at, task.comment = actor, timezone.now(), comment
    task.external_reference, task.evidence = external_reference, evidence
    _save(task)
    if node["payment"]:
        order.payment_status = node["payment"]
    _event(
        order,
        actor,
        "complete",
        f"完成：{task.name}",
        task,
        material_versions=evidence,
        external_reference=external_reference,
        comment=comment,
    )
    if task.position + 1 == len(order.steps_snapshot):
        if order.payment_status != "received":
            raise ValidationError("确认收款后才能结束工单。")
        _transition(order, "finish", actor, comment)
        order.completed_at = timezone.now()
    else:
        order.current_position += 1
        order.stage = order.steps_snapshot[order.current_position]["stage"]
        _new_task(order)
    _save(order)
    return task


@transaction.atomic
def return_task(*, pk, actor, comment):
    _comment(comment, required=True)
    order, task = _task(pk, actor)
    node = order.steps_snapshot[task.position]
    key = node["return_to"]
    if not key or task.kind not in {"approval", "external", "countersign"}:
        raise ValidationError("当前节点不支持退回补正。")
    target = next(i for i, step in enumerate(order.steps_snapshot) if step["key"] == key)
    _transition(task, "send_back", actor, comment)
    task.completed_at, task.decided_by, task.comment = timezone.now(), actor, comment
    _save(task)
    order.current_position = target
    order.stage = order.steps_snapshot[target]["stage"]
    # Earlier payment approvals are invalidated when their supporting materials change.
    order.payment_status = "draft" if order.stage != "payment" else "submitted"
    _save(order)
    correction = _new_task(order)
    correction.evidence = {
        "correction_baseline": {
            material: order.documents.filter(material=material).aggregate(n=Max("version"))["n"]
            or 0
            for material, _ in order.steps_snapshot[target]["required"]
        }
    }
    _save(correction)
    _event(
        order,
        actor,
        "return",
        f"退回至：{order.steps_snapshot[target]['name']}",
        task,
        comment=comment,
    )
    return task


@transaction.atomic
def claim_task(*, pk, actor):
    order, task = _task(pk, actor)
    if task.assigned_user_id:
        raise ValidationError("任务已经领取或指定处理人。")
    task.assigned_user, task.started_at = actor, timezone.now()
    _save(task)
    _event(order, actor, "claim", "领取任务", task)
    return task


def _target(user_id, order):
    target = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if not target or not target.has_perm("netbox_oa.change_maintenancetask"):
        raise ValidationError("目标人员不存在、未启用或没有任务处理权限。")
    _allowed(MaintenanceOrder, target, "view", order.pk)
    return target


@transaction.atomic
def transfer_task(*, pk, actor, user_id, comment):
    _comment(comment, required=True)
    order, task = _task(pk, actor)
    target = _target(user_id, order)
    if (
        task.assigned_group_id
        and not target.is_superuser
        and not target.groups.filter(pk=task.assigned_group_id).exists()
    ):
        raise ValidationError("只能转办给原经办组内的人员。")
    previous = task.assigned_user_id
    task.assigned_user = target
    _save(task)
    _allowed(MaintenanceTask, target, "change", task.pk)
    _event(
        order,
        actor,
        "transfer",
        "转办任务",
        task,
        previous_user=previous,
        target_user=target.pk,
        comment=comment,
    )
    return task


@transaction.atomic
def countersign_task(*, pk, actor, user_id, comment):
    _comment(comment, required=True)
    order, task = _task(pk, actor)
    if task.kind != "approval":
        raise ValidationError("只能在审核节点后增加会签。")
    target = _target(user_id, order)
    current = order.steps_snapshot[task.position]
    if (
        task.assigned_group_id
        and not target.is_superuser
        and not target.groups.filter(pk=task.assigned_group_id).exists()
    ):
        raise ValidationError("加签人员必须属于本审核节点的经办组。")
    step = {
        **current,
        "key": uuid4().hex,
        "name": f"加签审核（{target}）",
        "kind": "countersign",
        "role": "countersign",
        "group_id": task.assigned_group_id,
        "user_id": target.pk,
        "required": [],
        "payment": current["payment"],
    }
    current["payment"] = ""
    order.steps_snapshot.insert(task.position + 1, step)
    _save(order)
    _event(
        order, actor, "countersign", "追加审核节点", task, target_user=target.pk, comment=comment
    )
    return task


@transaction.atomic
def terminate_order(*, pk, actor, comment):
    _comment(comment, required=True)
    order = _order(pk, actor)
    if not actor.is_superuser:
        raise PermissionDenied("涉及已发生服务或费用，终止工单仅允许超级管理员办理。")
    _transition(order, "terminate", actor, comment)
    for task in order.tasks.filter(status="pending"):
        _transition(task, "cancel", actor, comment)
        task.completed_at, task.decided_by, task.comment = timezone.now(), actor, comment
        _save(task)
    _save(order)
    _event(order, actor, "terminate", "终止工单", comment=comment)
    return order


@transaction.atomic
def withdraw_order(*, pk, actor, comment=""):
    order = _order(pk, actor)
    if actor.pk not in {order.requester_id, order.owner_id} and not actor.is_superuser:
        raise PermissionDenied("只有申请人或线路主管可以撤销草稿。")
    _transition(order, "withdraw", actor, comment)
    _save(order)
    _event(order, actor, "withdraw", "撤销草稿", comment=comment)
    return order


@transaction.atomic
def follow_order(*, pk, actor):
    order = _order(pk, actor, "view")
    if order.followers.filter(pk=actor.pk).exists():
        order.followers.remove(actor)
    else:
        order.followers.add(actor)
    return order


def check_company_form_access(order, actor, material):
    if material not in COMPANY_FORMS:
        raise ValidationError("此材料由乙方提供，请上传附件。")
    _allowed(MaintenanceOrder, actor, "view", order.pk)
    _allowed(MaintenanceOrder, actor, "change", order.pk)
    _require_actor(actor, MaintenanceDocument, "add")
    if order.status not in {"draft", "active"}:
        raise ValidationError("工单已经结束，不允许修改表单。")
    current = order.tasks.filter(status="pending").first()
    if not actor.is_superuser and actor.pk not in {order.owner_id, order.requester_id}:
        if material != "order" or not current or not task_is_assigned(current, actor):
            raise PermissionDenied("只有甲方经办人员可以填写公司表单。")
        _allowed(MaintenanceTask, actor, "change", current.pk)
    if order.status == "active":
        key = order.steps_snapshot[current.position]["key"] if current else None
        allowed = {"order_fill"} if material == "order" else {"issue", "report_fill"}
        if key not in allowed:
            raise ValidationError("当前节点不能修改此表单，请退回对应填报节点后修改。")
        if material == "order":
            _allowed(MaintenanceTask, actor, "change", current.pk)
            if not task_is_assigned(current, actor):
                raise PermissionDenied("只有当前填报人员可以修改工单表单。")


def _create_company_document(order, actor, material, data):
    version = (order.documents.filter(material=material).aggregate(n=Max("version"))["n"] or 0) + 1
    document = MaintenanceDocument(
        order=order,
        material=material,
        version=version,
        kind="form",
        form_data={**data, "number": order.number},
        state="electronic",
        original_name=f"{COMPANY_FORMS[material]} v{version}",
        uploaded_by=actor,
    )
    _save(document)
    _allowed(MaintenanceDocument, actor, "add", document.pk)
    _event(
        order,
        actor,
        "form",
        "填写并生成公司表单",
        material=material,
        version=version,
        document_id=document.pk,
    )
    return document


@transaction.atomic
def save_company_form(*, pk, actor, material, data):
    from .maintenance_forms import MaintenanceCompanyForm

    order = _order(pk, actor)
    check_company_form_access(order, actor, material)
    form = MaintenanceCompanyForm(data, material=material)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    values = dict(form.cleaned_data)
    values["requested_at"] = values["requested_at"].isoformat()
    if material == "order":
        for key in ORDER_FORM_FIELDS:
            setattr(order, key, values[key])
        _save(order)
        _allowed(MaintenanceOrder, actor, "change", order.pk)
    return _create_company_document(order, actor, material, values)


@transaction.atomic
def upload_document(
    *, pk, actor, material, file, state="electronic", note="", source_document_id=None
):
    # Uploading may be performed by the owner or the actor of the current task.
    _allowed(MaintenanceOrder, actor, "view", pk)
    _require_actor(actor, MaintenanceDocument, "add")
    order = MaintenanceOrder.objects.select_for_update().get(pk=pk)
    if order.status not in {"draft", "active"}:
        raise ValidationError("工单已经结束，不允许追加材料。")
    current = order.tasks.filter(status="pending").first()
    if current:
        node = order.steps_snapshot[current.position]
        stage_material = {
            "ordering": {"order"},
            "report": {"report"},
            "settlement": {"settlement"},
            "payment": {"notice", "invoice", "contract", "verification"},
        }
        if material not in stage_material[order.stage]:
            raise ValidationError("只能提交当前阶段的材料，已完成阶段的材料保持不变。")
        # A revised electronic source must go back through review; later nodes only accept scans.
        if current.kind not in {"submission"} and state in {"electronic", "awaiting_paper"}:
            if material != "verification" or node["key"] != "invoice_check":
                raise ValidationError(
                    "修改电子版内容请先退回填报节点；当前节点仅可补充纸质或盖章版。"
                )
    if not actor.is_superuser and actor.pk != order.owner_id:
        if not current or not task_is_assigned(current, actor):
            raise PermissionDenied("只有线路主管或当前节点处理人可以提交材料。")
        _allowed(MaintenanceTask, actor, "change", current.pk)
    else:
        _allowed(MaintenanceOrder, actor, "change", pk)
    scan = state in {"paper", "sealed"}
    if material == "order" and not scan:
        raise ValidationError("维护服务工单请在公司表单中填写生成，只能上传纸质或盖章扫描件。")
    source = order.documents.filter(material=material).exclude(kind="scan").first()
    if scan:
        if not source or (material == "report" and source.kind == "form"):
            raise ValidationError("请先生成公司表单或上传乙方电子附件，再上传对应扫描件。")
        if source_document_id != source.pk:
            raise ValidationError("扫描件必须对应当前表单或附件版本，请刷新后重新选择。")
        _allowed(MaintenanceDocument, actor, "view", source.pk)
        if Path(file.name).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}:
            raise ValidationError("扫描件仅支持 PDF、PNG 和 JPEG。")
    elif source_document_id:
        raise ValidationError("电子附件不能指定扫描来源版本。")
    if Path(file.name).suffix.lower() not in {".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg"}:
        raise ValidationError("支持 PDF、DOCX、XLSX、PNG 和 JPEG 材料。")
    if file.size > 20 * 1024 * 1024:
        raise ValidationError("单份材料不能超过 20 MB。")
    version = (order.documents.filter(material=material).aggregate(n=Max("version"))["n"] or 0) + 1
    document = MaintenanceDocument(
        order=order,
        material=material,
        version=version,
        file=file,
        original_name=Path(file.name).name[:255],
        uploaded_by=actor,
        state=state,
        note=note,
        kind="scan" if scan else "attachment",
        source_document=source if scan else None,
    )
    document.full_clean()
    document.save()
    try:
        _allowed(MaintenanceDocument, actor, "add", document.pk)
        _event(
            order,
            actor,
            "upload",
            "上传扫描件" if scan else "上传乙方或业务附件",
            material=material,
            version=version,
            document_id=document.pk,
            state=state,
            source_document_id=document.source_document_id,
        )
    except Exception:
        document.file.delete(save=False)
        raise
    return document


@transaction.atomic
def add_link(*, pk, actor, object_type, object_id):
    order = _order(pk, actor)
    _require_actor(actor, MaintenanceLink, "add")
    model = object_type.model_class()
    if not model or not hasattr(model.objects, "restrict"):
        raise ValidationError("只支持具有 NetBox 对象权限的业务对象。")
    if not model.objects.restrict(actor, "view").filter(pk=object_id).exists():
        raise PermissionDenied("关联对象不存在或无查看权限。")
    target = model.objects.get(pk=object_id)
    link = MaintenanceLink(
        order=order, object_type=object_type, object_id=object_id, label=str(target)[:200]
    )
    _save(link)
    _allowed(MaintenanceLink, actor, "add", link.pk)
    _event(order, actor, "link", "关联业务对象", object_type=object_type.pk, object_id=object_id)
    return link
