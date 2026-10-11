from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import OuterRef, Q, Subquery
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View

from . import maintenance_services as services
from .maintenance_diagram import build_diagram
from .maintenance_flow import MATERIALS, ROLES, STAGES, build_steps
from .maintenance_forms import (
    MaintenanceActionForm,
    MaintenanceCompanyForm,
    MaintenanceDocumentForm,
    MaintenanceLinkForm,
    MaintenanceOrderEditForm,
    MaintenanceOrderForm,
    MaintenancePolicyForm,
)
from .maintenance_materials import COMPANY_FORMS, SERVICE_ITEMS, company_form_initial
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


def presentation(order, user, with_details=True):
    now = timezone.now()
    visible = order.tasks.restrict(user, "view").select_related(
        "assigned_group", "assigned_user", "decided_by"
    )
    if not with_details:
        visible = visible.filter(status="pending")
    latest = {task.position: task for task in visible.order_by("pk")}
    current = latest.get(order.current_position) if order.status == "active" else None
    can_act = with_details and current and actionable_tasks(user).filter(pk=current.pk).exists()
    nodes = []
    for index, step in enumerate(order.steps_snapshot):
        task = latest.get(index)
        state = "not_started"
        if task:
            state = task.status
        if index > order.current_position and order.status == "active":
            state = "not_started"
        nodes.append(
            {
                **step,
                "index": index,
                "task": task,
                "state": state,
                "current": bool(current and index == order.current_position),
            }
        )
    stages = []
    for key, name in STAGES:
        indexes = [i for i, step in enumerate(order.steps_snapshot) if step["stage"] == key]
        state = "not_started"
        if order.status == "completed" or (indexes and max(indexes) < order.current_position):
            state = "completed"
        elif order.stage == key:
            state = "active"
        stages.append({"key": key, "name": name, "state": state})
    if not with_details:
        waited = max(0, int((now - current.created).total_seconds() // 60)) if current else 0
        state = "normal"
        if current and now >= current.deadline:
            state = "overdue"
        elif current and now + timedelta(hours=order.warning_hours) >= current.deadline:
            state = "warning"
        return {
            "order": order,
            "stages": stages,
            "current": current,
            "waiting": f"{waited // 60}h {waited % 60}m",
            "deadline_state": state,
        }
    materials = []
    documents = list(
        order.documents.restrict(user, "view")
        .select_related("source_document")
        .order_by("material", "-version")
    )
    for key, name in MATERIALS:
        versions = [doc for doc in documents if doc.material == key]
        source = next((doc for doc in versions if doc.kind != "scan"), None)
        form_document = next((doc for doc in versions if doc.kind == "form"), None)
        attachments = [doc for doc in versions if doc.kind == "attachment"]
        scans = [doc for doc in versions if doc.kind == "scan"]
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
                "document": versions[0] if versions else None,
                "versions": versions,
                "form_document": form_document,
                "form_versions": [doc for doc in versions if doc.kind == "form"],
                "can_fill": can_fill,
                "attachments": attachments,
                "attachment": attachments[0] if attachments else None,
                "scans": scans,
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
    waited = max(0, int((now - current.created).total_seconds() // 60)) if current else 0
    deadline_state = "normal"
    if current:
        if now >= current.deadline:
            deadline_state = "overdue"
        elif now + timedelta(hours=order.warning_hours) >= current.deadline:
            deadline_state = "warning"
    return {
        "order": order,
        "stages": stages,
        "nodes": nodes,
        "current": current,
        "can_act": can_act,
        "materials": materials,
        "company_forms": [item for item in materials if item["key"] in COMPANY_FORMS],
        "attachment_materials": [item for item in materials if item["key"] != "order"],
        "links": links,
        "waiting": f"{waited // 60}h {waited % 60}m",
        "deadline_state": deadline_state,
        "following": order.followers.filter(pk=user.pk).exists(),
        "can_change": MaintenanceOrder.objects.restrict(user, "change")
        .filter(pk=order.pk)
        .exists(),
        "events": order.events.restrict(user, "view").select_related("actor")[:100],
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
        states = {"electronic": "电子版", "paper": "纸质签字版", "sealed": "盖章版"}
        roles = dict(ROLES) | {"owner": "线路主管（工单负责人）"}
        payments = dict(MaintenanceOrder._meta.get_field("payment_status").choices)
        stages = []
        for key, name in STAGES:
            nodes = []
            for position, step in enumerate(steps, 1):
                if step["stage"] != key:
                    continue
                rules = []
                if step["kind"] == "external":
                    rules.append("必填外部申请编号或处理凭证；人工登记结果")
                if step["key"] == "payment_plan":
                    rules.append("必填正数付款金额和有效服务期间")
                if step["key"] in {"issue", "paid", "receipt", "finance_handover"} or step[
                    "key"
                ].endswith("_mail"):
                    rules.append("必填办理说明")
                if step["key"] == "receipt":
                    rules.append("已确认实际付款后，确认收款并完成工单")
                nodes.append(
                    {
                        **step,
                        "position": position,
                        "kind_label": kinds[step["kind"]],
                        "role_label": roles[step["role"]],
                        "group": groups.get(step["role"]),
                        "materials": [
                            f"{dict(MATERIALS)[material]} · {states[state]}"
                            for material, state in step["required"]
                        ],
                        "return_label": names.get(step["return_to"], "")
                        if step["kind"] in {"approval", "external", "countersign"}
                        else "",
                        "payment_label": payments.get(step["payment"], ""),
                        "rules": rules,
                    }
                )
            stages.append({"key": key, "name": name, "nodes": nodes})
        return render(
            request,
            "netbox_oa/maintenance/flow_definition.html",
            {
                "stages": stages,
                "diagram": build_diagram(stages),
                "node_count": len(steps),
                "policies": policies,
                "policy": policy,
                "sla_hours": sla,
                "roles": [{"name": name, "group": groups.get(key)} for key, name in ROLES],
            },
        )


class MaintenanceWorkbenchView(LoginRequiredMixin, View):
    def get(self, request, pk=None):
        orders = MaintenanceOrder.objects.restrict(request.user, "view").select_related(
            "owner", "requester", "policy"
        )
        tasks = actionable_tasks(request.user)
        tab = request.GET.get("tab", "todo")
        filtered = orders
        if pk:
            selected = get_object_or_404(orders, pk=pk)
        else:
            selected = None
        if tab == "todo":
            filtered = filtered.filter(pk__in=tasks.values("order_id"))
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
                | Q(title__icontains=query)
                | Q(supplier__icontains=query)
                | Q(contract_number__icontains=query)
            )
        node = request.GET.get("node", "").strip()
        if node:
            filtered = filtered.filter(
                tasks__status="pending", tasks__name__icontains=node
            ).distinct()
        if request.GET.get("sort") == "waiting":
            pending = MaintenanceTask.objects.filter(order_id=OuterRef("pk"), status="pending")
            filtered = filtered.annotate(
                waiting_since=Subquery(pending.values("created")[:1])
            ).order_by("waiting_since", "pk")
        rows = list(filtered[:100])
        if not selected:
            selected = next(
                (order for order in rows if str(order.pk) == request.GET.get("selected")), None
            )
            selected = selected or (rows[0] if rows else None)
        now = timezone.now()
        warning = sum(
            now < deadline <= now + timedelta(hours=hours)
            for deadline, hours in tasks.values_list("deadline", "order__warning_hours")
        )
        month_start = timezone.localtime(now).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        context = {
            "rows": [presentation(order, request.user, with_details=False) for order in rows],
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
            "detail": presentation(selected, request.user) if selected else None,
            "action_form": MaintenanceActionForm(),
            "total": filtered.count(),
        }
        return render(request, "netbox_oa/maintenance/workbench.html", context)


class MaintenanceOrderCreateView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.has_perm("netbox_oa.add_maintenanceorder"):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render(
            request,
            "netbox_oa/maintenance/form.html",
            {"title": "发起维护服务工单", "form": MaintenanceOrderForm(actor=request.user)},
        )

    def post(self, request):
        form = MaintenanceOrderForm(request.POST, actor=request.user)
        if form.is_valid():
            try:
                return redirect(services.create_order(actor=request.user, **form.cleaned_data))
            except ValidationError as exc:
                form.add_error(None, exc)
        return render(
            request, "netbox_oa/maintenance/form.html", {"title": "发起维护服务工单", "form": form}
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
            messages.error(request, "; ".join(exc.messages))
        return redirect("plugins:netbox_oa:maintenanceorder", pk=pk)


class MaintenanceTaskActionView(LoginRequiredMixin, View):
    def post(self, request, pk, operation):
        _allowed(MaintenanceTask, request.user, "change", pk)
        task = get_object_or_404(MaintenanceTask, pk=pk)
        form = MaintenanceActionForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            handlers = {
                "complete": services.complete_task,
                "return": services.return_task,
                "claim": services.claim_task,
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
                    )
                }
            elif operation == "claim":
                kwargs = {}
            elif operation in {"transfer", "countersign"}:
                kwargs["user_id"] = data["user_id"].pk if data["user_id"] else None
            if operation not in handlers:
                messages.error(request, "未知任务操作。")
            else:
                try:
                    handlers[operation](pk=pk, actor=request.user, **kwargs)
                except ValidationError as exc:
                    messages.error(request, "; ".join(exc.messages))
                else:
                    messages.success(request, "操作完成。")
        else:
            messages.error(request, str(form.errors.as_text()))
        return redirect("plugins:netbox_oa:maintenanceorder", pk=task.order_id)


class MaintenanceUploadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        form = MaintenanceDocumentForm(
            request.POST, request.FILES, scan=request.POST.get("state") in {"paper", "sealed"}
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
                    if form.cleaned_data["state"] in {"paper", "sealed"}
                    else "附件新版本已保存。",
                )
        else:
            messages.error(request, str(form.errors.as_text()))
        return redirect("plugins:netbox_oa:maintenanceorder", pk=pk)


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
        form = MaintenanceCompanyForm(material=material, initial=initial)
        return self.render_form(request, order, material, form)

    def post(self, request, pk, material):
        order = get_object_or_404(MaintenanceOrder.objects.restrict(request.user, "view"), pk=pk)
        form = MaintenanceCompanyForm(request.POST, material=material)
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
        return render(
            request,
            "netbox_oa/maintenance/print.html",
            {
                "document": document,
                "data": data,
                "requested_at": data.get("requested_at", "")[:16].replace("T", " "),
                "service_items": SERVICE_ITEMS,
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
        return redirect("plugins:netbox_oa:maintenanceorder", pk=pk)
