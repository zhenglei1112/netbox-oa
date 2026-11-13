
"""Acceptance form, supporting scans, signatures, stamping and payment handoff."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_oa import maintenance_services as services
from netbox_oa import maintenance_signatures as signatures
from netbox_oa.maintenance_flow import STAGES, build_steps
from netbox_oa.maintenance_materials import company_form_initial, service_effect_label
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user
from netbox_oa.tests.test_signatures import image_data


class AcceptanceTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    grant = test_maintenance.MaintenanceTests.grant
    worker = test_maintenance.MaintenanceTests.worker

    def application(self):
        self.start()
        while self.order.steps_snapshot[self.current().position]["key"] != "acceptance_apply":
            self.complete()

    def form(self, **values):
        return services.save_company_form(
            pk=self.order.pk, actor=self.actor, material="acceptance",
            data={**company_form_initial(self.order), "service_effect": "passed",
                  "settlement_amount": "1234.56", **values},
        )

    def apply(self):
        form = self.form()
        self.upload("report", "scan")
        self.upload("settlement", "scan")
        self.sign()
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        return form

    def sign(self):
        session = signatures.create_session(pk=self.current().pk, actor=acting_user(self))
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        return session

    def test_three_stages_and_six_acceptance_nodes(self):
        self.assertEqual([key for key, _ in STAGES], ["ordering", "acceptance", "payment"])
        nodes = [node for node in build_steps({}, 24) if node["stage"] == "acceptance"]
        self.assertEqual([node["name"] for node in nodes], ["验收申请", "验收复核", "验收部门审核",
                         "验收公司审批"])
        self.assertEqual([node["role"] for node in nodes], ["requester", "reviewer", "department",
                         "senior"])
        self.assertEqual([node["return_to"] for node in nodes[1:4]], ["acceptance_apply"] * 3)

    def test_application_requires_form_and_both_scan_types(self):
        self.application()
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.form()
        self.upload("report", "scan")
        with self.assertRaisesMessage(ValidationError, "服务结算单"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        scan = self.upload("settlement", "scan")
        self.assertFalse(scan.is_sealed)
        self.sign()
        task = services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(set(task.evidence), {"acceptance", "report", "settlement", "signature"})
        self.assertEqual(self.current().name, "验收复核")

    def test_amount_choices_and_readonly_basic_information(self):
        self.application()
        for values in ({"service_effect": "unknown"}, {"settlement_amount": "-1"},
                       {"settlement_amount": "NaN"}, {"settlement_amount": "10.001"}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.form(**values)
        document = self.form(supplier="伪造单位", service_effect="passed",
                             settlement_amount="0.10")
        self.assertEqual(document.form_data["supplier"], self.order.supplier)
        self.assertEqual(document.form_data["settlement_amount"], "0.10")
        self.assertEqual(document.form_data["service_effect"], "passed")

    def test_acceptance_result_choices_and_history(self):
        self.application()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_company_form",
                      args=[self.order.pk, "acceptance"])
        page = self.client.get(url)
        field = page.context["form"].fields["service_effect"]
        self.assertEqual(field.label, "验收结果")
        self.assertEqual(list(field.choices), [("", "请选择"), ("passed", "通过"),
                                              ("failed", "不通过")])
        for value, label in (("passed", "通过"), ("failed", "不通过")):
            document = self.form(service_effect=value)
            self.assertEqual(document.form_data["service_effect"], value)
            for route in ("maintenance_print", "maintenance_verify"):
                response = self.client.get(reverse(
                    f"plugins:netbox_oa:{route}", args=[document.pk],
                ))
                self.assertContains(response, "验收结果")
                self.assertEqual(response.context[
                    "acceptance_result" if route == "maintenance_verify" else "service_effect_label"
                ], label)
                self.assertNotContains(response, "服务效果")
        for value in ("excellent", "good", "qualified", "unqualified"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.form(service_effect=value)
        for value, label in (("excellent", "优"), ("good", "良"),
                             ("qualified", "合格"), ("unqualified", "不合格")):
            self.assertEqual(service_effect_label(value), label)

    def test_three_handwritten_signatures_then_only_acceptance_form_is_stamped(self):
        self.application()
        form = self.apply()
        for name in ("验收复核", "验收部门审核"):
            self.assertEqual(self.current().name, name)
            with self.assertRaisesMessage(ValidationError, "手写签名"):
                services.complete_task(pk=self.current().pk, actor=acting_user(self))
            self.assertEqual(services.quick_complete_comment(self.current()), "")
            session = self.sign()
            self.assertEqual(session.document_id, form.pk)
            task = services.complete_task(pk=self.current().pk, actor=acting_user(self))
            self.assertEqual(task.evidence["signature"]["image_id"], session.pk)
            self.assertEqual(task.evidence["signature"]["date"], timezone.localdate().isoformat())
        self.assertEqual(self.current().name, "验收公司审批")
        self.assertFalse(self.current().signatures.exists())
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        form.refresh_from_db()
        self.assertTrue(form.electronic_seal)
        self.assertEqual(form.electronic_seal["date"], timezone.localdate().isoformat())
        with self.assertRaisesMessage(ValidationError, "仅保留系统电子件"):
            self.upload("acceptance", "scan", sealed=True)
        self.client.force_login(self.actor)
        review = self.client.get(reverse("plugins:netbox_oa:maintenance_review",
                                         args=[self.order.pk]), {"material": "acceptance"})
        files = review.context["selected_group"]["index_rows"][0]["files"]
        self.assertEqual([item["document"] for item in files], [form])
        self.assertEqual(self.current().name, "生成付款")
        self.assertFalse(self.order.documents.filter(material__in=["report", "settlement"],
                                                     is_sealed=True).exists())
        self.complete()
        self.complete()
        self.assertEqual(self.current().name, "付款")

    def test_reviewer_uses_their_own_signature_on_the_acceptance_form(self):
        self.application()
        form = self.apply()
        reviewer = self.worker("acceptance_reviewer")
        self.grant(reviewer, "maintenancedocument", "view")
        with self.assertRaisesMessage(ValidationError, "手写签名"):
            services.complete_task(pk=self.current().pk, actor=reviewer)
        session = signatures.create_session(pk=self.current().pk, actor=reviewer)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        task = services.complete_task(pk=self.current().pk, actor=reviewer)
        self.assertEqual(session.document_id, form.pk)
        self.assertEqual(task.evidence["signature"]["user_id"], reviewer.pk)
        self.assertNotEqual(task.evidence["signature"]["user_id"], self.order.requester_id)

    def test_return_requires_revised_form_and_scans_and_does_not_reuse_signatures(self):
        self.application()
        original = self.apply()
        self.sign()
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="请修订金额和附件",
        )
        self.assertEqual(self.current().name, "验收申请")
        revised = self.form(settlement_amount="2234.56")
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.upload("report", "scan")
        self.upload("settlement", "scan")
        self.sign()
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.client.force_login(self.actor)
        old = self.client.get(reverse("plugins:netbox_oa:maintenance_print", args=[original.pk]))
        new = self.client.get(reverse("plugins:netbox_oa:maintenance_print", args=[revised.pk]))
        self.assertIn("acceptance_review", old.context["signatures"])
        self.assertEqual(set(new.context["signatures"]), {"acceptance_apply"})
        with self.assertRaisesMessage(ValidationError, "手写签名"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        for _ in range(2):
            self.sign()
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        original.refresh_from_db()
        revised.refresh_from_db()
        self.assertFalse(original.electronic_seal)
        self.assertTrue(revised.electronic_seal)
        with self.assertRaisesMessage(ValidationError, "仅保留系统电子件"):
            services.upload_document(pk=self.order.pk, actor=self.actor, material="acceptance",
                                     state="scan", source_document_id=original.pk,
                                     file=SimpleUploadedFile("wrong.pdf", b"scan"))

    def test_print_has_codes_three_signature_dates_and_public_verification(self):
        self.application()
        form = self.apply()
        for _ in range(2):
            self.sign()
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.client.force_login(self.actor)
        page = self.client.get(reverse("plugins:netbox_oa:maintenance_print", args=[form.pk]))
        for text in ("工单服务验收单", "验收结果", "通过", "￥1,234.56", "复核人", "部门审核",
                     "验收人"):
            self.assertContains(page, text)
        for text in ("服务要求", "申请人", "部门审核人"):
            self.assertNotContains(page, text)
        self.assertEqual(len(page.context["signatures"]), 3)
        from netbox_oa.templatetags.oa_filters import chinese_date

        self.assertContains(page, chinese_date(page.context["requested_at"]))
        for signature in page.context["signatures"].values():
            self.assertContains(page, chinese_date(signature["date"]))
            self.assertNotContains(page, signature["date"])
        self.assertContains(page, "data:image/svg+xml;base64,", count=2)
        verification = reverse("plugins:netbox_oa:maintenance_verify", args=[form.pk])
        self.assertContains(page, verification)
        self.assertEqual(self.client.get(verification).status_code, 200)
        self.client.logout()
        self.assertContains(self.client.get(verification), self.order.number)

    def test_missing_service_effect_has_a_field_specific_message(self):
        self.application()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_company_form",
                      args=[self.order.pk, "acceptance"])
        page = self.client.get(url)
        self.assertContains(page, 'data-required-message="请选择验收结果。"')
        page = self.client.post(url, {**company_form_initial(self.order),
                                      "settlement_amount": "1234.56", "service_effect": ""})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["form"].errors["service_effect"], ["请选择验收结果。"])

    def test_upload_and_edit_entrypoints_follow_nodes_and_freeze_approval_evidence(self):
        self.application()
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenance_company_form",
                      args=[self.order.pk, "acceptance"])
        page = self.client.get(url)
        self.assertContains(page, "结算金额")
        self.assertTemplateUsed(page, "generic/object_edit.html")
        self.assertContains(page, 'class="object-edit mt-5"')
        self.assertContains(page, 'class="btn btn-outline-secondary btn-float"')
        self.assertNotContains(page, 'class="card oa-company-editor"')
        control = page.content.decode().split('data-settlement-amount-control>', 1)[1].split(
            '</div>', 1
        )[0]
        self.assertIn('name="settlement_amount"', control)
        self.assertIn(
            'class="btn d-inline-flex align-items-center justify-content-center"', control
        )
        self.assertIn('mdi mdi-calculator icon m-0 d-inline-flex', control)
        self.assertIn('aria-label="维护服务结算单计费明细"', control)
        self.assertIn('维护服务结算单计费明细', control)
        self.assertContains(page, 'aria-label="维护服务结算单计费明细"', count=1)
        self.apply()
        with self.assertRaises(ValidationError):
            self.form()
        for material in ("report", "settlement"):
            with self.assertRaisesMessage(ValidationError, "审批期间不能修改"):
                self.upload(material, "scan")
            upload_url = reverse("plugins:netbox_oa:maintenance_material_upload",
                                 args=[self.order.pk, material])
            self.assertEqual(self.client.get(upload_url).status_code, 403)
        other = self.worker("acceptance_other")
        other.groups.clear()
        with self.assertRaises(PermissionDenied):
            services.complete_task(pk=self.current().pk, actor=other)
