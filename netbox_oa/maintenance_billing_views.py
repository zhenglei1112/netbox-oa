"""Native NetBox interfaces for per-document settlement calculations."""

import json
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin

from . import maintenance_billing as billing
from .models import MaintenanceDocument, MaintenanceOrder


class BillingForm(forms.Form):
    lines = forms.JSONField(label="计费明细", widget=forms.HiddenInput, max_length=1000000)


def billing_return_query(request):
    return "?return_to=acceptance" if (
        request.GET.get("return_to", request.POST.get("return_to")) == "acceptance"
    ) else ""


def billing_list_url(request, order_id):
    return reverse("plugins:netbox_oa:settlement_billing_list", args=[order_id]) + (
        billing_return_query(request)
    )


class SettlementBillingListView(LoginRequiredMixin, View):
    def get(self, request, pk):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        visible = order.documents.restrict(request.user, "view").filter(material="settlement")
        if request.GET.get("document"):
            documents = [get_object_or_404(visible.filter(
                pk__in=billing.billing_documents(order).values("pk")
            ), pk=request.GET["document"])]
        else:
            documents = list(visible.filter(
                pk__in=billing.billing_documents(order).values("pk")
            ).order_by("item_number", "pk"))
        for document in documents:
            try:
                billing.check_access(document, request.user)
            except (PermissionDenied, ValidationError):
                document.can_edit_billing = False
            else:
                document.can_edit_billing = True
        try:
            billing.check_access(MaintenanceDocument(
                order=order, material="settlement", kind="billing"
            ), request.user)
        except (PermissionDenied, ValidationError):
            can_add = False
        else:
            can_add = True
        total = sum((Decimal(doc.billing_data.get("total", "0")) for doc in documents), Decimal(0))
        return render(request, "netbox_oa/maintenance/settlement_billing_list.html", {
            "order": order, "object": order, "documents": documents,
            "total": billing.money(total),
            "can_add_billing": can_add,
            "billing_return_query": billing_return_query(request),
            "return_url": reverse("plugins:netbox_oa:maintenance_company_form",
                                  args=[order.pk, "acceptance"])
            if billing_return_query(request) else order.get_absolute_url(),
            "return_label": "返回填写验收单" if billing_return_query(request) else "返回办理界面",
        })


@method_decorator(xframe_options_sameorigin, name="dispatch")
class SettlementBillingEditView(LoginRequiredMixin, View):
    def document(self, request, pk):
        document = get_object_or_404(
            MaintenanceDocument.objects.restrict(request.user, "view").select_related("order"),
            pk=pk, material="settlement",
        )
        try:
            billing.check_access(document, request.user)
        except ValidationError as exc:
            raise Http404("；".join(exc.messages)) from None
        return document

    def get(self, request, pk):
        document = self.document(request, pk)
        if request.GET.get("refresh_pricing") == "1":
            return JsonResponse(self.pricing_data(document, request.user),
                                headers={"Cache-Control": "no-store"})
        form = BillingForm(initial={"lines": document.billing_data.get("lines", [])})
        return self.render_form(request, document, form)

    def post(self, request, pk):
        document = self.document(request, pk)
        form = BillingForm(request.POST)
        if form.is_valid():
            try:
                if request.GET.get("preview") == "1":
                    return JsonResponse(billing.calculate_for_order(
                        document.order, request.user, form.cleaned_data["lines"],
                    ))
                if document.pk:
                    saved = billing.save_billing(
                        document.pk, request.user, form.cleaned_data["lines"]
                    )
                else:
                    saved = billing.create_billing(document.order, request.user,
                                                   form.cleaned_data["lines"])
            except ValidationError as exc:
                form.add_error("lines", exc)
            else:
                if request.GET.get("embed") == "1":
                    return render(request, "netbox_oa/maintenance/settlement_billing_complete.html",
                                  {"total": saved.billing_data["total"], "summary_html":
                                   render_to_string(
                                       "netbox_oa/maintenance/acceptance_billing_summary.html",
                                       {"billing_data": saved.billing_data}, request=request,
                                   )})
                messages.success(request, "计费明细已保存，请重新保存验收单并签名。")
                return redirect(billing_list_url(request, document.order_id))
        if request.GET.get("preview") == "1":
            return JsonResponse({"error": "；".join(
                str(error) for errors in form.errors.values() for error in errors
            )}, status=400)
        return self.render_form(request, document, form)

    def pricing_data(self, document, actor):
        error = ""
        try:
            schedule = billing.schedule_for(document.order, actor)
            rules = list(billing.catalogue(schedule).values())
        except ValidationError as exc:
            rules, error = [], "；".join(exc.messages)
        return {
            "rules": rules, "pricing_error": error,
            "routine_maintenance_allowed": billing.routine_maintenance_allowed(
                document.order, actor,
            ),
        }

    def render_form(self, request, document, form):
        initial = document.billing_data.get("lines", [])
        if form.is_bound:
            try:
                initial = json.loads(form.data.get("lines", "[]"))
                if not isinstance(initial, list):
                    initial = []
            except (TypeError, ValueError):
                initial = []
        template = ("netbox_oa/maintenance/settlement_billing_embedded.html"
                    if request.GET.get("embed") == "1"
                    else "netbox_oa/maintenance/settlement_billing_edit.html")
        return render(request, template, {
            "form": form, "document": document, "object": document.order,
            **self.pricing_data(document, request.user), "billing_initial": initial,
            "billing_contract_url": reverse(
                "plugins:netbox_contract:contract", args=[document.order.contract_id],
            ),
            "return_url": billing_list_url(request, document.order_id),
            "billing_return_query": billing_return_query(request),
        })


class SettlementBillingCreateView(SettlementBillingEditView):
    def document(self, request, pk):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        document = billing.billing_documents(order).restrict(request.user, "view").first()
        document = document or MaintenanceDocument(
            order=order, material="settlement", kind="billing", original_name="结算费用明细"
        )
        try:
            billing.check_access(document, request.user)
        except ValidationError as exc:
            raise Http404("；".join(exc.messages)) from None
        return document


