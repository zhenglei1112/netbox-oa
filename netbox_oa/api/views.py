from django.core.exceptions import ValidationError as DjangoValidationError
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from netbox_oa import services
from netbox_oa.filtersets import (
    WorkflowDefinitionFilterSet,
    WorkflowInstanceFilterSet,
    WorkflowTaskFilterSet,
)
from netbox_oa.models import WorkflowDefinition, WorkflowInstance, WorkflowTask

from .serializers import (
    ActionSerializer,
    WorkflowDefinitionSerializer,
    WorkflowInstanceSerializer,
    WorkflowTaskSerializer,
)


class TransitionPermissions(TokenPermissions):
    perms_map = {**TokenPermissions.perms_map, "POST": ["%(app_label)s.change_%(model_name)s"]}


class TransitionViewMixin:
    transition_actions = ()

    def initial(self, request, *args, **kwargs):
        if self.action in self.transition_actions:
            # NetBox maps POST to add; state actions require change instead.
            GenericViewSet.initial(self, request, *args, **kwargs)
            self.queryset = self.queryset.restrict(request.user, "change")
        else:
            super().initial(request, *args, **kwargs)

    def get_permissions(self):
        if self.action in self.transition_actions:
            return [TransitionPermissions()]
        return super().get_permissions()

    def run_action(self, request, handler, **kwargs):
        obj = self.get_object()
        serializer = ActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = handler(pk=obj.pk, actor=request.user, **serializer.validated_data, **kwargs)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(self.get_serializer(result).data)


class WorkflowDefinitionViewSet(NetBoxModelViewSet):
    queryset = WorkflowDefinition.objects.all()
    serializer_class = WorkflowDefinitionSerializer
    filterset_class = WorkflowDefinitionFilterSet


class WorkflowInstanceViewSet(TransitionViewMixin, NetBoxModelViewSet):
    queryset = WorkflowInstance.objects.select_related("definition", "requester")
    serializer_class = WorkflowInstanceSerializer
    filterset_class = WorkflowInstanceFilterSet
    http_method_names = ["get", "post", "head", "options"]
    transition_actions = ("submit", "cancel")

    def perform_create(self, serializer):
        try:
            super().perform_create(serializer)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        return self.run_action(request, services.submit_instance)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self.run_action(request, services.cancel_instance)


class WorkflowTaskViewSet(TransitionViewMixin, NetBoxReadOnlyModelViewSet):
    queryset = WorkflowTask.objects.select_related("instance", "assigned_group", "decided_by")
    serializer_class = WorkflowTaskSerializer
    filterset_class = WorkflowTaskFilterSet
    transition_actions = ("approve", "reject")

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self.run_action(request, services.decide_task, decision="approve")

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self.run_action(request, services.decide_task, decision="reject")
