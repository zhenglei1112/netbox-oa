from netbox.plugins import PluginTemplateExtension

from .contract_tables import contract_order_table
from .contract_views import contract_orders


class ContractOrders(PluginTemplateExtension):
    models = ["netbox_contract.contract"]

    def right_page(self):
        request = self.context["request"]
        if not request.user.has_perm("netbox_oa.view_maintenanceorder"):
            return ""
        table = contract_order_table(
            request, contract_orders(request.user, self.context["object"].pk)
        )
        return self.render("netbox_oa/contract_orders.html", extra_context={"oa_table": table})


template_extensions = [ContractOrders]
