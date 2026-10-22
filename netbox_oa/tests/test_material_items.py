"""Logical materials, revision links, approval evidence and system filenames."""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.models import MaintenanceDocument, MaintenanceOrder
from netbox_oa.tests import test_maintenance


class MaterialItemTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload

    def test_review_skips_workbench_history_and_reduces_queries(self):
        from unittest.mock import patch

        from django.test.utils import CaptureQueriesContext

        from netbox_oa.maintenance_views import presentation

        self.reach("payment_eip")
        self.order.refresh_from_db()
        with CaptureQueriesContext(connection) as full:
            presentation(self.order, self.actor)
        with CaptureQueriesContext(connection) as compact:
            detail = presentation(self.order, self.actor, materials_only=True)
        self.assertLess(len(compact), len(full))
        self.assertEqual(len(detail["materials"]), 7)
        self.client.force_login(self.actor)
        workbench = self.client.get(self.order.get_absolute_url())
        self.assertContains(workbench, 'aria-label="材料目录"')
        self.assertContains(workbench, 'target="review-order-frame"')
        self.assertContains(workbench, 'name="review-order-frame"')
        with patch("netbox_oa.maintenance_views.InboxPresentation", side_effect=AssertionError):
            page = self.client.get(reverse(
                "plugins:netbox_oa:maintenance_review", args=[self.order.pk]
            ))
        self.assertEqual(page.status_code, 200)

    def test_payment_evidence_is_direct_scan_without_stamping_approval(self):
        self.reach("payment_eip")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        for material in ("notice", "invoice", "contract", "verification"):
            with self.subTest(material=material):
                document = self.upload(material)
                self.assertFalse(document.is_sealed)
                self.assertIsNone(document.source_document_id)
                self.assertIn("_扫描件_v", document.display_name)
                self.assertEqual(Path(document.file.name).name, document.display_name)
                page = self.client.get(url, {"material": material})
                self.assertTrue(page.context["selected_group"]["can_upload"])
                entries = page.context["selected_group"]["index_rows"][0]["files"]
                self.assertEqual([entry["label"] for entry in entries], ["扫描件"])
                self.assertEqual(entries[0]["icon"], "mdi-file-image-outline")
                self.assertContains(page, 'data-file-type="scan"')
                self.assertIn("扫描件", next(
                    item["label"] for item in page.context["review_items"]
                    if item["document"].pk == document.pk
                ))
        self.complete()
        self.assertFalse(self.order.tasks.filter(status="pending").exists())

    def test_existing_active_payment_snapshot_gains_verification_once(self):
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps

        self.reach("payment_eip")
        self.order.refresh_from_db()
        step = self.order.steps_snapshot[self.current().position]
        step["required"] = [pair for pair in step["required"] if pair[0] != "verification"]
        self.order.save()
        migrate = import_module(
            "netbox_oa.migrations.0024_payment_verification"
        ).add_payment_verification
        schema_editor = SimpleNamespace(connection=connection)
        migrate(apps, schema_editor)
        migrate(apps, schema_editor)
        self.order.refresh_from_db()
        required = self.order.steps_snapshot[self.current().position]["required"]
        self.assertEqual(required.count(["verification", "electronic"]), 1)
        self.order.steps_snapshot[self.current().position]["required"] = []
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(
            status="completed", steps_snapshot=self.order.steps_snapshot,
        )
        migrate(apps, schema_editor)
        self.order.refresh_from_db()
        self.assertEqual(self.order.steps_snapshot[self.current().position]["required"], [])

    def test_order_has_one_logical_scan_and_preserves_prior_revisions(self):
        self.reach("order_scan_fill")
        first = self.upload("order", "scan")
        second = self.upload("order", "scan")
        self.assertEqual((first.item_number, second.item_number), (1, 1))
        self.assertEqual(second.replaces_document_id, first.pk)
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(list(services.material_scan_sources(order, "order")), [second])
        with self.assertRaisesMessage(ValidationError, "维护服务工单只有一份"):
            services.upload_material_scans(
                pk=order.pk, actor=self.actor, material="order",
                files=[SimpleUploadedFile("one.pdf", b"scan"),
                       SimpleUploadedFile("two.pdf", b"scan")],
            )
        self.assertTrue(MaintenanceDocument.objects.filter(pk=first.pk).exists())
        self.client.force_login(self.actor)
        page_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                           args=[order.pk, "order"])
        self.assertNotContains(self.client.get(page_url), "multiple")
        rejected = self.client.post(page_url, {"file": [
            SimpleUploadedFile("one.pdf", b"scan"), SimpleUploadedFile("two.pdf", b"scan"),
        ]})
        self.assertEqual(rejected.status_code, 200)
        self.assertTrue(rejected.context["form"].errors)

    def test_each_report_is_independent_and_update_keeps_its_number(self):
        self.reach("report_fill")
        first, other = self.upload("report"), self.upload("report")
        self.assertNotEqual(first.item_number, other.item_number)
        revised = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            file=SimpleUploadedFile("revised.pdf", b"scan"), upload_mode="replace",
            replaces_document_id=first.pk,
        )
        self.assertEqual(revised.item_number, first.item_number)
        self.assertEqual(revised.replaces_document_id, first.pk)
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(set(services.material_scan_sources(order, "report")), {revised, other})
        with self.assertRaisesMessage(ValidationError, "最新审批件"):
            services.upload_document(
                pk=order.pk, actor=self.actor, material="report", state="scan",
                file=SimpleUploadedFile("stale.pdf", b"scan"), upload_mode="replace",
                replaces_document_id=first.pk,
            )

    def test_sealing_links_to_exact_approved_revision_and_shows_both_files(self):
        self.reach("settlement_fill")
        first = self.upload("settlement")
        revised = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="settlement", state="scan",
            file=SimpleUploadedFile("revised.pdf", b"scan"), upload_mode="replace",
            replaces_document_id=first.pk,
        )
        while self.order.steps_snapshot[self.current().position]["key"] != "settlement_seal":
            self.complete()
        with self.assertRaisesMessage(ValidationError, "必须对应本次审批"):
            services.upload_document(
                pk=self.order.pk, actor=self.actor, material="settlement", state="scan",
                source_document_id=first.pk, file=SimpleUploadedFile("wrong.pdf", b"scan"),
            )
        sealed = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="settlement", state="scan",
            source_document_id=revised.pk, file=SimpleUploadedFile("sealed.pdf", b"scan"),
        )
        self.assertEqual(sealed.item_number, revised.item_number)
        self.assertEqual(sealed.source_document_id, revised.pk)
        self.client.force_login(self.actor)
        response = self.client.get(reverse("plugins:netbox_oa:maintenance_review",
                                           args=[self.order.pk]), {"material": "settlement"})
        self.assertEqual(response.context["selected_group"]["documents"], [sealed, revised])
        self.assertEqual(response.context["selected"], sealed)
        self.assertContains(response, f"对应审批件 v{revised.version}")
        self.assertContains(response, revised.short_name)
        self.assertContains(response, sealed.short_name)
        self.assertNotContains(response, revised.display_name)
        self.assertNotContains(response, sealed.display_name)
        self.assertEqual(response.context["selected_group"]["item_count"], 1)
        with self.assertRaisesMessage(ValidationError, "重复关联"):
            services.upload_document_files(
                pk=self.order.pk, actor=self.actor, material="settlement", state="scan",
                files=[SimpleUploadedFile("one.pdf", b"scan"),
                       SimpleUploadedFile("two.pdf", b"scan")],
                source_document_ids=[revised.pk, revised.pk],
            )

    def test_new_and_update_are_explicit_in_the_upload_form(self):
        self.reach("report_fill")
        first, other = self.upload("report"), self.upload("report")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_material_upload",
                      args=[self.order.pk, "report"])
        page = self.client.get(url)
        self.assertContains(page, "新增材料")
        self.assertContains(page, "更新已有材料")
        invalid = self.client.post(url, {
            "upload_mode": "replace", "file": SimpleUploadedFile("revision.pdf", b"scan"),
        })
        self.assertContains(invalid, "请为每个文件选择要更新的材料。")
        result = self.client.post(url, {
            "upload_mode": "replace", "replaces_document_ids": [first.pk, other.pk],
            "file": [SimpleUploadedFile("first.pdf", b"scan"),
                     SimpleUploadedFile("second.pdf", b"scan")],
        })
        self.assertEqual(result.status_code, 302)
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        active = list(services.material_scan_sources(order, "report"))
        self.assertEqual({doc.replaces_document_id for doc in active}, {first.pk, other.pk})

    def test_review_index_pairs_each_item_and_keeps_order_form_on_same_row(self):
        self.reach("report_fill")
        first, other = self.upload("report"), self.upload("report")
        while self.order.steps_snapshot[self.current().position]["key"] != "report_seal":
            self.complete()
        sealed = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            source_document_id=other.pk, file=SimpleUploadedFile("sealed.pdf", b"scan"),
        )
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        response = self.client.get(url, {"material": "report", "document": first.pk})
        rows = response.context["selected_group"]["index_rows"]
        self.assertEqual([row["label"] for row in rows], ["第 1 份", "第 2 份"])
        self.assertEqual([entry["document"] for entry in rows[0]["files"]], [first])
        self.assertEqual([entry["document"] for entry in rows[1]["files"]], [sealed, other])
        self.assertEqual([entry["label"] for entry in rows[1]["files"]], ["用印后", "审批件"])
        self.assertContains(response, f'aria-label="服务报告 第 1 份 审批件 v{first.version}"')
        self.assertEqual(response.context["selected"], first)
        report_items = [item for item in response.context["review_items"]
                        if item["group"]["key"] == "report"]
        self.assertEqual([item["document"] for item in report_items], [first, sealed, other])
        self.assertEqual([item["type"] for item in report_items], ["pdf", "pdf", "pdf"])
        self.assertEqual([item["document"] for item in report_items if item["preferred"]],
                         [first, sealed])
        self.assertEqual([item["item_id"] for item in report_items],
                         [first.pk, sealed.pk, sealed.pk])
        for document in [first, sealed, other]:
            self.assertContains(response, f'id="review-document-{document.pk}"')
        self.assertContains(response, 'aria-label="材料预览"')
        from netbox_oa.tests.test_inbox import PanelMarkup
        markup = PanelMarkup(response.content.decode())
        visible_documents = [attrs for tag, attrs in markup.elements
                             if tag == "article" and "hidden" not in attrs]
        self.assertEqual([attrs["data-document"] for attrs in visible_documents], [str(first.pk)])
        order_group = next(group for group in response.context["groups"] if group["key"] == "order")
        self.assertEqual(len(order_group["index_rows"]), 1)
        self.assertEqual([entry["label"] for entry in order_group["index_rows"][0]["files"]],
                         ["用印后", "审批件", "电子版"])

    def test_review_defaults_to_first_item_before_file_priority(self):
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        draft = self.client.get(url, {"material": "order", "embed": "1"})
        self.assertEqual(draft.context["selected"].kind, "form")
        self.reach("report_fill")
        first, second = self.upload("report"), self.upload("report")
        while self.order.steps_snapshot[self.current().position]["key"] != "report_seal":
            self.complete()
        second_sealed = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            source_document_id=second.pk, file=SimpleUploadedFile("second.pdf", b"scan"),
        )
        page = self.client.get(url, {"material": "report", "embed": "1"})
        self.assertEqual(page.context["selected"], first)
        rows = page.context["selected_group"]["index_rows"]
        self.assertEqual([row["default_document"] for row in rows], [first, second_sealed])
        from netbox_oa.tests.test_inbox import PanelMarkup

        links = [attrs for tag, attrs in PanelMarkup(page.content.decode()).elements
                 if tag == "a" and "material-row-link" in attrs.get("class", "")]
        report_links = [link for link in links if "material=report&" in link["href"]]
        self.assertEqual([link["href"] for link in report_links], [
            f"?embed=1&material=report&document={first.pk}",
            f"?embed=1&material=report&document={second_sealed.pk}",
        ])
        first_sealed = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            source_document_id=first.pk, file=SimpleUploadedFile("first.pdf", b"scan"),
        )
        self.assertEqual(self.client.get(url, {"material": "report"}).context["selected"],
                         first_sealed)
        self.assertEqual(self.client.get(url, {
            "material": "report", "document": second_sealed.pk,
        }).context["selected"], second_sealed)

    def test_uploaded_storage_and_download_names_are_normalized_original_is_preserved(self):
        self.reach("report_fill")
        document = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            file=SimpleUploadedFile("supplier-file.PDF", b"scan"),
        )
        self.assertEqual(document.original_name, "supplier-file.PDF")
        self.assertEqual(document.display_name,
                         f"{self.order.number}_服务报告_01_审批件_v{document.version}.pdf")
        self.assertEqual(Path(document.file.name).name, document.display_name)
        self.client.force_login(self.actor)
        download = self.client.get(document.get_absolute_url())
        self.assertEqual(download.status_code, 200)
        self.assertIn("filename*=utf-8", download["Content-Disposition"])
        download.close()

    def test_invalid_batch_revision_rolls_back_all_new_versions(self):
        self.reach("report_fill")
        first, other = self.upload("report"), self.upload("report")
        before = self.order.documents.count()
        with self.assertRaises(ValidationError):
            services.upload_material_scans(
                pk=self.order.pk, actor=self.actor, material="report", upload_mode="replace",
                replaces_document_ids=[first.pk, other.pk],
                files=[SimpleUploadedFile("ok.pdf", b"scan"),
                       SimpleUploadedFile("bad.exe", b"scan")],
            )
        self.assertEqual(self.order.documents.count(), before)
        self.assertFalse(first.replacement_documents.exists())
        self.assertFalse(other.replacement_documents.exists())


class MaterialItemMigrationTests(TransactionTestCase):
    tearDown = test_maintenance.MaintenanceTests.tearDown

    def setUp(self):
        # Other migration tests intentionally leave the schema at their target version.
        MigrationExecutor(connection).migrate([("netbox_oa", "0023_document_removed_at")])
        test_maintenance.MaintenanceTests.setUp(self)

    def test_existing_files_keep_storage_and_gain_item_and_revision_links(self):
        old_target = [("netbox_oa", "0021_maintenancesavedsignature")]
        new_target = [("netbox_oa", "0022_material_item_revisions")]
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        try:
            apps = executor.loader.project_state(old_target).apps
            Document = apps.get_model("netbox_oa", "MaintenanceDocument")
            common = {"order_id": self.order.pk, "uploaded_by_id": self.actor.pk,
                      "kind": "scan", "state": "scan"}
            first = Document.objects.create(
                **common, material="order", version=2, original_name="old.pdf", file="old.pdf"
            )
            revised = Document.objects.create(
                **common, material="order", version=3, original_name="new.pdf", file="new.pdf"
            )
            reports = [Document.objects.create(
                **common, material="report", version=index + 1,
                original_name=f"report{index}.pdf", file=f"report{index}.pdf",
            ) for index in range(2)]
            stamped = Document.objects.create(
                **common, material="report", version=3, source_document_id=reports[1].pk,
                is_sealed=True, original_name="stamped.pdf", file="stamped.pdf",
            )
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            MigratedDocument = executor.loader.project_state(new_target).apps.get_model(
                "netbox_oa", "MaintenanceDocument"
            )
            updated = MigratedDocument.objects.get(pk=revised.pk)
            self.assertEqual(updated.item_number, 1)
            self.assertEqual(updated.replaces_document_id, first.pk)
            self.assertEqual(updated.file.name, "new.pdf")
            self.assertEqual(MigratedDocument.objects.get(pk=reports[0].pk).item_number, 1)
            self.assertEqual(MigratedDocument.objects.get(pk=reports[1].pk).item_number, 2)
            linked = MigratedDocument.objects.get(pk=stamped.pk)
            self.assertEqual(linked.item_number, 2)
            self.assertEqual(linked.source_document_id, reports[1].pk)
            self.assertEqual(linked.file.name, "stamped.pdf")
        finally:
            MigrationExecutor(connection).migrate([("netbox_oa", "0023_document_removed_at")])
