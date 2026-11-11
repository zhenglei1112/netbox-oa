"""Render the actual sidebar template for isolated browser layout checks."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402

django.setup()

from django.template.loader import render_to_string  # noqa: E402

records = []
names = ["上传服务工单", "服务工单用印", "工单公司审批", "工单部门审核", "工单复核", "发起流程"]
for index in range(36):
    event = NS(
        pk=index + 1, task=NS(name=names[index % len(names)]),
        action="return" if index == 3 else "complete",
        created=datetime.now(timezone.utc),
        details={"comment": "完整办理意见", "external_reference": "EIP-123"},
    )
    records.append({
        "event": event, "actor_name": "赵明", "actor_full_name": "赵明",
        "short_label": "退回" if index == 3 else "同意", "symbol": "✓",
        "description": "完成办理",
    })

print(render_to_string("netbox_oa/maintenance/inbox_activity.html", {
    "detail_url": "?selected=1",
    "detail": {
        "order": NS(number="Y-TEST"), "current": NS(name="验收申请"),
        "waiting_for": "李明", "sidebar_events": records, "approval_events": records,
    },
}))
