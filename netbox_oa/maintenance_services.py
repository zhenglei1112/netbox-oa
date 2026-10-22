"""Permission-checked, transactional maintenance lifecycle operations."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone
from users.models import Group

from .maintenance_contracts import region_ids_from_names, selected_contract
from .maintenance_flow import FLOW_NAME, MATERIALS, ROLES, build_steps, scan_fill_key
from .maintenance_materials import (
    COMPANY_FORMS,
    ORDER_FORM_FIELDS,
    PRINTABLE_FORMS,
    company_form_initial,
)
from .maintenance_numbering import issue_number, number_from_token
from .models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceLink,
    MaintenanceNumberReservation,
    MaintenanceOrder,
    MaintenancePolicy,
    MaintenanceRole,
    MaintenanceTask,
)
from .services import _allowed, _require_actor, _save, _snapshot, _transition


def _event(order, actor, action, description, task=None, **details):
    event = MaintenanceEvent(
        order=order, actor=actor, task=task, action=action, description=description, details=details
    )
    event._changelog_message = event.operation_summary
    event.save()
    return event


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
        assigned_group_id=None if node["role"] == "requester" else node.get("group_id"),
        assigned_user_id=(
            order.requester_id
            if node["role"] == "requester"
            else node.get("user_id") or (order.owner_id if node["role"] == "owner" else None)
        ),
        deadline=timezone.now() + timedelta(hours=node["sla_hours"]),
        is_countersign=node["kind"] == "countersign",
    )
    _save(task)
    return task


def _begin_task(task, actor):
    """Bind a shared group task when its first successful work operation is saved."""
    _snapshot(task)
    if not task.assigned_user_id:
        task.assigned_user = actor
    if not task.started_at:
        task.started_at = timezone.now()
    _save(task)


@transaction.atomic
def save_policy(
    *, actor, name, groups, sla_hours=24, warning_hours=4, enabled=True, is_default=None, pk=None
):
    if is_default and not enabled:
        raise ValidationError("默认流程配置必须启用。")
    previous_defaults = (
        list(MaintenancePolicy.objects.select_for_update().filter(is_default=True).order_by("pk"))
        if is_default
        else []
    )
    if pk:
        _allowed(MaintenancePolicy, actor, "change", pk)
        policy = MaintenancePolicy.objects.select_for_update().get(pk=pk)
        _snapshot(policy)
    else:
        _require_actor(actor, MaintenancePolicy, "add")
        policy = MaintenancePolicy()
    if set(groups) != {role for role, _ in ROLES}:
        raise ValidationError("请配置全部四种经办角色。")
    ids = set(groups.values())
    if len(list(Group.objects.select_for_update().filter(pk__in=ids))) != len(ids):
        raise ValidationError("经办组不存在。")
    policy.name, policy.sla_hours, policy.warning_hours = name, sla_hours, warning_hours
    policy.enabled = enabled
    if is_default is not None:
        policy.is_default = is_default
    for previous in previous_defaults:
        if previous.pk == policy.pk:
            continue
        _allowed(MaintenancePolicy, actor, "change", previous.pk)
        _snapshot(previous)
        previous.is_default = False
        _save(previous)
    try:
        _save(policy)
    except IntegrityError as exc:
        raise ValidationError("流程配置已发生变化，请刷新页面后重试。") from exc
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
def create_order(*, actor, policy, number_token=None, changelog_message="", **data):
    _require_actor(actor, MaintenanceOrder, "add")
    _allowed(MaintenancePolicy, actor, "view", policy.pk)
    policy = MaintenancePolicy.objects.select_for_update().get(pk=policy.pk)
    if not policy.enabled:
        raise ValidationError("流程配置已停用。")
    owner = data.pop("owner", None) or actor
    if owner.pk != actor.pk:
        raise ValidationError("请由线路主管本人发起工单。")
    if not owner.is_active:
        raise ValidationError("线路主管账号未启用。")
    if "number" in data:
        raise ValidationError("工单号由系统自动生成，不能人工指定。")
    data.update(
        selected_contract(
            actor,
            data.get("contract_id"),
            data.get("service_region"),
            data.get("service_region_ids"),
        )
    )
    if "title" in data:
        raise ValidationError("工单仅使用自动生成的编号，不再填写工单名称。")
    number = (
        number_from_token(number_token, actor, lock=True)
        if number_token
        else issue_number(actor)[0]
    )
    order = MaintenanceOrder(policy=policy, requester=actor, owner=owner, number=number, **data)
    order._changelog_message = changelog_message
    _save(order)
    MaintenanceNumberReservation.objects.filter(number=number, actor=actor).update(used=True)
    if not MaintenanceOrder.objects.restrict(actor, "add").filter(pk=order.pk).exists():
        raise PermissionDenied("新工单不符合新增权限约束。")
    _event(order, actor, "create", "创建维护服务工单", changelog_message=changelog_message)
    _require_actor(actor, MaintenanceDocument, "add")
    _create_company_document(order, actor, "order", company_form_initial(order), audit=False)
    return order


@transaction.atomic
def update_order(*, pk, actor, expected_document_id=None, changelog_message="", **data):
    order = _order(pk, actor)
    if expected_document_id is not None:
        latest = order.documents.filter(material="order", kind="form").first()
        try:
            expected_document_id = int(expected_document_id)
        except (TypeError, ValueError):
            raise ValidationError("工单版本无效，请刷新页面。") from None
        if not latest or latest.pk != expected_document_id:
            raise ValidationError("工单已在其他页面修改，请刷新后重新核对并签名。")
    if order.status == "draft":
        if actor.pk not in {order.owner_id, order.requester_id} and not actor.is_superuser:
            raise PermissionDenied("只有申请人或线路主管可以修改草稿。")
    elif order.status == "active":
        current = order.tasks.get(status="pending")
        if order.steps_snapshot[current.position]["key"] not in {"order_correction"}:
            raise ValidationError("已提交审核的工单资料需要退回填报节点才能修改。")
        _allowed(MaintenanceTask, actor, "change", current.pk)
        if not task_is_assigned(current, actor):
            raise PermissionDenied("只有当前填报人员可以修改工单资料。")
    else:
        raise ValidationError("工单已结束，不能修改。")
    allowed = {
        "service_type",
        "priority",
        "supplier",
        "contract_number",
        "contract_id",
        "service_region",
        "service_region_ids",
        "service_location",
        "contact_phone",
        "requirements",
        "fault_reference",
    }
    if set(data) - allowed:
        raise ValidationError("只能修改工单业务信息。")
    if set(data) & {
        "contract_id",
        "contract_number",
        "supplier",
        "service_region",
        "service_region_ids",
    }:
        contract_id = data.get("contract_id", order.contract_id)
        region = data.get("service_region")
        region_ids = data.get("service_region_ids")
        if (
            not {"service_region", "service_region_ids"} & set(data)
            and contract_id == order.contract_id
        ):
            region = order.service_region
            region_ids = order.service_region_ids or None
        data.update(selected_contract(actor, contract_id, region, region_ids))
    data = {key: value for key, value in data.items() if getattr(order, key) != value}
    order._changelog_message = changelog_message
    if not data:
        if changelog_message:
            _save(order)
        _event(order, actor, "save", "保存工单资料（内容未变）",
               fields=[], changelog_message=changelog_message)
        return order
    for key, value in data.items():
        setattr(order, key, value)
    _save(order)
    _allowed(MaintenanceOrder, actor, "change", order.pk)
    previous = order.documents.filter(material="order", kind="form").first()
    if previous and set(data) & (set(ORDER_FORM_FIELDS) | {"priority"}):
        _require_actor(actor, MaintenanceDocument, "add")
        form_data = dict(previous.form_data)
        form_data.update({key: getattr(order, key) for key in ORDER_FORM_FIELDS})
        _create_company_document(order, actor, "order", form_data, audit=False)
    _event(order, actor, "edit", "修改并保存工单资料", fields=sorted(data),
           changelog_message=changelog_message)
    return order


@transaction.atomic
def start_order(*, pk, actor):
    order = _order(pk, actor)
    if order.owner_id != actor.pk and order.requester_id != actor.pk and not actor.is_superuser:
        raise PermissionDenied("只有申请人或线路主管可以发起流程。")
    if order.status != "draft":
        raise ValidationError("工单已提交审批，请刷新页面查看进度。")
    from .maintenance_signatures import draft_signature_for_submission

    signature = draft_signature_for_submission(order)
    selected_contract(actor, order.contract_id)
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
    order.current_position = next(
        i for i, node in enumerate(order.steps_snapshot) if not node.get("return_only")
    )
    order.warning_hours = policy.warning_hours
    _transition(order, "start", actor, f"发起{FLOW_NAME}")
    _save(order)
    order.reserved_groups.add(*ids)
    _new_task(order)
    _event(
        order,
        actor,
        "start",
        f"发起{FLOW_NAME}",
        document_id=signature.document_id,
        signature_id=signature.pk,
        submitter_id=actor.pk,
        signer_id=signature.signer_id,
    )
    return order


def report_scan_sources(order):
    """The current submission's complete set of scanned reports."""
    return material_scan_sources(order, "report")


def material_scan_candidates(order, material):
    """Latest approval scan for each logical material, including the previous submission."""
    scans = order.documents.filter(
        material=material, kind="scan", source_document__isnull=True, is_sealed=False,
        replacement_documents__isnull=True, removed_at__isnull=True,
    ).select_related("order")
    if material == "order":
        latest = scans.order_by("-version").values("pk")[:1]
        return scans.filter(pk__in=latest)
    return scans


def material_set_adjusted(order, task, material):
    if not task or order.steps_snapshot[task.position]["key"] != scan_fill_key(material):
        return False
    if material in task.evidence.get("retained_materials", {}):
        return True
    # Recognize deletions made before retained sets were recorded in task evidence.
    return order.events.filter(
        task=task, action="remove_material", details__material=material
    ).exists()


def material_scan_sources(order, material):
    fill_positions = [
        i for i, node in enumerate(order.steps_snapshot) if node["key"] == scan_fill_key(material)
    ]
    if not fill_positions:
        return order.documents.none()
    submission = order.tasks.filter(position=fill_positions[0]).order_by("-pk").first()
    scans = order.documents.filter(
        material=material, kind="scan", source_document__isnull=True, removed_at__isnull=True
    )
    if not submission:
        return scans.none()
    if submission.status == "completed":
        approved = scans.filter(pk__in=submission.evidence.get(material, []))
        if material == "order":
            return approved.filter(pk__in=approved.order_by("-version").values("pk")[:1])
        return approved
    baseline = submission.evidence.get("correction_baseline", {}).get(material, 0)
    retained = submission.evidence.get("retained_materials", {}).get(material, [])
    if baseline and material_set_adjusted(order, submission, material):
        retained = material_scan_candidates(order, material).values_list("pk", flat=True)
    return scans.filter(
        Q(version__gt=baseline) | Q(pk__in=retained),
        pk__in=material_scan_candidates(order, material).values("pk"),
    )


def material_maintenance_documents(order, task, material):
    """Current upload targets; historical and other-stage evidence is immutable."""
    documents = order.documents.filter(material=material, removed_at__isnull=True)
    if not task or order.status != "active" or task.status != "pending":
        return documents.none()
    node = order.steps_snapshot[task.position]
    if material not in dict(node.get("required", [])):
        return documents.none()
    if node.get("scan_material") == material:
        if node["key"] == scan_fill_key(material):
            return material_scan_candidates(order, material)
        if node["key"] == f"{material}_seal":
            return documents.filter(
                is_sealed=True, source_document__in=material_scan_sources(order, material),
                replacement_documents__isnull=True,
            )
        return documents.none()
    if node["kind"] == "submission" or node["key"] == "payment_eip":
        return documents.filter(kind="attachment", replacement_documents__isnull=True)
    return documents.none()


@transaction.atomic
def remove_document(*, pk, actor):
    document = MaintenanceDocument.objects.get(pk=pk)
    _allowed(MaintenanceDocument, actor, "view", pk)
    _allowed(MaintenanceDocument, actor, "delete", pk)
    _require_actor(actor, MaintenanceDocument, "add")
    order = MaintenanceOrder.objects.select_for_update().get(pk=document.order_id)
    current = order.tasks.filter(status="pending").first()
    if not current:
        raise ValidationError("当前节点不能删除材料。")
    order, current = _task(current.pk, actor)
    if not material_maintenance_documents(order, current, document.material).filter(pk=pk).exists():
        raise ValidationError("只能删除当前可上传节点对应的最新材料。")
    _snapshot(document)
    document.removed_at = timezone.now()
    _save(document)
    # Removing a slot must not reveal one of its older uploads as current again.
    history = order.documents.filter(
        material=document.material, kind=document.kind, is_sealed=document.is_sealed,
        item_number=document.item_number, removed_at__isnull=True,
    ) if document.item_number else order.documents.none()
    removed_ids = [document.pk]
    for previous in history:
        _snapshot(previous)
        previous.removed_at = document.removed_at
        previous._changelog_message = "删除同份材料的历史版本，保留文件及审批关联"
        _save(previous)
        removed_ids.append(previous.pk)
    node = order.steps_snapshot[current.position]
    if node["key"] == scan_fill_key(document.material):
        # Deleting unwanted items is an explicit revision of the submission set.
        # Keep the surviving originals instead of requiring redundant reuploads.
        current.evidence.setdefault("retained_materials", {})[document.material] = list(
            material_scan_candidates(order, document.material).values_list("pk", flat=True)
        )
        _save(current)
    _event(order, actor, "remove_material", "删除材料", current,
           document_id=document.pk, material=document.material, version=document.version,
           item_number=document.item_number, original_name=document.original_name,
           removed_document_ids=removed_ids)
    return document


def _materials(order, node):
    scan_material = node.get("scan_material")
    if scan_material:
        label = dict(MATERIALS)[scan_material]
        scans = list(material_scan_sources(order, scan_material))
        if not scans or any(not scan.file for scan in scans):
            raise ValidationError(
                f"请至少上传一份扫描版{label}。"
                + ("甲方表单和电子附件不能替代乙方报告。" if scan_material == "report" else "")
            )
        if any(state == "sealed" for _, state in node["required"]):
            sealed = []
            missing = []
            for scan in scans:
                stamped = scan.derived_documents.filter(
                    kind="scan", is_sealed=True, removed_at__isnull=True,
                    replacement_documents__isnull=True,
                ).first()
                if not stamped or not stamped.file:
                    missing.append(scan.short_name)
                else:
                    sealed.append(stamped.pk)
            if missing:
                raise ValidationError(
                    f"请上传每份{label}的用印扫描件，尚缺：{'、'.join(missing)}。"
                )
            return {scan_material: sealed}
        return {scan_material: [scan.pk for scan in scans]}
    ids = {}
    missing = []
    errors = []
    for material, state in node["required"]:
        document = order.documents.filter(
            material=material, removed_at__isnull=True, replacement_documents__isnull=True
        ).order_by("-version").first()
        rank = {"electronic": 0, "scan": 1, "sealed": 2}
        if document and document.kind == "form" and material == "report":
            errors.append("服务报告甲方表单不能替代乙方报告，请上传乙方完成的服务报告。")
            continue
        if (
            not document or (document.kind != "form" and not document.file)
            or (2 if document.is_sealed else rank[document.state]) < rank[state]
        ):
            label = dict(MATERIALS)[material]
            if material == "order" and state == "electronic":
                errors.append("请先填写并保存维护服务工单表单。")
                continue
            suffix = (
                "（用印扫描件）" if state == "sealed" else "（扫描件）" if state == "scan" else ""
            )
            missing.append(f"{label}{suffix}")
            continue
        ids[material] = document.pk
    if missing:
        errors.append(f"请上传以下材料：{'、'.join(missing)}。")
    if errors:
        raise ValidationError(errors)
    return ids


def _comment(comment, required=False):
    if len(comment) > 4000 or (required and not comment.strip()):
        raise ValidationError("请填写处理意见，最多 4000 字符。")


@transaction.atomic
def record_order_view(*, pk, actor):
    _allowed(MaintenanceOrder, actor, "view", pk)
    order = MaintenanceOrder.objects.get(pk=pk)
    return _event(order, actor, "view", "打开流程（查阅流程）")


@transaction.atomic
def record_form_print(*, pk, actor):
    """Record a print initiation, without changing the form or its business version."""
    _allowed(MaintenanceDocument, actor, "view", pk)
    document = MaintenanceDocument.objects.select_related("order").get(pk=pk)
    _allowed(MaintenanceOrder, actor, "view", document.order_id)
    if document.kind != "form" or document.material not in PRINTABLE_FORMS:
        raise ValidationError("仅公司表单支持通过插件发起打印。")
    return _event(
        document.order, actor, "print", "打印表单",
        document_id=document.pk, material=document.material, version=document.version,
    )


@transaction.atomic
def complete_task(*, pk, actor, comment="", external_reference=""):
    _comment(comment)
    order, task = _task(pk, actor)
    node = order.steps_snapshot[task.position]
    evidence = _materials(order, node)
    for material, baseline in task.evidence.get("correction_baseline", {}).items():
        latest = order.documents.filter(
            material=material, removed_at__isnull=True, replacement_documents__isnull=True
        ).order_by("-version").first()
        revised = (
            latest.source_document
            if latest and latest.kind == "scan" and latest.source_document_id
            else latest
        )
        adjusted = (
            evidence.get(material) and material_set_adjusted(order, task, material)
        )
        if (not revised or revised.version <= baseline) and not adjusted:
            raise ValidationError("退回补正后必须保存修订表单或上传修订附件，不能直接提交原材料。")
    if task.kind == "external" and not external_reference.strip():
        raise ValidationError("外部流程节点必须登记 EIP 或财务业务单号。")
    if len(external_reference) > 200:
        raise ValidationError("外部单号不能超过 200 字符。")
    handwritten = None
    if node["key"] in {"order_correction", "order_review", "order_final"}:
        from .maintenance_signatures import signature_for_approval

        handwritten = signature_for_approval(
            task, actor, required=node["key"] in {"order_correction"}
        )
    _begin_task(task, actor)
    _transition(task, "complete", actor, comment)
    task.decided_by, task.completed_at, task.comment = actor, timezone.now(), comment
    task.external_reference, task.evidence = external_reference, dict(evidence)
    if node["key"] in {"order_correction", "order_review", "order_final"}:
        signer = handwritten.signer if handwritten else actor
        task.evidence["signature"] = {
            "name": signer.get_full_name() or signer.get_username(),
            "user_id": signer.pk,
            "date": timezone.localdate(task.completed_at).isoformat(),
        }
        if handwritten:
            task.evidence["signature"]["image_id"] = handwritten.pk
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
    _begin_task(task, actor)
    _transition(task, "send_back", actor, comment)
    task.signatures.filter(cancelled=False).update(cancelled=True)
    task.completed_at, task.decided_by, task.comment = timezone.now(), actor, comment
    _save(task)
    order.current_position = target
    order.stage = order.steps_snapshot[target]["stage"]
    # Earlier payment approvals are invalidated when their supporting materials change.
    order.payment_status = (
        "draft" if key == "payment_plan" or order.stage != "payment" else "submitted"
    )
    _save(order)
    correction = _new_task(order)
    target_node = order.steps_snapshot[target]
    required_revisions = [] if key == "payment_plan" else target_node["required"]
    correction.evidence = {
        "correction_baseline": {
            material: order.documents.filter(material=material).aggregate(n=Max("version"))["n"]
            or 0
            for material, _ in required_revisions
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
    task.signatures.filter(cancelled=False).update(cancelled=True)
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
    _begin_task(task, actor)
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
        _snapshot(task)
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
        order._changelog_message = "取消关注工单"
        order.followers.remove(actor)
        _event(order, actor, "unfollow", "取消关注工单")
    else:
        order._changelog_message = "关注工单"
        order.followers.add(actor)
        _event(order, actor, "follow", "关注工单")
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
        allowed = {"order_correction"} if material == "order" else {"report_fill"}
        if key not in allowed:
            raise ValidationError("当前节点不能修改此表单，请退回对应填报节点后修改。")
        if material == "order":
            _allowed(MaintenanceTask, actor, "change", current.pk)
            if not task_is_assigned(current, actor):
                raise PermissionDenied("只有当前填报人员可以修改工单表单。")


def _create_company_document(order, actor, material, data, *, audit=True):
    if material == "order":
        data = {
            **data,
            "priority": order.priority,
            "signature": "",
            "signature_date": "",
        }
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
        audit=audit,
    )
    return document


@transaction.atomic
def save_company_form(*, pk, actor, material, data):
    from .maintenance_forms import MaintenanceCompanyForm

    order = _order(pk, actor)
    check_company_form_access(order, actor, material)
    if (
        material == "order"
        and isinstance(data, dict)
        and not hasattr(data, "getlist")
        and isinstance(data.get("service_region"), str)
    ):
        data = {**data, "service_region": region_ids_from_names(actor, data["service_region"])}
    form = MaintenanceCompanyForm(data, material=material, actor=actor)
    if not form.is_valid():
        raise ValidationError(form.errors.as_text())
    values = dict(form.cleaned_data)
    values["requested_at"] = values["requested_at"].isoformat()
    if material == "order":
        values["signature_date"] = (
            values["signature_date"].isoformat() if values.get("signature_date") else ""
        )
    if material == "order":
        for key in ORDER_FORM_FIELDS:
            setattr(order, key, values[key])
        _save(order)
        _allowed(MaintenanceOrder, actor, "change", order.pk)
        current = order.tasks.filter(status="pending").first()
        if current:
            _begin_task(current, actor)
    return _create_company_document(order, actor, material, values)


@transaction.atomic
def upload_document(
    *, pk, actor, material, file, state="electronic", note="", source_document_id=None,
    replaces_document_id=None, upload_mode=None,
):
    if state not in {"electronic", "scan"}:
        raise ValidationError("材料状态仅支持 electronic（电子版）和 scan（扫描版）。")
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
        if current.kind != "submission" and node["key"] != "payment_eip" and state == "electronic":
            raise ValidationError("修改电子版内容请先退回填报节点。")
    if not actor.is_superuser and actor.pk != order.owner_id:
        if not current or not task_is_assigned(current, actor):
            raise PermissionDenied("只有线路主管或当前节点处理人可以提交材料。")
        _allowed(MaintenanceTask, actor, "change", current.pk)
    else:
        _allowed(MaintenanceOrder, actor, "change", pk)
    scan = state == "scan"
    # Only the handling step can identify a new scan as a stamping result.
    is_sealed = bool(scan and current and node["key"] == f"{material}_seal")
    if material == "order" and not scan:
        raise ValidationError("维护服务工单请在公司表单中填写生成，只能上传纸质或盖章扫描件。")
    source = order.documents.filter(material=material).exclude(kind="scan").first()
    standalone_material = node.get("scan_material") if current else None
    if standalone_material and material == standalone_material:
        label = dict(MATERIALS)[material]
        if not scan:
            raise ValidationError(f"{label}必须上传 PDF、PNG 或 JPEG 扫描件。")
        if node["key"] == scan_fill_key(material):
            if source_document_id:
                raise ValidationError(f"上传{label}时请提交扫描版原件，无需关联电子附件。")
            source = None
        elif node["key"] == f"{material}_seal" and is_sealed:
            source = material_scan_sources(order, material).filter(pk=source_document_id).first()
            if not source:
                raise ValidationError(f"用印扫描件必须对应本次审批的{label}。")
            _allowed(MaintenanceDocument, actor, "view", source.pk)
        else:
            raise ValidationError(f"审批期间不能修改{label}，请先退回{label}填写环节。")
    elif scan:
        if not source or (material == "report" and source.kind == "form"):
            raise ValidationError("请先生成公司表单或上传乙方电子附件，再上传对应扫描件。")
        if source_document_id != source.pk:
            raise ValidationError("扫描件必须对应当前表单或附件版本，请刷新后重新选择。")
        _allowed(MaintenanceDocument, actor, "view", source.pk)
    elif source_document_id:
        raise ValidationError("电子附件不能指定扫描来源版本。")
    replaces = None
    if replaces_document_id and not (
        standalone_material == material and node["key"] == scan_fill_key(material)
    ):
        replaces = material_maintenance_documents(order, current, material).filter(
            pk=replaces_document_id
        ).first()
        if not replaces or replaces.kind != ("scan" if scan else "attachment"):
            raise ValidationError("覆盖对象必须是当前节点可维护的最新材料。")
        if is_sealed and replaces.source_document_id != source_document_id:
            raise ValidationError("覆盖用印件必须关联同一审批原件。")
        _allowed(MaintenanceDocument, actor, "view", replaces.pk)
    elif replaces_document_id or upload_mode:
        if not (standalone_material == material and node["key"] == scan_fill_key(material)):
            raise ValidationError("只能在材料上传节点新增或更新审批扫描件。")
        if upload_mode not in {None, "new", "replace"}:
            raise ValidationError("请选择新增材料或更新已有材料。")
        if upload_mode == "new" and replaces_document_id:
            raise ValidationError("新增材料不能指定更新对象。")
        if upload_mode == "replace" and not replaces_document_id:
            raise ValidationError("更新材料必须选择已有审批件。")
    if scan and not is_sealed and standalone_material == material:
        candidates = material_scan_candidates(order, material)
        if material == "order":
            replaces = candidates.first()
            if replaces_document_id and (not replaces or replaces.pk != replaces_document_id):
                raise ValidationError("维护服务工单只能更新当前扫描件。")
        elif replaces_document_id:
            replaces = candidates.filter(pk=replaces_document_id).first()
            if not replaces:
                raise ValidationError("更新对象必须为本工单同类材料的最新审批件。")
        if replaces:
            _allowed(MaintenanceDocument, actor, "view", replaces.pk)
    elif replaces_document_id and not replaces:
        raise ValidationError("用印扫描件应关联审批原件，不能更新审批件。")
    if scan and Path(file.name).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}:
        raise ValidationError("扫描件仅支持 PDF、PNG 和 JPEG。")
    if Path(file.name).suffix.lower() not in {".pdf", ".docx", ".xlsx", ".png", ".jpg", ".jpeg"}:
        raise ValidationError("支持 PDF、DOCX、XLSX、PNG 和 JPEG 材料。")
    if file.size > 20 * 1024 * 1024:
        raise ValidationError("单份材料不能超过 20 MB。")
    version = (order.documents.filter(material=material).aggregate(n=Max("version"))["n"] or 0) + 1
    item_number = (
        source.item_number if source and is_sealed else replaces.item_number if replaces
        else 1 if material == "order" else
        (order.documents.filter(material=material).aggregate(n=Max("item_number"))["n"] or 0) + 1
    )
    original_name = Path(file.name).name[:255]
    document = MaintenanceDocument(
        order=order,
        material=material,
        version=version,
        file=file,
        original_name=original_name,
        item_number=item_number,
        replaces_document=replaces,
        uploaded_by=actor,
        state=state,
        note=note,
        kind="scan" if scan else "attachment",
        is_sealed=is_sealed,
        uploaded_task=current,
        source_document=source if scan else None,
    )
    document.full_clean()
    file.name = document.display_name
    document.file.name = document.display_name
    if current and task_is_assigned(current, actor):
        if MaintenanceTask.objects.restrict(actor, "change").filter(pk=current.pk).exists():
            _begin_task(current, actor)
    document._changelog_message = (
        f"{'上传覆盖材料' if replaces else '上传材料'}：{document}"
    )[:200]
    document.save()
    try:
        _allowed(MaintenanceDocument, actor, "add", document.pk)
        _event(
            order,
            actor,
            "upload",
            "上传扫描件" if scan else "上传乙方或业务附件",
            task=current,
            material=material,
            version=version,
            document_id=document.pk,
            state=state,
            source_document_id=document.source_document_id,
            replaces_document_id=document.replaces_document_id,
            upload_mode="replace" if document.replaces_document_id else "new",
            item_number=document.item_number,
            original_name=original_name,
        )
    except Exception:
        document.file.delete(save=False)
        raise
    return document


def upload_document_files(
    *, pk, actor, files, material, state, note="", source_document_ids=None,
    replaces_document_ids=None, upload_mode=None,
):
    """Keep a multi-file submission atomic and clean up its storage on failure."""
    if not files:
        raise ValidationError("请至少选择一个文件。")
    if material == "order" and len(files) != 1:
        raise ValidationError("维护服务工单只有一份，每次只能上传一个文件。")
    source_document_ids = source_document_ids or [None] * len(files)
    if len(source_document_ids) != len(files):
        raise ValidationError("请为每个文件选择对应原件。")
    selected_sources = [pk for pk in source_document_ids if pk]
    if len(selected_sources) != len(set(selected_sources)):
        raise ValidationError("同一审批件不能重复关联用印文件。")
    replaces_document_ids = replaces_document_ids or [None] * len(files)
    if len(replaces_document_ids) != len(files):
        raise ValidationError("请为每个文件选择要更新的材料。")
    selected = [pk for pk in replaces_document_ids if pk]
    if len(selected) != len(set(selected)):
        raise ValidationError("同一份材料不能在一次上传中重复更新。")
    saved = []
    try:
        with transaction.atomic():
            for file, source_id, replaces_id in zip(
                files, source_document_ids, replaces_document_ids, strict=True
            ):
                saved.append(upload_document(
                    pk=pk, actor=actor, material=material, file=file, state=state,
                    note=note, source_document_id=source_id,
                    replaces_document_id=replaces_id, upload_mode=upload_mode,
                ))
    except Exception:
        for document in saved:
            document.file.delete(save=False)
        raise
    return saved


def upload_material_scans(
    *, pk, actor, files, material, note="", replaces_document_ids=None, upload_mode=None,
):
    """Save a submission batch atomically, including cleanup of stored files on failure."""
    if not files:
        raise ValidationError("请至少上传一份扫描版材料。")
    saved = []
    try:
        with transaction.atomic():
            _allowed(MaintenanceOrder, actor, "view", pk)
            order = MaintenanceOrder.objects.select_for_update().get(pk=pk)
            node = order.steps_snapshot[order.current_position] if order.steps_snapshot else {}
            scan_material = node.get("scan_material")
            if scan_material != material or node.get("key") != scan_fill_key(material):
                raise ValidationError("当前节点不能上传此扫描材料。")
            saved = upload_document_files(
                pk=pk, actor=actor, files=files, material=material, state="scan", note=note,
                replaces_document_ids=replaces_document_ids, upload_mode=upload_mode,
            )
    except Exception:
        for document in saved:
            document.file.delete(save=False)
        raise
    return saved


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
