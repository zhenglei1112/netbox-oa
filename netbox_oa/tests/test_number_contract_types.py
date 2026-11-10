
"""Contract type changes update drafts while submitted numbers remain stable."""

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_contracts import contract_type_snapshot
from netbox_oa.maintenance_materials import company_form_initial
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user
from tests.support.contracts.models import Contract, ContractType


@override_settings(PLUGINS_CONFIG={"netbox_oa": {
    "contract_type_number_codes": {"1": "W", "2": "K", "3": "D"},
}})
class NumberContractTypeTests(TestCase):
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    start = test_maintenance.MaintenanceTests.start

    def setUp(self):
        test_maintenance.MaintenanceTests.setUp(self)
        self.contract_type.name = "外购资源"
        self.contract_type.save()
        self.framework = ContractType.objects.create(name="代维-框架")
        self.daily = ContractType.objects.create(name="代维-日常")
        self.other_contract = Contract.objects.create(
            name="代维合同", number="HT-K", contract_type=self.framework,
            external_party_object=self.provider,
            custom_field_data=self.contract.custom_field_data,
        )
        self.order = services.update_order(pk=self.order.pk, actor=self.actor)

    def test_mapping_and_draft_contract_change_keep_reserved_month_and_tail(self):
        original = self.order.number
        base = self.order.number_reservation_id
        self.assertEqual(original, base[:8] + "W" + base[8:])
        self.assertEqual(contract_type_snapshot(self.other_contract)["code"], "K")
        with self.settings(PLUGINS_CONFIG={}):
            self.assertEqual(contract_type_snapshot(self.contract)["code"], "W")
            self.assertEqual(contract_type_snapshot(self.other_contract)["code"], "K")
        updated = services.update_order(
            pk=self.order.pk, actor=self.actor, contract_id=self.other_contract.pk,
        )
        self.assertEqual(updated.number, base[:8] + "K" + base[8:])
        self.assertEqual(updated.number_reservation_id, base)
        self.assertEqual(updated.number_type_snapshot, {})
        self.assertEqual(updated.documents.first().form_data["number"], updated.number)
        event = updated.events.filter(action="edit").first()
        self.assertEqual(event.details["contract_before"]["type"]["code"], "W")
        self.assertEqual(event.details["contract_after"]["type"]["code"], "K")
        self.other_contract.contract_type = self.daily
        self.assertEqual(contract_type_snapshot(self.other_contract)["code"], "D")

    def test_first_submission_freezes_type_and_returned_contract_change_keeps_number(self):
        self.start()
        original = self.order.number
        frozen = dict(self.order.number_type_snapshot)
        self.assertEqual(frozen["code"], "W")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="改用代维合同",
        )
        updated = services.update_order(
            pk=self.order.pk, actor=self.actor, contract_id=self.other_contract.pk,
        )
        self.assertEqual(updated.number, original)
        self.assertEqual(updated.number_type_snapshot, frozen)
        self.assertEqual(updated.contract_type_snapshot["code"], "K")
        self.assertEqual(updated.documents.first().form_data["number"], original)
        self.complete()
        self.order.refresh_from_db()
        self.assertEqual(self.order.number_type_snapshot, frozen)
        self.client.force_login(self.actor)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, "代维-框架")
        self.assertContains(page, "外购资源（W，首次提交时确定）")

    def test_company_form_change_updates_draft_number_and_audits_types(self):
        data = {**company_form_initial(self.order), "contract_id": self.other_contract.pk}
        document = services.save_company_form(
            pk=self.order.pk, actor=self.actor, material="order", data=data,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.number[8], "K")
        self.assertEqual(document.form_data["number"], self.order.number)
        event = self.order.events.filter(action="form").first()
        self.assertEqual(event.details["contract_before"]["type"]["code"], "W")
        self.assertEqual(event.details["contract_after"]["type"]["code"], "K")

    def test_changed_draft_requires_new_signature(self):
        test_maintenance.sign_draft(self.order, self.actor)
        services.update_order(pk=self.order.pk, actor=self.actor,
                              contract_id=self.other_contract.pk)
        with self.assertRaises(ValidationError):
            services.start_order(pk=self.order.pk, actor=self.actor)
        self.start()
        self.assertEqual(self.order.number_type_snapshot["code"], "K")

    def test_external_type_change_requires_saved_and_signed_revision(self):
        test_maintenance.sign_draft(self.order, self.actor)
        self.contract.contract_type = self.framework
        self.contract.save()
        with self.assertRaisesMessage(ValidationError, "重新保存草稿并签名"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        services.update_order(pk=self.order.pk, actor=self.actor)
        with self.assertRaises(ValidationError):
            services.start_order(pk=self.order.pk, actor=self.actor)
        self.start()
        self.assertEqual(self.order.number_type_snapshot["code"], "K")

    def test_external_type_change_is_visible_without_changing_submitted_number(self):
        self.start()
        original = self.order.number
        self.contract.contract_type = self.daily
        self.contract.save()
        self.client.force_login(self.actor)
        page = self.client.get(self.order.get_absolute_url())
        self.assertEqual(page.context["detail"]["current_contract_type"]["code"], "D")
        self.order.refresh_from_db()
        self.assertEqual(self.order.number, original)
        self.assertEqual(self.order.number_type_snapshot["code"], "W")

    def test_unmapped_type_can_save_draft_but_cannot_submit(self):
        unknown = ContractType.objects.create(name="未配置类型")
        self.contract.contract_type = unknown
        self.contract.save()
        updated = services.update_order(pk=self.order.pk, actor=self.actor)
        self.assertEqual(updated.number, updated.number_reservation_id)
        test_maintenance.sign_draft(updated, self.actor)
        with self.assertRaisesMessage(ValidationError, "配置该类型的一位编号代码"):
            services.start_order(pk=updated.pk, actor=self.actor)
        updated.refresh_from_db()
        self.assertEqual(updated.status, "draft")
        self.assertEqual(updated.number_type_snapshot, {})

    def test_type_snapshots_are_read_only_in_api(self):
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post("/api/maintenance/orders/", {
            "policy": self.policy.pk, "owner": self.actor.pk, "service_type": "fiber",
            "contract_id": self.contract.pk,
            "service_region_ids": [region.pk for region in self.regions],
            "service_location": "站点", "requirements": "维护",
            "contract_type_snapshot": {"code": "Z"}, "number_type_snapshot": {"code": "Z"},
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["contract_type_snapshot"]["code"], "W")
        self.assertEqual(response.data["number_type_snapshot"], {})

    def test_create_page_provides_type_preview_and_correction_page_locks_number(self):
        self.client.force_login(self.actor)
        page = self.client.get(reverse("plugins:netbox_oa:maintenanceorder_add"))
        self.assertContains(page, 'data-number-fixed="0"')
        self.assertEqual(page.context["form"].contract_options[str(self.contract.pk)]
                         ["contract_type_snapshot"]["code"], "W")
        self.start()
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="补正",
        )
        page = self.client.get(reverse("plugins:netbox_oa:maintenanceorder_edit",
                                       args=[self.order.pk]))
        self.assertContains(page, 'data-number-fixed="1"')
