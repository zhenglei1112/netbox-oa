from netbox.api.serializers import ValidatedModelSerializer
from rest_framework import serializers

from netbox_oa import maintenance_services, services
from netbox_oa.maintenance_contracts import order_object_links
from netbox_oa.maintenance_flow import ROLES
from netbox_oa.maintenance_materials import SERVICE_ITEMS
from netbox_oa.models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceLink,
    MaintenanceOrder,
    MaintenancePolicy,
    MaintenanceRole,
    MaintenanceTask,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowTask,
)


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


class MaintenancePolicySerializer(ValidatedModelSerializer):
    groups = serializers.SerializerMethodField()

    def get_groups(self, obj):
        return dict(obj.roles.filter(role__in=dict(ROLES)).values_list("role", "group_id"))

    class Meta:
        model = MaintenancePolicy
        fields = (
            "id",
            "url",
            "name",
            "enabled",
            "is_default",
            "sla_hours",
            "warning_hours",
            "groups",
        )


class MaintenanceRoleSerializer(ValidatedModelSerializer):
    class Meta:
        model = MaintenanceRole
        # This model is managed through policy configuration, without an API detail URL.
        fields = ("id", "display", "policy", "role", "group", "created", "last_updated")
        read_only_fields = fields


class ServiceItemsSerializerField(serializers.ListField):
    def to_internal_value(self, data):
        if isinstance(data, str):
            data = [data]
        return super().to_internal_value(data)


class MaintenanceOrderSerializer(ValidatedModelSerializer):
    service_type = ServiceItemsSerializerField(
        child=serializers.ChoiceField(choices=SERVICE_ITEMS), allow_empty=False,
    )
    current_contract_type = serializers.SerializerMethodField()

    def get_current_contract_type(self, obj):
        return order_object_links(obj, self.context["request"].user)["current_contract_type"]

    contract_id = serializers.IntegerField(min_value=1)
    service_region_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=False
    )

    def validate(self, attrs):
        return attrs

    def create(self, data):
        return maintenance_services.create_order(actor=self.context["request"].user, **data)

    class Meta:
        model = MaintenanceOrder
        fields = (
            "id",
            "url",
            "number",
            "policy",
            "requester",
            "owner",
            "service_type",
            "priority",
            "supplier",
            "contract_number",
            "contract_id",
            "contract_type_snapshot",
            "number_type_snapshot",
            "current_contract_type",
            "service_region",
            "service_region_ids",
            "service_location",
            "contact_phone",
            "requirements",
            "fault_reference",
            "status",
            "stage",
            "current_position",
            "steps_snapshot",
            "payment_status",
            "completed_at",
            "created",
            "last_updated",
        )
        read_only_fields = (
            "number",
            "contract_type_snapshot",
            "number_type_snapshot",
            "supplier",
            "contract_number",
            "service_region",
            "requester",
            "status",
            "stage",
            "current_position",
            "steps_snapshot",
            "payment_status",
            "completed_at",
        )


class MaintenanceTaskSerializer(ValidatedModelSerializer):
    class Meta:
        model = MaintenanceTask
        fields = (
            "id",
            "url",
            "order",
            "position",
            "name",
            "kind",
            "assigned_group",
            "assigned_user",
            "status",
            "deadline",
            "started_at",
            "completed_at",
            "decided_by",
            "comment",
            "external_reference",
            "evidence",
            "is_countersign",
            "created",
        )
        read_only_fields = fields


class MaintenanceDocumentSerializer(ValidatedModelSerializer):
    class Meta:
        model = MaintenanceDocument
        # Never serialize the private file's storage URL.
        fields = (
            "id",
            "order",
            "material",
            "version",
            "item_number",
            "replaces_document",
            "display_name",
            "original_name",
            "kind",
            "form_data",
            "billing_data",
            "source_document",
            "uploaded_task",
            "is_sealed",
            "uploaded_by",
            "state",
            "note",
            "created",
        )
        read_only_fields = fields


class MaintenanceEventSerializer(ValidatedModelSerializer):
    class Meta:
        model = MaintenanceEvent
        fields = (
            "id", "display", "order", "task", "actor", "action", "description", "details",
            "created", "last_updated",
        )
        read_only_fields = fields


class MaintenanceLinkSerializer(ValidatedModelSerializer):
    class Meta:
        model = MaintenanceLink
        fields = (
            "id", "display", "order", "object_type", "object_id", "label", "created",
            "last_updated",
        )
        read_only_fields = fields
