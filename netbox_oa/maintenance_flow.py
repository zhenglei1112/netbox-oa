"""Versioned maintenance process; copied into each order on submission."""

FLOW_NAME = "代维单位维护服务工单流程"

STAGES = (
    ("ordering", "工单下单"),
    ("acceptance", "工单验收"),
    ("payment", "付款申请"),
)
ROLES = (
    ("reviewer", "复核人"),
    ("department", "部门负责人"),
    ("senior", "上级负责人"),
    ("seal", "印章管理人"),
)
# Requirements may use "sealed" as a proof requirement, not a document state.
MATERIALS = (
    ("order", "维护服务工单"),
    ("report", "服务报告"),
    ("settlement", "服务结算单"),
    ("acceptance", "工单验收单"),
    ("notice", "付款通知书"),
    ("invoice", "电子发票"),
)
# Retired types remain readable on existing documents, but cannot be uploaded.
RETIRED_MATERIALS = (
    ("contract", "合同复印件"),
    ("verification", "发票验真结果"),
)
DOCUMENT_MATERIALS = MATERIALS + RETIRED_MATERIALS


def scan_fill_key(material):
    return "acceptance_apply" if material in {"report", "settlement"} else f"{material}_fill"


def build_steps(groups, sla_hours):
    nodes = []

    def add(stage, key, name, kind, role="owner", required=(), return_to=None, payment=""):
        nodes.append(
            {
                "stage": stage,
                "key": key,
                "name": name,
                "kind": kind,
                "role": role,
                "group_id": groups.get(role),
                "required": list(required),
                "return_to": return_to,
                "sla_hours": sla_hours,
                "payment": payment,
            }
        )

    add(
        "ordering",
        "order_correction",
        "工单修正",
        "submission",
        "requester",
        required=(("order", "electronic"),),
    )
    nodes[-1]["return_only"] = True
    add(
        "ordering",
        "order_review",
        "工单复核",
        "approval",
        "reviewer",
        required=(("order", "electronic"),),
        return_to="order_correction",
    )
    add(
        "ordering",
        "order_final",
        "工单部门审核",
        "approval",
        "department",
        required=(("order", "electronic"),),
        return_to="order_correction",
    )

    add(
        "ordering",
        "order_senior_seal",
        "工单公司审批",
        "approval",
        "senior",
        required=(("order", "electronic"),),
        return_to="order_correction",
    )
    add(
        "ordering",
        "order_seal",
        "服务工单用印",
        "action",
        "seal",
        required=(("order", "electronic"),),
    )
    add(
        "ordering",
        "order_sealed_upload",
        "上传用印后服务工单",
        "submission",
        "requester",
        required=(("order", "sealed"),),
    )
    for node in nodes[-3:]:
        node["scan_material"] = "order"

    requirements = (("acceptance", "electronic"), ("report", "scan"), ("settlement", "scan"))
    add("acceptance", "acceptance_apply", "验收申请", "submission", "requester",
        required=requirements)
    for key, name, role in (
        ("acceptance_review", "验收复核", "reviewer"),
        ("acceptance_department", "验收部门审核", "department"),
        ("acceptance_company", "验收公司审批", "senior"),
    ):
        add("acceptance", key, name, "approval", role, required=requirements,
            return_to="acceptance_apply")
    for node in nodes[-4:]:
        node["scan_materials"] = ["report", "settlement"]
    add("acceptance", "acceptance_seal", "验收用印", "action", "seal",
        required=(("acceptance", "electronic"),))
    add("acceptance", "acceptance_sealed_upload", "上传用印后验收单", "submission", "requester",
        required=(("acceptance", "sealed"),))
    for node in nodes[-2:]:
        node["scan_material"] = "acceptance"

    add(
        "payment",
        "payment_plan",
        "生成付款",
        "submission",
        "requester",
        required=(("acceptance", "sealed"), ("settlement", "scan"),
                  ("notice", "electronic"), ("invoice", "electronic")),
    )
    add(
        "payment",
        "payment_review",
        "付款部门审核",
        "approval",
        "department",
        required=(("acceptance", "sealed"), ("settlement", "scan"),
                  ("notice", "electronic"), ("invoice", "electronic")),
        return_to="payment_plan",
        payment="submitted",
    )
    for node in nodes:
        if node["key"] in {"payment_plan", "payment_review"}:
            node["scan_material"] = "acceptance"
            node["payment_contract_stage"] = True

    add(
        "payment",
        "payment_eip",
        "付款",
        "external",
        payment="requested",
    )
    return nodes
