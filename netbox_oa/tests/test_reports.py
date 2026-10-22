from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_flow import build_steps
from netbox_oa.models import MaintenanceDocument, MaintenanceOrder
from netbox_oa.tests import test_maintenance


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

    def test_exact_five_nodes_and_configured_roles(self):
        nodes = [
            node
            for node in build_steps({"department": 2, "senior": 3}, 24)
            if node["stage"] == "report"
        ]
        self.assertEqual(
            [node["name"] for node in nodes],
            ["服务报告", "服务报告用印部门审核", "服务报告用印公司审批",
             "服务报告用印", "上传用印后服务报告"],
        )
        self.assertEqual(
            [node["kind"] for node in nodes],
            ["submission", "approval", "approval", "action", "submission"]
        )
        self.assertEqual([nodes[1]["group_id"], nodes[2]["group_id"]], [2, 3])

    def test_missing_and_electronic_reports_cannot_complete(self):
        self.reach("report_fill")
        with self.assertRaisesMessage(ValidationError, "至少上传一份"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "必须上传"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="report",
                file=SimpleUploadedFile("report.docx", b"document"),
            )
        self.assertEqual(self.current().status, "pending")

    def test_multiple_reports_are_approved_and_sealed_as_a_set(self):
        self.reach("report_fill")
        scans = self.batch()
        self.assertTrue(all(scan.state == "scan" and not scan.is_sealed for scan in scans))
        self.assertTrue(all(scan.uploaded_task_id == self.current().pk for scan in scans))
        expected = {scan.pk for scan in scans}
        for name in ("服务报告", "服务报告用印部门审核", "服务报告用印公司审批"):
            self.assertEqual(self.current().name, name)
            task = services.complete_task(pk=self.current().pk, actor=self.actor)
            self.assertEqual(set(task.evidence["report"]), expected)
        self.assertEqual(self.current().name, "服务报告用印")
        self.complete()
        self.assertEqual(self.current().name, "上传用印后服务报告")
        with self.assertRaises(ValidationError) as missing:
            services.complete_task(pk=self.current().pk, actor=self.actor)
        for scan in scans:
            self.assertIn(scan.short_name, str(missing.exception))
        stamped = []
        for scan in scans:
            with self.assertRaisesMessage(ValidationError, "每份服务报告"):
                services.complete_task(pk=self.current().pk, actor=self.actor)
            stamped.append(
                services.upload_document(
                    pk=self.order.pk,
                    actor=self.actor,
                    material="report",
                    state="scan",
                    source_document_id=scan.pk,
                    file=SimpleUploadedFile("sealed.pdf", b"sealed"),
                )
            )
        self.assertTrue(all(scan.state == "scan" and scan.is_sealed for scan in stamped))
        self.assertTrue(all(scan.uploaded_task_id == self.current().pk for scan in stamped))
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(set(task.evidence["report"]), {scan.pk for scan in stamped})
        self.assertEqual(MaintenanceOrder.objects.get(pk=self.order.pk).stage, "settlement")

    def test_approval_freezes_reports_and_return_requires_a_new_set(self):
        self.reach("report_fill")
        original = self.batch()
        self.complete()
        with self.assertRaisesMessage(ValidationError, "审批期间不能修改"):
            self.upload("report", "scan")
        services.return_task(pk=self.current().pk, actor=self.actor, comment="请补全报告")
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        replacement = self.batch()
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(set(task.evidence["report"]), {scan.pk for scan in replacement})
        self.assertTrue(MaintenanceDocument.objects.filter(pk=original[0].pk).exists())
        self.complete()
        services.return_task(pk=self.current().pk, actor=self.actor, comment="公司审批退回")
        self.assertEqual(self.current().name, "服务报告")

    def test_batch_failure_rolls_back_documents_and_assignment(self):
        self.reach("report_fill")
        files = [*self.files(), SimpleUploadedFile("invalid.docx", b"not scan")]
        with self.assertRaisesMessage(ValidationError, "扫描件仅支持"):
            services.upload_material_scans(
                material="report", pk=self.order.pk, actor=self.actor, files=files
            )
        self.assertFalse(self.order.documents.filter(material="report").exists())
        self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
        self.assertIsNone(self.current().started_at)

    def test_web_multiple_upload_and_all_current_files_visible(self):
        self.reach("report_fill")
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
        for scan in services.report_scan_sources(MaintenanceOrder.objects.get(pk=self.order.pk)):
            self.assertContains(page, f'value="{scan.pk}"')

    def test_api_enforces_scan_upload_without_electronic_source(self):
        self.reach("report_fill")
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
