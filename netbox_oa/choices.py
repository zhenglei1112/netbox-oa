from django.db import models


class WorkflowStatus(models.TextChoices):
    DRAFT = "draft", "草稿"
    PENDING = "pending", "审批中"
    APPROVED = "approved", "已通过"
    REJECTED = "rejected", "已拒绝"
    CANCELLED = "cancelled", "已撤销"


class TaskStatus(models.TextChoices):
    PENDING = "pending", "待审批"
    APPROVED = "approved", "已通过"
    REJECTED = "rejected", "已拒绝"
    CANCELLED = "cancelled", "已撤销"
