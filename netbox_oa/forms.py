from django import forms

from .models import WorkflowDefinition, WorkflowInstance


class WorkflowDefinitionForm(forms.ModelForm):
    class Meta:
        model = WorkflowDefinition
        fields = ("name", "slug", "description", "enabled", "steps")


class WorkflowInstanceForm(forms.ModelForm):
    class Meta:
        model = WorkflowInstance
        fields = ("title", "definition", "payload")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["definition"].queryset = WorkflowDefinition.objects.restrict(
            user, "view"
        ).filter(enabled=True)
