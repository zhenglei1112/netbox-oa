"""Read-only contract-scoped entry points into the existing OA presentation."""

from importlib import import_module

from django.apps import apps
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import (
    Case,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
    prefetch_related_objects,
)
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import NoReverseMatch, reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin

from .maintenance_contracts import purchase_contracts
from .maintenance_presentation import InboxPresentation
from .maintenance_views import MaintenanceReviewView
from .models import MaintenanceOrder, MaintenanceTask


def contract_orders(user, contract_id):
    return MaintenanceOrder.objects.restrict(user, "view").filter(
        contract_id=contract_id
    ).select_related("requester").order_by("-created", "-pk")


def with_payment_progress(orders, user):
    """Match visible payments without issuing a separate query for every order."""
    if not user.has_perm("netbox_contract.view_invoice"):
        return orders.annotate(payment_progress=Value(-1, output_field=IntegerField()))
    model = apps.get_model("netbox_contract", "Invoice")
    ranks = {"计划": 1, "已提交": 2, "已支付": 3}
    invoices = model.objects.restrict(user, "view").filter(
        contracts=OuterRef("contract_id"), number__icontains=OuterRef("number"),
    ).exclude(template=True).annotate(
        progress=Case(
            *(When(status=code, then=Value(rank)) for code, rank in ranks.items()),
            default=Value(0), output_field=IntegerField(),
        )
    ).order_by("-progress", "pk")
    return orders.annotate(
        payment_progress=Subquery(invoices.values("progress")[:1]),
        payment_multiple=Exists(invoices.order_by("pk")[1:2]),
    )


class ContractOrderMixin:
    def get_order(self, request, pk, contract_id):
        try:
            contracts = purchase_contracts(request.user)
        except ValidationError as exc:
            raise Http404 from exc
        get_object_or_404(contracts, pk=contract_id)
        return get_object_or_404(contract_orders(request.user, contract_id), pk=pk)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class ContractOrderFlowView(ContractOrderMixin, LoginRequiredMixin, View):
    def get(self, request, contract_id, pk):
        order = self.get_order(request, pk, contract_id)
        inbox = InboxPresentation([order], request.user, MaintenanceTask.objects.none())
        history = inbox.history(order, limit=50)
        return render(request, "netbox_oa/contract_order_flow.html", {
            "detail": {
                "order": order,
                "current": inbox.summary(order)["current"],
                "approval_diagram": inbox.approval_diagram(order),
                "approval_events": history["approval_events"],
            },
            "history": history,
        })


class ContractOrderMaterialsView(ContractOrderMixin, MaintenanceReviewView):
    read_only = True


class ContractOrderPaymentsView(ContractOrderMixin, LoginRequiredMixin, View):
    def get(self, request, contract_id, pk):
        order = self.get_order(request, pk, contract_id)
        if not request.user.has_perm("netbox_contract.view_invoice"):
            raise PermissionDenied
        model = apps.get_model("netbox_contract", "Invoice")
        invoices = model.objects.restrict(request.user, "view").filter(
            contracts=contract_id, number__icontains=order.number
        ).exclude(template=True).distinct().order_by("pk").prefetch_related("contracts")
        module = import_module(f"{apps.get_app_config('netbox_contract').name}.tables")
        linked_faults = linked_payment_faults(request, contract_id, invoices)
        table = module.InvoiceListTable(invoices)
        table.columns.hide("contracts")
        table.prefix = "invoices_"
        table.configure(request)
        table.columns.show("tags")
        try:
            contract_faults_url = reverse(
                "plugins:netbox_otnfaults:contract_faults_fragment", args=[contract_id]
            )
        except NoReverseMatch:
            contract_faults_url = ""
        return render(request, "netbox_oa/contract_order_payments.html", {
            "table": table, "order": order, "linked_faults": linked_faults,
            "contract_faults_url": contract_faults_url,
        })


def payment_fault_filter(invoices):
    """Union each payment's own date/tag criteria; never widen gaps between payments."""
    criteria = Q(pk__in=[])
    for invoice in invoices:
        if not invoice.period_start or not invoice.period_end:
            continue
        if invoice.period_start > invoice.period_end:
            continue
        payment = Q(
            fault_occurrence_time__date__gte=invoice.period_start,
            fault_occurrence_time__date__lte=invoice.period_end,
        )
        tags = [tag.pk for tag in invoice.tags.all()]
        if tags:
            payment &= Q(tags__pk__in=tags)
        criteria |= payment
    return criteria


def linked_payment_faults(request, contract_id, invoices):
    try:
        model = apps.get_model("netbox_otnfaults", "OtnFault")
    except LookupError:
        return None
    # Evaluate once so payment rows, contracts and tags are shared by both tables.
    payments = list(invoices)
    prefetch_related_objects(payments, "tags")
    faults = model.objects.restrict(request.user, "view").filter(
        payment_fault_filter(payments), contract_id=contract_id
    ).distinct().select_related("duty_officer", "interruption_location_a").prefetch_related(
        "interruption_location", "tags"
    ).order_by("-fault_occurrence_time", "-pk")
    module = import_module(f"{apps.get_app_config('netbox_otnfaults').name}.tables")
    table = module.ContractOtnFaultTable(faults, prefix="faults_")
    table.configure(request)
    return table
