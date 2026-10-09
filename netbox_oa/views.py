from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django_fsm_log.models import StateLog
from netbox.object_actions import AddObject, EditObject
from netbox.views import generic

from . import filtersets, forms, models, services, tables


class WorkflowDefinitionListView(generic.ObjectListView):
    queryset = models.WorkflowDefinition.objects.all()
    table = tables.WorkflowDefinitionTable
    filterset = filtersets.WorkflowDefinitionFilterSet
    actions = (AddObject,)


class WorkflowDefinitionView(generic.ObjectView):
    queryset = models.WorkflowDefinition.objects.all()
    template_name = "netbox_oa/definition.html"
    actions = (EditObject,)


class WorkflowDefinitionEditView(generic.ObjectEditView):
    queryset = models.WorkflowDefinition.objects.all()
    form = forms.WorkflowDefinitionForm


class WorkflowInstanceListView(generic.ObjectListView):
    queryset = models.WorkflowInstance.objects.select_related("definition", "requester")
    table = tables.WorkflowInstanceTable
    filterset = filtersets.WorkflowInstanceFilterSet
    actions = (AddObject,)


class WorkflowInstanceView(generic.ObjectView):
    queryset = models.WorkflowInstance.objects.select_related("definition", "requester")
    template_name = "netbox_oa/instance.html"
    actions = ()

    def get_extra_context(self, request, instance):
        can_change = (
            models.WorkflowInstance.objects.restrict(request.user, "change")
            .filter(pk=instance.pk)
            .exists()
        )
        return {
            "tasks": instance.tasks.restrict(request.user, "view").select_related("assigned_group"),
            "logs": StateLog.objects.for_(instance).select_related("by").order_by("-timestamp"),
            "can_act": can_change
            and (request.user.is_superuser or instance.requester_id == request.user.pk),
        }


class WorkflowTaskListView(generic.ObjectListView):
    queryset = models.WorkflowTask.objects.select_related("instance", "assigned_group")
    table = tables.WorkflowTaskTable
    filterset = filtersets.WorkflowTaskFilterSet
    actions = ()


class WorkflowTaskView(generic.ObjectView):
    queryset = models.WorkflowTask.objects.select_related(
        "instance", "assigned_group", "decided_by"
    )
    template_name = "netbox_oa/task.html"
    actions = ()

    def get_extra_context(self, request, instance):
        return {
            "can_act": models.WorkflowTask.objects.restrict(request.user, "change")
            .filter(pk=instance.pk)
            .exists()
            and (
                request.user.is_superuser
                or request.user.groups.filter(pk=instance.assigned_group_id).exists()
            ),
            "logs": StateLog.objects.for_(instance).select_related("by").order_by("-timestamp"),
        }


class WorkflowInstanceCreateView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "netbox_oa.add_workflowinstance"
    raise_exception = True

    def get(self, request):
        return render(
            request,
            "netbox_oa/create_instance.html",
            {
                "form": forms.WorkflowInstanceForm(user=request.user),
            },
        )

    def post(self, request):
        form = forms.WorkflowInstanceForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                instance = services.create_instance(actor=request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                return redirect(instance)
        return render(request, "netbox_oa/create_instance.html", {"form": form})


class WorkflowActionView(LoginRequiredMixin, View):
    """POST-only, protected by Django CSRF middleware and service permissions."""

    def post(self, request, kind, pk, operation):
        comment = request.POST.get("comment", "")
        model = models.WorkflowInstance if kind == "instance" else models.WorkflowTask
        # Avoid disclosing the existence of objects outside the permitted scope.
        obj = get_object_or_404(model.objects.restrict(request.user, "change"), pk=pk)
        handlers = {
            ("instance", "submit"): services.submit_instance,
            ("instance", "cancel"): services.cancel_instance,
            ("task", "approve"): services.decide_task,
            ("task", "reject"): services.decide_task,
        }
        if (kind, operation) not in handlers or len(comment) > 4000:
            return render(request, "netbox_oa/action_error.html", status=400)
        kwargs = {"decision": operation} if kind == "task" else {}
        try:
            handlers[kind, operation](pk=pk, actor=request.user, comment=comment, **kwargs)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "操作已完成。")
        return redirect(obj)
