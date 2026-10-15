from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import OuterRef, Q, Subquery
from django.http import FileResponse, Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View

from . import maintenance_services as services
from .maintenance_diagram import build_diagram
from .maintenance_flow import FLOW_NAME, MATERIALS, ROLES, STAGES, build_steps
from .maintenance_forms import (
    MaintenanceActionForm,
    MaintenanceCompanyForm,
    MaintenanceDocumentForm,
    MaintenanceLinkForm,
    MaintenanceOrderEditForm,
    MaintenanceOrderForm,
    MaintenancePolicyForm,
    MaintenanceScanUploadForm,
)
from .maintenance_materials import COMPANY_FORMS, SERVICE_ITEMS, company_form_initial
from .maintenance_payments import payment_context
from .maintenance_presentation import InboxPresentation
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
    return tasks.filter(order__in=MaintenanceOrder.objects.restrict(user, "view"))


def presentation(order, user, with_details=True, inbox=None, history_limit=50):
    inbox = inbox or InboxPresentation([order], user, actionable_tasks(user))
    summary = inbox.summary(order)
    if not with_details:
        return summary
    materials = []
    scan_materials = {
        "report" if node.get("report_scans") else node["scan_material"]
        for node in order.steps_snapshot
        if node.get("report_scans") or node.get("scan_material")
    }
    node = order.steps_snapshot[order.current_position] if order.steps_snapshot else {}
    required_states = dict(node.get("required", []))
    documents = list(
        order.documents.restrict(user, "view")
        .select_related("source_document")
        .order_by("material", "-version")
    )
    visible_ids = {doc.pk for doc in documents}
    for key, name in MATERIALS:
        versions = [doc for doc in documents if doc.material == key]
        source = next((doc for doc in versions if doc.kind != "scan"), None)
        form_document = next((doc for doc in versions if doc.kind == "form"), None)
        attachments = [doc for doc in versions if doc.kind == "attachment"]
        scans = [doc for doc in versions if doc.kind == "scan"]
        scan_sources = (
            [doc for doc in services.material_scan_sources(order, key) if doc.pk in visible_ids]
            if key in scan_materials
            else []
        )
        if key in scan_materials:
            for report in scan_sources:
                report.sealed_scan = next(
                    (
                        scan
                        for scan in scans
                        if scan.source_document_id == report.pk and scan.is_sealed
                    ),
                    None,
                )
        can_fill = False
        if key in COMPANY_FORMS:
            try:
                services.check_company_form_access(order, user, key)
            except (PermissionDenied, ValidationError):
                pass
            else:
                can_fill = True
        materials.append(
            {
                "key": key,
                "name": name,
                "required_now": key in summary["required_materials"],
                "can_upload_now": (
                    node.get("key") in {f"{key}_fill", f"{key}_seal"}
                    if key in scan_materials
                    else (
                        key != "order"
                        and (node.get("kind") == "submission" or node.get("finish_on_application"))
                    )
                    or node.get("key") == "invoice_check"
                    or required_states.get(key) in {"scan", "paper", "sealed"}
                ),
                "current_file": (scan_sources[0] if scan_sources else None)
                or (attachments[0] if attachments else None)
                or form_document
                or source,
                "document": versions[0] if versions else None,
                "versions": versions,
                "form_document": form_document,
                "form_versions": [doc for doc in versions if doc.kind == "form"],
                "can_fill": can_fill,
                "attachments": attachments,
                "attachment": attachments[0] if attachments else None,
                "scans": scans,
                "scanned_material": key in scan_materials,
                "scan_sources": scan_sources,
                "can_upload_scans": node.get("key") == f"{key}_fill",
                "can_seal_scans": node.get("key") == f"{key}_seal",
                "scan_upload_form": MaintenanceScanUploadForm(
                    auto_id=f"id_{key}_batch_%s", material_name=name
                )
                if key in scan_materials
                else None,
                "scan_source": source
                if source and not (key == "report" and source.kind == "form")
                else None,
                "upload_form": MaintenanceDocumentForm(
                    initial={"material": key}, auto_id=f"id_{key}_%s"
                ),
                "scan_form": MaintenanceDocumentForm(
                    scan=True,
                    initial={"material": key, "source_document_id": source.pk if source else None},
                    auto_id=f"id_{key}_scan_%s",
                ),
            }
        )
    links = []
    for link in order.links.restrict(user, "view").select_related("object_type"):
        model = link.object_type.model_class()
        if (
            model
            and hasattr(model.objects, "restrict")
            and model.objects.restrict(user, "view").filter(pk=link.object_id).exists()
        ):
            links.append(link)
    payment_contract_stage = node.get("key") in {"payment_plan", "payment_review"} or bool(
        node.get("contract_payment_review")
    )
    payment_data = payment_context(order, user) if payment_contract_stage else {}
    return {
        **summary,
        **payment_data,
        "has_stage_operations": bool(
            payment_contract_stage
            or summary["required_materials"]
            or node.get("kind") == "external"
            or node.get("key") in {"order_review", "order_final", "payment_plan"}
        ),
        "settlement_filling": bool(
            node.get("contract_payment") and node.get("key") == "settlement_fill"
        ),
        "payment_contract_stage": payment_contract_stage,
        "can_mobile_sign": bool(
            summary["can_act"]
            and summary["current"]
            and order.steps_snapshot[summary["current"].position]["key"]
            in {"order_fill", "order_review", "order_final"}
            and (node.get("key") != "order_fill" or user.pk == order.requester_id)
        ),
        "applicant_signature_required": node.get("key") == "order_fill",
        "mobile_signature": summary["current"]
        .signatures.filter(signer=user, cancelled=False)
        .order_by("-pk")
        .first()
        if summary["current"] and summary["can_act"]
        else None,
        **inbox.history(order, history_limit),
        "materials": materials,
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
            "paper": "扫描版",
            "sealed": "扫描版",
            "awaiting_paper": "电子版",
        }
        roles = dict(ROLES) | {"owner": "线路主管（工单负责人）", "requester": "发起人"}
        payments = dict(MaintenanceOrder._meta.get_field("payment_status").choices)
        stages = []
        for key, name in STAGES:
            nodes = []
            for position, step in enumerate(steps, 1):
                if step["stage"] != key:
                    continue
                rules = []
                step_key = step["key"]
                if step_key == "order_fill":
                    rules.append("填写并保存维护服务工单，核对外购合同、服务单位及服务省市")
                    rules.append("必须由申请人本人完成手写签名后提交；表单修订后须重新签名")
                elif step_key in {"order_review", "order_final"}:
                    rules.append("核对工单内容并签字确认")
                elif step_key in {"payment_plan", "payment_review"}:
                    rules.append(
                        "点击付款信息进入外购合同详情页，由工单发起人维护付款信息后提交"
                        if step_key == "payment_plan"
                        else "点击付款信息进入外购合同详情页，核对付款信息后审核"
                    )
                    rules.append("OA 不要求选择付款记录或填写付款周期、金额")
                elif step.get("report_scans") or step.get("scan_material"):
                    material_key = "report" if step.get("report_scans") else step["scan_material"]
                    material_name = dict(MATERIALS)[material_key]
                    if step_key == f"{material_key}_fill":
                        rules.append(
                            f"至少上传一份扫描版{material_name}，"
                            "可多选 PDF、PNG、JPEG，每份不超过 20 MB"
                        )
                        if material_key == "settlement":
                            rules.append("本环节不填写结算期")
                    elif step_key == f"{material_key}_seal":
                        rules.append("逐份上传对应本次审批原件的用印后扫描件，全部齐备后方可提交")
                    else:
                        rules.append(f"审核本次提交的全部{material_name}；修改材料须先退回补正")
                if step["kind"] == "external":
                    rules.append("必填外部付款申请单号，由经办人登记申请结果")
                if step.get("finish_on_application"):
                    rules.append("提交后完成 OA 工单，不代表已付款或已收款")
                nodes.append(
                    {
                        **step,
                        "position": position,
                        "kind_label": kinds[step["kind"]],
                        "role_label": roles[step["role"]],
                        "group": groups.get(step["role"]),
                        "materials": [
                            f"{dict(MATERIALS)[material]} · "
                            + (
                                "扫描版（可多份）"
                                if (step.get("report_scans") or step.get("scan_material"))
                                and state in {"scan", "paper"}
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
                            if step.get("finish_on_application")
                            else "进入下一环节"
                        ),
                        "rules": rules,
                    }
                )
            stage_summary = {
                "ordering": "申请人填写工单并签名，经初审和部门审核后进入服务报告。",
                "report": "上传扫描版服务报告，经两级用印审批后逐份上传用印结果。",
                "settlement": "上传扫描版服务结算单，经两级用印审批后逐份上传用印结果。",
                "payment": "在合同中维护付款信息，经部门审核后登记外部付款申请。",
            }
            stages.append({"key": key, "name": name, "nodes": nodes, "summary": stage_summary[key]})
        return render(
            request,
            "netbox_oa/maintenance/flow_definition.html",
            {
                "stages": stages,
                "flow_name": FLOW_NAME,
                "diagram": build_diagram(stages),
                "node_count": len(steps),
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


class MaintenanceWorkbenchView(LoginRequiredMixin, View):
    def get(self, request, pk=None, action_form=None, action_error="", status=200):
        orders = MaintenanceOrder.objects.restrict(request.user, "view").select_related(
            "owner", "requester", "policy"
        )
        tasks = actionable_tasks(request.user)
        tab = request.GET.get("tab", "todo")
        if tab not in {"todo", "mine", "following", "all"}:
            tab = "todo"
        filtered = orders
        if pk:
            selected = get_object_or_404(orders, pk=pk)
        else:
            selected = None
        if tab == "todo":
            drafts = MaintenanceOrder.objects.restrict(request.user, "change").filter(
                status="draft"
            )
            if not request.user.is_superuser:
                drafts = drafts.filter(Q(requester=request.user) | Q(owner=request.user))
            filtered = filtered.filter(
                Q(pk__in=tasks.values("order_id")) | Q(pk__in=drafts.values("pk"))
            )
        elif tab == "mine":
            filtered = filtered.filter(requester=request.user)
        elif tab == "following":
            filtered = filtered.filter(followers=request.user)
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
        for key in ("selected", "history", "panel", "pane"):
            query_params.pop(key, None)
        base_url = reverse("plugins:netbox_oa:maintenance_workbench")
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
            ("todo", "待你处理"),
            ("mine", "你发起的"),
            ("following", "你关注的"),
            ("all", "全部工单"),
        ):
            params = query_params.copy()
            params["tab"] = key
            tabs.append({"key": key, "label": label, "url": f"{base_url}?{params.urlencode()}"})
        now = timezone.now()
        warning = sum(
            now < deadline <= now + timedelta(hours=hours)
            for deadline, hours in tasks.values_list("deadline", "order__warning_hours")
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
                "todo": tasks.count(),
                "warning": warning,
                "overdue": tasks.filter(deadline__lte=now).count(),
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
        return render(request, "netbox_oa/maintenance/workbench.html", context, status=status)


class MaintenanceOrderCreateView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.has_perm("netbox_oa.add_maintenanceorder"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return self.render_form(request, MaintenanceOrderForm(actor=request.user))

    def post(self, request):
        form = MaintenanceOrderForm(request.POST, actor=request.user)
        if form.is_valid():
            try:
                order = services.create_order(actor=request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, f"已创建维护服务工单 {order.number}。")
                if "_addanother" in request.POST:
                    return redirect("plugins:netbox_oa:maintenanceorder_add")
                return redirect(order)
        return self.render_form(request, form)

    def render_form(self, request, form):
        return render(
            request,
            "netbox_oa/maintenance/order_create.html",
            {
                "object": form.instance,
                "form": form,
                "return_url": reverse("plugins:netbox_oa:maintenance_workbench"),
            },
        )


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
        return render(
            request,
            "netbox_oa/maintenance/form.html",
            {
                "title": "修改维护工单资料",
                "form": MaintenanceOrderEditForm(instance=order, actor=request.user),
            },
        )

    def post(self, request, pk):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "change"), pk=pk)
        form = MaintenanceOrderEditForm(request.POST, instance=order, actor=request.user)
        if form.is_valid():
            try:
                services.update_order(pk=pk, actor=request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                return redirect(order)
        return render(
            request, "netbox_oa/maintenance/form.html", {"title": "修改维护工单资料", "form": form}
        )


def workbench_params(request):
    source = QueryDict(request.POST.get("return_query", ""))
    params = QueryDict(mutable=True)
    for key in ("tab", "q", "stage", "node", "sort"):
        if key in source:
            params[key] = source[key]
    return params


def workbench_redirect(request, order_id):
    params = workbench_params(request)
    params["selected"] = order_id
    panel = request.POST.get("return_panel", "")
    if panel in {f"material-{key}" for key, _ in MATERIALS}:
        params["panel"] = panel
    return redirect(f"{reverse('plugins:netbox_oa:maintenance_workbench')}?{params.urlencode()}")


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


class MaintenanceTaskActionView(LoginRequiredMixin, View):
    def post(self, request, pk, operation):
        _allowed(MaintenanceTask, request.user, "change", pk)
        task = get_object_or_404(MaintenanceTask, pk=pk)
        form = MaintenanceActionForm(request.POST, order=task.order, actor=request.user)
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
                        "payment_amount",
                        "period_start",
                        "period_end",
                        "settlement_period_mode",
                    )
                }
            elif operation in {"transfer", "countersign"}:
                kwargs["user_id"] = data["user_id"].pk if data["user_id"] else None
            if operation not in handlers:
                return workbench_error(request, task.order_id, "未知任务操作。", form)
            else:
                try:
                    if operation == "complete":
                        kwargs["settlement_period_mode"] = kwargs["settlement_period_mode"] or "day"
                    handlers[operation](pk=pk, actor=request.user, **kwargs)
                except ValidationError as exc:
                    return workbench_error(request, task.order_id, "; ".join(exc.messages), form)
                except PermissionDenied:
                    return workbench_error(
                        request,
                        task.order_id,
                        "当前任务的办理权限或分配已变化，请查看最新状态。",
                        form,
                        status=403,
                    )
                else:
                    messages.success(request, "操作完成。")
        else:
            return workbench_error(request, task.order_id, "请检查办理信息。", form)
        return workbench_redirect(request, task.order_id)


class MaintenanceUploadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if request.POST.get("report_scans") == "1" or request.POST.get("scan_material"):
            material = request.POST.get("scan_material", "report")
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
            scan=request.POST.get("state") in {"scan", "paper", "sealed"},
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
                    if form.cleaned_data["state"] in {"scan", "paper", "sealed"}
                    else "附件新版本已保存。",
                )
        else:
            messages.error(request, str(form.errors.as_text()))
        return workbench_redirect(request, pk)


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
        return FileResponse(
            document.file.open("rb"), as_attachment=True, filename=document.original_name
        )


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
            return redirect(order)
        previous = (
            order.documents.restrict(request.user, "view")
            .filter(material=material, kind="form")
            .first()
        )
        initial = dict(previous.form_data) if previous else company_form_initial(order)
        initial["requested_at"] = parse_datetime(initial["requested_at"])
        form = MaintenanceCompanyForm(material=material, initial=initial, actor=request.user)
        return self.render_form(request, order, material, form)

    def post(self, request, pk, material):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        try:
            services.check_company_form_access(order, request.user, material)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect(order)
        form = MaintenanceCompanyForm(request.POST, material=material, actor=request.user)
        if form.is_valid():
            try:
                document = services.save_company_form(
                    pk=pk, actor=request.user, material=material, data=request.POST
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, "表单已保存，可打印后签字盖章并上传扫描件。")
                return redirect("plugins:netbox_oa:maintenance_print", pk=document.pk)
        return self.render_form(request, order, material, form)

    def render_form(self, request, order, material, form):
        return render(
            request,
            "netbox_oa/maintenance/company_form.html",
            {
                "order": order,
                "material": material,
                "title": COMPANY_FORMS[material],
                "form": form,
            },
        )


class MaintenancePrintView(LoginRequiredMixin, View):
    def get(self, request, pk):
        document = get_object_or_404(
            MaintenanceDocument.objects.restrict(request.user, "view").select_related("order"),
            pk=pk,
            kind="form",
            material__in=COMPANY_FORMS,
        )
        _allowed(MaintenanceOrder, request.user, "view", document.order_id)
        data = document.form_data
        signatures = {}
        if document.material == "order":
            evidence_ids = {document.pk, *document.derived_documents.values_list("pk", flat=True)}
            for task in document.order.tasks.restrict(request.user, "view").filter(
                status="completed"
            ):
                if task.evidence.get("order") in evidence_ids:
                    node = document.order.steps_snapshot[task.position]
                    if node["key"] in {"order_fill", "order_review", "order_final"}:
                        signatures[node["key"]] = task.evidence.get("signature", {})
        return render(
            request,
            "netbox_oa/maintenance/print.html",
            {
                "document": document,
                "data": data,
                "requested_at": data.get("requested_at", "")[:16].replace("T", " "),
                "service_items": SERVICE_ITEMS,
                "signatures": signatures,
            },
        )


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
