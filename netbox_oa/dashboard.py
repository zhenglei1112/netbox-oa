"""NetBox home dashboard widgets."""

from django.db.models import F, Q
from django.template.loader import render_to_string
from django.urls import reverse
from extras.dashboard.widgets import DashboardWidget, register_widget

from .models import MaintenanceOrder


@register_widget
class MaintenanceTodoWidget(DashboardWidget):
    default_title = "工单待办"
    description = "分别显示当前账号的待办、可代办工单和个人草稿数量，点击进入工单 V2。"
    width = 2
    height = 2

    def render(self, request):
        from .maintenance_views import actionable_tasks

        pending_count = 0
        delegated_count = 0
        draft_count = 0
        if request.user.is_authenticated:
            tasks = actionable_tasks(request.user).filter(position=F("order__current_position"))
            assigned = Q(assigned_user=request.user) | Q(
                assigned_user__isnull=True, assigned_group__in=request.user.groups.all()
            )
            pending_count = tasks.filter(assigned).values(
                "order_id"
            ).distinct().count()
            delegated_count = tasks.exclude(assigned).values(
                "order_id"
            ).distinct().count()
            draft_count = MaintenanceOrder.objects.restrict(request.user, "view").filter(
                status="draft", requester=request.user,
            ).count()
        return render_to_string(
            "netbox_oa/inc/dashboard_todo_widget.html",
            {
                "pending_count": pending_count,
                "delegated_count": delegated_count,
                "has_pending": bool(pending_count or delegated_count),
                "draft_count": draft_count,
                "todo_url": reverse("plugins:netbox_oa:maintenance_workbench_v2") + "?tab=todo",
            },
            request=request,
        )
