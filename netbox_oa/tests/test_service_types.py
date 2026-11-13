from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_forms import MaintenanceCompanyForm, MaintenanceOrderEditForm
from netbox_oa.maintenance_materials import company_form_initial
from netbox_oa.tests import test_maintenance


class ServiceTypesTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant

    def test_edit_saves_multiple_selections_and_document_snapshot(self):
        form = MaintenanceOrderEditForm({
            "policy": self.policy.pk,
            "service_type": ["fiber", "power"],
            "priority": "medium",
            "contract_id": self.contract.pk,
            "service_region": [region.pk for region in self.regions],
            "service_location": self.order.service_location,
            "requirements": self.order.requirements,
        }, actor=self.actor, instance=self.order)
        self.assertTrue(form.is_valid(), form.errors)
        services.update_order(pk=self.order.pk, actor=self.actor, **{
            key: value for key, value in form.cleaned_data.items()
            if key not in {"policy", "changelog_message"}
        })
        self.order.refresh_from_db()
        self.assertEqual(self.order.service_type, ["fiber", "power"])
        document = self.order.documents.filter(material="order", kind="form").first()
        self.assertEqual(document.form_data["service_type"], ["fiber", "power"])
        reopened = MaintenanceOrderEditForm(actor=self.actor, instance=self.order)
        self.assertEqual(reopened["service_type"].value(), ["fiber", "power"])
        self.assertIn("multiple", str(reopened["service_type"]))
        self.assertEqual(self.order.get_service_type_display(), "光缆故障处置、应急供电")

    def test_company_form_and_disabled_acceptance_preserve_selections(self):
        self.order = services.update_order(
            pk=self.order.pk, actor=self.actor, service_type=["fiber", "power"],
        )
        initial = company_form_initial(self.order)
        form = MaintenanceCompanyForm(
            material="acceptance", actor=self.actor, order=self.order, initial=initial,
        )
        self.assertEqual(form["service_type"].value(), ["fiber", "power"])
        self.assertTrue(form.fields["service_type"].disabled)
        field = form.fields["service_type"]
        self.assertEqual(field.clean(["fiber", "power"]), ["fiber", "power"])

    def test_invalid_and_empty_selections_are_rejected(self):
        for value in ([], ["unknown"], ["fiber", "fiber"]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                services.update_order(pk=self.order.pk, actor=self.actor, service_type=value)
        from netbox_oa.api.maintenance import OrderSerializer

        field = OrderSerializer().fields["service_type"]
        self.assertEqual(field.run_validation(["fiber", "power"]), ["fiber", "power"])

    def test_print_and_verification_support_current_and_legacy_snapshots(self):
        self.client.force_login(self.actor)
        document = self.order.documents.filter(material="order", kind="form").first()
        for value, count in ((["fiber", "power"], 2), ("fiber", 1)):
            with self.subTest(value=value):
                document.form_data["service_type"] = value
                document.save()
                response = self.client.get(reverse(
                    "plugins:netbox_oa:maintenance_print", args=[document.pk],
                ))
                self.assertContains(response, "☑", count=count)
                self.assertNotContains(response, "（单选）")
                verified = self.client.get(reverse(
                    "plugins:netbox_oa:maintenance_verify", args=[document.pk],
                ))
                self.assertContains(verified, document.form_data["requirements"])
                self.assertNotContains(verified, "服务项目")
                document.refresh_from_db()
                self.assertEqual(document.form_data["service_type"], value)
