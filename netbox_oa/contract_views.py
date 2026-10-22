"""Read-only contract-scoped entry points into the existing OA presentation."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, render
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
        return render(request, "netbox_oa/contract_order_flow.html", {
            "detail": {
                "order": order,
                "current": inbox.summary(order)["current"],
                "approval_diagram": inbox.approval_diagram(order),
            },
            "history": inbox.history(order, limit=50),
        })


class ContractOrderMaterialsView(ContractOrderMixin, MaintenanceReviewView):
    read_only = True
