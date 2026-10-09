"""All state mutations go through this transaction and permission boundary.

Always lock the instance before locking a task to serialize concurrent decisions.
"""

from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django_fsm import TransitionNotAllowed
from users.models import Group

from .choices import TaskStatus, WorkflowStatus
from .models import WorkflowDefinition, WorkflowInstance, WorkflowTask


def _require_actor(actor, model, action):
    permission = f"netbox_oa.{action}_{model._meta.model_name}"
    if not actor.is_authenticated or not actor.is_active or not actor.has_perm(permission):
        raise PermissionDenied("没有执行此操作的权限。")


def _allowed(model, actor, action, pk):
    _require_actor(actor, model, action)
    if not model.objects.restrict(actor, action).filter(pk=pk).exists():
        raise PermissionDenied("此对象不在授权范围内。")


def _save(obj):
    obj.full_clean()
    obj.save()


def _snapshot(obj):
    obj.snapshot()


def _transition(obj, action, actor, comment):
    try:
        getattr(obj, action)(by=actor, description=comment)
    except TransitionNotAllowed as exc:
        raise ValidationError("当前状态不允许此操作。") from exc


@transaction.atomic
def create_instance(*, actor, definition, title, payload=None):
    _require_actor(actor, WorkflowInstance, "add")
    _allowed(WorkflowDefinition, actor, "view", definition.pk)
    definition = WorkflowDefinition.objects.select_for_update().get(pk=definition.pk)
    if not definition.enabled:
        raise ValidationError("该流程已停用。")
    instance = WorkflowInstance(
        definition=definition,
        requester=actor,
        title=title,
        payload={} if payload is None else payload,
    )
    _save(instance)
    if not WorkflowInstance.objects.restrict(actor, "add").filter(pk=instance.pk).exists():
        raise PermissionDenied("新流程实例不符合新增权限约束。")
    return instance


def _owned_instance(pk, actor):
    _allowed(WorkflowInstance, actor, "change", pk)
    instance = WorkflowInstance.objects.select_for_update().get(pk=pk)
    if instance.requester_id != actor.pk and not actor.is_superuser:
        raise PermissionDenied("只有申请人或超级管理员可以提交或撤销。")
    _snapshot(instance)
    return instance


def _new_task(instance):
    step = instance.steps_snapshot[instance.current_step]
    task = WorkflowTask(
        instance=instance,
        position=instance.current_step,
        name=step["name"],
        assigned_group_id=step["group_id"],
    )
    _save(task)


@transaction.atomic
def submit_instance(*, pk, actor, comment=""):
    instance = _owned_instance(pk, actor)
    definition = WorkflowDefinition.objects.select_for_update().get(pk=instance.definition_id)
    if not definition.enabled:
        raise ValidationError("该流程已停用。")
    definition.full_clean()
    # Preserve every future approver group with PROTECT before the first decision.
    # Group IDs in JSON alone do not provide referential integrity.
    instance.steps_snapshot = deepcopy(definition.steps)
    group_ids = {step["group_id"] for step in instance.steps_snapshot}
    if len(list(Group.objects.select_for_update().filter(pk__in=group_ids))) != len(group_ids):
        raise ValidationError("审批组不存在。")
    _transition(instance, "submit", actor, comment)
    _save(instance)
    instance.approval_groups.add(*group_ids)
    _new_task(instance)
    return instance


@transaction.atomic
def cancel_instance(*, pk, actor, comment=""):
    instance = _owned_instance(pk, actor)
    _transition(instance, "cancel", actor, comment)
    for task in instance.tasks.select_for_update().filter(status=TaskStatus.PENDING):
        _snapshot(task)
        _transition(task, "cancel", actor, comment)
        task.decided_by = actor
        task.decided_at = timezone.now()
        task.comment = comment
        _save(task)
    _save(instance)
    return instance


@transaction.atomic
def decide_task(*, pk, actor, decision, comment=""):
    if decision not in {"approve", "reject"}:
        raise ValidationError("不支持的审批操作。")
    _allowed(WorkflowTask, actor, "change", pk)
    instance_id = WorkflowTask.objects.values_list("instance_id", flat=True).get(pk=pk)
    instance = WorkflowInstance.objects.select_for_update().get(pk=instance_id)
    task = WorkflowTask.objects.select_for_update().get(pk=pk)
    if not actor.is_superuser and not actor.groups.filter(pk=task.assigned_group_id).exists():
        raise PermissionDenied("当前用户不属于此任务的审批组。")
    if instance.status != WorkflowStatus.PENDING or task.position != instance.current_step:
        raise ValidationError("流程不在此审批节点。")
    _snapshot(instance)
    _snapshot(task)
    _transition(task, decision, actor, comment)
    task.decided_by = actor
    task.decided_at = timezone.now()
    task.comment = comment
    _save(task)
    if decision == "reject":
        _transition(instance, "reject", actor, comment)
    elif instance.current_step + 1 == len(instance.steps_snapshot):
        _transition(instance, "approve", actor, comment)
    else:
        instance.current_step += 1
        _new_task(instance)
    _save(instance)
    return task
