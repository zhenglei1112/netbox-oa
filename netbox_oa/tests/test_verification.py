import base64
from xml.etree import ElementTree

from django.test import TestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.tests import test_maintenance


class VerificationTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    grant = test_maintenance.MaintenanceTests.grant
    worker = test_maintenance.MaintenanceTests.worker

    def urls(self):
        document = self.order.documents.filter(material="order", kind="form").first()
        return document, reverse("plugins:netbox_oa:maintenance_verify", args=[document.pk])

    def test_print_contains_real_svg_codes_and_document_verification_url(self):
        self.client.force_login(self.actor)
        document, url = self.urls()
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        )
        self.assertEqual(response.context["verification_url"], "http://testserver" + url)
        for key in ("order_barcode", "verification_qr"):
            value = response.context[key]
            self.assertTrue(value.startswith("data:image/svg+xml;base64,"))
            svg = ElementTree.fromstring(base64.b64decode(value.split(",", 1)[1]))
            self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertGreaterEqual(len(list(svg.iter())), 2)
        self.assertContains(response, 'class="order-machine-code"')
        barcode_block = response.content.decode().split('class="order-machine-code">', 1)[1].split(
            "</div>", 1
        )[0]
        self.assertNotIn("<span>", barcode_block)
        self.assertContains(response, 'class="verification-code"')
        page = self.client.get(url)
        self.assertContains(page, self.order.number)
        self.assertNotContains(page, "付款申请提交不代表实际已付款")
        self.assertContains(page, "合同付款信息")
        self.assertContains(page, "未提交")
        self.assertIn("no-store", page["Cache-Control"])

    def test_payment_application_time_and_reference_are_visible_after_completion(self):
        self.start()
        while self.order.tasks.filter(status="pending").exists():
            self.complete()
        self.client.force_login(self.actor)
        _, url = self.urls()
        response = self.client.get(url)
        payment = response.context["payment_task"]
        self.assertEqual(payment.external_reference, "EIP-TEST-001")
        self.assertIsNotNone(payment.completed_at)
        self.assertContains(response, "付款申请已提交")
        self.assertContains(response, "EIP-TEST-001")
        self.assertContains(response, "合同付款信息")

    def test_verification_requires_login_and_both_order_and_document_permissions(self):
        _, url = self.urls()
        self.assertEqual(self.client.get(url).status_code, 302)
        worker = self.worker("verification_no_document")
        self.client.force_login(worker)
        self.assertEqual(self.client.get(url).status_code, 404)
        outsider = type(self.actor).objects.create_user("verification_no_order", password="test")
        self.grant(outsider, "maintenancedocument", "view")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_paper_version_remains_identifiable_after_new_revision(self):
        self.client.force_login(self.actor)
        original, url = self.urls()
        services.update_order(pk=self.order.pk, actor=self.actor, requirements="新版本服务要求")
        response = self.client.get(url)
        self.assertEqual(response.context["document"].pk, original.pk)
        self.assertFalse(response.context["is_latest"])
        self.assertContains(response, "已有更新版本")
        self.assertContains(response, original.form_data["requirements"])

    def test_print_priority_uses_snapshot_or_order_when_not_recorded(self):
        self.client.force_login(self.actor)
        document, _ = self.urls()
        url = reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        document.form_data["priority"] = "high"
        document.save()
        response = self.client.get(url)
        self.assertEqual(response.context["priority_label"], "高")
        document.form_data.pop("priority")
        document.save()
        response = self.client.get(url)
        self.assertEqual(response.context["priority_label"], self.order.get_priority_display())
        document.refresh_from_db()
        self.assertNotIn("priority", document.form_data)

    def test_print_formats_snapshot_dates_without_changing_data(self):
        self.client.force_login(self.actor)
        document, _ = self.urls()
        document.form_data.update(
            requested_at="2026-10-16T10:32:00+08:00",
            signature_date="2026-10-16",
        )
        document.save()
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        )
        self.assertContains(response, "2026年10月16日", count=2)
        self.assertNotContains(response, "2026-10-16")
        self.assertNotContains(response, "10:32")
        self.assertContains(response, "____年__月__日")
        document.refresh_from_db()
        self.assertEqual(document.form_data["signature_date"], "2026-10-16")

    def test_chinese_date_filter_preserves_time_and_empty_placeholders(self):
        from netbox_oa.templatetags.oa_filters import chinese_date

        self.assertEqual(chinese_date("2026-01-06"), "2026年1月6日")
        self.assertEqual(chinese_date("2026-10-16 10:32", True), "2026年10月16日 10:32")
        self.assertEqual(chinese_date(""), "")
        self.assertEqual(chinese_date("____年__月__日"), "____年__月__日")
