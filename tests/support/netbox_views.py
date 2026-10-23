"""Small native view adapters for standalone HTTP tests, not production implementations."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.views import View
from django_tables2 import RequestConfig


class BaseObjectView(LoginRequiredMixin, View):
    def get_queryset(self, request):
        return self.queryset.all()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.queryset = self.get_queryset(request)
        model = self.queryset.model._meta
        if not request.user.has_perm(f"{model.app_label}.view_{model.model_name}"):
            raise PermissionDenied
        self.queryset = self.queryset.restrict(request.user, "view")
        return super().dispatch(request, *args, **kwargs)


class ObjectView(BaseObjectView):
    def get(self, request, **kwargs):
        instance = get_object_or_404(self.queryset, **kwargs)
        return render(request, self.template_name, {
            "object": instance, "actions": (),
            **self.get_extra_context(request, instance),
        })


class ObjectListView(BaseObjectView):
    def get_table(self, data, request, bulk_actions=True):
        table = self.table(data)
        for name in ("pk", "id", "actions", "created"):
            if name in table.columns:
                table.columns.hide(name)
        RequestConfig(request, paginate={"per_page": 25}).configure(table)
        return table

    def get(self, request):
        queryset = self.filterset(request.GET, self.queryset, request=request).qs
        return render(request, self.template_name, {
            "model": self.queryset.model, "table": self.get_table(queryset, request),
            "filter_form": self.filterset_form(request.GET), "actions": (),
            **self.get_extra_context(request),
        })
