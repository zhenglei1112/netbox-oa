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
