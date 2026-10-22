from uuid import uuid4

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django_fsm import FSMField, transition
from django_fsm_log.decorators import fsm_log_by, fsm_log_description
from netbox.models import ChangeLoggedModel

from .choices import TaskStatus, WorkflowStatus
from .maintenance_flow import MATERIALS, ROLES, STAGES
from .storage import MaintenanceDocumentStorage
from .validators import validate_payload, validate_steps


class WorkflowDefinition(ChangeLoggedModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="流程名称")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="流程标识")
    description = models.TextField(blank=True, verbose_name="描述说明")
    enabled = models.BooleanField(default=True, verbose_name="是否启用")
    steps = models.JSONField(default=list, validators=[validate_steps], verbose_name="审批节点")

    class Meta:
        ordering = ("name",)
        verbose_name = "流程定义"
        verbose_name_plural = "流程定义"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:workflowdefinition", args=[self.pk])

    def clean(self):
        super().clean()
        validate_steps(self.steps)
        from users.models import Group

        ids = {step["group_id"] for step in self.steps}
        if Group.objects.filter(pk__in=ids).count() != len(ids):
            raise ValidationError({"steps": "流程引用了不存在的审批组。"})


class WorkflowStateModel(ChangeLoggedModel):
    """Keep protected FSM states compatible with Django validation and reloads."""

    class Meta:
        abstract = True

    def __setattr__(self, name, value):
        if name == "status" and self.__dict__.get("_refreshing_fsm_state", False):
            # Database reloads restore persisted state without a transition.
            # Keep the field's shared protected flag enabled for every instance.
            self._meta.get_field(name).set_state(self, value)
            return
        super().__setattr__(name, value)

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        previous = self.__dict__.get("_refreshing_fsm_state", False)
        self._refreshing_fsm_state = True
        try:
            return super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        finally:
            self._refreshing_fsm_state = previous

    def clean_fields(self, exclude=None):
        excluded = set(exclude or ())
        super().clean_fields(exclude=excluded | {"status"})
        if "status" not in excluded:
            try:
                self._meta.get_field("status").clean(self.status, self)
            except ValidationError as exc:
                raise ValidationError({"status": exc.messages}) from exc


class WorkflowInstance(WorkflowStateModel):
    title = models.CharField(max_length=200, verbose_name="申请标题")
    definition = models.ForeignKey(
        WorkflowDefinition,
        on_delete=models.PROTECT,
        related_name="instances",
        verbose_name="流程定义",
    )
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="oa_requests",
        verbose_name="申请人",
    )
    status = FSMField(
        max_length=20,
        default=WorkflowStatus.DRAFT,
        choices=WorkflowStatus,
        protected=True,
        verbose_name="流程状态",
    )
    payload = models.JSONField(
        default=dict, blank=True, validators=[validate_payload], verbose_name="业务数据"
    )
    steps_snapshot = models.JSONField(default=list, blank=True, verbose_name="节点快照")
    current_step = models.PositiveIntegerField(default=0, verbose_name="当前节点")
    approval_groups = models.ManyToManyField(
        "users.Group",
        through="WorkflowGroupReservation",
        related_name="oa_instances",
        verbose_name="审批组",
    )

    class Meta:
        ordering = ("-created", "-pk")
        verbose_name = "流程实例"
        verbose_name_plural = "流程实例"

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:workflowinstance", args=[self.pk])

    def clean(self):
        super().clean()
        # JSONField(blank=True) skips validators for empty lists; require an object.
        validate_payload(self.payload)

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source=WorkflowStatus.DRAFT, target=WorkflowStatus.PENDING)
    def submit(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source=WorkflowStatus.PENDING, target=WorkflowStatus.APPROVED)
    def approve(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source=WorkflowStatus.PENDING, target=WorkflowStatus.REJECTED)
    def reject(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(
        field=status,
        source=[WorkflowStatus.DRAFT, WorkflowStatus.PENDING],
        target=WorkflowStatus.CANCELLED,
    )
    def cancel(self, *, by, description=""):
        pass


class WorkflowGroupReservation(models.Model):
    """Keep snapshot approver groups alive, including steps not yet reached."""

    _netbox_private = True
    instance = models.ForeignKey(WorkflowInstance, on_delete=models.CASCADE)
    group = models.ForeignKey("users.Group", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("instance", "group"), name="oa_unique_reserved_group")
        ]


class WorkflowTask(WorkflowStateModel):
    instance = models.ForeignKey(
        WorkflowInstance, on_delete=models.PROTECT, related_name="tasks", verbose_name="所属流程"
    )
    name = models.CharField(max_length=100, verbose_name="任务名称")
    position = models.PositiveIntegerField(verbose_name="节点序号")
    assigned_group = models.ForeignKey(
        "users.Group", on_delete=models.PROTECT, related_name="oa_tasks", verbose_name="审批组"
    )
    status = FSMField(
        max_length=20,
        default=TaskStatus.PENDING,
        choices=TaskStatus,
        protected=True,
        verbose_name="任务状态",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="oa_decisions",
        verbose_name="处理人",
    )
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name="处理时间")
    comment = models.TextField(blank=True, verbose_name="审批意见")

    class Meta:
        ordering = ("instance", "position")
        verbose_name = "审批任务"
        verbose_name_plural = "审批任务"
        constraints = [
            models.UniqueConstraint(fields=("instance", "position"), name="oa_unique_task_step"),
            models.UniqueConstraint(
                fields=("instance",),
                condition=models.Q(status="pending"),
                name="oa_one_pending_task",
            ),
        ]

    def __str__(self):
        return f"{self.instance}: {self.name}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:workflowtask", args=[self.pk])

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source=TaskStatus.PENDING, target=TaskStatus.APPROVED)
    def approve(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source=TaskStatus.PENDING, target=TaskStatus.REJECTED)
    def reject(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source=TaskStatus.PENDING, target=TaskStatus.CANCELLED)
    def cancel(self, *, by, description=""):
        pass


class MaintenancePolicy(ChangeLoggedModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="配置名称")
    enabled = models.BooleanField(default=True, verbose_name="启用")
    is_default = models.BooleanField(default=False, verbose_name="默认流程配置")
    sla_hours = models.PositiveIntegerField(default=24, verbose_name="节点时限（小时）")
    warning_hours = models.PositiveIntegerField(default=4, verbose_name="提前提醒（小时）")

    class Meta:
        verbose_name = "维护流程配置"
        verbose_name_plural = "维护流程配置"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=("is_default",),
                condition=models.Q(is_default=True),
                name="oa_one_default_maintenance_policy",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:maintenancepolicy", args=[self.pk])

    def clean(self):
        super().clean()
        if self.is_default and not self.enabled:
            raise ValidationError("默认流程配置必须启用。")
        if not self.sla_hours or self.warning_hours >= self.sla_hours:
            raise ValidationError("时限必须大于零，提前提醒时间必须小于节点时限。")


class MaintenanceRole(ChangeLoggedModel):
    policy = models.ForeignKey(MaintenancePolicy, on_delete=models.CASCADE, related_name="roles")
    role = models.CharField(max_length=20, choices=ROLES)
    group = models.ForeignKey("users.Group", on_delete=models.PROTECT)

    def get_absolute_url(self):
        return self.policy.get_absolute_url()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("policy", "role"), name="oa_policy_role")]
        verbose_name = "维护审批角色"
        verbose_name_plural = "维护审批角色"


class MaintenanceNumberSequence(models.Model):
    _netbox_private = True
    year = models.PositiveIntegerField(primary_key=True)
    last_value = models.PositiveIntegerField(default=0)


class MaintenanceNumberReservation(models.Model):
    _netbox_private = True
    number = models.CharField(max_length=40, primary_key=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    used = models.BooleanField(default=False)


class MaintenanceChangeLogMixin:
    """Business actions are logged once through MaintenanceEvent, not per model save."""

    def to_objectchange(self, action):
        if action != "delete":
            return None
        # NetBox's pre_delete handler requires an ObjectChange rather than None.
        # Preserve its contract for direct administrative deletion of model objects.
        change = super().to_objectchange(action)
        if hasattr(self, "order_id"):
            change.related_object = self.order
        return change


class MaintenanceOrder(MaintenanceChangeLogMixin, WorkflowStateModel):
    number = models.CharField(max_length=40, unique=True, verbose_name="工单编号")
    policy = models.ForeignKey(MaintenancePolicy, on_delete=models.PROTECT, verbose_name="流程配置")
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="maintenance_requests",
        verbose_name="发起人",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="maintenance_orders",
        verbose_name="线路主管",
    )
    service_type = models.CharField(
        max_length=20,
        choices=(
            ("fiber", "光缆故障"),
            ("construction", "施工盯防"),
            ("air", "空调保养"),
            ("inspection", "巡检服务"),
            ("cutover", "割接盯防"),
            ("power", "供电故障"),
        ),
        verbose_name="服务项目",
    )
    priority = models.CharField(
        max_length=10,
        choices=(("high", "高"), ("medium", "中"), ("low", "低")),
        default="medium",
        verbose_name="优先级",
    )
    supplier = models.CharField(max_length=200, verbose_name="服务单位")
    contract_id = models.PositiveBigIntegerField(null=True, blank=True, verbose_name="外购合同 ID")
    contract_number = models.CharField(max_length=100, verbose_name="合同编号")
    service_region = models.CharField(max_length=200, verbose_name="服务省市")
    service_region_ids = models.JSONField(default=list, blank=True, verbose_name="服务地区 ID")
    service_location = models.CharField(max_length=200, verbose_name="服务位置")
    contact_phone = models.CharField(max_length=40, blank=True, verbose_name="联系电话")
    requirements = models.TextField(verbose_name="服务要求")
    fault_reference = models.CharField(max_length=200, blank=True, verbose_name="故障编号")
    status = FSMField(
        max_length=20,
        default="draft",
        protected=True,
        choices=(
            ("draft", "草稿"),
            ("active", "进行中"),
            ("completed", "已完成"),
            ("terminated", "已终止"),
            ("cancelled", "已撤销"),
        ),
        verbose_name="工单状态",
    )
    stage = models.CharField(
        max_length=20, choices=STAGES, default="ordering", verbose_name="当前阶段"
    )
    steps_snapshot = models.JSONField(default=list, blank=True)
    current_position = models.PositiveIntegerField(default=0)
    warning_hours = models.PositiveIntegerField(default=4)
    payment_status = models.CharField(
        max_length=30,
        default="draft",
        choices=(
            ("draft", "未提交"),
            ("submitted", "付款计划已提交"),
            ("requested", "付款申请已提交"),
        ),
        verbose_name="付款状态",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    followers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="followed_maintenance_orders"
    )
    reserved_groups = models.ManyToManyField("users.Group", through="MaintenanceGroupReservation")

    class Meta:
        ordering = ("-created", "-pk")
        verbose_name = "维护服务工单"
        verbose_name_plural = "维护服务工单"

    def __str__(self):
        return self.number

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:maintenanceorder", args=[self.pk])

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="draft", target="active")
    def start(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="active", target="completed")
    def finish(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="active", target="terminated")
    def terminate(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="draft", target="cancelled")
    def withdraw(self, *, by, description=""):
        pass


class MaintenanceTask(MaintenanceChangeLogMixin, WorkflowStateModel):
    order = models.ForeignKey(MaintenanceOrder, on_delete=models.PROTECT, related_name="tasks")
    position = models.PositiveIntegerField()
    name = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=20,
        choices=(
            ("submission", "填报"),
            ("approval", "审核"),
            ("action", "办理"),
            ("external", "外部流程"),
            ("countersign", "加签"),
        ),
    )
    assigned_group = models.ForeignKey(
        "users.Group", on_delete=models.PROTECT, null=True, blank=True
    )
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="maintenance_tasks",
    )
    status = FSMField(
        max_length=20,
        default="pending",
        protected=True,
        choices=(
            ("pending", "待处理"),
            ("completed", "已完成"),
            ("returned", "已退回"),
            ("cancelled", "已取消"),
        ),
    )
    deadline = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="maintenance_decisions",
    )
    comment = models.TextField(blank=True)
    external_reference = models.CharField(max_length=200, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    is_countersign = models.BooleanField(default=False)

    class Meta:
        ordering = ("pk",)
        verbose_name = "维护办理任务"
        verbose_name_plural = "维护办理任务"
        constraints = [
            models.UniqueConstraint(
                fields=("order",),
                condition=models.Q(status="pending"),
                name="oa_one_maintenance_pending",
            )
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return self.order.get_absolute_url()

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="pending", target="completed")
    def complete(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="pending", target="returned")
    def send_back(self, *, by, description=""):
        pass

    @fsm_log_by
    @fsm_log_description
    @transition(field=status, source="pending", target="cancelled")
    def cancel(self, *, by, description=""):
        pass


class MaintenanceSavedSignature(models.Model):
    _netbox_private = True
    signer = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    image = models.BinaryField(editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class MaintenanceSignature(models.Model):
    _netbox_private = True
    key = models.UUIDField(default=uuid4, unique=True, editable=False)
    task = models.ForeignKey(
        MaintenanceTask, on_delete=models.PROTECT, related_name="signatures", null=True, blank=True
    )
    document = models.ForeignKey("MaintenanceDocument", on_delete=models.PROTECT)
    signer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    image = models.BinaryField(default=bytes, blank=True, editable=False)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    cancelled = models.BooleanField(default=False)


class MaintenanceGroupReservation(models.Model):
    _netbox_private = True
    order = models.ForeignKey(MaintenanceOrder, on_delete=models.CASCADE)
    group = models.ForeignKey("users.Group", on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("order", "group"), name="oa_order_group")]


class MaintenanceDocument(MaintenanceChangeLogMixin, ChangeLoggedModel):
    order = models.ForeignKey(MaintenanceOrder, on_delete=models.PROTECT, related_name="documents")
    material = models.CharField(max_length=20, choices=MATERIALS)
    version = models.PositiveIntegerField()
    item_number = models.PositiveIntegerField(default=0, editable=False)
    removed_at = models.DateTimeField(null=True, blank=True, editable=False)
    replaces_document = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="replacement_documents", editable=False,
    )
    file = models.FileField(
        storage=MaintenanceDocumentStorage(), upload_to="maintenance/%Y/%m/", blank=True
    )
    kind = models.CharField(
        max_length=20,
        default="attachment",
        choices=(("form", "填写表单"), ("attachment", "上传附件"), ("scan", "扫描件")),
    )
    form_data = models.JSONField(default=dict, blank=True)
    source_document = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="derived_documents"
    )
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_task = models.ForeignKey(
        "MaintenanceTask",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="uploaded_documents",
    )
    is_sealed = models.BooleanField(default=False, editable=False)
    state = models.CharField(
        max_length=20,
        default="electronic",
        choices=(("electronic", "电子版"), ("scan", "扫描版")),
    )
    note = models.TextField(blank=True)

    def get_absolute_url(self):
        return reverse("plugins:netbox_oa:maintenance_download", args=[self.pk])

    def __str__(self):
        return f"{self.order.number} · {self.short_name}"

    @property
    def display_name(self):
        if self.kind == "form":
            return self.original_name
        return f"{self.order.number}_{self.short_name}"

    @property
    def is_direct_scan(self):
        """Payment evidence is uploaded directly, without stamping approval."""
        return self.material in {"notice", "invoice", "contract", "verification"}

    @property
    def short_name(self):
        if self.kind == "form":
            return self.original_name
        from pathlib import Path

        label = dict(MATERIALS)[self.material]
        state = (
            "扫描件" if self.is_direct_scan else
            "用印后" if self.is_sealed else "审批件" if self.kind == "scan" else "附件"
        )
        number = f"_{self.item_number:02d}" if self.item_number else ""
        return (
            f"{label}{number}_{state}_v{self.version}"
            f"{Path(self.original_name).suffix.lower()}"
        )

    class Meta:
        ordering = ("material", "-version")
        verbose_name = "维护工单材料"
        verbose_name_plural = "维护工单材料"
        constraints = [
            models.UniqueConstraint(
                fields=("order", "material", "version"), name="oa_document_version"
            )
        ]


class MaintenanceEvent(ChangeLoggedModel):
    order = models.ForeignKey(MaintenanceOrder, on_delete=models.PROTECT, related_name="events")
    task = models.ForeignKey(MaintenanceTask, on_delete=models.PROTECT, null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=30)
    description = models.TextField()
    details = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.order.number} · {self.operation_summary}"

    AUDIT_OPERATIONS = {
        "create": "创建流程",
        "start": "批转流程",
        "complete": "批转流程",
        "return": "批转流程",
        "transfer": "批转流程",
        "countersign": "批转流程",
        "view": "打开流程（查阅流程）",
        "form": "保存表单",
        "edit": "保存表单",
        "save": "保存表单",
        "print": "打印表单",
        "upload": "上传材料",
        "remove_material": "删除材料",
    }

    @property
    def operation_summary(self):
        return self.AUDIT_OPERATIONS.get(self.action, self.description)[:200]

    def to_objectchange(self, action):
        if action == "delete":
            return super().to_objectchange(action)
        if (action != "create" or self.action not in self.AUDIT_OPERATIONS
                or not self.details.get("audit", True)):
            return None
        change = super().to_objectchange(action)
        change.related_object = self.order
        change.message = self.operation_summary
        change.object_repr = f"{self.order.number} · {self.operation_summary}"[:200]
        # Keep the global audit concise. Full comments and workflow evidence remain
        # in the existing business history instead of being duplicated in ObjectChange.
        change.postchange_data = {
            "order": self.order_id, "number": self.order.number,
            "operation": self.operation_summary, "action": self.action,
        }
        if self.task_id:
            change.postchange_data["task"] = self.task_id
        for key in ("document_id", "material", "version", "item_number", "target_user"):
            if self.details.get(key) is not None:
                change.postchange_data[key] = self.details[key]
        return change

    def get_absolute_url(self):
        return self.order.get_absolute_url()

    class Meta:
        ordering = ("-created", "-pk")
        verbose_name = "维护工单操作记录"
        verbose_name_plural = "维护工单操作记录"


class MaintenanceLink(MaintenanceChangeLogMixin, ChangeLoggedModel):
    order = models.ForeignKey(MaintenanceOrder, on_delete=models.PROTECT, related_name="links")
    object_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    object = GenericForeignKey("object_type", "object_id")
    label = models.CharField(max_length=200)

    def get_absolute_url(self):
        return self.order.get_absolute_url()

    class Meta:
        verbose_name = "维护工单关联对象"
        verbose_name_plural = "维护工单关联对象"
        constraints = [
            models.UniqueConstraint(
                fields=("order", "object_type", "object_id"), name="oa_order_link"
            )
        ]
