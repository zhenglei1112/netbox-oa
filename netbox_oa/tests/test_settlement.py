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
from netbox_oa.maintenance_payments import payment_context
from netbox_oa.models import MaintenanceOrder
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user
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





    def test_generation_and_approval_only_link_to_contract_details(self):
        self.reach("payment_plan")
        self.upload("notice")
        self.upload("invoice")
        self.client.force_login(self.actor)
        expected = reverse("plugins:netbox_contract:contract", args=[self.contract.pk])
        for name in ("生成付款", "付款部门审核"):
            self.assertEqual(self.current().name, name)
            self.client.force_login(acting_user(self))
            page = self.client.get(self.order.get_absolute_url())
            self.assertContains(page, f'href="{expected}">付款信息</a>', count=1)
            self.assertNotContains(page, 'name="contract_payment_id"')
            self.assertNotContains(page, "新增付款")
            self.assertNotContains(page, "修改所选付款")
            self.assertNotContains(page, "引用付款信息")
            self.assertEqual(urlparse(page.context["detail"]["payment_contract_url"]).query, "")
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(self.order_now().stage, "payment")
        self.assertFalse(Invoice.objects.exists())

    def test_external_payment_validation_does_not_block_oa_submission(self):
        self.reach("payment_plan")
        self.upload("notice")
        self.upload("invoice")
        self.new_payment(period_start=date(2026, 9, 30), period_end=date(2026, 9, 1))
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(self.order_now().stage, "payment")

    def test_payment_submission_and_approval_api_need_no_payment_fields(self):
        self.reach("payment_plan")
        self.upload("notice")
        self.upload("invoice")
        api = APIClient()
        api.force_authenticate(self.actor)
        for name in ("生成付款", "付款部门审核"):
            self.assertEqual(self.current().name, name)
            api.force_authenticate(acting_user(self))
            response = api.post(
                f"/api/maintenance/tasks/{self.current().pk}/complete/", {}, format="json"
            )
            self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.order_now().stage, "payment")

    def test_invoice_permissions_are_not_required_to_generate_and_approve(self):
        self.reach("payment_plan")
        self.upload("notice")
        self.upload("invoice")
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
        self.upload("notice")
        self.upload("invoice")
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
            pk=self.current().pk, actor=acting_user(self), comment="请核对合同内的付款信息"
        )
        self.assertEqual(self.current().name, "生成付款")
        self.assertEqual(self.order_now().stage, "payment")
        self.assertEqual(self.order_now().payment_status, "draft")
        self.assertFalse(self.current().evidence["correction_baseline"])
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(self.order_now().stage, "payment")

    def test_api_and_web_upload_need_no_period_fields(self):
        self.reach("acceptance_apply")
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
        self.assertContains(page, "上传服务验收申请单")
        upload_page = self.client.get(reverse(
            "plugins:netbox_oa:maintenance_material_upload", args=[self.order.pk, "settlement"]
        ))
        self.assertNotContains(upload_page, "退回补正后，请重新上传")
        self.upload("report", "scan")
        self.upload("acceptance")
        test_maintenance.sign_task(self.current(), self.actor)
        api = APIClient()
        api.force_authenticate(self.actor)
        response = api.post(
            f"/api/maintenance/tasks/{self.current().pk}/complete/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_payment_approval_can_read_attachments_but_cannot_change_them(self):
        self.reach("payment_plan")
        documents = [self.upload(material) for material in ("notice", "invoice")]
        self.client.force_login(acting_user(self))
        page = self.client.get(self.order.get_absolute_url())
        for material in ("notice", "invoice"):
            self.assertContains(page, f"panel=material-{material}&amp;pane=upload")
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(self.current().name, "付款部门审核")
        self.client.force_login(acting_user(self))
        page = self.client.get(self.order.get_absolute_url())
        for document in documents:
            material = document.material
            item = next(item for item in page.context["detail"]["materials"]
                        if item["key"] == material)
            self.assertEqual(item["current_file"], document)
            self.assertFalse(item["can_upload_now"])
            review = self.client.get(reverse("plugins:netbox_oa:maintenance_review",
                                             args=[self.order.pk]), {"material": material})
            self.assertContains(review, document.original_name)
            self.assertIn(document, review.context["selected_group"]["documents"])
            for state in ("electronic", "scan"):
                with self.assertRaisesMessage(ValidationError, "只能在生成付款环节上传"):
                    self.upload(material, state)
            with self.assertRaises(ValidationError):
                services.remove_document(pk=document.pk, actor=acting_user(self))
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(self.current().name, "付款")
        self.assertFalse(dict(self.order_now().steps_snapshot[self.current().position]["required"]))
