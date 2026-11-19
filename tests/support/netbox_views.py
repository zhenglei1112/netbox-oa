"""Small native view adapters for standalone HTTP tests, not production implementations."""

import csv

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
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


class ObjectChangeLogView(LoginRequiredMixin, View):
    """URL and object-permission stand-in; NetBox owns changelog rendering."""

    base_template = None

    def get(self, request, model, **kwargs):
        meta = model._meta
        if not request.user.has_perm(f"{meta.app_label}.view_{meta.model_name}"):
            raise PermissionDenied
        get_object_or_404(model.objects.restrict(request.user, "view"), **kwargs)
        return HttpResponse(status=204)


class ObjectListView(BaseObjectView):
    template_name = "generic/object_list.html"
    filterset_form = None

    def get_extra_context(self, request):
        return {}

    def get_table(self, data, request, bulk_actions=True):
        table = self.table(data)
        for name in ("pk", "id", "actions", "created"):
            if name in table.columns:
                table.columns.hide(name)
        RequestConfig(request, paginate={"per_page": 25}).configure(table)
        return table

    def get(self, request):
        queryset = self.filterset(request.GET, self.queryset, request=request).qs
        table = self.get_table(queryset, request)
        if request.GET.get("export") in ("", "table"):
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = 'attachment; filename="ledger.csv"'
            csv.writer(response).writerows(table.as_values(exclude_columns=("pk", "actions")))
            return response
        return render(request, self.template_name, {
            "model": self.queryset.model, "table": table,
            "filter_form": self.filterset_form(request.GET) if self.filterset_form else None,
            "actions": getattr(self, "actions", ()),
            **self.get_extra_context(request),
        })


class ObjectEditView(BaseObjectView):
    pass
