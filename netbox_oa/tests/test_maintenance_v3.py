"""HTTP regression coverage for the native maintenance UI and shared action returns."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_v3 import scoped_orders
from netbox_oa.models import MaintenanceOrder
from netbox_oa.tests import test_maintenance as maintenance
from netbox_oa.tests.test_maintenance import sign_draft
from netbox_oa.tests.test_signatures import image_data


class MaintenanceV3Tests(TestCase):
    setUp = maintenance.MaintenanceTests.setUp
    tearDown = maintenance.MaintenanceTests.tearDown
    grant = maintenance.MaintenanceTests.grant
    start = maintenance.MaintenanceTests.start
    current = maintenance.MaintenanceTests.current
    complete = maintenance.MaintenanceTests.complete
    upload = maintenance.MaintenanceTests.upload
    reach = maintenance.MaintenanceTests.reach

    def url(self, name="detail", **kwargs):
        if name in {"detail", "edit", "draft_signature"}:
            kwargs.setdefault("pk", self.order.pk)
        return reverse(f"plugins:netbox_oa:maintenance_v3_{name}", kwargs=kwargs)

    def login(self, actor=None):
        self.client.force_login(actor or self.actor)

    def action(self, operation, *, task=False, **data):
        name = "maintenancetask_action" if task else "maintenanceorder_action"
        return self.client.post(reverse(f"plugins:netbox_oa:{name}", kwargs={
            "pk": self.current().pk if task else self.order.pk, "operation": operation,
        }), {"return_query": "ui=v3&scope=todo&page=2", **data})

    def edit_data(self):
        return {
            "contract_id": self.contract.pk,
            "service_region": [region.pk for region in self.regions],
            "service_type": "fiber", "priority": "high", "supplier": self.provider.name,
            "service_location": "v3 服务地点", "requirements": "v3 服务要求",
            "contact_phone": "12345678", "return_query": "scope=mine&page=2",
        }

    def test_native_list_defaults_to_todo_and_links_to_v3(self):
        self.login()
        response = self.client.get(self.url("list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "generic/object_list.html")
        self.assertContains(response, self.order.number)
        self.assertContains(response, self.url() + "?scope=todo")
        self.assertNotContains(response, "maintenance-inbox.js")
        self.assertNotContains(response, "maintenance-v2.css")

    def test_native_detail_edit_and_material_templates(self):
        self.login()
        detail = self.client.get(self.url())
        self.assertTemplateUsed(detail, "generic/object.html")
        self.assertContains(detail, "提交审批")
        self.assertContains(detail, self.url("draft_signature"))
        self.assertNotContains(detail, "maintenance-inbox.js")
        self.assertNotContains(detail, "maintenance.css")
        self.assertTemplateUsed(self.client.get(self.url("edit")), "generic/object_edit.html")
        materials = self.client.get(self.url(), {"section": "materials"})
        self.assertContains(materials, "文件与历史版本")
        self.assertContains(materials, "查看 / 打印表单")

    def test_save_edit_returns_to_detail_and_does_not_submit(self):
        self.login()
        document = self.order.documents.get(material="order", kind="form")
        response = self.client.post(self.url("edit"), {
            **self.edit_data(), "expected_document_id": document.pk,
        })
        self.assertRedirects(response, self.url() + "?scope=mine&page=2")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "draft")
        self.assertEqual(self.order.requirements, "v3 服务要求")
        self.assertFalse(self.order.tasks.exists())

    def test_create_saves_draft_then_returns_to_v3(self):
        self.login()
        response = self.client.get(self.url("add"), {"scope": "mine"})
        form = response.context["form"]
        response = self.client.post(self.url("add"), {
            **self.edit_data(), "policy": self.policy.pk,
            "number_token": form.initial["number_token"],
        })
        created = MaintenanceOrder.objects.exclude(pk=self.order.pk).get()
        self.assertEqual(created.status, "draft")
        self.assertRedirects(response, reverse(
            "plugins:netbox_oa:maintenance_v3_detail", args=[created.pk],
        ) + "?scope=mine&page=2")

    def test_edit_version_conflict_keeps_form(self):
        self.login()
        response = self.client.post(self.url("edit"), {
            **self.edit_data(), "expected_document_id": "999999",
        })
        self.assertContains(response, "工单已在其他页面修改")
        self.assertTemplateUsed(response, "generic/object_edit.html")
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.requirements, "v3 服务要求")

    def test_draft_signature_capture_start_and_history(self):
        from netbox_oa import maintenance_signatures as signing
        from netbox_oa.models import MaintenanceSignature

        self.login()
        response = self.client.post(self.url("draft_signature"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("qr", response.json())
        signature = MaintenanceSignature.objects.get(document__order=self.order)
        signing.capture_signature(token=signing.mobile_token(signature), data_url=image_data())
        response = self.action("start")
        self.assertRedirects(response, self.url() + "?scope=todo&page=2")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "active")
        history = self.client.get(self.url(), {"section": "history"})
        self.assertContains(history, "审批签名")
        self.assertContains(history, "申请人签名")

    def test_unsigned_submission_error_stays_v3(self):
        self.login()
        response = self.action("start")
        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "netbox_oa/maintenance/v3/detail.html")
        self.assertContains(response, "完成手写签名", status_code=400)

    def test_edit_invalidates_existing_signature(self):
        self.login()
        sign_draft(self.order, self.actor)
        self.client.post(self.url("edit"), self.edit_data())
        response = self.action("start")
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "draft")

    def test_approval_return_correction_and_resubmit(self):
        self.login()
        self.start()
        task = self.current()
        response = self.action("return", task=True, comment="请补充说明")
        self.assertRedirects(response, self.url() + "?scope=todo&page=2")
        task.refresh_from_db()
        self.assertEqual(task.status, "returned")
        self.assertEqual(self.current().order.steps_snapshot[self.current().position]["key"],
                         "order_correction")
        response = self.client.post(self.url("edit"), self.edit_data())
        self.assertEqual(response.status_code, 302)
        from netbox_oa.tests.test_maintenance import sign_task

        sign_task(self.current())
        response = self.action("complete", task=True, comment="已补充")
        self.assertRedirects(response, self.url() + "?scope=todo&page=2")

    def test_approval_validation_error_keeps_comment_and_external_fields(self):
        self.login()
        self.start()
        with patch("netbox_oa.maintenance_services.complete_task", side_effect=PermissionDenied):
            response = self.action("complete", task=True, comment="保留审核意见")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "保留审核意见", status_code=403)
        self.assertTemplateUsed(response, "generic/object.html")

    def test_invalid_action_fields_are_displayed(self):
        self.login()
        self.start()
        response = self.action("complete", task=True, comment="超" * 4001)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context["action_form"].errors)
        self.assertEqual(response.context["action_form"]["comment"].value(), "超" * 4001)

    def test_follow_and_withdraw_return_v3(self):
        self.login()
        self.assertRedirects(self.action("follow"), self.url() + "?scope=todo&page=2")
        self.assertTrue(self.order.followers.filter(pk=self.actor.pk).exists())
        self.assertRedirects(self.action("withdraw"), self.url() + "?scope=todo&page=2")

    def test_scope_selection_and_search(self):
        self.login()
        self.order.followers.add(self.actor)
        for scope in ("todo", "mine", "following", "all"):
            response = self.client.get(self.url("list"), {"scope": scope})
            self.assertContains(response, self.order.number)
        self.assertNotContains(self.client.get(self.url("list"), {"scope": "done"}),
                               self.order.number)
        self.assertNotContains(self.client.get(self.url("list"), {"q": "does-not-exist"}),
                               self.order.number)
        self.start()
        self.complete()
        self.assertContains(self.client.get(self.url("list"), {"scope": "done"}),
                            self.order.number)

    def test_current_node_filter_and_sort(self):
        self.login()
        self.start()
        response = self.client.get(self.url("list"), {"node": self.current().name,
                                                      "sort": "v3_deadline"})
        self.assertContains(response, self.order.number)
        self.assertNotContains(self.client.get(self.url("list"), {"node": "missing-node"}),
                               self.order.number)

    def test_todo_requires_assignment_and_permission(self):
        other = get_user_model().objects.create_user(username="v3_other", password="test")
        for model in ("maintenanceorder", "maintenancetask"):
            for action in ("view", "change"):
                self.grant(other, model, action)
        self.assertFalse(scoped_orders(other, "todo").exists())
        self.start()
        self.assertFalse(scoped_orders(other, "todo").exists())
        other.groups.add(self.group)
        self.assertTrue(scoped_orders(other, "todo").exists())
        task = self.current()
        task.assigned_user = self.actor
        task.save()
        self.assertFalse(scoped_orders(other, "todo").exists())

    def test_unauthorized_and_anonymous_access(self):
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        other = get_user_model().objects.create_user(username="v3_unauthorized", password="test")
        self.login(other)
        for name in ("list", "detail", "edit", "add", "draft_signature"):
            response = self.client.post(self.url(name)) if name == "draft_signature" else (
                self.client.get(self.url(name))
            )
            self.assertIn(response.status_code, (403, 404))

    def test_native_dispatch_can_initialize_anonymous_queryset(self):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from netbox_oa.maintenance_v3 import MaintenanceV3ListView

        request = RequestFactory().get(self.url("list"))
        request.user = AnonymousUser()
        self.assertFalse(MaintenanceV3ListView().get_queryset(request).exists())

    def test_object_permissions_apply_to_all_list_scopes(self):
        self.login()
        queryset_class = type(MaintenanceOrder.objects.all())
        original = queryset_class.restrict

        def restricted(queryset, user, action):
            result = original(queryset, user, action)
            if queryset.model is MaintenanceOrder and action == "view":
                return result.exclude(pk=self.order.pk)
            return result

        with patch.object(queryset_class, "restrict", restricted):
            for scope in ("todo", "mine", "all", "following", "done"):
                response = self.client.get(self.url("list"), {"scope": scope})
                self.assertNotContains(response, self.order.number)
            self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_list_pagination_and_filter_context(self):
        self.login()
        data = {
            field.attname: getattr(self.order, field.attname)
            for field in MaintenanceOrder._meta.fields
            if not field.primary_key and field.name not in {"number", "created", "last_updated"}
        }
        MaintenanceOrder.objects.bulk_create([
            MaintenanceOrder(number=f"V3-PAGE-{i:03}", **data) for i in range(27)
        ])
        response = self.client.get(self.url("list"), {"scope": "all", "page": 2, "q": "V3-PAGE"})
        self.assertEqual(response.context["table"].paginator.count, 27)
        self.assertEqual(len(response.context["table"].page.object_list), 2)
        self.assertContains(response, "scope=all&amp;q=V3-PAGE&amp;page=2")
        self.assertIsInstance(response.context["table"].list_query, QueryDict)

    def test_no_untrusted_redirects(self):
        self.login()
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenanceorder_action", args=[self.order.pk, "follow"],
        ), {"return_query": "ui=v3&next=https://example.org&scope=todo"})
        self.assertRedirects(response, self.url() + "?scope=todo")

    def test_active_order_cannot_be_edited_during_review(self):
        self.login()
        self.start()
        self.assertEqual(self.client.get(self.url("edit")).status_code, 403)
        self.assertNotContains(self.client.get(self.url()), '> 编辑</a>')

    def test_material_upload_revision_delete_and_history_stay_v3(self):
        self.login()
        self.reach("report_fill")
        upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                             args=[self.order.pk, "report"])
        response = self.client.post(upload_url + "?embed=1", {
            "file": SimpleUploadedFile("first.pdf", b"report", "application/pdf"),
            "upload_mode": "new",
        })
        self.assertEqual(response.status_code, 200)
        first = self.order.documents.get(material="report")
        response = self.client.post(upload_url + f"?embed=1&replace={first.pk}", {
            "file": SimpleUploadedFile("second.pdf", b"updated report", "application/pdf"),
        })
        self.assertEqual(response.status_code, 200)
        second = self.order.documents.get(replaces_document=first)
        self.assertEqual(first.item_number, second.item_number)
        materials = self.client.get(self.url(), {"section": "materials"})
        self.assertContains(materials, f"replace={second.pk}")
        self.assertContains(materials, "first.pdf")
        self.assertContains(materials, "second.pdf")
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenance_material_remove", args=[second.pk],
        ))
        self.assertEqual(response.status_code, 200)
        self.assertContains(self.client.get(self.url(), {"section": "materials"}), "已删除")
        response = self.action("complete", task=True, comment="材料检查")
        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "netbox_oa/maintenance/v3/detail.html")
        self.assertContains(self.client.get(self.url(), {"section": "history"}), "删除材料")

    def test_sealed_upload_materials_link_exact_approval_version(self):
        self.login()
        self.reach("report_fill")
        source = self.upload("report")
        while self.order.steps_snapshot[self.current().position]["key"] != "report_sealed_upload":
            self.complete()
        sealed = services.upload_document(
            pk=self.order.pk, actor=self.actor, material="report", state="scan",
            source_document_id=source.pk,
            file=SimpleUploadedFile("sealed.pdf", b"sealed", "application/pdf"),
        )
        response = self.client.get(self.url(), {"section": "materials"})
        self.assertContains(response, "用印后")
        materials = response.context["detail"]["materials"]
        report = next(item for item in materials if item["key"] == "report")
        self.assertTrue(report["stage_upload_complete"])
        self.assertEqual([doc.pk for doc in report["maintainable"]], [sealed.pk])
        self.assertEqual(sealed.source_document_id, source.pk)

    def test_stale_task_submission_preserves_comment(self):
        self.login()
        self.start()
        task_id = self.current().pk
        self.complete()
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenancetask_action", args=[task_id, "complete"],
        ), {"return_query": "ui=v3", "comment": "重复提交的意见"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "重复提交的意见", status_code=400)
        self.assertContains(response, "任务已处理", status_code=400)

    def test_draft_signature_rejects_non_requester_and_requires_csrf(self):
        from django.test import Client

        other = get_user_model().objects.create_superuser(
            username="v3_other_admin", email="other@example.test", password="test",
        )
        self.login(other)
        self.assertEqual(self.client.post(self.url("draft_signature")).status_code, 403)
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.actor)
        self.assertEqual(protected.post(self.url("draft_signature")).status_code, 403)

    def test_transfer_countersign_and_terminate_return_to_v3(self):
        self.login()
        self.start()
        other = get_user_model().objects.create_user(username="v3_transfer", password="test")
        other.groups.add(self.group)
        for model in ("maintenanceorder", "maintenancetask"):
            for action in ("view", "change"):
                self.grant(other, model, action)
        response = self.action("transfer", task=True, user_id=other.pk, comment="请协助")
        self.assertRedirects(response, self.url() + "?scope=todo&page=2")
        self.assertEqual(self.current().assigned_user_id, other.pk)
        response = self.action("countersign", task=True, user_id=other.pk, comment="请会签")
        self.assertRedirects(response, self.url() + "?scope=todo&page=2")
        response = self.action("terminate", comment="测试结束")
        self.assertRedirects(response, self.url() + "?scope=todo&page=2")

    def test_review_retains_v3_return_when_switching_material(self):
        self.login()
        response = self.client.get(reverse(
            "plugins:netbox_oa:maintenance_review", args=[self.order.pk],
        ), {"material": "order", "ui": "v3", "scope": "mine", "page": "2"})
        self.assertContains(response, self.url() + "?scope=mine&amp;page=2")
        self.assertContains(response, "ui=v3&amp;scope=mine&amp;page=2&amp;material=report")

    def test_print_and_download_form_keep_v3_return(self):
        self.login()
        document = self.order.documents.get(material="order", kind="form")
        query = {"ui": "v3", "scope": "mine", "page": 2, "section": "materials"}
        download = self.client.get(reverse(
            "plugins:netbox_oa:maintenance_download", args=[document.pk],
        ), query)
        self.assertEqual(download.status_code, 302)
        self.assertIn("ui=v3", download.url)
        response = self.client.get(download.url)
        self.assertContains(response, self.url() + "?scope=mine&amp;page=2&amp;section=materials")

    def test_v3_header_list_deadline_and_materials_display_full_timestamps(self):
        from datetime import datetime, timezone

        self.login()
        self.start()
        stamp = datetime(2026, 4, 10, 8, 21, 32, tzinfo=timezone.utc)
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(created=stamp, last_updated=stamp)
        self.order.documents.update(created=stamp)
        self.order.tasks.filter(status="pending").update(deadline=stamp)
        response = self.client.get(self.url())
        self.assertContains(response, "2026-04-10 16:21:32", count=3)
        response = self.client.get(self.url("list"))
        self.assertEqual(response.context["table"].rows[0].get_cell("v3_deadline"),
                         "2026-04-10 16:21:32")
        self.assertEqual(response.context["table"].rows[0].get_cell("created"),
                         "2026-04-10 16:21:32")
        response = self.client.get(self.url(), {"section": "materials"})
        self.assertContains(response, "2026-04-10 16:21:32", count=3)
