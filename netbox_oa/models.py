from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django_fsm import FSMField, transition
from django_fsm_log.decorators import fsm_log_by, fsm_log_description
from netbox.models import ChangeLoggedModel

from .choices import TaskStatus, WorkflowStatus
from .validators import validate_payload, validate_steps


class WorkflowDefinition(ChangeLoggedModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    enabled = models.BooleanField(default=True)
    steps = models.JSONField(default=list, validators=[validate_steps])

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
    """Validate protected FSM fields without Django assigning their value again."""

    class Meta:
        abstract = True

    def clean_fields(self, exclude=None):
        excluded = set(exclude or ())
        super().clean_fields(exclude=excluded | {"status"})
        if "status" not in excluded:
            try:
                self._meta.get_field("status").clean(self.status, self)
            except ValidationError as exc:
                raise ValidationError({"status": exc.messages}) from exc


class WorkflowInstance(WorkflowStateModel):
    title = models.CharField(max_length=200)
    definition = models.ForeignKey(
        WorkflowDefinition, on_delete=models.PROTECT, related_name="instances"
    )
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="oa_requests"
    )
    status = FSMField(
        max_length=20, default=WorkflowStatus.DRAFT, choices=WorkflowStatus, protected=True
    )
    payload = models.JSONField(default=dict, blank=True, validators=[validate_payload])
    steps_snapshot = models.JSONField(default=list, blank=True)
    current_step = models.PositiveIntegerField(default=0)
    approval_groups = models.ManyToManyField(
        "users.Group", through="WorkflowGroupReservation", related_name="oa_instances"
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
    instance = models.ForeignKey(WorkflowInstance, on_delete=models.PROTECT, related_name="tasks")
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField()
    assigned_group = models.ForeignKey(
        "users.Group", on_delete=models.PROTECT, related_name="oa_tasks"
    )
    status = FSMField(max_length=20, default=TaskStatus.PENDING, choices=TaskStatus, protected=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="oa_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True)

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
