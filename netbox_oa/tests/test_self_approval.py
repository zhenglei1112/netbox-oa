"""Requesters cannot approve their own orders through any handling entry point."""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa import maintenance_signatures as signatures
from netbox_oa.maintenance_forms import MaintenanceActionForm
from netbox_oa.maintenance_views import actionable_tasks
from netbox_oa.models import MaintenanceOrder, MaintenanceTask
from netbox_oa.tests import test_maintenance


class SelfApprovalTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    grant = test_maintenance.MaintenanceTests.grant
    worker = test_maintenance.MaintenanceTests.worker

    def set_node(self, key, kind=None):
        position = next(i for i, node in enumerate(self.order.steps_snapshot) if node["key"] == key)
        node = self.order.steps_snapshot[position]
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(current_position=position,
                                                                 stage=node["stage"])
        self.order.tasks.update(status="completed")
        return MaintenanceTask.objects.create(
            order=self.order, position=position, name=node["name"], kind=kind or node["kind"],
            assigned_group=self.group, deadline=timezone.now(),
        )

    def test_every_approval_and_countersign_blocks_requester_even_as_superuser(self):
        self.start()
        for key in ("order_review", "order_final", "order_senior_seal", "acceptance_review",
                    "acceptance_department", "acceptance_company", "payment_review"):
            task = self.set_node(key)
            before = self.order.events.count()
            for handler, kwargs in ((services.complete_task, {}),
                                    (services.return_task, {"comment": "本人退回"}),
                                    (services.quick_complete_task, {})):
                with self.subTest(key=key, handler=handler.__name__):
                    with self.assertRaisesMessage(services.SelfApprovalDenied, "不能审批自己发起"):
                        handler(pk=task.pk, actor=self.actor, **kwargs)
            task.refresh_from_db()
            self.assertEqual(task.status, "pending")
            self.assertIsNone(task.started_at)
            self.assertEqual(self.order.events.count(), before)
        task = self.set_node("order_review", kind="countersign")
        with self.assertRaises(services.SelfApprovalDenied):
            services.complete_task(pk=task.pk, actor=self.actor)

    def test_normal_requester_is_blocked_even_when_in_the_approval_group(self):
        self.start()
        requester = self.worker("normal_requester")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(requester=requester)
        with self.assertRaises(services.SelfApprovalDenied):
            services.complete_task(pk=self.current().pk, actor=requester)
        self.assertFalse(actionable_tasks(requester).filter(order=self.order).exists())

    def test_requester_warning_requires_current_assignment_and_change_permission(self):
        self.start()
        requester = self.worker("viewing_requester")
        self.grant(requester, "maintenancetask", "view")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(requester=requester)

        def page():
            self.client.force_login(requester)
            return self.client.get(self.order.get_absolute_url())

        requester.groups.clear()
        response = page()
        self.assertNotContains(response, "不能审批自己发起")
        self.assertFalse(response.context["detail"]["self_approval_blocked"])
        requester.groups.add(self.group)
        response = page()
        self.assertContains(response, "不能审批自己发起")
        self.assertTrue(response.context["detail"]["self_approval_blocked"])
        task = self.current()
        MaintenanceTask.objects.filter(pk=task.pk).update(assigned_user=self.approver)
        self.assertNotContains(page(), "不能审批自己发起")
        MaintenanceTask.objects.filter(pk=task.pk).update(assigned_user=requester)
        requester.groups.clear()
        self.assertContains(page(), "不能审批自己发起")
        requester.user_permissions.clear()
        self.grant(requester, "maintenanceorder", "view")
        self.grant(requester, "maintenancetask", "view")
        self.assertNotContains(page(), "不能审批自己发起")

    def test_web_api_quick_complete_and_signature_endpoints_reject_self_approval(self):
        self.start()
        task = self.current()
        self.client.force_login(self.actor)
        response = self.client.post(reverse("plugins:netbox_oa:maintenancetask_action",
                                             args=[task.pk, "complete"]), {"comment": "同意"})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "不能审批自己发起", status_code=403)
        api = APIClient()
        api.force_authenticate(self.actor)
        self.assertEqual(
            api.post(f"/api/maintenance/tasks/{task.pk}/complete/", {}).status_code, 403
        )
        response = self.client.post(reverse("plugins:netbox_oa:maintenancetask_quick_complete",
                                             args=[task.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertIn("不能审批自己发起", response.json()["error"])
        response = self.client.post(reverse("plugins:netbox_oa:maintenance_signature_create",
                                             args=[task.pk]), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(task.signatures.exists())
        page = self.client.get(self.order.get_absolute_url())
        self.assertNotContains(page, "不能审批自己发起")
        self.assertNotContains(page, 'class="oa-actions"')
        self.assertFalse(actionable_tasks(self.actor).filter(pk=task.pk).exists())
        MaintenanceTask.objects.filter(pk=task.pk).update(assigned_user=self.actor)
        self.assertContains(self.client.get(self.order.get_absolute_url()), "不能审批自己发起")

    def test_transfer_and_countersign_cannot_target_requester(self):
        self.start()
        task = self.current()
        for handler in (services.transfer_task, services.countersign_task):
            with self.subTest(handler=handler.__name__), self.assertRaisesMessage(
                ValidationError, "发起人"
            ):
                handler(pk=task.pk, actor=self.approver, user_id=self.actor.pk, comment="转交")
        form = MaintenanceActionForm(order=self.order, actor=self.approver)
        self.assertFalse(form.fields["user_id"].queryset.filter(pk=self.actor.pk).exists())
        services.transfer_task(
            pk=task.pk, actor=self.actor, user_id=self.approver.pk, comment="请审批"
        )
        task.refresh_from_db()
        self.assertEqual(task.assigned_user_id, self.approver.pk)
        test_maintenance.sign_task(task, self.approver)
        completed = services.complete_task(pk=task.pk, actor=self.approver)
        self.assertEqual(completed.decided_by_id, self.approver.pk)

    def test_other_approver_can_handle_order_while_requester_can_still_correct_it(self):
        self.start()
        task = self.current()
        services.return_task(pk=task.pk, actor=self.approver, comment="请补正")
        revised = self.upload("order")
        test_maintenance.sign_task(self.current())
        services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertTrue(revised.pk)
        self.assertEqual(self.current().name, "工单复核")

    def test_acceptance_signature_must_belong_to_another_approver(self):
        self.start()
        while self.order.steps_snapshot[self.current().position]["key"] != "acceptance_review":
            self.complete()
        with self.assertRaises(services.SelfApprovalDenied):
            signatures.create_session(pk=self.current().pk, actor=self.actor)
        session = signatures.create_session(pk=self.current().pk, actor=self.approver)
        self.assertEqual(session.document.material, "acceptance")
