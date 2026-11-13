
"""Order sealing uses the reviewed form without a pre-seal scan submission."""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_flow import build_steps
from netbox_oa.tests import test_draft_submission, test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


class ServiceOrderTests(TestCase):
    setUp = test_draft_submission.DraftSubmissionTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_draft_submission.DraftSubmissionTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload

    def reach_company_approval(self):
        self.start()
        self.complete()
        self.complete()
        self.assertEqual(self.current().name, "工单公司审批")

    def test_sequence_roles_and_return_targets(self):
        nodes = [node for node in build_steps(
            {"department": 2, "senior": 3, "seal": 4}, 24
        ) if node["stage"] == "ordering"]
        self.assertEqual([node["key"] for node in nodes], [
            "order_correction", "order_review", "order_final", "order_senior_seal",
        ])
        self.assertEqual(nodes[-1]["role"], "senior")
        self.assertEqual(nodes[-1]["group_id"], 3)
        self.assertEqual(nodes[-1]["return_to"], "order_correction")
        self.assertEqual(nodes[-1]["required"], [("order", "electronic")])

    def test_approve_and_seal_form_without_uploading_pre_seal_scan(self):
        self.reach_company_approval()
        form = self.order.documents.get(material="order", kind="form")
        self.assertFalse(self.order.documents.filter(kind="scan").exists())
        page = self.client.get(self.order.get_absolute_url())
        self.assertNotContains(page, 'aria-label="上传"')
        upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                             args=[self.order.pk, "order"])
        self.assertEqual(self.client.get(upload_url).status_code, 403)
        with self.assertRaisesMessage(ValidationError, "仅保留系统电子件"):
            self.upload("order", "scan")
        self.assertFalse(form.electronic_seal)
        task = self.complete()
        form.refresh_from_db()
        self.assertEqual(task.evidence["order"], form.pk)
        self.assertEqual(self.current().name, "验收申请")
        self.assertEqual(form.electronic_seal["date"], timezone.localdate().isoformat())
        self.assertEqual(form.electronic_seal["document_id"], form.pk)
        self.assertEqual(form.electronic_seal["approved_by"], self.approver.pk)
        self.assertTrue(form.electronic_seal["image"].startswith("data:image/png;base64,"))
        printed = self.client.get(reverse("plugins:netbox_oa:maintenance_print", args=[form.pk]))
        self.assertContains(printed, 'class="electronic-seal"')
        self.assertNotContains(self.client.get(self.order.get_absolute_url()), "用印后")
        self.assertFalse(self.order.documents.filter(kind="scan").exists())

    def test_missing_seal_rolls_back_company_approval(self):
        self.reach_company_approval()
        task = self.current()
        with patch("netbox_oa.maintenance_seals.files", side_effect=OSError("missing")):
            with self.assertRaisesMessage(ValidationError, "电子印章文件不可用"):
                self.complete()
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertEqual(self.current().pk, task.pk)
        self.assertFalse(self.order.documents.get(material="order").electronic_seal)

    def test_company_countersign_must_finish_before_stamp(self):
        self.reach_company_approval()
        services.countersign_task(
            pk=self.current().pk, actor=self.approver, user_id=self.approver.pk,
            comment="追加公司审核",
        )
        self.complete()
        document = self.order.documents.get(material="order")
        self.assertFalse(document.electronic_seal)
        self.assertEqual(self.current().kind, "countersign")
        self.complete()
        document.refresh_from_db()
        self.assertTrue(document.electronic_seal)
        self.assertEqual(self.current().name, "验收申请")

    def test_payment_requires_approved_electronic_acceptance(self):
        self.reach_company_approval()
        while self.current().name != "生成付款":
            self.complete()
        document = self.order.documents.get(material="acceptance")
        document.electronic_seal = {}
        document.save(update_fields=["electronic_seal"])
        with self.assertRaisesMessage(ValidationError, "请先完成公司审批"):
            self.complete()

    def test_company_return_revises_form_and_repeats_order_reviews(self):
        self.reach_company_approval()
        original = self.order.documents.get(material="order", kind="form")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="请修订工单",
        )
        self.assertEqual(self.current().name, "工单修正")
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        revised = self.upload("order")
        for name in ("工单修正", "工单复核", "工单部门审核"):
            self.assertEqual(self.current().name, name)
            self.complete()
        self.assertEqual(self.current().name, "工单公司审批")
        self.assertNotEqual(original.pk, revised.pk)
        self.assertEqual(self.complete().evidence["order"], revised.pk)
        original.refresh_from_db()
        revised.refresh_from_db()
        self.assertFalse(original.electronic_seal)
        self.assertTrue(revised.electronic_seal)
        self.assertEqual(self.current().name, "验收申请")

    def test_batch_endpoint_rejects_pre_seal_order_scans(self):
        self.reach_company_approval()
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenance_upload", args=[self.order.pk]
        ), {"scan_material": "order", "file": [SimpleUploadedFile("before.pdf", b"scan")]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.order.documents.filter(kind="scan").exists())
