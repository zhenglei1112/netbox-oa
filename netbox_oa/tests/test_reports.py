from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.models import MaintenanceDocument
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


class ReportTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    reach = test_maintenance.MaintenanceTests.reach
    worker = test_maintenance.MaintenanceTests.worker

    def files(self):
        return [SimpleUploadedFile(name, b"scan") for name in ("report-one.pdf", "report-two.jpg")]

    def batch(self):
        return services.upload_material_scans(
            material="report", pk=self.order.pk, actor=self.actor, files=self.files()
        )


    def test_missing_and_electronic_reports_cannot_complete(self):
        self.reach("acceptance_apply")
        with self.assertRaisesMessage(ValidationError, "至少上传一份"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        with self.assertRaisesMessage(ValidationError, "必须上传"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="report",
                file=SimpleUploadedFile("report.docx", b"document"),
            )
        self.assertEqual(self.current().status, "pending")


    def test_approval_freezes_reports_and_return_requires_a_new_set(self):
        self.reach("acceptance_apply")
        original = self.batch()
        self.complete()
        with self.assertRaisesMessage(ValidationError, "审批期间不能修改"):
            self.upload("report", "scan")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="请补全报告",
        )
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        replacement = self.batch()
        self.upload("acceptance")
        self.upload("settlement", "scan")
        test_maintenance.sign_task(self.current(), acting_user(self))
        task = services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(set(task.evidence["report"]), {scan.pk for scan in replacement})
        self.assertTrue(MaintenanceDocument.objects.filter(pk=original[0].pk).exists())
        self.complete()
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="公司审批退回",
        )
        self.assertEqual(self.current().name, "验收申请")

    def test_batch_failure_rolls_back_documents_and_assignment(self):
        self.reach("acceptance_apply")
        started_at = self.current().started_at
        files = [*self.files(), SimpleUploadedFile("invalid.docx", b"not scan")]
        with self.assertRaisesMessage(ValidationError, "扫描件仅支持"):
            services.upload_material_scans(
                material="report", pk=self.order.pk, actor=self.actor, files=files
            )
        self.assertFalse(self.order.documents.filter(material="report").exists())
        self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
        self.assertEqual(self.current().started_at, started_at)

    def test_web_multiple_upload_and_all_current_files_visible(self):
        self.reach("acceptance_apply")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_upload", args=[self.order.pk])
        response = self.client.post(url, {"scan_material": "report", "file": self.files()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.order.documents.filter(material="report", kind="scan").count(), 2)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, "report-one.pdf")
        self.assertContains(page, "report-two.jpg")
        self.assertContains(page, "本次服务报告 · 2 份")
        upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                             args=[self.order.pk, "report"])
        self.assertContains(self.client.get(upload_url), "multiple")
        self.complete()
        self.complete()
        self.complete()
        self.complete()
        page = self.client.get(upload_url)
        self.assertEqual(page.status_code, 403)

    def test_api_enforces_scan_upload_without_electronic_source(self):
        self.reach("acceptance_apply")
        client = APIClient()
        client.force_authenticate(self.actor)
        url = "/api/maintenance/documents/"
        response = client.post(
            url,
            {
                "order_id": self.order.pk,
                "material": "report",
                "state": "electronic",
                "file": SimpleUploadedFile("report.pdf", b"scan"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        response = client.post(
            url,
            {
                "order_id": self.order.pk,
                "material": "report",
                "state": "scan",
                "is_sealed": True,
                "file": SimpleUploadedFile("report.pdf", b"scan"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["kind"], "scan")
        self.assertEqual(response.data["state"], "scan")
        self.assertFalse(response.data["is_sealed"])
        self.assertEqual(response.data["uploaded_task"], self.current().pk)
        self.assertIsNone(response.data["source_document"])
