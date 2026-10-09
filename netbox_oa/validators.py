from django.core.exceptions import ValidationError


def validate_steps(value):
    """A workflow contains one or more ordered group approval steps."""
    if not isinstance(value, list) or not value:
        raise ValidationError("流程至少需要一个审批节点。")
    for step in value:
        if not isinstance(step, dict) or set(step) != {"name", "group_id"}:
            raise ValidationError("每个节点必须且只能包含 name 和 group_id。")
        if not isinstance(step["name"], str) or not step["name"].strip():
            raise ValidationError("节点名称不能为空。")
        if len(step["name"]) > 100:
            raise ValidationError("节点名称不能超过 100 个字符。")
        if type(step["group_id"]) is not int or step["group_id"] <= 0:
            raise ValidationError("group_id 必须为正整数。")


def validate_payload(value):
    if not isinstance(value, dict):
        raise ValidationError("业务数据必须是 JSON 对象。")
