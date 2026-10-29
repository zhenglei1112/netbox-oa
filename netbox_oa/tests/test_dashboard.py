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
