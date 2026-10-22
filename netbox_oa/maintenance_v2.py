"""Task-specific guidance for the optional v2 workbench (no workflow decisions)."""

from .maintenance_flow import STAGES


def handling_guide(detail):
    if not detail:
        return {}
    order = detail["order"]
    task = detail["current"]
    node = order.steps_snapshot[task.position] if task else {}
    key = node.get("key", "")
    kind = task.kind if task else ""
    title = "查看工单进展"
    steps = []
    tone = "review"
    if order.status == "draft":
        title = "填写工单 → 申请人签名 → 提交审批"
        steps = [
            "补全服务事项、合同和服务单位信息。",
            "申请人完成手写签名后提交；修改表单后须重新签名。",
        ]
        tone = "submission"
    elif task:
        if key == "order_correction":
            title = "按退回意见修正工单，重新签名后提交"
            steps = [
                "查看审批经过中的退回原因，编辑工单内容。",
                "申请人重新签名后提交，流程将重新经过初审和部门审核。",
            ]
            tone = "submission"
        elif key in {"order_review", "order_final"}:
            title = "核对工单内容，签署审核意见"
            steps = [
                "审阅服务事项、服务范围、合同及申请人签名。",
                "完成本环节签名并填写意见；有问题可退回申请人补正。",
            ]
        elif key == "payment_plan":
            title = "核对用印结算单，完善合同付款信息"
            steps = [
                "核对本次服务结算单及对应的用印扫描件。",
                "进入付款信息完成付款资料准备，再提交部门审核。",
            ]
            tone = "payment"
        elif key == "payment_review":
            title = "核对结算依据与付款信息，完成付款审核"
            steps = [
                "比对用印结算单、合同及付款信息。",
                "核对无误后同意；需调整时退回生成付款环节。",
            ]
            tone = "payment"
        elif kind == "external":
            title = "准备付款附件，登记外部流程结果"
            steps = [
                "上传付款通知书、电子发票、合同复印件和发票验真结果。",
                "确认外部付款流程已通过，填写外部流程编号后登记。",
            ]
            tone = "payment"
        elif key.endswith("_sealed_upload"):
            title = "上传用印后材料，核对后提交"
            steps = [
                "由发起流程的线路主管逐份关联审批原件上传用印件。",
                "核对用印内容、页数及签章，全部补齐后提交。",
            ]
            tone = "submission"
        elif kind == "submission":
            title = "上传本次审批材料，核对后提交"
            steps = [
                "上传乙方签章后的扫描件，核对内容、页数及签章是否完整。",
                "维护服务工单保留一份；报告和结算单可多份，上传时明确新增或更新。",
                "退回补正后须按本次要求更新材料，再提交审批。",
            ]
            tone = "submission"
        elif kind == "action" and key.endswith("_seal"):
            title = "按审批内容完成用印"
            steps = [
                "查阅本次审批通过的扫描版本，按审批内容办理用印。",
                "确认用印完成后提交，交由发起流程的线路主管上传用印后材料。",
            ]
            tone = "seal"
        elif kind in {"approval", "countersign"}:
            title = "逐份审阅本次材料，确认是否同意"
            steps = [
                "核对每份审批件的内容、金额及签章完整性。",
                "填写审核意见后同意；存在问题时退回补正。",
            ]
        else:
            title = "核对本环节要求，确认办理结果"
            steps = []
    stages = []
    current_stage = next((i for i, (key, _) in enumerate(STAGES) if key == order.stage), -1)
    for index, (stage_key, label) in enumerate(STAGES):
        state = "pending"
        if order.status == "completed":
            state = "completed"
        elif order.status == "active":
            state = "current" if index == current_stage else "pending"
        stages.append({"key": stage_key, "label": label, "state": state})
    required = [item for item in detail["materials"] if item["required_now"]]
    pending_uploads = [
        item["name"]
        for item in required
        if item["stage_upload_required"] and not item["stage_upload_complete"]
    ]
    return {
        "title": title,
        "steps": steps,
        "tone": tone,
        "stages": stages,
        "pending_uploads": pending_uploads,
        "required_count": len(required),
    }
