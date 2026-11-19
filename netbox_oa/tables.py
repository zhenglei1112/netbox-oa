import django_tables2 as tables
from django.urls import reverse
from netbox.tables import NetBoxTable

from .maintenance_flow import FLOW_NAME
from .models import WorkflowDefinition, WorkflowInstance, WorkflowTask


class MaintenanceWorkflowEntry:
    name = FLOW_NAME
    slug = "maintenance-service-order"
    description = "内置流程 · 只读"
    enabled = None
    id = None
    pk = None

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:maintenance_workflow_summary")


class WorkflowDefinitionTable(NetBoxTable):
    name = tables.Column(linkify=True, verbose_name="流程名称")
    slug = tables.Column(verbose_name="流程标识")
    enabled = tables.BooleanColumn(verbose_name="是否启用")
    description = tables.Column(verbose_name="描述说明")

    class Meta(NetBoxTable.Meta):
        model = WorkflowDefinition
        fields = ("pk", "id", "name", "slug", "enabled", "description")
        default_columns = ("name", "slug", "enabled", "description")


class WorkflowInstanceTable(NetBoxTable):
    title = tables.Column(linkify=True, verbose_name="申请标题")
    definition = tables.Column(linkify=True, verbose_name="流程定义")
    requester = tables.Column(verbose_name="申请人")
    status = tables.Column(verbose_name="流程状态")
    created = tables.DateTimeColumn(verbose_name="创建时间", format="Y-m-d H:i:s")

    class Meta(NetBoxTable.Meta):
        model = WorkflowInstance
        fields = ("pk", "id", "title", "definition", "requester", "status", "created")
        default_columns = ("title", "definition", "requester", "status", "created")


class WorkflowTaskTable(NetBoxTable):
    name = tables.Column(linkify=True, verbose_name="任务名称")
    instance = tables.Column(linkify=True, verbose_name="所属流程")
    assigned_group = tables.Column(verbose_name="审批组")
    status = tables.Column(verbose_name="任务状态")
    decided_at = tables.DateTimeColumn(verbose_name="处理时间", format="Y-m-d H:i:s")

    class Meta(NetBoxTable.Meta):
        model = WorkflowTask
        fields = ("pk", "id", "name", "instance", "assigned_group", "status", "decided_at")
        default_columns = ("name", "instance", "assigned_group", "status", "decided_at")
