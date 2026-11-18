import csv
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db.models import Q
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from netbox.object_actions import BulkExport

from netbox_oa.ledger import (
    WorkOrderLedgerFilterSet,
    WorkOrderLedgerTable,
    ledger_choices,
    ledger_orders,
    ledger_totals,
)
from netbox_oa.models import MaintenanceDocument, MaintenanceOrder, WorkOrderLedger
from netbox_oa.tests import test_maintenance


class WorkOrderLedgerTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload
    grant = test_maintenance.MaintenanceTests.grant
    worker = test_maintenance.MaintenanceTests.worker

    def url(self):
        return reverse("plugins:netbox_oa:workorderledger_list")

    def copy_order(self, **values):
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        order.pk = None
        order.number_reservation_id = None
        order.number = f"LEDGER-{MaintenanceOrder.objects.count()}"
        for key, value in values.items():
            setattr(order, key, value)
        order.save()
        return order

    def form_document(self, order, material, **values):
        values.setdefault("version", 1)
        return MaintenanceDocument.objects.update_or_create(
            order=order, material=material, version=values.pop("version"),
            defaults={"kind": "form", "uploaded_by": self.actor, **values},
        )[0]

    def filter(self, **params):
        request = RequestFactory().get(self.url(), params)
        request.user = self.actor
        filters = WorkOrderLedgerFilterSet(
            request.GET, ledger_orders(self.actor), request=request,
        )
        self.assertTrue(filters.is_valid(), filters.errors)
        return list(filters.qs.values_list("pk", flat=True))

    def test_native_list_fields_links_and_concrete_step(self):
        self.start()
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(
            service_type=["fiber", "construction"],
        )
        self.client.force_login(self.actor)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "generic/object_list.html")
        self.assertIsInstance(response.context["table"], WorkOrderLedgerTable)
        self.assertEqual(list(response.context["table"].columns.names()), [
            "pk", "sequence", "order_date", "number", "service_region", "settlement_amount",
            "service_type", "contract_number", "supplier", "owner", "current_step",
        ])
        for text in ("工单台账", "工单复核", "下单起始日期", "下单截止日期", "服务类型匹配"):
            self.assertContains(response, text)
        self.assertContains(response, self.order.get_absolute_url())
        self.assertContains(response, "光缆故障处置")
        self.assertContains(response, '<span class="badge text-bg-primary">光缆故障处置</span>',
                            html=True)
        self.assertContains(response, '<span class="badge text-bg-info">施工盯防</span>', html=True)
        self.assertContains(response, '<span class="badge text-bg-info">工单复核</span>', html=True)
        self.assertEqual(tuple(response.context["actions"]), (BulkExport,))

    def test_csv_export_includes_all_filtered_pages_with_continuous_sequence(self):
        included = [self.copy_order(service_region="广西") for _ in range(26)]
        self.client.force_login(self.actor)

        response = self.client.get(self.url(), {"export": "table", "province": "广西", "page": 2})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        content = b"".join(response.streaming_content) if response.streaming else response.content
        rows = list(csv.DictReader(content.decode("utf-8-sig").splitlines()))
        self.assertCountEqual([row["工单号"] for row in rows], [order.number for order in included])
        self.assertEqual([int(row["序号"]) for row in rows], list(range(1, 27)))

    def test_order_link_uses_the_viewers_current_workbench_tab(self):
        reviewer = self.worker("ledger_reviewer")
        self.grant(reviewer, "workorderledger", "view")
        self.grant(reviewer, "maintenancetask", "view")
        url = self.order.get_absolute_url()

        def open_order(user, tab):
            self.client.force_login(user)
            self.assertContains(self.client.get(self.url()), f'href="{url}"')
            response = self.client.get(url)
            self.assertEqual(response.context["tab"], tab)
            self.assertEqual(response.context["detail"]["order"].pk, self.order.pk)
            self.assertFalse(response.context["outside_list"])
            self.assertEqual(response.context["rows"][0]["order"].pk, self.order.pk)
            self.assertContains(
                response, f'class="oa-order selected" data-order-id="{self.order.pk}"'
            )

        open_order(self.actor, "todo")
        self.start()
        self.order.followers.add(self.actor, reviewer, self.approver)
        open_order(self.actor, "done")
        open_order(reviewer, "todo")
        open_order(self.approver, "delegate")
        self.complete()
        self.order.tasks.filter(status="completed").update(decided_by=reviewer)
        self.order.tasks.filter(status="pending").update(assigned_user=self.approver)
        open_order(reviewer, "done")
        open_order(self.approver, "todo")

        while self.order.steps_snapshot[self.current().position]["key"] != "payment_plan":
            self.complete()
        open_order(self.actor, "pending_payment")
        self.assertEqual(self.client.get(url, {"tab": "following"}).context["tab"], "following")

    def test_direct_link_keeps_older_order_visible_and_respects_tab_permissions(self):
        reader = get_user_model().objects.create_user(username="ledger_following_reader")
        reader.user_permissions.add(Permission.objects.get(codename="view_maintenanceorder"))
        order = self.copy_order(requester=reader)
        for _ in range(100):
            self.copy_order(requester=reader)
        self.client.force_login(self.actor)
        response = self.client.get(order.get_absolute_url())
        self.assertEqual(response.context["tab"], "all")
        self.assertEqual(response.context["total"], 102)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["order"].pk, order.pk)
        self.assertEqual(sum(row["order"].pk == order.pk for row in rows), 1)

        self.client.force_login(reader)
        response = self.client.get(self.order.get_absolute_url())
        self.assertTrue(response.context["outside_list"])
        self.assertNotIn("all", {tab["key"] for tab in response.context["tabs"]})
        self.order.followers.add(reader)
        response = self.client.get(self.order.get_absolute_url())
        self.assertEqual(response.context["tab"], "following")
        self.assertFalse(response.context["outside_list"])

    def test_seal_date_sort_and_inclusive_date_range(self):
        newer = self.copy_order()
        self.form_document(self.order, "order", electronic_seal={"date": "2026-11-16"})
        self.form_document(newer, "order", electronic_seal={"date": "2026-11-15"})
        unsealed = self.copy_order()
        self.assertEqual(self.filter(), [self.order.pk, newer.pk, unsealed.pk])
        self.assertEqual(self.filter(order_date_after="2026-11-15", order_date_before="2026-11-15"),
                         [newer.pk])
        self.assertEqual(self.filter(order_date_after="2026-11-16"), [self.order.pk])
        self.assertEqual(self.filter(order_date_before="2026-11-15"), [newer.pk])
        table = WorkOrderLedgerTable(ledger_orders(self.actor))
        self.assertEqual([row.pk for row in table.data], [self.order.pk, newer.pk, unsealed.pk])
        table.order_by = "order_date"
        self.assertEqual([row.pk for row in table.data], [newer.pk, self.order.pk, unsealed.pk])

    def test_actual_approved_form_provides_seal_date(self):
        self.reach("acceptance_apply")
        document = self.order.documents.get(material="order", kind="form")
        row = ledger_orders(self.actor).get(pk=self.order.pk)
        self.assertEqual(row.order_date, document.electronic_seal["date"])
        self.assertEqual(row.current_task_name, self.current().name)

    def test_service_type_or_and_and_other_filters_combine(self):
        both = self.copy_order(service_type=["fiber", "inspection"], supplier="另一个服务商")
        inspection = self.copy_order(service_type=["inspection"], service_region="广西")
        self.assertCountEqual(self.filter(service_type=["fiber", "inspection"]),
                              [self.order.pk, both.pk, inspection.pk])
        self.assertEqual(self.filter(service_type=["fiber", "inspection"], service_type_mode="and"),
                         [both.pk])
        self.assertEqual(self.filter(province="广西"), [inspection.pk])
        self.assertCountEqual(self.filter(province="广东"), [self.order.pk, both.pk])
        self.assertEqual(self.filter(
            province="广东", owner_id=self.actor.pk, supplier="另一个服务商",
            contract_number=self.order.contract_number, service_type=["inspection"],
        ), [both.pk])
        self.assertEqual(self.filter(q=inspection.number), [inspection.pk])

    def test_latest_removed_and_unsealed_versions_do_not_revive_history(self):
        self.form_document(self.order, "order", electronic_seal={"date": "2026-11-15"})
        self.form_document(self.order, "order", version=2)
        self.assertIsNone(ledger_orders(self.actor).get().order_date)
        document = self.order.documents.get(material="order", version=2)
        document.electronic_seal = {"date": "2026-11-16"}
        document.removed_at = timezone.now()
        document.save()
        self.assertIsNone(ledger_orders(self.actor).get().order_date)
        self.assertEqual(self.filter(order_date_after="2026-11-01"), [])
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "1234.50"})
        self.client.force_login(self.actor)
        self.assertContains(self.client.get(self.url()), "1,234.50")
        self.form_document(self.order, "acceptance", version=2, removed_at=timezone.now(),
                           form_data={"settlement_amount": "9876.00"})
        response = self.client.get(self.url())
        self.assertNotContains(response, "1,234.50")
        self.assertNotContains(response, "9,876.00")

    def test_access_and_object_permissions_cover_options_documents_and_tasks(self):
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        reader = get_user_model().objects.create_user(username="ledger_reader")
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        reader.user_permissions.add(Permission.objects.get(codename="view_maintenanceorder"))
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.get(self.url(), {"_ledger_totals": "1"}).status_code, 403)
        self.assertEqual(self.client.get(self.url(), {"export": "table"}).status_code, 403)
        reader.user_permissions.add(Permission.objects.get(codename="view_workorderledger"))
        self.start()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "工单复核")
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "5555.00"})
        self.assertNotContains(self.client.get(self.url()), "5,555.00")
        queryset_class = type(MaintenanceOrder.objects.all())
        original = queryset_class.restrict

        def restrict(queryset, user, action):
            visible = original(queryset, user, action)
            return visible.none() if queryset.model is WorkOrderLedger else visible

        with patch.object(queryset_class, "restrict", restrict):
            self.assertEqual(ledger_choices(self.actor)["supplier"], [])
            self.assertNotContains(self.client.get(self.url()), self.order.number)

    def test_personal_and_province_ledger_scope_is_independent_of_order_detail(self):
        reader = get_user_model().objects.create_user(username="province_ledger_reader")
        reader.user_permissions.add(Permission.objects.get(codename="view_workorderledger"))
        self.client.force_login(reader)
        personal = self.copy_order(requester=reader, service_region="广西")
        hidden = self.copy_order(service_region="江苏", supplier="江苏供应商")
        queryset_class = type(WorkOrderLedger.objects.all())
        original = queryset_class.restrict

        def restrict(queryset, user, action):
            visible = original(queryset, user, action)
            if queryset.model is WorkOrderLedger:
                return visible.filter(
                    Q(requester=user) | Q(service_region__regex=r"(^| / )广东( / |$)"),
                )
            if queryset.model is MaintenanceOrder:
                return visible.filter(pk=personal.pk)
            return visible

        with patch.object(queryset_class, "restrict", restrict):
            response = self.client.get(self.url())
            self.assertContains(response, self.order.number)
            self.assertContains(response, personal.number)
            self.assertNotContains(response, hidden.number)
            self.assertNotContains(response, "江苏供应商")
            export = self.client.get(self.url(), {"export": "table"})
            self.assertContains(export, personal.number)
            self.assertNotContains(export, hidden.number)
            self.assertNotContains(response, personal.get_absolute_url())
            self.assertNotContains(response, self.order.get_absolute_url())
            self.assertEqual(self.client.get(personal.get_absolute_url()).status_code, 404)
            self.assertContains(self.client.get(self.url(), {
                "_ledger_totals": "1", "_selected": [self.order.pk, personal.pk, hidden.pk],
            }), "查询条目 2")
            reader.user_permissions.add(Permission.objects.get(codename="view_maintenanceorder"))
            response = self.client.get(self.url())
            self.assertContains(response, personal.get_absolute_url())
            self.assertNotContains(response, self.order.get_absolute_url())

    def test_pagination_sequence_and_terminal_status(self):
        for _ in range(26):
            self.copy_order()
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(status="terminated")
        self.client.force_login(self.actor)
        response = self.client.get(self.url(), {"page": 2})
        self.assertEqual(response.context["table"].page.number, 2)
        rows = list(response.context["table"].page.object_list)
        self.assertEqual(len(rows), 2)
        # Rendered page must continue numbering after the first 25 rows.
        self.assertContains(response, ">26</td>", html=False)
        self.assertContains(response, ">27</td>", html=False)
        self.assertContains(response, "已终止")
        self.assertContains(response, '<span class="badge text-bg-danger">已终止</span>', html=True)

    def test_zero_settlement_is_displayed(self):
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "0"})
        self.client.force_login(self.actor)
        self.assertContains(self.client.get(self.url()), "0.00")

    def test_contract_and_supplier_links_follow_accessible_contract(self):
        from tests.support.contracts.models import ServiceProvider

        self.client.force_login(self.actor)
        contract_url = reverse("plugins:netbox_contract:contract", args=[self.contract.pk])
        with patch.object(ServiceProvider, "get_absolute_url", return_value="/providers/1/",
                          create=True):
            response = self.client.get(self.url())
            self.assertContains(
                response, f'<a href="{contract_url}">{self.order.contract_number}</a>', html=True,
            )
            self.assertContains(response, f'<a href="/providers/1/">{self.order.supplier}</a>',
                                html=True)
            reader = get_user_model().objects.create_user(username="ledger_contract_reader")
            reader.user_permissions.add(Permission.objects.get(codename="view_workorderledger"))
            self.client.force_login(reader)
            response = self.client.get(self.url())
            self.assertContains(response, self.order.contract_number)
            self.assertContains(response, self.order.supplier)
            self.assertNotContains(response, f'href="{contract_url}"')
            self.assertNotContains(response, 'href="/providers/1/"')
        self.client.force_login(self.actor)
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(contract_id=None)
        self.assertNotContains(self.client.get(self.url()), f'href="{contract_url}"')

    def test_totals_cover_all_filtered_pages_and_all_three_stages(self):
        acceptance = self.copy_order()
        payment = self.copy_order()
        MaintenanceOrder.objects.filter(pk=acceptance.pk).update(stage="acceptance")
        MaintenanceOrder.objects.filter(pk=payment.pk).update(stage="payment", status="completed")
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "1234.56"})
        self.form_document(acceptance, "acceptance", form_data={"settlement_amount": "0.44"})
        self.form_document(payment, "acceptance", form_data={"settlement_amount": "2000"})
        for _ in range(25):
            self.copy_order()
        self.client.force_login(self.actor)
        for page in (1, 2):
            response = self.client.get(self.url(), {"page": page})
            self.assertEqual(response.context["ledger_totals"], {
                "count": 28, "amount": Decimal("3235.00"),
                "ordering": 26, "acceptance": 1, "payment": 1, "completed": 1,
            })
            self.assertTrue(response.context["table"].columns["pk"].visible)
            self.assertContains(response, "总结算金额 3,235.00 元")
            self.assertContains(response, '<span class="btn btn-info pe-none">工单下单 26</span>',
                                html=True)
            self.assertContains(response, '<span class="btn btn-success pe-none">已完成 1</span>',
                                html=True)

    def test_totals_update_with_quick_search_and_ignore_selection(self):
        other = self.copy_order()
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "12.34"})
        self.form_document(other, "acceptance", form_data={"settlement_amount": "56.78"})
        self.client.force_login(self.actor)
        response = self.client.get(self.url(), {"q": self.order.number, "pk": other.pk},
                                   HTTP_HX_REQUEST="true")
        self.assertContains(response, 'hx-swap-oob="outerHTML"')
        self.assertEqual(response.context["ledger_totals"]["count"], 1)
        self.assertEqual(response.context["ledger_totals"]["amount"], Decimal("12.34"))
        self.assertContains(response, "总结算金额 12.34 元")

    def test_totals_do_not_include_deleted_or_inaccessible_material(self):
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "1000"})
        self.form_document(self.order, "acceptance", version=2, removed_at=timezone.now(),
                           form_data={"settlement_amount": "2000"})
        self.assertEqual(ledger_totals(ledger_orders(self.actor))["amount"], Decimal("0"))
        reader = get_user_model().objects.create_user(username="totals_reader")
        reader.user_permissions.add(Permission.objects.get(codename="view_workorderledger"))
        self.form_document(self.order, "acceptance", version=3,
                           form_data={"settlement_amount": "3000"})
        self.assertEqual(ledger_totals(ledger_orders(reader))["amount"], Decimal("0"))
        self.assertEqual(ledger_totals(ledger_orders(self.actor))["amount"], Decimal("3000"))

    def test_empty_totals_are_zero(self):
        self.assertEqual(ledger_totals(ledger_orders(self.actor).none()), {
            "count": 0, "amount": Decimal("0"), "ordering": 0, "acceptance": 0, "payment": 0,
            "completed": 0,
        })

    def test_selection_totals_and_empty_selection_fallback(self):
        other = self.copy_order()
        self.form_document(self.order, "acceptance", form_data={"settlement_amount": "12.34"})
        self.form_document(other, "acceptance", form_data={"settlement_amount": "56.78"})
        self.client.force_login(self.actor)
        params = {"_ledger_totals": "1", "_selected": [other.pk]}
        response = self.client.get(self.url(), params)
        self.assertContains(response, "查询条目 1")
        self.assertContains(response, "总结算金额 56.78 元")
        self.assertNotContains(response, '<table')
        params["_selected"] = [self.order.pk, other.pk]
        self.assertContains(self.client.get(self.url(), params), "总结算金额 69.12 元")
        params["_selected"] = []
        self.assertContains(self.client.get(self.url(), params), "查询条目 2")
        params.update({"_selected": [other.pk], "_ledger_all": "1", "page": 2})
        self.assertContains(self.client.get(self.url(), params), "总结算金额 69.12 元")

    def test_selection_cannot_bypass_filter_or_object_permissions(self):
        other = self.copy_order()
        self.client.force_login(self.actor)
        params = {"_ledger_totals": "1", "_selected": [other.pk], "q": self.order.number}
        self.assertContains(self.client.get(self.url(), params), "查询条目 0")
        queryset_class = type(MaintenanceOrder.objects.all())
        original = queryset_class.restrict

        def restrict(queryset, user, action):
            visible = original(queryset, user, action)
            return visible.exclude(pk=other.pk) if queryset.model is WorkOrderLedger else visible

        params.pop("q")
        with patch.object(queryset_class, "restrict", restrict):
            self.assertContains(self.client.get(self.url(), params), "查询条目 0")
        for invalid in ("invalid", "-1", "9223372036854775808"):
            params["_selected"] = [invalid]
            self.assertEqual(self.client.get(self.url(), params).status_code, 400)
        self.client.logout()
        self.assertEqual(self.client.get(self.url(), params).status_code, 302)
