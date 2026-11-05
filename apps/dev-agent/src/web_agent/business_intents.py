"""本地状态操作只接受明确的用户表达，不从引用、问句或否定中提取授权。"""

import re


def goal_confirmation(message):
    """返回明确版本或 0（当前这版）；无法确认时返回 None。"""
    text = message.strip().rstrip("。！!")
    numbered = re.fullmatch(r"确认第\s*(\d+)\s*版目标", text)
    if numbered:
        return int(numbered[1])
    if re.fullmatch(
        r"(?:确认这版目标|这版目标确认|这版月目标和周目标都确认)"
        r"(?:[，,]\s*(?:接着|继续)(?:帮我)?(?:排|制定)(?:下周|周)计划)?", text
    ):
        return 0
    return None
