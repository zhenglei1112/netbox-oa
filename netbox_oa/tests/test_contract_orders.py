from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import django_tables2 as tables
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db.models import Q
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from netbox_oa.contract_tables import contract_order_table
from netbox_oa.contract_views import contract_orders
from netbox_oa.models import MaintenanceDocument, MaintenanceOrder
from netbox_oa.template_content import ContractOrders
from netbox_oa.tests import test_maintenance


def invoice_table_class(model, fields):
    base = tables.table_factory(model, fields=fields)
    table_class = type("TestInvoiceTable", (base,), {
        "tags": tables.Column(default="—"),
        "Meta": type("Meta", (base.Meta,), {"fields": (*fields, "tags")}),
    })

    def configure(self, request):
        tables.RequestConfig(request, paginate={"per_page": 25}).configure(self)

    table_class.configure = configure
    return table_class


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
        self.reach("acceptance_apply")
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
        self.assertNotContains(response, "本工单启动时保存的流程快照")
        self.assertNotContains(response, "不代表已付款或已收款")
        self.assertEqual(
            [lane["name"] for lane in response.context["detail"]["approval_diagram"]["lanes"]],
            ["工单下单", "工单验收", "付款申请"],
        )
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
            copy.number_reservation_id = None
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
        self.assertIn('class="btn-group dropdown"', html)
        self.assertIn('data-bs-toggle="dropdown"', html)
        self.assertIn('class="dropdown-menu dropdown-menu-end"', html)
        self.assertIn('mdi mdi-history', html)
        self.assertIn('Changelog</a>', html)
        for row in table.paginated_rows:
            self.assertIn(reverse("plugins:netbox_oa:maintenanceorder_changelog",
                                  args=[row.record.pk]), html)
        self.assertNotIn('type="checkbox"', html)
        self.assertNotIn("btn-sm", html)
        self.assertNotIn('target="_blank"', html)

    def test_changelog_route_uses_native_view_for_order(self):
        from django.urls import resolve
        from netbox.views import generic

        url = reverse("plugins:netbox_oa:maintenanceorder_changelog", args=[self.order.pk])
        match = resolve(url)
        self.assertIs(match.func.view_class, generic.ObjectChangeLogView)
        self.assertEqual(match.func.view_initkwargs, {
            "base_template": "netbox_oa/maintenance/changelog.html",
        })
        self.assertEqual(match.kwargs, {"pk": self.order.pk, "model": MaintenanceOrder})
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(url).status_code, 204)
        user = get_user_model().objects.create_user(username="no_order_changelog")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_acceptance_amount_uses_latest_visible_active_form(self):
        from django.utils import timezone

        request = RequestFactory().get("/contract/")
        request.user = self.actor

        def cell():
            table = contract_order_table(request, contract_orders(self.actor, self.contract.pk))
            rows = list(table.rows)
            with self.assertNumQueries(0):
                return rows[0].get_cell("acceptance_amount")

        self.assertEqual(cell(), "—")
        for version, amount in ((1, "100.10"), (2, "1234.56")):
            latest = MaintenanceDocument.objects.create(
                order=self.order, material="acceptance", kind="form", version=version,
                form_data={"settlement_amount": amount}, uploaded_by=self.actor,
                original_name="验收单",
            )
        self.assertEqual(cell(), "1,234.56")
        latest.form_data = {"settlement_amount": "0"}
        latest.save()
        self.assertEqual(cell(), "0.00")
        latest.form_data = {}
        latest.save()
        self.assertEqual(cell(), "—")
        latest.removed_at = timezone.now()
        latest.save()
        self.assertEqual(cell(), "—")
        queryset = type(MaintenanceDocument.objects.all())
        original = queryset.restrict

        def restrict(query, user, action):
            visible = original(query, user, action)
            return visible.none() if query.model is MaintenanceDocument else visible

        with patch.object(queryset, "restrict", restrict):
            self.assertEqual(cell(), "—")

    def test_payment_progress_badges_and_multiple_matches(self):
        from tests.support.contracts.models import Invoice

        request = RequestFactory().get("/contract/")
        request.user = self.actor

        def cell():
            table = contract_order_table(request, contract_orders(self.actor, self.contract.pk))
            return table.rows[0].get_cell("payment_progress")

        self.assertIn('title="未纳入付款"', cell())
        for status, color in (("计划", "warning"), ("已提交", "info"), ("已支付", "success")):
            invoice = Invoice.objects.create(
                number="付款-" + self.order.number.lower() + "-第一期", amount=100, status=status,
            )
            invoice.contracts.add(self.contract)
            html = cell()
            self.assertIn(f'title="已纳入付款（{status}）"', html)
            self.assertIn(f'class="badge text-bg-{color}"', html)
            self.assertIn(f'>{status}</span>', html)
            self.assertNotIn('mdi mdi-check"', html)
            invoice.delete()
        # Multiple payments override their individual states, including equal states.
        invoice = Invoice.objects.create(number=self.order.number, amount=100, status="计划")
        invoice.contracts.add(self.contract)
        second = Invoice.objects.create(number=self.order.number + "-第二期", amount=100)
        second.contracts.add(self.contract)
        for status in ("计划", "已提交", "已支付"):
            second.status = status
            second.save()
            html = cell()
            self.assertIn('title="存在多笔付款"', html)
            self.assertIn('aria-label="存在多笔付款"', html)
            self.assertIn("mdi-help-circle-outline", html)
            self.assertNotIn('mdi mdi-check"', html)
        second.delete()
        self.assertIn('title="已纳入付款（计划）"', cell())

    def test_payment_progress_excludes_templates_other_contracts_and_unrelated_names(self):
        from tests.support.contracts.models import Invoice

        invoice = Invoice.objects.create(number=self.order.number, amount=100, status="已支付")
        template = Invoice.objects.create(
            number=self.order.number, amount=100, status="已支付", template=True,
        )
        template.contracts.add(self.contract)
        unrelated = Invoice.objects.create(number="不相关付款", amount=100, status="已支付")
        unrelated.contracts.add(self.contract)
        request = RequestFactory().get("/contract/")
        request.user = self.actor
        table = contract_order_table(request, contract_orders(self.actor, self.contract.pk))
        self.assertIn('title="未纳入付款"', table.rows[0].get_cell("payment_progress"))
        invoice.contracts.add(self.contract)
        table = contract_order_table(request, contract_orders(self.actor, self.contract.pk))
        self.assertIn('title="已纳入付款（已支付）"', table.rows[0].get_cell("payment_progress"))
        queryset = type(Invoice.objects.all())
        original = queryset.restrict

        def restrict(query, user, action):
            visible = original(query, user, action)
            return visible.exclude(pk=invoice.pk) if query.model is Invoice else visible

        with patch.object(queryset, "restrict", restrict):
            table = contract_order_table(request, contract_orders(self.actor, self.contract.pk))
            self.assertIn('title="未纳入付款"', table.rows[0].get_cell("payment_progress"))

    def test_payment_progress_without_invoice_permission_does_not_imply_unpaid(self):
        user = self.worker("order_without_invoice_access")
        request = RequestFactory().get("/contract/")
        request.user = user
        table = contract_order_table(request, contract_orders(user, self.contract.pk))
        html = table.rows[0].get_cell("payment_progress")
        self.assertIn('title="无付款查看权限"', html)
        self.assertNotIn("未纳入付款", html)

    def test_payment_progress_renders_without_per_row_queries(self):
        request = RequestFactory().get("/contract/")
        request.user = self.actor
        table = contract_order_table(request, contract_orders(self.actor, self.contract.pk))
        rows = list(table.rows)
        with self.assertNumQueries(0):
            for row in rows:
                self.assertIn('title="未纳入付款"', row.get_cell("payment_progress"))

    def test_empty_list_and_no_permission(self):
        request = RequestFactory().get("/contract/")
        request.user = self.actor
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(contract_id=None)
        extension = ContractOrders({"request": request, "object": self.contract})
        self.assertIn("暂无可查看的关联工单", extension.right_page())
        request.user = get_user_model().objects.create_user(username="no_oa")
        self.assertEqual(extension.right_page(), "")

    def test_payment_filter_matches_contained_number_and_contract(self):
        from tests.support.contracts.models import Invoice

        match = Invoice.objects.create(number=self.order.number, amount=100)
        match.contracts.add(self.contract)
        matches = [match]
        for number in (
            self.order.number + "-第一期",
            "付款-" + self.order.number,
            "付款-" + self.order.number.lower() + "-第二期",
        ):
            invoice = Invoice.objects.create(number=number, amount=200)
            invoice.contracts.add(self.contract)
            matches.append(invoice)
        other = Invoice.objects.create(number="不相关付款", amount=200)
        other.contracts.add(self.contract)
        Invoice.objects.create(number=self.order.number, amount=300)
        template = Invoice.objects.create(number=self.order.number, amount=400, template=True)
        template.contracts.add(self.contract)
        table_class = invoice_table_class(Invoice, fields=("number", "amount", "contracts"))
        self.client.force_login(self.actor)
        with patch("netbox_oa.contract_views.import_module", return_value=SimpleNamespace(
            InvoiceListTable=table_class
        )):
            response = self.client.get(self.url("payments"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["table"].data), matches)
        self.assertContains(response, "付款名称包含：")
        self.assertContains(response, "清除筛选")
        self.assertContains(response, self.order.number)
        self.assertNotContains(response, other.number)

    def test_payment_filter_requires_invoice_and_order_access(self):
        user = get_user_model().objects.create_user(username="payment_no_access")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url("payments")).status_code, 404)
        self.grant(user, "maintenanceorder", "view")
        user.user_permissions.add(Permission.objects.get(
            content_type__app_label="netbox_contract", codename="view_contract"
        ))
        self.assertEqual(self.client.get(self.url("payments")).status_code, 403)

    def test_filtered_payments_do_not_query_contract_for_each_row(self):
        from tests.support.contracts.models import Invoice

        for _ in range(5):
            invoice = Invoice.objects.create(number=self.order.number, amount=100)
            invoice.contracts.add(self.contract)
        table_class = invoice_table_class(Invoice, fields=("number", "contracts"))
        self.client.force_login(self.actor)
        with patch("netbox_oa.contract_views.import_module", return_value=SimpleNamespace(
            InvoiceListTable=table_class
        )):
            response = self.client.get(self.url("payments"))
        # Native payment action buttons read contracts.all for every payment.
        rows = list(response.context["table"].paginated_rows)
        with self.assertNumQueries(0):
            for row in rows:
                self.assertEqual(list(row.record.contracts.all()), [self.contract])

    def test_filter_uses_native_column_configuration_and_prefix(self):
        from tests.support.contracts.models import Invoice

        invoice = Invoice.objects.create(number=self.order.number, amount=100)
        invoice.contracts.add(self.contract)
        table_class = invoice_table_class(Invoice, fields=("number", "amount", "contracts"))
        configured = []

        def configure(table, request):
            configured.append(table.prefix)
            # Model the native table's saved/default column selection. The amount
            # column represents an optional column such as the custom SLA field.
            table.columns.hide("amount")
            table.columns.hide("tags")
            table.sequence = ("number", "contracts", "amount")
            tables.RequestConfig(request, paginate={"per_page": 25}).configure(table)

        table_class.configure = configure
        self.client.force_login(self.actor)
        with patch("netbox_oa.contract_views.import_module", return_value=SimpleNamespace(
            InvoiceListTable=table_class
        )):
            response = self.client.get(self.url("payments"))
        self.assertEqual(configured, ["invoices_"])
        table = response.context["table"]
        self.assertEqual([column.name for column in table.columns], ["number", "tags"])
        self.assertEqual(table.prefixed_order_by_field, "invoices_sort")


class PaymentFaultFilterTests(SimpleTestCase):
    def payment(self, start, end, tags=()):
        return SimpleNamespace(
            period_start=start, period_end=end,
            tags=SimpleNamespace(all=lambda: [SimpleNamespace(pk=pk) for pk in tags]),
        )

    def test_periods_and_tags_stay_grouped_per_payment(self):
        from netbox_oa.contract_views import payment_fault_filter

        first = self.payment(date(2026, 1, 1), date(2026, 1, 2), (1, 2))
        second = self.payment(date(2026, 2, 1), date(2026, 2, 2), (3,))
        expected = Q(pk__in=[]) | (
            Q(fault_occurrence_time__date__gte=first.period_start,
              fault_occurrence_time__date__lte=first.period_end) & Q(tags__pk__in=[1, 2])
        ) | (
            Q(fault_occurrence_time__date__gte=second.period_start,
              fault_occurrence_time__date__lte=second.period_end) & Q(tags__pk__in=[3])
        )
        self.assertEqual(payment_fault_filter([first, second]), expected)

    def test_no_payment_or_invalid_dates_match_nothing(self):
        from netbox_oa.contract_views import payment_fault_filter

        self.assertEqual(payment_fault_filter([]), Q(pk__in=[]))
        self.assertEqual(payment_fault_filter([
            self.payment(None, date(2026, 1, 1)),
            self.payment(date(2026, 2, 1), date(2026, 1, 1)),
        ]), Q(pk__in=[]))

    def test_untagged_payment_filters_only_its_period(self):
        from netbox_oa.contract_views import payment_fault_filter

        start, end = date(2026, 1, 1), date(2026, 1, 3)
        self.assertEqual(payment_fault_filter([self.payment(start, end)]), Q(pk__in=[]) | Q(
            fault_occurrence_time__date__gte=start, fault_occurrence_time__date__lte=end
        ))
