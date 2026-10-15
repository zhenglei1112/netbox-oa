from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from users.models import Group

from .maintenance_contracts import (
    contract_values,
    purchase_contracts,
    region_ids_from_names,
    selected_contract,
    service_regions,
)
from .maintenance_flow import FLOW_NAME, ROLES
from .maintenance_materials import SERVICE_ITEMS
from .maintenance_numbering import issue_number, number_from_token
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
    name = forms.CharField(label="配置名称", max_length=100, initial=FLOW_NAME)
    enabled = forms.BooleanField(label="启用此配置", required=False, initial=True)
    is_default = forms.BooleanField(label="设为默认流程配置", required=False)
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
                is_default=policy.is_default,
            )
            initial.update(dict(policy.roles.values_list("role", "group_id")))
        super().__init__(*args, **kwargs)
        for role, label in ROLES:
            self.fields[role] = forms.ModelChoiceField(label=label, queryset=Group.objects.all())
        self.style_fields()


class ContractFieldsMixin:
    def setup_contract_fields(self, actor):
        self.actor = actor
        self.contract_error = ""
        try:
            contracts = list(purchase_contracts(actor))
        except ValidationError as exc:
            contracts = []
            self.contract_error = "; ".join(exc.messages)
        self.fields["contract_id"] = forms.TypedChoiceField(
            label="合同编号（外购合同）",
            coerce=int,
            choices=[("", "请选择外购合同")]
            + [(item.pk, f"{item.number or '未填写编号'} · {item.name}") for item in contracts],
            help_text=self.contract_error,
        )
        self.contract_options = {str(item.pk): contract_values(item, actor) for item in contracts}
        self.fields["supplier"].required = False
        self.fields["supplier"].widget.attrs["readonly"] = True
        self.fields["supplier"].help_text = "选择外购合同后自动带入"
        self.fields["service_region"] = forms.ModelMultipleChoiceField(
            label="服务省市",
            queryset=service_regions(actor),
            help_text="从 NetBox 地区中选择，可多选；合同自动带入后仍可调整。",
        )
        if not self.is_bound:
            instance = getattr(self, "instance", None)
            ids = self.initial.get("service_region_ids") or getattr(
                instance, "service_region_ids", []
            )
            names = self.initial.get("service_region") or getattr(instance, "service_region", "")
            self.initial["service_region"] = ids or region_ids_from_names(actor, names)
            if ids or names:
                self.fields["service_region"].widget.attrs["data-oa-preserve-empty"] = "1"
            if names and not self.initial["service_region"]:
                self.fields["service_region"].help_text += f" 原省市：{names}；请重新选择地区。"
        else:
            self.fields["service_region"].widget.attrs["data-oa-preserve-empty"] = "1"

    def clean(self):
        data = super().clean()
        if "contract_id" in self.fields:
            if self.contract_error:
                raise ValidationError(self.contract_error)
            if data.get("contract_id") and "service_region" in data:
                data.update(
                    selected_contract(
                        self.actor,
                        data["contract_id"],
                        service_region_ids=[item.pk for item in data["service_region"]],
                    )
                )
        return data


class MaintenanceOrderForm(ContractFieldsMixin, StyledForm, forms.ModelForm):
    number = forms.CharField(label="工单编号", disabled=True, required=False)
    number_token = forms.CharField(widget=forms.HiddenInput)

    class Meta:
        model = MaintenanceOrder
        fields = (
            "policy",
            "service_type",
            "priority",
            "contract_id",
            "supplier",
            "service_region",
            "service_location",
            "contact_phone",
            "requirements",
        )

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["policy"].queryset = MaintenancePolicy.objects.restrict(actor, "view").filter(
            enabled=True
        )
        if not self.is_bound and not self.instance.pk and not self.initial.get("policy"):
            policies = self.fields["policy"].queryset
            default = policies.filter(is_default=True).first()
            if default is None:
                available = list(policies[:2])
                default = available[0] if len(available) == 1 else None
            if default:
                self.initial["policy"] = default.pk
        self.setup_contract_fields(actor)
        if self.instance.pk:
            self.initial["number"] = self.instance.number
            self.fields.pop("number_token")
        elif self.is_bound:
            try:
                self.initial["number"] = number_from_token(self.data.get("number_token"), actor)
            except ValidationError:
                self.initial["number"] = ""
        else:
            try:
                self.initial["number"], self.initial["number_token"] = issue_number(actor)
            except ValidationError as exc:
                self.fields["number"].help_text = "; ".join(exc.messages)
        self.order_fields(["number", *self._meta.fields, "number_token"])
        self.style_fields()

    def clean(self):
        data = super().clean()
        data.pop("number", None)
        if not self.instance.pk and data.get("number_token"):
            number_from_token(data["number_token"], self.actor)
        return data


class MaintenanceOrderEditForm(MaintenanceOrderForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("policy")


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
    period_start = forms.CharField(
        label="结算期起始日期",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "YYYY-MM-DD 或 YYYY-MM"}),
    )
    period_end = forms.CharField(
        label="结算期结束日期",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "YYYY-MM-DD 或 YYYY-MM"}),
    )
    settlement_period_mode = forms.ChoiceField(
        label="结算期填写方式",
        choices=(("day", "按日"), ("month", "按月")),
        required=False,
        initial="day",
        help_text="按日填写 YYYY-MM-DD；按月填写 YYYY-MM，按月初至月末结算。",
    )
    user_id = forms.ModelChoiceField(
        label="转办 / 加签人员",
        required=False,
        queryset=get_user_model().objects.filter(is_active=True),
    )

    def __init__(self, *args, order=None, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class MultipleScanInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleScanField(forms.FileField):
    widget = MultipleScanInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super(MultipleScanField, self).clean(file, initial) for file in files]


class MaintenanceScanUploadForm(StyledForm, forms.Form):
    file = MultipleScanField(
        label="扫描版服务报告（可多选）",
        widget=MultipleScanInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"}),
    )
    note = forms.CharField(label="备注", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, material_name="服务报告", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].label = f"扫描版{material_name}（可多选）"
        self.style_fields()


class MaintenanceDocumentForm(StyledForm, forms.ModelForm):
    source_document_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = MaintenanceDocument
        fields = ("material", "file", "state", "note")
        labels = {"material": "材料类型", "file": "材料文件", "state": "材料形态", "note": "备注"}

    def __init__(self, *args, scan=False, **kwargs):
        if args and args[0] is not None:
            data = args[0].copy()
            data["state"] = {"paper": "scan", "sealed": "scan", "awaiting_paper": "electronic"}.get(
                data.get("state"), data.get("state")
            )
            args = (data, *args[1:])
        super().__init__(*args, **kwargs)
        self.fields["state"].choices = [
            choice
            for choice in self.fields["state"].choices
            if choice[0] in ({"scan"} if scan else {"electronic"})
        ]
        if scan:
            self.fields["source_document_id"].required = True
            self.fields["state"].initial = "scan"
            self.fields["file"].label = "扫描件文件"
            self.fields["file"].widget.attrs["accept"] = ".pdf,.png,.jpg,.jpeg"
        self.fields["file"].required = True
        self.fields["note"].widget.attrs["rows"] = 2
        self.style_fields()


class MaintenanceCompanyForm(ContractFieldsMixin, StyledForm, forms.Form):
    company_name = forms.CharField(label="甲方单位", max_length=200)
    requested_at = forms.DateTimeField(
        label="发起日期",
        widget=forms.DateTimeInput(format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}),
    )
    applicant = forms.CharField(label="发起人", max_length=150)
    contact_phone = forms.CharField(label="联系电话", max_length=40, required=False)
    contract_number = forms.CharField(label="合同编号", max_length=100)
    supplier = forms.CharField(label="服务单位", max_length=200)
    service_region = forms.CharField(label="服务省市", max_length=200)
    service_location = forms.CharField(label="服务位置", max_length=200)
    service_type = forms.ChoiceField(label="服务项目（单选）", choices=SERVICE_ITEMS)
    requirements = forms.CharField(
        label="服务要求", max_length=10000, widget=forms.Textarea(attrs={"rows": 6})
    )
    signature = forms.CharField(label="签字", disabled=True, required=False)
    signature_date = forms.DateField(label="日期", disabled=True, required=False)

    def __init__(self, *args, material, actor, **kwargs):
        super().__init__(*args, **kwargs)
        if material == "report":
            self.fields.pop("requirements")
            self.fields.pop("contact_phone")
            self.fields["requested_at"].label = "申请时间"
            self.fields["applicant"].label = "申请人员"
            self.fields["supplier"].label = "维护单位"
            self.fields.pop("signature")
            self.fields.pop("signature_date")
        else:
            self.fields["requested_at"].widget = forms.DateInput(
                format="%Y-%m-%d", attrs={"type": "date"}
            )
            self.fields.pop("contract_number")
            self.setup_contract_fields(actor)
            self.order_fields(
                [
                    "company_name",
                    "requested_at",
                    "applicant",
                    "contact_phone",
                    "contract_id",
                    "supplier",
                    "service_region",
                    "service_location",
                    "service_type",
                    "requirements",
                    "signature",
                    "signature_date",
                ]
            )
            self.fields["applicant"].disabled = True
            self.fields["applicant"].initial = actor.get_full_name() or actor.get_username()
            self.initial["applicant"] = self.fields["applicant"].initial
            self.initial["signature"] = ""
            self.initial["signature_date"] = None
            self.fields["signature"].help_text = "请在发起工单环节完成申请人手写签名"
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
