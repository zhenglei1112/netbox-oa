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
        self.assertEqual(len(detail["materials"]), 6)
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
        for material in ("notice", "invoice"):
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

    def test_retired_payment_materials_cannot_be_uploaded(self):
        from netbox_oa.api.maintenance import DocumentWriteSerializer
        from netbox_oa.maintenance_forms import MaintenanceDocumentForm

        self.reach("payment_eip")
        self.client.force_login(self.actor)
        api_choices = DocumentWriteSerializer().fields["material"].choices
        form_choices = dict(MaintenanceDocumentForm().fields["material"].choices)
        for material in ("contract", "verification"):
            with self.subTest(material=material):
                self.assertNotIn(material, api_choices)
                self.assertNotIn(material, form_choices)
                with self.assertRaisesMessage(ValidationError, "不支持上传此类材料"):
                    self.upload(material)
                response = self.client.get(reverse(
                    "plugins:netbox_oa:maintenance_material_upload",
                    args=[self.order.pk, material],
                ))
                self.assertEqual(response.status_code, 404)
        self.assertFalse(self.order.documents.filter(
            material__in=("contract", "verification"),
        ).exists())

    def test_retired_payment_requirements_are_removed_but_history_is_preserved(self):
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps

        self.reach("payment_eip")
        self.order.refresh_from_db()
        step = self.order.steps_snapshot[self.current().position]
        step["required"].extend([["contract", "electronic"], ["verification", "electronic"]])
        original_steps = self.order.steps_snapshot.copy()
        self.order.save()
        historical = MaintenanceDocument.objects.create(
            order=self.order, material="contract", kind="attachment", state="electronic",
            version=1, original_name="old-contract.pdf", file="old-contract.pdf",
            uploaded_by=self.actor,
        )
        migrate = import_module(
            "netbox_oa.migrations.0028_remove_obsolete_payment_requirements"
        ).remove_obsolete_payment_requirements
        schema_editor = SimpleNamespace(connection=connection)
        migrate(apps, schema_editor)
        migrate(apps, schema_editor)
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.steps_snapshot[self.current().position]["required"],
            [["notice", "electronic"], ["invoice", "electronic"]],
        )
        historical.refresh_from_db()
        self.assertEqual(historical.file.name, "old-contract.pdf")
        self.assertIn("合同复印件", historical.short_name)
        self.client.force_login(self.actor)
        response = self.client.get(reverse(
            "plugins:netbox_oa:maintenance_review", args=[self.order.pk],
        ), {"material": "contract"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["selected_group"]["can_upload"])
        self.assertEqual(response.context["selected_group"]["documents"], [historical])

        MaintenanceOrder.objects.filter(pk=self.order.pk).update(
            status="completed", steps_snapshot=original_steps,
        )
        migrate(apps, schema_editor)
        self.order.refresh_from_db()
        self.assertEqual(self.order.steps_snapshot, original_steps)

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
        self.reach("order_sealed_upload")
        first = self.upload("order", "scan", sealed=True)
        second = self.upload("order", "scan", sealed=True)
        self.assertEqual((first.item_number, second.item_number), (1, 1))
        self.assertEqual(second.source_document_id, first.source_document_id)
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(list(services.material_scan_sources(order, "order")),
                         [first.source_document])
        with self.assertRaisesMessage(ValidationError, "当前节点不能上传此扫描材料"):
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
        self.reach("acceptance_apply")
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


    def test_new_and_update_are_explicit_in_the_upload_form(self):
        self.reach("acceptance_apply")
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


    def test_review_hides_single_item_labels_across_workbenches_and_contract_embed(self):
        from netbox_oa.tests.test_inbox import PanelMarkup

        self.reach("acceptance_apply")
        first = self.upload("report")
        self.client.force_login(self.actor)
        review_url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        contract_url = reverse("plugins:netbox_oa:contract_order_materials", kwargs={
            "contract_id": self.contract.pk, "pk": self.order.pk,
        })
        entries = [(review_url, {"embed": "1"}),
                   (review_url, {"embed": "1", "ui": "v2"}),
                   (contract_url, {"embed": "1"})]
        for multiple in (False, True):
            if multiple:
                self.upload("report")
            for url, query in entries:
                with self.subTest(url=url, query=query, multiple=multiple):
                    response = self.client.get(url, {**query, "material": "report"})
                    self.assertEqual(response.status_code, 200)
                    markup = PanelMarkup(response.content.decode())
                    row_links = [attrs for tag, attrs in markup.elements
                                 if tag == "a" and "material-row-link"
                                 in attrs.get("class", "").split()]
                    self.assertEqual(len(row_links), 2 if multiple else 0)
                    self.assertTrue(all("material=report&" in link["href"]
                                        for link in row_links))
                    file_links = [attrs for tag, attrs in markup.elements
                                  if tag == "a" and "material-file"
                                  in attrs.get("class", "").split()]
                    order_group = next(group for group in response.context["groups"]
                                       if group["key"] == "order")
                    self.assertEqual(len(order_group["index_rows"]), 1)
                    self.assertEqual(len(order_group["documents"]), 2)
                    self.assertEqual(len(file_links), sum(len(group["documents"])
                                                         for group in response.context["groups"]))
                    self.assertContains(
                        response, f'aria-label="服务报告 第 1 份 扫描件 v{first.version}"'
                    )
                    if multiple:
                        self.assertContains(response, '>第 1 份</a>')
                        self.assertContains(response, '>第 2 份</a>')
                    else:
                        self.assertNotContains(response, '>第 1 份</a>')


    def test_uploaded_storage_and_download_names_are_normalized_original_is_preserved(self):
        self.reach("acceptance_apply")
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
        self.reach("acceptance_apply")
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
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("netbox_oa"))
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
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes("netbox_oa"))
