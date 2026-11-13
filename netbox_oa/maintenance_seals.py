"""Private seal snapshots bound to the exact company-approved form version."""

import base64
import hashlib
from importlib.resources import files
from io import BytesIO

from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image

from .services import _save, _snapshot


def stamp_approved_form(order, task, actor):
    # A company approval with additional countersigners is not final until all agree.
    steps = order.steps_snapshot
    if task.position + 1 < len(steps) and steps[task.position + 1]["kind"] == "countersign":
        return
    position = task.position
    while position > 0 and steps[position]["kind"] == "countersign":
        position -= 1
    material = {
        "order_senior_seal": "order", "acceptance_company": "acceptance",
    }.get(steps[position]["key"])
    if not material:
        return
    approval = task if position == task.position else order.tasks.filter(
        position=position, status="completed",
    ).latest("pk")
    document = order.documents.select_for_update().get(
        pk=approval.evidence[material], material=material, kind="form", removed_at__isnull=True,
    )
    try:
        content = files("netbox_oa").joinpath("assets/maintenance-seal.png").read_bytes()
        with Image.open(BytesIO(content)) as image:
            if image.format != "PNG":
                raise ValueError("Expected PNG seal")
            image.verify()
    except (OSError, ValueError) as error:
        raise ValidationError("电子印章文件不可用，请联系管理员，审批尚未完成。") from error
    _snapshot(document)
    document.electronic_seal = {
        "image": "data:image/png;base64," + base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "date": timezone.localdate(task.completed_at).isoformat(),
        "approved_at": task.completed_at.isoformat(),
        "approved_by": actor.pk,
        "task_id": task.pk,
        "document_id": document.pk,
        "version": document.version,
    }
    _save(document)
    from .maintenance_services import _event

    _event(order, actor, "electronic_seal", "公司审批通过，自动加盖电子公章", task,
           document_id=document.pk, material=material, version=document.version,
           seal_sha256=document.electronic_seal["sha256"], date=document.electronic_seal["date"])
