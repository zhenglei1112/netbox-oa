"""Read external purchase contracts without a hard migration dependency on the plugin."""

import re

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import NoReverseMatch, reverse

from . import DEFAULT_CONTRACT_TYPE_NUMBER_CODES


def service_regions(actor):
    return apps.get_model("dcim", "Region").objects.restrict(actor, "view").order_by("name", "pk")


def region_selection(actor, ids):
    if not isinstance(ids, (list, tuple)):
        raise ValidationError("请从 NetBox 地区中选择服务省市。")
    try:
        ids = list(dict.fromkeys(int(pk) for pk in ids))
    except (TypeError, ValueError):
        raise ValidationError("请从 NetBox 地区中选择服务省市。") from None
    regions = {item.pk: str(item) for item in service_regions(actor).filter(pk__in=ids)}
    if not ids or len(regions) != len(ids):
        raise ValidationError("请选择存在且有查看权限的 NetBox 地区。")
    return {"service_region_ids": ids, "service_region": " / ".join(regions[pk] for pk in ids)}


def region_ids_from_names(actor, names):
    names = [name.strip() for name in names.split(" / ") if name.strip()]
    regions = list(service_regions(actor).filter(name__in=names))
    if len(regions) != len(names) or len({item.name for item in regions}) != len(names):
        return []
    mapping = {item.name: item.pk for item in regions}
    return [mapping[name] for name in names]


def purchase_contracts(actor):
    try:
        model = apps.get_model("netbox_contract", "Contract")
    except LookupError:
        raise ValidationError("请先启用 netbox-contract，才能选择外购合同。") from None
    return model.objects.restrict(actor, "view").select_related("contract_type").order_by(
        "number", "pk"
    )


def contract_type_snapshot(contract):
    contract_type = contract.contract_type
    if contract_type is None:
        return {}
    config = getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_oa", {})
    mapping = config.get("contract_type_number_codes", DEFAULT_CONTRACT_TYPE_NUMBER_CODES)
    if not isinstance(mapping, dict):
        raise ValidationError("合同类型编号代码配置必须为类型 ID 或名称与代码的映射。")
    codes = [str(value).strip().upper() for value in mapping.values()]
    if any(not re.fullmatch(r"[A-Z0-9]", code) for code in codes) or len(set(codes)) != len(codes):
        raise ValidationError("合同类型编号代码须为互不重复的一位大写字母或数字。")
    code = mapping.get(str(contract_type.pk), mapping.get(contract_type.name, ""))
    return {
        "id": contract_type.pk,
        "name": contract_type.name,
        "code": str(code).strip().upper(),
    }


def contract_values(contract, actor):
    config = getattr(settings, "PLUGINS_CONFIG", {}).get("netbox_oa", {})
    region_key = config.get("contract_service_region_field", "ProvincesOfResources")
    region_ids = (contract.custom_field_data or {}).get(region_key) or []
    region = ""
    selected_ids = []
    if isinstance(region_ids, list) and region_ids:
        try:
            region_ids = list(dict.fromkeys(int(pk) for pk in region_ids))
            model = apps.get_model("dcim", "Region")
            regions = {
                item.pk: str(item)
                for item in model.objects.restrict(actor, "view").filter(pk__in=region_ids)
            }
            if len(regions) == len(region_ids):
                region = " / ".join(regions[pk] for pk in region_ids)
                selected_ids = region_ids
        except (LookupError, TypeError, ValueError):
            pass
    return {
        "contract_id": contract.pk,
        "contract_type_snapshot": contract_type_snapshot(contract),
        "contract_number": contract.number or "",
        "supplier": str(contract.external_party_object or ""),
        "service_region": str(region or ""),
        "service_region_ids": selected_ids,
    }


def selected_contract(actor, contract_id, service_region=None, service_region_ids=None):
    try:
        contract = purchase_contracts(actor).filter(pk=contract_id).first()
    except (TypeError, ValueError):
        contract = None
    if contract is None:
        raise ValidationError("请选择有查看权限的外购合同。")
    values = contract_values(contract, actor)
    if not all(values[key] for key in ("contract_number", "supplier")):
        raise ValidationError("所选外购合同缺少合同编号或服务单位，请完善合同资料。")
    if service_region_ids is not None:
        values.update(region_selection(actor, service_region_ids))
    elif service_region is not None:
        ids = (
            region_ids_from_names(actor, service_region)
            if isinstance(service_region, str)
            else service_region
        )
        values.update(region_selection(actor, ids))
    return values


def order_object_links(order, actor):
    """Resolve object links from the accessible contract, keeping stored labels intact."""
    result = {"supplier_url": "", "contract_url": "",
              "current_contract_type": order.contract_type_snapshot}
    if not order.contract_id:
        return result
    try:
        contract = purchase_contracts(actor).filter(pk=order.contract_id).first()
    except ValidationError:
        return result
    if contract is None:
        return result
    result["current_contract_type"] = contract_type_snapshot(contract)
    try:
        result["contract_url"] = reverse(
            "plugins:netbox_contract:contract", args=[contract.pk]
        )
    except NoReverseMatch:
        pass
    supplier = contract.external_party_object
    if supplier is not None and callable(getattr(supplier, "get_absolute_url", None)):
        try:
            result["supplier_url"] = supplier.get_absolute_url()
        except NoReverseMatch:
            pass
    return result
