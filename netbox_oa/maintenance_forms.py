from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from users.models import Group

from .maintenance_flow import ROLES
from .maintenance_materials import SERVICE_ITEMS
from .models import MaintenanceDocument, MaintenanceOrder, MaintenancePolicy


class StyledForm:
    def style_fields(self):
        for field in self.fields.values():
            field.widget.attrs["class"] = (
                "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            )
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"


class MaintenancePolicyForm(StyledForm, forms.Form):
    name = forms.CharField(label="配置名称", max_length=100)
    enabled = forms.BooleanField(label="启用此配置", required=False, initial=True)
    sla_hours = forms.IntegerField(label="节点时限（小时）", min_value=1, initial=24)
    warning_hours = forms.IntegerField(label="提前提醒（小时）", min_value=0, initial=4)

    def __init__(self, *args, policy=None, **kwargs):
        initial = kwargs.setdefault("initial", {})
        if policy:
            initial.update(
                name=policy.name,
                sla_hours=policy.sla_hours,
                warning_hours=policy.warning_hours,
                enabled=policy.enabled,
            )
            initial.update(dict(policy.roles.values_list("role", "group_id")))
        super().__init__(*args, **kwargs)
        for role, label in ROLES:
            self.fields[role] = forms.ModelChoiceField(label=label, queryset=Group.objects.all())
        self.style_fields()


class MaintenanceOrderForm(StyledForm, forms.ModelForm):
    class Meta:
        model = MaintenanceOrder
        fields = (
            "title",
            "policy",
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
        )

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["policy"].queryset = MaintenancePolicy.objects.restrict(actor, "view").filter(
            enabled=True
        )
        self.fields["owner"].queryset = get_user_model().objects.filter(is_active=True)
        self.fields["owner"].initial = actor.pk
        self.style_fields()


class MaintenanceOrderEditForm(MaintenanceOrderForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("policy")
        self.fields.pop("owner")


class MaintenanceActionForm(StyledForm, forms.Form):
    comment = forms.CharField(
        label="处理意见或办理凭证说明",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    external_reference = forms.CharField(label="EIP / 财务业务单号", max_length=200, required=False)
    payment_amount = forms.DecimalField(
        label="付款金额", max_digits=14, decimal_places=2, required=False
    )
    period_start = forms.DateField(
        label="周期开始", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    period_end = forms.DateField(
        label="周期结束", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    user_id = forms.ModelChoiceField(
        label="转办 / 加签人员",
        required=False,
        queryset=get_user_model().objects.filter(is_active=True),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class MaintenanceDocumentForm(StyledForm, forms.ModelForm):
    source_document_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = MaintenanceDocument
        fields = ("material", "file", "state", "note")
        labels = {"material": "材料类型", "file": "材料文件", "state": "材料形态", "note": "备注"}

    def __init__(self, *args, scan=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["state"].choices = [
            choice
            for choice in self.fields["state"].choices
            if choice[0] in ({"paper", "sealed"} if scan else {"electronic", "awaiting_paper"})
        ]
        if scan:
            self.fields["source_document_id"].required = True
            self.fields["state"].initial = "sealed"
            self.fields["file"].label = "扫描件文件"
            self.fields["file"].widget.attrs["accept"] = ".pdf,.png,.jpg,.jpeg"
        self.fields["file"].required = True
        self.fields["note"].widget.attrs["rows"] = 2
        self.style_fields()


class MaintenanceCompanyForm(StyledForm, forms.Form):
    company_name = forms.CharField(label="甲方单位", max_length=200)
    requested_at = forms.DateTimeField(
        label="发起时间",
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
    )
    applicant = forms.CharField(label="发起人员", max_length=150)
    contact_phone = forms.CharField(label="联系电话", max_length=40, required=False)
    contract_number = forms.CharField(label="合同编号", max_length=100)
    supplier = forms.CharField(label="服务单位", max_length=200)
    service_region = forms.CharField(label="服务省市", max_length=200)
    service_location = forms.CharField(label="服务位置", max_length=200)
    service_type = forms.ChoiceField(label="服务项目（单选）", choices=SERVICE_ITEMS)
    requirements = forms.CharField(
        label="服务要求", max_length=10000, widget=forms.Textarea(attrs={"rows": 6})
    )

    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, **kwargs)
        if material == "report":
            self.fields.pop("requirements")
            self.fields.pop("contact_phone")
            self.fields["requested_at"].label = "申请时间"
            self.fields["applicant"].label = "申请人员"
            self.fields["supplier"].label = "维护单位"
        self.style_fields()


class MaintenanceLinkForm(StyledForm, forms.Form):
    object_type = forms.ModelChoiceField(label="业务对象类型", queryset=ContentType.objects.all())
    object_id = forms.IntegerField(label="业务对象 ID", min_value=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["object_type"].queryset = ContentType.objects.filter(
            pk__in=[
                ct.pk
                for ct in ContentType.objects.all()
                if ct.model_class() and hasattr(ct.model_class().objects, "restrict")
            ]
        )
        self.style_fields()
