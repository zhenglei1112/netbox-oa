from netbox.api.serializers import ValidatedModelSerializer
from rest_framework import serializers

from netbox_oa import services
from netbox_oa.models import WorkflowDefinition, WorkflowInstance, WorkflowTask


class WorkflowDefinitionSerializer(ValidatedModelSerializer):
    class Meta:
        model = WorkflowDefinition
        fields = (
            "id",
            "url",
            "display",
            "name",
            "slug",
            "description",
            "enabled",
            "steps",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class WorkflowInstanceSerializer(ValidatedModelSerializer):
    # Instance creation is validated in the service: requester is always the actor.
    def validate(self, attrs):
        return attrs

    def create(self, validated_data):
        return services.create_instance(actor=self.context["request"].user, **validated_data)

    class Meta:
        model = WorkflowInstance
        fields = (
            "id",
            "url",
            "display",
            "title",
            "definition",
            "requester",
            "status",
            "payload",
            "steps_snapshot",
            "current_step",
            "created",
            "last_updated",
        )
        read_only_fields = (
            "requester",
            "status",
            "steps_snapshot",
            "current_step",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "status")


class WorkflowTaskSerializer(ValidatedModelSerializer):
    class Meta:
        model = WorkflowTask
        fields = (
            "id",
            "url",
            "display",
            "instance",
            "name",
            "position",
            "assigned_group",
            "status",
            "decided_by",
            "decided_at",
            "comment",
            "created",
            "last_updated",
        )
        read_only_fields = fields
        brief_fields = ("id", "url", "display", "status")


class ActionSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, max_length=4000, default="")
