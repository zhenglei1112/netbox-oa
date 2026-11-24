
"""Inbox presentation and HTTP regressions using the standalone permission harness."""

import re
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa import maintenance_signatures as signatures
from netbox_oa.maintenance_flow import MATERIALS
from netbox_oa.maintenance_presentation import InboxPresentation
from netbox_oa.maintenance_views import actionable_tasks
from netbox_oa.models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceOrder,
    MaintenanceTask,
)
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user
from netbox_oa.tests.test_signatures import image_data


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

    def page(self, actor=None, **params):
        self.client.force_login(actor or acting_user(self))
        return self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench"), {"tab": "all", **params}
        )

    def action(self, operation, data=None, task=None, actor=None):
        self.client.force_login(actor or acting_user(self))
        return self.client.post(
            reverse(
                "plugins:netbox_oa:maintenancetask_action",
                args=[(task or self.current()).pk, operation],
            ),
            data or {},
        )

    def test_completed_order_displays_library_cards_inline_without_handling_workspace(self):
        self.start()
        while self.order.tasks.filter(status="pending").exists():
            self.complete()
        response = self.page()
        self.assertEqual(response.context["detail"]["order"].status, "completed")
        html = response.content.decode()
        section = html.split('<section class="oa-document-section"', 1)[1].split(
            "</section>", 1
        )[0]
        self.assertIn("工单资料库", section)
        self.assertEqual(section.count('<article class="oa-document-card'), len(MATERIALS))
        for _, name in MATERIALS:
            self.assertIn(name, section)
        self.assertNotIn("oa-library-overview", section)
        self.assertNotIn("oa-current-materials", section)
        self.assertNotIn('class="oa-current-workspace"', html)
        self.assertNotIn("办理状态", html)
        self.assertContains(response, self.order.requirements)
        self.assertIn('data-oa-open="review-report"', section)
        self.assertIn('data-oa-open="review-order"', section)
        order_card = section.split('<article class="oa-document-card', 1)[1].split(
            "</article>", 1
        )[0]
        self.assertIn('aria-label="查看"', order_card)
        self.assertIn('mdi-eye-outline', order_card)
        self.assertNotIn('data-oa-open="material-order"', order_card)

    def test_done_tab_lists_personally_completed_tasks_once_with_filters(self):
        handler = self.worker("done_handler")
        self.grant(handler, "maintenancetask", "view")
        self.start()
        self.assertEqual(self.page(actor=handler, tab="done").context["total"], 0)
        self.complete()
        self.complete()
        self.order.tasks.filter(status="completed").update(decided_by=handler)
        self.assertEqual(self.page(actor=handler, tab="done").context["total"], 0)
        MaintenanceTask.objects.filter(pk=self.current().pk).update(assigned_user=self.approver)
        response = self.page(actor=handler, tab="done")
        self.assertEqual(response.context["tab"], "done")
        self.assertEqual(response.context["total"], 1)
        self.assertEqual([row["order"].pk for row in response.context["rows"]], [self.order.pk])
        self.assertContains(response, ">已办 <span")
        self.assertIn("tab=done", response.context["list_url"])
        self.assertEqual(
            self.page(actor=handler, tab="done", q=self.order.number).context["total"], 1
        )
        self.assertEqual(
            self.page(actor=handler, tab="done", q="does-not-exist").context["total"], 0
        )
        self.assertEqual(self.page(actor=handler, tab="done", stage="payment").context["total"], 0)
        colleague = self.worker("done_colleague")
        self.grant(colleague, "maintenancetask", "view")
        self.assertEqual(self.page(actor=colleague, tab="done").context["total"], 0)

    def test_payment_plan_moves_from_todo_to_pending_payment(self):
        self.reach("payment_plan")
        response = self.page(actor=self.actor, tab="pending_payment")
        self.assertEqual([row["order"].pk for row in response.context["rows"]], [self.order.pk])
        counts = {item["key"]: item["count"] for item in response.context["tabs"]}
        self.assertEqual(counts["pending_payment"], 1)
        self.assertEqual(counts["todo"], 0)
        self.assertEqual(self.page(actor=self.actor, tab="todo").context["total"], 0)
        self.assertEqual(self.page(actor=self.approver, tab="pending_payment").context["total"], 0)
        for params in ({"q": "missing"}, {"stage": "ordering"}, {"node": "付款部门审核"}):
            with self.subTest(params=params):
                self.assertEqual(self.page(actor=self.actor, tab="pending_payment",
                                           **params).context["total"], 0)
        legacy = self.page(actor=self.actor, tab="pending_payment_application")
        self.assertEqual(legacy.context["tab"], "todo")
        self.assertEqual(legacy.context["total"], 0)
        self.complete()
        self.assertEqual(self.page(actor=self.actor, tab="pending_payment").context["total"], 0)

    def test_pending_payment_uses_requester_and_snapshot_key(self):
        self.reach("payment_plan")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(owner=self.approver)
        self.order.tasks.filter(pk=self.current().pk).update(name="自定义付款节点名称")
        self.assertEqual(self.page(actor=self.actor, tab="pending_payment").context["total"], 1)
        self.assertEqual(self.page(actor=self.approver, tab="pending_payment").context["total"], 0)
        services.transfer_task(pk=self.current().pk, actor=self.actor,
                               user_id=self.approver.pk, comment="交办付款资料准备")
        self.assertEqual(self.page(actor=self.actor, tab="pending_payment").context["total"], 1)
        self.assertEqual(self.page(actor=self.approver, tab="todo").context["total"], 1)
        services.terminate_order(pk=self.order.pk, actor=self.actor, comment="停止办理")
        self.assertEqual(self.page(actor=self.actor, tab="pending_payment").context["total"], 0)

    def test_pending_payment_tab_only_lists_requesters_payment_step(self):
        self.reach("payment_eip")
        response = self.page(actor=self.actor, tab="pending_payment")
        self.assertEqual(response.context["tab"], "pending_payment")
        self.assertEqual([row["order"].pk for row in response.context["rows"]], [self.order.pk])
        self.assertContains(response, ">待付款 <span")
        self.assertEqual(self.page(actor=self.actor, tab="todo").context["total"], 0)
        self.assertEqual(self.page(actor=self.approver, tab="pending_payment").context["total"], 0)
        self.assertEqual(
            self.page(actor=self.actor, tab="pending_payment", q="missing").context["total"], 0
        )
        self.assertEqual(
            self.page(actor=self.actor, tab="pending_payment", stage="ordering").context["total"], 0
        )
        self.complete()
        self.assertEqual(self.page(actor=self.actor, tab="pending_payment").context["total"], 0)

    def test_tab_counts_match_groups_and_ignore_search(self):
        self.reach("payment_eip")
        self.order.followers.add(self.actor)
        response = self.page(actor=self.actor, q="missing")
        counts = {item["key"]: item["count"] for item in response.context["tabs"]}
        self.assertEqual(counts, {
            "todo": 0, "pending_payment": 1, "delegate": 0,
            "done": 0, "following": 1, "all": 1,
        })
        self.assertEqual(response.context["total"], 0)
        for key, count in counts.items():
            page = self.page(actor=self.actor, tab=key)
            self.assertEqual(page.context["total"], count)
        self.assertContains(response, '<span class="oa-tab-count">1</span>')

    def test_done_tab_requires_task_view_permission(self):
        self.start()
        user = self.worker("done_without_view")
        task = self.current()
        MaintenanceTask.objects.filter(pk=task.pk).update(status="completed", decided_by=user)
        self.assertEqual(self.page(actor=user, tab="done").context["total"], 0)
        self.grant(user, "maintenancetask", "view")
        self.assertEqual(self.page(actor=user, tab="done").context["total"], 1)

    def test_done_tab_includes_own_orders_without_completed_tasks(self):
        response = self.page(actor=self.actor, tab="done")
        self.assertEqual(response.context["total"], 0)
        self.assertEqual([tab["key"] for tab in response.context["tabs"]],
                         ["todo", "pending_payment",
                          "delegate", "done", "following", "all"])
        self.assertNotContains(response, ">发起</a>")
        self.start()
        self.assertEqual(self.page(actor=self.actor, tab="done").context["total"], 1)
        self.assertEqual(self.page(actor=self.actor, tab="done", q="missing").context["total"], 0)
        self.complete()
        self.order.tasks.filter(status="completed").update(decided_by=self.actor)
        self.assertEqual(self.page(actor=self.actor, tab="done").context["total"], 1)

    def test_old_mine_tab_opens_done_tab(self):
        self.start()
        response = self.page(actor=self.actor, tab="mine")
        self.assertEqual(response.context["tab"], "done")
        self.assertEqual(response.context["total"], 1)
        self.assertIn("tab=done", response.context["list_url"])

    def test_returned_order_leaves_done_until_requester_resubmits(self):
        self.start()
        self.assertEqual(self.page(actor=self.actor, tab="done").context["total"], 1)
        services.return_task(pk=self.current().pk, actor=self.approver, comment="请补正")
        self.assertEqual(self.page(actor=self.actor, tab="todo").context["total"], 1)
        self.assertEqual(self.page(actor=self.actor, tab="done").context["total"], 0)
        self.upload("order")
        self.complete()
        self.assertEqual(self.page(actor=self.actor, tab="todo").context["total"], 0)
        self.assertEqual(self.page(actor=self.actor, tab="done").context["total"], 1)

    def test_initiated_time_matches_start_event_instead_of_draft_creation(self):
        draft = self.page().context["rows"][0]
        self.assertEqual(draft["initiated_at"], self.order.created)
        self.start()
        started = MaintenanceEvent.objects.get(order=self.order, action="start")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(
            created=started.created - timedelta(minutes=3)
        )
        response = self.page()
        row = response.context["rows"][0]
        detail = response.context["detail"]
        self.assertEqual(row["initiated_at"], started.created)
        self.assertEqual(detail["initiated_at"], started.created)
        self.assertNotEqual(row["initiated_at"], row["order"].created)

    def test_draft_material_card_offers_edit_instead_of_print_or_upload(self):
        response = self.page()
        self.assertEqual(response.context["detail"]["required_materials"], {"order"})
        html = response.content.decode()
        card = html.split('<article class="oa-document-card', 1)[1].split("</article>", 1)[0]
        self.assertIn('aria-label="编辑"', card)
        self.assertIn(
            reverse("plugins:netbox_oa:maintenanceorder_edit", args=[self.order.pk]), card
        )
        self.assertNotIn('aria-label="打印"', card)
        self.assertNotIn('aria-label="上传"', card)
        self.assertNotIn('aria-label="审阅"', card)

    def test_seal_stages_hide_upload_and_reject_upload_page(self):
        self.start()
        for material in ("order", "acceptance"):
            key = {"order": "order_senior_seal", "acceptance": "acceptance_company"}[material]
            while self.order.steps_snapshot[self.current().position]["key"] != key:
                self.complete()
            response = self.page()
            item = next(item for item in response.context["detail"]["materials"]
                        if item["key"] == material)
            self.assertFalse(item["can_upload_now"])
            self.assertFalse(item["can_seal_scans"])
            self.assertFalse(item["stage_upload_required"])
            self.assertFalse(services.material_maintenance_documents(
                self.order, self.current(), material,
            ).exists())
            response = self.client.get(
                reverse("plugins:netbox_oa:maintenance_material_upload",
                        args=[self.order.pk, material]),
            )
            self.assertEqual(response.status_code, 403)
            self.complete()

    def test_sealing_stages_offer_upload_and_view_without_print(self):
        self.reach("order_senior_seal")
        response = self.page()
        card = response.content.decode().split('<article class="oa-document-card', 1)[1].split(
            "</article>", 1
        )[0]
        self.assertNotIn('aria-label="上传"', card)
        self.complete()
        self.assertEqual(self.current().name, "验收申请")
        self.assertNotContains(self.page(), "用印后")

    def test_payment_error_shows_every_missing_material_without_state_explanation(self):
        self.reach("payment_plan")
        page = self.page()
        self.assertContains(page, "提交资料")
        self.assertNotContains(page, "外部付款申请单号")
        self.assertNotContains(page, "不代表已付款或已收款")
        self.assertNotContains(page, "登记外部流程已通过")
        response = self.action("complete", {"external_reference": "EIP-001"})
        self.assertContains(
            response, "请上传以下材料：付款通知书、电子发票。",
            status_code=400,
        )
        self.assertNotContains(response, "材料状态至少", status_code=400)

    def test_material_filling_classification_applies_to_every_material(self):
        for step in (None, "order_review", "acceptance_apply", "acceptance_apply", "payment_eip"):
            with self.subTest(step=step):
                if step:
                    if step == "order_review":
                        self.start()
                    while self.order.steps_snapshot[self.current().position]["key"] != step:
                        self.complete()
                response = self.page()
                html = response.content.decode()
                for material in response.context["detail"]["materials"]:
                    requires_filling = material["key"] in {"order", "acceptance"}
                    self.assertEqual(material["requires_filling"], requires_filling)
                    if not requires_filling:
                        self.assertFalse(material["can_fill"])
                        self.assertNotIn(
                            reverse(
                                "plugins:netbox_oa:maintenance_company_form",
                                args=[self.order.pk, material["key"]],
                            ),
                            html,
                        )
                cards = html.split('<article class="oa-document-card')[1:]
                for card in cards:
                    card = card.split("</article>", 1)[0]
                    if "维护服务工单" not in card and "工单服务验收单" not in card:
                        self.assertIn("扫描件", card)
                        self.assertNotIn('aria-label="编辑"', card)
                self.assertNotIn("无需填写", html)
                self.assertNotIn("服务结算单", html)
                self.assertNotIn("mdi-calculator", html)
                self.assertIn("服务验收申请单", html)
                self.assertEqual(
                    [(group["name"], [item["key"] for item in group["materials"]])
                     for group in response.context["detail"]["document_groups"]],
                    [("电子版", ["order", "acceptance"]),
                     ("扫描件", ["settlement", "report", "notice", "invoice"])],
                )
                self.assertNotIn("甲方表单用于填写基础信息", html)


    def test_company_forms_use_only_electronic_cards_and_preview(self):
        self.reach("acceptance_apply")
        detail = self.page().context["detail"]
        self.assertEqual([group["key"] for group in detail["document_groups"]],
                         ["electronic", "scan"])
        electronic = detail["document_groups"][0]["materials"][0]
        self.assertTrue(electronic["current_file"].electronic_seal)
        self.assertFalse(electronic["can_upload_now"])
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        preview = self.client.get(url, {"material": "order", "category": "electronic"})
        self.assertEqual(preview.context["selected"].pk, electronic["current_file"].pk)
        self.assertNotContains(preview, "用印后")

    def test_approved_forms_are_printable_in_next_submission_workspace(self):
        self.start()
        for step, key in (("acceptance_apply", "order"), ("payment_plan", "acceptance")):
            with self.subTest(step=step, material=key):
                while self.order.steps_snapshot[self.current().position]["key"] != step:
                    self.complete()
                response = self.page()
                current_cards = [
                    item for group in response.context["detail"]["current_document_groups"]
                    for item in group["materials"]
                ]
                material = next(item for item in current_cards if item["key"] == key)
                self.assertTrue(material["can_print_now"])
                document = material["form_document"]
                self.assertTrue(document.electronic_seal)
                section = response.content.decode().split(
                    '<section class="oa-document-section"', 1
                )[1].split("</section>", 1)[0]
                card = next(card.split("</article>", 1)[0]
                            for card in section.split('<article class="oa-document-card')[1:]
                            if material["name"] in card.split("</article>", 1)[0])
                url = reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
                self.assertIn('aria-label="打印"', card)
                self.assertIn(url, card)
                self.assertIn('aria-label="查看"', card)
                self.assertNotIn('aria-label="编辑"', card)
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_unapproved_forms_do_not_offer_next_stage_printing(self):
        self.start()
        for step, key in (("order_senior_seal", "order"),
                          ("acceptance_company", "acceptance")):
            with self.subTest(step=step):
                while self.order.steps_snapshot[self.current().position]["key"] != step:
                    self.complete()
                detail = self.page().context["detail"]
                material = next(item for item in detail["materials"] if item["key"] == key)
                self.assertFalse(material["can_print_now"])

    def test_non_filling_material_operations_always_offer_read_alongside_upload(self):
        for key, _ in MATERIALS:
            if key == "order":
                continue
            for required, can_act, can_upload, permission, kind in (
                (True, True, True, True, "submission"),
                (True, True, True, True, "action"),
                (True, True, False, True, "approval"),
                (True, True, False, True, "countersign"),
                (True, False, True, True, "submission"),
                (True, True, True, False, "submission"),
                (False, True, True, True, "submission"),
            ):
                with self.subTest(material=key, required=required, actor=can_act,
                                  upload=can_upload, permission=permission, kind=kind):
                    html = render_to_string(
                        "netbox_oa/maintenance/inbox_material_operations.html",
                        {
                            "detail": {
                                "order": self.order,
                                "can_act": can_act,
                                "current": {"kind": kind},
                            },
                            "material": {
                                "key": key, "requires_filling": False,
                                "required_now": required, "can_upload_now": can_upload,
                                # Legacy forms must never introduce a print button.
                                "form_document": {"pk": 999},
                            },
                            "perms": {"netbox_oa": {"add_maintenancedocument": permission}},
                            "detail_url": "/workbench/?selected=1",
                        },
                    )
                    uploads = required and can_act and can_upload and permission
                    self.assertIn('aria-label="查看"', html)
                    self.assertIn(f'data-oa-open="review-{key}"', html)
                    if uploads:
                        self.assertIn('aria-label="上传"', html)
                    else:
                        self.assertNotIn('aria-label="上传"', html)
                    self.assertNotIn('aria-label="打印"', html)
                    self.assertNotIn('aria-label="编辑"', html)
                    self.assertNotIn("btn-icon", html)

    def test_review_opens_current_material_and_supports_index_switching(self):
        self.start()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_group"]["key"], "order")
        document = response.context["selected"]
        self.assertEqual(document.material, "order")
        self.assertContains(response, 'aria-label="材料索引"')
        self.assertContains(response, "?material=report")
        printed = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk]), {"embed": "1"}
        )
        self.assertNotContains(printed, 'class="print-toolbar"')
        self.assertEqual(printed["X-Frame-Options"], "SAMEORIGIN")
        empty = self.client.get(url, {"material": "settlement"})
        self.assertIsNone(empty.context["selected"])
        self.assertTrue(empty.context["review_items"])
        self.assertContains(empty, 'aria-label="材料预览"')
        self.assertEqual(self.client.get(url, {"material": "unknown"}).status_code, 404)
        self.assertEqual(self.client.get(url, {"document": 99999}).status_code, 404)

    def test_review_file_types_keep_their_order_and_selected_version(self):
        self.reach("acceptance_apply")
        self.client.force_login(self.actor)
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk]),
            {"material": "order"},
        )
        markup = PanelMarkup(response.content.decode())
        files = [attrs for tag, attrs in markup.elements
                 if tag == "a" and "material-file" in attrs.get("class", "").split()]
        self.assertEqual({attrs["data-file-type"] for attrs in files},
                         {"electronic", "scan"})
        selected = [attrs for attrs in files if attrs.get("aria-current") == "page"]
        self.assertEqual(len(selected), 1)
        self.assertIn("btn-primary", selected[0]["class"].split())
        self.assertTrue(selected[0]["href"].endswith(
            f"document={response.context['selected'].pk}"
        ))
        self.assertNotContains(response, "<select")

    def test_stage_upload_notice_uses_current_task_and_resets_after_return(self):
        self.reach("acceptance_apply")
        response = self.page()
        self.assertContains(response, "本阶段待上传")
        document = self.upload("report")
        response = self.page()
        material = next(item for item in response.context["detail"]["materials"]
                        if item["key"] == "report")
        self.assertTrue(material["stage_upload_complete"])
        self.assertEqual(material["stage_upload_document"], document)
        self.assertContains(response, "本阶段已完成上传")
        uploaded_at = timezone.localtime(document.created).strftime("%Y-%m-%d %H:%M:%S")
        self.assertContains(response, f"最近上传：{uploaded_at}")
        self.assertContains(response, f" · v{document.version}")
        self.complete()
        response = self.page()
        self.assertNotContains(response, "本阶段已完成上传")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="重新上传",
        )
        response = self.page()
        self.assertContains(response, "本阶段待上传")
        self.assertNotContains(response, "本阶段已完成上传")
        revised = self.upload("report")
        response = self.page()
        material = next(item for item in response.context["detail"]["materials"]
                        if item["key"] == "report")
        self.assertTrue(material["stage_upload_complete"])
        self.assertEqual(material["stage_upload_document"], revised)

    def test_returned_scans_remain_browsable_and_can_be_revised(self):
        self.reach("acceptance_apply")
        for material_key in ("report",):
            fill_key = "acceptance_apply"
            while self.order.steps_snapshot[self.current().position]["key"] != fill_key:
                self.complete()
            with self.subTest(material=material_key):
                originals = [self.upload(material_key), self.upload(material_key)]
                self.complete()
                services.return_task(
                    pk=self.current().pk, actor=acting_user(self), comment="补正材料",
                )
                response = self.page()
                material = next(item for item in response.context["detail"]["materials"]
                                if item["key"] == material_key)
                self.assertEqual(set(material["scan_sources"]), set(originals))
                self.assertFalse(material["stage_upload_complete"])
                with self.assertRaises(ValidationError):
                    services.complete_task(pk=self.current().pk, actor=acting_user(self))
                review_url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
                review = self.client.get(review_url, {"material": material_key})
                self.assertEqual(set(review.context["selected_group"]["documents"]), set(originals))
                self.assertEqual(review.context["selected_group"]["item_count"], 2)
                self.assertEqual(self.client.get(review_url, {
                    "material": material_key, "document": originals[0].pk,
                }).context["selected"], originals[0])
                revised = services.upload_document(
                    pk=self.order.pk, actor=self.actor, material=material_key, state="scan",
                    upload_mode="replace", replaces_document_id=originals[0].pk,
                    file=SimpleUploadedFile("revised.pdf", b"scan"),
                )
                review = self.client.get(review_url, {"material": material_key})
                self.assertEqual(set(review.context["selected_group"]["documents"]),
                                 {revised, originals[1]})
                self.assertTrue(MaintenanceDocument.objects.filter(pk=originals[0].pk).exists())




    def test_attachment_upload_notice_does_not_count_earlier_task_uploads(self):
        self.upload("notice")
        self.reach("payment_plan")
        response = self.page()
        material = next(item for item in response.context["detail"]["materials"]
                        if item["key"] == "notice")
        self.assertFalse(material["stage_upload_complete"])
        self.assertIsNone(material["stage_upload_document"])
        document = self.upload("notice")
        response = self.page()
        material = next(item for item in response.context["detail"]["materials"]
                        if item["key"] == "notice")
        self.assertTrue(material["stage_upload_complete"])
        self.assertEqual(material["stage_upload_document"], document)

    def test_upload_embedded_form_and_completion_stay_inside_modal(self):
        self.reach("acceptance_apply")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_material_upload",
                      args=[self.order.pk, "report"]) + "?embed=1"
        page = self.client.get(url)
        self.assertContains(page, 'class="oa-upload-embedded"')
        self.assertContains(page, f'data-upload-heading="{self.order.number} · 服务报告"')
        self.assertNotContains(page, '<h1')
        self.assertNotContains(page, 'name="note"')
        self.assertContains(page, "data-upload-cancel")
        self.assertEqual(page.headers["X-Frame-Options"], "SAMEORIGIN")
        invalid = self.client.post(url, {"file": SimpleUploadedFile("bad.docx", b"bad")})
        self.assertContains(invalid, 'class="oa-upload-embedded"')
        self.assertTrue(invalid.context["form"].errors)
        completed = self.client.post(url, {"file": SimpleUploadedFile("scan.pdf", b"scan")})
        self.assertEqual(completed.status_code, 200)
        self.assertContains(completed, "maintenance-upload-complete.js")
        self.assertContains(completed, "材料上传成功")
        self.assertNotIn("Location", completed.headers)
        self.assertEqual(self.order.documents.filter(material="report").count(), 1)



    def test_dedicated_upload_page_handles_attachments_and_hides_other_nodes(self):
        self.reach("payment_plan")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_material_upload",
                      args=[self.order.pk, "notice"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["scan"])
        failed = self.client.post(url, {"file": [
            SimpleUploadedFile("valid.pdf", b"document"),
            SimpleUploadedFile("bad.exe", b"document"),
        ]})
        self.assertEqual(failed.status_code, 200)
        self.assertTrue(failed.context["form"].errors)
        self.assertFalse(self.order.documents.filter(material="notice").exists())
        result = self.client.post(url, {"file": [
            SimpleUploadedFile("notice.pdf", b"document"),
            SimpleUploadedFile("revised.pdf", b"document"),
        ]})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(
            self.order.documents.filter(material="notice", kind="attachment").count(), 2
        )
        review = self.client.get(reverse("plugins:netbox_oa:maintenance_review",
                                        args=[self.order.pk]), {"material": "notice"})
        self.assertEqual(len(review.context["selected_group"]["documents"]), 2)
        self.assertContains(review, "notice.pdf")
        self.assertContains(review, "revised.pdf")
        forbidden = reverse("plugins:netbox_oa:maintenance_material_upload",
                            args=[self.order.pk, "report"])
        self.assertEqual(self.client.get(forbidden).status_code, 403)
        self.assertEqual(self.client.get(url.replace("notice", "unknown")).status_code, 404)

    def test_review_material_heading_upload_follows_current_node_permissions(self):
        self.reach("acceptance_apply")
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        for embed in ("", "1"):
            response = self.client.get(url, {"material": "report", "embed": embed})
            allowed = [group["key"] for group in response.context["groups"] if group["can_upload"]]
            self.assertEqual(allowed, ["settlement", "report"])
            self.assertNotContains(response, "mdi-calculator")
            self.assertContains(response, "服务验收申请单")
            self.assertContains(response, 'data-review-upload="report"', count=1)
            self.assertContains(response, reverse(
                "plugins:netbox_oa:maintenance_material_upload", args=[self.order.pk, "report"]
            ))
            self.assertContains(response, 'class="btn btn-ghost-primary review-upload"')
            self.assertNotContains(response, 'data-review-upload="order"')

        original_has_perm = type(self.actor).has_perm

        def without_upload_permission(user, permission, obj=None):
            if permission == "netbox_oa.add_maintenancedocument":
                return False
            return original_has_perm(user, permission, obj)

        with patch.object(type(self.actor), "has_perm", without_upload_permission):
            response = self.client.get(url, {"material": "report"})
            self.assertNotContains(response, "data-review-upload=")
            upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                                 args=[self.order.pk, "report"])
            self.assertEqual(self.client.get(upload_url).status_code, 403)
            self.assertEqual(self.client.post(upload_url, {}).status_code, 403)

        self.complete()
        response = self.client.get(url, {"material": "report"})
        self.assertNotContains(response, "data-review-upload=")

    def test_review_opens_in_current_page_drawer(self):
        self.start()
        response = self.page()
        self.assertContains(response, 'data-oa-open="review-order"')
        self.assertContains(response, 'class="oa-panel oa-review-panel"')
        self.assertContains(response, "data-review-src=")
        self.assertNotContains(response, 'class="oa-review-frame" title="维护服务工单审阅" src=')
        opened = self.page(panel="review-order")
        markup = PanelMarkup(opened.content.decode())
        self.assertTrue(
            any(
                tag == "dialog" and attrs.get("id") == "review-order" and "open" in attrs
                for tag, attrs in markup.elements
            )
        )
        self.assertTrue(
            any(
                tag == "iframe" and attrs.get("src", "").endswith(
                    "?material=order&category=electronic&embed=1"
                )
                for tag, attrs in markup.elements
            )
        )
        viewer = self.client.get(
            reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk]),
            {"material": "order", "embed": "1"},
        )
        self.assertEqual(viewer["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(viewer, "?embed=1&amp;material=report")
        self.assertNotContains(viewer, "返回审批")
        self.assertNotContains(viewer, 'class="oa-review-header"')
        self.assertContains(viewer, 'class="review-document-heading"')
        self.assertNotContains(viewer, "data-pdf-viewer")
        review_panel = (
            opened.content.decode().split('id="review-order"', 1)[1].split("</dialog>", 1)[0]
        )
        self.assertNotIn("oa-panel-header", review_panel)
        self.assertIn('class="oa-review-header"', review_panel)
        self.assertIn(f"<strong>{self.order.number}</strong>", review_panel)
        self.assertIn('aria-label="关闭材料审阅"', review_panel)

    def test_review_previews_scans_inline_and_checks_permissions(self):
        self.reach("acceptance_apply")
        self.upload("report")
        self.upload("report")
        self.complete()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_review", args=[self.order.pk])
        response = self.client.get(url, {"material": "report"})
        document = response.context["selected"]
        self.assertEqual(document.kind, "scan")
        files = response.context["selected_group"]["documents"]
        self.assertEqual(len(files), 2)
        switched = self.client.get(url, {"material": "report", "document": files[1].pk})
        self.assertEqual(switched.context["selected"].pk, files[1].pk)
        preview_url = reverse("plugins:netbox_oa:maintenance_preview", args=[document.pk])
        preview = self.client.get(preview_url)
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview["Content-Disposition"].startswith("inline;"))
        self.assertEqual(preview["X-Frame-Options"], "SAMEORIGIN")
        preview.close()
        worker = self.worker("no_document_access")
        self.grant(worker, "maintenanceorder", "view")
        self.client.force_login(worker)
        response = self.client.get(url, {"material": "report"})
        self.assertIsNone(response.context["selected"])
        self.assertEqual(response.context["review_items"], [])
        self.assertEqual(self.client.get(preview_url).status_code, 404)
        outsider = type(self.actor).objects.create_user("no_order_access", password="test")
        self.grant(outsider, "maintenancedocument", "view")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(preview_url).status_code, 403)

    def test_group_personal_and_admin_status_are_distinct(self):
        self.reach("order_review")
        worker = self.worker("handler")
        self.grant(worker, "maintenancetask", "view")
        self.grant(worker, "maintenanceevent", "view")
        response = self.page(worker)
        self.assertEqual(response.context["rows"][0]["status_label"], "待你审批")
        self.assertContains(response, "Maintenance handlers")
        admin = self.page().context["rows"][0]
        self.assertEqual(admin["status_label"], "可代办")
        self.assertFalse(admin["assigned_to_me"])
        self.grant(worker, "maintenancedocument", "add")
        self.grant(worker, "maintenancedocument", "view")
        session = signatures.create_session(pk=self.current().pk, actor=worker)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        other = self.worker("other")
        self.grant(other, "maintenancetask", "view")
        row = self.page(other).context["rows"][0]
        self.assertEqual(row["status_label"], "等待他人")
        self.assertFalse(row["can_act"])
        self.assertEqual(row["waiting_for"], "handler")

    def test_approval_node_contains_controls_and_required_material(self):
        self.reach("order_review")
        self.approver.groups.add(self.group)
        response = self.page()
        self.assertEqual(response.context["detail"]["status_label"], "待你审批")
        self.assertContains(response, ">同意</button>")
        self.assertContains(response, "退回补正")
        materials = {item["key"]: item for item in response.context["detail"]["materials"]}
        self.assertTrue(materials["order"]["required_now"])
        self.assertFalse(materials["report"]["required_now"])
        self.assertContains(response, 'aria-label="审阅"')
        html = response.content.decode()
        cards, workspace = html.split('<section class="oa-v2-handle"', 1)
        self.assertIn('aria-label="审阅"', cards)
        self.assertNotIn('aria-label="审阅"', workspace)
        self.assertIn("data-signature-widget", workspace)

    def test_material_upload_operations_are_in_current_cards_at_each_stage(self):
        self.start()
        for step, label in (
            ("acceptance_apply", 'aria-label="上传"'),
            ("acceptance_apply", 'aria-label="上传"'),
        ):
            with self.subTest(step=step):
                while self.order.steps_snapshot[self.current().position]["key"] != step:
                    self.complete()
                response = self.page()
                html = response.content.decode()
                cards, workspace = html.split('<section class="oa-v2-handle"', 1)
                self.assertIn(label, cards)
                current_card = next(
                    card.split("</article>", 1)[0]
                    for card in cards.split('<article class="oa-document-card')[1:]
                    if label in card.split("</article>", 1)[0]
                )
                self.assertIn('aria-label="上传"', current_card)
                self.assertIn('aria-label="查看"', current_card)
                self.assertNotIn('class="oa-stage-materials', cards)
                self.assertNotIn(label, workspace)
                self.assertIn("提交资料", workspace)

    def test_completed_node_becomes_history_and_returns_to_original_list(self):
        self.reach("order_review")
        worker = self.worker("first_handler")
        self.grant(worker, "maintenancetask", "view")
        self.grant(worker, "maintenanceevent", "view")
        self.grant(worker, "maintenancedocument", "add")
        self.grant(worker, "maintenancedocument", "view")
        task = self.current()
        session = signatures.create_session(pk=task.pk, actor=worker)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
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
        self.assertEqual(query, {
            "tab": ["todo"], "sort": ["waiting"], "q": ["惠州"],
            "stage": ["ordering"], "node": ["确认"],
            "selected": [str(self.order.pk)], "restore": ["1"],
        })
        page = self.client.get(response.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["tab"], "todo")
        self.assertEqual(page.context["total"], 0)
        self.assertIsNone(page.context["detail"])
        self.assertFalse(page.context["outside_list"])
        self.assertContains(page, "已同意。")
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.comment, "编号确认无误")
        history = self.page(actor=worker, selected=self.order.pk)
        self.assertContains(history, "编号确认无误")
        self.assertContains(history, "工单复核")

    def test_reading_and_failed_operations_do_not_assign_shared_task(self):
        self.reach("order_review")
        first, second = self.worker("shared_first"), self.worker("shared_second")
        self.grant(first, "maintenancedocument", "add")
        self.grant(first, "maintenancedocument", "view")
        for actor in (first, second):
            self.grant(actor, "maintenancetask", "view")
            page = self.page(actor, tab="todo")
            self.assertEqual(page.context["total"], 1)
            self.assertTrue(page.context["rows"][0]["can_act"])
        self.assertIsNone(self.current().assigned_user_id)
        self.assertIsNone(self.current().started_at)
        with self.assertRaises(ValidationError):
            services.return_task(pk=self.current().pk, actor=first, comment="x" * 4001)
        with self.assertRaises(ValidationError):
            services.upload_document(
                pk=self.order.pk,
                actor=first,
                material="report",
                file=SimpleUploadedFile("report.exe", b"invalid"),
            )
        self.assertIsNone(self.current().assigned_user_id)
        self.assertIsNone(self.current().started_at)
        self.grant(first, "maintenancedocument", "view")
        signatures.create_session(pk=self.current().pk, actor=first)
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

    def test_approval_actions_default_empty_comments(self):
        self.start()
        task = self.current()
        test_maintenance.sign_task(task, self.approver)
        response = self.page()
        self.assertEqual(response.context["action_form"].fields["comment"].label, "处理意见")
        response = self.action("complete")
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.comment, "同意")
        event = MaintenanceEvent.objects.get(task=task, action="complete")
        self.assertEqual(event.details["comment"], "同意")
        task = self.current()
        response = self.action("return", {"comment": " \n\t"})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.comment, "退回补正")
        event = MaintenanceEvent.objects.get(task=task, action="return")
        self.assertEqual(event.details["comment"], "退回补正")

    def test_approval_actions_preserve_written_comments(self):
        self.start()
        task = self.current()
        test_maintenance.sign_task(task, self.approver)
        self.assertEqual(self.action("complete", {"comment": "核对无误"}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.comment, "核对无误")
        task = self.current()
        self.assertEqual(self.action("return", {"comment": "请补充服务范围"}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.comment, "请补充服务范围")

    def test_invalid_action_keeps_bound_fields_and_filters(self):
        self.reach("order_review")
        response = self.action("return", {
            "comment": " ", "user_id": "invalid", "return_query": "tab=done&sort=waiting",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["tab"], "done")
        self.assertTrue(response.context["action_form"].is_bound)
        self.assertEqual(response.context["action_form"]["comment"].value(), " ")
        self.assertTrue(response.context["action_error"])
        response = self.action("complete", {"comment": "保留我的意见", "user_id": "invalid"})
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
        for expected in (100, 117):
            response = self.client.get(response.context["history_url"])
            self.assertEqual(len(response.context["detail"]["events"]), expected)
        self.assertFalse(response.context["detail"]["has_earlier_events"])

    def test_return_and_reapproval_do_not_replace_earlier_events(self):
        self.reach("order_review")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="请补充说明",
        )
        self.upload("order")
        self.complete()
        test_maintenance.sign_task(self.current(), acting_user(self))
        services.complete_task(pk=self.current().pk, actor=acting_user(self), comment="补正后同意")
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
            pk=self.current().pk, actor=acting_user(self), user_id=target.pk, comment="加签意见"
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

    def test_actionable_draft_is_in_todo_and_review_moves_to_delegate_for_admin(self):
        response = self.page(tab="todo")
        self.assertEqual([row["order"].pk for row in response.context["rows"]], [self.order.pk])
        self.assertEqual(response.context["rows"][0]["status_label"], "草稿")
        self.assertContains(response, "编辑、签名并提交审批")
        self.assertContains(response, "撤销草稿")
        self.assertFalse(self.order.tasks.exists())
        self.start()
        self.assertEqual(self.page(tab="todo").context["total"], 0)
        response = self.page(tab="delegate")
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["detail"]["current"].pk, self.current().pk)

    def test_delegate_tab_separates_delegable_tasks_and_preserves_filters(self):
        self.start()
        response = self.page(actor=self.approver, tab="delegate", sort="waiting")
        self.assertEqual(response.context["tab"], "delegate")
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["rows"][0]["status_label"], "可代办")
        self.assertTrue(response.context["rows"][0]["can_act"])
        self.assertContains(response, ">代办 <span")
        self.assertIn("tab=delegate", response.context["rows"][0]["url"])
        self.assertEqual(response.context["stats"]["todo"], 0)
        self.assertEqual(self.page(actor=self.approver, tab="todo").context["total"], 0)
        self.assertEqual(self.page(tab="delegate", q=self.order.number).context["total"], 1)
        self.assertEqual(self.page(tab="delegate", q="missing").context["total"], 0)
        self.assertEqual(self.page(tab="delegate", stage="payment").context["total"], 0)
        self.assertEqual(self.page(actor=self.actor, tab="delegate").context["total"], 0)
        response = self.action("return", {
            "comment": " ", "user_id": "invalid", "return_query": "tab=delegate",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context["tab"], "delegate")

    def test_delegate_tab_respects_personal_group_and_explicit_assignments(self):
        self.start()
        self.approver.groups.add(self.group)
        self.assertEqual(self.page(actor=self.approver, tab="todo").context["total"], 1)
        self.assertEqual(self.page(actor=self.approver, tab="delegate").context["total"], 0)
        worker = self.worker("delegate_assignee")
        self.grant(worker, "maintenancetask", "view")
        MaintenanceTask.objects.filter(pk=self.current().pk).update(assigned_user=worker)
        self.assertEqual(self.page(actor=self.approver, tab="todo").context["total"], 0)
        self.assertEqual(self.page(actor=self.approver, tab="delegate").context["total"], 1)
        self.assertEqual(self.page(actor=worker, tab="todo").context["total"], 1)
        self.assertEqual(self.page(actor=worker, tab="delegate").context["total"], 0)
        worker.groups.clear()
        MaintenanceTask.objects.filter(pk=self.current().pk).update(assigned_user=None)
        self.assertEqual(self.page(actor=worker, tab="todo").context["total"], 0)
        self.assertEqual(self.page(actor=worker, tab="delegate").context["total"], 0)

    def test_removed_seal_steps_do_not_create_requester_reminders(self):
        self.reach("order_senior_seal")
        requester = self.worker("seal_requester")
        self.grant(requester, "maintenancetask", "view")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(requester=requester)
        response = self.page(actor=requester, tab="todo")
        self.assertEqual(response.context["total"], 0)


    def test_draft_todo_only_shows_requesters_own_editable_drafts(self):
        self.assertEqual(self.page(self.actor, tab="todo").context["total"], 1)
        requester = self.worker("draft_requester")
        other = self.worker("unrelated_draft_user")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(requester=requester)
        self.assertEqual(self.page(self.actor, tab="todo").context["total"], 0)
        self.assertEqual(self.page(self.approver, tab="todo").context["total"], 0)
        self.assertEqual(self.page(requester, tab="todo").context["total"], 0)
        self.grant(requester, "maintenanceorder", "change")
        self.assertEqual(self.page(requester, tab="todo").context["total"], 1)
        self.grant(other, "maintenanceorder", "change")
        self.assertEqual(self.page(other, tab="todo").context["total"], 0)
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(owner=other)
        self.assertEqual(self.page(other, tab="todo").context["total"], 0)
        self.assertEqual(self.page(requester, tab="todo").context["total"], 1)
        self.assertEqual(self.page(self.actor, tab="delegate").context["total"], 0)

    def test_withdrawn_draft_disappears_from_todo(self):
        self.assertEqual(self.page(tab="todo").context["total"], 1)
        services.withdraw_order(pk=self.order.pk, actor=self.actor)
        self.assertEqual(self.page(tab="todo").context["total"], 0)

    def test_draft_cancelled_and_terminated_do_not_show_pending_operations(self):
        response = self.page()
        self.assertContains(response, "编辑、签名并提交审批")
        self.assertIsNone(response.context["detail"]["current"])
        services.withdraw_order(pk=self.order.pk, actor=self.actor)
        response = self.page()
        self.assertContains(response, "已撤销")
        self.assertNotContains(response, 'class="oa-actions"')
        self.assertNotContains(response, "编辑、签名并提交审批")

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
        self.reach("order_review")
        first, second = self.worker("first"), self.worker("second")
        self.grant(first, "maintenancetask", "view")
        self.grant(second, "maintenancedocument", "add")
        self.grant(second, "maintenancedocument", "view")
        signatures.create_session(pk=self.current().pk, actor=second)
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
        self.assertNotContains(response, 'class="oa-actions"')
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
        test_maintenance.sign_draft(order, self.actor)
        services.start_order(pk=order.pk, actor=self.actor)
        services.terminate_order(pk=order.pk, actor=self.actor, comment="服务取消，无费用")
        response = self.page(selected=order.pk)
        self.assertEqual(response.context["detail"]["status_label"], "已终止")
        self.assertEqual(response.context["detail"]["future_nodes"], [])
        self.assertNotContains(response, 'class="oa-actions"')
        self.assertContains(response, "服务取消，无费用")

    def test_material_workspace_has_no_accordions_and_supports_native_links(self):
        self.reach("order_review")
        response = self.page(panel="material-order", pane="versions")
        self.assertNotContains(response, "<details")
        self.assertNotContains(response, "<summary")
        self.assertNotContains(response, "管理资料 →")
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
                "material": "notice",
                "state": "electronic",
                "note": "新付款通知书版本",
                "file": SimpleUploadedFile(
                    "notice.pdf", b"document", content_type="application/pdf"
                ),
                "return_query": "tab=done&sort=waiting",
                "return_panel": "material-notice",
            },
        )
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.url).query)
        self.assertEqual(query["panel"], ["material-notice"])
        self.assertEqual(query["tab"], ["done"])
        response = self.client.get(response.url)
        self.assertEqual(response.status_code, 200)
        material = next(
            item for item in response.context["detail"]["materials"] if item["key"] == "notice"
        )
        self.assertEqual(material["current_file"].original_name, "notice.pdf")
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

    def test_routing_requires_comment_and_displays_error_inside_dialog(self):
        self.start()
        target = self.worker("routing_target")
        task = self.current()

        for operation, comment in (("transfer", ""), ("countersign", "   ")):
            with self.subTest(operation=operation):
                response = self.action(operation, {
                    "user_id": target.pk, "comment": comment, "return_panel": "routing",
                })
                self.assertContains(
                    response, "请填写处理意见，最多 4000 字符。", count=1, status_code=400,
                )
                start = response.content.decode().split('id="routing"', 1)[1]
                panel = start.split("</dialog>", 1)[0]
                self.assertIn("data-oa-inline-errors", panel)
                self.assertIn('role="alert" tabindex="-1" autofocus', panel)
                self.assertIn("请填写处理意见，最多 4000 字符。", panel)
                self.assertEqual(
                    response.context["routing_form"]["user_id"].value(), str(target.pk),
                )
                self.assertFalse(self.order.events.filter(action=operation, task=task).exists())

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
        self.assertContains(response, 'aria-label="快捷办理"')
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

    def test_activity_pending_shows_assignee_and_waiting_consistent_with_main_panel(self):
        self.start()
        current = self.current()
        now = timezone.now()
        cases = (
            ("normal", now + timedelta(days=5), "", ""),
            ("warning", now + timedelta(hours=1), '<strong class="text-warning"> · 即将超时</strong>', " · 即将超时"),
            ("overdue", now - timedelta(hours=2), '<strong class="text-danger"> · 已超时</strong>', " · 已超时"),
        )
        for state, deadline, badge_html, badge_text in cases:
            with self.subTest(state=state):
                MaintenanceTask.objects.filter(pk=current.pk).update(
                    created=now - timedelta(hours=3),
                    deadline=deadline,
                )
                response = self.page()
                detail = response.context["detail"]
                self.assertEqual(detail["deadline_state"], state)
                html = response.content.decode()
                main_assignee = html.split('<footer class="oa-v2-assignee">', 1)[1].split("</footer>", 1)[0]
                audit_pending = html.split('<div class="oa-audit-pending">', 1)[1].split("</div>", 1)[0]
                expected_assignee = f"<span>办理人 / 组：{detail['waiting_for']}</span>"
                expected_waiting = f"<span>已等待 {detail['waiting']}{badge_html}</span>"
                self.assertIn(expected_assignee, main_assignee)
                self.assertIn(expected_waiting, main_assignee)
                self.assertIn(expected_assignee, audit_pending)
                self.assertIn(expected_waiting, audit_pending)
                elements = PanelMarkup(html).elements
                compact_pending = next(
                    attrs for tag, attrs in elements
                    if tag == "a" and "oa-audit-compact-pending" in attrs.get("class", "")
                )
                expected_label = (
                    f"{detail['current'].name} · 办理人 / 组：{detail['waiting_for']}"
                    f" · 已等待 {detail['waiting']}{badge_text}"
                )
                self.assertIn(expected_label, compact_pending["title"])
                self.assertEqual(compact_pending["aria-label"], expected_label)

    def test_compact_activity_labels_and_complete_hover_details(self):
        from netbox_oa.templatetags.oa_filters import activity_initial

        for name, expected in (
            ("工单复核", "复"), ("工单部门审核", "部"), ("工单公司审批", "司"),
            ("服务工单用印", "印"), ("上传用户后服务工单", "传"), ("验收申请", "申"),
            ("生成付款", "申"), ("付款", "款"), ("工单修正", "修"), ("发起流程", "申"),
        ):
            with self.subTest(name=name):
                self.assertEqual(activity_initial(name), expected)
        self.start()
        self.complete()
        event = MaintenanceEvent.objects.filter(order=self.order, action="complete").latest("pk")
        event.actor.first_name = "赵明"
        event.actor.last_name = ""
        event.actor.save()
        event.details = {"comment": "完整意见<保留>", "external_reference": "EIP-123"}
        event.save()
        for index in range(7):
            MaintenanceEvent.objects.create(
                order=self.order, actor=self.actor, action="transfer",
                description=f"转办记录 {index}",
            )
        response = self.page()
        elements = PanelMarkup(response.content.decode()).elements
        icons = [attrs for tag, attrs in elements if tag == "a" and "data-oa-event" in attrs]
        self.assertGreater(len(icons), 6)
        self.assertEqual(len(icons), len(response.context["detail"]["sidebar_events"]))
        target = next(attrs for attrs in icons if attrs["data-oa-event"] == str(event.pk))
        self.assertIn("完整意见<保留>", target["title"])
        self.assertIn("EIP-123", target["title"])
        item = next(item for item in response.context["detail"]["sidebar_events"]
                    if item["event"].pk == event.pk)
        self.assertEqual(item["actor_full_name"], "赵明")
        self.assertIn("赵明", target["title"])
        compact = response.content.decode().split('<nav class="oa-audit-icons"', 1)[1]
        compact = compact.split('</nav>', 1)[0]
        self.assertNotIn('oa-audit-symbol', compact)
        self.assertNotIn('actor_initial', compact)
        self.assertIn('oa-audit-icon-records', compact)
        self.assertContains(response, 'data-oa-audit-toggle', count=2)

    def test_full_activity_uses_horizontal_nodes_and_keeps_comments_and_event_targets(self):
        self.start()
        completed = self.current()
        self.complete()
        event = MaintenanceEvent.objects.get(order=self.order, task=completed, action="complete")
        comment = "已核对服务内容与合同。\n" + "办理意见完整保留。" * 30
        event.details.update(comment=comment, external_reference="EIP-2026-001")
        event.save()
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="请补充服务说明",
        )
        response = self.page(panel="activity")
        html = response.content.decode()
        timeline = html.split('<ol class="oa-record-timeline"', 1)[1].split("</ol>", 1)[0]
        expected = [str(item["event"].pk)
                    for item in response.context["detail"]["approval_events"]]
        self.assertEqual(re.findall(r'data-event-id="(\d+)"', timeline), expected)
        self.assertIn("oa-record-warning", timeline)
        details = timeline
        self.assertIn("请补充服务说明", details)
        self.assertIn("已核对服务内容与合同。<br>", details)
        self.assertIn("办理意见完整保留。" * 30, details)
        self.assertIn("外部单号：EIP-2026-001", details)
        self.assertEqual(timeline.count('class="oa-record-circle"'), len(expected))
        self.assertEqual(timeline.count('class="oa-record-detail"'), len(expected))
        self.assertNotIn("data-record-target", timeline)
        self.assertNotIn("data-record-panel", timeline)
        self.assertNotIn("hidden", timeline)
        self.assertNotIn("<a ", timeline)
        self.assertNotIn("完成：", timeline)
        record = timeline.split(f'data-event-id="{event.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("办理意见完整保留。" * 30, record)
        self.assertIn("外部单号：EIP-2026-001", record)
        self.assertIn(event.created.strftime("%Y"), record)
        self.assertNotIn("oa-clamp-two", timeline)
        self.assertNotIn("oa-audit-records", timeline)
        sidebar = html.split('<div class="oa-audit-scroll"', 1)[1].split("</ol>", 1)[0]
        self.assertIn("oa-audit-records", sidebar)
        self.assertNotIn("oa-record-timeline", sidebar)

    def test_horizontal_records_remove_system_descriptions_and_repeated_outcomes(self):
        self.start()
        task = self.current()
        self.complete()
        event = MaintenanceEvent.objects.get(order=self.order, task=task, action="complete")
        event.details["comment"] = "同意"
        event.save()
        response = self.page(panel="activity")
        timeline = response.content.decode().split('<ol class="oa-record-timeline"', 1)[1].split(
            "</ol>", 1
        )[0]
        record = timeline.split(f'data-event-id="{event.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertEqual(record.count("同意"), 1)
        self.assertNotIn("完成：", record)
        self.assertIn(task.name, record)
        initial = timeline.split('class="oa-record-node', 1)[1].split("</li>", 1)[0]
        self.assertEqual(initial.count("发起流程"), 1)
        self.assertNotIn("发起代维单位维护服务工单流程", initial)

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
        self.assertEqual(len(response.context["detail"]["events"]), 57)
        self.assertFalse(response.context["detail"]["has_earlier_events"])

    def test_approval_diagram_uses_snapshot_and_shows_actual_approver_and_time(self):
        self.start()
        first = self.current()
        self.approver.first_name = "审批人甲"
        self.approver.save()
        self.complete()
        first.refresh_from_db()
        response = self.page(panel="activity")
        diagram = response.context["detail"]["approval_diagram"]
        self.assertEqual(len(diagram["lanes"]), 3)
        reviewed = next(node for node in diagram["nodes"] if node["key"] == "order_review")
        self.assertEqual(reviewed["state"], "completed")
        self.assertEqual(reviewed["attempts"][0]["actor"], "审批人甲")
        self.assertEqual(reviewed["attempts"][0]["time"], first.completed_at)
        current = next(node for node in diagram["nodes"] if node["state"] == "current")
        self.assertEqual(current["key"], "order_final")
        self.assertContains(response, "工单审批经过流程图")
        self.assertContains(response, "审批人甲 · 已完成")
        nodes = re.findall(r'<g class="oa-flow-node[^\"]*">(.*?)</g>',
                           response.content.decode(), re.DOTALL)
        for markup in nodes:
            visible = re.findall(r'<text\b[^>]*>(.*?)</text>', markup, re.DOTALL)
            self.assertLessEqual(visible.count("已完成"), 1)
            self.assertLessEqual(visible.count("未办理"), 1)
            self.assertNotIn("审批人甲 · 已完成", visible)
            self.assertNotIn("尚未办理", visible)
        self.assertContains(response, "详细办理记录与意见")
        self.assertNotContains(response, "本工单启动时保存的流程快照")
        self.assertNotContains(response, "不代表已付款或已收款")
        dialog = response.content.decode().split('id="activity"', 1)[1].split("</dialog>", 1)[0]
        header, body = dialog.split("</header>", 1)
        for label in ("审批经过", self.order.number, "已完成", "当前环节", "已退回",
                      "未办理", "实线：顺序办理", "虚线：退回补正"):
            self.assertIn(label, header)
        self.assertIn('aria-label="审批经过图例"', header)
        self.assertNotIn('class="oa-flow-legend"', body)
        self.order.steps_snapshot[1]["name"] = "本单保存的初审名称"
        self.order.save()
        response = self.page(panel="activity")
        self.assertContains(response, "本单保存的初审名称")

    def test_approval_diagram_keeps_return_and_reapproval_attempts(self):
        self.start()
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="补齐资料",
        )
        services.update_order(pk=self.order.pk, actor=self.actor, requirements="补齐后的服务要求")
        self.complete()
        self.complete()
        response = self.page(panel="activity")
        diagram = response.context["detail"]["approval_diagram"]
        node = next(node for node in diagram["nodes"] if node["key"] == "order_review")
        self.assertEqual([attempt["status"] for attempt in node["attempts"]], ["已退回", "已完成"])
        self.assertTrue(all(attempt["time"] for attempt in node["attempts"]))
        self.assertEqual(node["height"], 94)
        single = next(item for item in diagram["nodes"] if len(item["attempts"]) == 1)
        self.assertEqual(single["height"], 64)
        self.assertLess(single["height"], node["height"])
        for item in diagram["nodes"]:
            for attempt in item["attempts"]:
                self.assertGreater(attempt["actor_y"], item["label_lines"][-1]["y"])
                self.assertLess(attempt["time_y"], item["bottom"])
            following = next((other for other in diagram["nodes"]
                              if other["stage"] == item["stage"] and other["y"] > item["y"]), None)
            if following:
                self.assertEqual(following["y"] - item["bottom"], 24)
        self.assertContains(response, "补齐资料")

    def test_approval_diagram_does_not_expose_restricted_task_decisions(self):
        self.start()
        self.actor.first_name = "不可见的审批人"
        self.actor.save()
        self.complete()
        viewer = self.worker("diagram_order_only")
        response = self.page(actor=viewer, panel="activity")
        diagram = response.context["detail"]["approval_diagram"]
        self.assertTrue(all(not node["attempts"] for node in diagram["nodes"]))
        svg = response.content.decode().split('id="oa-approval-flow-title"', 1)[1].split(
            "</svg>", 1
        )[0]
        self.assertNotIn("不可见的审批人", svg)

    def test_automatic_payment_history_uses_compact_comment(self):
        self.reach("payment_review")
        originals = {}
        for prefix in ("付款已提交", "付款已支付"):
            task = self.current()
            self.complete()
            event = self.order.events.get(task=task, action="complete")
            comment = (
                f"{prefix}，校验通过，自动流转。\n\n"
                "付款编号：Y-GD2611D14B\n付款金额：2200.00 元\n付款状态：已支付"
            )
            event.details = {**event.details, "comment": comment}
            event.save()
            originals[event.pk] = comment
        response = self.page(panel="activity")
        records = response.context["detail"]["approval_events"]
        for item in records:
            if item["event"].pk in originals:
                self.assertEqual(item["record_comment"], "自动批转")
                self.assertEqual(item["event"].details["comment"], originals[item["event"].pk])
        timeline = render_to_string(
            "netbox_oa/maintenance/inbox_activity_timeline.html", {"activity_records": records}
        )
        self.assertEqual(timeline.count("自动批转"), 2)
        self.assertNotIn("付款金额", timeline)
        # A payment handler's own opinion must remain visible in full.
        event.details["comment"] = "已核对付款金额，请保留银行回单。"
        event.save()
        response = self.page(panel="activity")
        item = next(item for item in response.context["detail"]["approval_events"]
                    if item["event"].pk == event.pk)
        self.assertEqual(item["record_comment"], event.details["comment"])

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

    def test_waiting_duration_matches_in_list_and_detail(self):
        self.start()
        now = timezone.now()
        MaintenanceTask.objects.filter(pk=self.current().pk).update(
            created=now - timedelta(hours=158, minutes=24, seconds=30),
            deadline=now - timedelta(days=1),
        )
        with patch("netbox_oa.maintenance_presentation.timezone.now", return_value=now):
            response = self.page()
        self.assertEqual(response.context["rows"][0]["waiting"], "6天14小时24分钟")
        self.assertEqual(response.context["detail"]["waiting"], "6天14小时24分钟")
        self.assertContains(response, "6天14小时24分钟", count=5)
