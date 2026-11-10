"""Server-issued numbers shared by the create page and its transactional save."""

import re
import secrets

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import MaintenanceNumberReservation, MaintenanceOrder

SALT = "netbox_oa.maintenance.number"


@transaction.atomic
def issue_number(actor):
    today = timezone.localdate()
    prefix = f"Y-GD{today.year % 100:02d}{today.month:02d}"
    used = set(MaintenanceNumberReservation.objects.filter(
        number__startswith=prefix
    ).values_list("number", flat=True))
    used.update(MaintenanceOrder.objects.filter(
        number__startswith=prefix
    ).values_list("number", flat=True))
    available = [
        f"{prefix}{value:02d}{letter}"
        for letter in "ABCDE" for value in range(1, 100)
        if f"{prefix}{value:02d}{letter}" not in used
    ]
    while available:
        number = secrets.choice(available)
        try:
            # A savepoint keeps a competing reservation from breaking the caller's transaction.
            with transaction.atomic():
                MaintenanceNumberReservation.objects.create(number=number, actor=actor)
        except IntegrityError:
            if not MaintenanceNumberReservation.objects.filter(number=number).exists():
                raise
            available.remove(number)
        else:
            break
    else:
        raise ValidationError("本月工单编号已用完（最多预留 495 个），请联系管理员。")
    return number, signing.dumps({"number": number, "actor_id": actor.pk}, salt=SALT)


def number_from_token(token, actor, *, lock=False):
    try:
        data = signing.loads(token, salt=SALT, max_age=86400)
        number = data["number"]
        if data["actor_id"] != actor.pk or not re.fullmatch(
            r"Y-GD[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[1-9][0-9])[A-E]", number
        ):
            raise ValueError
        reservations = MaintenanceNumberReservation.objects
        if lock:
            reservations = reservations.select_for_update()
        if not reservations.filter(number=number, actor=actor, used=False).first():
            raise ValueError
        return number
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise ValidationError("工单编号凭证已失效，请重新打开新增页面。") from None


def draft_number(base_number, type_snapshot):
    """The reservation owns the month/tail; the draft may change its type character."""
    code = type_snapshot.get("code", "")
    return f"{base_number[:8]}{code}{base_number[8:]}"
