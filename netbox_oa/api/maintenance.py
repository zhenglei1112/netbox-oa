from django.core.exceptions import ValidationError as DjangoValidationError
from netbox.api.serializers import ValidatedModelSerializer
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from netbox_oa import maintenance_services as services
from netbox_oa.models import (
    MaintenanceDocument,
    MaintenanceOrder,
    MaintenancePolicy,
    MaintenanceTask,
)

from .views import TransitionViewMixin


class PolicySerializer(ValidatedModelSerializer):
    groups = serializers.SerializerMethodField()

    def get_groups(self, obj):
        return dict(obj.roles.values_list("role", "group_id"))

    class Meta:
        model = MaintenancePolicy
        fields = ("id", "url", "name", "enabled", "sla_hours", "warning_hours", "groups")


class PolicyWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    enabled = serializers.BooleanField(default=True)
    sla_hours = serializers.IntegerField(min_value=1, default=24)
    warning_hours = serializers.IntegerField(min_value=0, default=4)
    groups = serializers.DictField(child=serializers.IntegerField(min_value=1))


class OrderSerializer(ValidatedModelSerializer):
    def validate(self, attrs):
        return attrs

    def create(self, data):
        return services.create_order(actor=self.context["request"].user, **data)

    class Meta:
        model = MaintenanceOrder
        fields = (
            "id",
            "url",
            "number",
            "title",
            "policy",
            "requester",
            "owner",
            "service_type",
            "priority",
            "supplier",
            "contract_number",
            "service_region",
            "service_location",
            "contact_phone",
            "requirements",
            "fault_reference",
            "status",
            "stage",
            "current_position",
            "steps_snapshot",
            "payment_status",
            "payment_amount",
            "period_start",
            "period_end",
            "completed_at",
            "created",
            "last_updated",
        )
        read_only_fields = (
            "number",
            "requester",
            "status",
            "stage",
            "current_position",
            "steps_snapshot",
            "payment_status",
            "payment_amount",
            "period_start",
            "period_end",
            "completed_at",
        )


class TaskSerializer(ValidatedModelSerializer):
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


class TaskActionSerializer(serializers.Serializer):
    comment = serializers.CharField(max_length=4000, required=False, allow_blank=True, default="")
    external_reference = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    user_id = serializers.IntegerField(min_value=1, required=False)
    payment_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    period_start = serializers.DateField(required=False)
    period_end = serializers.DateField(required=False)


class DocumentSerializer(ValidatedModelSerializer):
    class Meta:
        model = MaintenanceDocument
        # Never serialize the private file's storage URL.
        fields = (
            "id",
            "order",
            "material",
            "version",
            "original_name",
            "kind",
            "form_data",
            "source_document",
            "uploaded_by",
            "state",
            "note",
            "created",
        )
        read_only_fields = fields


class DocumentWriteSerializer(serializers.Serializer):
    source_document_id = serializers.IntegerField(min_value=1, required=False)
    order_id = serializers.IntegerField(min_value=1)
    material = serializers.ChoiceField(
        choices=MaintenanceDocument._meta.get_field("material").choices
    )
    state = serializers.ChoiceField(
        choices=MaintenanceDocument._meta.get_field("state").choices, default="electronic"
    )
    file = serializers.FileField()
    note = serializers.CharField(required=False, allow_blank=True, default="")


def service_response(view, request, handler, data, **kwargs):
    data.is_valid(raise_exception=True)
    try:
        obj = handler(actor=request.user, **data.validated_data, **kwargs)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.messages) from exc
    return Response(view.get_serializer(obj).data)


class PolicyViewSet(TransitionViewMixin, NetBoxModelViewSet):
    queryset = MaintenancePolicy.objects.all()
    serializer_class = PolicySerializer
    http_method_names = ["get", "post", "head", "options"]
    transition_actions = ("configure",)

    def create(self, request, *args, **kwargs):
        response = service_response(
            self, request, services.save_policy, PolicyWriteSerializer(data=request.data)
        )
        response.status_code = 201
        return response

    @action(detail=True, methods=["post"])
    def configure(self, request, pk=None):
        obj = self.get_object()
        return service_response(
            self, request, services.save_policy, PolicyWriteSerializer(data=request.data), pk=obj.pk
        )


class OrderViewSet(TransitionViewMixin, NetBoxModelViewSet):
    queryset = MaintenanceOrder.objects.select_related("policy", "requester", "owner")
    serializer_class = OrderSerializer
    http_method_names = ["get", "post", "head", "options"]
    transition_actions = ("start", "withdraw", "terminate")
    filterset_fields = (
        "number",
        "stage",
        "status",
        "requester_id",
        "owner_id",
        "priority",
        "service_type",
    )

    def perform_create(self, serializer):
        try:
            super().perform_create(serializer)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        obj = self.get_object()
        try:
            result = services.start_order(pk=obj.pk, actor=request.user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(self.get_serializer(result).data)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        obj = self.get_object()
        data = TaskActionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            result = services.withdraw_order(
                pk=obj.pk, actor=request.user, comment=data.validated_data["comment"]
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(self.get_serializer(result).data)

    @action(detail=True, methods=["post"])
    def terminate(self, request, pk=None):
        obj = self.get_object()
        data = TaskActionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            result = services.terminate_order(
                pk=obj.pk, actor=request.user, comment=data.validated_data["comment"]
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(self.get_serializer(result).data)


class TaskViewSet(TransitionViewMixin, NetBoxReadOnlyModelViewSet):
    queryset = MaintenanceTask.objects.select_related("order", "assigned_user", "assigned_group")
    serializer_class = TaskSerializer
    transition_actions = ("complete", "send_back", "claim", "transfer", "countersign")
    filterset_fields = ("order_id", "status", "kind", "assigned_user_id", "assigned_group_id")

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(order__in=MaintenanceOrder.objects.restrict(self.request.user, "view"))
        )

    def mutate(self, request, operation):
        obj = self.get_object()
        data = TaskActionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = {
            "complete": (
                "comment",
                "external_reference",
                "payment_amount",
                "period_start",
                "period_end",
            ),
            "return": ("comment",),
            "claim": (),
            "transfer": ("user_id", "comment"),
            "countersign": ("user_id", "comment"),
        }[operation]
        kwargs = {key: data.validated_data[key] for key in fields if key in data.validated_data}
        if operation in {"transfer", "countersign"} and "user_id" not in kwargs:
            raise serializers.ValidationError("请选择目标人员。")
        handlers = {
            "complete": services.complete_task,
            "return": services.return_task,
            "claim": services.claim_task,
            "transfer": services.transfer_task,
            "countersign": services.countersign_task,
        }
        try:
            result = handlers[operation](pk=obj.pk, actor=request.user, **kwargs)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(self.get_serializer(result).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        return self.mutate(request, "complete")

    @action(detail=True, methods=["post"], url_path="return")
    def send_back(self, request, pk=None):
        return self.mutate(request, "return")

    @action(detail=True, methods=["post"])
    def claim(self, request, pk=None):
        return self.mutate(request, "claim")

    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        return self.mutate(request, "transfer")

    @action(detail=True, methods=["post"])
    def countersign(self, request, pk=None):
        return self.mutate(request, "countersign")


class DocumentViewSet(NetBoxReadOnlyModelViewSet):
    queryset = MaintenanceDocument.objects.all()
    serializer_class = DocumentSerializer
    filterset_fields = ("order_id", "material", "version")

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(order__in=MaintenanceOrder.objects.restrict(self.request.user, "view"))
        )

    def create(self, request, *args, **kwargs):
        data = DocumentWriteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        fields = dict(data.validated_data)
        pk = fields.pop("order_id")
        try:
            result = services.upload_document(pk=pk, actor=request.user, **fields)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return Response(self.get_serializer(result).data, status=201)
