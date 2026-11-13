"""The sole workbench uses v2 permissions, forms and workflow actions."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_oa.maintenance_views import GUIDE_COLLAPSED_CONFIG
from netbox_oa.tests import test_draft_submission, test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


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
        self.client.force_login(acting_user(self))
        return self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench_v2"),
            {"tab": "all", "selected": self.order.pk, **params},
        )

    def test_guide_mode_is_saved_for_current_user_and_restored_in_html(self):
        # The standalone user lacks NetBox's config; mirror its public API.
        from unittest.mock import Mock

        configs = {}

        def config(user):
            if user.pk not in configs:
                values = {}
                configs[user.pk] = Mock(
                    get=lambda key, default=None: values.get(key, default),
                    set=Mock(side_effect=lambda key, value, commit=False:
                             values.update({key: value})),
                )
            return configs[user.pk]

        url = reverse("plugins:netbox_oa:maintenance_guide_preference")
        with patch.object(get_user_model(), "config", property(config), create=True):
            self.assertContains(
                self.page(), 'aria-expanded="true" aria-controls="oa-guide-content"'
            )
            current_user = acting_user(self)
            response = self.client.post(url, {"collapsed": "true", "user": self.approver.pk})
            self.assertEqual(response.json(), {"collapsed": True})
            configs[current_user.pk].set.assert_called_once_with(
                GUIDE_COLLAPSED_CONFIG, True, commit=True
            )
            self.assertContains(self.page(), '<div id="oa-guide-content" hidden>')
            self.assertContains(self.page(), 'aria-expanded="false"')
            self.client.force_login(self.approver)
            response = self.client.get(
                reverse("plugins:netbox_oa:maintenance_workbench_v2"),
                {"tab": "all", "selected": self.order.pk},
            )
            self.assertFalse(response.context["guide_collapsed"])
            self.page()
            self.assertEqual(self.client.post(url, {"collapsed": "false"}).status_code, 200)
            self.assertNotContains(self.page(), '<div id="oa-guide-content" hidden>')
            self.assertEqual(self.client.post(url, {"collapsed": "invalid"}).status_code, 400)
            self.assertEqual(self.client.get(url).status_code, 405)
            from django.test import Client

            csrf_client = Client(enforce_csrf_checks=True)
            csrf_client.force_login(current_user)
            self.assertEqual(csrf_client.post(url, {"collapsed": "true"}).status_code, 403)
            self.client.logout()
            self.assertEqual(self.client.post(url, {"collapsed": "true"}).status_code, 302)

    def test_all_entrypoints_use_v2_without_version_switches(self):
        self.page()
        for url in (reverse("plugins:netbox_oa:maintenance_workbench"),
                    reverse("plugins:netbox_oa:maintenance_workbench_v2"),
                    self.order.get_absolute_url()):
            response = self.client.get(url, {"tab": "all", "selected": self.order.pk})
            self.assertTemplateUsed(response, "netbox_oa/maintenance/workbench_v2.html")
            self.assertContains(response, "快捷办理")
            self.assertContains(response, "申请人签名")
            self.assertNotContains(response, "data-oa-version-switch")
            self.assertNotIn("version_switch_url", response.context)
            self.assertNotIn("ui", parse_qs(response.context["return_query"]))
            self.assertEqual(response.context["detail"]["order"].pk, self.order.pk)
        for path in ("/plugins/oa/maintenance/v3/",
                     f"/plugins/oa/maintenance/v3/orders/{self.order.pk}/"):
            self.assertEqual(self.client.get(path).status_code, 404)
        for version in ("v1", "v3"):
            response = self.page(ui=version)
            self.assertTemplateUsed(response, "netbox_oa/maintenance/workbench_v2.html")
            self.assertNotContains(response, "data-oa-version-switch")

    def test_workbench_links_supplier_and_contract(self):
        from tests.support.contracts.models import ServiceProvider

        with patch.object(
            ServiceProvider, "get_absolute_url", return_value="/providers/1/", create=True
        ):
            response = self.page()
            original = self.client.get(self.order.get_absolute_url())
        contract_url = reverse("plugins:netbox_contract:contract", args=[self.contract.pk])
        for page in (response, original):
            self.assertContains(page, f'<a href="/providers/1/">{self.order.supplier}</a>')
            self.assertContains(
                page, f'<a href="{contract_url}">{self.order.contract_number}</a>'
            )

    def test_missing_contract_keeps_labels_without_links(self):
        self.order.contract_id = None
        self.order.save()
        response = self.page()
        self.assertEqual(response.context["detail"]["contract_url"], "")
        self.assertEqual(response.context["detail"]["supplier_url"], "")
        self.assertContains(response, self.order.supplier)
        self.assertContains(response, self.order.contract_number)

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
            if key.endswith("_sealed_upload"):
                self.assertContains(response, "逐份关联审批原件")
                self.assertTrue(response.context["guide"]["pending_uploads"])
            if key == "payment_eip":
                self.assertContains(response, 'name="external_reference"')
            self.complete()
        self.assertEqual(len(seen), 10)
        response = self.page()
        self.assertNotContains(response, 'class="oa-actions"')
        self.assertContains(response, "工单资料库")
        self.assertTrue(all(s["state"] == "completed" for s in response.context["guide"]["stages"]))

    def test_action_success_and_errors_remain_in_v2(self):
        self.start()
        self.page()
        task = self.current()
        url = reverse("plugins:netbox_oa:maintenancetask_action", args=[task.pk, "return"])
        response = self.client.post(url, {"comment": "x" * 4001, "return_query": "ui=v2&tab=all"})
        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "netbox_oa/maintenance/workbench_v2.html")
        response = self.client.post(
            url, {"comment": "请补充服务范围", "return_query": "ui=v2&tab=all"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/maintenance/v2/", response.url)
        response = self.client.get(response.url)
        self.assertEqual(response.context["tab"], "all")
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["detail"]["order"].pk, self.order.pk)
        self.assertFalse(response.context["outside_list"])
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench_v2"), {"tab": "delegate"}
        )
        self.assertEqual(response.context["total"], 1)
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
        url = reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk])
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
