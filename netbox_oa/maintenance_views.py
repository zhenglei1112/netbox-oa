from datetime import timedelta
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.http import FileResponse, Http404, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin

from . import maintenance_services as services
from .maintenance_contracts import order_object_links
from .maintenance_diagram import build_diagram
from .maintenance_flow import (
    FLOW_NAME,
    MATERIALS,
    RETIRED_MATERIALS,
    ROLES,
    STAGES,
    build_steps,
    scan_fill_key,
)
from .maintenance_forms import (
    MaintenanceActionForm,
    MaintenanceCompanyForm,
    MaintenanceDocumentForm,
    MaintenanceLinkForm,
    MaintenanceMaterialUploadForm,
    MaintenanceOrderEditForm,
    MaintenanceOrderForm,
    MaintenancePolicyForm,
    MaintenanceScanUploadForm,
)
from .maintenance_materials import (
    ACCEPTANCE_SIGNING_NODES,
    COMPANY_FORMS,
    PRINTABLE_FORMS,
    SERVICE_ITEMS,
    TASK_SIGNING_NODES,
    company_form_initial,
    service_effect_label,
    service_type_values,
)
from .maintenance_payments import payment_context
from .maintenance_presentation import InboxPresentation
from .maintenance_v2 import handling_guide
from .models import (
    MaintenanceDocument,
    MaintenanceOrder,
    MaintenancePolicy,
    MaintenanceTask,
)
from .services import _allowed


def actionable_tasks(user):
    tasks = MaintenanceTask.objects.restrict(user, "change").filter(
        status="pending", order__status="active"
    )
    if not user.is_superuser:
        tasks = tasks.filter(
            Q(assigned_user=user)
            | Q(assigned_user__isnull=True, assigned_group__in=user.groups.all())
        )
    return tasks.filter(order__in=MaintenanceOrder.objects.restrict(user, "view")).exclude(
        kind__in=["approval", "countersign"], order__requester=user,
    )


def personal_todo_tasks(user, tasks=None):
    """Tasks assigned to the current user or one of their groups."""
    tasks = actionable_tasks(user) if tasks is None else tasks
    return tasks.filter(
        Q(assigned_user=user)
        | Q(assigned_user__isnull=True, assigned_group__in=user.groups.all())
    )


def presentation(
    order, user, with_details=True, inbox=None, history_limit=50, materials_only=False,
):
    if materials_only:
        current = order.tasks.restrict(user, "view").filter(status="pending").first()
        step = order.steps_snapshot[current.position] if current else {}
        summary = {
            "current": current,
            "can_act": bool(current and actionable_tasks(user).filter(pk=current.pk).exists()),
            "required_materials": {"order"} if order.status == "draft" else {
                key for key, _ in step.get("required", [])
            },
        }
    else:
        inbox = inbox or InboxPresentation([order], user, actionable_tasks(user))
        summary = inbox.summary(order)
    if not with_details:
        return summary
    materials = []
    scan_materials = {
        material for node in order.steps_snapshot
        for material in ([node["scan_material"]] if node.get("scan_material") else [])
        + node.get("scan_materials", [])
    }
    node = order.steps_snapshot[order.current_position] if order.steps_snapshot else {}
    required_states = dict(node.get("required", []))
    documents = list(
        order.documents.restrict(user, "view").filter(removed_at__isnull=True)
        .exclude(kind="billing")
        .select_related("source_document", "replaces_document", "order")
        .order_by("material", "-version")
    )
    visible_ids = {doc.pk for doc in documents}
    replaced_ids = set(order.documents.exclude(replaces_document_id=None).values_list(
        "replaces_document_id", flat=True
    ))
    historical_materials = tuple(
        (key, name) for key, name in RETIRED_MATERIALS
        if any(doc.material == key for doc in documents)
    )
    for key, name in MATERIALS + historical_materials:
        versions = [doc for doc in documents if doc.material == key]
        source = next((doc for doc in versions
                       if doc.kind != "scan" and doc.pk not in replaced_ids), None)
        form_document = next((doc for doc in versions if doc.kind == "form"), None)
        attachments = [doc for doc in versions
                       if doc.kind == "attachment" and doc.pk not in replaced_ids]
        scans = [doc for doc in versions if doc.kind == "scan" and doc.pk not in replaced_ids]
        all_scan_sources = (
            list(services.material_scan_sources(order, key))
            if key in scan_materials
            else []
        )
        scan_sources = [doc for doc in all_scan_sources if doc.pk in visible_ids]
        # Correction requires fresh evidence, but previous uploads remain browsable
        # and available as the targets of an explicit revision upload.
        display_scan_sources = (
            [doc for doc in services.material_scan_candidates(order, key) if doc.pk in visible_ids]
            if key in scan_materials and node.get("key") == scan_fill_key(key)
            else scan_sources
        )
        for doc in display_scan_sources:
            doc.order = order
        if key in scan_materials:
            for report in {doc.pk: doc for doc in [*scan_sources, *display_scan_sources]}.values():
                report.sealed_scan = next(
                    (
                        scan
                        for scan in scans
                        if scan.source_document_id == report.pk and scan.is_sealed
                    ),
                    None,
                )
        can_fill = False
        if key in COMPANY_FORMS and not materials_only:
            try:
                services.check_company_form_access(order, user, key)
            except (PermissionDenied, ValidationError):
                pass
            else:
                can_fill = True
        can_upload_now = (
            node.get("key") in {scan_fill_key(key), f"{key}_sealed_upload"}
            if key in scan_materials
            else (
                key != "order"
                and (node.get("kind") == "submission" or node.get("key") == "payment_eip")
            )
            or required_states.get(key) in {"scan", "sealed"}
        )
        if key in {"notice", "invoice"}:
            can_upload_now = node.get("key") == "payment_plan"
        can_upload_now = can_upload_now and key in dict(MATERIALS) and key not in COMPANY_FORMS
        current = summary["current"]
        stage_upload_required = bool(
            current and key in summary["required_materials"] and can_upload_now
        )
        stage_uploads = [
            doc for doc in versions
            if stage_upload_required and doc.uploaded_task_id == current.pk
            and doc.kind != "form" and doc.file
            and doc.version > current.evidence.get("correction_baseline", {}).get(key, 0)
        ]
        missing_upload_sources = []
        stage_upload_count = len(stage_uploads)
        stage_upload_total = stage_upload_count
        stage_materials_adjusted = False
        if key in scan_materials:
            if node.get("key") == f"{key}_sealed_upload":
                source_ids = {doc.pk for doc in scan_sources}
                stage_uploads = [
                    doc for doc in stage_uploads
                    if doc.is_sealed and doc.source_document_id in source_ids
                ]
                # Match the same current-original evidence accepted by workflow validation.
                uploaded_sources = {
                    doc.pk for doc in scan_sources if doc.sealed_scan and doc.sealed_scan.file
                }
                missing_upload_sources = [
                    doc for doc in scan_sources if doc.pk not in uploaded_sources
                ]
                stage_upload_count = len(uploaded_sources)
                stage_upload_total = len(all_scan_sources)
                stage_upload_complete = (
                    bool(stage_uploads) and bool(source_ids)
                    and len(scan_sources) == len(all_scan_sources)
                    and source_ids <= uploaded_sources
                )
            else:
                stage_uploads = [
                    doc for doc in stage_uploads if not doc.source_document_id and not doc.is_sealed
                ]
                stage_upload_complete = bool(stage_uploads)
                stage_materials_adjusted = bool(
                    stage_upload_required and scan_sources
                    and services.material_set_adjusted(order, current, key)
                )
                if stage_materials_adjusted:
                    stage_upload_complete = all(doc.file for doc in scan_sources)
                    stage_upload_count = len(scan_sources)
        else:
            stage_upload_complete = bool(stage_uploads)
        materials.append(
            {
                "key": key,
                "name": name,
                "requires_filling": key in COMPANY_FORMS,
                "required_now": key in summary["required_materials"],
                "can_upload_now": can_upload_now,
                "stage_upload_required": stage_upload_required,
                "stage_upload_complete": stage_upload_complete,
                "stage_materials_adjusted": stage_materials_adjusted,
                "stage_upload_document": stage_uploads[0] if stage_uploads else None,
                "stage_upload_count": stage_upload_count,
                "stage_upload_total": stage_upload_total,
                "missing_upload_sources": missing_upload_sources,
                "current_file": (display_scan_sources[0] if display_scan_sources else None)
                or (attachments[0] if attachments else None)
                or form_document
                or source,
                "document": versions[0] if versions else None,
                "versions": versions,
                "form_document": form_document,
                "can_print_now": bool(
                    order.status == "active" and form_document and form_document.electronic_seal
                    and (key, node.get("key")) in {
                        ("order", "acceptance_apply"), ("acceptance", "payment_plan"),
                    }
                ),
                "form_versions": [doc for doc in versions if doc.kind == "form"],
                "can_fill": can_fill,
                "attachments": attachments,
                "attachment": attachments[0] if attachments else None,
                "scans": scans,
                "scanned_material": key in scan_materials,
                "scan_sources": display_scan_sources,
                "can_upload_scans": node.get("key") == scan_fill_key(key),
                "can_seal_scans": node.get("key") == f"{key}_sealed_upload",
                "scan_upload_form": MaintenanceScanUploadForm(
                    auto_id=f"id_{key}_batch_%s", material_name=name
                )
                if key in scan_materials and not materials_only
                else None,
                "scan_source": source
                if source and not (key == "report" and source.kind == "form")
                else None,
                "upload_form": MaintenanceDocumentForm(
                    initial={"material": key}, auto_id=f"id_{key}_%s"
                ) if not materials_only else None,
                "scan_form": MaintenanceDocumentForm(
                    scan=True,
                    initial={"material": key, "source_document_id": source.pk if source else None},
                    auto_id=f"id_{key}_scan_%s",
                ) if not materials_only else None,
            }
        )
    if materials_only:
        return {**summary, "materials": materials}
    document_groups = [
        {"key": "electronic", "name": "电子版", "materials": []},
        {"key": "scan", "name": "扫描件", "materials": []},
    ]
    for material in materials:
        key = material["key"]
        categories = document_groups[:1] if key in COMPANY_FORMS else document_groups[1:]
        for group in categories:
            category = group["key"]
            electronic = category == "electronic"
            sealed = category == "sealed"
            required = material["required_now"] and (
                not (electronic or sealed)
                or (required_states.get(key) == "sealed") == sealed
            )
            current_file = material["current_file"]
            if electronic:
                current_file = material["form_document"]
            elif sealed:
                current_file = next((
                    doc for doc in material["scans"]
                    if doc.is_sealed and material["form_document"]
                    and doc.source_document_id == material["form_document"].pk
                ), None)
            group["materials"].append({
                **material,
                "category": category,
                "category_label": group["name"],
                "review_key": f"{key}-sealed" if sealed else key,
                "required_now": required,
                "requires_filling": electronic,
                "can_fill": electronic and material["can_fill"],
                "can_upload_now": not electronic and material["can_upload_now"],
                "stage_upload_required": required and material["stage_upload_required"],
                "current_file": current_file,
                "file_count": len(material["scan_sources"]) if material["scanned_material"]
                else len(material["attachments"]),
            })
    document_cards = [item for group in document_groups for item in group["materials"]]
    current_document_groups = [
        {**group, "materials": [item for item in group["materials"]
                               if item["required_now"] or item["can_print_now"]]}
        for group in document_groups
    ]
    current_document_groups = [group for group in current_document_groups if group["materials"]]
    links = []
    for link in order.links.restrict(user, "view").select_related("object_type"):
        model = link.object_type.model_class()
        if (
            model
            and hasattr(model.objects, "restrict")
            and model.objects.restrict(user, "view").filter(pk=link.object_id).exists()
        ):
            links.append(link)
    payment_contract_stage = bool(node.get("payment_contract_stage"))
    payment_data = payment_context(order, user) if payment_contract_stage else {}
    return {
        **summary,
        **payment_data,
        **order_object_links(order, user),
        "has_stage_operations": bool(
            payment_contract_stage
            or node.get("kind") == "external"
            or node.get("key") in TASK_SIGNING_NODES
        ),
        "payment_contract_stage": payment_contract_stage,
        "payment_application": node.get("key") == "payment_eip",
        "can_mobile_sign": bool(
            summary["can_act"]
            and summary["current"]
            and order.steps_snapshot[summary["current"].position]["key"] in TASK_SIGNING_NODES
            and (node.get("key") not in {"order_correction"} or user.pk == order.requester_id)
        ),
        "applicant_signature_required": node.get("key") in {"order_correction"},
        "order_correction": node.get("key") == "order_correction",
        "mobile_signature": summary["current"]
        .signatures.filter(signer=user, cancelled=False)
        .order_by("-pk")
        .first()
        if summary["current"] and summary["can_act"]
        else None,
        **inbox.history(order, history_limit),
        "approval_diagram": inbox.approval_diagram(order),
        "materials": materials,
        "document_groups": document_groups,
        "current_document_groups": current_document_groups,
        "document_cards": document_cards,
        "required_company_forms": [
            item for item in materials if item["key"] in COMPANY_FORMS and item["required_now"]
        ],
        "other_company_forms": [
            item for item in materials if item["key"] in COMPANY_FORMS and not item["required_now"]
        ],
        "required_attachments": [
            item for item in materials if item["key"] != "order" and item["required_now"]
        ],
        "other_attachments": [
            item for item in materials if item["key"] != "order" and not item["required_now"]
        ],
        "links": links,
        "following": order.followers.filter(pk=user.pk).exists(),
        "can_change": MaintenanceOrder.objects.restrict(user, "change")
        .filter(pk=order.pk)
        .exists(),
    }


class MaintenanceFlowDefinitionView(LoginRequiredMixin, View):
    """Read the current process directly from the executable definition."""

    def get(self, request):
        if not (
            request.user.has_perm("netbox_oa.view_maintenanceorder")
            or request.user.has_perm("netbox_oa.view_maintenancepolicy")
        ):
            raise PermissionDenied()
        policies = MaintenancePolicy.objects.restrict(request.user, "view").prefetch_related(
            "roles__group"
        )
        policy_id = request.GET.get("policy", "")
        policy = get_object_or_404(policies, pk=policy_id) if policy_id.isdecimal() else None
        if policy_id and policy is None:
            raise Http404("流程配置编号无效")
        groups = {role.role: role.group for role in policy.roles.all()} if policy else {}
        sla = policy.sla_hours if policy else MaintenancePolicy._meta.get_field("sla_hours").default
        steps = build_steps({key: group.pk for key, group in groups.items()}, sla)
        names = {step["key"]: step["name"] for step in steps}
        kinds = {
            "submission": "材料提交",
            "approval": "内部审批",
            "action": "办理确认",
            "external": "外部结果登记",
        }
        states = {
            "electronic": "电子版",
            "scan": "扫描版",
            "sealed": "扫描版",
        }
        roles = dict(ROLES) | {"owner": "线路主管（工单负责人）", "requester": "发起人"}
        payments = dict(MaintenanceOrder._meta.get_field("payment_status").choices)
        stages = []
        normal_position = 0
        for key, name in STAGES:
            nodes = []
            for step in steps:
                if step["stage"] != key:
                    continue
                if not step.get("return_only"):
                    normal_position += 1
                rules = []
                step_key = step["key"]
                if step_key in {"order_correction"}:
                    rules.append("填写并保存维护服务工单，核对外购合同、服务单位及服务省市")
                    rules.append("必须由申请人本人完成手写签名后提交；表单修订后须重新签名")
                elif step_key == "acceptance_apply":
                    rules.append("填写工单服务验收单，基本信息自动带入且只读，选择验收结果并填写结算金额")
                    rules.append(
                        "至少上传一份服务报告和一份服务结算单扫描件，两类附件均无需用印；"
                        "支持多选或拖入 PDF、PNG、JPEG 文件，每份不超过 20 MB"
                    )
                    rules.append("退回后修订验收单并更新两类附件，再重新提交复核")
                    rules.append("完成本环节手写签名后才能提交，签名绑定当前验收单版本")
                elif step_key in ACCEPTANCE_SIGNING_NODES:
                    rules.append("核对验收单及全部扫描附件，完成本环节手写签名（自动记录日期）")
                    rules.append("签名绑定本次验收单版本；修订后须重新签名，审批期间不能修改材料")
                elif step_key in {"order_review", "order_final"}:
                    rules.append("核对工单内容，完成本人手写签名后同意批转；签名绑定当前工单版本")
                elif step_key in {"payment_plan", "payment_review"}:
                    rules.append(
                        "点击付款信息进入外购合同详情页，由工单发起人维护付款信息后提交"
                        if step_key == "payment_plan"
                        else "点击付款信息进入外购合同详情页，核对付款信息后审核"
                    )
                    rules.append(
                        "上传付款通知书、电子发票，材料齐备后提交部门审核"
                        if step_key == "payment_plan"
                        else "查阅付款通知书、电子发票；需修改时退回生成付款"
                    )
                elif step.get("scan_material"):
                    material_key = step["scan_material"]
                    material_name = dict(MATERIALS)[material_key]
                    if step_key == f"{material_key}_sealed_upload":
                        rules.append("逐份上传对应本次审批原件的用印后扫描件，全部齐备后方可提交")
                        rules.append("本环节只能维护用印件，审批原件及历史版本保留查阅")
                    elif step_key == f"{material_key}_seal":
                        rules.append("按本次审批通过的材料办理用印，完成后交由发起人上传用印后材料")
                    else:
                        rules.append(
                            f"审核已签字的系统{material_name}表单，无需上传用印前扫描件；"
                            "修改材料须先退回工单修正"
                        )
                if step_key in {"order_senior_seal", "acceptance_company"}:
                    rules.append("审批通过后自动在本次电子表单上加盖公章和审批日期")
                if step["kind"] == "external":
                    rules.append("必填外部付款申请单号，由经办人登记申请结果")
                nodes.append(
                    {
                        **step,
                        "position": "修正" if step.get("return_only") else normal_position,
                        "kind_label": kinds[step["kind"]],
                        "role_label": roles[step["role"]],
                        "group": groups.get(step["role"]),
                        "materials": [
                            f"{dict(MATERIALS)[material]} · "
                            + (
                                "扫描版（可多份）"
                                if material in {"report", "settlement"} and state == "scan"
                                else "扫描版（用印后）"
                                if state == "sealed"
                                else states[state]
                            )
                            for material, state in step["required"]
                        ],
                        "return_label": names.get(step["return_to"], "")
                        if step["kind"] in {"approval", "external", "countersign"}
                        else "",
                        "payment_label": payments.get(step["payment"], ""),
                        "completion_label": (
                            "工单完成；付款申请已提交"
                            if step["key"] == "payment_eip"
                            else "重新进入工单复核"
                            if step.get("return_only")
                            else "进入下一环节"
                        ),
                        "rules": rules,
                    }
                )
            stage_summary = {
                "ordering": (
                    "申请人填写工单并签名，经复核、部门审核后直接进行工单公司审批，"
                    "公司审批通过后自动加盖电子公章和日期，进入工单验收。"
                ),
                "acceptance": (
                    "填写验收单并上传服务报告、服务结算单，申请人、复核人和部门审核人签名后，"
                    "公司审批通过后自动加盖电子公章和日期，进入付款申请。"
                ),
                "payment": "在合同中维护付款信息，经部门审核后登记外部付款申请。",
            }
            nodes.sort(key=lambda node: bool(node.get("return_only")))
            stages.append(
                {
                    "key": key,
                    "name": name,
                    "nodes": nodes,
                    "summary": stage_summary[key],
                    "normal_count": sum(not node.get("return_only") for node in nodes),
                    "return_count": sum(bool(node.get("return_only")) for node in nodes),
                }
            )
        return render(
            request,
            "netbox_oa/maintenance/flow_definition.html",
            {
                "stages": stages,
                "flow_name": FLOW_NAME,
                "diagram": build_diagram(stages),
                "node_count": sum(not step.get("return_only") for step in steps),
                "correction_count": sum(bool(step.get("return_only")) for step in steps),
                "stage_count": len(stages),
                "policies": policies,
                "policy": policy,
                "sla_hours": sla,
                "roles": [
                    {"name": name, "group": groups.get(key)}
                    for key, name in ROLES
                    if any(step["role"] == key for step in steps)
                ],
            },
        )


GUIDE_COLLAPSED_CONFIG = "plugins.netbox_oa.workbench.guide_collapsed"


class MaintenanceGuidePreferenceView(LoginRequiredMixin, View):
    def post(self, request):
        collapsed = request.POST.get("collapsed")
        if collapsed not in {"true", "false"}:
            return JsonResponse({"error": "无效的收起状态。"}, status=400)
        request.user.config.set(GUIDE_COLLAPSED_CONFIG, collapsed == "true", commit=True)
        return JsonResponse({"collapsed": collapsed == "true"})


class MaintenanceWorkbenchView(LoginRequiredMixin, View):
    def get(self, request, pk=None, action_form=None, action_error="", status=200):
        orders = MaintenanceOrder.objects.restrict(request.user, "view").select_related(
            "owner", "requester", "policy"
        )
        tasks = actionable_tasks(request.user)
        assigned = Q(assigned_user=request.user) | Q(
            assigned_user__isnull=True, assigned_group__in=request.user.groups.all()
        )
        personal_tasks = personal_todo_tasks(request.user, tasks)
        # Read the frozen workflow key rather than relying on editable task names.
        pending_payment_ids = [
            order_id for order_id, position, steps in orders.filter(
                status="active", stage="payment", requester=request.user
            ).values_list("pk", "current_position", "steps_snapshot")
            if 0 <= position < len(steps)
            and steps[position].get("key") in {"payment_plan", "payment_eip"}
        ]
        tab = request.GET.get("tab", "todo")
        if tab == "mine":
            tab = "done"
        if tab not in {"todo", "pending_payment", "delegate", "done", "following", "all"}:
            tab = "todo"
        drafts = MaintenanceOrder.objects.restrict(request.user, "change").filter(
            status="draft", requester=request.user
        )
        completed = MaintenanceTask.objects.restrict(request.user, "view").filter(
            status="completed", decided_by=request.user
        )
        groups = {
            "todo": orders.filter(
                Q(pk__in=personal_tasks.values("order_id")) | Q(pk__in=drafts.values("pk"))
            ).exclude(pk__in=pending_payment_ids),
            "pending_payment": orders.filter(pk__in=pending_payment_ids, requester=request.user),
            "delegate": orders.filter(
                pk__in=tasks.exclude(assigned).values("order_id")
            ).exclude(pk__in=personal_tasks.values("order_id")),
            "done": orders.filter(
                Q(requester=request.user) | Q(pk__in=completed.values("order_id"))
            ).exclude(status="draft").exclude(pk__in=personal_tasks.values("order_id")),
            "following": orders.filter(followers=request.user),
            "all": orders,
        }
        filtered = groups[tab]
        if pk:
            selected = get_object_or_404(orders, pk=pk)
        else:
            selected = None
        stage = request.GET.get("stage", "")
        if stage in dict(STAGES):
            filtered = filtered.filter(stage=stage)
        query = request.GET.get("q", "").strip()
        if query:
            filtered = filtered.filter(
                Q(number__icontains=query)
                | Q(supplier__icontains=query)
                | Q(contract_number__icontains=query)
            )
        node = request.GET.get("node", "").strip()
        if node:
            matching = MaintenanceTask.objects.restrict(request.user, "view").filter(
                status="pending", name__icontains=node
            )
            filtered = filtered.filter(pk__in=matching.values("order_id"))
        if request.GET.get("sort") == "waiting":
            pending = MaintenanceTask.objects.restrict(request.user, "view").filter(
                order_id=OuterRef("pk"), status="pending"
            )
            filtered = filtered.annotate(
                waiting_since=Subquery(pending.values("created")[:1])
            ).order_by("waiting_since", "pk")
        rows = list(filtered[:100])
        if not selected:
            selected_id = request.GET.get("selected", "")
            if selected_id.isdecimal():
                if request.GET.get("restore") == "1":
                    selected = filtered.filter(pk=selected_id).first()
                    if selected is None:
                        selected = rows[0] if rows else None
                else:
                    selected = get_object_or_404(orders, pk=selected_id)
            else:
                selected = rows[0] if rows else None
        display_orders = {order.pk: order for order in rows}
        if selected:
            display_orders[selected.pk] = selected
        inbox = InboxPresentation(list(display_orders.values()), request.user, tasks)
        try:
            history_limit = max(50, int(request.GET.get("history", "50")))
        except (ValueError, TypeError):
            history_limit = 50
        # Bound user input by the number of visible records, not an arbitrary truncation.
        if selected and history_limit > 50:
            history_limit = min(history_limit, inbox.events.filter(order=selected).count() + 50)
        query_params = request.GET.copy()
        query_params["tab"] = tab
        for key in ("selected", "history", "panel", "pane", "restore"):
            query_params.pop(key, None)
        query_params.pop("ui", None)
        base_url = reverse("plugins:netbox_oa:maintenance_workbench_v2")
        list_url = f"{base_url}?{query_params.urlencode()}"
        detail_params = query_params.copy()
        if selected:
            detail_params["selected"] = selected.pk
        detail_url = f"{base_url}?{detail_params.urlencode()}"
        detail_params["history"] = history_limit + 50
        row_data = [
            presentation(order, request.user, with_details=False, inbox=inbox) for order in rows
        ]
        for row in row_data:
            params = query_params.copy()
            params["selected"] = row["order"].pk
            row["url"] = f"{base_url}?{params.urlencode()}"
        tabs = []
        for key, label in (
            ("todo", "待办"),
            ("pending_payment", "待付款"),
            ("delegate", "代办"),
            ("done", "已办"),
            ("following", "关注"),
            ("all", "全部"),
        ):
            if key == "all" and not request.user.is_superuser:
                continue
            params = query_params.copy()
            params["tab"] = key
            tabs.append({
                "key": key, "label": label, "count": groups[key].count(),
                "url": f"{base_url}?{params.urlencode()}",
            })
        now = timezone.now()
        warning = sum(
            now < deadline <= now + timedelta(hours=hours)
            for deadline, hours in personal_tasks.values_list("deadline", "order__warning_hours")
        )
        month_start = timezone.localtime(now).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        context = {
            "rows": row_data,
            "tabs": tabs,
            "list_url": list_url,
            "detail_url": detail_url,
            "history_url": f"{base_url}?{detail_params.urlencode()}",
            "return_query": query_params.urlencode(),
            "outside_list": bool(selected and not filtered.filter(pk=selected.pk).exists()),
            "mobile_detail": bool(pk or request.GET.get("selected")),
            "action_error": action_error,
            "panel_id": request.GET.get("panel", ""),
            "panel_pane": request.GET.get("pane", "files"),
            "routing_form": MaintenanceActionForm(
                action_form.data if action_form is not None and action_form.is_bound else None,
                auto_id="id_routing_%s",
            ),
            "tab": tab,
            "stage_choices": STAGES,
            "stage_filter": stage,
            "q": query,
            "node_filter": node,
            "stats": {
                "todo": personal_tasks.count(),
                "warning": warning,
                "overdue": personal_tasks.filter(deadline__lte=now).count(),
                "active": orders.filter(status="active").count(),
                "completed": orders.filter(
                    status="completed", completed_at__gte=month_start
                ).count(),
            },
            "detail": presentation(
                selected,
                request.user,
                inbox=inbox,
                history_limit=history_limit,
            )
            if selected
            else None,
            "action_form": action_form
            if action_form is not None
            else MaintenanceActionForm(order=selected, actor=request.user),
            "total": filtered.count(),
        }
        context["workbench_url"] = base_url
        context["guide_collapsed"] = getattr(request.user, "config", {}).get(
            GUIDE_COLLAPSED_CONFIG, False
        ) is True
        context["guide"] = handling_guide(context["detail"])
        response = render(
            request, "netbox_oa/maintenance/workbench_v2.html", context, status=status
        )
        return response


def order_form_response(request, form, order=None):
    from .models import MaintenanceSignature

    document = order.documents.filter(material="order", kind="form").first() if order else None
    current = (
        order.tasks.filter(status="pending").first() if order and order.status == "active" else None
    )
    signing_form = (
        not order
        or order.status == "draft"
        or bool(current and order.steps_snapshot[current.position]["key"] == "order_correction")
    )
    signature = None
    if document and signing_form:
        signature = (
            MaintenanceSignature.objects.filter(
                document=document,
                signer=request.user,
                task=current,
                cancelled=False,
                submitted_at__isnull=True,
            )
            .order_by("-pk")
            .first()
        )
    if form.is_bound and form.errors:
        signature = None
    return render(
        request,
        "netbox_oa/maintenance/order_create.html"
        if signing_form
        else "netbox_oa/maintenance/form.html",
        {
            "title": "新增维护服务工单" if not order else "修改维护工单资料",
            "object": order or form.instance,
            "form": form,
            "return_url": (
                workbench_return_url(request, order.pk) if order
                else reverse("plugins:netbox_oa:maintenance_workbench_v2")
            ),
            "return_query": workbench_params(request).urlencode(),
            "signing_form": signing_form,
            "signature_save_url": request.get_full_path(),
            "expected_document_id": request.POST.get(
                "expected_document_id", document.pk if document else ""
            ),
            "correction_form": bool(current and signing_form),
            "can_add_another": (
                (not order or order.status == "draft")
                and request.user.has_perm("netbox_oa.add_maintenanceorder")
            ),
            "detail": {"current": current, "mobile_signature": signature},
        },
    )


def order_form_result(request, order):
    from . import maintenance_signatures as signing
    from .maintenance_signature_views import signature_payload

    detail_url = workbench_return_url(request, order.pk)
    if "_signature" in request.POST:
        if order.status == "draft":
            signature = signing.create_draft_session(pk=order.pk, actor=request.user)
        else:
            signature = signing.create_session(
                pk=order.tasks.get(status="pending").pk, actor=request.user
            )
        result = signature_payload(signature, request)
        result.update(
            form_action=(
                reverse("plugins:netbox_oa:maintenanceorder_edit", args=[order.pk])
            ),
            document_id=signature.document_id,
        )
        return JsonResponse(result)
    if "_submit" in request.POST:
        if order.status == "draft":
            services.start_order(pk=order.pk, actor=request.user)
        else:
            services.complete_task(pk=order.tasks.get(status="pending").pk, actor=request.user)
        messages.success(request, "已提交审批。")
        return redirect(detail_url)
    if "_addanother" in request.POST:
        messages.success(request, f"已保存草稿 {order.number}。")
        return redirect(reverse("plugins:netbox_oa:maintenanceorder_add"))
    if order.status == "draft":
        messages.success(request, "草稿已保存，可继续签名并提交审批。")
        params = QueryDict(mutable=True)
        params["return_query"] = workbench_params(request).urlencode()
        return redirect(
            reverse("plugins:netbox_oa:maintenanceorder_edit", args=[order.pk])
            + (f"?{params.urlencode()}" if params["return_query"] else "")
        )
    return redirect(detail_url)


class MaintenanceOrderCreateView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.has_perm("netbox_oa.add_maintenanceorder"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return order_form_response(request, MaintenanceOrderForm(actor=request.user))

    def post(self, request):
        form = MaintenanceOrderForm(request.POST, actor=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    order = services.create_order(actor=request.user, **form.cleaned_data)
                    return order_form_result(request, order)
            except ValidationError as exc:
                form.add_error(None, exc)
        if "_signature" in request.POST:
            return JsonResponse({"error": form.errors.as_text()}, status=400)
        return order_form_response(request, form)


class MaintenancePolicyView(LoginRequiredMixin, View):
    def get(self, request, pk=None):
        policy = (
            get_object_or_404(MaintenancePolicy.objects.restrict(request.user, "view"), pk=pk)
            if pk
            else None
        )
        if policy:
            _allowed(MaintenancePolicy, request.user, "change", pk)
        elif not request.user.has_perm("netbox_oa.add_maintenancepolicy"):
            raise PermissionDenied()
        return render(
            request,
            "netbox_oa/maintenance/form.html",
            {
                "title": "维护流程角色与时限配置",
                "form": MaintenancePolicyForm(policy=policy),
                "policies": MaintenancePolicy.objects.restrict(request.user, "view"),
            },
        )

    def post(self, request, pk=None):
        form = MaintenancePolicyForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data.copy()
            from .maintenance_flow import ROLES

            groups = {role: data.pop(role).pk for role, _ in ROLES}
            try:
                policy = services.save_policy(actor=request.user, pk=pk, groups=groups, **data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "流程角色配置已保存。")
                return redirect(policy)
        return render(
            request, "netbox_oa/maintenance/form.html", {"title": "维护流程配置", "form": form}
        )


class MaintenanceOrderEditView(LoginRequiredMixin, View):
    def get(self, request, pk):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "change"), pk=pk)
        return order_form_response(
            request, MaintenanceOrderEditForm(instance=order, actor=request.user), order
        )

    def post(self, request, pk):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "change"), pk=pk)
        form = MaintenanceOrderEditForm(request.POST, instance=order, actor=request.user)
        if form.is_valid():
            try:
                with transaction.atomic():
                    order = services.update_order(
                        pk=pk,
                        actor=request.user,
                        expected_document_id=request.POST.get("expected_document_id") or None,
                        audit=not ("_signature" in request.POST or "_submit" in request.POST),
                        **form.cleaned_data,
                    )
                    return order_form_result(request, order)
            except ValidationError as exc:
                form.add_error(None, exc)
        if "_signature" in request.POST:
            return JsonResponse({"error": form.errors.as_text()}, status=400)
        return order_form_response(request, form, order)


def workbench_params(request):
    source = QueryDict(request.POST.get("return_query", request.GET.get("return_query", "")))
    params = QueryDict(mutable=True)
    for key in (
        "tab", "q", "stage", "node", "sort", "history",
    ):
        if key in source:
            params[key] = source[key]
    return params


def workbench_return_url(request, order_id):
    params = workbench_params(request)
    params["selected"] = order_id
    params["restore"] = "1"
    return f"{reverse('plugins:netbox_oa:maintenance_workbench_v2')}?{params.urlencode()}"


def workbench_redirect(request, order_id):
    params = workbench_params(request)
    params["selected"] = order_id
    params["restore"] = "1"
    panel = request.POST.get("return_panel", "")
    if panel in {f"material-{key}" for key, _ in MATERIALS}:
        params["panel"] = panel
    params.pop("ui", None)
    return redirect(
        f"{reverse('plugins:netbox_oa:maintenance_workbench_v2')}?{params.urlencode()}"
    )


def task_success_redirect(request, order_id):
    """Retain the current category and select the first remaining order if needed."""
    return redirect(workbench_return_url(request, order_id))


def workbench_error(request, order_id, error, form=None, status=400):
    request.GET = workbench_params(request)
    if request.POST.get("return_panel") == "routing":
        request.GET["panel"] = "routing"
    return MaintenanceWorkbenchView().get(
        request, pk=order_id, action_form=form, action_error=error, status=status
    )


class MaintenanceOrderActionView(LoginRequiredMixin, View):
    def post(self, request, pk, operation):
        handlers = {
            "start": services.start_order,
            "follow": services.follow_order,
            "withdraw": services.withdraw_order,
            "terminate": services.terminate_order,
        }
        if operation not in handlers:
            raise ValidationError("未知操作。")
        kwargs = (
            {"comment": request.POST.get("comment", "")}
            if operation in {"withdraw", "terminate"}
            else {}
        )
        try:
            handlers[operation](pk=pk, actor=request.user, **kwargs)
        except ValidationError as exc:
            return workbench_error(request, pk, "; ".join(exc.messages))
        return workbench_redirect(request, pk)


class MaintenanceTaskQuickCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        get_object_or_404(MaintenanceTask, pk=pk)
        try:
            services.quick_complete_task(pk=pk, actor=request.user)
        except PermissionDenied as exc:
            return JsonResponse(
                {"error": str(exc) if isinstance(exc, services.SelfApprovalDenied)
                 else "当前任务的办理权限或分配已变化，请刷新页面。"}, status=403,
            )
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        return JsonResponse({"success": True})


class MaintenanceTaskActionView(LoginRequiredMixin, View):
    def post(self, request, pk, operation):
        task = get_object_or_404(MaintenanceTask, pk=pk)
        form = MaintenanceActionForm(request.POST, order=task.order, actor=request.user)
        _allowed(MaintenanceTask, request.user, "change", pk)
        if form.is_valid():
            data = form.cleaned_data
            handlers = {
                "complete": services.complete_task,
                "return": services.return_task,
                "transfer": services.transfer_task,
                "countersign": services.countersign_task,
            }
            kwargs = {"comment": data["comment"]}
            if operation == "complete":
                kwargs = {
                    key: data[key]
                    for key in (
                        "comment",
                        "external_reference",
                    )
                }
            elif operation in {"transfer", "countersign"}:
                kwargs["user_id"] = data["user_id"].pk if data["user_id"] else None
            if operation not in handlers:
                return workbench_error(request, task.order_id, "未知任务操作。", form)
            else:
                try:
                    handlers[operation](pk=pk, actor=request.user, **kwargs)
                except ValidationError as exc:
                    return workbench_error(request, task.order_id, "; ".join(exc.messages), form)
                except PermissionDenied as exc:
                    return workbench_error(
                        request,
                        task.order_id,
                        str(exc) if isinstance(exc, services.SelfApprovalDenied)
                        else "当前任务的办理权限或分配已变化，请查看最新状态。",
                        form,
                        status=403,
                    )
                else:
                    result = {
                        "complete": "已同意。" if task.kind in {"approval", "countersign"}
                        else "本阶段办理完成。",
                        "return": "已退回补正。",
                        "transfer": "已转办。",
                        "countersign": "已加签。",
                    }[operation]
                    messages.success(
                        request, result,
                        extra_tags="oa-task-completed",
                    )
        else:
            return workbench_error(request, task.order_id, "请检查办理信息。", form)
        return task_success_redirect(request, task.order_id)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class MaintenanceMaterialUploadView(LoginRequiredMixin, View):
    def get(self, request, pk, material):
        return self.upload_page(request, pk, material)

    def post(self, request, pk, material):
        return self.upload_page(request, pk, material)

    def upload_page(self, request, pk, material):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        detail = presentation(order, request.user)
        item = next((item for item in detail["materials"] if item["key"] == material), None)
        if item is None:
            raise Http404("材料类型不存在。")
        if not (
            item["required_now"] and detail["can_act"] and item["can_upload_now"]
            and request.user.has_perm("netbox_oa.add_maintenancedocument")
        ):
            raise PermissionDenied("当前节点无权上传此材料。")
        batch = item["scanned_material"] and item["can_upload_scans"]
        node = order.steps_snapshot[order.current_position]
        scan = item["scanned_material"] or dict(node.get("required", [])).get(material) in {
            "scan", "sealed"
        }
        data = request.POST.copy() if request.method == "POST" else None
        if data is not None:
            # Material and document state come from the route and current node.
            data["material"] = material
            data["state"] = "scan" if scan else "electronic"
        requires_source = scan and not batch
        replacement = None
        replacement_id = request.GET.get("replace")
        if replacement_id:
            if not replacement_id.isdecimal():
                raise Http404("材料文件不存在。")
            replacement = get_object_or_404(
                services.material_maintenance_documents(order, detail["current"], material)
                .restrict(request.user, "view"), pk=replacement_id,
            )
            if data is not None:
                data["upload_mode"] = "replace"
                data.setlist("replaces_document_ids", [replacement.pk])
                if requires_source:
                    data.setlist("source_document_ids", [replacement.source_document_id])
        allow_revision = batch and material in {"report", "settlement"}
        revision_sources = list(
            services.material_scan_candidates(order, material).restrict(request.user, "view")
        ) if allow_revision else []
        sources = item["scan_sources"] if item["scanned_material"] else (
            [item["scan_source"]] if item["scan_source"] else []
        )
        next_source = next(
            (source for source in sources
             if item["can_seal_scans"] and not (
                 source.sealed_scan and source.sealed_scan.file
             )), sources[0] if sources else None,
        )
        choices = []
        for source in sources:
            label = f"{source.short_name}（原文件：{source.original_name}）"
            if item["can_seal_scans"]:
                label += (
                    "（已上传用印件）" if source.sealed_scan and source.sealed_scan.file
                    else "（待上传用印件）"
                )
            choices.append((source.pk, label))
        if data is not None and not data.getlist("source_document_ids"):
            data.setlist("source_document_ids", data.getlist("source_document_id"))
        form = MaintenanceMaterialUploadForm(
            data, request.FILES or None, material_name=item["name"], scan=scan,
            source_choices=choices, requires_source=requires_source,
            allow_revision=allow_revision and not replacement,
            multiple=material not in COMPANY_FORMS and not replacement,
            revision_choices=[(doc.pk, doc.short_name) for doc in (
                [replacement] if replacement else revision_sources
            )],
            initial={
                "source_document_ids": [replacement.source_document_id]
                if replacement and requires_source else [next_source.pk] if next_source else [],
                "upload_mode": "replace" if replacement else "new",
                "replaces_document_ids": [replacement.pk] if replacement else [],
            },
        )
        if data is not None and form.is_valid():
            try:
                if replacement and len(form.cleaned_data["file"]) != 1:
                    raise ValidationError("覆盖已有材料时每次只能上传一个文件。")
                if batch:
                    services.upload_material_scans(
                        pk=pk, actor=request.user, material=material,
                        files=form.cleaned_data["file"], note=form.cleaned_data["note"],
                        upload_mode=(
                            "replace" if replacement else
                            form.cleaned_data.get("upload_mode") if allow_revision else None
                        ),
                        replaces_document_ids=[replacement.pk] if replacement else
                        form.cleaned_data.get("replaces_document_ids") if allow_revision else None,
                    )
                else:
                    services.upload_document_files(
                        pk=pk, actor=request.user, material=material,
                        files=form.cleaned_data["file"], state="scan" if scan else "electronic",
                        note=form.cleaned_data["note"],
                        source_document_ids=form.cleaned_data["source_document_ids"]
                        if requires_source else None,
                        replaces_document_ids=[replacement.pk] if replacement else None,
                    )
            except ValidationError as exc:
                form.add_error(None, "; ".join(exc.messages))
            else:
                if request.GET.get("embed") == "1":
                    return render(request, "netbox_oa/maintenance/upload_complete.html")
                messages.success(request, "材料上传成功。")
                return redirect(
                    f"{reverse('plugins:netbox_oa:maintenance_review', args=[pk])}"
                    f"?material={material}"
                )
        return render(request, "netbox_oa/maintenance/material_upload.html", {
            "order": order, "material": item, "form": form, "batch": batch, "scan": scan,
            "allow_revision": allow_revision and not replacement,
            "multiple": material not in COMPANY_FORMS and not replacement,
            "replacement": replacement,
            "upload_base": "netbox_oa/maintenance/upload_embedded.html"
            if request.GET.get("embed") == "1" else "base/layout.html",
        })


class MaintenanceMaterialRemoveView(LoginRequiredMixin, View):
    def post(self, request, pk):
        document = get_object_or_404(MaintenanceDocument, pk=pk)
        try:
            services.remove_document(pk=pk, actor=request.user)
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        return JsonResponse({"removed": document.pk})


class MaintenanceUploadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if request.POST.get("scan_material"):
            material = request.POST["scan_material"]
            form = MaintenanceScanUploadForm(request.POST, request.FILES)
            if form.is_valid():
                try:
                    documents = services.upload_material_scans(
                        pk=pk,
                        actor=request.user,
                        files=form.cleaned_data["file"],
                        note=form.cleaned_data["note"],
                        material=material,
                    )
                except ValidationError as exc:
                    messages.error(request, "; ".join(exc.messages))
                else:
                    messages.success(request, f"已保存 {len(documents)} 份扫描版材料。")
            else:
                messages.error(request, form.errors.as_text())
            return workbench_redirect(request, pk)
        form = MaintenanceDocumentForm(
            request.POST,
            request.FILES,
            scan=request.POST.get("state") == "scan",
        )
        if form.is_valid():
            try:
                services.upload_document(pk=pk, actor=request.user, **form.cleaned_data)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(
                    request,
                    "扫描件已保存。"
                    if form.cleaned_data["state"] in {"scan", "sealed"}
                    else "附件新版本已保存。",
                )
        else:
            messages.error(request, str(form.errors.as_text()))
        return workbench_redirect(request, pk)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class MaintenanceReviewView(LoginRequiredMixin, View):
    read_only = False

    def get_order(self, request, pk, **kwargs):
        return get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)

    def get(self, request, pk, **kwargs):
        order = self.get_order(request, pk, **kwargs)
        detail = presentation(order, request.user, materials_only=True)
        groups = []
        for material in detail["materials"]:
            can_upload = bool(
                not self.read_only and material["required_now"] and detail["can_act"]
                and material["can_upload_now"]
                and request.user.has_perm("netbox_oa.add_maintenancedocument")
            )
            maintainable_ids = set(services.material_maintenance_documents(
                order, detail["current"], material["key"]
            ).restrict(request.user, "view").values_list("pk", flat=True)) if can_upload else set()
            documents = []
            for scan in material["scan_sources"]:
                documents.append(scan)
                if scan.sealed_scan:
                    documents.append(scan.sealed_scan)
            if material["form_document"] and material["form_document"] not in documents:
                documents.append(material["form_document"])
            if not documents and material["attachments"]:
                latest_task_id = material["attachments"][0].uploaded_task_id
                documents.extend(
                    doc for doc in material["attachments"] if doc.uploaded_task_id == latest_task_id
                )
            if not documents and material["current_file"]:
                documents.append(material["current_file"])
            documents.sort(key=lambda document: (
                0 if document.is_sealed else 1 if document.kind == "scan" else 2
            ))
            index_rows = {}
            for document in documents:
                if document.kind == "form":
                    row_key = 1 if material["key"] in COMPANY_FORMS else "form"
                else:
                    row_key = document.item_number or document.pk
                row = index_rows.setdefault(row_key, {
                    "label": "表单" if row_key == "form" else f"第 {row_key} 份",
                    "files": [],
                })
                label, icon = (
                    ("用印后", "mdi-stamper") if document.is_sealed else
                    ("电子版", "mdi-file-edit-outline") if document.kind == "form" else
                    ("扫描件", "mdi-file-image-outline")
                )
                row["files"].append({"document": document, "label": label, "icon": icon})
                if document.pk in maintainable_ids:
                    row["maintenance_document"] = document
                    row["can_remove"] = MaintenanceDocument.objects.restrict(
                        request.user, "delete"
                    ).filter(pk=document.pk).exists()
            ordered_rows = [row for key, row in sorted(
                index_rows.items(), key=lambda item: (item[0] == "form", str(item[0]).zfill(12))
            )]
            for row in ordered_rows:
                row["default_document"] = row["files"][0]["document"]
            groups.append({
                "key": material["key"], "name": material["name"], "documents": documents,
                "index_rows": ordered_rows,
                "default_document": ordered_rows[0]["default_document"] if ordered_rows else None,
                "item_count": 1 if material["key"] == "order" and documents else (
                    len(material["scan_sources"])
                    if material["scanned_material"] else len(documents)
                ),
                "can_upload": can_upload,
            })
        selected_key = request.GET.get("material") or next(
            (item["key"] for item in detail["materials"] if item["required_now"]), "order"
        )
        group = next((item for item in groups if item["key"] == selected_key), None)
        if group is None:
            raise Http404("材料类型不存在。")
        document_id = request.GET.get("document")
        selected = next(
            (item for item in group["documents"] if str(item.pk) == document_id), None
        ) if document_id else group["default_document"]
        category = request.GET.get("category")
        if not document_id and category in {"electronic", "sealed", "scan"}:
            selected = next((doc for doc in group["documents"] if (
                (category == "electronic" and doc.kind == "form")
                or (category == "sealed" and doc.is_sealed)
                or (category == "scan" and doc.kind != "form" and not doc.is_sealed)
            )), None)
        if document_id and selected is None:
            raise Http404("材料文件不存在或无权查看。")
        review_items = []
        for item_group in groups:
            for row in item_group["index_rows"]:
                for entry in row["files"]:
                    document = entry["document"]
                    suffix = Path(document.original_name).suffix.lower()
                    review_items.append({
                        "document": document,
                        "item_id": row["default_document"].pk,
                        "preferred": document.pk == row["default_document"].pk,
                        "group": item_group,
                        "label": f'{item_group["name"]} · {row["label"]} · {entry["label"]}',
                        "type": "form" if document.kind == "form" else (
                            "pdf" if suffix == ".pdf" else
                            "image" if suffix in {".png", ".jpg", ".jpeg"} else "download"
                        ),
                    })
        review_return_url = order.get_absolute_url()
        return render(request, "netbox_oa/maintenance/review.html", {
            "order": order, "groups": groups, "selected_group": group, "selected": selected,
            "review_items": review_items,
            "read_only": self.read_only,
            "review_return_url": review_return_url,
            "review_query": "",
        })


@method_decorator(xframe_options_sameorigin, name="dispatch")
class MaintenancePreviewView(LoginRequiredMixin, View):
    def get(self, request, pk):
        document = get_object_or_404(
            MaintenanceDocument.objects.restrict(request.user, "view"), pk=pk
        )
        _allowed(MaintenanceOrder, request.user, "view", document.order_id)
        content_type = {
            ".pdf": "application/pdf", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        }.get(Path(document.original_name).suffix.lower())
        if not document.file or not content_type:
            raise Http404("此文件不支持在线预览。")
        response = FileResponse(
            document.file.open("rb"), content_type=content_type,
            as_attachment=False, filename=document.display_name,
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class MaintenanceDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        document = get_object_or_404(
            MaintenanceDocument.objects.restrict(request.user, "view"), pk=pk
        )
        _allowed(MaintenanceOrder, request.user, "view", document.order_id)
        if document.kind == "form":
            return redirect("plugins:netbox_oa:maintenance_print", pk=document.pk)
        if not document.file:
            raise Http404("附件文件不存在。")
        response = FileResponse(
            document.file.open("rb"), as_attachment=True, filename=document.display_name
        )
        return response


class MaintenanceCompanyFormView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if kwargs["material"] not in COMPANY_FORMS:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk, material):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        try:
            services.check_company_form_access(order, request.user, material)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(workbench_return_url(request, order.pk))
        previous = (
            order.documents.restrict(request.user, "view")
            .filter(material=material, kind="form")
            .first()
        )
        initial = dict(previous.form_data) if previous else company_form_initial(order)
        initial["requested_at"] = parse_datetime(initial["requested_at"])
        form = MaintenanceCompanyForm(
            material=material, initial=initial, actor=request.user, order=order
        )
        return self.render_form(request, order, material, form)

    def post(self, request, pk, material):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        try:
            services.check_company_form_access(order, request.user, material)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(workbench_return_url(request, order.pk))
        form = MaintenanceCompanyForm(
            request.POST, material=material, actor=request.user, order=order
        )
        if form.is_valid():
            try:
                services.save_company_form(
                    pk=pk, actor=request.user, material=material, data=request.POST
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "表单已保存。")
                return redirect(workbench_return_url(request, order.pk))
        return self.render_form(request, order, material, form)

    def render_form(self, request, order, material, form):
        acceptance_billing = {}
        if material == "acceptance":
            from .maintenance_billing import billing_documents

            document = billing_documents(order).restrict(request.user, "view").first()
            acceptance_billing = document.billing_data if document else {}
        return render(
            request,
            "netbox_oa/maintenance/company_form.html",
            {
                "order": order,
                "material": material,
                "title": COMPANY_FORMS[material],
                "form": form,
                "object": order,
                "return_url": workbench_return_url(request, order.pk),
                "return_query": workbench_params(request).urlencode(),
                "acceptance_billing": acceptance_billing,
            },
        )


@method_decorator(xframe_options_sameorigin, name="dispatch")
class MaintenancePrintView(LoginRequiredMixin, View):
    def get(self, request, pk):
        document = get_object_or_404(
            MaintenanceDocument.objects.restrict(request.user, "view").select_related("order"),
            pk=pk,
            kind="form",
            material__in=PRINTABLE_FORMS,
        )
        _allowed(MaintenanceOrder, request.user, "view", document.order_id)
        data = document.form_data
        codes = {}
        if document.material in COMPANY_FORMS:
            from .maintenance_codes import print_codes

            codes = print_codes(document.order.number, request.build_absolute_uri(
                reverse("plugins:netbox_oa:maintenance_verify", args=[document.pk])
            ))
        signatures = {}
        if document.material == "order":
            from .models import MaintenanceSignature

            submitted = (
                MaintenanceSignature.objects.filter(
                    document=document,
                    task__isnull=True,
                    submitted_at__isnull=False,
                    cancelled=False,
                )
                .select_related("signer")
                .order_by("-pk")
                .first()
            )
            if submitted:
                signatures["applicant"] = {
                    "name": submitted.signer.get_full_name() or submitted.signer.get_username(),
                    "user_id": submitted.signer_id,
                    "date": timezone.localdate(submitted.submitted_at).isoformat(),
                    "image_id": submitted.pk,
                }
            evidence_ids = {document.pk, *document.derived_documents.values_list("pk", flat=True)}
            for task in document.order.tasks.restrict(request.user, "view").filter(
                status="completed"
            ):
                if (
                    isinstance(task.evidence.get("order"), int)
                    and task.evidence["order"] in evidence_ids
                ):
                    node = document.order.steps_snapshot[task.position]
                    if node["key"] in {
                        "order_correction",
                        "order_review",
                        "order_final",
                    }:
                        signatures[
                            "applicant" if node["key"] == "order_correction" else node["key"]
                        ] = task.evidence.get("signature", {})
        if document.material == "acceptance":
            for task in document.order.tasks.restrict(request.user, "view").filter(
                status="completed"
            ).order_by("pk"):
                if task.evidence.get("acceptance") == document.pk:
                    key = document.order.steps_snapshot[task.position]["key"]
                    if key in ACCEPTANCE_SIGNING_NODES:
                        signatures[key] = task.evidence.get("signature", {})
        print_return_url = document.order.get_absolute_url()
        response = render(
            request,
            "netbox_oa/maintenance/print.html",
            {
                "document": document,
                "print_return_url": print_return_url,
                **codes,
                "data": data,
                "requested_at": data.get("requested_at", ""),
                "service_items": SERVICE_ITEMS,
                "selected_service_types": service_type_values(data.get("service_type")),
                "priority_label": dict(MaintenanceOrder._meta.get_field("priority").choices).get(
                    data.get("priority", document.order.priority), "—"
                ),
                "signatures": signatures,
                "service_effect_label": service_effect_label(data.get("service_effect")),
                "acceptance_signatures": [
                    {"label": label, **signatures.get(key, {})}
                    for key, label in (("acceptance_apply", "验收人"),
                                       ("acceptance_review", "复核人"),
                                       ("acceptance_department", "部门审核"))
                ],
            },
        )
        return response


class MaintenancePrintEventView(LoginRequiredMixin, View):
    def post(self, request, pk):
        get_object_or_404(MaintenanceDocument.objects.restrict(request.user, "view"), pk=pk)
        try:
            event = services.record_form_print(pk=pk, actor=request.user)
        except ValidationError as exc:
            return JsonResponse({"error": "; ".join(exc.messages)}, status=400)
        return JsonResponse({"recorded": True, "event_id": event.pk})


@method_decorator(login_not_required, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class MaintenanceVerificationView(View):
    def get(self, request, pk):
        from .maintenance_verification import verification_context

        # Keep the existing URL so previously printed QR codes also work.
        document = get_object_or_404(
            MaintenanceDocument.objects.select_related(
                "order", "order__requester"
            ),
            pk=pk, material__in=COMPANY_FORMS, kind="form", removed_at__isnull=True,
        )
        if request.method == "GET":
            # Atomic increment without a changelog entry per scan; HEAD does not count.
            MaintenanceDocument.objects.filter(pk=document.pk).update(
                scan_count=F("scan_count") + 1
            )
            document.refresh_from_db(fields=["scan_count"])
        response = render(
            request, "netbox_oa/maintenance/verify.html", verification_context(document)
        )
        response["Referrer-Policy"] = "no-referrer"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


class MaintenanceLinkView(LoginRequiredMixin, View):
    def post(self, request, pk):
        form = MaintenanceLinkForm(request.POST)
        if form.is_valid():
            try:
                services.add_link(pk=pk, actor=request.user, **form.cleaned_data)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
        else:
            messages.error(request, str(form.errors.as_text()))
        return workbench_redirect(request, pk)
