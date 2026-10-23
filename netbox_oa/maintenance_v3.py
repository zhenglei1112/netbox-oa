"""Native NetBox object pages over the existing maintenance workflow services."""

import django_filters
import django_tables2 as tables
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import CharField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import Http404, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.views import View
from django.views.decorators.cache import never_cache
from netbox.filtersets import BaseFilterSet
from netbox.forms import NetBoxModelFilterSetForm
from netbox.tables import NetBoxTable
from netbox.views import generic

from . import maintenance_services as services
from . import maintenance_signatures as signing
from .maintenance_flow import STAGES
from .maintenance_forms import (
    MaintenanceActionForm,
    MaintenanceOrderEditForm,
    MaintenanceOrderForm,
    StyledForm,
)
from .maintenance_signature_views import signature_payload
from .maintenance_v2 import handling_guide
from .maintenance_views import actionable_tasks, presentation
from .models import MaintenanceDocument, MaintenanceOrder, MaintenanceSignature, MaintenanceTask

SCOPES = (
    ("todo", "待办工单"), ("done", "已办工单"), ("mine", "发起工单"),
    ("following", "关注工单"), ("all", "全部可见工单"),
)
LIST_KEYS = (
    "scope", "q", "status", "stage", "node", "sort", "page", "per_page", "tableconfig_id",
    "filter", "filter_id",
)


def params(request):
    source = (
        QueryDict(request.POST.get("return_query", ""))
        if request.method == "POST" else request.GET
    )
    result = QueryDict(mutable=True)
    for key in (*LIST_KEYS, "section", "history"):
        if key in source:
            result.setlist(key, source.getlist(key))
    return result


def page_url(name, *, pk=None, query=None):
    url = reverse(f"plugins:netbox_oa:maintenance_v3_{name}", args=[pk] if pk else [])
    return f"{url}?{query.urlencode()}" if query else url


def detail_url(request, pk):
    return page_url("detail", pk=pk, query=params(request))


def scoped_orders(user, scope):
    orders = MaintenanceOrder.objects.restrict(user, "view")
    if scope == "todo":
        drafts = MaintenanceOrder.objects.restrict(user, "change").filter(status="draft")
        if not user.is_superuser:
            drafts = drafts.filter(Q(requester=user) | Q(owner=user))
        orders = orders.filter(
            Q(pk__in=actionable_tasks(user).values("order_id")) | Q(pk__in=drafts.values("pk"))
        )
    elif scope == "done":
        tasks = MaintenanceTask.objects.restrict(user, "view").filter(
            status="completed", decided_by=user,
        )
        orders = orders.filter(pk__in=tasks.values("order_id"))
    elif scope == "mine":
        orders = orders.filter(requester=user)
    elif scope == "following":
        orders = orders.filter(followers=user)
    return orders


class MaintenanceV3FilterSet(BaseFilterSet):
    q = django_filters.CharFilter(method="search", label="搜索")
    scope = django_filters.ChoiceFilter(choices=SCOPES, method="filter_scope", label="工单范围")
    node = django_filters.CharFilter(
        field_name="v3_node", lookup_expr="icontains", label="当前节点",
    )
    status = django_filters.ChoiceFilter(choices=MaintenanceOrder._meta.get_field("status").choices)
    stage = django_filters.ChoiceFilter(choices=STAGES)

    class Meta:
        model = MaintenanceOrder
        fields = ("q", "scope", "status", "stage", "node")

    def filter_scope(self, queryset, name, value):
        return queryset.filter(pk__in=scoped_orders(self.request.user, value).values("pk"))

    def search(self, queryset, name, value):
        value = value.strip()
        return queryset.filter(
            Q(number__icontains=value) | Q(supplier__icontains=value)
            | Q(contract_number__icontains=value)
        )


class MaintenanceV3FilterForm(StyledForm, NetBoxModelFilterSetForm):
    model = MaintenanceOrder
    scope = forms.ChoiceField(choices=SCOPES, required=False, label="工单范围", initial="todo")
    status = forms.ChoiceField(
        choices=(("", "---------"), *MaintenanceOrder._meta.get_field("status").choices),
        required=False, label="状态",
    )
    stage = forms.ChoiceField(choices=(("", "---------"), *STAGES), required=False, label="阶段")
    node = forms.CharField(required=False, label="当前节点")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class MaintenanceV3Table(NetBoxTable):
    id = tables.Column(linkify=False)
    number = tables.Column(verbose_name="工单编号")
    supplier = tables.Column(verbose_name="服务单位")
    requester = tables.Column(verbose_name="发起人", linkify=False)
    status = tables.Column(verbose_name="状态")
    stage = tables.Column(verbose_name="阶段")
    v3_node = tables.Column(verbose_name="当前节点")
    v3_assignee = tables.Column(verbose_name="办理人 / 组")
    v3_deadline = tables.DateTimeColumn(verbose_name="到期时间", format="Y-m-d H:i:s")
    created = tables.DateTimeColumn(verbose_name="创建时间", format="Y-m-d H:i:s")
    priority = tables.Column(verbose_name="优先级")
    list_query = None

    def render_number(self, value, record):
        return format_html('<a href="{}">{}</a>', page_url(
            "detail", pk=record.pk, query=self.list_query,
        ), value)

    class Meta(NetBoxTable.Meta):
        model = MaintenanceOrder
        exclude = ("actions",)
        fields = (
            "pk", "id", "number", "supplier", "requester", "status", "stage", "v3_node",
            "v3_assignee", "v3_deadline", "priority", "created",
        )
        default_columns = (
            "number", "supplier", "requester", "status", "stage", "v3_node",
            "v3_assignee", "v3_deadline", "priority",
        )

    def render_id(self, value, record):
        return self.render_number(value, record)


class MaintenanceV3ListView(generic.ObjectListView):
    queryset = MaintenanceOrder.objects.all()
    table = MaintenanceV3Table
    filterset = MaintenanceV3FilterSet
    filterset_form = MaintenanceV3FilterForm
    template_name = "netbox_oa/maintenance/v3/list.html"
    actions = ()

    def get_queryset(self, request):
        # NetBox initializes the queryset before its authentication/permission mixin.
        if not request.user.is_authenticated:
            return self.queryset.none()
        # Make the default explicit so filtering, pagination and saved table links agree.
        if not request.GET.get("scope"):
            request.GET = request.GET.copy()
            request.GET["scope"] = "todo"
        pending = MaintenanceTask.objects.restrict(request.user, "view").filter(
            order_id=OuterRef("pk"), order__status="active", status="pending",
        ).order_by("pk")
        return MaintenanceOrder.objects.restrict(request.user, "view").select_related(
            "requester", "owner", "policy",
        ).annotate(
            v3_node=Subquery(pending.values("name")[:1]),
            v3_assignee=Subquery(pending.annotate(label=Coalesce(
                "assigned_user__username", "assigned_group__name", Value("待分配"),
                output_field=CharField(),
            )).values("label")[:1]),
            v3_deadline=Subquery(pending.values("deadline")[:1]),
        ).order_by("-created", "-pk")

    def get_table(self, data, request, bulk_actions=True):
        table = super().get_table(data, request, bulk_actions=False)
        table.list_query = params(request)
        return table

    def get_extra_context(self, request):
        return {"add_url": page_url("add", query=params(request))}


def detail_context(request, order, action_form=None, action_error=""):
    detail = presentation(order, request.user, history_limit=history_limit(request))
    editable = detail["can_change"] and (
        (order.status == "draft" and (
            request.user.is_superuser or request.user.pk in {order.requester_id, order.owner_id}
        )) or (detail["order_correction"] and detail["can_act"])
    )
    if order.status == "draft" and editable:
        detail["can_mobile_sign"] = request.user.pk == order.requester_id
        document = order.documents.restrict(request.user, "view").filter(
            material="order", kind="form",
        ).first()
        detail["mobile_signature"] = MaintenanceSignature.objects.filter(
            document=document, signer=request.user, task__isnull=True,
            cancelled=False, submitted_at__isnull=True,
        ).order_by("-pk").first() if document else None
    all_versions = list(order.documents.restrict(request.user, "view").select_related(
        "source_document",
    ).order_by("-version", "-pk"))
    for material in detail["materials"]:
        can_upload = bool(
            material["required_now"] and detail["can_act"] and material["can_upload_now"]
            and request.user.has_perm("netbox_oa.add_maintenancedocument")
        )
        maintainable = services.material_maintenance_documents(
            order, detail["current"], material["key"],
        ).restrict(request.user, "view") if can_upload else MaintenanceDocument.objects.none()
        material["can_upload"] = can_upload
        material["maintainable"] = list(maintainable)
        removable = set(maintainable.restrict(request.user, "delete").values_list("pk", flat=True))
        for document in material["maintainable"]:
            document.v3_can_remove = document.pk in removable
        material["versions"] = [doc for doc in all_versions if doc.material == material["key"]]
    query = params(request)
    query["ui"] = "v3"
    list_query = params(request)
    list_query.pop("section", None)
    list_query.pop("history", None)
    visible_documents = order.documents.restrict(request.user, "view")
    visible_tasks = order.tasks.restrict(request.user, "view")
    signatures = MaintenanceSignature.objects.filter(
        document__in=visible_documents, cancelled=False,
    ).filter(
        Q(task__isnull=True, submitted_at__isnull=False) | Q(task__in=visible_tasks)
    ).select_related("signer", "task", "document").order_by("pk")
    published_signatures = [signature for signature in signatures if (
        signature.uploaded_at and (
            (not signature.task_id and signature.submitted_at)
            or (signature.task_id and signature.task.status == "completed"
                and signature.task.evidence.get("signature", {}).get("image_id") == signature.pk)
        )
    )]
    return {
        "object": order, "detail": detail, "actions": (), "editable": editable,
        "guide": handling_guide(detail),
        "section": query.get("section") if query.get("section") in {
            "detail", "materials", "history",
        } else "detail",
        "return_query": query.urlencode(), "list_query": list_query.urlencode(),
        "list_url": page_url("list", query=list_query),
        "detail_url": detail_url(request, order.pk),
        "edit_url": page_url("edit", pk=order.pk, query=params(request)),
        "signature_create_url": reverse(
            "plugins:netbox_oa:maintenance_v3_draft_signature", args=[order.pk],
        ) if order.status == "draft" else (
            reverse("plugins:netbox_oa:maintenance_signature_create", args=[detail["current"].pk])
            if detail["current"] else ""
        ),
        "action_form": action_form if action_form is not None else MaintenanceActionForm(
            order=order, actor=request.user,
        ),
        "action_error": action_error,
        "comment_value": request.POST.get("comment", ""),
        "next_history": history_limit(request) + 50,
        "signatures": published_signatures,
    }


def history_limit(request):
    try:
        return max(50, int(params(request).get("history", 50)))
    except (ValueError, TypeError):
        return 50


def error_response(request, pk, error, form=None, status=400):
    order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
    return render(request, "netbox_oa/maintenance/v3/detail.html", detail_context(
        request, order, action_form=form, action_error=error,
    ), status=status)


class MaintenanceV3DetailView(generic.ObjectView):
    queryset = MaintenanceOrder.objects.all()
    template_name = "netbox_oa/maintenance/v3/detail.html"
    actions = ()

    def get_queryset(self, request):
        return super().get_queryset(request).restrict(request.user, "view").select_related(
            "requester", "owner", "policy",
        )

    def get_extra_context(self, request, instance):
        return detail_context(request, instance)

    def get(self, request, **kwargs):
        response = super().get(request, **kwargs)
        services.record_order_view(pk=kwargs["pk"], actor=request.user)
        return response


class MaintenanceV3EditView(LoginRequiredMixin, View):
    def get_order(self, request, pk):
        if pk is None:
            if not request.user.has_perm("netbox_oa.add_maintenanceorder"):
                raise PermissionDenied
            return None
        order = get_object_or_404(
            MaintenanceOrder.objects.restrict(request.user, "view").restrict(
                request.user, "change",
            ),
            pk=pk,
        )
        if order.status == "draft":
            if not request.user.is_superuser and request.user.pk not in {
                order.requester_id, order.owner_id,
            }:
                raise PermissionDenied
        elif not (
            order.status == "active" and actionable_tasks(request.user).filter(order=order).exists()
            and order.steps_snapshot[order.current_position]["key"] == "order_correction"
        ):
            raise PermissionDenied("当前节点不能编辑工单。")
        return order

    def page(self, request, pk=None):
        order = self.get_order(request, pk)
        form_class = MaintenanceOrderEditForm if order else MaintenanceOrderForm
        form = form_class(
            request.POST if request.method == "POST" else None,
            instance=order, actor=request.user,
        )
        document = order.documents.filter(material="order", kind="form").first() if order else None
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    if order:
                        saved = services.update_order(
                            pk=order.pk, actor=request.user,
                            expected_document_id=request.POST.get("expected_document_id"),
                            **form.cleaned_data,
                        )
                    else:
                        saved = services.create_order(actor=request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "工单已保存，请在详情页签名并提交审批。")
                return redirect(detail_url(request, saved.pk))
        return render(request, "netbox_oa/maintenance/v3/edit.html", {
            "object": order or form.instance, "form": form,
            "title": "修改维护服务工单" if order else "新增维护服务工单",
            "return_url": detail_url(request, order.pk) if order else page_url(
                "list", query=params(request),
            ),
            "return_query": params(request).urlencode(),
            "expected_document_id": request.POST.get(
                "expected_document_id", document.pk if document else "",
            ),
        })

    get = page
    post = page


class MaintenanceV3CompanyFormView(MaintenanceV3EditView):
    def page(self, request, pk, material):
        # Only the maintenance order is filled in-app; reports/settlements are uploaded.
        if material != "order":
            raise Http404
        return super().page(request, pk=pk)

    get = page
    post = page


@method_decorator(never_cache, name="dispatch")
class MaintenanceV3DraftSignatureView(LoginRequiredMixin, View):
    def post(self, request, pk):
        get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        try:
            signature = signing.create_draft_session(pk=pk, actor=request.user)
        except ValidationError as exc:
            return JsonResponse({"error": "；".join(exc.messages)}, status=400)
        return JsonResponse(signature_payload(signature, request))
