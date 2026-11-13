from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from netbox.forms.mixins import ChangelogMessageMixin
from users.models import Group

from .maintenance_contracts import (
    contract_values,
    purchase_contracts,
    region_ids_from_names,
    selected_contract,
    service_regions,
)
from .maintenance_flow import FLOW_NAME, MATERIALS, ROLES
from .maintenance_materials import (
    SERVICE_EFFECTS,
    SERVICE_ITEMS,
    company_form_initial,
    service_type_values,
)
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
        self.fields["service_region"] = forms.ModelMultipleChoiceField(
            label="服务省市",
            queryset=service_regions(actor),
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


class ServiceItemsField(forms.MultipleChoiceField):
    def __init__(self, **kwargs):
        super().__init__(label="服务项目", choices=SERVICE_ITEMS, **kwargs)

    def to_python(self, value):
        if isinstance(value, str):
            value = service_type_values(value)
        return super().to_python(value)


class MaintenanceOrderForm(ContractFieldsMixin, StyledForm, ChangelogMessageMixin, forms.ModelForm):
    service_type = ServiceItemsField()
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
        self.initial["service_type"] = service_type_values(self.initial.get("service_type"))
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
        self.fields["number"].widget.attrs["data-number-base"] = (
            self.instance.number_reservation_id
            if self.instance.pk else self.initial.get("number", "")
        )
        if not self.fields["number"].help_text:
            self.fields["number"].help_text = (
                "草稿编号随合同类型调整，首次提交审批后固定。"
                if not self.instance.pk or self.instance.status == "draft"
                else "编号中的合同类型以首次提交审批时为准。"
            )
        self.fields["number"].widget.attrs["data-number-fixed"] = (
            "1" if self.instance.pk and self.instance.status != "draft" else "0"
        )
        self.style_fields()

    def clean(self):
        data = super().clean()
        data.pop("number", None)
        data.pop("contract_type_snapshot", None)
        if not self.instance.pk and data.get("number_token"):
            number_from_token(data["number_token"], self.actor)
        return data


class MaintenanceOrderEditForm(MaintenanceOrderForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("policy")


class MaintenanceActionForm(StyledForm, forms.Form):
    comment = forms.CharField(
        label="处理意见",
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    external_reference = forms.CharField(label="EIP / 财务业务单号", max_length=200, required=False)
    user_id = forms.ModelChoiceField(
        label="转办 / 加签人员",
        required=False,
        queryset=get_user_model().objects.filter(is_active=True),
    )

    def __init__(self, *args, order=None, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if order:
            current = order.tasks.filter(status="pending").first()
            if current and order.steps_snapshot[current.position]["key"] == "payment_eip":
                self.fields["external_reference"].label = "外部付款申请单号"
            if current and current.kind in {"approval", "countersign"}:
                self.fields["user_id"].queryset = self.fields["user_id"].queryset.exclude(
                    pk=order.requester_id
                )
        self.style_fields()


class MultipleUploadInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleUploadField(forms.FileField):
    widget = MultipleUploadInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super(MultipleUploadField, self).clean(file, initial) for file in files]


class MaintenanceScanUploadForm(StyledForm, forms.Form):
    file = MultipleUploadField(
        label="扫描版服务报告",
        widget=MultipleUploadInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"}),
    )
    note = forms.CharField(label="备注", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, material_name="服务报告", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["file"].label = f"扫描版{material_name}"
        self.style_fields()

class UploadSourceSelect(forms.Select):
    def value_from_datadict(self, data, files, name):
        return data.getlist(name)


class MaintenanceMaterialUploadForm(MaintenanceScanUploadForm):
    upload_mode = forms.ChoiceField(
        label="本次操作", choices=(("new", "新增材料"), ("replace", "更新已有材料")),
        required=False, initial="new",
    )
    replaces_document_ids = forms.TypedMultipleChoiceField(
        label="更新哪份材料", coerce=int, required=False, widget=UploadSourceSelect,
    )
    source_document_ids = forms.TypedMultipleChoiceField(
        label="对应原件", coerce=int, required=False, widget=UploadSourceSelect,
    )

    def __init__(
        self, *args, scan=False, source_choices=(), requires_source=False,
        revision_choices=(), allow_revision=False, multiple=True, **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.requires_source = requires_source
        self.allow_revision = allow_revision
        self.multiple = multiple
        self.fields["replaces_document_ids"].choices = revision_choices
        self.fields["replaces_document_ids"].widget.attrs["disabled"] = True
        self.fields["source_document_ids"].choices = source_choices
        self.fields["source_document_ids"].required = requires_source
        if not scan:
            self.fields["file"].label = "材料文件（可多选）"
            self.fields["file"].widget.attrs["accept"] = ".pdf,.docx,.xlsx,.png,.jpg,.jpeg"
        if not multiple:
            self.fields["file"].widget = forms.ClearableFileInput(attrs={
                "accept": self.fields["file"].widget.attrs.get("accept", ""),
            })
        self.style_fields()

    def clean(self):
        data = super().clean()
        if not self.multiple and len(self.files.getlist("file")) > 1:
            self.add_error("file", "每次只能上传一个文件。")
        if self.allow_revision:
            data["upload_mode"] = data.get("upload_mode") or "new"
            replacements = data.get("replaces_document_ids", [])
            if data["upload_mode"] == "replace":
                if len(replacements) != len(data.get("file", [])) or not replacements:
                    self.add_error("replaces_document_ids", "请为每个文件选择要更新的材料。")
                elif len(replacements) != len(set(replacements)):
                    self.add_error("replaces_document_ids", "同一份材料不能重复更新。")
            elif replacements:
                self.add_error("replaces_document_ids", "新增材料不能指定更新对象。")
        if self.requires_source and "file" in data and "source_document_ids" in data:
            if len(data["file"]) != len(data["source_document_ids"]):
                self.add_error("source_document_ids", "请为每个文件选择对应原件。")
            elif len(data["source_document_ids"]) != len(set(data["source_document_ids"])):
                self.add_error("source_document_ids", "同一审批件不能重复关联用印文件。")
        return data


class MaintenanceDocumentForm(StyledForm, forms.ModelForm):
    source_document_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = MaintenanceDocument
        fields = ("material", "file", "state", "note")
        labels = {"material": "材料类型", "file": "材料文件", "state": "材料形态", "note": "备注"}

    def __init__(self, *args, scan=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["material"].choices = MATERIALS
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

    def clean_file(self):
        if len(self.files.getlist("file")) > 1:
            raise forms.ValidationError("每次只能上传一个文件。")
        return self.cleaned_data["file"]


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
    service_type = ServiceItemsField()
    requirements = forms.CharField(
        label="服务要求", max_length=10000, widget=forms.Textarea(attrs={"rows": 6})
    )
    signature = forms.CharField(label="签字", disabled=True, required=False)
    signature_date = forms.DateField(label="日期", disabled=True, required=False)

    def __init__(self, *args, material, actor, order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["service_type"] = service_type_values(self.initial.get("service_type"))
        if material == "acceptance":
            self.fields.pop("signature")
            self.fields.pop("signature_date")
            for name, value in company_form_initial(order).items():
                if name in self.fields:
                    self.fields[name].disabled = True
                    self.initial[name] = value
            self.fields["service_effect"] = forms.ChoiceField(
                label="验收结果", choices=(("", "请选择"), *SERVICE_EFFECTS),
                error_messages={"required": "请选择验收结果。",
                                "invalid_choice": "请选择有效的验收结果。"},
                widget=forms.Select(attrs={"data-required-message": "请选择验收结果。"}),
            )
            self.fields["settlement_amount"] = forms.DecimalField(
                label="结算金额（元）", max_digits=14, decimal_places=2, min_value=0,
                widget=forms.NumberInput(attrs={"step": "0.01"}),
            )
            from .maintenance_billing import configured, summary

            if configured(order):
                self.fields["settlement_amount"].disabled = True
                self.initial["settlement_amount"] = summary(order)["total"]
                self.fields["settlement_amount"].help_text = (
                    "自动汇总结算费用明细"
                )
        elif material == "report":
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
