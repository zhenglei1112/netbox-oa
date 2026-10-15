"""Permission-checked link to payment information on the purchase contract."""

from django.core.exceptions import ValidationError
from django.urls import NoReverseMatch, reverse

from .maintenance_contracts import purchase_contracts


def payment_context(order, actor):
    result = {"payment_contract_url": "", "payment_contract_error": ""}
    try:
        contract = purchase_contracts(actor).filter(pk=order.contract_id).first()
        if contract is None:
            result["payment_contract_error"] = "外购合同已删除或你没有查看权限。"
        else:
            result["payment_contract_url"] = reverse(
                "plugins:netbox_contract:contract", args=[contract.pk]
            )
    except ValidationError as exc:
        result["payment_contract_error"] = "; ".join(exc.messages)
    except NoReverseMatch:
        result["payment_contract_error"] = "外购合同详情页面不可用。"
    return result
