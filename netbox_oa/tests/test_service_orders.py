"""Service order scans must be approved and stamped before reporting starts."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_flow import build_steps
from netbox_oa.models import MaintenanceOrder
from netbox_oa.tests import test_draft_submission, test_maintenance


class ServiceOrderTests(TestCase):
    setUp = test_draft_submission.DraftSubmissionTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_draft_submission.DraftSubmissionTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    grant = test_maintenance.MaintenanceTests.grant
    worker = test_maintenance.MaintenanceTests.worker

    def reach_service_order(self):
        self.start()
        self.complete()
        self.complete()
        self.assertEqual(self.current().name, "服务工单")

    def batch(self):
        return services.upload_material_scans(
            pk=self.order.pk,
            actor=self.actor,
            material="order",
            files=[SimpleUploadedFile("before.pdf", b"scan")],
        )

    def test_sequence_roles_and_return_targets(self):
        nodes = build_steps({"department": 2, "senior": 3, "seal": 4}, 24)
        keys = [node["key"] for node in nodes]
        start = keys.index("order_final") + 1
        new = nodes[start : start + 4]
        self.assertEqual(
            [node["name"] for node in new],
            [
                "服务工单",
                "服务工单用印部门审核",
                "服务工单用印公司审批",
                "服务工单用印",
            ],
        )
        self.assertEqual(
            [node["role"] for node in new],
            [
                "requester",
                "department",
                "senior",
                "seal",
            ],
        )
        self.assertEqual([node["group_id"] for node in new], [None, 2, 3, 4])
        self.assertEqual(
            [node["return_to"] for node in new[1:3]],
            [
                "order_scan_fill",
                "order_scan_fill",
            ],
        )
        self.assertEqual(nodes[start + 4]["key"], "report_fill")

    def test_form_cannot_replace_scan_and_print_is_available(self):
        self.reach_service_order()
        with self.assertRaisesMessage(ValidationError, "至少上传一份"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, 'aria-label="打印"')
        self.assertContains(page, 'aria-label="上传"')
        self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
        with self.assertRaises(ValidationError):
            services.upload_material_scans(
                pk=self.order.pk,
                actor=self.actor,
                material="order",
                files=[SimpleUploadedFile("invalid.docx", b"document")],
            )

    def test_complete_approvals_and_matching_stamped_scans_then_report(self):
        self.reach_service_order()
        scan = self.batch()[0]
        self.assertIsNone(scan.source_document_id)
        self.assertFalse(scan.is_sealed)
        for name in ("服务工单", "服务工单用印部门审核", "服务工单用印公司审批"):
            self.assertEqual(self.current().name, name)
            task = services.complete_task(pk=self.current().pk, actor=self.actor)
            self.assertEqual(task.evidence["order"], [scan.pk])
        self.assertEqual(self.current().name, "服务工单用印")
        with self.assertRaisesMessage(ValidationError, "每份"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        form = self.order.documents.filter(kind="form", material="order").first()
        with self.assertRaisesMessage(ValidationError, "必须对应本次审批"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="order",
                state="scan",
                source_document_id=form.pk,
                file=SimpleUploadedFile("wrong.pdf", b"scan"),
            )
        upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                             args=[self.order.pk, "order"])
        self.assertContains(self.client.get(upload_url), "用印后")
        stamped = services.upload_document(
            pk=self.order.pk,
            actor=self.actor,
            material="order",
            state="scan",
            source_document_id=scan.pk,
            file=SimpleUploadedFile("after.pdf", b"scan"),
        )
        self.assertTrue(stamped.is_sealed)
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(task.evidence["order"], [stamped.pk])
        self.assertEqual(self.current().name, "服务报告")
        self.assertEqual(
            self.client.get(
                reverse("plugins:netbox_oa:maintenance_print", args=[form.pk])
            ).status_code,
            200,
        )

    def test_approval_freezes_scans_and_return_requires_new_submission(self):
        self.reach_service_order()
        original = self.batch()[0]
        self.complete()
        with self.assertRaisesMessage(ValidationError, "审批期间不能修改"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="order",
                state="scan",
                file=SimpleUploadedFile("changed.pdf", b"scan"),
            )
        services.return_task(pk=self.current().pk, actor=self.actor, comment="请补正扫描件")
        self.assertEqual(self.current().name, "服务工单")
        with self.assertRaisesMessage(ValidationError, "至少上传一份"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        revised = self.batch()[0]
        self.assertGreater(revised.version, original.version)
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(task.evidence["order"], [revised.pk])

    def test_requester_handles_submission_even_when_owner_differs(self):
        owner = self.worker("other_owner")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(owner=owner)
        self.order.refresh_from_db()
        self.reach_service_order()
        self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
        with self.assertRaises(PermissionDenied):
            services.complete_task(pk=self.current().pk, actor=owner)

    def test_batch_upload_endpoint_accepts_service_order_scans(self):
        self.reach_service_order()
        response = self.client.post(
            reverse("plugins:netbox_oa:maintenance_upload", args=[self.order.pk]),
            {"scan_material": "order", "file": [SimpleUploadedFile("before.pdf", b"scan")]},
        )
        self.assertEqual(response.status_code, 302)
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(len(task.evidence["order"]), 1)
