"""The alternate workbench must use the same permissions and workflow actions."""

from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_oa.tests import test_draft_submission, test_maintenance


class WorkbenchV2Tests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload
    worker = test_maintenance.MaintenanceTests.worker

    def page(self, **params):
        self.client.force_login(self.actor)
        return self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench_v2"),
            {"tab": "all", "selected": self.order.pk, **params},
        )

    def test_original_and_v2_are_separate_and_preserve_selection(self):
        response = self.page(q=self.order.number)
        self.assertTemplateUsed(response, "netbox_oa/maintenance/workbench_v2.html")
        self.assertContains(response, "快捷办理")
        self.assertContains(response, "申请人签名")
        self.assertEqual(parse_qs(response.context["return_query"])["ui"], ["v2"])
        for tab in response.context["tabs"]:
            self.assertIn("/maintenance/v2/", tab["url"])
        old = self.client.get(response.context["version_switch_url"])
        self.assertTemplateUsed(old, "netbox_oa/maintenance/workbench.html")
        self.assertNotContains(old, 'class="oa-v2-workspace"')
        self.assertEqual(old.context["detail"]["order"].pk, self.order.pk)

    def test_each_live_node_has_guidance_and_completed_order_has_no_action(self):
        self.start()
        seen = set()
        while self.order.tasks.filter(status="pending").exists():
            task = self.current()
            key = self.order.steps_snapshot[task.position]["key"]
            response = self.page()
            seen.add(key)
            self.assertContains(response, task.name)
            self.assertTrue(response.context["guide"]["steps"])
            self.assertContains(response, 'class="oa-actions"')
            self.assertEqual(
                [item["key"] for item in response.context["guide"]["stages"]
                 if item["state"] == "current"],
                [self.order.steps_snapshot[task.position]["stage"]],
            )
            if key.endswith("_seal") and task.kind == "action":
                self.assertContains(response, "逐份关联审批原件")
                self.assertTrue(response.context["guide"]["pending_uploads"])
            if key == "payment_eip":
                self.assertContains(response, 'name="external_reference"')
            self.complete()
        self.assertEqual(len(seen), 17)
        response = self.page()
        self.assertNotContains(response, 'class="oa-actions"')
        self.assertContains(response, "工单资料库")
        self.assertTrue(all(s["state"] == "completed" for s in response.context["guide"]["stages"]))

    def test_action_success_and_errors_remain_in_v2(self):
        self.start()
        self.page()
        task = self.current()
        url = reverse("plugins:netbox_oa:maintenancetask_action", args=[task.pk, "return"])
        response = self.client.post(url, {"comment": "", "return_query": "ui=v2&tab=all"})
        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "netbox_oa/maintenance/workbench_v2.html")
        response = self.client.post(
            url, {"comment": "请补充服务范围", "return_query": "ui=v2&tab=all"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/maintenance/v2/", response.url)
        response = self.client.get(response.url)
        self.assertContains(response, "按退回意见修正工单")
        self.assertContains(response, "请补充服务范围")

    def test_follow_preserves_filters_and_selected_order(self):
        self.page()
        response = self.client.post(
            reverse("plugins:netbox_oa:maintenanceorder_action", args=[self.order.pk, "follow"]),
            {"return_query": "ui=v2&tab=all&q=HT"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/maintenance/v2/", response.url)
        params = parse_qs(urlparse(response.url).query)
        self.assertEqual(params["q"], ["HT"])
        self.assertEqual(params["selected"], [str(self.order.pk)])

    def test_draft_edit_sign_and_submit_keep_v2(self):
        self.page()
        url = reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk]) + "?ui=v2"
        response = self.client.get(url)
        self.assertIn("/maintenance/v2/", response.context["return_url"])
        self.assertEqual(response.context["signature_save_url"], url)
        data = test_draft_submission.DraftSubmissionTests.form_data(self)
        response = self.client.post(url, {**data, "_save": "1"})
        self.assertEqual(response.url, url)
        response = self.client.post(url, {**data, "_signature": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["form_action"], url)
        test_maintenance.sign_draft(self.order, self.actor)
        response = self.client.post(url, {**data, "_submit": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/maintenance/v2/", response.url)

    def test_no_action_for_viewer_and_no_access_without_permission(self):
        self.start()
        viewer = get_user_model().objects.create_user(username="v2_viewer")
        self.grant(viewer, "maintenanceorder", "view")
        self.grant(viewer, "maintenancetask", "view")
        self.client.force_login(viewer)
        url = reverse("plugins:netbox_oa:maintenance_workbench_v2")
        response = self.client.get(url, {"selected": self.order.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "当前由 <strong>")
        self.assertNotContains(response, 'class="oa-actions"')
        self.assertNotContains(response, "data-oa-upload")
        outsider = get_user_model().objects.create_user(username="v2_outsider")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url, {"selected": self.order.pk}).status_code, 404)

    def test_empty_search_and_anonymous_request(self):
        response = self.page(q="no-such-order", selected="")
        self.assertContains(response, "当前范围暂无工单")
        self.assertEqual(response.context["guide"], {})
        self.client.logout()
        self.assertEqual(
            self.client.get(reverse("plugins:netbox_oa:maintenance_workbench_v2")).status_code,
            302,
        )
