"""Short-lived mobile capture grants; authenticated desktop confirmation and private images."""

import base64
import binascii
from contextlib import contextmanager
from copy import copy
from datetime import timedelta
from io import BytesIO
from secrets import compare_digest

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .models import MaintenanceSavedSignature, MaintenanceSignature
from .services import _allowed

SIGNING_SALT = "netbox_oa.mobile_signature"
SIGNING_SECONDS = 900
SUBMISSION_SALT = "netbox_oa.mobile_signature_submission"
SIGNING_NODES = {"order_correction", "order_review", "order_final"}


@contextmanager
def capture_audit_actor(actor):
    """Attribute only the validated capture writes to the grant's signer.

    NetBox's change logging reads current_request, independently of event.actor.
    Use a scoped copy so the phone remains anonymous and no login is performed.
    """
    try:
        from netbox.context import current_request
    except ModuleNotFoundError as error:
        if error.name != "netbox.context":
            raise
        # The standalone test harness does not provide NetBox's request context.
        yield
        return
    request = current_request.get()
    if request is None:
        yield
        return
    audit_request = copy(request)
    audit_request.user = actor
    token = current_request.set(audit_request)
    try:
        yield
    finally:
        current_request.reset(token)


def order_form(order):
    latest = order.documents.filter(
        material="order", removed_at__isnull=True
    ).order_by("-version").first()
    source = latest.source_document if latest and latest.kind == "scan" else latest
    if not source or source.kind != "form":
        raise ValidationError("请先保存维护服务工单表单。")
    return source


def validate_session(session):
    from .maintenance_services import task_is_assigned

    task, actor = session.task, session.signer
    order = session.document.order
    if task is None:
        if (
            session.cancelled
            or session.submitted_at
            or timezone.now() >= session.expires_at
            or not actor.is_active
            or order.status != "draft"
            or actor.pk != order.requester_id
            or order_form(order).pk != session.document_id
        ):
            raise ValidationError("签字链接已失效，请重新添加签名。")
        _allowed(type(order), actor, "view", order.pk)
        _allowed(type(order), actor, "change", order.pk)
        _allowed(type(session.document), actor, "view", session.document_id)
        return
    if (
        session.cancelled
        or timezone.now() >= session.expires_at
        or not actor.is_active
        or order.status != "active"
        or task.status != "pending"
        or task.position != order.current_position
        or not (0 <= task.position < len(order.steps_snapshot))
        or order.steps_snapshot[task.position]["key"] not in SIGNING_NODES
        or (
            order.steps_snapshot[task.position]["key"] in {"order_correction"}
            and actor.pk != order.requester_id
        )
        or not task_is_assigned(task, actor)
        or order_form(order).pk != session.document_id
    ):
        raise ValidationError("签字链接已失效，请在电脑端重新生成二维码。")
    _allowed(type(task), actor, "change", task.pk)
    _allowed(type(order), actor, "view", order.pk)
    _allowed(type(session.document), actor, "view", session.document_id)


def mobile_token(session):
    return signing.dumps({"id": session.pk, "key": str(session.key)}, salt=SIGNING_SALT)


def submission_token(session):
    """Separate, form-only credential for the cookie-independent capture endpoint."""
    return signing.dumps({"id": session.pk, "key": str(session.key)}, salt=SUBMISSION_SALT)


def validate_submission(session, token):
    try:
        data = signing.loads(token or "", salt=SUBMISSION_SALT, max_age=SIGNING_SECONDS)
        if data["id"] != session.pk or not compare_digest(str(data["key"]), str(session.key)):
            raise ValueError
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise PermissionDenied("签名提交凭证无效，请重新打开签字页面。") from None


def session_from_token(token, *, lock=False):
    try:
        data = signing.loads(token, salt=SIGNING_SALT, max_age=SIGNING_SECONDS)
        query = MaintenanceSignature.objects.select_related(
            "task__order", "signer", "document__order"
        )
        if lock:
            # Lock the order first, matching every workflow mutation's lock order.
            order_id = query.values_list("document__order_id", flat=True).get(pk=data["id"])
            from .models import MaintenanceOrder

            MaintenanceOrder.objects.select_for_update().get(pk=order_id)
            query = query.select_for_update(of=("self",))
        session = query.get(pk=data["id"])
        if not compare_digest(str(session.key), str(data["key"])):
            raise ValueError
    except (
        signing.BadSignature,
        KeyError,
        TypeError,
        ValueError,
        MaintenanceSignature.DoesNotExist,
    ):
        raise ValidationError("签字链接无效或已过期。") from None
    validate_session(session)
    return session


@transaction.atomic
def create_session(*, pk, actor):
    from .maintenance_services import _begin_task, _event, _task

    order, task = _task(pk, actor)
    if order.steps_snapshot[task.position]["key"] not in SIGNING_NODES:
        raise ValidationError("仅工单修正、工单初审和工单部门审核支持手机签字。")
    if (
        order.steps_snapshot[task.position]["key"] in {"order_correction"}
        and actor.pk != order.requester_id
    ):
        raise PermissionDenied("工单修正必须由申请人本人签名。")
    document = order_form(order)
    _allowed(type(document), actor, "view", document.pk)
    _begin_task(task, actor)
    task.signatures.filter(cancelled=False).update(cancelled=True)
    session = MaintenanceSignature.objects.create(
        task=task,
        document=document,
        signer=actor,
        expires_at=timezone.now() + timedelta(seconds=SIGNING_SECONDS),
    )
    _event(order, actor, "signature", "发起手机手写签名", task)
    return session


def normalize_image(data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        raise ValidationError("请提交 PNG 格式的手写签名。")
    if len(data_url) > 350000:
        raise ValidationError("签名图片不能超过 256 KB。")
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG" or not (
                64 <= image.width <= 1600 and 32 <= image.height <= 800
            ):
                raise ValueError
            image.load()
            rgba = image.convert("RGBA")
            # Keep only dark ink, stripping backgrounds and unrelated image metadata.
            ink = Image.new("RGBA", rgba.size)
            pixels = [
                (0, 0, 0, alpha) if alpha > 20 and min(r, g, b) < 200 else (0, 0, 0, 0)
                for r, g, b, alpha in rgba.getdata()
            ]
            if sum(pixel[3] > 0 for pixel in pixels) < 20:
                raise ValueError
            ink.putdata(pixels)
            box = ink.getbbox()
            output = BytesIO()
            ink.crop(box).save(output, format="PNG")
            return output.getvalue()
    except (
        binascii.Error,
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        raise ValidationError("签名为空或图片无效，请清空后重新书写名字。") from None


@transaction.atomic
def capture_signature(*, token, data_url, save_signature=False):
    from .maintenance_services import _event

    session = session_from_token(token, lock=True)
    if session.uploaded_at or session.confirmed_at:
        raise ValidationError("该签字链接已使用，请在电脑端重新生成二维码。")
    session.image = normalize_image(data_url)
    session.uploaded_at = timezone.now()
    with capture_audit_actor(session.signer):
        session.save(update_fields=["image", "uploaded_at"])
        if save_signature:
            MaintenanceSavedSignature.objects.update_or_create(
                signer=session.signer, defaults={"image": session.image}
            )
        _event(
            session.document.order, session.signer, "signature", "手机手写签名已上传", session.task
        )
    return session


@transaction.atomic
def reuse_signature(*, pk, actor):
    """Copy only this authenticated signer's saved ink into a fresh capture."""
    from .maintenance_services import _event

    original = MaintenanceSignature.objects.get(pk=pk)
    if original.signer_id != actor.pk:
        raise PermissionDenied
    session = session_from_token(mobile_token(original), lock=True)
    if session.uploaded_at or session.confirmed_at:
        raise ValidationError("本次已有签名，请先删除后重新添加。")
    saved = MaintenanceSavedSignature.objects.filter(signer=actor).first()
    if not saved or not saved.image:
        raise ValidationError("暂无已保存签名，请使用手机扫码签名。")
    session.image = bytes(saved.image)
    session.uploaded_at = timezone.now()
    session.save(update_fields=["image", "uploaded_at"])
    _event(session.document.order, actor, "signature", "使用已保存签名", session.task)
    return session


@transaction.atomic
def confirm_signature(*, pk, actor):
    from .maintenance_services import _event, _task

    task_id = MaintenanceSignature.objects.values_list("task_id", flat=True).get(pk=pk)
    order, task = _task(task_id, actor) if task_id else (None, None)
    if not task_id:
        from .models import MaintenanceOrder

        order_id = MaintenanceSignature.objects.values_list("document__order_id", flat=True).get(
            pk=pk
        )
        MaintenanceOrder.objects.select_for_update().get(pk=order_id)
    session = (
        MaintenanceSignature.objects.select_for_update(of=("self",))
        .select_related("task__order", "signer", "document__order")
        .get(pk=pk)
    )
    if session.signer_id != actor.pk:
        raise PermissionDenied("只能确认自己的手写签名。")
    validate_session(session)
    order = session.document.order
    if not task:
        type(order).objects.select_for_update().get(pk=order.pk)
    if not session.image or not session.uploaded_at:
        raise ValidationError("请先在手机上提交手写签名。")
    if not session.confirmed_at:
        session.confirmed_at = timezone.now()
        session.save(update_fields=["confirmed_at"])
        _event(order, actor, "signature", "确认使用手写签名", task)
    return session


def signature_for_approval(task, actor, *, required=False):
    signer = task.order.requester if required else actor
    session = task.signatures.filter(signer=signer, cancelled=False).order_by("-pk").first()
    if session is None:
        if required:
            raise ValidationError("请先由申请人完成手写签名后再提交工单。")
        return None
    if not session.uploaded_at or not session.image:
        raise ValidationError("请先完成手机签名，或删除本次签名请求。")
    if order_form(task.order).pk != session.document_id:
        raise ValidationError("表单版本已变化，请重新签名。")
    # Clicking the approval action confirms the displayed handwriting. Mobile
    # upload alone still cannot approve a task or publish a printable signature.
    if not session.confirmed_at:
        session.confirmed_at = timezone.now()
        session.save(update_fields=["confirmed_at"])
    return session


@transaction.atomic
def cancel_signature(*, pk, actor):
    from .maintenance_services import _event, _task

    session = MaintenanceSignature.objects.select_related("task").get(pk=pk)
    order = session.document.order
    if session.task_id:
        order, task = _task(session.task_id, actor)
    else:
        order = type(order).objects.select_for_update().get(pk=order.pk)
        task = None
        _allowed(type(order), actor, "change", order.pk)
        if order.status != "draft" or session.submitted_at:
            raise ValidationError("已提交的申请人签名不能删除。")
    if session.signer_id != actor.pk:
        raise PermissionDenied("只能取消自己的签名请求。")
    session.cancelled = True
    session.save(update_fields=["cancelled"])
    _event(order, actor, "signature", "取消手机签名请求", task)
    return session


@transaction.atomic
def create_draft_session(*, pk, actor):
    from .maintenance_services import _event, _order

    order = _order(pk, actor)
    if order.status != "draft" or actor.pk != order.requester_id:
        raise PermissionDenied("草稿必须由申请人本人签名。")
    document = order_form(order)
    _allowed(type(document), actor, "view", document.pk)
    MaintenanceSignature.objects.filter(
        document__order=order, task__isnull=True, submitted_at__isnull=True, cancelled=False
    ).update(cancelled=True)
    session = MaintenanceSignature.objects.create(
        document=document,
        signer=actor,
        expires_at=timezone.now() + timedelta(seconds=SIGNING_SECONDS),
    )
    _event(order, actor, "signature", "发起申请人手写签名")
    return session


def draft_signature_for_submission(order):
    session = (
        MaintenanceSignature.objects.filter(
            document__order=order,
            task__isnull=True,
            signer=order.requester,
            cancelled=False,
            submitted_at__isnull=True,
        )
        .order_by("-pk")
        .first()
    )
    if not session or not session.image or not session.uploaded_at:
        raise ValidationError("请先由申请人完成手写签名后提交审批。")
    if not session.signer.is_active or session.document_id != order_form(order).pk:
        raise ValidationError("表单版本已变化，请重新签名。")
    session.confirmed_at = session.confirmed_at or timezone.now()
    session.submitted_at = timezone.now()
    session.save(update_fields=["confirmed_at", "submitted_at"])
    return session
