import django_tables2 as tables
from netbox.tables import NetBoxTable

from .models import MaintenanceOrder


class ContractOrderTable(NetBoxTable):
    number = tables.Column(verbose_name="工单编号", linkify=False)
    service_type = tables.Column(verbose_name="服务类型")
    requester = tables.Column(verbose_name="发起人", linkify=False)
    status = tables.Column(verbose_name="状态")
    stage = tables.Column(verbose_name="当前阶段")
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
        fields = ("number", "service_type", "requester", "status", "stage", "created", "actions")
        default_columns = fields
        exclude = ("pk", "id")
        empty_text = "暂无可查看的关联工单"
        order_by = ("-created",)


def contract_order_table(request, orders):
    table = ContractOrderTable(orders, prefix="oa_orders-")
    # Keep this embedded list fixed and read-only, including when custom columns exist.
    for name in list(table.columns.names()):
        if name not in table.Meta.fields:
            table.columns.hide(name)
    tables.RequestConfig(request, paginate={"per_page": 25}).configure(table)
    return table
