import django_tables2 as tables
from django.db.models import OuterRef, Subquery
from netbox.tables import NetBoxTable

from .contract_views import with_payment_progress
from .models import MaintenanceDocument, MaintenanceOrder


class ContractOrderTable(NetBoxTable):
    number = tables.Column(verbose_name="工单编号", linkify=False)
    service_type = tables.Column(verbose_name="服务类型")
    requester = tables.Column(verbose_name="发起人", linkify=False)
    status = tables.Column(verbose_name="状态")
    stage = tables.Column(verbose_name="当前阶段")
    acceptance_amount = tables.TemplateColumn(
        template_code=(
            '{% if record.acceptance_removed_at == None and '
            'record.acceptance_form_data.settlement_amount != None and '
            'record.acceptance_form_data.settlement_amount != "" %}'
            '{{ record.acceptance_form_data.settlement_amount|floatformat:"2g" }}'
            '{% else %}—{% endif %}'
        ),
        verbose_name="验收单支付金额（元）", orderable=False, empty_values=(),
        attrs={"td": {"class": "text-start text-nowrap"}},
    )
    payment_progress = tables.TemplateColumn(
        template_name="netbox_oa/contract_order_payment_progress.html",
        verbose_name="付款情况", orderable=False, empty_values=(),
    )
    created = tables.DateTimeColumn(
        verbose_name="创建时间", format="Y-m-d H:i:s", order_by=("created", "pk")
    )
    actions = tables.TemplateColumn(
        template_name="netbox_oa/contract_order_actions.html",
        verbose_name="", orderable=False,
        attrs={"td": {"class": "text-end text-nowrap noprint p-1"}},
    )

    class Meta(NetBoxTable.Meta):
        model = MaintenanceOrder
        fields = (
            "number", "service_type", "requester", "status", "stage",
            "acceptance_amount", "payment_progress", "created", "actions",
        )
        default_columns = fields
        exclude = ("pk", "id")
        empty_text = "暂无可查看的关联工单"
        order_by = ("-created",)


def contract_order_table(request, orders):
    acceptance = MaintenanceDocument.objects.restrict(request.user, "view").filter(
        order_id=OuterRef("pk"), material="acceptance", kind="form",
    ).order_by("-version", "-pk")
    orders = orders.annotate(
        acceptance_form_data=Subquery(acceptance.values("form_data")[:1]),
        acceptance_removed_at=Subquery(acceptance.values("removed_at")[:1]),
    )
    table = ContractOrderTable(with_payment_progress(orders, request.user), prefix="oa_orders-")
    # Keep this embedded list fixed and read-only, including when custom columns exist.
    for name in list(table.columns.names()):
        if name not in table.Meta.fields:
            table.columns.hide(name)
    tables.RequestConfig(request, paginate={"per_page": 25}).configure(table)
    return table
