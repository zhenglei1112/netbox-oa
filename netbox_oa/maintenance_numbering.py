"""Server-issued numbers shared by the create page and its transactional save."""

import re

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import MaintenanceNumberReservation, MaintenanceNumberSequence, MaintenanceOrder

SALT = "netbox_oa.maintenance.number"


@transaction.atomic
def issue_number(actor):
    year = timezone.localdate().year
    MaintenanceNumberSequence.objects.get_or_create(year=year)
    sequence = MaintenanceNumberSequence.objects.select_for_update().get(year=year)
    while True:
        sequence.last_value += 1
        if sequence.last_value > 9999:
            raise ValidationError("本年度四位工单流水号已用完。")
        number = f"Y-GD{year}{sequence.last_value:04d}"
        if not (
            MaintenanceOrder.objects.filter(number=number).exists()
            or MaintenanceNumberReservation.objects.filter(number=number).exists()
        ):
            break
    sequence.save(update_fields=["last_value"])
    MaintenanceNumberReservation.objects.create(number=number, actor=actor)
    return number, signing.dumps({"number": number, "actor_id": actor.pk}, salt=SALT)


def number_from_token(token, actor, *, lock=False):
    try:
        data = signing.loads(token, salt=SALT, max_age=86400)
        number = data["number"]
        if data["actor_id"] != actor.pk or not re.fullmatch(r"Y-GD\d{8}", number):
            raise ValueError
        reservations = MaintenanceNumberReservation.objects
        if lock:
            reservations = reservations.select_for_update()
        if not reservations.filter(number=number, actor=actor, used=False).first():
            raise ValueError
        return number
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        raise ValidationError("工单编号凭证已失效，请重新打开新增页面。") from None
