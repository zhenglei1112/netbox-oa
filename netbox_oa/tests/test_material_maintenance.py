from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.models import MaintenanceDocument, MaintenanceEvent, MaintenanceOrder
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


class MaterialMaintenanceTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload
    worker = test_maintenance.MaintenanceTests.worker

    def review(self, material):
        self.client.force_login(self.actor)
        return self.client.get(reverse("plugins:netbox_oa:maintenance_review",
                                       args=[self.order.pk]), {"material": material})

    def replace(self, document):
        url = reverse("plugins:netbox_oa:maintenance_material_upload",
                      args=[self.order.pk, document.material]) + f"?replace={document.pk}"
        self.client.force_login(self.actor)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "multiple")
        response = self.client.post(url, {"file": SimpleUploadedFile("new.pdf", b"scan")})
        self.assertEqual(response.status_code, 302, response.content)
        return self.order.documents.get(replaces_document=document)

    def test_approval_replace_keeps_item_and_delete_removes_slot_not_history(self):
        self.reach("acceptance_apply")
        first, second = self.upload("report"), self.upload("report")
        self.assertContains(self.review("report"), f"?replace={first.pk}")
        revised = self.replace(first)
        self.assertEqual(revised.item_number, first.item_number)
        services.remove_document(pk=revised.pk, actor=self.actor)
        group = self.review("report").context["selected_group"]
        self.assertEqual(group["documents"], [second])
        self.assertEqual(group["item_count"], 1)
        first.refresh_from_db()
        self.assertIsNotNone(first.removed_at)
        self.assertTrue(first.file.storage.exists(first.file.name))
        self.assertTrue(MaintenanceEvent.objects.filter(action="remove_material",
                                                       order=self.order).exists())
        self.assertEqual(set(services.material_scan_sources(
            MaintenanceOrder.objects.get(pk=self.order.pk), "report")), {second})
        with self.assertRaises(ValidationError):
            services.remove_document(pk=first.pk, actor=self.actor)


    def test_payment_attachment_replace_and_remove_do_not_revive_old_version(self):
        self.reach("payment_plan")
        first = self.upload("notice")
        revised = self.replace(first)
        self.assertEqual(revised.item_number, first.item_number)
        self.assertEqual(self.review("notice").context["selected_group"]["documents"], [revised])
        services.remove_document(pk=revised.pk, actor=self.actor)
        self.assertEqual(self.review("notice").context["selected_group"]["documents"], [])
        with self.assertRaisesMessage(ValidationError, "付款通知书"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self),
                                   external_reference="EIP-001")

    def test_approval_phase_form_and_cross_order_targets_cannot_be_modified(self):
        self.reach("acceptance_apply")
        document = self.upload("report")
        form = self.order.documents.get(kind="form", material="order")
        with self.assertRaises(ValidationError):
            services.remove_document(pk=form.pk, actor=self.actor)
        url = reverse("plugins:netbox_oa:maintenance_material_upload",
                      args=[self.order.pk, "report"]) + f"?replace={form.pk}"
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.complete()
        with self.assertRaises(ValidationError):
            services.remove_document(pk=document.pk, actor=self.actor)
        self.assertNotContains(self.review("report"), "data-material-remove")

    def test_remove_requires_delete_permission_and_assignment_and_post(self):
        self.reach("acceptance_apply")
        document = self.upload("report")
        user = self.worker("material_worker")
        for action in ("view", "add"):
            self.grant(user, "maintenancedocument", action)
        with self.assertRaises(PermissionDenied):
            services.remove_document(pk=document.pk, actor=user)
        self.grant(user, "maintenancedocument", "delete")
        user = type(user).objects.get(pk=user.pk)
        with self.assertRaises(PermissionDenied):
            services.remove_document(pk=document.pk, actor=user)
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_material_remove", args=[document.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertTrue(MaintenanceDocument.objects.filter(pk=document.pk).exists())

    def test_menu_hides_delete_action_without_permission(self):
        self.reach("acceptance_apply")
        document = self.upload("report")
        user = self.worker("upload_only")
        for action in ("view", "add"):
            self.grant(user, "maintenancedocument", action)
        self.grant(user, "maintenancetask", "view")
        task = self.current()
        task.assigned_user = user
        task.save()
        self.client.force_login(user)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        page = self.client.get(url, {"material": "report"})
        self.assertContains(page, f"?replace={document.pk}")
        self.assertNotContains(page, "删除材料")
        self.assertNotContains(page, "缺少材料删除权限")
        self.assertNotContains(page, "data-material-remove")
        self.grant(user, "maintenancedocument", "delete")
        page = self.client.get(url, {"material": "report"})
        self.assertContains(page, "data-material-remove")
        self.assertNotContains(page, "缺少材料删除权限")

    def test_return_delete_two_of_three_retains_original_for_resubmission(self):
        self.reach("acceptance_apply")
        for material in ("report", "settlement"):
            while self.order.steps_snapshot[self.current().position]["key"] != "acceptance_apply":
                self.complete()
            with self.subTest(material=material):
                for previous in services.material_scan_candidates(self.order, material):
                    services.remove_document(pk=previous.pk, actor=self.actor)
                originals = [self.upload(material) for _ in range(3)]
                self.complete()
                services.return_task(
                    pk=self.current().pk, actor=acting_user(self), comment="仅保留第一份",
                )
                with self.assertRaises(ValidationError):
                    services.complete_task(pk=self.current().pk, actor=acting_user(self))
                for document in originals[1:]:
                    services.remove_document(pk=document.pk, actor=self.actor)
                if material == "report":
                    # Existing deployments already recorded the deletions as audit events.
                    current = self.current()
                    current.evidence.pop("retained_materials")
                    current.save()
                order = MaintenanceOrder.objects.get(pk=self.order.pk)
                self.assertEqual(list(services.material_scan_sources(order, material)),
                                 originals[:1])
                self.client.force_login(self.actor)
                response = self.client.get(reverse("plugins:netbox_oa:maintenance_workbench"),
                                           {"tab": "all", "selected": order.pk})
                item = next(item for item in response.context["detail"]["materials"]
                            if item["key"] == material)
                self.assertTrue(item["stage_upload_complete"])
                self.assertContains(response, "本阶段材料已调整 · 保留 1 份")
                self.upload("acceptance")
                self.upload("settlement" if material == "report" else "report", "scan")
                test_maintenance.sign_task(self.current(), acting_user(self))
                completed = services.complete_task(pk=self.current().pk, actor=acting_user(self))
                self.assertEqual(completed.evidence[material], [originals[0].pk])
                # A subsequent return must require a new adjustment again.
                services.return_task(
                    pk=self.current().pk, actor=acting_user(self), comment="再次补正",
                )
                with self.assertRaises(ValidationError):
                    services.complete_task(pk=self.current().pk, actor=acting_user(self))
                services.remove_document(pk=originals[0].pk, actor=self.actor)
                with self.assertRaisesMessage(ValidationError, "至少上传一份"):
                    services.complete_task(pk=self.current().pk, actor=acting_user(self))
                self.upload(material)
