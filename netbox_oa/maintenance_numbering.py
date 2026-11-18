"""Server-issued numbers shared by the create page and its transactional save."""

import re
import secrets
from string import ascii_uppercase

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import MaintenanceNumberReservation, MaintenanceOrder, MaintenanceOwnerCode

SALT = "netbox_oa.maintenance.number"


def owner_letter(actor):
    """Allocate once, in first-use order; unique constraints arbitrate competing requests."""
    for letter in ascii_uppercase:
        existing = MaintenanceOwnerCode.objects.filter(actor=actor).first()
        if existing:
            return existing.letter
        if MaintenanceOwnerCode.objects.filter(letter=letter).exists():
            continue
        try:
            with transaction.atomic():
                MaintenanceOwnerCode.objects.create(actor=actor, letter=letter)
        except IntegrityError:
            existing = MaintenanceOwnerCode.objects.filter(actor=actor).first()
            if existing:
                return existing.letter
            if not MaintenanceOwnerCode.objects.filter(letter=letter).exists():
                raise
        else:
            return letter
    existing = MaintenanceOwnerCode.objects.filter(actor=actor).first()
    if existing:
        return existing.letter
    raise ValidationError("线路主管编号字母已用完（最多支持 26 人），请联系管理员。")


@transaction.atomic
def issue_number(actor):
    letter = owner_letter(actor)
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
        for value in range(1, 100)
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
        raise ValidationError("本月工单编号已用完（每位线路主管最多预留 99 个），请联系管理员。")
    return number, signing.dumps({"number": number, "actor_id": actor.pk}, salt=SALT)


def number_from_token(token, actor, *, lock=False):
    try:
        data = signing.loads(token, salt=SALT, max_age=86400)
        number = data["number"]
        if data["actor_id"] != actor.pk or not re.fullmatch(
            r"Y-GD[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[1-9][0-9])[A-Z]", number
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
