import django_filters

from .models import WorkflowDefinition, WorkflowInstance, WorkflowTask


class WorkflowDefinitionFilterSet(django_filters.FilterSet):
    class Meta:
        model = WorkflowDefinition
        fields = ("id", "name", "slug", "enabled")


class WorkflowInstanceFilterSet(django_filters.FilterSet):
    class Meta:
        model = WorkflowInstance
        fields = ("id", "definition_id", "requester_id", "status")


class WorkflowTaskFilterSet(django_filters.FilterSet):
    class Meta:
        model = WorkflowTask
        fields = ("id", "instance_id", "assigned_group_id", "status")
