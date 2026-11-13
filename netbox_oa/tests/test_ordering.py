from datetime import date
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from users.models import Group

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_contracts import selected_contract
from netbox_oa.maintenance_flow import ROLES
from netbox_oa.maintenance_forms import MaintenanceOrderEditForm, MaintenanceOrderForm
from netbox_oa.maintenance_numbering import SALT, issue_number, number_from_token
from netbox_oa.models import MaintenanceNumberReservation, MaintenanceOrder
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user
from tests.support.contracts.models import Contract
from tests.support.dcim.models import Region


class OrderingTests(TestCase):
    def setUp(self):
        test_maintenance.MaintenanceTests.setUp(self)
        self.extra_regions = [Region.objects.create(name=name) for name in ("广西", "南宁", "桂林")]

    tearDown = test_maintenance.MaintenanceTests.tearDown
    grant = test_maintenance.MaintenanceTests.grant
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    reach = test_maintenance.MaintenanceTests.reach
    worker = test_maintenance.MaintenanceTests.worker

    def create_data(self):
        return {
            "number_token": issue_number(self.actor)[1],
            "service_type": "fiber",
            "contract_id": self.contract.pk,
            "service_region": [region.pk for region in self.regions],
            "service_location": "惠州站点",
            "requirements": "维护线路",
        }

    def test_creating_order_generates_form_and_can_submit_after_signing(self):
        order = services.create_order(actor=self.actor, policy=self.policy, **self.create_data())
        document = order.documents.get(material="order", kind="form")
        self.assertEqual(document.form_data["number"], order.number)
        self.assertEqual(document.form_data["requirements"], order.requirements)
        self.assertEqual(document.form_data["supplier"], order.supplier)
        self.assertEqual(document.form_data["service_region"], order.service_region)
        test_maintenance.sign_draft(order, self.actor)
        services.start_order(pk=order.pk, actor=self.actor)
        task = order.tasks.get(status="pending")
        self.assertEqual(task.name, "工单复核")
        self.assertEqual(order.documents.count(), 1)

    def test_only_enabled_policy_is_selected_by_default(self):
        form = MaintenanceOrderForm(actor=self.actor)
        self.assertEqual(form["policy"].value(), self.policy.pk)

    def test_explicit_default_is_selected_and_replaces_previous_default(self):
        groups = {role: self.group.pk for role, _ in ROLES}
        first = services.save_policy(
            actor=self.actor,
            pk=self.policy.pk,
            name=self.policy.name,
            groups=groups,
            is_default=True,
        )
        second = services.save_policy(
            actor=self.actor,
            name="Another policy",
            groups=groups,
            is_default=True,
        )
        first.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        self.assertEqual(MaintenanceOrderForm(actor=self.actor)["policy"].value(), second.pk)
        form = MaintenanceOrderForm(actor=self.actor, initial={"policy": first.pk})
        self.assertEqual(form["policy"].value(), first.pk)
        form = MaintenanceOrderForm(
            {**self.create_data(), "policy": first.pk, "priority": "medium"}, actor=self.actor
        )
        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(form.cleaned_data["policy"].pk, first.pk)

    def test_multiple_policies_without_default_require_manual_selection(self):
        services.save_policy(
            actor=self.actor,
            name="Another policy",
            groups={role: self.group.pk for role, _ in ROLES},
        )
        self.assertIsNone(MaintenanceOrderForm(actor=self.actor)["policy"].value())

    def test_disabled_or_invisible_default_is_not_selected(self):
        self.policy.is_default = True
        self.policy.save()
        reader = self.worker("policy_reader")
        self.assertIsNone(MaintenanceOrderForm(actor=reader)["policy"].value())
        with self.assertRaisesMessage(ValidationError, "必须启用"):
            services.save_policy(
                actor=self.actor,
                pk=self.policy.pk,
                name=self.policy.name,
                groups={role: self.group.pk for role, _ in ROLES},
                enabled=False,
            )

    def test_policy_page_can_set_default(self):
        self.client.force_login(self.actor)
        response = self.client.post(
            self.policy.get_absolute_url(),
            {
                **{role: self.group.pk for role, _ in ROLES},
                "name": self.policy.name,
                "enabled": "on",
                "is_default": "on",
                "sla_hours": 24,
                "warning_hours": 4,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.is_default)
        page = self.client.get(self.policy.get_absolute_url())
        self.assertTrue(page.context["form"]["is_default"].value())

    def test_concurrent_default_conflict_returns_validation_error(self):
        count = self.policy.__class__.objects.count()
        with patch("netbox_oa.maintenance_services._save", side_effect=IntegrityError("conflict")):
            with self.assertRaisesMessage(ValidationError, "刷新页面后重试"):
                services.save_policy(
                    actor=self.actor,
                    name="Concurrent default",
                    is_default=True,
                    groups={role: self.group.pk for role, _ in ROLES},
                )
        self.assertEqual(self.policy.__class__.objects.count(), count)

    def configure_ordering(self):
        groups = {role: self.group.pk for role, _ in ROLES}
        groups["reviewer"] = Group.objects.create(name="Configured initial reviewers").pk
        groups["department"] = Group.objects.create(name="Configured department reviewers").pk
        services.save_policy(
            actor=self.actor, pk=self.policy.pk, name=self.policy.name, groups=groups
        )
        return groups

    def test_current_ordering_nodes_and_configured_group_approvals_with_signed_print(self):
        groups = self.configure_ordering()
        self.start()
        self.assertEqual(
            [step["name"] for step in self.order.steps_snapshot if step["stage"] == "ordering"],
            [
                "工单修正",
                "工单复核",
                "工单部门审核",
                "工单公司审批",
            ],
        )
        self.assertIsNone(self.current().assigned_user_id)
        document = self.order.documents.get(material="order", kind="form")
        for role, name in (("reviewer", "reviewer_a"), ("department", "reviewer_b")):
            task = self.current()
            reviewer = get_user_model().objects.create_user(username=name)
            reviewer.groups.add(groups[role])
            self.grant(reviewer, "maintenancetask", "change")
            self.grant(reviewer, "maintenanceorder", "view")
            self.assertEqual(task.assigned_group_id, groups[role])
            self.assertIsNone(task.assigned_user_id)
            self.grant(reviewer, "maintenancedocument", "view")
            signature = test_maintenance.sign_task(task, reviewer)
            services.complete_task(pk=task.pk, actor=reviewer)
            task.refresh_from_db()
            self.assertEqual(
                task.evidence["signature"],
                {
                    "image_id": signature.pk,
                    "name": name,
                    "user_id": reviewer.pk,
                    "date": timezone.localdate().isoformat(),
                },
            )
        self.order.refresh_from_db()
        self.assertEqual(self.current().name, "工单公司审批")
        self.client.force_login(self.actor)
        printed = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        )
        self.assertEqual(printed.context["signatures"]["order_review"]["name"], "reviewer_a")
        self.assertEqual(printed.context["signatures"]["order_final"]["name"], "reviewer_b")
        self.assertContains(printed, "reviewer_a的手写签名")
        self.assertContains(printed, "reviewer_b的手写签名")

    def test_members_outside_configured_group_cannot_approve(self):
        self.configure_ordering()
        self.reach("order_review")
        other = self.worker("other_group_member")
        with self.assertRaises(PermissionDenied):
            services.complete_task(pk=self.current().pk, actor=other)

    def test_duplicate_names_do_not_affect_automatic_assignment(self):
        first, second = self.worker("first_reviewer"), self.worker("second_reviewer")
        for reviewer in (first, second):
            reviewer.first_name = "同名审核员"
            reviewer.save()
        self.reach("order_review")
        self.assertIsNone(self.current().assigned_user_id)
        self.grant(first, "maintenancedocument", "view")
        test_maintenance.sign_task(self.current(), first)
        task = services.complete_task(pk=self.current().pk, actor=first)
        self.assertEqual(task.assigned_user_id, first.pk)
        self.assertIsNotNone(task.started_at)
        with self.assertRaises(ValidationError):
            services.complete_task(pk=task.pk, actor=second)
        self.assertEqual(task.evidence["signature"]["user_id"], first.pk)

    def test_updated_group_configuration_only_applies_to_future_orders(self):
        groups = self.configure_ordering()
        self.reach("order_review")
        replacement = Group.objects.create(name="Replacement reviewers")
        updated = {**groups, "reviewer": replacement.pk, "department": replacement.pk}
        services.save_policy(
            actor=self.actor, pk=self.policy.pk, name=self.policy.name, groups=updated
        )
        self.order.refresh_from_db()
        self.assertEqual(self.current().assigned_group_id, groups["reviewer"])
        self.assertEqual(self.order.steps_snapshot[2]["group_id"], groups["department"])
        new_order = services.create_order(
            actor=self.actor, policy=self.policy, **self.create_data()
        )
        test_maintenance.sign_draft(new_order, self.actor)
        new_order = services.start_order(pk=new_order.pk, actor=self.actor)
        self.assertEqual(new_order.steps_snapshot[1]["group_id"], replacement.pk)
        self.assertEqual(new_order.steps_snapshot[2]["group_id"], replacement.pk)

    def test_policy_page_saves_approval_groups_and_flow_preview_uses_them(self):
        groups = self.configure_ordering()
        self.client.force_login(self.actor)
        url = self.policy.get_absolute_url()
        page = self.client.get(url)
        self.assertEqual(page.context["form"]["reviewer"].value(), groups["reviewer"])
        replacement = Group.objects.create(name="Chosen reviewers")
        data = {
            **groups,
            "reviewer": replacement.pk,
            "department": replacement.pk,
            "name": self.policy.name,
            "enabled": "on",
            "sla_hours": 24,
            "warning_hours": 4,
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            dict(self.policy.roles.values_list("role", "group_id")),
            {**groups, "reviewer": replacement.pk, "department": replacement.pk},
        )
        preview = self.client.get(
            reverse("plugins:netbox_oa:maintenance_flow_definition"), {"policy": self.policy.pk}
        )
        nodes = preview.context["stages"][0]["nodes"]
        self.assertEqual(nodes[0]["group"].pk, replacement.pk)
        self.assertEqual(nodes[1]["group"].pk, replacement.pk)
        self.assertEqual(nodes[0]["role_label"], "复核人")
        self.assertContains(preview, "复核人")
        self.assertNotContains(preview, "初审人")
        self.assertEqual(nodes[1]["role_label"], "部门负责人")

    def test_contract_keeps_number_and_supplier_but_allows_manual_region(self):
        order = services.create_order(
            actor=self.actor,
            policy=self.policy,
            **{**self.create_data(), "service_region": [r.pk for r in self.extra_regions[:2]]},
            supplier="伪造单位",
            contract_number="伪造合同",
        )
        self.assertEqual(order.contract_number, self.contract.number)
        self.assertEqual(order.supplier, self.provider.name)
        self.assertEqual(order.service_region, "广西 / 南宁")
        self.assertEqual(order.contract_id, self.contract.pk)

    def test_contract_form_and_create_page(self):
        form = MaintenanceOrderForm(
            {**self.create_data(), "policy": self.policy.pk, "priority": "medium"},
            actor=self.actor,
        )
        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertEqual(form.cleaned_data["service_region"], "广东 / 惠州")
        self.assertEqual(form.cleaned_data["service_region_ids"], [r.pk for r in self.regions])
        self.assertTrue(form.fields["service_region"].widget.allow_multiple_selected)
        self.assertEqual(form.fields["service_region"].queryset.model._meta.label, "dcim.Region")
        self.assertNotIn("owner", form.fields)
        self.assertNotIn("title", form.fields)
        self.assertTrue(form.fields["number"].disabled)
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        page = self.client.get(url)
        self.assertTemplateUsed(page, "generic/object_edit.html")
        self.assertTemplateUsed(page, "form_helpers/render_field.html")
        self.assertContains(page, "oa-contract-options")
        self.assertContains(page, "maintenance-contract.js")
        response = self.client.post(url, form.data)
        self.assertEqual(response.status_code, 302)

    def test_framework_contract_without_regions_can_create_and_start_with_manual_region(self):
        self.contract.custom_field_data = {}
        self.contract.save()
        self.client.force_login(self.actor)
        data = {
            **self.create_data(),
            "policy": self.policy.pk,
            "priority": "medium",
            "service_region": [r.pk for r in self.extra_regions[:2]],
        }
        form = MaintenanceOrderForm(data, actor=self.actor)
        self.assertTrue(form.is_valid(), form.errors.as_text())
        self.assertNotIn("readonly", form.fields["service_region"].widget.attrs)
        self.assertTrue(form.fields["supplier"].widget.attrs["readonly"])
        response = self.client.post(reverse("plugins:netbox_oa:maintenanceorder_add"), data)
        self.assertEqual(response.status_code, 302)
        order = MaintenanceOrder.objects.exclude(pk=self.order.pk).get()
        self.assertEqual(set(order.service_region.split(" / ")), {"广西", "南宁"})
        self.assertEqual(
            order.documents.get(kind="form").form_data["service_region"], order.service_region
        )
        test_maintenance.sign_draft(order, self.actor)
        services.start_order(pk=order.pk, actor=self.actor)
        self.assertEqual(order.tasks.get(status="pending").name, "工单复核")

    def test_framework_contract_without_manual_region_has_field_error(self):
        self.contract.custom_field_data = {}
        self.contract.save()
        data = {
            **self.create_data(),
            "policy": self.policy.pk,
            "priority": "medium",
            "service_region": [],
        }
        form = MaintenanceOrderForm(data, actor=self.actor)
        self.assertFalse(form.is_valid())
        self.assertIn("service_region", form.errors)
        with self.assertRaises(ValidationError):
            services.create_order(
                actor=self.actor, policy=self.policy, **{**self.create_data(), "service_region": []}
            )

    def test_manual_region_survives_order_and_company_form_edits(self):
        self.upload("order")
        services.update_order(pk=self.order.pk, actor=self.actor, service_region="广西 / 南宁")
        services.update_order(pk=self.order.pk, actor=self.actor, supplier="ignored")
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(order.service_region, "广西 / 南宁")
        self.assertEqual(order.supplier, self.provider.name)
        document = order.documents.filter(kind="form").first()
        values = {
            **document.form_data,
            "service_region": [self.extra_regions[0].pk, self.extra_regions[2].pk],
        }
        revised = services.save_company_form(
            pk=order.pk, actor=self.actor, material="order", data=values
        )
        self.assertEqual(revised.form_data["service_region"], "广西 / 桂林")
        self.assertEqual(MaintenanceOrder.objects.get(pk=order.pk).service_region, "广西 / 桂林")

    def test_api_allows_framework_contract_manual_region(self):
        self.contract.custom_field_data = {}
        self.contract.save()
        client = APIClient()
        client.force_authenticate(self.actor)
        payload = {
            **self.create_data(),
            "policy": self.policy.pk,
            "owner": self.actor.pk,
            "service_region_ids": [self.extra_regions[0].pk],
        }
        response = client.post("/api/maintenance/orders/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["service_region"], "广西")
        self.assertEqual(response.data["service_region_ids"], [self.extra_regions[0].pk])
        self.assertEqual(response.data["contract_number"], self.contract.number)

    def test_region_selector_rejects_text_nonexistent_and_hidden_regions(self):
        for value in ("江苏", [999999], []):
            form = MaintenanceOrderForm(
                {**self.create_data(), "policy": self.policy.pk, "service_region": value},
                actor=self.actor,
            )
            self.assertFalse(form.is_valid())
            self.assertIn("service_region", form.errors)
        reader = self.worker("region_selector")
        with self.assertRaises(ValidationError):
            selected_contract(reader, self.contract.pk, service_region_ids=[self.regions[0].pk])
        form = MaintenanceOrderForm(actor=reader)
        self.assertFalse(form.fields["service_region"].queryset.exists())

    def test_region_ids_survive_rename_and_legacy_snapshot_can_be_selected(self):
        region = self.regions[0]
        self.assertEqual(self.order.service_region_ids, [r.pk for r in self.regions])
        original_name = self.order.service_region
        region.name = "广东省"
        region.save()
        form = MaintenanceOrderEditForm(instance=self.order, actor=self.actor)
        self.assertEqual(form["service_region"].value(), self.order.service_region_ids)
        self.assertEqual(self.order.service_region, original_name)
        self.order.service_region_ids = []
        self.order.service_region = "广东省 / 惠州"
        self.order.save()
        form = MaintenanceOrderEditForm(instance=self.order, actor=self.actor)
        self.assertEqual(form["service_region"].value(), [r.pk for r in self.regions])

    def test_create_and_add_another_saves_order_and_returns_to_blank_native_form(self):
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        data = {
            **self.create_data(),
            "policy": self.policy.pk,
            "priority": "medium",
            "_addanother": "",
        }
        count = MaintenanceOrder.objects.count()
        response = self.client.post(url, data)
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(MaintenanceOrder.objects.count(), count + 1)
        order = MaintenanceOrder.objects.latest("pk")
        self.assertEqual(order.contract_id, self.contract.pk)
        page = self.client.get(response.url)
        self.assertTemplateUsed(page, "generic/object_edit.html")
        self.assertFalse(page.context["form"].is_bound)
        self.assertContains(page, order.number)

    def test_native_form_preserves_input_and_displays_field_and_service_errors(self):
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        data = {**self.create_data(), "policy": self.policy.pk, "priority": "medium"}
        count = MaintenanceOrder.objects.count()
        response = self.client.post(url, {**data, "service_location": "", "_addanother": ""})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "form_helpers/render_errors.html")
        self.assertIn("service_location", response.context["form"].errors)
        self.assertEqual(response.context["form"]["requirements"].value(), data["requirements"])
        with patch(
            "netbox_oa.maintenance_views.services.create_order",
            side_effect=ValidationError("合同资料已变更，请重新选择合同。"),
        ):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "generic/object_edit.html")
        self.assertContains(response, "合同资料已变更，请重新选择合同。")
        self.assertEqual(response.context["form"]["contract_id"].value(), str(self.contract.pk))
        self.assertEqual(MaintenanceOrder.objects.count(), count)

    def test_create_page_number_is_read_only_and_saved_without_a_title(self):
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        page = self.client.get(url)
        form = page.context["form"]
        number = form["number"].value()
        self.assertRegex(number, r"^Y-GD[0-9]{6}[A-E]$")
        self.assertNotIn("title", form.fields)
        self.assertTrue(form.fields["number"].disabled)
        response = self.client.post(
            url,
            {
                **self.create_data(),
                "policy": self.policy.pk,
                "priority": "medium",
                "number_token": form["number_token"].value(),
                "number": "FORGED",
            },
        )
        self.assertEqual(response.status_code, 302)
        order = MaintenanceOrder.objects.latest("pk")
        self.assertEqual(order.number, number[:8] + "A" + number[8:])
        self.assertEqual(str(order), order.number)

    def test_number_token_cannot_be_forged_or_reused(self):
        self.client.force_login(self.actor)
        url = reverse("plugins:netbox_oa:maintenanceorder_add")
        data = {**self.create_data(), "policy": self.policy.pk, "priority": "medium"}
        count = MaintenanceOrder.objects.count()
        response = self.client.post(url, {**data, "number_token": "FORGED"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].non_field_errors())
        self.assertEqual(MaintenanceOrder.objects.count(), count)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MaintenanceOrder.objects.count(), count + 1)

    def test_random_numbers_include_year_month_and_reuse_tail_across_months(self):
        with patch(
            "netbox_oa.maintenance_numbering.secrets.choice", side_effect=lambda values: values[-1]
        ):
            with patch("netbox_oa.maintenance_numbering.timezone.localdate",
                       return_value=date(2030, 1, 1)):
                first, token = issue_number(self.actor)
                second, _ = issue_number(self.actor)
            with patch("netbox_oa.maintenance_numbering.timezone.localdate",
                       return_value=date(2030, 2, 1)):
                next_month, _ = issue_number(self.actor)
            with patch("netbox_oa.maintenance_numbering.timezone.localdate",
                       return_value=date(2031, 1, 1)):
                next_year, _ = issue_number(self.actor)
        self.assertEqual(first, "Y-GD300199E")
        self.assertEqual(second, "Y-GD300198E")
        self.assertEqual(next_month, "Y-GD300299E")
        self.assertEqual(next_year, "Y-GD310199E")
        self.assertEqual(number_from_token(token, self.actor), first)
        other = get_user_model().objects.create_user(username="another_number_actor")
        with self.assertRaises(ValidationError):
            number_from_token(token, other)

    def test_reserved_number_is_not_reallocated_when_page_is_abandoned(self):
        first, _ = issue_number(self.actor)
        second, _ = issue_number(self.actor)
        self.assertNotEqual(first, second)
        self.assertTrue(
            MaintenanceNumberReservation.objects.filter(number=first, used=False).exists()
        )

    def test_competing_reservation_retries_without_breaking_transaction(self):
        competing = "Y-GD300101A"
        MaintenanceNumberReservation.objects.create(number=competing, actor=self.actor)
        original_filter = MaintenanceNumberReservation.objects.filter

        def stale_available_numbers(**kwargs):
            queryset = original_filter(**kwargs)
            # Simulate a reservation created after this request read the available set.
            if "number__startswith" in kwargs:
                return queryset.exclude(number=competing)
            return queryset

        with patch("netbox_oa.maintenance_numbering.timezone.localdate",
                   return_value=date(2030, 1, 1)), patch(
            "netbox_oa.maintenance_numbering.secrets.choice", side_effect=lambda values: values[0]
        ), patch.object(MaintenanceNumberReservation.objects, "filter", stale_available_numbers):
            number, token = issue_number(self.actor)
        self.assertEqual(number, "Y-GD300102A")
        self.assertEqual(number_from_token(token, self.actor), number)
        self.assertTrue(MaintenanceNumberReservation.objects.filter(number=competing).exists())

    def test_signed_tokens_reject_invalid_number_formats(self):
        for number in ("Y-GD20260001", "Y-GD2611B03", "Y-GD2611-03B", "G-GD261103B",
                       "Y-GD260003B", "Y-GD261303B", "Y-GD261103F", "Y-GD261100B", "Y-GD26113B"):
            with self.subTest(number=number):
                MaintenanceNumberReservation.objects.create(number=number, actor=self.actor)
                token = signing.dumps({"number": number, "actor_id": self.actor.pk}, salt=SALT)
                with self.assertRaisesMessage(ValidationError, "凭证已失效"):
                    number_from_token(token, self.actor)

    def test_monthly_number_exhaustion_displays_error_on_create_page(self):
        today = timezone.localdate()
        prefix = f"Y-GD{today.year % 100:02d}{today.month:02d}"
        existing = set(MaintenanceNumberReservation.objects.values_list("number", flat=True))
        MaintenanceNumberReservation.objects.bulk_create([
            MaintenanceNumberReservation(number=f"{prefix}{value:02d}{letter}", actor=self.actor)
            for letter in "ABCDE" for value in range(1, 100)
            if f"{prefix}{value:02d}{letter}" not in existing
        ])
        self.client.force_login(self.actor)
        page = self.client.get(reverse("plugins:netbox_oa:maintenanceorder_add"))
        self.assertContains(page, "本月工单编号已用完")
        self.assertContains(page, "495")

    def test_workbench_and_api_only_use_order_number(self):
        self.client.force_login(self.actor)
        page = self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench"),
            {"tab": "all", "selected": self.order.pk},
        )
        self.assertContains(page, f"<h2>{self.order.number}</h2>", count=2, html=True)
        self.assertEqual(str(self.order), self.order.number)
        self.assertNotIn("title", [field.name for field in MaintenanceOrder._meta.fields])
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.actor)
        response = client.get(f"/api/maintenance/orders/{self.order.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["number"], self.order.number)
        self.assertNotIn("title", response.data)

    def test_deleted_or_hidden_contract_is_rejected_but_missing_regions_allow_manual_entry(self):
        with self.assertRaises(ValidationError):
            selected_contract(self.actor, 999999)
        with patch(
            "netbox_oa.maintenance_contracts.purchase_contracts",
            return_value=Contract.objects.none(),
        ):
            with self.assertRaises(ValidationError):
                selected_contract(self.actor, self.contract.pk)
        self.contract.custom_field_data = {"ProvincesOfResources": [self.regions[0].pk, 999999]}
        self.contract.save()
        self.assertEqual(selected_contract(self.actor, self.contract.pk)["service_region"], "")
        self.assertEqual(
            selected_contract(self.actor, self.contract.pk, "广西")["service_region"], "广西"
        )

    def test_contract_permission_is_required_and_hidden_regions_are_not_disclosed(self):
        reader = get_user_model().objects.create_user(username="contract_reader")
        with self.assertRaises(ValidationError):
            selected_contract(reader, self.contract.pk)
        reader.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="netbox_contract", codename="view_contract"
            )
        )
        reader = get_user_model().objects.get(pk=reader.pk)
        self.assertEqual(selected_contract(reader, self.contract.pk)["service_region"], "")
        with self.assertRaises(ValidationError):
            selected_contract(
                reader, self.contract.pk, service_region_ids=[self.extra_regions[0].pk]
            )
        reader.user_permissions.add(
            Permission.objects.get(content_type__app_label="dcim", codename="view_region")
        )
        reader = get_user_model().objects.get(pk=reader.pk)
        self.assertEqual(
            selected_contract(reader, self.contract.pk)["service_region"], "广东 / 惠州"
        )

    def test_new_revision_does_not_inherit_approval_signatures(self):
        self.reach("order_final")
        original = self.order.documents.get(material="order")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="修订服务范围",
        )
        revision = self.upload("order")
        self.client.force_login(self.actor)
        old_print = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[original.pk])
        )
        new_print = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[revision.pk])
        )
        self.assertIn("order_review", old_print.context["signatures"])
        self.assertNotIn("order_review", new_print.context["signatures"])
        self.assertNotIn("order_final", new_print.context["signatures"])

    def test_missing_contract_plugin_has_actionable_error(self):
        get_model = apps.get_model

        def without_contract(app_label, model_name):
            if app_label == "netbox_contract":
                raise LookupError
            return get_model(app_label, model_name)

        with patch("netbox_oa.maintenance_contracts.apps.get_model", side_effect=without_contract):
            form = MaintenanceOrderForm(actor=self.actor)
            self.assertIn("请先启用 netbox-contract", form.contract_error)
            with self.assertRaisesMessage(ValidationError, "请先启用 netbox-contract"):
                selected_contract(self.actor, self.contract.pk)

    def test_contract_change_updates_revision_but_preserves_previous_snapshot(self):
        original = self.upload("order")
        new_contract = Contract.objects.create(
            name="第二份外购合同",
            number="HT-NEW",
            external_party_object=self.provider,
            custom_field_data={"ProvincesOfResources": [self.regions[1].pk]},
        )
        services.update_order(pk=self.order.pk, actor=self.actor, contract_id=new_contract.pk)
        latest = self.order.documents.first()
        self.assertEqual(latest.form_data["contract_id"], new_contract.pk)
        self.assertEqual(latest.form_data["contract_number"], "HT-NEW")
        self.assertEqual(latest.form_data["service_region"], "惠州")
        original.refresh_from_db()
        self.assertEqual(original.form_data["contract_number"], self.contract.number)

    def test_number_is_automatic_unique_and_immutable(self):
        orders = [
            services.create_order(actor=self.actor, policy=self.policy, **self.create_data())
            for _ in range(3)
        ]
        self.assertEqual(len({self.order.number, *(order.number for order in orders)}), 4)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MaintenanceOrder.objects.filter(pk=orders[0].pk).update(number=self.order.number)
        with self.assertRaisesMessage(ValidationError, "不能人工指定"):
            services.create_order(
                actor=self.actor, policy=self.policy, number="MANUAL", **self.create_data()
            )
        with self.assertRaises(ValidationError):
            services.update_order(pk=self.order.pk, actor=self.actor, number="MANUAL")

    def test_only_owner_can_initiate_new_order(self):
        other = get_user_model().objects.create_user(username="other_owner")
        with self.assertRaisesMessage(ValidationError, "本人发起"):
            services.create_order(
                actor=self.actor, policy=self.policy, owner=other, **self.create_data()
            )
