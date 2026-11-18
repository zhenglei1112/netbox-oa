"""Permission-filtered inbox and activity presentation, independent of workflow writes."""

from collections import defaultdict
from datetime import timedelta
from textwrap import wrap

from django.contrib.auth import get_user_model
from django.db.models import Min, OuterRef, Q, Subquery
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from users.models import Group

from .maintenance_flow import ROLES
from .maintenance_materials import SERVICE_ITEMS, service_type_values
from .models import MaintenanceEvent, MaintenanceOrder, MaintenanceTask
from .time_display import format_duration


def service_type_badges(value):
    values = service_type_values(value)
    if not values:
        return "—"
    colors = {
        "fiber": "primary", "construction": "info", "cutover": "purple",
        "air": "success", "inspection": "secondary", "power": "warning",
    }
    labels = dict(SERVICE_ITEMS)
    badges = format_html_join(
        "", '<span class="badge text-bg-{}">{}</span>',
        ((colors.get(item, "secondary"), labels.get(item, item)) for item in values),
    )
    return format_html('<div class="d-flex flex-wrap gap-1">{}</div>', badges)


def person_name(person, viewer=None):
    if viewer and person.pk == viewer.pk:
        return "你"
    return person.get_full_name().strip() or person.get_username()


def event_label(event):
    if event.action == "complete" and event.task and event.task.kind in {"approval", "countersign"}:
        return "同意"
    return event.description


def activity_meta(event):
    labels = {
        "create": ("创建申请", "+"),
        "start": ("发起流程", "→"),
        "edit": ("更新资料", "≡"),
        "return": ("退回补正", "↩"),
        "claim": ("领取任务", "●"),
        "transfer": ("转办", "⇄"),
        "countersign": ("加签", "+"),
        "upload": ("上传材料", "↑"),
        "remove_material": ("删除材料", "×"),
        "form": ("保存表单", "▤"),
        "terminate": ("终止工单", "×"),
        "withdraw": ("撤销申请", "×"),
        "link": ("关联对象", "↗"),
    }
    if event.action == "complete":
        kind = event.task.kind if event.task else "action"
        label = {
            "approval": "同意",
            "countersign": "同意",
            "submission": "提交资料",
            "external": "登记结果",
        }.get(kind, "完成办理")
        return label, "✓"
    return labels.get(event.action, ("更新进展", "·"))


class InboxPresentation:
    """Load shared list data once, never query events separately for each row."""

    def __init__(self, orders, user, actionable):
        self.user = user
        self.now = timezone.now()
        self.group_ids = set(user.groups.values_list("pk", flat=True))
        ids = [order.pk for order in orders]
        self.actionable = set(actionable.filter(order_id__in=ids).values_list("pk", flat=True))
        self.changeable = set(MaintenanceTask.objects.restrict(user, "change").filter(
            order_id__in=ids, status="pending", order__status="active"
        ).values_list("pk", flat=True))
        visible_tasks = MaintenanceTask.objects.restrict(user, "view")
        self.tasks = defaultdict(list)
        for task in (
            visible_tasks.filter(order_id__in=ids)
            .select_related("assigned_group", "assigned_user", "decided_by")
            .order_by("pk")
        ):
            self.tasks[task.order_id].append(task)
        self.events = MaintenanceEvent.objects.restrict(user, "view").filter(
            Q(task__isnull=True) | Q(task__in=visible_tasks)
        ).exclude(action__in={"view", "preview", "download", "print"})
        self.started_at = dict(
            self.events.filter(order_id__in=ids, action="start")
            .order_by()
            .values("order_id")
            .annotate(started_at=Min("created"))
            .values_list("order_id", "started_at")
        )
        latest = self.events.filter(order_id=OuterRef("pk")).order_by("-created", "-pk")
        event_ids = (
            MaintenanceOrder.objects.filter(pk__in=ids)
            .annotate(latest_event=Subquery(latest.values("pk")[:1]))
            .values_list("latest_event", flat=True)
        )
        self.latest = {
            event.order_id: event
            for event in self.events.filter(pk__in=event_ids).select_related("actor", "task")
        }
        # Future assignments are only named when those directory objects are visible.
        self.people = self._directory(
            get_user_model(),
            user,
            {
                step["user_id"]
                for order in orders
                for step in order.steps_snapshot
                if step.get("user_id")
            },
        )
        self.groups = self._directory(
            Group,
            user,
            {
                step["group_id"]
                for order in orders
                for step in order.steps_snapshot
                if step.get("group_id")
            },
        )

    @staticmethod
    def _directory(model, user, ids):
        query = model.objects.filter(pk__in=ids)
        if hasattr(query, "restrict"):
            query = query.restrict(user, "view")
        elif not user.is_superuser:
            return {}
        return {obj.pk: obj for obj in query}

    def assignee(self, task):
        if task.assigned_user_id:
            return person_name(task.assigned_user, self.user)
        return task.assigned_group.name if task.assigned_group_id else "待分配"

    def summary(self, order):
        latest = {task.position: task for task in self.tasks[order.pk]}
        current = latest.get(order.current_position) if order.status == "active" else None
        if current and current.status != "pending":
            current = None
        assigned = bool(
            current
            and (
                current.assigned_user_id == self.user.pk
                or (not current.assigned_user_id and current.assigned_group_id in self.group_ids)
            )
        )
        has_task_permission = bool(current and assigned and current.pk in self.changeable)
        own_approval = bool(current and current.kind in {"approval", "countersign"}
                            and order.requester_id == self.user.pk)
        self_approval_blocked = has_task_permission and own_approval
        can_act = bool(current and current.pk in self.actionable and not own_approval)
        status = order.get_status_display()
        if order.status == "active":
            status = "等待他人" if current else "进行中"
            if self_approval_blocked:
                status = "等待其他审批人"
            if can_act:
                status = (
                    ("待你审批" if current.kind in {"approval", "countersign"} else "待你处理")
                    if assigned
                    else "可代办"
                )
        nodes = []
        for index, step in enumerate(order.steps_snapshot):
            task = latest.get(index)
            if step.get("return_only") and not task and index != order.current_position:
                continue
            future = order.status == "active" and index > order.current_position
            state = task.status if task and not future else "not_started"
            if task and not future:
                label = (
                    person_name(task.decided_by, self.user)
                    if task.decided_by_id
                    else self.assignee(task)
                )
            elif step.get("role") == "requester":
                label = person_name(order.requester, self.user)
            elif step.get("user_id"):
                person = self.people.get(step["user_id"])
                label = person_name(person, self.user) if person else "指定办理人"
            elif step.get("role") == "owner":
                label = person_name(order.owner, self.user)
            else:
                group = self.groups.get(step.get("group_id"))
                label = group.name if group else dict(ROLES).get(step.get("role"), "办理组")
            nodes.append(
                {
                    **step,
                    "index": index,
                    "task": task,
                    "state": state,
                    "label": label,
                    "name": "加签审核" if step.get("kind") == "countersign" else step["name"],
                    "current": bool(current and index == order.current_position),
                    "symbol": {
                        "completed": "✓",
                        "pending": "●",
                        "returned": "↩",
                        "cancelled": "×",
                    }.get(state, "○"),
                }
            )
        progress = [
            {
                "label": person_name(order.requester, self.user),
                "name": "发起",
                "symbol": "✓" if order.status != "draft" else "○",
            },
            *nodes,
        ]
        if len(progress) > 5:
            done = [node for node in nodes if node["state"] == "completed"]
            preview = [progress[0]]
            if done:
                preview.append(done[-1])
            preview.extend(node for node in nodes if node["current"])
            future_nodes = [node for node in nodes if node["index"] > order.current_position]
            if order.status == "active" and future_nodes:
                preview.append(future_nodes[0])
        else:
            preview = progress
        event = self.latest.get(order.pk)
        activity = (
            f"{person_name(event.actor, self.user)} · {event_label(event)}"
            if event
            else "暂无可查看进展"
        )
        waiting_for = self.assignee(current) if current else ""
        if waiting_for:
            activity += f"，等待{waiting_for}"
        waited = max(0, int((self.now - current.created).total_seconds() // 60)) if current else 0
        deadline_state = "normal"
        if current and self.now >= current.deadline:
            deadline_state = "overdue"
        elif current and self.now + timedelta(hours=order.warning_hours) >= current.deadline:
            deadline_state = "warning"
        step = order.steps_snapshot[current.position] if current else {}
        return {
            "order": order,
            "current": current,
            "can_act": can_act,
            "self_approval_blocked": self_approval_blocked,
            "assigned_to_me": assigned,
            "status_label": status,
            "requester_name": person_name(order.requester, self.user),
            "initiated_at": (
                order.created if order.status == "draft" else self.started_at.get(order.pk)
            ),
            "waiting_for": waiting_for,
            "latest_activity": activity,
            "activity_at": event.created if event else order.created,
            "nodes": nodes,
            "progress": progress,
            "progress_preview": preview,
            "future_nodes": [node for node in nodes if node["index"] > order.current_position]
            if order.status == "active"
            else [],
            "waiting": format_duration(waited),
            "deadline_state": deadline_state,
            "required_materials": {"order"}
            if order.status == "draft"
            else {key for key, _ in step.get("required", [])},
            "can_return": bool(step.get("return_to"))
            and current.kind in {"approval", "external", "countersign"}
            if current
            else False,
        }

    def history(self, order, limit):
        records = list(
            self.events.filter(order=order)
            .select_related("actor", "task")
            .order_by("-created", "-pk")[: limit + 1]
        )
        events = []
        for event in reversed(records[:limit]):
            label, symbol = activity_meta(event)
            events.append(
                {
                    "event": event,
                    "actor_name": person_name(event.actor, self.user),
                    "action_label": event_label(event),
                    "description": event.description,
                    "short_label": label,
                    "symbol": symbol,
                }
            )
        grouped = {}
        current_activity = None
        current_comments = []
        actions = {
            "create",
            "start",
            "complete",
            "return",
            "transfer",
            "countersign",
            "terminate",
            "withdraw",
        }
        approval_records = (
            self.events.filter(order=order, action__in=actions)
            .select_related("actor", "task")
            .order_by("created", "pk")
        )
        for event in approval_records:
            label, symbol = activity_meta(event)
            comment = event.details.get("comment", "")
            record_comment = comment if comment.strip() != label else ""
            if event.action == "complete" and event.task_id:
                node = order.steps_snapshot[event.task.position]
                automatic_payment_comments = {
                    "payment_review": "付款已提交，校验通过，自动流转。",
                    "payment_eip": "付款已支付，校验通过，自动流转。",
                }
                automatic_comment = automatic_payment_comments.get(node.get("key"))
                if automatic_comment and comment.startswith(automatic_comment):
                    record_comment = "自动批转"
            record_context = event.description if event.action in {
                "return", "transfer", "countersign"
            } and event.description not in {label, comment} else ""
            item = {
                "event": event,
                "actor_name": person_name(event.actor, self.user),
                "actor_full_name": person_name(event.actor),
                "action_label": event_label(event),
                "description": event.description,
                "short_label": label,
                "symbol": symbol,
                "record_comment": record_comment,
                "record_context": record_context,
            }
            if event.task_id:
                if event.task.status == "pending" and event.task.position == order.current_position:
                    current_activity = item
                    if comment := event.details.get("comment"):
                        current_comments.append(comment)
                    continue  # The current node already has a single waiting entry.
                key = ("task", event.task_id)
            elif event.action in {"create", "start"}:
                key = ("init", order.pk)
            else:
                key = (event.action, event.pk)
            previous = grouped.get(key)
            if previous and previous["event"].action in {"complete", "return"}:
                continue
            grouped[key] = item
        approval_events = sorted(
            grouped.values(), key=lambda item: (item["event"].created, item["event"].pk)
        )
        return {
            "events": events,
            "approval_events": approval_events,
            "current_activity": current_activity,
            "current_activity_comment": "\n".join(current_comments),
            "recent_events": list(reversed(approval_events[-6:])),
            "sidebar_events": list(reversed(approval_events)),
            "has_earlier_events": len(records) > limit,
        }

    def approval_diagram(self, order):
        from .maintenance_diagram import build_diagram
        from .maintenance_flow import STAGES

        if not order.steps_snapshot:
            return None
        stages = {}
        normal_position = 0
        for position, step in enumerate(order.steps_snapshot):
            if not step.get("return_only"):
                normal_position += 1
            attempts = []
            tasks = [task for task in self.tasks[order.pk] if task.position == position]
            for task in tasks:
                if task.decided_by_id:
                    actor = task.decided_by.get_full_name() or task.decided_by.get_username()
                elif task.assigned_user_id:
                    actor = task.assigned_user.get_full_name() or task.assigned_user.get_username()
                else:
                    actor = "待办理"
                attempts.append(
                    {
                        "actor": actor,
                        "time": task.completed_at,
                        "status": task.get_status_display(),
                        "name": task.name,
                        "comment": task.comment,
                    }
                )
            state = tasks[-1].status if tasks else "not_started"
            if order.status == "active" and position == order.current_position:
                state = "current"
            node = {
                **step,
                "position": "修正" if step.get("return_only") else normal_position,
                "return_label": bool(step.get("return_to")),
                "attempts": attempts,
                "state": state,
                "state_label": {
                    "completed": "已完成",
                    "current": "当前环节",
                    "returned": "已退回",
                    "cancelled": "已取消",
                    "pending": "待处理",
                }.get(state, "未办理"),
            }
            stages.setdefault(
                step["stage"],
                {
                    "name": dict(STAGES).get(step["stage"], step["stage"]),
                    "nodes": [],
                },
            )["nodes"].append(node)
        # Match the definition diagram: correction is a side path below the main lane.
        for stage in stages.values():
            stage["nodes"].sort(key=lambda node: bool(node.get("return_only")))
        for stage in stages.values():
            for node in stage["nodes"]:
                title_extra = (len(wrap(node["name"], 17)) - 1) * 20
                node["height"] = 64 + title_extra + max(0, len(node["attempts"]) - 1) * 30
        diagram = build_diagram(list(stages.values()), node_gap=24)
        diagram["node_height"] = max(node["height"] for node in diagram["nodes"])
        for node in diagram["nodes"]:
            content_y = node["label_lines"][-1]["y"] + 17
            for index, attempt in enumerate(node["attempts"]):
                attempt["actor_y"] = content_y + index * 30
                attempt["time_y"] = attempt["actor_y"] + 14
            node["empty_y"] = content_y
        return diagram
