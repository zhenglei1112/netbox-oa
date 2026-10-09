from django import forms

from .models import WorkflowDefinition, WorkflowInstance


class WorkflowDefinitionForm(forms.ModelForm):
    class Meta:
        model = WorkflowDefinition
        fields = ("name", "slug", "description", "enabled", "steps")
        labels = {
            "name": "流程名称",
            "slug": "流程标识",
            "description": "描述说明",
            "enabled": "是否启用",
            "steps": "审批节点 (JSON)",
        }
        help_texts = {
            "steps": '按顺序执行的节点数组，例如：[{"name": "部门审批", "group_id": 1}]',
        }


class WorkflowInstanceForm(forms.ModelForm):
    class Meta:
        model = WorkflowInstance
        fields = ("title", "definition", "payload")
        labels = {
            "title": "申请标题",
            "definition": "流程定义",
            "payload": "业务数据 (JSON)",
        }
        help_texts = {
            "payload": '选填，业务参数 JSON 对象，例如：{"device_id": 42}',
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition"].queryset = WorkflowDefinition.objects.restrict(
            user, "view"
        ).filter(enabled=True)
