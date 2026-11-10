
"""Order sealing uses the reviewed form without a pre-seal scan submission."""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

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
            "order_seal", "order_sealed_upload",
        ])
        self.assertEqual([node["role"] for node in nodes[-3:]], [
            "senior", "seal", "requester",
        ])
        self.assertEqual([node["group_id"] for node in nodes[-3:]], [3, 4, None])
        self.assertEqual(nodes[3]["return_to"], "order_correction")
        for node in nodes[3:5]:
            self.assertEqual(node["required"], [("order", "electronic")])

    def test_approve_and_seal_form_without_uploading_pre_seal_scan(self):
        self.reach_company_approval()
        form = self.order.documents.get(material="order", kind="form")
        self.assertFalse(self.order.documents.filter(kind="scan").exists())
        page = self.client.get(self.order.get_absolute_url())
        self.assertNotContains(page, 'aria-label="上传"')
        upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                             args=[self.order.pk, "order"])
        self.assertEqual(self.client.get(upload_url).status_code, 403)
        with self.assertRaisesMessage(ValidationError, "无需上传用印前扫描件"):
            self.upload("order", "scan")
        for name in ("工单公司审批", "服务工单用印"):
            self.assertEqual(self.current().name, name)
            task = self.complete()
            self.assertEqual(task.evidence["order"], [form.pk])
        self.assertEqual(self.current().name, "上传用印后服务工单")
        self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
        with self.assertRaisesMessage(ValidationError, "用印扫描件"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertContains(self.client.get(upload_url), "用印后")
        stamped = self.upload("order", "scan", sealed=True)
        self.assertEqual(stamped.source_document_id, form.pk)
        self.assertTrue(stamped.is_sealed)
        task = self.complete()
        self.assertEqual(task.evidence["order"], [stamped.pk])
        self.assertEqual(self.current().name, "验收申请")
        self.assertEqual(self.client.get(reverse(
            "plugins:netbox_oa:maintenance_print", args=[form.pk]
        )).status_code, 200)

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
        self.assertEqual(self.complete().evidence["order"], [revised.pk])
        self.complete()
        with self.assertRaisesMessage(ValidationError, "必须对应本次审批"):
            services.upload_document(
                pk=self.order.pk, actor=self.actor, material="order", state="scan",
                source_document_id=original.pk,
                file=SimpleUploadedFile("wrong.pdf", b"scan"),
            )
        self.assertEqual(self.upload("order", "scan", sealed=True).source_document_id, revised.pk)

    def test_batch_endpoint_rejects_pre_seal_order_scans(self):
        self.reach_company_approval()
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenance_upload", args=[self.order.pk]
        ), {"scan_material": "order", "file": [SimpleUploadedFile("before.pdf", b"scan")]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.order.documents.filter(kind="scan").exists())
