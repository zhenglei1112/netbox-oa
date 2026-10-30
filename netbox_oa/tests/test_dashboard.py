from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from django.urls import reverse

from netbox_oa.dashboard import MaintenanceTodoWidget
from netbox_oa.tests import test_maintenance


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
        html = self.render_widget(self.actor)
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
        for assignment, personal in ((None, False), (self.actor, True), (other, False)):
            with self.subTest(assignment=assignment):
                type(task).objects.filter(pk=task.pk).update(assigned_user=assignment)
                with patch("netbox_oa.dashboard.render_to_string", return_value="") as render:
                    self.render_widget(self.actor)
                context = render.call_args.args[1]
                self.assertEqual(context["pending_count"], int(personal))
                self.assertEqual(context["delegated_count"], int(not personal))
                self.assertTrue(context["has_pending"])
                self.assertEqual(context["draft_count"], 0)
                summary = InboxPresentation(
                    [self.order], self.actor, actionable_tasks(self.actor)
                ).summary(self.order)
                self.assertEqual(summary["status_label"], "待你审批" if personal else "可代办")
        type(task).objects.filter(pk=task.pk).update(assigned_user=None)
        self.group.users.add(self.actor)
        with patch("netbox_oa.dashboard.render_to_string", return_value="") as render:
            self.render_widget(self.actor)
        self.assertEqual(render.call_args.args[1]["pending_count"], 1)
        self.assertEqual(render.call_args.args[1]["delegated_count"], 0)
