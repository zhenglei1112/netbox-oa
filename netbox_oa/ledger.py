"""Read-only work order ledger using NetBox's native object list."""

import re
from functools import partial

import django_filters
import django_tables2 as tables
from django import forms
from django.core.exceptions import ValidationError
from django.db import connections
from django.db.models import (
    Case,
    CharField,
    Count,
    DecimalField,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, NullIf
from django.http import HttpResponse, HttpResponseBadRequest
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html, strip_tags
from netbox.filtersets import BaseFilterSet
from netbox.forms import NetBoxModelFilterSetForm
from netbox.object_actions import BulkExport
from netbox.tables import NetBoxTable
from netbox.views import generic

from .maintenance_contracts import purchase_contracts
from .maintenance_materials import SERVICE_ITEMS, service_type_label
from .maintenance_presentation import person_name, service_type_badges
from .models import MaintenanceDocument, MaintenanceOrder, MaintenanceTask, WorkOrderLedger


def ledger_orders(user):
    documents = MaintenanceDocument.objects.restrict(user, "view").filter(
        order_id=OuterRef("pk"), kind="form",
    ).order_by("-version", "-pk")
    # Select the latest version first: deleted/revised forms must not revive old values.
    order_form = documents.filter(material="order")
    acceptance = documents.filter(material="acceptance")
    current = MaintenanceTask.objects.restrict(user, "view").filter(
        order_id=OuterRef("pk"), position=OuterRef("current_position"), status="pending",
    ).order_by("-pk")
    return WorkOrderLedger.objects.restrict(user, "view").select_related("owner").annotate(
        latest_order_date=Subquery(order_form.annotate(
            seal_date=KeyTextTransform("date", "electronic_seal"),
        ).values("seal_date")[:1]),
        order_removed_at=Subquery(order_form.values("removed_at")[:1]),
        acceptance_form_data=Subquery(acceptance.values("form_data")[:1]),
        acceptance_removed_at=Subquery(acceptance.values("removed_at")[:1]),
        current_task_name=Subquery(current.values("name")[:1]),
    ).annotate(
        order_date=Case(
            When(order_removed_at__isnull=True, then=F("latest_order_date")),
            default=Value(None), output_field=CharField(),
        ),
    ).order_by(F("order_date").desc(nulls_last=True), "-created", "-pk")


def ledger_choices(user):
    orders = WorkOrderLedger.objects.restrict(user, "view") if user else (
        WorkOrderLedger.objects.none()
    )
    provinces = sorted({
        part.strip()
        for value in orders.order_by().values_list("service_region", flat=True).distinct()
        for part in value.split(" / ") if part.strip()
    })
    owners = orders.order_by("owner_id").values_list(
        "owner_id", "owner__first_name", "owner__last_name", "owner__username",
    ).distinct()
    return {
        "province": [(value, value) for value in provinces],
        "owner_id": [(pk, f"{first} {last}".strip() or username)
                     for pk, first, last, username in owners],
        "supplier": [(value, value) for value in orders.order_by("supplier").values_list(
            "supplier", flat=True,
        ).distinct()],
        "contract_number": [(value, value) for value in orders.order_by(
            "contract_number",
        ).values_list("contract_number", flat=True).distinct()],
    }


def ledger_totals(orders):
    amount_field = DecimalField(max_digits=20, decimal_places=2)
    amount = Case(
        When(acceptance_removed_at__isnull=True, then=Cast(
            NullIf(KeyTextTransform("settlement_amount", "acceptance_form_data"), Value("")),
            output_field=amount_field,
        )),
        default=Value(0), output_field=amount_field,
    )
    return orders.aggregate(
        count=Count("pk"),
        amount=Coalesce(Sum(amount), Value(0), output_field=amount_field),
        ordering=Count("pk", filter=Q(stage="ordering")),
        acceptance=Count("pk", filter=Q(stage="acceptance")),
        payment=Count("pk", filter=Q(stage="payment")),
        completed=Count("pk", filter=Q(status="completed")),
    )


def ledger_object_links(orders, user):
    ids = {order.contract_id for order in orders if order.contract_id}
    if not ids:
        return {}
    try:
        contracts = purchase_contracts(user).filter(pk__in=ids).prefetch_related(
            "external_party_object",
        )
    except ValidationError:
        return {}
    links = {}
    for contract in contracts:
        urls = {}
        try:
            urls["contract"] = reverse("plugins:netbox_contract:contract", args=[contract.pk])
        except NoReverseMatch:
            pass
        supplier = contract.external_party_object
        if supplier is not None and callable(getattr(supplier, "get_absolute_url", None)):
            try:
                urls["supplier"] = supplier.get_absolute_url()
            except NoReverseMatch:
                pass
        links[contract.pk] = urls
    return links


class WorkOrderLedgerFilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method="search", label="搜索")
    order_date_after = django_filters.DateFilter(
        field_name="order_date", lookup_expr="gte", label="下单起始日期",
    )
    order_date_before = django_filters.DateFilter(
        field_name="order_date", lookup_expr="lte", label="下单截止日期",
    )
    province = django_filters.ChoiceFilter(method="filter_province", label="省份")
    owner_id = django_filters.ChoiceFilter(label="线路主管")
    service_type = django_filters.MultipleChoiceFilter(
        choices=SERVICE_ITEMS, method="filter_service_types", label="服务类型",
    )
    service_type_mode = django_filters.ChoiceFilter(
        choices=(("or", "或"), ("and", "与")), method="ignore_mode", label="服务类型匹配",
    )
    supplier = django_filters.ChoiceFilter(label="服务商")
    contract_number = django_filters.ChoiceFilter(label="所属合同")

    class Meta:
        model = WorkOrderLedger
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, choices in ledger_choices(self.request.user if self.request else None).items():
            self.filters[name].extra["choices"] = choices

    def search(self, queryset, name, value):
        return queryset.filter(
            Q(number__icontains=value) | Q(contract_number__icontains=value)
            | Q(supplier__icontains=value)
        )

    def filter_province(self, queryset, name, value):
        return queryset.filter(service_region__regex=rf"(^| / ){re.escape(value)}( / |$)")

    def ignore_mode(self, queryset, name, value):
        return queryset

    def filter_service_types(self, queryset, name, values):
        criteria = []
        for value in values:
            item = Q(service_type=value)  # Legacy scalar snapshots.
            if connections[queryset.db].features.supports_json_field_contains:
                item |= Q(service_type__contains=[value])
            else:
                # SQLite domain harness: valid lists contain at most all service choices.
                for index in range(len(SERVICE_ITEMS)):
                    item |= Q(**{f"service_type__{index}": value})
            criteria.append(item)
        match = criteria[0]
        for item in criteria[1:]:
            if self.form.cleaned_data.get("service_type_mode") == "and":
                match &= item
            else:
                match |= item
        return queryset.filter(match)


class WorkOrderLedgerFilterForm(NetBoxModelFilterSetForm):
    model = WorkOrderLedger
    order_date_after = forms.DateField(
        required=False, label="下单起始日期", widget=forms.DateInput(attrs={"type": "date"}),
    )
    order_date_before = forms.DateField(
        required=False, label="下单截止日期", widget=forms.DateInput(attrs={"type": "date"}),
    )
    province = forms.ChoiceField(required=False, label="省份")
    owner_id = forms.ChoiceField(required=False, label="线路主管")
    service_type = forms.MultipleChoiceField(
        required=False, label="服务类型", choices=SERVICE_ITEMS,
    )
    service_type_mode = forms.ChoiceField(
        required=False, label="服务类型匹配", choices=(("or", "或"), ("and", "与")),
    )
    supplier = forms.ChoiceField(required=False, label="服务商")
    contract_number = forms.ChoiceField(required=False, label="所属合同")

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        for name, choices in ledger_choices(user).items():
            self.fields[name].choices = [("", "---------"), *choices]


class WorkOrderLedgerTable(NetBoxTable):
    sequence = tables.Column(empty_values=(), orderable=False, verbose_name="序号")
    order_date = tables.Column(verbose_name="下单日期")
    number = tables.Column(
        verbose_name="工单号",
        linkify=lambda record, table: (
            reverse("plugins:netbox_oa:maintenanceorder", args=[record.pk])
            if record.pk in getattr(table, "detail_ids", ()) else None
        ),
    )
    service_region = tables.Column(verbose_name="省份")
    settlement_amount = tables.TemplateColumn(
        template_code=(
            '{% if record.acceptance_removed_at == None and '
            'record.acceptance_form_data.settlement_amount != None and '
            'record.acceptance_form_data.settlement_amount != "" %}'
            '{{ record.acceptance_form_data.settlement_amount|floatformat:"2g" }}'
            '{% else %}—{% endif %}'
        ), verbose_name="结算金额", orderable=False,
    )
    service_type = tables.Column(verbose_name="服务类型", orderable=False)
    contract_number = tables.Column(
        verbose_name="所属合同",
        linkify=lambda record, table: getattr(table, "object_links", {}).get(
            record.contract_id, {},
        ).get("contract"),
    )
    supplier = tables.Column(
        verbose_name="服务商",
        linkify=lambda record, table: getattr(table, "object_links", {}).get(
            record.contract_id, {},
        ).get("supplier"),
    )
    owner = tables.Column(verbose_name="线路主管")
    current_step = tables.Column(empty_values=(), verbose_name="所在环节", orderable=False)

    class Meta(NetBoxTable.Meta):
        model = WorkOrderLedger
        fields = (
            "pk", "sequence", "order_date", "number", "service_region", "settlement_amount",
            "service_type", "contract_number", "supplier", "owner", "current_step",
        )
        default_columns = fields
        exclude = ("id", "actions")
        empty_text = "暂无工单"
        order_by = ("-order_date",)

    def order_order_date(self, queryset, is_descending):
        direction = F("order_date").desc if is_descending else F("order_date").asc
        return queryset.order_by(direction(nulls_last=True), "-created", "-pk"), True

    def render_sequence(self, bound_row):
        return bound_row.row_counter + (self.page.start_index() if hasattr(self, "page") else 1)

    def value_sequence(self, bound_row):
        return bound_row.row_counter + 1

    def render_order_date(self, value, record):
        return "—" if record.order_removed_at else value

    def render_service_type(self, record):
        return service_type_badges(record.service_type)

    def value_service_type(self, record):
        return service_type_label(record.service_type)

    def render_owner(self, value):
        return person_name(value)

    def render_current_step(self, record):
        if record.status != "active":
            label = record.get_status_display()
            color = {
                "completed": "success", "terminated": "danger", "cancelled": "secondary",
            }.get(record.status, "secondary")
        else:
            label = record.current_task_name or "—"
            color = {
                "ordering": "info", "acceptance": "warning", "payment": "purple",
            }.get(record.stage, "secondary")
        return format_html('<span class="badge text-bg-{}">{}</span>', color, label)

    def value_current_step(self, record):
        return strip_tags(self.render_current_step(record))


class WorkOrderLedgerView(generic.ObjectListView):
    queryset = WorkOrderLedger.objects.all()
    table = WorkOrderLedgerTable
    filterset = WorkOrderLedgerFilterSet
    filterset_form = WorkOrderLedgerFilterForm
    template_name = "netbox_oa/workorder_ledger.html"
    actions = (BulkExport,)

    def get_queryset(self, request):
        self.filterset_form = partial(WorkOrderLedgerFilterForm, user=request.user)
        return ledger_orders(request.user)

    def get_table(self, data, request, bulk_actions=True):
        self.totals = ledger_totals(data)
        table = super().get_table(data, request, bulk_actions)
        table.columns.show("pk")
        # Reading the raw records avoids advancing the table's row counter before rendering.
        orders = table.page.object_list.data if hasattr(table, "page") else data
        table.detail_ids = set(
            MaintenanceOrder.objects.restrict(request.user, "view")
            .filter(pk__in=[order.pk for order in orders]).values_list("pk", flat=True)
        )
        table.object_links = ledger_object_links(orders, request.user)
        return table

    def get_extra_context(self, request):
        return {"ledger_totals": self.totals}

    def get(self, request):
        if request.GET.get("_ledger_totals") == "1":
            orders = self.filterset(request.GET, self.queryset, request=request).qs
            if request.GET.get("_ledger_all") != "1" and request.GET.getlist("_selected"):
                try:
                    ids = [int(value) for value in request.GET.getlist("_selected")]
                    if any(value <= 0 or value > 9223372036854775807 for value in ids):
                        raise ValueError
                except ValueError:
                    return HttpResponseBadRequest("无效的工单选择。")
                orders = orders.filter(pk__in=ids)
            return HttpResponse(render_to_string(
                "netbox_oa/workorder_ledger_totals.html",
                {"ledger_totals": ledger_totals(orders)}, request=request,
            ))
        response = super().get(request)
        # Native quick search, sorting and pagination return only the table fragment.
        if (request.headers.get("HX-Request") == "true"
                and request.headers.get("HX-Boosted") != "true"
                and "export" not in request.GET):
            response.content += render_to_string(
                "netbox_oa/workorder_ledger_totals.html",
                {"ledger_totals": self.totals, "oob": True}, request=request,
            ).encode(response.charset)
        return response
