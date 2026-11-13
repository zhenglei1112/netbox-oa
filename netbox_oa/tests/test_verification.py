import base64
from xml.etree import ElementTree

from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

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
        self.assertNotContains(page, "合同付款信息")
        self.assertContains(page, "未提交")
        self.assertIn("no-store", page["Cache-Control"])

    def test_completed_order_shows_completed_phase_without_internal_payment_data(self):
        self.start()
        while self.order.tasks.filter(status="pending").exists():
            self.complete()
        self.client.force_login(self.actor)
        _, url = self.urls()
        response = self.client.get(url)
        self.assertEqual(response.context["status_label"], "已完成")
        self.assertTrue(all(phase["done"] for phase in response.context["phases"]))
        self.assertNotContains(response, "EIP-TEST-001")
        self.assertNotContains(response, "合同付款信息")

    def test_verification_is_public_even_with_login_middleware_and_no_permissions(self):
        _, url = self.urls()
        self.assertFalse(resolve(url).func.login_required)
        with override_settings(MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.auth.middleware.LoginRequiredMiddleware",
        ]):
            self.assertContains(self.client.get(url), self.order.number)
        worker = self.worker("verification_no_document")
        self.client.force_login(worker)
        self.assertEqual(self.client.get(url).status_code, 200)
        outsider = type(self.actor).objects.create_user("verification_no_order", password="test")
        self.grant(outsider, "maintenancedocument", "view")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.logout()
        document, _ = self.urls()
        self.assertEqual(self.client.get(reverse(
            "plugins:netbox_oa:maintenance_print", args=[document.pk],
        )).status_code, 302)

    def test_paper_version_remains_identifiable_after_new_revision(self):
        self.client.force_login(self.actor)
        original, url = self.urls()
        services.update_order(pk=self.order.pk, actor=self.actor, requirements="新版本服务要求")
        response = self.client.get(url)
        self.assertFalse(response.context["is_latest"])
        self.assertContains(response, "已有更新版本")
        self.assertContains(response, original.form_data["requirements"])
        self.assertNotContains(response, "新版本服务要求")
        self.assertNotContains(response, "表单版本")
        self.assertNotContains(response, "扫描版本 v")
        self.assertNotContains(response, "当前版本")

    def test_scan_count_increments_per_document_but_not_on_head_or_invalid_requests(self):
        document, url = self.urls()
        event_count = self.order.events.count()
        for expected in (1, 2):
            page = self.client.get(url)
            self.assertEqual(page.context["scan_count"], expected)
            self.assertContains(page, f'已被扫描 <strong>{expected}</strong> 次')
        self.assertEqual(self.client.head(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 405)
        document.refresh_from_db()
        self.assertEqual(document.scan_count, 2)
        self.assertEqual(self.order.events.count(), event_count)
        services.update_order(pk=self.order.pk, actor=self.actor, requirements="修订内容")
        latest, latest_url = self.urls()
        self.assertNotEqual(latest.pk, document.pk)
        self.assertEqual(self.client.get(latest_url).context["scan_count"], 1)
        self.assertEqual(self.client.get(url).context["scan_count"], 3)
        latest.removed_at = timezone.now()
        latest.save(update_fields=["removed_at"])
        self.assertEqual(self.client.get(latest_url).status_code, 404)
        latest.refresh_from_db()
        self.assertEqual(latest.scan_count, 1)
        self.assertEqual(self.client.get(url + "missing/").status_code, 404)

    def test_summary_uses_snapshot_and_real_submission_not_draft_creation(self):
        document, url = self.urls()
        document.form_data.update(
            supplier="快照服务公司", service_region="浙江省", service_location="快照位置",
            requirements='核验要求<script>alert("x")</script>',
            contact_phone="PRIVATE-PHONE", contract_number="PRIVATE-CONTRACT",
            internal_note="PRIVATE-NOTE",
        )
        document.save()
        self.assertIsNone(self.client.get(url).context["submitted_at"])
        self.start()
        submitted = self.order.events.filter(action="start").first()
        self.assertNotEqual(submitted.created, self.order.created)
        response = self.client.get(url)
        self.assertEqual(response.context["submitted_at"], submitted.created)
        for value in ("快照服务公司", "浙江省", "快照位置", "核验要求"):
            self.assertContains(response, value)
        for value in ("PRIVATE-PHONE", "PRIVATE-CONTRACT", "PRIVATE-NOTE", "进入工单",
                      "办理记录", '<script>alert("x")</script>'):
            self.assertNotContains(response, value)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_all_phases_and_inactive_states(self):
        from netbox_oa.models import MaintenanceOrder

        _, url = self.urls()
        for stage, status, expected, index in (
            ("ordering", "draft", "未提交", None),
            ("ordering", "active", "工单阶段", 0),
            ("acceptance", "active", "验收阶段", 1),
            ("payment", "active", "待付款", 2),
            ("payment", "completed", "已完成", 3),
            ("acceptance", "terminated", "已终止", None),
            ("ordering", "cancelled", "已撤销", None),
        ):
            with self.subTest(status=status, stage=stage):
                MaintenanceOrder.objects.filter(pk=self.order.pk).update(stage=stage, status=status)
                response = self.client.get(url)
                self.assertEqual(response.context["status_label"], expected)
                self.assertEqual(
                    [i for i, phase in enumerate(response.context["phases"]) if phase["current"]],
                    [] if index is None else [index],
                )

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
