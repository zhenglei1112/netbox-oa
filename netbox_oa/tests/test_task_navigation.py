"""Successful handling retains list filters; failed handling preserves inputs."""

from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.urls import reverse

from netbox_oa import maintenance_services as services
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


class TaskNavigationTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    reach = test_maintenance.MaintenanceTests.reach
    worker = test_maintenance.MaintenanceTests.worker
    grant = test_maintenance.MaintenanceTests.grant

    def handle(self, operation, expected, *, version="", **data):
        self.start()
        test_maintenance.sign_task(self.current(), acting_user(self))
        services.create_order(
            actor=self.actor, policy=self.policy, service_type="fiber",
            contract_id=self.contract.pk, service_location="下一条待办", requirements="待办理",
        )
        self.client.force_login(acting_user(self))
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenancetask_action", args=[self.current().pk, operation],
        ), {
            "comment": "办理意见", "return_panel": "routing",
            "return_query": f"ui={version}&tab=done&q=missing&stage=report&node=不存在"
                            "&sort=waiting&page=9&scope=done&history=100&section=history",
            **data,
        })
        route = "maintenance_workbench_v2"
        self.assertEqual(urlsplit(response.url).path, reverse(f"plugins:netbox_oa:{route}"))
        params = parse_qs(urlsplit(response.url).query)
        self.assertEqual(params["tab"], ["done"])
        self.assertEqual(params["q"], ["missing"])
        self.assertEqual(params["restore"], ["1"])
        page = self.client.get(response.url)
        self.assertIsNone(page.context["detail"])
        self.assertContains(page, expected)
        self.assertContains(page, "data-oa-task-completed")
        self.assertNotContains(page, 'class="oa-action-error"')

    def test_approval_retains_filters_and_displays_result(self):
        self.handle("complete", "已同意。")

    def test_return_retains_v2_filters(self):
        self.handle("return", "已退回补正。", version="v2")

    def test_transfer_closes_routing_panel_and_retains_category(self):
        other = self.worker("next_handler")
        self.handle("transfer", "已转办。", user_id=other.pk)

    def test_countersign_retains_filters_with_result(self):
        other = self.worker("extra_handler")
        self.handle("countersign", "已加签。", version="v2", user_id=other.pk)

    def test_last_task_returns_to_empty_todo(self):
        self.reach("payment_eip")
        for material, state in self.order.steps_snapshot[self.current().position]["required"]:
            self.upload(material, state)
        self.client.force_login(acting_user(self))
        response = self.client.post(reverse(
            "plugins:netbox_oa:maintenancetask_action", args=[self.current().pk, "complete"],
        ), {"comment": "付款申请完成", "external_reference": "EIP-NAVIGATION"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["detail"])
        self.assertContains(response, "本阶段办理完成。")
