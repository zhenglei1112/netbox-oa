
"""Operation coverage and the native ObjectChange adapter contract.

The standalone harness substitutes NetBox's base model. The adapter tests patch only
its to_objectchange boundary; production keeps NetBox's own signal/user/request handling.
"""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save
from django.forms.models import model_to_dict
from django.test import Client, TestCase
from django.urls import reverse
from netbox.models import ChangeLoggedModel

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_materials import company_form_initial
from netbox_oa.maintenance_views import presentation
from netbox_oa.models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceLink,
    MaintenanceOrder,
    MaintenanceTask,
)
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


def native_change(obj, action):
    return SimpleNamespace(
        changed_object=obj,
        action=action,
        message=getattr(obj, "_changelog_message", "") or "",
        postchange_data=model_to_dict(obj),
    )


class MaintenanceAuditTests(TestCase):
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    upload = test_maintenance.MaintenanceTests.upload
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    worker = test_maintenance.MaintenanceTests.worker
    grant = test_maintenance.MaintenanceTests.grant

    def setUp(self):
        test_maintenance.MaintenanceTests.setUp(self)
        self.client.force_login(self.actor)

    @contextmanager
    def native_changes(self):
        changes = []

        def capture(sender, instance, created, **_kwargs):
            if isinstance(instance, (MaintenanceOrder, MaintenanceTask, MaintenanceDocument,
                                     MaintenanceEvent, MaintenanceLink)):
                change = instance.to_objectchange("create" if created else "update")
                if change is not None:
                    changes.append(change)

        with patch.object(ChangeLoggedModel, "to_objectchange", native_change, create=True):
            post_save.connect(capture)
            try:
                yield changes
            finally:
                post_save.disconnect(capture)

    def test_native_audit_uses_business_events_instead_of_raw_model_saves(self):
        document = self.order.documents.get()
        task = MaintenanceTask(order=self.order, name="审核任务")
        link = MaintenanceLink(
            order=self.order, object_type=ContentType.objects.get_for_model(self.order),
            object_id=self.order.pk, label=self.order.number,
        )
        with patch.object(ChangeLoggedModel, "to_objectchange", native_change, create=True):
            for obj in (self.order, document, task, link):
                for action in ("create", "update"):
                    with self.subTest(model=type(obj).__name__, action=action):
                        self.assertIsNone(obj.to_objectchange(action))
            event = self.order.events.get(action="create")
            change = event.to_objectchange("create")
            self.assertEqual(change.related_object, self.order)
            self.assertEqual(change.message, "创建流程")
            self.assertIn(self.order.number, change.object_repr)
            # Automatic initial form generation is part of creation, not another save.
            self.assertIsNone(self.order.events.get(action="form").to_objectchange("create"))

    def test_only_six_operation_categories_are_emitted_with_concise_payloads(self):
        labels = set()
        with patch.object(ChangeLoggedModel, "to_objectchange", native_change, create=True):
            for action in (*MaintenanceEvent.AUDIT_OPERATIONS, "signature", "follow", "unfollow",
                           "link", "preview", "download", "terminate", "withdraw", "view"):
                event = services._event(
                    self.order, self.actor, action, "详细业务动作",
                    comment="办理意见" * 500, material="report", version=12, document_id=7,
                )
                change = event.to_objectchange("create")
                self.assertEqual(event.details["comment"], "办理意见" * 500)
                if action in MaintenanceEvent.AUDIT_OPERATIONS:
                    labels.add(change.message)
                    self.assertNotIn("details", change.postchange_data)
                    self.assertNotIn("comment", change.postchange_data)
                    self.assertNotIn("办理意见", change.message)
                    self.assertEqual(change.postchange_data["document_id"], 7)
                    self.assertIsNone(event.to_objectchange("update"))
                else:
                    self.assertIsNone(change)
        self.assertEqual(labels, {"创建流程", "批转流程", "保存表单",
                                  "打印表单", "上传材料", "删除材料"})

    def test_create_and_form_save_each_emit_one_native_record(self):
        fields = ("service_type", "contract_id", "contract_number", "supplier", "service_region",
                  "service_region_ids", "service_location", "requirements")
        with self.native_changes() as changes:
            order = services.create_order(
                actor=self.actor, policy=self.policy,
                **{key: getattr(self.order, key) for key in fields},
            )
            self.assertEqual([change.message for change in changes], ["创建流程"])
            services.update_order(pk=order.pk, actor=self.actor, requirements="修订要求")
            self.assertEqual([change.message for change in changes], ["创建流程", "保存表单"])
            services.update_order(pk=order.pk, actor=self.actor, requirements="修订要求")
            self.assertEqual([change.message for change in changes],
                             ["创建流程", "保存表单", "保存表单"])

    def test_no_change_save_is_audited_without_revising_document(self):
        version_ids = list(self.order.documents.values_list("pk", flat=True))
        services.update_order(
            pk=self.order.pk, actor=self.actor, requirements=self.order.requirements
        )
        event = self.order.events.get(action="save")
        self.assertEqual(event.actor, self.actor)
        self.assertEqual(event.details["fields"], [])
        self.assertEqual(list(self.order.documents.values_list("pk", flat=True)), version_ids)

    def test_internal_save_keeps_business_history_without_native_save_records(self):
        with self.native_changes() as changes:
            services.update_order(
                pk=self.order.pk, actor=self.actor, requirements="签名前保存内容", audit=False,
            )
            services.update_order(
                pk=self.order.pk, actor=self.actor, requirements="签名前保存内容", audit=False,
            )
        self.assertEqual(changes, [])
        self.order.refresh_from_db()
        self.assertEqual(self.order.requirements, "签名前保存内容")
        self.assertEqual(set(self.order.events.filter(action__in=("edit", "save"))
                             .values_list("action", flat=True)), {"edit", "save"})
        self.assertFalse(self.order.events.filter(action__in=("edit", "save"),
                                                 details__audit=True).exists())

    def test_failed_mutation_rolls_back_changes_and_its_operation_records(self):
        before = self.order.events.count(), self.order.documents.count()
        original = services._event

        def fail_after_event(*args, **kwargs):
            original(*args, **kwargs)
            raise ValidationError("操作失败")

        with patch.object(services, "_event", side_effect=fail_after_event):
            with self.assertRaises(ValidationError):
                services.update_order(pk=self.order.pk, actor=self.actor, requirements="修订内容")
        self.assertEqual((self.order.events.count(), self.order.documents.count()), before)
        self.order.refresh_from_db()
        self.assertEqual(self.order.requirements, "修复光缆中断故障")

    def test_form_save_records_material_version_and_changelog_message_before_save(self):
        saved = []

        def capture(sender, instance, **_kwargs):
            saved.append((instance.action, instance._changelog_message))

        post_save.connect(capture, sender=MaintenanceEvent)
        try:
            document = services.save_company_form(
                pk=self.order.pk, actor=self.actor, material="order",
                data=company_form_initial(self.order),
            )
        finally:
            post_save.disconnect(capture, sender=MaintenanceEvent)
        event = self.order.events.filter(action="form").first()
        self.assertEqual(event.details["document_id"], document.pk)
        self.assertEqual(event.details["version"], document.version)
        self.assertEqual(saved[-1][1], "保存表单")

    def test_upload_replace_and_delete_log_every_affected_document_version(self):
        self.reach("acceptance_apply")
        first = self.upload("report")
        revised = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            file=SimpleUploadedFile("revision.pdf", b"scan"),
            replaces_document_id=first.pk, upload_mode="replace",
        )
        upload = self.order.events.filter(action="upload").first()
        self.assertEqual(upload.task_id, self.current().pk)
        self.assertEqual(upload.details["replaces_document_id"], first.pk)
        self.assertEqual(upload.details["item_number"], revised.item_number)
        saved = []

        def capture(sender, instance, **_kwargs):
            saved.append(instance.pk)

        post_save.connect(capture, sender=MaintenanceDocument)
        try:
            services.remove_document(pk=revised.pk, actor=self.actor)
        finally:
            post_save.disconnect(capture, sender=MaintenanceDocument)
        self.assertCountEqual(saved, [first.pk, revised.pk])
        event = self.order.events.get(action="remove_material")
        self.assertCountEqual(event.details["removed_document_ids"], saved)
        self.assertTrue(self.order.documents.get(pk=first.pk).file)

    def test_transfer_and_completion_preserve_full_opinions(self):
        self.reach("order_review")
        task = self.current()
        target = self.worker("audit_transferee")
        comment = "批转意见：请核实合同及本次材料。"
        services.transfer_task(pk=task.pk, actor=self.actor, user_id=target.pk, comment=comment)
        event = self.order.events.get(action="transfer")
        self.assertEqual(event.details["comment"], comment)
        self.assertEqual(event.details["target_user"], target.pk)
        self.assertEqual(event.operation_summary, "批转流程")
        self.grant(target, "maintenancedocument", "view")
        test_maintenance.sign_task(task, target)
        services.complete_task(pk=task.pk, actor=target, comment="同意办理")
        event = self.order.events.filter(action="complete").first()
        self.assertEqual(event.details["comment"], "同意办理")
        self.assertEqual(event.task.comment, "同意办理")

    def test_each_save_refreshes_the_snapshot_for_the_next_task_change(self):
        self.reach("order_review")
        task = self.current()
        baselines = []

        def snapshot(obj):
            obj._prechange_snapshot = deepcopy(model_to_dict(obj))

        def capture(sender, instance, **_kwargs):
            if instance.pk == task.pk:
                baselines.append(deepcopy(instance._prechange_snapshot))

        test_maintenance.sign_task(task, acting_user(self))
        task.refresh_from_db()
        with patch.object(ChangeLoggedModel, "snapshot", snapshot):
            post_save.connect(capture, sender=MaintenanceTask)
            try:
                services.complete_task(pk=task.pk, actor=acting_user(self), comment="已核实")
            finally:
                post_save.disconnect(capture, sender=MaintenanceTask)
        self.assertEqual(len(baselines), 2)
        self.assertIsNotNone(baselines[0]["started_at"])
        self.assertIsNotNone(baselines[1]["started_at"])
        self.assertEqual(baselines[1]["status"], "pending")
        self.assertEqual(baselines[1]["comment"], "")

    def test_material_preview_and_download_do_not_add_native_or_business_audit_noise(self):
        self.reach("acceptance_apply")
        document = self.upload("report")
        before = self.order.events.count()
        with self.native_changes() as changes:
            for route in ("maintenance_preview", "maintenance_download"):
                response = self.client.get(
                    reverse(f"plugins:netbox_oa:{route}", args=[document.pk])
                )
                self.assertEqual(response.status_code, 200)
                response.close()
        self.assertEqual(self.order.events.count(), before)
        self.assertEqual(changes, [])

    def test_opening_refreshing_and_browsing_flow_do_not_add_audit_records(self):
        base = reverse("plugins:netbox_oa:maintenance_workbench")
        before = self.order.events.count()
        with self.native_changes() as changes:
            for url, params, headers in (
                (base, {}, {}),
                (self.order.get_absolute_url(), {}, {}),
                (self.order.get_absolute_url(), {}, {}),
                (base, {"selected": self.order.pk}, {}),
                (base, {"selected": self.order.pk, "restore": "1"}, {}),
                (base, {"selected": self.order.pk, "history": "100"}, {}),
                *((base, {"selected": self.order.pk}, {
                    "HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_X_OA_NAVIGATION": mode,
                }) for mode in ("search", "all", "detail")),
            ):
                with self.subTest(url=url, params=params, headers=headers):
                    self.assertEqual(self.client.get(url, params, **headers).status_code, 200)
        self.assertEqual(self.order.events.count(), before)
        self.assertEqual(changes, [])

    def test_printing_does_not_replace_workflow_progress(self):
        before = presentation(self.order, self.actor, with_details=False)
        services.record_form_print(pk=self.order.documents.get().pk, actor=self.actor)
        after = presentation(self.order, self.actor, with_details=False)
        self.assertEqual(before["latest_activity"], after["latest_activity"])
        self.assertEqual(before["activity_at"], after["activity_at"])

    def test_print_is_post_csrf_protected_and_preview_does_not_claim_printing(self):
        document = self.order.documents.get()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_print_event", args=[document.pk])
        page = client.get(reverse("plugins:netbox_oa:maintenance_print", args=[document.pk]))
        self.assertContains(page, 'data-audit-url=')
        self.assertFalse(self.order.events.filter(action="print").exists())
        self.assertFalse(self.order.events.filter(action="preview").exists())
        self.assertEqual(client.get(url).status_code, 405)
        self.assertEqual(client.post(url).status_code, 403)
        response = client.post(url, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertEqual(response.status_code, 200)
        event = self.order.events.get(action="print")
        self.assertEqual(response.json()["event_id"], event.pk)
        self.assertEqual(event.details["document_id"], document.pk)
        self.assertEqual(event.actor, self.actor)

    def test_denied_read_and_print_operations_never_create_a_success_record(self):
        outsider = get_user_model().objects.create_user("audit_outsider", password="test")
        self.client.force_login(outsider)
        document = self.order.documents.get()
        before = self.order.events.count()
        for route in ("maintenance_print", "maintenance_download", "maintenance_preview"):
            response = self.client.get(reverse(f"plugins:netbox_oa:{route}", args=[document.pk]))
            self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse("plugins:netbox_oa:maintenance_print_event",
                                            args=[document.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.order.events.count(), before)

    def test_follow_and_unfollow_are_distinct_operations(self):
        services.follow_order(pk=self.order.pk, actor=self.actor)
        self.assertTrue(self.order.events.filter(action="follow", actor=self.actor).exists())
        services.follow_order(pk=self.order.pk, actor=self.actor)
        self.assertTrue(self.order.events.filter(action="unfollow", actor=self.actor).exists())
