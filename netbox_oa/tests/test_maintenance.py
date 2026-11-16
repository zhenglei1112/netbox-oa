from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django_fsm_log.models import StateLog
from rest_framework.test import APIClient
from users.models import Group

from netbox_oa import maintenance_services as services
from netbox_oa import maintenance_signatures as signatures
from netbox_oa.maintenance_flow import ROLES, STAGES, build_steps
from netbox_oa.maintenance_materials import TASK_SIGNING_NODES, company_form_initial
from netbox_oa.models import (
    MaintenanceDocument,
    MaintenanceEvent,
    MaintenanceOrder,
    MaintenanceTask,
)
from netbox_oa.tests import test_workflows
from tests.support.contracts.models import Contract, ContractType, ServiceProvider
from tests.support.dcim.models import Region


def sign_draft(order, actor):
    from netbox_oa import maintenance_signatures as signatures
    from netbox_oa.tests.test_signatures import image_data

    session = signatures.create_draft_session(pk=order.pk, actor=actor)
    return signatures.capture_signature(
        token=signatures.mobile_token(session), data_url=image_data()
    )


def sign_task(task, signer=None):
    from netbox_oa import maintenance_signatures as signatures
    from netbox_oa.tests.test_signatures import image_data

    signer = signer or (
        task.order.requester if task.kind == "submission" else
        get_user_model().objects.filter(is_superuser=True).exclude(
            pk=task.order.requester_id
        ).first()
    )
    session = signatures.create_session(pk=task.pk, actor=signer)
    return signatures.capture_signature(
        token=signatures.mobile_token(session), data_url=image_data()
    )


def acting_user(case, task=None):
    task = task or case.order.tasks.filter(status="pending").first()
    if task and task.kind in {"approval", "countersign"} and (
        task.order.requester_id == case.actor.pk
    ):
        return case.approver
    return case.actor


class MaintenanceTests(TestCase):
    grant = test_workflows.WorkflowTests.grant

    def test_maintenance_serializers_follow_netbox_discovery_convention(self):
        from netbox_oa.api.maintenance import (
            DocumentViewSet,
            OrderViewSet,
            PolicyViewSet,
            TaskViewSet,
        )

        # NetBox resolves model serializers by this path, independently of the viewset.
        for viewset in (PolicyViewSet, OrderViewSet, TaskViewSet, DocumentViewSet):
            model = viewset.queryset.model
            with self.subTest(model=model.__name__):
                serializer = import_string(
                    f"{model._meta.app_label}.api.serializers.{model.__name__}Serializer"
                )
                self.assertIs(serializer.Meta.model, model)
                self.assertIs(serializer, viewset.serializer_class)

        policy_serializer = PolicyViewSet.serializer_class(self.policy)
        self.assertEqual(policy_serializer.data["groups"], {
            role: self.group.pk for role, _ in ROLES
        })
        self.assertEqual(policy_serializer.data["sla_hours"], self.policy.sla_hours)

    def test_policy_configuration_pages_and_save(self):
        self.client.force_login(self.actor)
        add_url = reverse("plugins:netbox_oa:maintenancepolicy_add")
        edit_url = reverse("plugins:netbox_oa:maintenancepolicy", args=[self.policy.pk])
        for url in (add_url, edit_url):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "维护流程角色与时限配置")

        response = self.client.post(edit_url, {
            "name": self.policy.name,
            "enabled": "on",
            "is_default": "on",
            "sla_hours": 48,
            "warning_hours": 6,
            **{role: self.group.pk for role, _ in ROLES},
        })
        self.assertRedirects(response, edit_url)
        self.policy.refresh_from_db()
        self.assertEqual(self.policy.sla_hours, 48)
        self.assertEqual(self.policy.warning_hours, 6)

    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(
            username="maintenance_admin", email="admin@example.test", password="test"
        )
        self.approver = get_user_model().objects.create_superuser(
            username="maintenance_reviewer", email="reviewer@example.test", password="test"
        )
        self.group = Group.objects.create(name="Maintenance handlers")
        self.regions = [Region.objects.create(name=name) for name in ("广东", "惠州")]
        self.provider = ServiceProvider.objects.create(name="测试服务单位")
        self.contract_type = ContractType.objects.create(name="维护")
        self.contract = Contract.objects.create(
            contract_type=self.contract_type,
            name="测试外购合同",
            number="HT-2026-006",
            external_party_object=self.provider,
            custom_field_data={"ProvincesOfResources": [region.pk for region in self.regions]},
        )
        self.policy = services.save_policy(
            actor=self.actor, name="默认维护流程", groups={key: self.group.pk for key, _ in ROLES}
        )
        self.order = services.create_order(
            actor=self.actor,
            policy=self.policy,
            service_type="fiber",
            supplier="测试服务单位",
            contract_id=self.contract.pk,
            contract_number="HT-2026-006",
            service_region="广东 / 惠州",
            service_location="惠州站点",
            requirements="修复光缆中断故障",
        )

    def tearDown(self):
        for document in MaintenanceDocument.objects.all():
            document.file.delete(save=False)
        TestCase.tearDown(self)

    def start(self):
        self.order.refresh_from_db()
        sign_draft(self.order, self.order.requester)
        self.order = services.start_order(pk=self.order.pk, actor=self.actor)
        return self.order

    def current(self):
        return self.order.tasks.get(status="pending")

    def upload(self, material, state="electronic", *, sealed=False):
        if material in {"order", "acceptance"} and state == "electronic" and not sealed:
            order = MaintenanceOrder.objects.get(pk=self.order.pk)
            return services.save_company_form(
                pk=order.pk, actor=self.actor, material=material,
                data={**company_form_initial(order), "service_effect": "passed",
                      "settlement_amount": "1234.56"},
            )
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        node = order.steps_snapshot[order.current_position] if order.steps_snapshot else {}
        if material in {"report", "settlement"} or node.get("scan_material") == material:
            if state == "electronic":
                state = "scan"
            source = (
                services.material_scan_sources(
                    MaintenanceOrder.objects.get(pk=self.order.pk), material
                ).first()
                if sealed
                else None
            )
        else:
            source = self.order.documents.filter(material=material).exclude(kind="scan").first()
        return services.upload_document(
            pk=self.order.pk,
            actor=self.actor,
            material=material,
            state="scan" if sealed else state,
            source_document_id=source.pk if source and (state == "scan" or sealed) else None,
            file=SimpleUploadedFile(
                "material.pdf", b"test document", content_type="application/pdf"
            ),
        )

    def complete(self):
        task = self.current()
        actor = acting_user(self, task)
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        node = order.steps_snapshot[task.position]
        scan_material = node.get("scan_material")
        form = order.documents.filter(material="acceptance", kind="form").first()
        if node["key"] == "acceptance_apply" and (
            not form or form.version <= task.evidence.get("correction_baseline", {}).get(
                "acceptance", 0
            )
        ):
            self.upload("acceptance")
        for material in node.get("scan_materials", []):
            if not services.material_scan_sources(order, material).exists():
                self.upload(material, "scan")
        if scan_material:
            sources = list(services.material_scan_sources(order, scan_material))
            if not sources:
                self.upload(scan_material, "scan")
                sources = list(services.material_scan_sources(order, scan_material))
            if any(state == "sealed" for _, state in node["required"]):
                for source in sources:
                    if not source.derived_documents.filter(is_sealed=True).exists():
                        services.upload_document(
                            pk=order.pk,
                            actor=self.actor,
                            material=scan_material,
                            state="scan",
                            source_document_id=source.pk,
                            file=SimpleUploadedFile("stamped.pdf", b"scan"),
                        )
        for material, state in node["required"]:
            if material != scan_material and not order.documents.filter(material=material).exists():
                self.upload(material, state)
        if node["key"] in TASK_SIGNING_NODES and not task.signatures.filter(
            signer=actor, cancelled=False
        ).exists():
            sign_task(task, actor)
        kwargs = {"external_reference": "EIP-TEST-001"} if task.kind == "external" else {}
        return services.complete_task(
            pk=task.pk, actor=actor, comment="已核对凭证并完成办理", **kwargs
        )

    def reach(self, key):
        self.start()
        while (
            MaintenanceOrder.objects.get(pk=self.order.pk).steps_snapshot[self.current().position][
                "key"
            ]
            != key
        ):
            self.complete()
        if key == "acceptance_apply":
            self.upload("acceptance")
            self.upload("settlement", "scan")

    def test_material_validation_lists_all_missing_payment_documents_together(self):
        self.reach("payment_plan")
        with self.assertRaises(ValidationError) as caught:
            services.complete_task(
                pk=self.current().pk, actor=acting_user(self), external_reference="EIP-001"
            )
        self.assertEqual(caught.exception.messages, [
            "请上传以下材料：付款通知书、电子发票。"
        ])
        self.upload("notice")
        with self.assertRaises(ValidationError) as caught:
            services.complete_task(
                pk=self.current().pk, actor=acting_user(self), external_reference="EIP-001"
            )
        self.assertEqual(caught.exception.messages, [
            "请上传以下材料：电子发票。"
        ])
        self.assertNotIn("材料状态至少", str(caught.exception))
        self.upload("invoice")
        self.assertEqual(services.complete_task(
            pk=self.current().pk, actor=acting_user(self), external_reference="EIP-001"
        ).status, "completed")

    def test_full_four_stage_lifecycle_finishes_after_payment_application(self):
        self.start()
        visited = set()
        while self.order.tasks.filter(status="pending").exists():
            order = MaintenanceOrder.objects.get(pk=self.order.pk)
            visited.add(order.stage)
            task = self.current()
            if task.name == "付款":
                self.assertEqual(order.status, "active")
                self.assertEqual(order.payment_status, "submitted")
                self.assertIsNone(order.completed_at)
            self.complete()
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(visited, {key for key, _ in STAGES})
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.payment_status, "requested")
        self.assertIsNotNone(order.completed_at)
        self.assertEqual(
            order.tasks.count(), sum(not node.get("return_only") for node in order.steps_snapshot)
        )
        self.assertEqual(StateLog.objects.for_(order).count(), 2)

    def test_flow_definition_matches_runtime_and_selected_policy(self):
        self.client.force_login(self.actor)
        self.policy.sla_hours = 36
        self.policy.save()
        url = reverse("plugins:netbox_oa:maintenance_flow_definition")
        response = self.client.get(url, {"policy": self.policy.pk})
        self.assertEqual(response.status_code, 200)
        nodes = [node for stage in response.context["stages"] for node in stage["nodes"]]
        expected = build_steps({key: self.group.pk for key, _ in ROLES}, 36)
        expected = [
            node
            for key, _ in STAGES
            for node in sorted(
                [step for step in expected if step["stage"] == key],
                key=lambda step: bool(step.get("return_only")),
            )
        ]
        self.assertEqual([node["key"] for node in nodes], [node["key"] for node in expected])
        self.assertContains(response, "每节点时限 1天12小时0分钟")
        self.assertContains(response, "提前 4小时0分钟预警")
        self.assertContains(response, "时限：1天12小时0分钟", count=len(nodes))
        self.assertEqual(response.context["node_count"], 10)
        self.assertEqual(response.context["correction_count"], 1)
        self.assertContains(response, "3 个顺序环节")
        diagram = response.context["diagram"]
        sequential = [
            edge
            for edge in diagram["edges"]
            if edge["source"] not in {"start", "order_correction"} and edge["target"] != "end"
        ]
        expected_main = [node for node in expected if not node.get("return_only")]
        self.assertEqual(
            [(edge["source"], edge["target"]) for edge in sequential],
            list(
                zip(
                    [node["key"] for node in expected_main[:-1]],
                    [node["key"] for node in expected_main[1:]],
                )
            ),
        )
        self.assertEqual(
            [(edge["source"], edge["target"]) for edge in diagram["returns"]],
            [(node["key"], node["return_to"]) for node in nodes if node["return_label"]],
        )
        self.assertContains(response, 'aria-label="维护工单流程图"')
        for node in expected:
            self.assertContains(response, f'href="#flow-node-{node["key"]}"')
        for node in nodes:
            self.assertContains(response, node["name"])
            self.assertEqual(node["sla_hours"], 36)
        self.assertContains(response, self.group.name)
        self.assertEqual(
            next(node for node in nodes if node["key"] == "acceptance_review")["return_label"],
            "验收申请",
        )
        self.assertEqual(
            [len(stage["nodes"]) for stage in response.context["stages"]], [4, 4, 3]
        )
        by_key = {node["key"]: node for node in nodes}
        for key in ("acceptance_apply", "acceptance_apply", "payment_plan"):
            self.assertEqual(by_key[key]["role_label"], "发起人")
            self.assertIsNone(by_key[key]["group"])
        self.assertIn("填写", by_key["acceptance_apply"]["rules"][0])
        self.assertIn("手写签名", by_key["acceptance_review"]["rules"][0])
        self.assertIn("自动", by_key["acceptance_company"]["rules"][-1])
        self.assertIn("合同详情页", by_key["payment_plan"]["rules"][0])
        self.assertEqual(by_key["payment_eip"]["completion_label"], "工单完成；付款申请已提交")
        self.assertContains(response, "付款申请已提交 · 工单完成")
        self.assertNotContains(response, "不代表已付款或已收款")
        self.assertNotContains(response, "已确认收款 · 工单完成")
        self.assertNotContains(response, "实际付款及收款确认完成后")
        self.assertNotContains(response, "财务经办组")
        self.assertEqual(self.client.post(url).status_code, 405)

    def test_flow_rules_describe_current_acceptance_materials_and_return_paths(self):
        self.client.force_login(self.actor)
        response = self.client.get(reverse("plugins:netbox_oa:maintenance_flow_definition"))
        nodes = {
            node["key"]: node for stage in response.context["stages"] for node in stage["nodes"]
        }
        for key in ("acceptance_apply", "acceptance_review", "acceptance_department",
                    "acceptance_company"):
            self.assertEqual(nodes[key]["materials"], [
                "工单服务验收单 · 电子版",
                "服务报告 · 扫描版（可多份）",
                "服务验收申请单 · 扫描版（可多份）",
            ])
        for key in ("order_review", "order_final", "order_senior_seal"):
            self.assertEqual(nodes[key]["return_label"], "工单修正")
        self.assertEqual(nodes["payment_review"]["return_label"], "生成付款")
        self.assertEqual(nodes["order_correction"]["completion_label"], "重新进入工单复核")
        self.assertIn("自动", nodes["order_senior_seal"]["rules"][0])
        self.assertIn("本次验收单版本", nodes["acceptance_department"]["rules"][1])
        self.assertNotIn("acceptance_sealed_upload", nodes)
        self.assertContains(response, "发起人不能审批自己发起的工单")
        self.assertContains(response, "验收复核使用复核人用户组")
        for key in ("report_fill", "settlement_fill", "order_department_seal"):
            self.assertNotIn(key, nodes)

    def test_flow_definition_permissions_and_policy_visibility(self):
        url = reverse("plugins:netbox_oa:maintenance_flow_definition")
        self.assertEqual(self.client.get(url).status_code, 302)
        user = get_user_model().objects.create_user(username="definition_reader")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.grant(user, "maintenanceorder", "view")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "仅查看流程结构")
        self.assertNotContains(response, self.policy.name)
        self.assertEqual(self.client.get(url, {"policy": self.policy.pk}).status_code, 404)
        self.assertEqual(self.client.get(url, {"policy": "invalid"}).status_code, 404)

    def test_material_gate_and_duplicate_completion(self):
        self.reach("acceptance_apply")
        task = self.current()
        with self.assertRaises(ValidationError):
            services.complete_task(pk=task.pk, actor=acting_user(self))
        self.assertEqual(MaintenanceTask.objects.get(pk=task.pk).status, "pending")
        self.upload("report", "scan")
        self.complete()
        with self.assertRaises(ValidationError):
            services.complete_task(pk=task.pk, actor=acting_user(self))

    def test_order_and_task_can_refresh_after_transition(self):
        original = self.order
        self.start()
        original.refresh_from_db()
        self.assertEqual(original.status, "active")
        task = self.current()
        self.complete()
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        for obj in (original, task):
            with self.assertRaises(AttributeError):
                obj.status = "cancelled"

    def test_return_requires_new_revision_and_reruns_review(self):
        self.reach("order_review")
        task = self.current()
        document = self.order.documents.get(material="order")
        services.return_task(
            pk=task.pk, actor=acting_user(self), comment="补全服务范围",
        )
        self.assertEqual(self.current().name, "工单修正")
        with self.assertRaises(ValidationError):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        revision = self.upload("order")
        self.assertEqual(revision.version, document.version + 1)
        self.complete()
        self.assertEqual(self.current().name, "工单复核")
        self.assertEqual(MaintenanceTask.objects.get(pk=task.pk).status, "returned")

    def test_external_node_requires_reference(self):
        self.reach("payment_eip")
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(
            [step["name"] for step in order.steps_snapshot if step["stage"] == "payment"],
            ["生成付款", "付款部门审核", "付款"],
        )
        self.client.force_login(self.actor)
        page = self.client.get(self.order.get_absolute_url())
        for material in ("notice", "invoice"):
            self.assertNotContains(page, f"panel=material-{material}&amp;pane=upload")
            with self.assertRaisesMessage(ValidationError, "只能在生成付款环节上传"):
                self.upload(material)
        with self.assertRaisesMessage(ValidationError, "外部流程节点必须登记"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.complete()
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.payment_status, "requested")

    def test_payment_plan_can_submit_without_payment_selection(self):
        self.reach("payment_plan")
        self.upload("notice")
        self.upload("invoice")
        services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.assertEqual(self.current().name, "付款部门审核")

    def test_configuration_snapshot_and_future_group_protection(self):
        self.start()
        replacement = Group.objects.create(name="Replacement")
        services.save_policy(
            actor=self.actor,
            pk=self.policy.pk,
            name=self.policy.name,
            groups={key: replacement.pk for key, _ in ROLES},
        )
        self.assertEqual(self.order.steps_snapshot[1]["group_id"], self.group.pk)
        with self.assertRaises(ProtectedError):
            self.group.delete()

    def worker(self, username):
        actor = get_user_model().objects.create_user(username=username, password="test")
        actor.groups.add(self.group)
        self.grant(actor, "maintenancetask", "change")
        self.grant(actor, "maintenanceorder", "view")
        return actor

    def test_two_submission_steps_belong_to_requester_even_when_owner_differs(self):
        requester, owner = self.worker("requester"), self.worker("owner")
        self.grant(requester, "maintenancedocument", "view")
        self.grant(requester, "maintenanceorder", "change")
        MaintenanceOrder.objects.filter(pk=self.order.pk).update(requester=requester, owner=owner)
        self.start()
        checked = []
        while self.order.tasks.filter(status="pending").exists():
            task = self.current()
            order = MaintenanceOrder.objects.get(pk=self.order.pk)
            node = order.steps_snapshot[task.position]
            if node["key"] in {"acceptance_apply", "acceptance_apply", "payment_plan"}:
                self.assertEqual(node["role"], "requester")
                self.assertEqual(task.assigned_user_id, requester.pk)
                self.assertIsNone(task.assigned_group_id)
                self.assertFalse(services.task_is_assigned(task, owner))
                with self.assertRaises(PermissionDenied):
                    services.complete_task(pk=task.pk, actor=owner)
                if node["key"] == "payment_plan":
                    self.upload("notice")
                    self.upload("invoice")
                else:
                    self.upload("acceptance")
                    self.upload("report", "scan")
                    self.upload("settlement", "scan")
                if node["key"] in TASK_SIGNING_NODES:
                    sign_task(task, requester)
                services.complete_task(pk=task.pk, actor=requester)
                checked.append(node["key"])
            else:
                self.complete()
        self.assertEqual(
            checked, ["acceptance_apply", "payment_plan"]
        )

    def test_automatic_assignment_and_transfer_prevent_other_members_handling_task(self):
        self.reach("order_review")
        first, second = self.worker("first"), self.worker("second")
        self.grant(second, "maintenancedocument", "view")
        self.grant(first, "maintenancedocument", "add")
        self.grant(first, "maintenancedocument", "view")
        signatures.create_session(pk=self.current().pk, actor=first)
        task = self.current()
        self.assertEqual(task.assigned_user_id, first.pk)
        self.assertIsNotNone(task.started_at)
        with self.assertRaises(PermissionDenied):
            services.complete_task(pk=task.pk, actor=second)
        services.transfer_task(pk=task.pk, actor=first, user_id=second.pk, comment="交班")
        with self.assertRaises(PermissionDenied):
            services.complete_task(pk=task.pk, actor=first)
        sign_task(task, second)
        services.complete_task(pk=task.pk, actor=second)

    def test_countersign_inserts_approval_before_next_node(self):
        self.reach("order_review")
        target = self.worker("extra_reviewer")
        services.countersign_task(
            pk=self.current().pk, actor=acting_user(self), user_id=target.pk, comment="补充审核"
        )
        self.complete()
        task = self.current()
        self.assertEqual(task.kind, "countersign")
        self.assertEqual(task.assigned_user_id, target.pk)
        services.complete_task(pk=task.pk, actor=target)
        task.refresh_from_db()
        self.assertEqual(task.comment, "同意")
        self.assertEqual(self.current().name, "工单部门审核")

    def test_failure_rolls_back_task_event_and_state_log(self):
        self.start()
        task = self.current()
        events = MaintenanceEvent.objects.count()
        with patch(
            "netbox_oa.maintenance_services._new_task", side_effect=ValidationError("failure")
        ):
            with self.assertRaises(ValidationError):
                services.complete_task(pk=task.pk, actor=acting_user(self))
        self.assertEqual(MaintenanceTask.objects.get(pk=task.pk).status, "pending")
        self.assertEqual(MaintenanceEvent.objects.count(), events)
        self.assertEqual(StateLog.objects.for_(task).count(), 0)

    def test_payment_submission_waits_for_countersign(self):
        self.reach("payment_review")
        target = self.worker("payment_reviewer")
        services.countersign_task(
            pk=self.current().pk, actor=acting_user(self), user_id=target.pk, comment="增加复核"
        )
        self.complete()
        self.assertEqual(MaintenanceOrder.objects.get(pk=self.order.pk).payment_status, "draft")
        services.complete_task(pk=self.current().pk, actor=target, comment="复核通过")
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        self.assertEqual(order.payment_status, "submitted")
        self.assertEqual(order.stage, "payment")

    def test_stage_change_blocks_modification_of_previous_material(self):
        self.reach("acceptance_apply")
        with self.assertRaises(ValidationError):
            self.upload("order")

    def test_terminal_state_and_draft_withdrawal(self):
        services.withdraw_order(pk=self.order.pk, actor=self.actor)
        with self.assertRaises(ValidationError):
            services.start_order(pk=self.order.pk, actor=self.actor)

    def test_follow_does_not_grant_access(self):
        services.follow_order(pk=self.order.pk, actor=self.actor)
        self.assertTrue(self.order.followers.filter(pk=self.actor.pk).exists())
        outsider = get_user_model().objects.create_user(username="outsider", password="test")
        with self.assertRaises(PermissionDenied):
            services.follow_order(pk=self.order.pk, actor=outsider)

    def test_workbench_forms_and_private_download(self):
        self.start()
        self.client.force_login(self.actor)
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench"), {"tab": "all"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order.number)
        self.assertContains(response, "工单复核")
        self.assertContains(response, "工单下单")
        self.assertEqual(
            self.client.get(reverse("plugins:netbox_oa:maintenanceorder_add")).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                reverse("plugins:netbox_oa:maintenancepolicy", args=[self.policy.pk])
            ).status_code,
            200,
        )
        while self.order.steps_snapshot[self.current().position]["key"] != "acceptance_apply":
            self.complete()
        document = self.upload("report", "scan")
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_download", args=[document.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        response.close()
        with self.assertRaises(ValueError):
            _ = document.file.url
        outsider = get_user_model().objects.create_user(
            username="download_outsider", password="test"
        )
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(
                reverse("plugins:netbox_oa:maintenance_download", args=[document.pk])
            ).status_code,
            404,
        )

    def test_http_actions_post_only_and_csrf_protected(self):
        self.start()
        self.actor = acting_user(self)
        self.client.force_login(self.actor)
        url = reverse(
            "plugins:netbox_oa:maintenancetask_action", args=[self.current().pk, "complete"]
        )
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        self.assertEqual(client.post(url, {}).status_code, 403)
        sign_task(self.current())
        self.assertEqual(self.client.post(url, {}).status_code, 302)

    def test_waiting_filter_and_deadline_stats(self):
        self.start()
        self.actor = acting_user(self)
        task = self.current()
        task.deadline = timezone.now() - timedelta(minutes=1)
        task.save()
        self.client.force_login(self.actor)
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_workbench"), {"sort": "waiting"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "已超时")
        self.assertEqual(response.context["stats"]["overdue"], 1)

    def test_update_order_only_in_draft_or_correction(self):
        services.update_order(pk=self.order.pk, actor=self.actor, service_location="修订位置")
        self.reach("order_review")
        with self.assertRaises(ValidationError):
            services.update_order(
                pk=self.order.pk, actor=self.actor, service_location="不能覆盖已审核内容"
            )
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="补正",
        )
        services.update_order(pk=self.order.pk, actor=self.actor, requirements="修订服务要求")
        self.assertEqual(
            MaintenanceOrder.objects.get(pk=self.order.pk).requirements, "修订服务要求"
        )


class MaintenanceAPITests(TestCase):
    setUp = MaintenanceTests.setUp
    tearDown = MaintenanceTests.tearDown
    start = MaintenanceTests.start
    current = MaintenanceTests.current
    complete = MaintenanceTests.complete
    upload = MaintenanceTests.upload
    worker = MaintenanceTests.worker
    grant = MaintenanceTests.grant

    def api(self, actor=None, token=None):
        client = APIClient()
        client.force_authenticate(user=actor or self.actor, token=token)
        return client

    def test_create_rejects_direct_state_changes(self):
        response = self.api().post(
            "/api/maintenance/orders/",
            {
                "title": "API工单",
                "policy": self.policy.pk,
                "owner": self.actor.pk,
                "service_type": "fiber",
                "supplier": "服务单位",
                "contract_id": self.contract.pk,
                "contract_number": "HT-1",
                "service_region": "广东",
                "service_location": "站点",
                "requirements": "维修",
                "status": "completed",
                "number": "FAKE",
                "payment_status": "received",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "draft")
        self.assertNotEqual(response.data["number"], "FAKE")
        self.assertEqual(response.data["requester"], self.actor.pk)
        self.assertEqual(
            self.api()
            .patch(f"/api/maintenance/orders/{self.order.pk}/", {}, format="json")
            .status_code,
            405,
        )

    def test_post_transition_uses_change_permission_and_token_write_gate(self):
        self.start()
        self.actor = acting_user(self)
        task = self.current()
        worker = self.actor
        sign_task(task)
        response = self.api(worker).post(
            f"/api/maintenance/tasks/{task.pk}/complete/", {}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "completed")
        next_task = self.current()
        response = self.api(token=SimpleNamespace(write_enabled=False)).post(
            f"/api/maintenance/tasks/{next_task.pk}/complete/", {}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MaintenanceTask.objects.get(pk=next_task.pk).status, "pending")

    def test_api_withdraw_and_policy_configuration(self):
        response = self.api().post(
            f"/api/maintenance/orders/{self.order.pk}/withdraw/", {"comment": "撤销"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "cancelled")
        response = self.api().post(
            f"/api/maintenance/policies/{self.policy.pk}/configure/",
            {
                "name": self.policy.name,
                "groups": {key: self.group.pk for key, _ in ROLES},
                "sla_hours": 48,
                "warning_hours": 6,
                "enabled": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["enabled"])

    def test_document_metadata_requires_parent_order_permission(self):
        document = self.upload("order")
        response = self.api().get(f"/api/maintenance/documents/{document.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("file", response.data)
        outsider = get_user_model().objects.create_user(
            username="metadata_outsider", password="test"
        )
        self.grant(outsider, "maintenancedocument", "view")
        self.assertEqual(
            self.api(outsider).get(f"/api/maintenance/documents/{document.pk}/").status_code, 404
        )

    def test_api_upload_and_invalid_target(self):
        response = self.api().post(
            "/api/maintenance/documents/",
            {
                "order_id": self.order.pk,
                "material": "settlement",
                "file": SimpleUploadedFile("settlement.pdf", b"document"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.start()
        response = self.api().post(
            f"/api/maintenance/tasks/{self.current().pk}/transfer/",
            {"comment": "转办"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class MaintenanceCompanyFormTests(TestCase):
    setUp = MaintenanceTests.setUp
    tearDown = MaintenanceTests.tearDown
    start = MaintenanceTests.start
    current = MaintenanceTests.current
    complete = MaintenanceTests.complete
    reach = MaintenanceTests.reach
    upload = MaintenanceTests.upload
    grant = MaintenanceTests.grant

    def form_data(self):
        return company_form_initial(MaintenanceOrder.objects.get(pk=self.order.pk))

    def form_url(self, material="order"):
        return reverse("plugins:netbox_oa:maintenance_company_form", args=[self.order.pk, material])

    def test_fill_print_and_scan_round_trip(self):
        self.client.force_login(self.actor)
        page = self.client.get(self.form_url())
        self.assertContains(page, 'class="btn btn-primary" type="submit">保存</button>')
        self.assertContains(page, f'href="{self.order.get_absolute_url()}">取消</a>')
        data = self.form_data()
        data["service_region"] = [region.pk for region in self.regions]
        data["requirements"] = "核对端口并修复故障"
        response = self.client.post(self.form_url(), data)
        document = self.order.documents.filter(kind="form").first()
        self.assertRedirects(response, self.order.get_absolute_url())
        self.assertEqual(document.form_data["requirements"], data["requirements"])
        self.assertEqual(
            MaintenanceOrder.objects.get(pk=self.order.pk).requirements, data["requirements"]
        )
        self.assertFalse(document.file)
        printed = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        )
        self.assertContains(printed, data["requirements"])
        self.assertContains(printed, "部门审核")
        self.assertContains(printed, "print-document")
        edited = self.client.get(self.form_url())
        self.assertContains(edited, 'type="date"')
        self.assertContains(edited, 'value="' + data["requested_at"][:10] + '"')
        self.reach("acceptance_apply")
        document.refresh_from_db()
        self.assertTrue(document.electronic_seal)
        self.assertFalse(self.order.documents.filter(kind="scan").exists())
        page = self.client.get(self.order.get_absolute_url())
        self.assertNotContains(page, "用印后")

    def test_report_company_section_is_not_supplier_evidence(self):
        self.reach("acceptance_apply")
        with self.assertRaisesMessage(ValidationError, "此材料由乙方提供"):
            services.save_company_form(
                pk=self.order.pk, actor=self.actor, material="report", data=self.form_data()
            )
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.form_url("report")).status_code, 404)
        self.assertEqual(
            self.client.post(self.form_url("report"), self.form_data()).status_code, 404
        )
        # A form created before reports became scan-only remains readable.
        document = MaintenanceDocument.objects.create(
            order=self.order, material="report", kind="form", version=1,
            form_data=self.form_data(), state="electronic", uploaded_by=self.actor,
        )
        with self.assertRaisesMessage(ValidationError, "不能替代乙方报告"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        with self.assertRaisesMessage(ValidationError, "上传服务报告时请提交扫描版原件"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="report",
                state="scan",
                source_document_id=document.pk,
                file=SimpleUploadedFile("scan.pdf", b"scan"),
            )
        self.client.force_login(self.actor)
        response = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        )
        for label in ("甲方填写", "乙方填写", "服务开始时间", "单次费用核算", "验收人员"):
            self.assertContains(response, label)
        self.upload("report")
        self.complete()
        self.assertEqual(self.current().name, "验收复核")

    def test_scan_rejects_stale_source_and_non_scan_file(self):
        original = self.upload("order")
        current = self.upload("order")
        self.reach("order_senior_seal")
        for source, filename, message in (
            (original.pk, "scan.pdf", "仅保留系统电子件"),
            (current.pk, "scan.docx", "仅保留系统电子件"),
            (None, "scan.pdf", "仅保留系统电子件"),
        ):
            with self.subTest(source=source, filename=filename):
                with self.assertRaisesMessage(ValidationError, message):
                    services.upload_document(
                        pk=self.order.pk,
                        actor=self.actor,
                        material="order",
                        state="scan",
                        source_document_id=source,
                        file=SimpleUploadedFile(filename, b"scan"),
                    )
        self.assertEqual(self.order.documents.count(), 3)

    def test_revised_form_keeps_old_print_and_requires_new_seal(self):
        original = self.upload("order")
        data = self.form_data()
        data["requirements"] = "新的服务范围"
        current = services.save_company_form(
            pk=self.order.pk, actor=self.actor, material="order", data=data
        )
        self.assertEqual(current.version, original.version + 1)
        with self.assertRaises(ValidationError):
            services._materials(self.order, {"required": [("order", "sealed")]})
        self.client.force_login(self.actor)
        old_print = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[original.pk])
        )
        self.assertContains(old_print, original.form_data["requirements"])
        self.assertNotContains(old_print, "新的服务范围")

    def test_basic_order_edit_creates_matching_form_revision(self):
        original = self.upload("order")
        services.update_order(pk=self.order.pk, actor=self.actor, requirements="修改后的要求")
        latest = self.order.documents.first()
        self.assertEqual(latest.version, original.version + 1)
        self.assertEqual(latest.form_data["requirements"], "修改后的要求")

    def test_form_edits_follow_review_and_terminal_guards(self):
        self.reach("order_review")
        with self.assertRaisesMessage(ValidationError, "当前节点不能修改"):
            self.upload("order")
        services.return_task(
            pk=self.current().pk, actor=acting_user(self), comment="修订要求",
        )
        with self.assertRaisesMessage(ValidationError, "仅保留系统电子件"):
            self.upload("order", sealed=True)
        with self.assertRaisesMessage(ValidationError, "退回补正后必须"):
            services.complete_task(pk=self.current().pk, actor=acting_user(self))
        self.upload("order")
        self.complete()
        services.terminate_order(pk=self.order.pk, actor=self.actor, comment="停止服务")
        with self.assertRaisesMessage(ValidationError, "工单已经结束"):
            self.upload("order")

    def test_form_and_print_permissions_and_csrf(self):
        document = self.upload("order")
        outsider = get_user_model().objects.create_user(username="form_outsider", password="test")
        self.grant(outsider, "maintenancedocument", "view")
        self.client.force_login(outsider)
        print_url = reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        self.assertEqual(self.client.get(print_url).status_code, 403)
        self.assertEqual(self.client.post(self.form_url(), self.form_data()).status_code, 404)
        self.grant(outsider, "maintenanceorder", "view")
        self.grant(outsider, "maintenanceorder", "change")
        self.grant(outsider, "maintenancedocument", "add")
        self.assertEqual(self.client.post(self.form_url(), self.form_data()).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.actor)
        self.assertEqual(csrf_client.post(self.form_url(), self.form_data()).status_code, 403)
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(self.form_url("settlement")).status_code, 404)

    def test_company_order_cannot_be_replaced_by_electronic_upload(self):
        with self.assertRaisesMessage(ValidationError, "仅保留系统电子件"):
            services.upload_document(
                pk=self.order.pk,
                actor=self.actor,
                material="order",
                file=SimpleUploadedFile("order.docx", b"document"),
            )
        self.assertEqual(self.order.documents.count(), 1)
