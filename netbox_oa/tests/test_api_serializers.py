from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.test import SimpleTestCase
from django.utils.module_loading import import_string
from netbox.models import ChangeLoggedModel


class ModelSerializerDiscoveryTests(SimpleTestCase):
    def test_all_change_logged_models_have_discoverable_serializers(self):
        # NetBox can discover these through UI relations and changelog views,
        # including models which do not have a dedicated API viewset.
        for model in apps.get_app_config("netbox_oa").get_models():
            if not issubclass(model, ChangeLoggedModel):
                continue
            with self.subTest(model=model.__name__):
                serializer = import_string(
                    f"{model._meta.app_label}.api.serializers.{model.__name__}Serializer"
                )
                self.assertIs(serializer.Meta.model, model)
                self.assertTrue(serializer().fields)


class MaintenanceOrderSerializationTests(SimpleTestCase):
    def test_requestless_serialization_uses_contract_type_snapshot(self):
        from netbox_oa.api.serializers import MaintenanceOrderSerializer
        from netbox_oa.models import MaintenanceOrder

        snapshot = {"id": 2, "name": "代维-框架", "code": "K"}
        order = MaintenanceOrder(contract_id=123, contract_type_snapshot=snapshot)
        for context in ({}, {"request": None}):
            with self.subTest(context=context), patch(
                "netbox_oa.api.serializers.order_object_links"
            ) as lookup:
                serializer = MaintenanceOrderSerializer(
                    order, fields=("current_contract_type",), context=context,
                )
                self.assertEqual(serializer.data["current_contract_type"], snapshot)
                lookup.assert_not_called()

    def test_api_serialization_keeps_permission_checked_current_contract_type(self):
        from netbox_oa.api.serializers import MaintenanceOrderSerializer
        from netbox_oa.models import MaintenanceOrder

        actor = object()
        order = MaintenanceOrder(contract_type_snapshot={"code": "K"})
        current = {"id": 3, "name": "代维-日常", "code": "D"}
        with patch(
            "netbox_oa.api.serializers.order_object_links",
            return_value={"current_contract_type": current},
        ) as lookup:
            serializer = MaintenanceOrderSerializer(
                order, fields=("current_contract_type",),
                context={"request": SimpleNamespace(user=actor)},
            )
            self.assertEqual(serializer.data["current_contract_type"], current)
            lookup.assert_called_once_with(order, actor)
