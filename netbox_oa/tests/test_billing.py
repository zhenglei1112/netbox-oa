"""Authoritative contract billing, version preservation and acceptance aggregation."""

import json
from copy import deepcopy

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from netbox_oa import maintenance_billing as billing
from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_forms import MaintenanceCompanyForm
from netbox_oa.maintenance_materials import company_form_initial
from netbox_oa.models import MaintenanceDocument
from netbox_oa.tests import test_maintenance


def schedule():
    return {
        "schema_version": 1, "kind": "framework", "currency": "CNY", "price_basis": "inclusive",
        "effective": {"start_date": "2026-01-01", "end_date": "2026-12-31"},
        "services": [
            {"code": "B1", "name": "施工盯防", "unit": "shift", "method": "fixed",
             "unit_price": "12000"},
            {"code": "A3", "name": "光缆修复", "unit": "fault", "method": "tiered",
             "boundaries_confirmed": True, "tier_basis": "cores", "tiers": [
                 {"label": "48芯及以下", "min": "0", "max": "48", "min_inclusive": True,
                  "max_inclusive": True, "unit_price": "100"},
                 {"label": "48芯以上", "min": "48", "max": None, "min_inclusive": False,
                  "max_inclusive": False, "unit_price": "200"},
             ]},
            {"code": "E1", "name": "油机发电", "unit": "shift", "method": "duration",
             "base_hours": "8", "base_price": "100", "overtime_price": "10",
             "overtime_rounding": "ceil_hour"},
        ], "materials": [],
    }


def line(**values):
    return {"rule_key": "services:B1", "started_at": "2026-09-04T06:00",
            "ended_at": "2026-09-04T15:30", "quantity": "1", "deduction": "0", **values}


class BillingCalculationTests(SimpleTestCase):
    def test_selected_contract_tier_sets_price_and_rejects_invalid_choices(self):
        result = billing.calculate(schedule(), [
            line(rule_key="services:A3", tier_index="0", quantity="2", deduction="10"),
            line(rule_key="services:A3", tier_index="1", unit_price="1"),
        ])
        self.assertEqual(result["total"], "390.00")
        self.assertEqual(result["lines"][0]["tier_index"], "0")
        self.assertEqual(result["lines"][1]["tier"]["label"], "48芯以上")
        for selection in ("", "-1", "2", "0.5", True, [], "999999999"):
            with self.subTest(selection=selection), self.assertRaises(ValidationError):
                billing.calculate(schedule(), [line(rule_key="services:A3", tier_index=selection)])

    def test_fixed_tier_and_duration_follow_contract_prices(self):
        result = billing.calculate(schedule(), [
            line(quantity="2", deduction="100", unit_price="0", net="1"),
            line(rule_key="services:A3", specification="48"),
            line(rule_key="services:A3", specification="49"),
            line(rule_key="services:E1", quantity="2"),
        ])
        self.assertEqual([row["net"] for row in result["lines"]],
                         ["23900.00", "100.00", "200.00", "240.00"])
        self.assertEqual(result["total"], "24440.00")
        pricing = schedule()
        pricing["services"][2]["overtime_rounding"] = "pro_rata"
        self.assertEqual(billing.calculate(pricing, [line(rule_key="services:E1")])["total"],
                         "115.00")

    def test_decimal_rounding_and_material_quantities(self):
        pricing = schedule()
        pricing["materials"] = [{"code": "M01", "name": "光缆", "unit": "km",
                                 "unit_price": "0.335"}]
        self.assertEqual(billing.calculate(pricing, [line(rule_key="materials:M01", quantity="3")])[
            "total"], "1.01")

    def test_invalid_or_unconfigured_charges_are_rejected(self):
        for values in (
            {"rule_key": "services:OTHER"}, {"quantity": "NaN"}, {"quantity": "0"},
            {"deduction": "Infinity"}, {"deduction": "12001"}, {"deduction": "0.001"},
            {"ended_at": "2026-09-03T00:00"}, {"started_at": "2025-09-04T00:00"},
            {"rule_key": "services:A3", "specification": ""},
            {"rule_key": "services:E1", "ended_at": "2026-09-04T06:00"},
            {"unit_price": "1", "rule_key": "no-contract-item"},
            {"rule_key": []}, {"quantity": "999999999999"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                billing.calculate(schedule(), [line(**values)])
        for key, value in (("unit_price", None), ("unit_price", "NaN")):
            pricing = schedule()
            pricing["services"][0][key] = value
            with self.assertRaises(ValidationError):
                billing.calculate(pricing, [line()])
        pricing = schedule()
        pricing["services"][1]["boundaries_confirmed"] = False
        with self.assertRaises(ValidationError):
            billing.calculate(pricing, [line(rule_key="services:A3", specification="24")])
        pricing = schedule()
        pricing["services"][2]["overtime_rounding"] = "pending"
        with self.assertRaises(ValidationError):
            billing.calculate(pricing, [line(rule_key="services:E1")])
        with self.assertRaises(ValidationError):
            billing.calculate(schedule(), [])


class SettlementBillingTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload
    worker = test_maintenance.MaintenanceTests.worker

    def prepare(self):
        self.reach("acceptance_apply")
        self.contract.maintenance_pricing = schedule()
        self.contract.save()
        self.upload("report", "scan")
        return services.material_scan_sources(self.order, "settlement").get()

    def save_acceptance(self, **values):
        return services.save_company_form(
            pk=self.order.pk, actor=self.actor, material="acceptance",
            data={**company_form_initial(self.order), "service_effect": "good",
                  "settlement_amount": "0.01", **values},
        )

    def test_updates_keep_one_record_aggregate_and_ignore_client_totals(self):
        original = self.prepare()
        count = self.order.documents.count()
        first = billing.save_billing(original.pk, self.actor, [line(quantity="2", deduction="100")])
        original.refresh_from_db()
        self.assertEqual(self.order.documents.count(), count + 1)
        self.assertEqual(original.billing_data, {})
        self.assertEqual(first.kind, "billing")
        self.assertEqual(first.item_number, original.item_number)
        self.assertIsNone(first.replaces_document_id)
        self.assertFalse(first.file)
        second = self.upload("settlement", "scan")
        self.assertEqual(self.save_acceptance().form_data["settlement_amount"], "23900.00")
        second = billing.save_billing(second.pk, self.actor,
                                     [line(quantity="2", deduction="100"), line()])
        self.assertEqual(second.pk, first.pk)
        form = self.save_acceptance()
        self.assertEqual(form.form_data["settlement_amount"], "35900.00")
        self.assertEqual({row["document_id"] for row in form.form_data["settlement_billing"][
            "documents"]}, {first.pk, second.pk})
        pricing = deepcopy(self.contract.maintenance_pricing)
        pricing["services"][0]["unit_price"] = "500"
        self.contract.maintenance_pricing = pricing
        self.contract.save()
        first.refresh_from_db()
        self.assertEqual(first.billing_data["lines"][0]["rule"]["unit_price"], "12000")
        self.assertEqual(billing.summary(self.order)["total"], "35900.00")

    def test_stale_acceptance_is_blocked_and_approval_freezes_details(self):
        original = self.prepare()
        first = billing.save_billing(original.pk, self.actor, [line()])
        acceptance = self.save_acceptance()
        revised = billing.save_billing(first.pk, self.actor, [line(deduction="10")])
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.form_data["settlement_amount"], "12000.00")
        self.assertEqual(acceptance.form_data["settlement_billing"]["total"], "12000.00")
        with self.assertRaisesMessage(ValidationError, "重新保存验收单"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        self.save_acceptance()
        test_maintenance.sign_task(self.current(), self.actor)
        services.complete_task(pk=self.current().pk, actor=self.actor)
        with self.assertRaises(ValidationError):
            billing.save_billing(revised.pk, self.actor, [line()])

    def test_acceptance_print_uses_saved_fee_details_before_signatures(self):
        self.client.force_login(self.actor)
        original = self.prepare()
        current = billing.save_billing(original.pk, self.actor, [
            line(quantity="2", deduction="100"),
            line(rule_key="services:A3", tier_index="1"),
            line(rule_key="services:E1"),
        ])
        acceptance = self.save_acceptance()
        billing.save_billing(current.pk, self.actor, [line(deduction="10")])
        page = self.client.get(reverse("plugins:netbox_oa:maintenance_print",
                                       args=[acceptance.pk]))
        self.assertContains(page, "收费明细")
        self.assertContains(page, "48芯以上")
        self.assertContains(page, "2026-09-04 06:00:00")
        self.assertContains(page, "2026-09-04 15:30:00")
        self.assertContains(page, "服务时长 9小时30分钟")
        self.assertContains(page, "合计（元）：24220.00")
        self.assertNotContains(page, "11990.00")
        html = page.content.decode()
        self.assertEqual(html.count('<table '), 1)
        self.assertContains(page, 'class="document-table acceptance-table"')
        self.assertLess(html.index('aria-label="收费明细"'),
                        html.index('class="applicant-row"'))

    def test_acceptance_print_without_fee_snapshot_keeps_original_form(self):
        self.client.force_login(self.actor)
        self.reach("acceptance_apply")
        acceptance = self.save_acceptance(settlement_amount="100")
        page = self.client.get(reverse("plugins:netbox_oa:maintenance_print",
                                       args=[acceptance.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "100")
        self.assertNotContains(page, 'aria-label="收费明细"')

    def test_deleted_and_replaced_scans_do_not_restore_old_amounts(self):
        original = self.prepare()
        first = billing.save_billing(original.pk, self.actor, [line()])
        second_scan = self.upload("settlement", "scan")
        second = billing.save_billing(second_scan.pk, self.actor, [line(deduction="100")])
        self.assertEqual(first.pk, second.pk)
        services.remove_document(pk=original.pk, actor=self.actor)
        self.assertEqual(billing.summary(self.order, require_complete=True)["total"], "11900.00")
        self.assertFalse(services.material_scan_candidates(self.order, "settlement").filter(
            pk=original.pk).exists())
        services.upload_document(
            pk=self.order.pk, actor=self.actor, material="settlement", state="scan",
            file=test_maintenance.SimpleUploadedFile("new.pdf", b"new"),
            upload_mode="replace", replaces_document_id=second_scan.pk,
        )
        self.assertEqual(billing.summary(self.order, require_complete=True)["total"], "11900.00")

    def test_editor_preview_permissions_and_readonly_acceptance_amount(self):
        original = self.prepare()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:settlement_billing_edit", args=[original.pk])
        self.assertTemplateUsed(self.client.get(url), "generic/object_edit.html")
        count = MaintenanceDocument.objects.count()
        data = {"lines": json.dumps([line(deduction="100")])}
        response = self.client.post(url + "?preview=1", data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], "11900.00")
        self.assertEqual(MaintenanceDocument.objects.count(), count)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.get(url).status_code, 200)
        form = MaintenanceCompanyForm(material="acceptance", actor=self.actor, order=self.order)
        self.assertTrue(form.fields["settlement_amount"].disabled)
        self.assertEqual(form.initial["settlement_amount"], "11900.00")
        current = services.material_scan_sources(self.order, "settlement").get()
        other = self.worker("billing_outsider")
        with self.assertRaises(PermissionDenied):
            billing.save_billing(current.pk, other, [line()])

    def test_fees_can_be_created_without_scans_and_uploads_need_no_association(self):
        original = self.prepare()
        services.remove_document(pk=original.pk, actor=self.actor)
        self.client.force_login(self.actor)
        overview = reverse("plugins:netbox_oa:settlement_billing_list", args=[self.order.pk])
        create = reverse("plugins:netbox_oa:settlement_billing_create", args=[self.order.pk])
        page = self.client.get(overview)
        self.assertTrue(page.context["can_add_billing"])
        self.assertContains(page, "维护计费明细")
        self.assertNotContains(page, "新增费用明细")
        self.assertNotContains(page, "请先在验收申请环节上传服务结算单")
        self.assertTemplateUsed(self.client.get(create), "generic/object_edit.html")
        count = self.order.documents.count()
        data = {"lines": json.dumps([line()])}
        self.assertEqual(self.client.post(create + "?preview=1", data).status_code, 200)
        self.assertEqual(self.order.documents.count(), count)
        self.assertEqual(self.client.post(create, data).status_code, 302)
        first = billing.billing_documents(self.order).get()
        self.assertEqual(first.kind, "billing")
        self.assertFalse(first.file)
        self.assertEqual(first.item_number, 1)
        second = billing.create_billing(self.order, self.actor, [line(), line(deduction="100")])
        self.assertEqual(second.item_number, 1)
        self.assertEqual(second.pk, first.pk)
        revised = billing.save_billing(first.pk, self.actor, [line(), line(deduction="100")])
        self.assertEqual(revised.pk, first.pk)
        self.assertEqual(self.order.documents.filter(kind="billing").count(), 1)
        overview_page = self.client.get(overview)
        self.assertNotContains(overview_page, "计费历史")
        self.assertNotContains(overview_page, "新增费用明细")
        self.assertNotContains(overview_page, "删除费用明细")
        self.assertNotContains(overview_page, f'费用明细 {revised.item_number} · v')
        self.assertEqual(revised.item_number, first.item_number)
        form = self.save_acceptance()
        self.assertEqual(form.form_data["settlement_amount"], "23900.00")
        self.assertFalse(services.material_scan_sources(self.order, "settlement").exists())
        page = self.client.get(self.order.get_absolute_url())
        material = next(row for row in page.context["detail"]["materials"]
                        if row["key"] == "settlement")
        self.assertEqual(material["scan_sources"], [])
        self.assertTrue(all(doc.kind != "billing" for doc in material["versions"]))
        snapshot = billing.summary(self.order)
        uploaded = self.upload("settlement", "scan")
        self.assertFalse(uploaded.billing_data)
        self.assertIsNone(uploaded.source_document_id)
        self.assertIsNone(uploaded.replaces_document_id)
        self.assertEqual(billing.summary(self.order), snapshot)
        self.assertNotIn(uploaded.pk, [row["document_id"] for row in snapshot["documents"]])
        test_maintenance.sign_task(self.current(), self.actor)
        services.complete_task(pk=self.current().pk, actor=self.actor)
        with self.assertRaises(ValidationError):
            billing.create_billing(self.order, self.actor, [line()])

    def test_fee_updates_and_deletion_do_not_create_or_restore_versions(self):
        original = self.prepare()
        services.remove_document(pk=original.pk, actor=self.actor)
        first = billing.create_billing(self.order, self.actor, [line()])
        latest = billing.save_billing(first.pk, self.actor, [line(deduction="100")])
        with self.assertRaises(ValidationError):
            billing.remove_billing(latest.pk, self.actor)
        self.assertEqual(billing.billing_documents(self.order).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.billing_data["total"], "11900.00")
        self.assertEqual(first.pk, latest.pk)
        self.assertEqual(self.order.documents.filter(kind="billing").count(), 1)
        self.assertEqual(billing.create_billing(self.order, self.actor, [line()]).pk, first.pk)
        self.client.force_login(self.actor)
        url = f"/plugins/oa/maintenance/materials/{first.pk}/billing/remove/"
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_billing_navigation_preserves_acceptance_entry_through_edits(self):
        self.prepare()
        self.client.force_login(self.actor)
        overview = reverse("plugins:netbox_oa:settlement_billing_list", args=[self.order.pk])
        acceptance = reverse("plugins:netbox_oa:maintenance_company_form",
                             args=[self.order.pk, "acceptance"])
        marker = "?return_to=acceptance"
        create = reverse("plugins:netbox_oa:settlement_billing_create", args=[self.order.pk])
        acceptance_page = self.client.get(acceptance)
        self.assertContains(acceptance_page, create + marker)
        self.assertNotContains(acceptance_page, overview + marker)
        self.assertContains(acceptance_page, 'data-billing-summary')
        self.assertContains(acceptance_page, '暂无费用明细。')
        page = self.client.get(overview + marker)
        self.assertEqual(page.context["return_url"], acceptance)
        self.assertContains(page, "返回填写验收单")
        self.assertContains(page, create + marker)
        page = self.client.get(create + marker)
        self.assertEqual(page.context["return_url"], overview + marker)
        invalid = self.client.post(create + marker, {"lines": "[]"})
        self.assertEqual(invalid.context["return_url"], overview + marker)
        data = {"lines": json.dumps([line()])}
        saved = self.client.post(create + marker, data)
        self.assertRedirects(saved, overview + marker)
        document = billing.billing_documents(self.order).get()
        edit = reverse("plugins:netbox_oa:settlement_billing_edit", args=[document.pk])
        self.assertContains(self.client.get(overview + marker), edit + marker)
        self.assertRedirects(self.client.post(edit + marker, data), overview + marker)
        self.assertEqual(self.client.get(overview).context["return_url"],
                         self.order.get_absolute_url())
        self.assertEqual(self.client.get(overview + "?return_to=https://example.test").context[
            "return_url"], self.order.get_absolute_url())

    def test_embedded_maintenance_uses_modal_form_and_returns_saved_amount(self):
        self.prepare()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:settlement_billing_create", args=[self.order.pk])
        page = self.client.get(url, {"embed": "1", "return_to": "acceptance"})
        self.assertTemplateUsed(page, "netbox_oa/maintenance/settlement_billing_embedded.html")
        self.assertEqual(page["X-Frame-Options"], "SAMEORIGIN")
        self.assertNotContains(page, 'class="object-edit mt-5"')
        self.assertContains(page, "data-billing-cancel")
        self.assertContains(page, 'class="btn btn-primary" type="submit"')
        invalid = self.client.post(url + "?embed=1", {"lines": "[]"})
        self.assertTemplateUsed(invalid, "netbox_oa/maintenance/settlement_billing_embedded.html")
        saved = self.client.post(url + "?embed=1", {"lines": json.dumps([line(deduction="100")])})
        self.assertEqual(saved.status_code, 200)
        self.assertTemplateUsed(saved, "netbox_oa/maintenance/settlement_billing_complete.html")
        self.assertContains(saved, 'data-total="11900.00"')
        self.assertEqual(saved["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(saved, 'oa-billing-summary-result')
        summary_html = saved.context["summary_html"]
        self.assertIn('11900.00', summary_html)
        self.assertIn('2026-09-04', summary_html)
        self.assertNotIn('T06:00', summary_html)
        acceptance = reverse("plugins:netbox_oa:maintenance_company_form",
                             args=[self.order.pk, "acceptance"])
        acceptance_page = self.client.get(acceptance)
        self.assertContains(acceptance_page, summary_html, html=True)
        self.assertEqual(acceptance_page.context["acceptance_billing"]["total"], "11900.00")

    def test_acceptance_save_retains_category_filters_and_selection(self):
        self.prepare()
        billing.create_billing(self.order, self.actor, [line()])
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_company_form",
                      args=[self.order.pk, "acceptance"])
        query = f"tab=all&q={self.order.number}&stage=acceptance"
        page = self.client.get(url, {"return_query": query})
        self.assertEqual(page.context["return_query"], query)
        response = self.client.post(url, {
            **company_form_initial(self.order), "service_effect": "good",
            "settlement_amount": "0", "return_query": query,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tab"], "all")
        self.assertEqual(response.context["q"], self.order.number)
        self.assertEqual(response.context["detail"]["order"].pk, self.order.pk)
        self.assertFalse(response.context["outside_list"])

    def test_return_selection_falls_back_within_current_category(self):
        self.prepare()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_workbench_v2")
        page = self.client.get(url, {"tab": "all", "selected": "999999999", "restore": "1"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["detail"]["order"].pk, page.context["rows"][0]["order"].pk)
        self.assertFalse(page.context["outside_list"])
        page = self.client.get(url, {"tab": "all", "selected": self.order.pk,
                                     "q": "no-matching-order", "restore": "1"})
        self.assertIsNone(page.context["detail"])
        self.assertEqual(page.context["q"], "no-matching-order")


class SingleBillingMigrationTests(TransactionTestCase):
    tearDown = test_maintenance.MaintenanceTests.tearDown

    def test_merges_existing_fees_without_changing_scans_or_approval_snapshots(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes("netbox_oa")
        executor.migrate(latest)
        test_maintenance.MaintenanceTests.setUp(self)
        previous = [("netbox_oa", "0031_alter_maintenancedocument_kind")]
        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        try:
            Document = executor.loader.project_state(previous).apps.get_model(
                "netbox_oa", "MaintenanceDocument"
            )
            common = {"order_id": self.order.pk, "material": "settlement",
                      "uploaded_by_id": self.actor.pk}
            old = Document.objects.create(
                **common, kind="billing", version=10, item_number=1,
                original_name="旧计费版本", billing_data={"lines": [{"name": "旧版本"}],
                                                        "total": "999.00"},
            )
            Document.objects.create(
                **common, kind="billing", version=11, item_number=1, replaces_document_id=old.pk,
                original_name="费用一",
                billing_data={"lines": [{"name": "服务一"}], "total": "100.00"},
            )
            target = Document.objects.create(
                **common, kind="billing", version=12, item_number=2,
                original_name="费用二",
                billing_data={"lines": [{"name": "服务二"}], "total": "200.00"},
            )
            scan = Document.objects.create(
                **common, kind="scan", version=13, item_number=3, file="old-scan.pdf",
                original_name="old-scan.pdf", billing_data={"lines": [{"name": "扫描件旧计费"}],
                                                          "total": "50.00"},
            )
            snapshot = {"settlement_amount": "350.00", "settlement_billing": {"total": "350.00"}}
            acceptance = Document.objects.create(
                order_id=self.order.pk, uploaded_by_id=self.actor.pk, material="acceptance",
                kind="form", version=1, original_name="验收单", form_data=snapshot,
            )
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            Document = executor.loader.project_state(latest).apps.get_model(
                "netbox_oa", "MaintenanceDocument"
            )
            current = Document.objects.get(order_id=self.order.pk, kind="billing",
                                           removed_at__isnull=True)
            self.assertEqual(current.pk, target.pk)
            self.assertEqual(current.billing_data["total"], "350.00")
            self.assertEqual(len(current.billing_data["lines"]), 3)
            self.assertEqual(Document.objects.get(pk=acceptance.pk).form_data, snapshot)
            self.assertEqual(Document.objects.get(pk=scan.pk).file.name, "old-scan.pdf")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
