"""Inbox presentation and HTTP regressions using the standalone permission harness."""

from html.parser import HTMLParser
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django_fsm_log.models import StateLog
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_presentation import InboxPresentation
from netbox_oa.maintenance_views import actionable_tasks
from netbox_oa.models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceOrder,
    MaintenanceTask,
)
from netbox_oa.tests import test_maintenance


class PanelMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class InboxTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach
    upload = test_maintenance.MaintenanceTests.upload
    worker = test_maintenance.MaintenanceTests.worker

    def test_legacy_flow_names_are_normalized_in_ui_and_data_migration(self):
        self.start()
        event = self.order.events.get(action="start")
        timestamp, actor_id = event.created, event.actor_id
        old = "发起四阶段维护流程"
        self.order.events.filter(pk=event.pk).update(description=old)
        StateLog.objects.for_(self.order).update(description="发起维护流程")
        page = self.page()
        self.assertNotContains(page, old)
        self.assertContains(page, "发起代维单位维护服务工单流程")
        note = MaintenanceEvent.objects.create(
            order=self.order,
            actor=self.actor,
            action="edit",
            description=old,
            details={"comment": "原始备注"},
        )
        migration = import_module("netbox_oa.migrations.0008_normalize_maintenance_flow_name")
        migration.normalize_flow_name(apps, SimpleNamespace(connection=connection))
        event.refresh_from_db()
        self.assertEqual(event.description, "发起代维单位维护服务工单流程")
        self.assertEqual(event.created, timestamp)
        self.assertEqual(event.actor_id, actor_id)
        self.assertEqual(
            StateLog.objects.for_(self.order).get().description,
            "发起代维单位维护服务工单流程",
        )
        note.refresh_from_db()
        self.assertEqual(note.description, old)
        self.assertEqual(note.details["comment"], "原始备注")

    def page(self, actor=None, **params):
        self.client.force_login(actor or self.actor)
        return self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench"), {"tab": "all", **params}
        )

    def action(self, operation, data=None, task=None, actor=None):
        self.client.force_login(actor or self.actor)
        return self.client.post(
            reverse(
                "plugins:netbox_oa:maintenancetask_action",
                args=[(task or self.current()).pk, operation],
            ),
            data or {},
        )

    def test_group_personal_and_admin_status_are_distinct(self):
        test_maintenance.MaintenanceTests.reach_legacy_shared_report(self)
        worker = self.worker("handler")
        self.grant(worker, "maintenancetask", "view")
        self.grant(worker, "maintenanceevent", "view")
        response = self.page(worker)
        self.assertEqual(response.context["rows"][0]["status_label"], "待你处理")
        self.assertContains(response, "Maintenance handlers")
        admin = self.page().context["rows"][0]
        self.assertEqual(admin["status_label"], "可代办")
        self.assertFalse(admin["assigned_to_me"])
        self.grant(worker, "maintenancedocument", "add")
        services.upload_document(
            pk=self.order.pk,
            actor=worker,
            material="report",
            state="paper",
            file=SimpleUploadedFile("report.pdf", b"report"),
        )
        other = self.worker("other")
        self.grant(other, "maintenancetask", "view")
        row = self.page(other).context["rows"][0]
        self.assertEqual(row["status_label"], "等待他人")
        self.assertFalse(row["can_act"])
        self.assertEqual(row["waiting_for"], "handler")

    def test_approval_node_contains_controls_and_required_material(self):
        self.reach("order_review")
        self.actor.groups.add(self.group)
        response = self.page()
        self.assertEqual(response.context["detail"]["status_label"], "待你审批")
        self.assertContains(response, ">同意</button>")
        self.assertContains(response, "退回补正")
        materials = {item["key"]: item for item in response.context["detail"]["materials"]}
        self.assertTrue(materials["order"]["required_now"])
        self.assertFalse(materials["report"]["required_now"])
        self.assertContains(response, "查看 / 打印表单")

    def test_completed_node_becomes_history_and_detail_stays_outside_todo(self):
        test_maintenance.MaintenanceTests.reach_legacy_shared_report(self)
        worker = self.worker("first_handler")
        self.grant(worker, "maintenancetask", "view")
        self.grant(worker, "maintenanceevent", "view")
        self.grant(worker, "maintenancedocument", "add")
        services.upload_document(
            pk=self.order.pk,
            actor=worker,
            material="report",
            state="paper",
            file=SimpleUploadedFile("report.pdf", b"report"),
        )
        response = self.action(
            "complete",
            {
                "comment": "编号确认无误",
                "return_query": "tab=todo&sort=waiting&q=惠州&stage=ordering&node=确认",
            },
            actor=worker,
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.url).query)
        self.assertEqual(query["selected"], [str(self.order.pk)])
        self.assertEqual(query["sort"], ["waiting"])
        page = self.client.get(response.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["total"], 0)
        self.assertTrue(page.context["outside_list"])
        self.assertContains(page, "编号确认无误")
        self.assertContains(page, "发起工单")

    def test_reading_and_failed_operations_do_not_assign_shared_task(self):
        test_maintenance.MaintenanceTests.reach_legacy_shared_report(self)
        first, second = self.worker("shared_first"), self.worker("shared_second")
        self.grant(first, "maintenancedocument", "add")
        for actor in (first, second):
            self.grant(actor, "maintenancetask", "view")
            page = self.page(actor, tab="todo")
            self.assertEqual(page.context["total"], 1)
            self.assertTrue(page.context["rows"][0]["can_act"])
        self.assertIsNone(self.current().assigned_user_id)
        self.assertIsNone(self.current().started_at)
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=first)
        with self.assertRaises(ValidationError):
            services.upload_document(
                pk=self.order.pk,
                actor=first,
                material="report",
                file=SimpleUploadedFile("report.exe", b"invalid"),
            )
        self.assertIsNone(self.current().assigned_user_id)
        self.assertIsNone(self.current().started_at)
        services.upload_document(
            pk=self.order.pk,
            actor=first,
            material="report",
            state="paper",
            file=SimpleUploadedFile("report.pdf", b"report"),
        )
        self.assertEqual(self.current().assigned_user_id, first.pk)
        self.assertIsNotNone(self.current().started_at)
        self.assertEqual(self.page(second, tab="todo").context["total"], 0)

    def test_explicit_claim_ui_and_endpoint_are_removed(self):
        self.reach("order_review")
        page = self.page()
        self.assertNotContains(page, "领取任务")
        self.assertNotContains(page, "待领取")
        response = self.action("claim")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.current().assigned_user_id)
        client = APIClient()
        client.force_authenticate(user=self.actor)
        response = client.post(f"/api/maintenance/tasks/{self.current().pk}/claim/", {})
        self.assertEqual(response.status_code, 404)

    def test_invalid_action_keeps_bound_fields_and_filters(self):
        self.reach("order_review")
        response = self.action("return", {"comment": " ", "return_query": "tab=mine&sort=waiting"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["tab"], "mine")
        self.assertTrue(response.context["action_form"].is_bound)
        self.assertEqual(response.context["action_form"]["comment"].value(), " ")
        self.assertTrue(response.context["action_error"])
        response = self.action("complete", {"comment": "保留我的意见", "payment_amount": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "保留我的意见", status_code=400)
        self.assertTrue(response.context["action_form"].errors)
        self.assertEqual(self.current().status, "pending")

    def test_stale_task_refreshes_current_state_and_preserves_comment(self):
        self.start()
        old_task = self.current()
        self.complete()
        response = self.action("complete", {"comment": "重复页面填写"}, task=old_task)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["detail"]["current"].pk, self.current().pk)
        self.assertContains(response, "任务已处理", status_code=400)
        self.assertContains(response, "重复页面填写", status_code=400)

    def test_event_history_paging_is_chronological_and_complete(self):
        for index in range(115):
            MaintenanceEvent.objects.create(
                order=self.order,
                actor=self.actor,
                action="edit",
                description=f"历史记录 {index:03d}",
            )
        response = self.page()
        events = response.context["detail"]["events"]
        self.assertEqual(len(events), 50)
        self.assertEqual(events[0]["event"].description, "历史记录 065")
        self.assertEqual(events[-1]["event"].description, "历史记录 114")
        self.assertTrue(response.context["detail"]["has_earlier_events"])
        for expected in (100, 116):
            response = self.client.get(response.context["history_url"])
            self.assertEqual(len(response.context["detail"]["events"]), expected)
        self.assertFalse(response.context["detail"]["has_earlier_events"])

    def test_return_and_reapproval_do_not_replace_earlier_events(self):
        self.reach("order_review")
        services.return_task(pk=self.current().pk, actor=self.actor, comment="请补充说明")
        self.upload("order")
        self.complete()
        services.complete_task(pk=self.current().pk, actor=self.actor, comment="补正后同意")
        response = self.page()
        events = response.context["detail"]["events"]
        comments = [item["event"].details.get("comment") for item in events]
        self.assertLess(comments.index("请补充说明"), comments.index("补正后同意"))
        self.assertEqual(events[-1]["action_label"], "同意")
        self.assertContains(response, "工单部门审核")

    def test_transfer_and_countersign_show_real_assignees(self):
        self.reach("order_review")
        target = self.worker("extra_reviewer")
        services.transfer_task(
            pk=self.current().pk, actor=self.actor, user_id=target.pk, comment="请复核"
        )
        services.countersign_task(
            pk=self.current().pk, actor=self.actor, user_id=target.pk, comment="加签意见"
        )
        response = self.page()
        self.assertEqual(response.context["detail"]["waiting_for"], "extra_reviewer")
        future = response.context["detail"]["future_nodes"]
        self.assertEqual(future[0]["kind"], "countersign")
        self.assertEqual(future[0]["label"], "extra_reviewer")
        self.assertContains(response, "请复核")
        self.assertContains(response, "加签意见")

    def test_object_restrictions_hide_events_tasks_documents_and_node_filter(self):
        self.start()
        self.complete()
        hidden = self.current()
        MaintenanceEvent.objects.create(
            order=self.order,
            actor=self.actor,
            task=hidden,
            action="claim",
            description="隐藏事件",
            details={"comment": "不应泄露的意见"},
        )
        queryset_class = type(MaintenanceTask.objects.all())
        original = queryset_class.restrict

        def restrict(query, user, action):
            result = original(query, user, action)
            if action == "view" and query.model is MaintenanceTask:
                return result.exclude(pk=hidden.pk)
            if action == "view" and query.model is MaintenanceDocument:
                return result.none()
            return result

        with patch.object(queryset_class, "restrict", restrict):
            response = self.page()
            self.assertIsNone(response.context["detail"]["current"])
            self.assertNotContains(response, "不应泄露的意见")
            self.assertNotContains(response, "隐藏事件")
            self.assertNotContains(response, "查看 / 打印表单")
            self.assertEqual(self.page(node=hidden.name).context["total"], 0)
        viewer = self.worker("no_history")
        self.grant(viewer, "maintenancetask", "view")
        response = self.page(viewer)
        self.assertEqual(response.context["detail"]["events"], [])
        self.assertNotContains(response, "不应泄露的意见")

    def test_selected_order_still_requires_view_permission(self):
        outsider = self.worker("outsider")
        queryset_class = type(MaintenanceOrder.objects.all())
        original = queryset_class.restrict

        def restrict(query, user, action):
            result = original(query, user, action)
            return result.exclude(pk=self.order.pk) if query.model is MaintenanceOrder else result

        with patch.object(queryset_class, "restrict", restrict):
            self.assertEqual(self.page(outsider, selected=self.order.pk).status_code, 404)

    def test_actionable_draft_is_in_todo_and_stays_after_starting(self):
        response = self.page(tab="todo")
        self.assertEqual([row["order"].pk for row in response.context["rows"]], [self.order.pk])
        self.assertEqual(response.context["rows"][0]["status_label"], "草稿")
        self.assertContains(response, ">发起流程</button>")
        self.assertContains(response, "撤销草稿")
        self.assertFalse(self.order.tasks.exists())
        self.start()
        response = self.page(tab="todo")
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["detail"]["current"].pk, self.current().pk)

    def test_draft_todo_respects_requester_owner_and_change_permission(self):
        requester = self.worker("draft_requester")
        other = self.worker("unrelated_draft_user")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(requester=requester)
        self.assertEqual(self.page(requester, tab="todo").context["total"], 0)
        self.grant(requester, "maintenanceorder", "change")
        self.assertEqual(self.page(requester, tab="todo").context["total"], 1)
        self.grant(other, "maintenanceorder", "change")
        self.assertEqual(self.page(other, tab="todo").context["total"], 0)
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(owner=other)
        self.assertEqual(self.page(other, tab="todo").context["total"], 1)

    def test_withdrawn_draft_disappears_from_todo(self):
        self.assertEqual(self.page(tab="todo").context["total"], 1)
        services.withdraw_order(pk=self.order.pk, actor=self.actor)
        self.assertEqual(self.page(tab="todo").context["total"], 0)

    def test_draft_cancelled_and_terminated_do_not_show_pending_operations(self):
        response = self.page()
        self.assertContains(response, ">发起流程</button>")
        self.assertIsNone(response.context["detail"]["current"])
        services.withdraw_order(pk=self.order.pk, actor=self.actor)
        response = self.page()
        self.assertContains(response, "已撤销")
        self.assertNotContains(response, 'id="oa-current"')
        self.assertNotContains(response, ">发起流程</button>")

    def test_summary_queries_do_not_grow_per_order(self):
        self.start()
        for index in range(8):
            services.create_order(
                actor=self.actor,
                policy=self.policy,
                service_type="fiber",
                supplier="单位",
                contract_id=self.contract.pk,
                contract_number="HT",
                service_region="广东",
                service_location="站点",
                requirements="维护",
            )
        orders = list(MaintenanceOrder.objects.select_related("requester", "owner"))
        counts = []
        for selected in (orders[:1], orders):
            with CaptureQueriesContext(connection) as queries:
                inbox = InboxPresentation(selected, self.actor, actionable_tasks(self.actor))
                for order in selected:
                    inbox.summary(order)
            counts.append(len(queries))
        # Active orders add one fixed directory lookup for their configured groups.
        self.assertLessEqual(counts[1], counts[0] + 1)

    def test_changed_assignee_refreshes_permission_error_without_exposing_controls(self):
        test_maintenance.MaintenanceTests.reach_legacy_shared_report(self)
        first, second = self.worker("first"), self.worker("second")
        self.grant(first, "maintenancetask", "view")
        self.grant(second, "maintenancedocument", "add")
        services.upload_document(
            pk=self.order.pk,
            actor=second,
            material="report",
            state="paper",
            file=SimpleUploadedFile("report.pdf", b"report"),
        )
        response = self.action("complete", {"comment": "尚未提交的说明"}, actor=first)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.context["detail"]["can_act"])
        self.assertEqual(response.context["detail"]["waiting_for"], "second")
        self.assertContains(response, "尚未提交的说明", status_code=403)
        self.assertNotContains(response, "确认办理完成", status_code=403)

    def test_completed_and_terminated_orders_have_no_future_or_current_node(self):
        self.start()
        while self.order.tasks.filter(status="pending").exists():
            self.complete()
        response = self.page()
        self.assertEqual(response.context["detail"]["status_label"], "已完成")
        self.assertEqual(response.context["detail"]["future_nodes"], [])
        self.assertNotContains(response, 'id="oa-current"')
        order = services.create_order(
            actor=self.actor,
            policy=self.policy,
            service_type="fiber",
            supplier="单位",
            contract_id=self.contract.pk,
            contract_number="HT",
            service_region="广东",
            service_location="站点",
            requirements="维护",
        )
        services.start_order(pk=order.pk, actor=self.actor)
        services.terminate_order(pk=order.pk, actor=self.actor, comment="服务取消，无费用")
        response = self.page(selected=order.pk)
        self.assertEqual(response.context["detail"]["status_label"], "已终止")
        self.assertEqual(response.context["detail"]["future_nodes"], [])
        self.assertNotContains(response, 'id="oa-current"')
        self.assertContains(response, "服务取消，无费用")

    def test_material_workspace_has_no_accordions_and_supports_native_links(self):
        self.reach("order_review")
        response = self.page(panel="material-order", pane="versions")
        self.assertNotContains(response, "<details")
        self.assertNotContains(response, "<summary")
        self.assertContains(response, "管理资料 →")
        self.assertContains(response, "上传 / 修订")
        elements = PanelMarkup(response.content.decode()).elements
        dialog = next(
            attrs
            for tag, attrs in elements
            if tag == "dialog" and attrs.get("id") == "material-order"
        )
        self.assertIn("open", dialog)
        self.assertTrue(
            any(
                tag == "section"
                and attrs.get("data-oa-tab-panel") == "versions"
                and "hidden" not in attrs
                for tag, attrs in elements
            )
        )
        response = self.page(panel="library")
        elements = PanelMarkup(response.content.decode()).elements
        self.assertTrue(
            any(
                tag == "dialog" and attrs.get("id") == "library" and "open" in attrs
                for tag, attrs in elements
            )
        )

    def test_upload_returns_to_material_workspace_with_new_version(self):
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse("plugins:netbox_oa:maintenance_upload", args=[self.order.pk]),
            {
                "material": "contract",
                "state": "electronic",
                "note": "新合同版本",
                "file": SimpleUploadedFile(
                    "contract.pdf", b"document", content_type="application/pdf"
                ),
                "return_query": "tab=mine&sort=waiting",
                "return_panel": "material-contract",
            },
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.url).query)
        self.assertEqual(query["panel"], ["material-contract"])
        self.assertEqual(query["tab"], ["mine"])
        response = self.client.get(response.url)
        self.assertEqual(response.status_code, 200)
        material = next(
            item for item in response.context["detail"]["materials"] if item["key"] == "contract"
        )
        self.assertEqual(material["current_file"].original_name, "contract.pdf")
        self.assertEqual(material["current_file"].version, 1)

    def test_transfer_failure_reopens_panel_and_preserves_inputs(self):
        self.start()
        response = self.action("transfer", {"comment": "需要交接", "return_panel": "routing"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["panel_id"], "routing")
        self.assertEqual(response.context["routing_form"]["comment"].value(), "需要交接")
        elements = PanelMarkup(response.content.decode()).elements
        self.assertTrue(
            any(
                tag == "dialog" and attrs.get("id") == "routing" and "open" in attrs
                for tag, attrs in elements
            )
        )
        self.assertNotContains(response, "<details", status_code=400)

    def test_activity_is_a_separate_region_with_six_recent_icons(self):
        self.start()
        self.complete()
        for index in range(10):
            MaintenanceEvent.objects.create(
                order=self.order,
                actor=self.actor,
                action="edit",
                description=f"更新资料 {index}",
                details={"comment": "保留完整的办理意见"},
            )
        response = self.page()
        self.assertContains(response, '<aside class="card oa-audit" aria-label="审批经过">')
        self.assertContains(response, 'aria-label="当前办理"')
        self.assertContains(response, 'aria-label="查看审批经过"')
        self.assertNotContains(response, "更新资料 9")
        recent = response.context["detail"]["recent_events"]
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["event"].action, "complete")
        self.assertEqual(len({item["event"].task_id for item in recent}), 2)
        elements = PanelMarkup(response.content.decode()).elements
        icons = [attrs for tag, attrs in elements if tag == "a" and "data-oa-event" in attrs]
        self.assertEqual(len(icons), 2)
        self.assertTrue(all(attrs.get("aria-label") for attrs in icons))

    def test_activity_panel_and_paging_work_without_javascript(self):
        for index in range(55):
            MaintenanceEvent.objects.create(
                order=self.order,
                actor=self.actor,
                action="edit",
                description=f"记录 {index}",
            )
        response = self.page(panel="activity")
        elements = PanelMarkup(response.content.decode()).elements
        self.assertTrue(
            any(
                tag == "dialog" and attrs.get("id") == "activity" and "open" in attrs
                for tag, attrs in elements
            )
        )
        self.assertEqual(len(response.context["detail"]["events"]), 50)
        response = self.client.get(response.context["history_url"] + "&panel=activity")
        self.assertEqual(response.context["panel_id"], "activity")
        self.assertEqual(len(response.context["detail"]["events"]), 56)
        self.assertFalse(response.context["detail"]["has_earlier_events"])

    def test_signature_updates_are_folded_into_one_node_record(self):
        self.start()
        task = self.current()
        self.complete()
        existing_signatures = self.order.events.filter(action="signature").count()
        for description in ("发起手机手写签名", "手机手写签名已上传", "确认使用手写签名"):
            MaintenanceEvent.objects.create(
                order=self.order,
                actor=self.actor,
                task=task,
                action="signature",
                description=description,
            )
        response = self.page(panel="activity")
        records = response.context["detail"]["approval_events"]
        self.assertEqual(len([item for item in records if item["event"].task_id == task.pk]), 1)
        self.assertEqual(records[-1]["event"].action, "complete")
        self.assertNotContains(response, "更新进展")
        self.assertNotContains(response, "手机手写签名已上传")
        self.assertEqual(
            self.order.events.filter(action="signature").count(), existing_signatures + 3
        )
