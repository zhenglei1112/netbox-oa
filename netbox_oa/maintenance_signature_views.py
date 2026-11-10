import base64

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from qrcode.image.svg import SvgPathImage

from . import maintenance_signatures as signing
from .maintenance_services import SelfApprovalDenied
from .models import MaintenanceSavedSignature, MaintenanceSignature, MaintenanceTask
from .services import _allowed


def owned(pk, user):
    signature = get_object_or_404(
        MaintenanceSignature.objects.select_related("task__order", "signer", "document__order"),
        pk=pk,
    )
    if signature.signer_id != user.pk:
        raise PermissionDenied
    _allowed(type(signature.document.order), user, "view", signature.document.order_id)
    if signature.task_id:
        _allowed(type(signature.task), user, "view", signature.task_id)
    else:
        _allowed(type(signature.document.order), user, "change", signature.document.order_id)
    return signature


def signature_payload(signature, request):
    def url(suffix):
        return reverse(
            "plugins:netbox_oa:maintenance_signature" + suffix, kwargs={"pk": signature.pk}
        )

    mobile_url = request.build_absolute_uri(
        reverse(
            "plugins:netbox_oa:maintenance_signature_mobile",
            kwargs={"token": signing.mobile_token(signature)},
        )
    )
    qr = qrcode.make(mobile_url, image_factory=SvgPathImage).to_string()
    return {
        "qr": "data:image/svg+xml;base64," + base64.b64encode(qr).decode("ascii"),
        "status_url": url("_status"),
        "action_url": url(""),
        "image_url": url("_image"),
        "has_saved_signature": MaintenanceSavedSignature.objects.filter(
            signer_id=signature.signer_id
        ).exists(),
    }


def wants_json(request):
    return request.headers.get("Accept") == "application/json"


@method_decorator(never_cache, name="dispatch")
class SignatureCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        task = get_object_or_404(MaintenanceTask, pk=pk)
        try:
            signature = signing.create_session(pk=pk, actor=request.user)
        except SelfApprovalDenied as error:
            if wants_json(request):
                return JsonResponse({"error": str(error)}, status=403)
            raise
        except ValidationError as error:
            if wants_json(request):
                return JsonResponse({"error": "；".join(error.messages)}, status=400)
            messages.error(request, "；".join(error.messages))
            return redirect(task.order.get_absolute_url())
        if wants_json(request):
            return JsonResponse(signature_payload(signature, request))
        return redirect("plugins:netbox_oa:maintenance_signature", pk=signature.pk)


@method_decorator(never_cache, name="dispatch")
class SignatureView(LoginRequiredMixin, View):
    def get(self, request, pk):
        signature = owned(pk, request.user)
        mobile_url = request.build_absolute_uri(
            reverse(
                "plugins:netbox_oa:maintenance_signature_mobile",
                kwargs={"token": signing.mobile_token(signature)},
            )
        )
        qr = qrcode.make(mobile_url, image_factory=SvgPathImage).to_string()
        response = render(
            request,
            "netbox_oa/maintenance/signature.html",
            {
                "signature": signature,
                "qr": base64.b64encode(qr).decode("ascii"),
                "mobile_url": mobile_url,
                "has_saved_signature": MaintenanceSavedSignature.objects.filter(
                    signer=request.user
                ).exists(),
            },
        )
        # Authenticated desktop POSTs need a same-origin Referer for Django's
        # strict HTTPS CSRF check when the browser does not send Origin.
        # Cross-origin requests still omit Referer to protect the mobile grant.
        response["Referrer-Policy"] = "same-origin"
        return response

    def post(self, request, pk):
        signature = owned(pk, request.user)
        try:
            if request.POST.get("operation") == "cancel":
                signature = signing.cancel_signature(pk=pk, actor=request.user)
            elif request.POST.get("operation") == "reuse":
                signature = signing.reuse_signature(pk=pk, actor=request.user)
            else:
                signature = signing.confirm_signature(pk=pk, actor=request.user)
        except ValidationError as error:
            if wants_json(request):
                return JsonResponse({"error": "；".join(error.messages)}, status=400)
            messages.error(request, "；".join(error.messages))
            return redirect("plugins:netbox_oa:maintenance_signature", pk=pk)
        if wants_json(request):
            return JsonResponse({
                "state": "empty" if signature.cancelled else (
                    "confirmed" if signature.confirmed_at else "uploaded"
                )
            })
        if request.POST.get("operation") == "reuse":
            return redirect("plugins:netbox_oa:maintenance_signature", pk=pk)
        return redirect(signature.document.order.get_absolute_url())


@method_decorator(never_cache, name="dispatch")
class SignatureStatusView(LoginRequiredMixin, View):
    def get(self, request, pk):
        signature = owned(pk, request.user)
        state = "waiting"
        try:
            signing.validate_session(signature)
        except (ValidationError, PermissionDenied):
            state = "invalid"
        else:
            if signature.confirmed_at:
                state = "confirmed"
            elif signature.uploaded_at:
                state = "uploaded"
        return JsonResponse({"state": state})


@method_decorator(login_not_required, name="dispatch")
@method_decorator(never_cache, name="dispatch")
# This endpoint uses no ambient login/cookie authority: both the short-lived URL
# grant and the separately signed form credential must pass before capture.
@method_decorator(csrf_exempt, name="dispatch")
class MobileSignatureView(View):
    def get(self, request, token):
        return self.page(request, token)

    def post(self, request, token):
        return self.page(request, token, upload=True)

    def page(self, request, token, upload=False):
        context, status = {}, 200
        try:
            signature = signing.session_from_token(token)
            context["number"] = signature.document.order.number
            context["node"] = signature.task.name if signature.task_id else "申请人签名"
            context["success"] = bool(signature.uploaded_at)
            context["submission_token"] = signing.submission_token(signature)
            if upload:
                signing.validate_submission(signature, request.POST.get("submission_token"))
                signing.capture_signature(
                    token=token,
                    data_url=request.POST.get("image_data"),
                    save_signature=request.POST.get("save_signature") == "on",
                )
                context["success"] = True
        except (ValidationError, PermissionDenied) as error:
            context["error"] = (
                "；".join(error.messages)
                if isinstance(error, ValidationError)
                else "签字链接已失效。"
            )
            status = 403 if isinstance(error, PermissionDenied) else 400
        response = render(
            request, "netbox_oa/maintenance/signature_mobile.html", context, status=status
        )
        response["Referrer-Policy"] = "no-referrer"
        return response


@method_decorator(never_cache, name="dispatch")
class SignatureImageView(LoginRequiredMixin, View):
    def get(self, request, pk):
        signature = get_object_or_404(
            MaintenanceSignature.objects.select_related("task__order", "document__order"), pk=pk
        )
        task = signature.task
        _allowed(type(signature.document.order), request.user, "view", signature.document.order_id)
        if task:
            _allowed(type(task), request.user, "view", task.pk)
        _allowed(type(signature.document), request.user, "view", signature.document_id)
        published = (
            bool(signature.submitted_at)
            if task is None
            else task.status == "completed"
            and task.evidence.get("signature", {}).get("image_id") == pk
        )
        if not signature.image or (not published and request.user.pk != signature.signer_id):
            raise Http404
        response = HttpResponse(bytes(signature.image), content_type="image/png")
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        return response
