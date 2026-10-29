"""NetBox home dashboard widgets."""

from django.template.loader import render_to_string
from django.urls import reverse
from extras.dashboard.widgets import DashboardWidget, register_widget


@register_widget
class MaintenanceTodoWidget(DashboardWidget):
    default_title = "工单待办"
    description = "显示当前用户需要审批或办理的工单业务数量，点击进入工单 V2。"
    width = 2
    height = 2

    def render(self, request):
        from .maintenance_views import actionable_tasks

        pending_count = 0
        if request.user.is_authenticated:
            pending_count = actionable_tasks(request.user).values("order_id").distinct().count()
        return render_to_string(
            "netbox_oa/inc/dashboard_todo_widget.html",
            {
                "pending_count": pending_count,
                "todo_url": reverse("plugins:netbox_oa:maintenance_workbench_v2") + "?tab=todo",
            },
            request=request,
        )
