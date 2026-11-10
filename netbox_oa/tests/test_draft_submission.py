
"""Draft signing and submission through the current production workflow."""

from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa import maintenance_signatures as signing
from netbox_oa.models import MaintenanceOrder, MaintenanceSignature
from netbox_oa.tests import test_maintenance, test_ordering
from netbox_oa.tests.test_maintenance import acting_user
from netbox_oa.tests.test_signatures import image_data


class DraftSubmissionTests(TestCase):
    tearDown = test_maintenance.MaintenanceTests.tearDown
    current = test_maintenance.MaintenanceTests.current
    upload = test_maintenance.MaintenanceTests.upload
    grant = test_maintenance.MaintenanceTests.grant
    worker = test_maintenance.MaintenanceTests.worker

    def setUp(self):
        test_maintenance.MaintenanceTests.setUp(self)
        self.client.force_login(self.actor)

    def start(self):
        signature = test_maintenance.sign_draft(self.order, self.actor)
        self.order = services.start_order(pk=self.order.pk, actor=self.actor)
        return signature

    def complete(self):
        return test_maintenance.MaintenanceTests.complete(self)

    def form_data(self):
        return {
            "service_type": "fiber",
            "priority": "medium",
            "contract_id": self.contract.pk,
            "service_region": [r.pk for r in self.regions],
            "service_location": "惠州站点",
            "contact_phone": "",
            "requirements": self.order.requirements,
        }

    def test_native_changelog_layout_and_create_message(self):
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        response = self.client.get(url)
        self.assertContains(response, 'name="changelog_message"', count=1)
        self.assertContains(response, "bg-primary-subtle border border-primary rounded-1")
        self.assertNotContains(response, "sticky-actions-footer")
        self.assertNotContains(response, "<hr")
        html = response.content.decode()
        self.assertLess(
            html.index("oa-applicant-signature"), html.index('id="id_changelog_message"')
        )
        data = {
            **test_ordering.OrderingTests.create_data(self),
            "policy": self.policy.pk,
            "priority": "medium",
            "changelog_message": "创建维护工单",
            "_save": "1",
        }
        saved_messages = []
        save = services._save

        def record_save(obj):
            if isinstance(obj, MaintenanceOrder):
                saved_messages.append(obj._changelog_message)
            return save(obj)

        with patch.object(services, "_save", side_effect=record_save):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(saved_messages, ["创建维护工单"])

    def test_edit_changelog_does_not_revise_signed_document(self):
        signature = test_maintenance.sign_draft(self.order, self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk])
        data = {**self.form_data(), "changelog_message": "补充变更说明", "_save": "1"}
        saved_messages = []
        save = services._save

        def record_save(obj):
            if isinstance(obj, MaintenanceOrder):
                saved_messages.append(obj._changelog_message)
            return save(obj)

        with patch.object(services, "_save", side_effect=record_save):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(saved_messages, ["补充变更说明"])
        self.assertEqual(self.order.documents.count(), 1)
        self.assertEqual(self.order.documents.first().pk, signature.document_id)
        response = self.client.post(url, {**data, "changelog_message": "x" * 201})
        self.assertEqual(response.status_code, 200)
        self.assertIn("changelog_message", response.context["form"].errors)

    def test_signed_draft_keeps_save_and_add_another_without_duplicate_order(self):
        signature = test_maintenance.sign_draft(self.order, self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk])
        self.assertContains(self.client.get(url), 'name="_addanother"')
        count = MaintenanceOrder.objects.count()
        response = self.client.post(
            url,
            {
                **self.form_data(),
                "expected_document_id": signature.document_id,
                "_addanother": "1",
            },
        )
        self.assertRedirects(response, reverse("plugins:netbox_oa:maintenanceorder_add"))
        self.assertEqual(MaintenanceOrder.objects.count(), count)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "draft")
        self.assertEqual(self.order.documents.first().pk, signature.document_id)

    def test_priority_change_revises_print_snapshot_and_requires_resigning(self):
        signature = test_maintenance.sign_draft(self.order, self.actor)
        original = self.order.documents.first()
        old_priority = original.form_data["priority"]
        services.update_order(pk=self.order.pk, actor=self.actor, priority="high")
        latest = self.order.documents.first()
        self.assertNotEqual(latest.pk, original.pk)
        self.assertEqual(latest.form_data["priority"], "high")
        original.refresh_from_db()
        self.assertEqual(original.form_data["priority"], old_priority)
        with self.assertRaisesMessage(ValidationError, "表单版本已变化"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        printed = self.client.get(reverse("plugins:netbox_oa:maintenance_print", args=[latest.pk]))
        self.assertContains(printed, "紧急程度")
        self.assertNotContains(printed, "验收情况 / 甲方意见")
        self.assertContains(printed, '<th>复核</th>', html=True)
        self.assertContains(printed, '<th>部门审核</th>', html=True)
        self.assertContains(printed, 'colspan="4" class="seal-cell"')
        self.assertContains(printed, "<strong>甲方：中交信通网络科技有限公司</strong>", html=True)
        self.assertContains(printed, "<strong>盖章：</strong>", html=True)
        self.assertContains(printed, '<strong class="seal-date">日期：</strong>', html=True)
        self.assertEqual(signature.document_id, original.pk)

    def test_full_current_flow_starts_at_review_and_never_visits_correction(self):
        signature = self.start()
        self.assertEqual(self.current().name, "工单复核")
        self.assertEqual(
            len([n for n in self.order.steps_snapshot if not n.get("return_only")]), 14
        )
        self.assertNotIn("order_fill", [n["key"] for n in self.order.steps_snapshot])
        while self.order.tasks.filter(status="pending").exists():
            self.complete()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        self.assertEqual(self.order.tasks.count(), 14)
        self.assertFalse(self.order.tasks.filter(name="工单修正").exists())
        signature.refresh_from_db()
        self.assertIsNotNone(signature.submitted_at)
        self.assertIsNone(signature.task_id)

    def test_unsigned_and_stale_signatures_cannot_start_and_start_is_not_repeatable(self):
        with self.assertRaisesMessage(ValidationError, "申请人完成手写签名"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        self.assertFalse(self.order.tasks.exists())
        test_maintenance.sign_draft(self.order, self.actor)
        self.upload("order")
        with self.assertRaisesMessage(ValidationError, "表单版本已变化"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        signature = self.start()
        with self.assertRaisesMessage(ValidationError, "已提交审批"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        self.assertEqual(self.order.tasks.count(), 1)
        with self.assertRaises(ValidationError):
            signing.cancel_signature(pk=signature.pk, actor=self.actor)

    def test_new_page_saves_and_signs_before_direct_submission_without_duplicate_order(self):
        data = {
            **test_ordering.OrderingTests.create_data(self),
            "policy": self.policy.pk,
            "priority": "medium",
        }
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        self.assertContains(self.client.get(url), "保存草稿")
        response = self.client.post(
            url, {**data, "_signature": "1"}, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        signature = MaintenanceSignature.objects.get(
            document_id=result["document_id"], task__isnull=True
        )
        order = signature.document.order
        self.assertEqual(order.status, "draft")
        self.assertFalse(order.tasks.exists())
        signing.capture_signature(token=signing.mobile_token(signature), data_url=image_data())
        response = self.client.post(
            result["form_action"],
            {**data, "expected_document_id": signature.document_id, "_submit": "1"},
        )
        self.assertEqual(response.status_code, 302, response.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "active")
        self.assertEqual(order.tasks.get(status="pending").name, "工单复核")
        self.assertEqual(order.documents.count(), 1)
        self.assertEqual(MaintenanceOrder.objects.count(), 2)
        response = self.client.post(
            result["form_action"],
            {**data, "expected_document_id": signature.document_id, "_submit": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(order.tasks.count(), 1)
        printed = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[signature.document_id])
        )
        image_url = reverse("plugins:netbox_oa:maintenance_signature_image", args=[signature.pk])
        self.assertContains(printed, image_url)

    def test_draft_edit_invalidates_signature_and_rejects_stale_browser(self):
        signature = test_maintenance.sign_draft(self.order, self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk])
        data = {
            **self.form_data(),
            "expected_document_id": signature.document_id,
            "requirements": "修改后的服务内容",
            "_save": "1",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        with self.assertRaisesMessage(ValidationError, "表单版本已变化"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        response = self.client.post(
            url, {**data, "_signature": "1"}, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("其他页面修改", response.json()["error"])
        self.assertEqual(self.order.documents.filter(kind="form").count(), 2)

    def test_both_approvals_return_to_correction_and_require_revision_and_new_signature(self):
        original = self.start()
        for position in ("order_review", "order_final"):
            if position == "order_final":
                test_maintenance.sign_task(self.current(), acting_user(self))
                services.complete_task(pk=self.current().pk, actor=acting_user(self))
            services.return_task(
                pk=self.current().pk, actor=acting_user(self), comment="请修正内容",
            )
            self.assertEqual(self.current().name, "工单修正")
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, "active")
            self.assertEqual(self.current().assigned_user_id, self.order.requester_id)
            with self.assertRaisesMessage(ValidationError, "退回补正后必须"):
                services.complete_task(pk=self.current().pk, actor=acting_user(self))
            self.upload("order")
            with self.assertRaisesMessage(ValidationError, "申请人完成手写签名"):
                services.complete_task(pk=self.current().pk, actor=acting_user(self))
            signature = test_maintenance.sign_task(self.current())
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
            self.assertEqual(self.current().name, "工单复核")
            printed = self.client.get(
                reverse("plugins:netbox_oa:maintenance_print", args=[signature.document_id])
            )
            self.assertContains(
                printed,
                reverse("plugins:netbox_oa:maintenance_signature_image", args=[signature.pk]),
            )
        self.assertTrue(
            MaintenanceSignature.objects.filter(pk=original.pk, submitted_at__isnull=False).exists()
        )
        self.assertEqual(self.order.tasks.filter(name="工单修正", status="completed").count(), 2)

    def test_api_requires_draft_signature_and_phone_does_not_start_workflow(self):
        api = APIClient()
        api.force_authenticate(self.actor)
        url = f"/api/maintenance/orders/{self.order.pk}/start/"
        self.assertEqual(api.post(url, {}).status_code, 400)
        signature = test_maintenance.sign_draft(self.order, self.actor)
        self.assertFalse(self.order.tasks.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "draft")
        self.assertEqual(api.post(url, {}).status_code, 200)
        self.assertEqual(self.current().name, "工单复核")
        signature.refresh_from_db()
        self.assertIsNotNone(signature.submitted_at)

    def test_only_requester_can_create_draft_signature_and_cancel_before_submission(self):
        other = self.worker("draft_signer_other")
        self.grant(other, "maintenanceorder", "change")
        self.grant(other, "maintenancedocument", "view")
        with self.assertRaises(PermissionDenied):
            signing.create_draft_session(pk=self.order.pk, actor=other)
        signature = test_maintenance.sign_draft(self.order, self.actor)
        signing.cancel_signature(pk=signature.pk, actor=self.actor)
        with self.assertRaises(ValidationError):
            services.start_order(pk=self.order.pk, actor=self.actor)

    def test_incomplete_configuration_rolls_back_submission_and_keeps_signature(self):
        signature = test_maintenance.sign_draft(self.order, self.actor)
        self.policy.roles.filter(role="reviewer").delete()
        with self.assertRaisesMessage(ValidationError, "配置全部经办角色"):
            services.start_order(pk=self.order.pk, actor=self.actor)
        signature.refresh_from_db()
        self.order.refresh_from_db()
        self.assertIsNone(signature.submitted_at)
        self.assertEqual(self.order.status, "draft")
        self.assertFalse(self.order.tasks.exists())

    def test_draft_mobile_capture_and_signature_visibility(self):
        import re

        from django.test import Client

        session = signing.create_draft_session(pk=self.order.pk, actor=self.actor)
        desktop_url = reverse("plugins:netbox_oa:maintenance_signature", args=[session.pk])
        self.assertEqual(self.client.get(desktop_url).status_code, 200)
        mobile_url = reverse(
            "plugins:netbox_oa:maintenance_signature_mobile", args=[signing.mobile_token(session)]
        )
        phone = Client(enforce_csrf_checks=True)
        page = phone.get(mobile_url)
        self.assertEqual(page.status_code, 200)
        token = re.search(r'name="submission_token" value="([^"]+)"', page.content.decode()).group(
            1
        )
        phone.cookies.clear()
        self.assertEqual(
            phone.post(
                mobile_url, {"submission_token": token, "image_data": image_data()}
            ).status_code,
            200,
        )
        self.assertFalse(self.order.tasks.exists())
        image_url = reverse("plugins:netbox_oa:maintenance_signature_image", args=[session.pk])
        viewer = self.worker("draft_image_viewer")
        self.grant(viewer, "maintenancedocument", "view")
        viewer_client = Client()
        viewer_client.force_login(viewer)
        self.assertEqual(viewer_client.get(image_url).status_code, 404)
        self.assertEqual(self.client.post(desktop_url).status_code, 302)
        services.start_order(pk=self.order.pk, actor=self.actor)
        self.assertEqual(viewer_client.get(image_url).status_code, 200)
        self.assertEqual(self.current().name, "工单复核")

    def test_correction_edit_page_saves_signs_and_resubmits_directly_to_review(self):
        original = self.start()
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="请补充服务位置",
        )
        workbench = self.client.get(self.order.get_absolute_url())
        self.assertContains(workbench, 'aria-label="编辑"')
        self.assertContains(
            workbench, reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk])
        )
        self.assertContains(workbench, ">提交审批</button>")
        url = reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk])
        page = self.client.get(url)
        self.assertContains(page, "保存修改")
        self.assertContains(page, "data-save-form")
        data = {
            **self.form_data(),
            "requirements": "修订后的内容",
            "expected_document_id": original.document_id,
        }
        response = self.client.post(
            url, {**data, "_signature": "1"}, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        document_id = response.json()["document_id"]
        session = MaintenanceSignature.objects.get(document_id=document_id)
        self.assertEqual(session.task_id, self.current().pk)
        signing.capture_signature(token=signing.mobile_token(session), data_url=image_data())
        response = self.client.post(
            url, {**data, "expected_document_id": document_id, "_submit": "1"}
        )
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(self.current().name, "工单复核")
        self.assertEqual(self.order.documents.filter(kind="form").count(), 2)
        original_print = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[original.document_id])
        )
        self.assertContains(
            original_print,
            reverse("plugins:netbox_oa:maintenance_signature_image", args=[original.pk]),
        )
        self.assertNotContains(
            original_print,
            reverse("plugins:netbox_oa:maintenance_signature_image", args=[session.pk]),
        )
