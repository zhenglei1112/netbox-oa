import django_tables2 as tables
from netbox.tables import NetBoxTable

from .models import WorkflowDefinition, WorkflowInstance, WorkflowTask


class WorkflowDefinitionTable(NetBoxTable):
    name = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = WorkflowDefinition
        fields = ("pk", "id", "name", "slug", "enabled", "description")
        default_columns = ("name", "slug", "enabled", "description")


class WorkflowInstanceTable(NetBoxTable):
    title = tables.Column(linkify=True)
    definition = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = WorkflowInstance
        fields = ("pk", "id", "title", "definition", "requester", "status", "created")
        default_columns = ("title", "definition", "requester", "status", "created")


class WorkflowTaskTable(NetBoxTable):
    name = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = WorkflowTask
        fields = ("pk", "id", "name", "instance", "assigned_group", "status", "decided_at")
        default_columns = ("name", "instance", "assigned_group", "status", "decided_at")
