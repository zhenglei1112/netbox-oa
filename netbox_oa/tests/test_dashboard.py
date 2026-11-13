from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.urls import reverse

from netbox_oa.dashboard import MaintenanceTodoListWidget, MaintenanceTodoWidget
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


class MaintenanceTodoWidgetTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start

    def render_widget(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return MaintenanceTodoWidget().render(request)

    def test_pending_task_and_v2_link(self):
        self.start()
        html = self.render_widget(self.approver)
        self.assertIn('oa-todo-widget-count">1</span>', html)
        self.assertIn("mdi-clipboard-alert-outline", html)
        self.assertIn(reverse("plugins:netbox_oa:maintenance_workbench_v2") + "?tab=todo", html)

    def test_draft_and_inactive_orders_are_not_pending(self):
        self.assertIn("暂无待办工单", self.render_widget(self.actor))
        self.start()
        type(self.order).objects.filter(pk=self.order.pk).update(status="cancelled")
        self.assertIn("暂无待办工单", self.render_widget(self.actor))

    def test_assignment_and_permissions(self):
        from django.contrib.auth import get_user_model

        self.start()
        user = get_user_model().objects.create_user(username="dashboard_worker")
        self.grant(user, "maintenanceorder", "view")
        self.grant(user, "maintenancetask", "change")
        self.assertIn("暂无待办工单", self.render_widget(user))
        self.group.users.add(user)
        self.assertIn('oa-todo-widget-count">1</span>', self.render_widget(user))
        task = self.order.tasks.get(status="pending")
        task.assigned_user = self.actor
        task.save()
        self.assertIn("暂无待办工单", self.render_widget(user))
        task.assigned_user = user
        task.save()
        self.assertIn('oa-todo-widget-count">1</span>', self.render_widget(user))
        user.user_permissions.clear()
        user = get_user_model().objects.get(pk=user.pk)
        self.assertIn("暂无待办工单", self.render_widget(user))

    def test_anonymous_empty_state_is_linked(self):
        html = self.render_widget(AnonymousUser())
        self.assertIn("暂无待办工单", html)
        self.assertIn("mdi-clipboard-check-outline", html)
        self.assertIn("?tab=todo", html)

    def test_default_layout(self):
        self.assertEqual(MaintenanceTodoWidget.default_title, "工单待办")
        self.assertEqual((MaintenanceTodoWidget.width, MaintenanceTodoWidget.height), (2, 2))

    def test_drafts_belong_to_current_requester_even_for_admin(self):
        from django.contrib.auth import get_user_model

        self.assertIn("草稿 <strong>1</strong>", self.render_widget(self.actor))
        other = get_user_model().objects.create_user(username="other_requester")
        # The current account still owns the order, but is no longer its applicant.
        type(self.order).objects.filter(pk=self.order.pk).update(requester=other)
        self.assertIn("草稿 <strong>0</strong>", self.render_widget(self.actor))
        self.assertIn("草稿 <strong>0</strong>", self.render_widget(other))
        self.grant(other, "maintenanceorder", "view")
        other = get_user_model().objects.get(pk=other.pk)
        self.assertIn("草稿 <strong>1</strong>", self.render_widget(other))

    def test_submitted_order_and_anonymous_have_no_drafts(self):
        self.start()
        self.assertIn("草稿 <strong>0</strong>", self.render_widget(self.actor))
        self.assertIn("草稿 <strong>0</strong>", self.render_widget(AnonymousUser()))

    def test_personal_and_delegated_tasks_follow_list_badges(self):
        from django.contrib.auth import get_user_model

        from netbox_oa.maintenance_presentation import InboxPresentation
        from netbox_oa.maintenance_views import actionable_tasks

        self.start()
        task = self.order.tasks.get(status="pending")
        other = get_user_model().objects.create_user(username="assigned_elsewhere")
        for assignment, personal in ((None, False), (self.approver, True), (other, False)):
            with self.subTest(assignment=assignment):
                type(task).objects.filter(pk=task.pk).update(assigned_user=assignment)
                with patch("netbox_oa.dashboard.render_to_string", return_value="") as render:
                    self.render_widget(self.approver)
                context = render.call_args.args[1]
                self.assertEqual(context["pending_count"], int(personal))
                self.assertEqual(context["delegated_count"], int(not personal))
                self.assertTrue(context["has_pending"])
                self.assertEqual(context["draft_count"], 0)
                summary = InboxPresentation(
                    [self.order], self.approver, actionable_tasks(self.approver)
                ).summary(self.order)
                self.assertEqual(summary["status_label"], "待你审批" if personal else "可代办")
        type(task).objects.filter(pk=task.pk).update(assigned_user=None)
        self.group.users.add(self.approver)
        with patch("netbox_oa.dashboard.render_to_string", return_value="") as render:
            self.render_widget(self.approver)
        self.assertEqual(render.call_args.args[1]["pending_count"], 1)
        self.assertEqual(render.call_args.args[1]["delegated_count"], 0)


class MaintenanceTodoListWidgetTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start

    def render_widget(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return MaintenanceTodoListWidget().render(request)

    def set_node(self, key):
        from django.utils import timezone

        from netbox_oa.models import MaintenanceTask

        position = next(i for i, node in enumerate(self.order.steps_snapshot) if node["key"] == key)
        node = self.order.steps_snapshot[position]
        type(self.order).objects.filter(pk=self.order.pk).update(current_position=position)
        self.order.tasks.update(status="completed")
        return MaintenanceTask.objects.create(
            order=self.order, position=position, kind=node["kind"], name=node["name"],
            assigned_user=self.approver if node["kind"] in {"approval", "countersign"}
            else self.actor, deadline=timezone.now(),
        )

    def post_task(self, task):
        self.client.force_login(acting_user(self, task))
        return self.client.post(reverse(
            "plugins:netbox_oa:maintenancetask_quick_complete", args=[task.pk],
        ), {"comment": "客户端不应控制自动意见"})

    def test_list_shows_number_requester_and_disables_signature_step(self):
        self.start()
        html = self.render_widget(self.approver)
        self.assertIn(self.order.number, html)
        self.assertIn(self.actor.username, html)
        self.assertIn("disabled title=", html)
        self.assertNotIn(self.order.requirements, html)
        self.assertIn("暂无待办工单", self.render_widget(AnonymousUser()))

    def test_quick_complete_approval_and_seal_with_server_comments(self):
        from netbox_oa.models import MaintenanceEvent

        self.start()
        for key, comment in (
            ("order_senior_seal", "同意"),
        ):
            with self.subTest(key=key):
                task = self.set_node(key)
                html = self.render_widget(acting_user(self, task))
                self.assertNotIn("disabled title=", html)
                with patch(
                    "netbox_oa.maintenance_services._materials",
                    return_value={"order": self.order.documents.get(material="order").pk},
                ) as materials:
                    response = self.post_task(task)
                self.assertEqual(response.status_code, 200)
                materials.assert_called_once()
                task.refresh_from_db()
                self.assertEqual(task.status, "completed")
                self.assertEqual(task.comment, comment)
                self.assertEqual(task.decided_by, acting_user(self, task))
                self.assertEqual(
                    MaintenanceEvent.objects.get(task=task, action="complete").details["comment"],
                    comment,
                )
                self.assertEqual(self.post_task(task).status_code, 400)

    def test_input_upload_and_signature_steps_cannot_be_bypassed(self):
        self.start()
        for key in (
            "order_correction", "order_review", "order_final",
            "acceptance_apply", "payment_plan", "payment_eip",
        ):
            with self.subTest(key=key):
                task = self.set_node(key)
                self.assertEqual(self.post_task(task).status_code, 400)
                task.refresh_from_db()
                self.assertEqual(task.status, "pending")

    def test_missing_materials_still_block_quick_complete(self):
        self.start()
        for key in ("acceptance_company",):
            with self.subTest(key=key):
                task = self.set_node(key)
                html = self.render_widget(self.approver)
                self.assertIn("同意", html)
                self.assertNotIn("disabled title=", html)
                response = self.post_task(task)
                self.assertEqual(response.status_code, 400)
                task.refresh_from_db()
                self.assertEqual(task.status, "pending")

    def test_assignment_permissions_and_stale_order(self):
        from django.contrib.auth import get_user_model

        self.start()
        task = self.set_node("order_senior_seal")
        other = get_user_model().objects.create_user(username="todo_outsider")
        self.grant(other, "maintenanceorder", "view")
        self.grant(other, "maintenancetask", "change")
        self.assertNotIn(self.order.number, self.render_widget(other))
        self.client.force_login(other)
        url = reverse("plugins:netbox_oa:maintenancetask_quick_complete", args=[task.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        type(self.order).objects.filter(pk=self.order.pk).update(status="cancelled")
        self.assertEqual(self.post_task(task).status_code, 400)
        self.assertNotIn(self.order.number, self.render_widget(self.actor))

    def test_csrf_and_post_required(self):
        from django.test import Client

        self.start()
        task = self.set_node("order_senior_seal")
        url = reverse("plugins:netbox_oa:maintenancetask_quick_complete", args=[task.pk])
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        self.assertEqual(client.post(url).status_code, 403)
