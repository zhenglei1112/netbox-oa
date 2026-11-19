import django_filters
from django.db.models import Q

from .models import WorkflowDefinition, WorkflowInstance, WorkflowTask


class WorkflowDefinitionFilterSet(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains", label="流程名称")
    slug = django_filters.CharFilter(lookup_expr="icontains", label="流程标识")
    enabled = django_filters.BooleanFilter(label="是否启用")

    class Meta:
        model = WorkflowDefinition
        fields = ("id", "name", "slug", "enabled")


class WorkflowDefinitionListFilterSet(WorkflowDefinitionFilterSet):
    q = django_filters.CharFilter(method="search", label="搜索")

    def search(self, queryset, name, value):
        return queryset.filter(Q(name__icontains=value) | Q(slug__icontains=value))

    def matches_read_only_entry(self, entry):
        self.form.is_valid()
        filters = self.form.cleaned_data
        search = filters.get("q", "").casefold()
        name = entry.name.casefold()
        slug = entry.slug.casefold()
        return (
            filters.get("id") is None
            and filters.get("enabled") is None
            and filters.get("name", "").casefold() in name
            and filters.get("slug", "").casefold() in slug
            and (search in name or search in slug)
        )


class WorkflowInstanceFilterSet(django_filters.FilterSet):
    definition_id = django_filters.NumberFilter(label="流程定义 ID")
    requester_id = django_filters.NumberFilter(label="申请人 ID")
    status = django_filters.ChoiceFilter(
        choices=WorkflowInstance._meta.get_field("status").choices, label="流程状态"
    )

    class Meta:
        model = WorkflowInstance
        fields = ("id", "definition_id", "requester_id", "status")


class WorkflowTaskFilterSet(django_filters.FilterSet):
    instance_id = django_filters.NumberFilter(label="所属流程 ID")
    assigned_group_id = django_filters.NumberFilter(label="审批组 ID")
    status = django_filters.ChoiceFilter(
        choices=WorkflowTask._meta.get_field("status").choices, label="任务状态"
    )

    class Meta:
        model = WorkflowTask
        fields = ("id", "instance_id", "assigned_group_id", "status")
