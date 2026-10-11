"""Versioned maintenance process; copied into each order on submission."""

STAGES = (
    ("ordering", "工单下单"),
    ("report", "服务报告"),
    ("settlement", "服务结算"),
    ("payment", "付款及收尾"),
)
ROLES = (
    ("numbering", "编号管理员"),
    ("reviewer", "初审人"),
    ("department", "部门负责人"),
    ("senior", "上级负责人"),
    ("seal", "印章管理人"),
    ("supplier", "服务单位经办组"),
    ("finance", "财务经办组"),
)
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
                required = ((material, "paper" if key == "settlement_final" else "electronic"),)
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

    def seal(stage, material, return_to):
        add(
            stage,
            f"{stage}_eip",
            "提交 EIP 用印申请",
            "external",
            required=((material, "paper"),),
            return_to=return_to,
        )
        add(
            stage,
            f"{stage}_department_seal",
            "登记部门负责人用印审批结果",
            "external",
            "department",
            return_to=return_to,
        )
        add(
            stage,
            f"{stage}_senior_seal",
            "登记上级负责人用印审批结果",
            "external",
            "senior",
            return_to=return_to,
        )
        add(
            stage,
            f"{stage}_seal",
            "确认印章管理人盖章",
            "action",
            "seal",
            required=((material, "sealed"),),
            return_to=return_to,
        )

    add("ordering", "number", "确认唯一工单编号", "action", "numbering")
    add(
        "ordering",
        "order_fill",
        "填写并提交维护服务工单",
        "submission",
        required=(("order", "electronic"),),
    )
    add("ordering", "order_review", "工单初审", "approval", "reviewer", return_to="order_fill")
    add("ordering", "order_final", "工单部门终审", "approval", "department", return_to="order_fill")
    seal("ordering", "order", "order_fill")
    add("ordering", "issue", "下发服务单位并确认服务安排", "action")

    add(
        "report",
        "report_fill",
        "服务单位填报服务报告",
        "submission",
        "supplier",
        required=(("report", "electronic"),),
    )
    add("report", "report_review", "线路主管电子版初审", "approval", return_to="report_fill")
    add("report", "report_mail", "服务单位盖章并寄送服务报告", "action", "supplier")
    add(
        "report",
        "report_paper",
        "线路主管签收及纸质二审",
        "approval",
        required=(("report", "paper"),),
        return_to="report_fill",
    )
    seal("report", "report", "report_fill")

    add(
        "settlement",
        "settlement_fill",
        "服务单位填报服务结算单",
        "submission",
        "supplier",
        required=(("settlement", "electronic"),),
    )
    add(
        "settlement",
        "settlement_review",
        "线路主管电子版初审",
        "approval",
        return_to="settlement_fill",
    )
    add("settlement", "settlement_mail", "服务单位盖章并寄送结算单", "action", "supplier")
    add(
        "settlement",
        "settlement_paper",
        "线路主管纸质审核及签字",
        "approval",
        required=(("settlement", "paper"),),
        return_to="settlement_fill",
    )
    add(
        "settlement",
        "settlement_final",
        "结算单部门终审",
        "approval",
        "department",
        return_to="settlement_fill",
    )
    seal("settlement", "settlement", "settlement_fill")
    add("settlement", "payment_plan", "登记付款计划", "submission")
    add(
        "settlement",
        "payment_review",
        "部门审核付款计划",
        "approval",
        "department",
        required=(("settlement", "sealed"),),
        return_to="payment_plan",
        payment="submitted",
    )

    add(
        "payment",
        "payment_docs",
        "服务单位提交支付材料",
        "submission",
        "supplier",
        required=(("notice", "electronic"), ("invoice", "electronic"), ("contract", "electronic")),
    )
    add(
        "payment",
        "payment_docs_review",
        "线路主管审核支付材料",
        "approval",
        return_to="payment_docs",
    )
    add(
        "payment",
        "invoice_check",
        "发票验真并上传结果",
        "action",
        required=(("verification", "electronic"),),
    )
    add(
        "payment",
        "payment_eip",
        "提交 EIP 付款申请",
        "external",
        payment="requested",
        return_to="payment_docs",
    )
    add("payment", "finance_handover", "纸质资料交财务", "action", payment="awaiting_payment")
    add(
        "payment",
        "finance_process",
        "登记财务处理结果",
        "external",
        "finance",
        return_to="payment_docs",
    )
    add("payment", "paid", "确认实际付款", "action", "finance", payment="paid")
    add("payment", "receipt", "确认服务单位已收款", "action", payment="received")
    return nodes
