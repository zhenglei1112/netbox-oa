from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_flow import build_steps
from netbox_oa.maintenance_payments import payment_context
from netbox_oa.models import MaintenanceOrder
from netbox_oa.tests import test_maintenance
from tests.support.contracts.models import Invoice


class SettlementTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    reach = test_maintenance.MaintenanceTests.reach
    worker = test_maintenance.MaintenanceTests.worker

    def order_now(self):
        return MaintenanceOrder.objects.get(pk=self.order.pk)

    def new_payment(self, **values):
        order = self.order_now()
        fields = dict(
            number=order.number,
            date=date(2026, 9, 1),
            status="draft",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            amount=Decimal("8860.00"),
            currency="CNY",
        )
        fields.update(values)
        payment = Invoice.objects.create(**fields)
        payment.contracts.add(order.contract_id)
        return payment

    def test_settlement_has_four_nodes_and_payment_has_three(self):
        nodes = [node for node in build_steps({}, 24) if node["stage"] == "settlement"]
        self.assertEqual(
            [node["name"] for node in nodes],
            [
                "服务结算单",
                "服务结算单用印部门审核",
                "服务结算单用印公司审批",
                "服务结算单用印",
            ],
        )
        self.assertEqual(
            [node["role"] for node in nodes],
            [
                "requester",
                "department",
                "senior",
                "seal",
            ],
        )

        payment_nodes = [node for node in build_steps({}, 24) if node["stage"] == "payment"]
        self.assertEqual(
            [node["name"] for node in payment_nodes], ["生成付款", "付款部门审核", "付款"]
        )
        self.assertEqual(
            [node["role"] for node in payment_nodes], ["requester", "department", "owner"]
        )

    def test_existing_instance_group_migration_preserves_progress_and_rules(self):
        from copy import deepcopy
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps

        self.reach("payment_review")
        order = self.order_now()
        task = self.current()
        for node in order.steps_snapshot:
            if node["key"] in {"payment_plan", "payment_review"}:
                node["stage"] = "settlement"
        order.stage = "settlement"
        order.save()
        expected = deepcopy(order.steps_snapshot)
        for node in expected:
            if node["key"] in {"payment_plan", "payment_review"}:
                node["stage"] = "payment"
        migration = import_module("netbox_oa.migrations.0016_payment_step_group")
        migration.forwards(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        order.refresh_from_db()
        self.assertEqual(order.steps_snapshot, expected)
        self.assertEqual(order.stage, "payment")
        self.assertEqual(order.current_position, task.position)
        self.assertEqual(self.current().pk, task.pk)
        self.assertEqual(self.current().status, "pending")
        services.return_task(pk=task.pk, actor=self.actor, comment="请核对付款")
        self.assertEqual(self.order_now().stage, "payment")
        self.assertEqual(self.order_now().payment_status, "draft")
        self.assertEqual(self.current().name, "生成付款")
        self.assertFalse(self.current().evidence["correction_baseline"])
        self.complete()
        self.complete()
        self.assertEqual(self.current().name, "付款")
        self.assertEqual(self.order_now().payment_status, "submitted")

    def test_scanned_settlement_is_required_but_period_is_not(self):
        self.reach("settlement_fill")
        with self.assertRaisesMessage(ValidationError, "扫描版服务结算单"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "必须上传"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="settlement",
                file=SimpleUploadedFile("settlement.pdf", b"electronic"),
            )
        self.upload("settlement")
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertIsNone(self.order_now().period_start)
        self.assertIsNone(self.order_now().period_end)
        self.assertNotIn("settlement_period", task.evidence)
        self.assertEqual(self.current().name, "服务结算单用印部门审核")

    def test_upload_ignores_period_parameters(self):
        self.reach("settlement_fill")
        self.upload("settlement")
        services.complete_task(
            pk=self.current().pk,
            actor=self.actor,
            settlement_period_mode="month",
            period_start="2026-07",
            period_end="2026-09",
        )
        self.assertIsNone(self.order_now().period_start)
        self.assertIsNone(self.order_now().period_end)

    def test_settlement_return_requires_new_scan_and_reapproval(self):
        self.reach("settlement_department_seal")
        with self.assertRaisesMessage(ValidationError, "审批期间不能修改"):
            self.upload("settlement", "paper")
        services.return_task(pk=self.current().pk, actor=self.actor, comment="补全结算单")
        with self.assertRaises(ValidationError):
            services.complete_task(
                pk=self.current().pk,
                actor=self.actor,
                period_start="2026-08-01",
                period_end="2026-08-31",
            )
        self.upload("settlement")
        self.complete()
        self.assertEqual(self.current().name, "服务结算单用印部门审核")

    def test_seal_requires_matching_scanned_original(self):
        self.reach("settlement_seal")
        with self.assertRaisesMessage(ValidationError, "用印扫描件"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "必须对应本次审批"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="settlement",
                state="scan",
                source_document_id=999999,
                file=SimpleUploadedFile("sealed.pdf", b"scan"),
            )
        self.complete()
        self.assertEqual(self.current().name, "生成付款")

    def test_generation_and_approval_only_link_to_contract_details(self):
        self.reach("payment_plan")
        self.client.force_login(self.actor)
        expected = reverse("plugins:netbox_contract:contract", args=[self.contract.pk])
        for name in ("生成付款", "付款部门审核"):
            self.assertEqual(self.current().name, name)
            page = self.client.get(self.order.get_absolute_url())
            self.assertContains(page, f'href="{expected}">付款信息</a>', count=1)
            self.assertNotContains(page, 'name="contract_payment_id"')
            self.assertNotContains(page, "新增付款")
            self.assertNotContains(page, "修改所选付款")
            self.assertNotContains(page, "引用付款信息")
            self.assertEqual(urlparse(page.context["detail"]["payment_contract_url"]).query, "")
            services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(self.order_now().stage, "payment")
        self.assertFalse(Invoice.objects.exists())
        self.assertIsNone(self.order_now().contract_payment_id)
        self.assertIsNone(self.order_now().payment_amount)
        self.assertIsNone(self.order_now().period_start)

    def test_external_payment_validation_does_not_block_oa_submission(self):
        self.reach("payment_plan")
        self.new_payment(period_start=date(2026, 9, 30), period_end=date(2026, 9, 1))
        services.complete_task(pk=self.current().pk, actor=self.actor)
        services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(self.order_now().stage, "payment")

    def test_payment_submission_and_approval_api_need_no_payment_fields(self):
        self.reach("payment_plan")
        api = APIClient()
        api.force_authenticate(self.actor)
        for name in ("生成付款", "付款部门审核"):
            self.assertEqual(self.current().name, name)
            response = api.post(
                f"/api/maintenance/tasks/{self.current().pk}/complete/", {}, format="json"
            )
            self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.order_now().stage, "payment")

    def test_invoice_permissions_are_not_required_to_generate_and_approve(self):
        self.reach("payment_plan")
        actor = self.worker("contract_payment_handler")
        self.grant(actor, "maintenancetask", "view")
        actor.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="netbox_contract", codename="view_contract"
            )
        )
        services.transfer_task(
            pk=self.current().pk, actor=self.actor, user_id=actor.pk, comment="交办"
        )
        self.assertFalse(actor.has_perm("netbox_contract.view_invoice"))
        self.assertFalse(actor.has_perm("netbox_contract.add_invoice"))
        self.client.force_login(actor)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, ">付款信息</a>", count=1)
        services.complete_task(pk=self.current().pk, actor=actor)
        services.complete_task(pk=self.current().pk, actor=actor)
        self.assertEqual(self.order_now().stage, "payment")

    def test_contract_information_link_respects_view_permission(self):
        self.reach("payment_plan")
        reader = self.worker("hidden_payment_contract")
        self.grant(reader, "maintenancetask", "view")
        services.transfer_task(
            pk=self.current().pk, actor=self.actor, user_id=reader.pk, comment="交办"
        )
        data = payment_context(self.order_now(), reader)
        self.assertFalse(data["payment_contract_url"])
        self.assertIn("没有查看权限", data["payment_contract_error"])
        self.client.force_login(reader)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, 'type="button" disabled>付款信息</button>')
        expected = reverse("plugins:netbox_contract:contract", args=[self.contract.pk])
        self.assertNotContains(page, f'href="{expected}"')

    def test_return_to_generation_does_not_require_revising_documents_or_linking_payment(self):
        self.reach("payment_review")
        services.return_task(
            pk=self.current().pk, actor=self.actor, comment="请核对合同内的付款信息"
        )
        self.assertEqual(self.current().name, "生成付款")
        self.assertEqual(self.order_now().stage, "payment")
        self.assertEqual(self.order_now().payment_status, "draft")
        self.assertFalse(self.current().evidence["correction_baseline"])
        services.complete_task(pk=self.current().pk, actor=self.actor)
        services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(self.order_now().stage, "payment")

    def test_old_payment_snapshot_is_preserved_without_revalidation(self):
        self.reach("payment_review")
        payment = self.new_payment()
        order = self.order_now()
        order.contract_payment_id = payment.pk
        order.contract_payment_snapshot = {"id": payment.pk, "amount": "8000.00"}
        order.save()
        payment.amount = Decimal("9000.00")
        payment.save()
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertNotIn("contract_payment", task.evidence)
        self.assertEqual(self.order_now().contract_payment_snapshot["amount"], "8000.00")

    def test_api_and_web_upload_need_no_period_fields(self):
        self.reach("settlement_fill")
        self.client.force_login(self.actor)
        self.client.post(
            reverse("plugins:netbox_oa:maintenance_upload", args=[self.order.pk]),
            {
                "scan_material": "settlement",
                "file": SimpleUploadedFile("settlement.png", b"scan"),
            },
        )
        self.assertTrue(self.order.documents.filter(material="settlement", kind="scan").exists())
        page = self.client.get(self.order.get_absolute_url())
        self.assertNotContains(page, 'name="settlement_period_mode"')
        self.assertNotContains(page, 'name="period_start"')
        self.assertNotContains(page, 'name="period_end"')
        self.assertContains(page, "上传服务结算单")
        self.assertContains(page, "上传扫描版服务结算单")
        api = APIClient()
        api.force_authenticate(self.actor)
        response = api.post(
            f"/api/maintenance/tasks/{self.current().pk}/complete/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.order_now().period_end)

    def test_historical_settlement_snapshot_keeps_electronic_submission(self):
        self.reach("settlement_fill")
        order = self.order_now()
        node = order.steps_snapshot[order.current_position]
        node.pop("scan_material")
        node.pop("contract_payment")
        node["required"] = [["settlement", "electronic"]]
        order.save()
        document = services.upload_document(
            pk=order.pk,
            actor=self.actor,
            material="settlement",
            file=SimpleUploadedFile("legacy.xlsx", b"data"),
        )
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(task.evidence["settlement"], document.pk)
