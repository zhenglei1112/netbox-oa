"""Versioned maintenance process; copied into each order on submission."""

FLOW_NAME = "代维单位维护服务工单流程"

STAGES = (
    ("ordering", "工单下单"),
    ("report", "服务报告"),
    ("settlement", "服务结算"),
    ("payment", "付款申请"),
)
ROLES = (
    ("reviewer", "初审人"),
    ("department", "部门负责人"),
    ("senior", "上级负责人"),
    ("seal", "印章管理人"),
    ("supplier", "服务单位经办组"),
    ("finance", "财务经办组"),
)
# Requirements may use "sealed" as a proof requirement, not a document state.
# Old snapshots containing "paper" remain supported by the service layer.
MATERIALS = (
    ("order", "维护服务工单"),
    ("report", "服务报告"),
    ("settlement", "服务结算单"),
    ("notice", "付款通知书"),
    ("invoice", "电子发票"),
    ("contract", "合同复印件"),
    ("verification", "发票验真结果"),
)


def build_steps(groups, sla_hours):
    nodes = []

    def add(stage, key, name, kind, role="owner", required=(), return_to=None, payment=""):
        if kind == "approval" and not required:
            material = {"ordering": "order", "report": "report", "settlement": "settlement"}.get(
                stage
            )
            if material:
                required = ((material, "scan" if key == "settlement_final" else "electronic"),)
            else:
                required = (
                    ("notice", "electronic"),
                    ("invoice", "electronic"),
                    ("contract", "electronic"),
                )
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
        "order_fill",
        "发起工单",
        "submission",
        "requester",
        required=(("order", "electronic"),),
    )
    add("ordering", "order_review", "工单初审", "approval", "reviewer", return_to="order_fill")
    add("ordering", "order_final", "工单部门审核", "approval", "department", return_to="order_fill")

    add(
        "report",
        "report_fill",
        "服务报告",
        "submission",
        "requester",
        required=(("report", "scan"),),
    )
    add(
        "report",
        "report_department_seal",
        "服务报告用印部门审核",
        "approval",
        "department",
        required=(("report", "scan"),),
        return_to="report_fill",
    )
    add(
        "report",
        "report_senior_seal",
        "服务报告用印公司审批",
        "approval",
        "senior",
        required=(("report", "scan"),),
        return_to="report_fill",
    )
    add(
        "report",
        "report_seal",
        "服务报告用印",
        "action",
        "seal",
        required=(("report", "sealed"),),
    )
    for node in nodes:
        if node["stage"] == "report":
            node["report_scans"] = True

    add(
        "settlement",
        "settlement_fill",
        "服务结算单",
        "submission",
        "requester",
        required=(("settlement", "scan"),),
    )
    add(
        "settlement",
        "settlement_department_seal",
        "服务结算单用印部门审核",
        "approval",
        "department",
        required=(("settlement", "scan"),),
        return_to="settlement_fill",
    )
    add(
        "settlement",
        "settlement_senior_seal",
        "服务结算单用印公司审批",
        "approval",
        "senior",
        required=(("settlement", "scan"),),
        return_to="settlement_fill",
    )
    add(
        "settlement",
        "settlement_seal",
        "服务结算单用印",
        "action",
        "seal",
        required=(("settlement", "sealed"),),
    )
    for node in nodes:
        if node["stage"] == "settlement":
            node["scan_material"] = "settlement"
    add(
        "payment",
        "payment_plan",
        "生成付款",
        "submission",
        "requester",
        required=(("settlement", "sealed"),),
    )
    add(
        "payment",
        "payment_review",
        "付款部门审核",
        "approval",
        "department",
        required=(("settlement", "sealed"),),
        return_to="payment_plan",
        payment="submitted",
    )
    for node in nodes:
        if node["stage"] == "settlement" or node["key"] in {"payment_plan", "payment_review"}:
            node["contract_payment"] = True
            node["scan_material"] = "settlement"
        if node["key"] == "payment_review":
            node["contract_payment_review"] = True

    add(
        "payment",
        "payment_eip",
        "付款",
        "external",
        required=(("notice", "electronic"), ("invoice", "electronic"), ("contract", "electronic")),
        payment="requested",
    )
    # Applying for payment completes OA handling, without asserting payment/receipt.
    nodes[-1]["finish_on_application"] = True
    return nodes
