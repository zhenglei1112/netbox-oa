from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase
from django.urls import reverse

from netbox_oa.contract_tables import contract_order_table
from netbox_oa.contract_views import contract_orders
from netbox_oa.models import MaintenanceDocument, MaintenanceOrder
from netbox_oa.template_content import ContractOrders
from netbox_oa.tests import test_maintenance


class ContractOrderTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload
    worker = test_maintenance.MaintenanceTests.worker
    grant = test_maintenance.MaintenanceTests.grant

    def url(self, kind="materials", contract_id=None):
        return reverse(f"plugins:netbox_oa:contract_order_{kind}", kwargs={
            "contract_id": contract_id or self.contract.pk, "pk": self.order.pk,
        })

    def test_superuser_material_page_is_always_readonly(self):
        self.reach("report_fill")
        document = self.upload("report")
        self.client.force_login(self.actor)
        normal = self.client.get(reverse("plugins:netbox_oa:maintenance_review",
                                         args=[self.order.pk]), {"material": "report"})
        self.assertContains(normal, f"?replace={document.pk}")
        for suffix in ("", "?material=order&embed=0", "?material=report&read_only=0"):
            response = self.client.get(self.url() + suffix)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["read_only"])
            for group in response.context["groups"]:
                self.assertFalse(group["can_upload"])
                self.assertTrue(all("maintenance_document" not in row
                                    for row in group["index_rows"]))
            for text in ("返回审批", "上传覆盖", "删除材料", "data-oa-upload",
                         "maintenance-review-maintenance.js"):
                self.assertNotContains(response, text)
            self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_flow_reuses_history_and_has_no_operations(self):
        self.start()
        self.client.force_login(self.actor)
        response = self.client.get(self.url("flow"))
        self.assertContains(response, "完整审批经过")
        self.assertTemplateUsed(response, "netbox_oa/maintenance/inbox_approval_content.html")
        self.assertTemplateUsed(response, "netbox_oa/maintenance/inbox_activity_timeline.html")
        self.assertContains(response, "详细办理记录与意见")
        self.assertContains(response, 'aria-label="审批经过图例"')
        self.assertNotContains(response, '<table')
        self.assertNotContains(response, 'aria-label="关闭审批经过"')
        self.assertTrue(response.context["history"]["approval_events"])
        self.assertNotContains(response, '<form')
        self.assertNotContains(response, 'data-oa-upload')

    def test_access_and_contract_association_for_both_pages(self):
        for kind in ("flow", "materials"):
            self.assertEqual(self.client.get(self.url(kind)).status_code, 302)
        user = get_user_model().objects.create_user(username="contract_reader")
        self.client.force_login(user)
        for kind in ("flow", "materials"):
            self.assertEqual(self.client.get(self.url(kind)).status_code, 404)
        user.user_permissions.add(Permission.objects.get(codename="view_contract"))
        for kind in ("flow", "materials"):
            self.assertEqual(self.client.get(self.url(kind)).status_code, 404)
        user.user_permissions.add(Permission.objects.get(codename="view_maintenanceorder"))
        for kind in ("flow", "materials"):
            response = self.client.get(self.url(kind))
            self.assertEqual(response.status_code, 200)
            if kind == "materials":
                self.assertFalse(response.context["review_items"])
            else:
                self.assertFalse(response.context["history"]["approval_events"])
        self.client.force_login(self.actor)
        for kind in ("flow", "materials"):
            response = self.client.get(self.url(kind, self.contract.pk + 99))
            self.assertEqual(response.status_code, 404)
            self.assertEqual(self.client.post(self.url(kind)).status_code, 405)
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(contract_id=self.contract.pk + 1)
        for kind in ("flow", "materials"):
            self.assertEqual(self.client.get(self.url(kind)).status_code, 404)

    def test_document_object_filter_is_preserved(self):
        self.start()
        self.client.force_login(self.actor)
        document = self.order.documents.first()
        queryset = MaintenanceDocument.objects.all()
        original_restrict = type(queryset).restrict

        def restrict(query, user, action):
            visible = original_restrict(query, user, action)
            if query.model is MaintenanceDocument:
                return visible.exclude(pk=document.pk)
            return visible

        with patch.object(type(queryset), "restrict", restrict):
            response = self.client.get(self.url(), {
                "material": document.material, "document": document.pk,
            })
        self.assertEqual(response.status_code, 404)

    def test_contract_permission_is_required_even_with_oa_access(self):
        user = get_user_model().objects.create_user(username="oa_reader")
        user.user_permissions.add(Permission.objects.get(codename="view_maintenanceorder"))
        self.client.force_login(user)
        for kind in ("flow", "materials"):
            self.assertEqual(self.client.get(self.url(kind)).status_code, 404)

    def test_missing_contract_plugin_returns_not_found(self):
        from django.core.exceptions import ValidationError

        self.client.force_login(self.actor)
        with patch(
            "netbox_oa.contract_views.purchase_contracts", side_effect=ValidationError("缺失")
        ):
            for kind in ("flow", "materials"):
                self.assertEqual(self.client.get(self.url(kind)).status_code, 404)

    def test_list_association_all_states_pagination_sort_and_readonly_actions(self):
        for index in range(28):
            copy = MaintenanceOrder.objects.get(pk=self.order.pk)
            copy.pk = None
            copy.number = f"CONTRACT-TEST-{index:02}"
            copy.save()
            MaintenanceOrder.objects.filter(pk=copy.pk).update(
                status=("draft", "active", "completed", "cancelled", "terminated")[index % 5]
            )
        MaintenanceOrder.objects.filter(pk=copy.pk).update(contract_id=self.contract.pk + 1)
        request = RequestFactory().get("/contract/", {
            "invoices_page": 3, "oa_orders-page": 2, "oa_orders-sort": "number",
        })
        request.user = self.actor
        queryset = contract_orders(self.actor, self.contract.pk)
        self.assertEqual(queryset.count(), 28)
        self.assertEqual(set(queryset.values_list("status", flat=True)),
                         {"draft", "active", "completed", "cancelled", "terminated"})
        table = contract_order_table(request, queryset)
        self.assertEqual(table.page.number, 2)
        self.assertEqual(len(table.page.object_list), 3)
        self.assertEqual(list(table.columns.names()), list(table.Meta.fields))
        self.assertEqual(str(table.order_by), "number")
        html = ContractOrders({"request": request, "object": self.contract}).right_page()
        self.assertIn("invoices_page=3", html)
        self.assertIn("oa_orders-page=1", html)
        self.assertIn("查看办理流程", html)
        self.assertIn("查看材料", html)
        self.assertIn('aria-label="服务工单"', html)
        self.assertEqual(table.columns["actions"].header, "")
        self.assertRegex(
            table.rows[0].get_cell("created"), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
        )
        self.assertIn('class="btn btn-outline-primary"', html)
        self.assertIn('class="btn btn-outline-info"', html)
        self.assertNotIn('type="checkbox"', html)
        self.assertNotIn("btn-sm", html)
        self.assertNotIn('target="_blank"', html)

    def test_empty_list_and_no_permission(self):
        request = RequestFactory().get("/contract/")
        request.user = self.actor
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(contract_id=None)
        extension = ContractOrders({"request": request, "object": self.contract})
        self.assertIn("暂无可查看的关联工单", extension.right_page())
        request.user = get_user_model().objects.create_user(username="no_oa")
        self.assertEqual(extension.right_page(), "")
