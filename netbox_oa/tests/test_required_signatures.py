"""Every signing step enforces handwriting at the domain and HTTP boundaries."""

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from netbox_oa import maintenance_services as services
from netbox_oa.tests import test_maintenance
from netbox_oa.tests.test_maintenance import acting_user


class RequiredSignatureTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    complete = test_maintenance.MaintenanceTests.complete
    upload = test_maintenance.MaintenanceTests.upload

    def test_five_steps_reject_missing_signatures_and_publish_signed_evidence(self):
        self.start()
        for key in ("order_review", "order_final", "acceptance_apply",
                    "acceptance_review", "acceptance_department"):
            while self.order.steps_snapshot[self.current().position]["key"] != key:
                self.complete()
            if key == "acceptance_apply":
                self.upload("acceptance")
                self.upload("report", "scan")
                self.upload("settlement", "scan")
            task = self.current()
            actor = acting_user(self)
            with self.subTest(key=key):
                with self.assertRaisesMessage(ValidationError, "手写签名"):
                    services.complete_task(pk=task.pk, actor=actor, comment="同意")
                api = APIClient()
                api.force_authenticate(actor)
                response = api.post(f"/api/maintenance/tasks/{task.pk}/complete/", {})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(services.quick_complete_comment(task), "")
                task.refresh_from_db()
                self.assertEqual(task.status, "pending")
                completed = self.complete()
                self.assertTrue(completed.evidence["signature"]["image_id"])
                self.assertEqual(completed.evidence["signature"]["user_id"], actor.pk)

    def test_company_approvals_have_no_signing_widget_and_can_complete_without_signature(self):
        self.start()
        for key in ("order_senior_seal", "acceptance_company"):
            while self.order.steps_snapshot[self.current().position]["key"] != key:
                self.complete()
            task = self.current()
            self.client.force_login(acting_user(self))
            response = self.client.get(self.order.get_absolute_url())
            self.assertFalse(response.context["detail"]["can_mobile_sign"])
            self.assertFalse(task.signatures.exists())
            completed = services.complete_task(pk=task.pk, actor=acting_user(self))
            self.assertNotIn("signature", completed.evidence)
