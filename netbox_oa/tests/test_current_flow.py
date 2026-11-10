
"""The current workflow is the only supported runtime contract."""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.maintenance_flow import ROLES, build_steps
from netbox_oa.maintenance_forms import MaintenanceActionForm
from netbox_oa.models import MaintenanceOrder
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user

REMOVED_PAYMENT_FIELDS = {
    "payment_amount",
    "period_start",
    "period_end",
    "settlement_period_mode",
    "contract_payment_id",
    "contract_payment_snapshot",
}


class CurrentFlowTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload
    reach = test_maintenance.MaintenanceTests.reach

    def test_only_current_roles_nodes_and_flags_are_built(self):
        self.assertEqual(set(dict(ROLES)), {"reviewer", "department", "senior", "seal"})
        nodes = build_steps({}, 24)
        self.assertEqual(sum(not node.get("return_only") for node in nodes), 14)
        self.assertFalse(
            {"order_fill", "paid", "receipt", "issue", "invoice_check"}
            & {node["key"] for node in nodes}
        )
        self.assertTrue(
            all(
                "report_scans" not in node and "finish_on_application" not in node for node in nodes
            )
        )
        self.assertEqual(self.policy.roles.count(), 4)
        self.start()
        self.assertEqual(self.current().name, "工单复核")

    def test_seal_then_requester_upload_for_both_company_forms(self):
        self.start()
        for material, label in (("order", "服务工单"), ("acceptance", "验收单")):
            while self.order.steps_snapshot[self.current().position]["key"] != f"{material}_seal":
                self.complete()
            seal_task = self.current()
            with self.assertRaises(ValidationError):
                self.upload(material, "scan", sealed=True)
            self.complete()
            seal_task.refresh_from_db()
            self.assertEqual(seal_task.status, "completed")
            upload_task = self.current()
            self.assertEqual(upload_task.name, f"上传用印后{label}")
            self.assertEqual(upload_task.assigned_user_id, self.order.requester_id)
            self.assertIsNone(upload_task.assigned_group_id)
            with self.assertRaisesMessage(ValidationError, "用印扫描件"):
                services.complete_task(pk=upload_task.pk, actor=acting_user(self))
            stamped = self.upload(material, "scan", sealed=True)
            self.assertTrue(stamped.is_sealed)
            self.assertEqual(stamped.uploaded_task_id, upload_task.pk)
            self.complete()

    def test_removed_payment_fields_are_absent_from_schema_forms_and_api(self):
        self.assertFalse(
            REMOVED_PAYMENT_FIELDS & {field.name for field in MaintenanceOrder._meta.fields}
        )
        self.assertFalse(REMOVED_PAYMENT_FIELDS & set(MaintenanceActionForm().fields))
        api = APIClient()
        api.force_authenticate(self.actor)
        response = api.get(f"/api/maintenance/orders/{self.order.pk}/")
        self.assertEqual(response.status_code, 200)
        from netbox_oa.api.maintenance import TaskActionSerializer

        self.assertFalse(REMOVED_PAYMENT_FIELDS & set(TaskActionSerializer().fields))
        self.assertFalse(REMOVED_PAYMENT_FIELDS & set(response.data))
        self.assertEqual(
            set(dict(MaintenanceOrder._meta.get_field("payment_status").choices)),
            {
                "draft",
                "submitted",
                "requested",
            },
        )

    def test_old_material_states_are_rejected_by_service_api_and_web(self):
        self.reach("acceptance_apply")
        started_at = self.current().started_at
        api = APIClient()
        api.force_authenticate(self.actor)
        self.client.force_login(self.actor)
        for state in ("paper", "sealed", "awaiting_paper"):
            with self.subTest(state=state):
                with self.assertRaisesMessage(ValidationError, "材料状态仅支持"):
                    services.upload_document(
                        pk=self.order.pk,
                        actor=self.actor,
                        material="report",
                        state=state,
                        file=SimpleUploadedFile("report.pdf", b"scan"),
                    )
                response = api.post(
                    "/api/maintenance/documents/",
                    {
                        "order_id": self.order.pk,
                        "material": "report",
                        "state": state,
                        "file": SimpleUploadedFile("report.pdf", b"scan"),
                    },
                    format="multipart",
                )
                self.assertEqual(response.status_code, 400)
                response = self.client.post(
                    reverse(
                        "plugins:netbox_oa:maintenance_upload",
                        args=[self.order.pk],
                    ),
                    {
                        "material": "report",
                        "state": state,
                        "file": SimpleUploadedFile("report.pdf", b"scan"),
                    },
                )
                self.assertEqual(response.status_code, 302)
        self.assertFalse(self.order.documents.filter(material="report").exists())
        self.assertEqual(self.current().started_at, started_at)

    def test_old_batch_upload_flag_does_not_upload_materials(self):
        self.reach("acceptance_apply")
        self.client.force_login(self.actor)
        self.client.post(
            reverse("plugins:netbox_oa:maintenance_upload", args=[self.order.pk]),
            {
                "report_scans": "1",
                "file": SimpleUploadedFile("report.pdf", b"scan"),
            },
        )
        self.assertFalse(self.order.documents.filter(material="report").exists())


class CurrentFlowMigrationTests(TransactionTestCase):
    """Exercise a real 0019 -> 0020 upgrade without modifying instance snapshots."""

    def test_upgrade_removes_fields_and_obsolete_roles_but_keeps_groups_and_instances(self):
        old_target = [("netbox_oa", "0019_draft_applicant_signature")]
        new_target = [("netbox_oa", "0020_remove_maintenanceorder_contract_payment_id_and_more")]
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        try:
            old_apps = executor.loader.project_state(old_target).apps
            group = old_apps.get_model("users", "Group").objects.create(name="Migration handlers")
            user = old_apps.get_model("users", "User").objects.create(username="migration_owner")
            policy = old_apps.get_model("netbox_oa", "MaintenancePolicy").objects.create(
                name="Migration policy"
            )
            role_model = old_apps.get_model("netbox_oa", "MaintenanceRole")
            for role in (
                "reviewer",
                "department",
                "senior",
                "seal",
                "supplier",
                "finance",
                "owner",
            ):
                role_model.objects.create(policy=policy, role=role, group=group)
            snapshot = build_steps({role: group.pk for role, _ in ROLES}, 24)
            order = old_apps.get_model("netbox_oa", "MaintenanceOrder").objects.create(
                number="Y-GD20260001",
                policy=policy,
                requester=user,
                owner=user,
                status="active",
                current_position=1,
                steps_snapshot=snapshot,
            )
            reservation = old_apps.get_model(
                "netbox_oa", "MaintenanceNumberSequence"
            ).objects.create(
                year=2026,
                last_value=14,
            )
            snapshot = (
                old_apps.get_model("netbox_oa", "MaintenanceOrder")
                .objects.values_list("steps_snapshot", flat=True)
                .get(pk=order.pk)
            )
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            new_apps = executor.loader.project_state(new_target).apps
            remaining = new_apps.get_model("netbox_oa", "MaintenanceRole").objects.filter(
                policy_id=policy.pk
            )
            self.assertEqual(set(remaining.values_list("role", flat=True)), set(dict(ROLES)))
            self.assertTrue(
                new_apps.get_model("users", "Group").objects.filter(pk=group.pk).exists()
            )
            new_order = new_apps.get_model("netbox_oa", "MaintenanceOrder").objects.get(pk=order.pk)
            self.assertEqual(new_order.steps_snapshot, snapshot)
            self.assertEqual(new_order.current_position, 1)
            self.assertEqual(new_order.status, "active")
            self.assertEqual(
                new_apps.get_model("netbox_oa", "MaintenanceNumberSequence")
                .objects.get(pk=reservation.pk)
                .last_value,
                14,
            )
            with connection.cursor() as cursor:
                fields = {
                    column.name
                    for column in connection.introspection.get_table_description(
                        cursor,
                        new_order._meta.db_table,
                    )
                }
            self.assertFalse(REMOVED_PAYMENT_FIELDS & fields)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes("netbox_oa"))
