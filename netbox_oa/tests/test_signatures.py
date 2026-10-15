import base64
import re
import sys
from contextvars import ContextVar
from datetime import timedelta
from io import BytesIO
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageDraw

from netbox_oa import maintenance_services as services
from netbox_oa import maintenance_signatures as signatures
from netbox_oa.tests import test_maintenance


def image_data(blank=False):
    image = Image.new("RGBA", (1000, 400), "white")
    if not blank:
        ImageDraw.Draw(image).line([(100, 100), (300, 200), (500, 100)], fill="black", width=6)
    output = BytesIO()
    image.save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


class SignatureTests(TestCase):
    setUp = test_maintenance.MaintenanceTests.setUp
    tearDown = test_maintenance.MaintenanceTests.tearDown
    start = test_maintenance.MaintenanceTests.start
    current = test_maintenance.MaintenanceTests.current
    upload = test_maintenance.MaintenanceTests.upload
    complete = test_maintenance.MaintenanceTests.complete
    reach = test_maintenance.MaintenanceTests.reach

    def session(self):
        self.reach("order_review")
        return signatures.create_session(pk=self.current().pk, actor=self.actor)

    def url(self, name, **kwargs):
        return reverse("plugins:netbox_oa:maintenance_signature" + name, kwargs=kwargs)

    def test_mobile_capture_csrf_desktop_confirm_and_print(self):
        session = self.session()
        desktop = Client()
        desktop.force_login(self.actor)
        page = desktop.get(self.url("", pk=session.pk))
        self.assertContains(page, "data:image/svg+xml;base64,")
        mobile_url = self.url("_mobile", token=signatures.mobile_token(session))
        phone = Client(enforce_csrf_checks=True)
        self.assertEqual(phone.post(mobile_url, {"image_data": image_data()}).status_code, 403)
        mobile_page = phone.get(mobile_url)
        self.assertContains(mobile_page, "data-signature-canvas-form")
        submit_token = re.search(
            r'name="submission_token" value="([^"]+)"', mobile_page.content.decode()
        ).group(1)
        phone.cookies.clear()
        response = phone.post(
            mobile_url,
            {
                "image_data": image_data(),
                "submission_token": submit_token,
            },
        )
        self.assertContains(response, "签名已提交")
        self.assertEqual(phone.get(self.url("_image", pk=session.pk)).status_code, 302)
        self.assertEqual(
            desktop.get(self.url("_status", pk=session.pk)).json()["state"], "uploaded"
        )
        self.assertEqual(desktop.post(self.url("", pk=session.pk)).status_code, 302)
        task = self.current()
        self.complete()
        task.refresh_from_db()
        self.assertEqual(task.evidence["signature"]["image_id"], session.pk)
        response = desktop.get(self.url("_image", pk=session.pk))
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("no-store", response["Cache-Control"])
        printed = desktop.get(
            reverse("plugins:netbox_oa:maintenance_print", kwargs={"pk": session.document_id})
        )
        self.assertContains(printed, self.url("_image", pk=session.pk))

    def test_https_submission_without_cookie_or_referer(self):
        session = self.session()
        url = self.url("_mobile", token=signatures.mobile_token(session))
        phone = Client(enforce_csrf_checks=True)
        page = phone.get(url, secure=True)
        token = re.search(r'name="submission_token" value="([^"]+)"', page.content.decode()).group(
            1
        )
        phone.cookies.clear()
        response = phone.post(
            url, {"image_data": image_data(), "submission_token": token}, secure=True
        )
        self.assertContains(response, "签名已提交")

    def test_submission_credential_cannot_be_replaced_or_reused_for_another_session(self):
        session = self.session()
        url = self.url("_mobile", token=signatures.mobile_token(session))
        phone = Client(enforce_csrf_checks=True)
        page = phone.get(url)
        token = re.search(r'name="submission_token" value="([^"]+)"', page.content.decode()).group(
            1
        )
        self.assertEqual(
            phone.post(
                url, {"image_data": image_data(), "submission_token": token + "x"}
            ).status_code,
            403,
        )
        other = signatures.create_session(pk=session.task_id, actor=self.actor)
        other_url = self.url("_mobile", token=signatures.mobile_token(other))
        self.assertEqual(
            phone.post(
                other_url, {"image_data": image_data(), "submission_token": token}
            ).status_code,
            403,
        )
        self.assertEqual(phone.get(self.url("_image", pk=session.pk)).status_code, 302)

    def test_expired_tampered_and_regenerated_tokens(self):
        session = self.session()
        token = signatures.mobile_token(session)
        with self.assertRaises(ValidationError):
            signatures.capture_signature(token=token + "x", data_url=image_data())
        signatures.create_session(pk=session.task_id, actor=self.actor)
        with self.assertRaises(ValidationError):
            signatures.capture_signature(token=token, data_url=image_data())
        session = signatures.create_session(pk=session.task_id, actor=self.actor)
        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save()
        with self.assertRaises(ValidationError):
            signatures.capture_signature(
                token=signatures.mobile_token(session), data_url=image_data()
            )
        signatures.cancel_signature(pk=session.pk, actor=self.actor)
        self.complete()

    def test_single_use_and_empty_or_invalid_images(self):
        session = self.session()
        token = signatures.mobile_token(session)
        for data in [
            image_data(blank=True),
            "data:image/png;base64,xxx",
            "evil",
            "data:image/png;base64," + "a" * 350000,
        ]:
            with self.assertRaises(ValidationError):
                signatures.capture_signature(token=token, data_url=data)
        signatures.capture_signature(token=token, data_url=image_data())
        with self.assertRaises(ValidationError):
            signatures.capture_signature(token=token, data_url=image_data())

    def test_other_actor_cannot_confirm_or_view_session(self):
        from django.contrib.auth import get_user_model

        session = self.session()
        other = get_user_model().objects.create_superuser(username="other", password="test")
        with self.assertRaises(PermissionDenied):
            signatures.confirm_signature(pk=session.pk, actor=other)
        client = Client()
        client.force_login(other)
        self.assertEqual(client.get(self.url("", pk=session.pk)).status_code, 403)
        self.assertEqual(client.get(self.url("_image", pk=session.pk)).status_code, 404)

    def test_only_ordering_nodes_support_signatures(self):
        self.reach("report_fill")
        with self.assertRaises(ValidationError):
            signatures.create_session(pk=self.current().pk, actor=self.actor)

    def test_widget_json_add_delete_and_approve_uploaded_signature(self):
        self.reach("order_review")
        desktop = Client(enforce_csrf_checks=True)
        desktop.force_login(self.actor)
        page = desktop.get(self.order.get_absolute_url())
        self.assertContains(page, "添加签名")
        self.assertNotContains(page, "查看签名 / 二维码")
        csrf = desktop.cookies["csrftoken"].value
        create_url = self.url("_create", pk=self.current().pk)
        self.assertEqual(desktop.post(create_url, HTTP_ACCEPT="application/json").status_code, 403)
        response = desktop.post(
            create_url, {"csrfmiddlewaretoken": csrf}, HTTP_ACCEPT="application/json"
        )
        data = response.json()
        self.assertTrue(data["qr"].startswith("data:image/svg+xml;base64,"))
        session = self.current().signatures.latest("pk")
        with self.assertRaises(ValidationError):
            self.complete()
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        self.assertEqual(desktop.get(data["status_url"]).json()["state"], "uploaded")
        self.assertContains(desktop.get(self.order.get_absolute_url()), 'aria-label="删除签名"')
        deleted = desktop.post(
            data["action_url"],
            {
                "csrfmiddlewaretoken": csrf,
                "operation": "cancel",
            },
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(deleted.json()["state"], "empty")
        session.refresh_from_db()
        self.assertTrue(session.cancelled)
        response = desktop.post(
            create_url, {"csrfmiddlewaretoken": csrf}, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200)
        replacement = self.current().signatures.latest("pk")
        signatures.capture_signature(
            token=signatures.mobile_token(replacement), data_url=image_data()
        )
        task = self.current()
        self.complete()
        task.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(task.evidence["signature"]["image_id"], replacement.pk)
        self.assertIsNotNone(replacement.confirmed_at)

    def test_desktop_confirmation_still_requires_csrf(self):
        session = self.session()
        desktop = Client(enforce_csrf_checks=True)
        desktop.force_login(self.actor)
        response = desktop.post(self.url("", pk=session.pk))
        self.assertEqual(response.status_code, 403)
        session.refresh_from_db()
        self.assertIsNone(session.confirmed_at)

    @override_settings(CSRF_COOKIE_HTTPONLY=True, CSRF_COOKIE_NAME="nb_csrftoken")
    def test_desktop_confirmation_keeps_same_origin_referer_for_https_csrf(self):
        session = self.session()
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        desktop = Client(enforce_csrf_checks=True)
        desktop.force_login(self.actor)
        url = self.url("", pk=session.pk)
        page = desktop.get(url, secure=True)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        token = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)
        response = desktop.post(
            url,
            {"csrfmiddlewaretoken": token},
            secure=True,
            HTTP_REFERER="https://testserver" + url,
        )
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertIsNotNone(session.confirmed_at)

    def test_desktop_cancel_keeps_csrf_protection(self):
        session = self.session()
        desktop = Client(enforce_csrf_checks=True)
        desktop.force_login(self.actor)
        url = self.url("", pk=session.pk)
        self.assertEqual(desktop.post(url, {"operation": "cancel"}).status_code, 403)
        page = desktop.get(url, secure=True)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        token = re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()
        ).group(1)
        self.assertEqual(
            desktop.post(
                url,
                {"csrfmiddlewaretoken": token, "operation": "cancel"},
                secure=True,
                HTTP_REFERER="https://testserver" + url,
            ).status_code,
            302,
        )
        session.refresh_from_db()
        self.assertTrue(session.cancelled)

    def test_mobile_capture_uses_signer_for_netbox_audit_and_restores_request(self):
        from django.contrib.auth.models import AnonymousUser
        from django.db.models.signals import post_save

        from netbox_oa.models import MaintenanceEvent

        session = self.session()
        original = SimpleNamespace(user=AnonymousUser(), id="mobile-request")
        context = ModuleType("netbox.context")
        context.current_request = ContextVar("test_request", default=original)
        recorded = []

        def audit(sender, instance, **kwargs):
            request = context.current_request.get()
            # NetBox change logging assigns request.user to a real user FK.
            MaintenanceEvent(actor=request.user)
            recorded.append((request.user.pk, request.id))

        post_save.connect(audit, sender=MaintenanceEvent, weak=False)
        try:
            with patch.dict(sys.modules, {"netbox.context": context}):
                signatures.capture_signature(
                    token=signatures.mobile_token(session), data_url=image_data()
                )
        finally:
            post_save.disconnect(audit, sender=MaintenanceEvent)
        self.assertEqual(recorded, [(self.actor.pk, original.id)])
        self.assertIs(context.current_request.get(), original)
        self.assertFalse(original.user.is_authenticated)
        with patch.dict(sys.modules, {"netbox.context": context}):
            with self.assertRaises(RuntimeError):
                with signatures.capture_audit_actor(self.actor):
                    self.assertEqual(context.current_request.get().user, self.actor)
                    raise RuntimeError("save failed")
        self.assertIs(context.current_request.get(), original)

    def test_return_and_transfer_invalidate_mobile_grants(self):
        from django.contrib.auth import get_user_model

        from netbox_oa import maintenance_services as services

        session = self.session()
        token = signatures.mobile_token(session)
        other = get_user_model().objects.create_superuser(username="transfer", password="test")
        services.transfer_task(
            pk=session.task_id, actor=self.actor, user_id=other.pk, comment="转办"
        )
        with self.assertRaises(ValidationError):
            signatures.capture_signature(token=token, data_url=image_data())
        session = signatures.create_session(pk=session.task_id, actor=other)
        services.return_task(pk=session.task_id, actor=other, comment="退回补正")
        with self.assertRaises(ValidationError):
            signatures.capture_signature(
                token=signatures.mobile_token(session), data_url=image_data()
            )

    def test_department_review_signature_and_confirmation_expiry(self):
        self.reach("order_final")
        session = signatures.create_session(pk=self.current().pk, actor=self.actor)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        signatures.confirm_signature(pk=session.pk, actor=self.actor)
        session.expires_at = timezone.now() - timedelta(seconds=1)
        session.save(update_fields=["expires_at"])
        task = self.current()
        self.complete()
        task.refresh_from_db()
        self.assertEqual(task.evidence["signature"]["image_id"], session.pk)

    def test_applicant_signature_required_in_ui_api_and_print(self):
        from rest_framework.test import APIClient

        self.start()
        document = self.upload("order")
        self.client.force_login(self.actor)
        page = self.client.get(self.order.get_absolute_url())
        self.assertContains(page, "data-signature-widget")
        self.assertContains(page, "申请人须完成手写签名")
        self.assertEqual(document.form_data["signature"], "")
        with self.assertRaisesMessage(ValidationError, "请先由申请人完成手写签名"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        api = APIClient()
        api.force_authenticate(self.actor)
        url = f"/api/maintenance/tasks/{self.current().pk}/complete/"
        self.assertEqual(api.post(url, {}, format="json").status_code, 400)
        session = signatures.create_session(pk=self.current().pk, actor=self.actor)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        response = api.post(url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["evidence"]["signature"]["image_id"], session.pk)
        self.assertEqual(self.current().name, "工单初审")
        printed = self.client.get(
            reverse("plugins:netbox_oa:maintenance_print", args=[document.pk])
        )
        self.assertContains(printed, "申请人签字")
        self.assertContains(printed, self.url("_image", pk=session.pk))

    def test_applicant_signature_must_be_replaced_after_revision_or_deletion(self):
        self.start()
        self.upload("order")
        session = signatures.create_session(pk=self.current().pk, actor=self.actor)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        self.upload("order")
        with self.assertRaisesMessage(ValidationError, "表单版本已变化"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        session = signatures.create_session(pk=self.current().pk, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "请先完成手机签名"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        signatures.cancel_signature(pk=session.pk, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "请先由申请人完成手写签名"):
            services.complete_task(pk=self.current().pk, actor=self.actor)
        self.complete()
        self.assertEqual(self.current().name, "工单初审")

    def test_applicant_alone_signs_after_existing_initial_task_migration(self):
        from importlib import import_module

        from django.apps import apps
        from django.contrib.auth import get_user_model

        from netbox_oa.models import MaintenanceOrder

        self.start()
        self.upload("order")
        applicant = get_user_model().objects.create_user(
            username="actual_applicant", password="test"
        )
        for model, action in (
            ("maintenancetask", "change"),
            ("maintenanceorder", "view"),
            ("maintenancedocument", "view"),
        ):
            test_maintenance.MaintenanceTests.grant(self, applicant, model, action)
        order = MaintenanceOrder.objects.get(pk=self.order.pk)
        order.requester = applicant
        order.steps_snapshot[order.current_position]["role"] = "owner"
        order.save()
        migration = import_module("netbox_oa.migrations.0018_requester_order_start")
        migration.assign_applicant(
            apps, SimpleNamespace(connection=SimpleNamespace(alias="default"))
        )
        self.assertEqual(self.current().assigned_user_id, applicant.pk)
        with self.assertRaisesMessage(PermissionDenied, "申请人本人签名"):
            signatures.create_session(pk=self.current().pk, actor=self.actor)
        session = signatures.create_session(pk=self.current().pk, actor=applicant)
        signatures.capture_signature(token=signatures.mobile_token(session), data_url=image_data())
        task = services.complete_task(pk=self.current().pk, actor=self.actor)
        self.assertEqual(task.evidence["signature"]["user_id"], applicant.pk)
        self.assertEqual(task.evidence["signature"]["image_id"], session.pk)
        self.assertEqual(task.decided_by_id, self.actor.pk)
