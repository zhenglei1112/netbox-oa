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
        return services.upload_report_scans(pk=self.order.pk, actor=self.actor, files=self.files())

    def test_exact_four_nodes_and_configured_roles(self):
        nodes = [
            node
            for node in build_steps({"department": 2, "senior": 3}, 24)
            if node["stage"] == "report"
        ]
        self.assertEqual(
            [node["name"] for node in nodes],
            ["服务报告", "服务报告用印部门审核", "服务报告用印公司审批", "服务报告用印"],
        )
        self.assertEqual(
            [node["kind"] for node in nodes], ["submission", "approval", "approval", "action"]
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
            self.upload("report", "paper")
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
            services.upload_report_scans(pk=self.order.pk, actor=self.actor, files=files)
        self.assertFalse(self.order.documents.filter(material="report").exists())
        self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
        self.assertIsNone(self.current().started_at)

    def test_web_multi_upload_and_all_current_files_visible(self):
        self.reach("report_fill")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_upload", args=[self.order.pk])
        response = self.client.post(url, {"report_scans": "1", "file": self.files()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.order.documents.filter(material="report", kind="scan").count(), 2)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, "report-one.pdf")
        self.assertContains(page, "report-two.jpg")
        self.assertContains(page, "multiple")
        self.assertContains(page, "本次服务报告 · 2 份")
        self.complete()
        self.complete()
        self.complete()
        page = self.client.get(self.order.get_absolute_url())
        for scan in services.report_scan_sources(MaintenanceOrder.objects.get(pk=self.order.pk)):
            self.assertContains(page, f'name="source_document_id" value="{scan.pk}"')

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

    def test_legacy_snapshot_preserves_electronic_submission(self):
        self.reach("report_fill")
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        node = order.steps_snapshot[order.current_position]
        node.pop("report_scans")
        node["required"] = [["report", "electronic"]]
        order.save()
        document = services.upload_document(
            pk=order.pk,
            actor=self.actor,
            material="report",
            file=SimpleUploadedFile("legacy.docx", b"document"),
        )
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(task.evidence["report"], document.pk)

    def test_two_state_choices_and_legacy_state_migration(self):
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps

        self.assertEqual(
            MaintenanceDocument._meta.get_field("state").choices,
            [("electronic", "电子版"), ("scan", "扫描版")],
        )
        self.reach("report_fill")
        scans = self.batch()
        # Simulate pre-migration rows without changing source relationships or evidence.
        MaintenanceDocument.objects.filter(pk=scans[0].pk).update(state="sealed")
        MaintenanceDocument.objects.filter(pk=scans[1].pk).update(state="paper")
        migration = import_module("netbox_oa.migrations.0013_document_scan_states")
        migration.normalize_states(
            apps, SimpleNamespace(connection=SimpleNamespace(alias="default"))
        )
        scans[0].refresh_from_db()
        scans[1].refresh_from_db()
        self.assertEqual(scans[0].state, "scan")
        self.assertTrue(scans[0].is_sealed)
        self.assertEqual(scans[1].state, "scan")
        self.assertFalse(scans[1].is_sealed)
        self.assertIsNone(scans[0].source_document_id)

    def test_legacy_upload_values_are_normalized(self):
        self.reach("report_fill")
        document = self.upload("report", "paper")
        self.assertEqual(document.state, "scan")
        self.assertFalse(document.is_sealed)
