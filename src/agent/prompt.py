"""系统提示。

数据文件路径**只在这里出现一次**，且以「照抄即可」的形式给出。
这是 `run_python` 不接收路径参数的前提：模型没有发挥空间，
也就不存在「猜一个宿主机路径去读」的可能。
"""

from __future__ import annotations

from typing import Sequence

from ..schema.extractor import DatasetSchema

SYSTEM_PROMPT = """你是一名数据分析师。用户会用自然语言提出关于数据的问题。

工作方式：
1. 先想清楚要算什么，再用 run_python 写代码执行；
2. 只有 stdout 会回传给你 —— 关键结论必须 print 出来；
3. 代码在隔离沙箱里跑，数据只读、只有 /out 可写、无网络。

铁律：
- **列名以 get_schema 为准**。不确定就先调用 get_schema，不要凭记忆猜 —— 列名写错是最常见的失败原因。
- 代码报错时，**先读 stderr 定位**，再改。不要原样重试同一段代码。
- 同一个错误连续失败两次后，**换一种完全不同的实现思路**。
- 最终回答用中文，给出具体数字，并说明数字是怎么算出来的。"""


def build_system_prompt(schemas: Sequence[DatasetSchema]) -> str:
    """拼系统提示，并把可用文件清单钉在最前面。"""
    if not schemas:
        return SYSTEM_PROMPT + "\n\n（当前会话还没有加载数据文件。）"

    lines = ["你当前可以访问的数据文件（**路径请照抄，不要改动**）："]
    for schema in schemas:
        lines.append(f"- {schema.data_path}")
    lines.append("")
    lines.append("数据概况：")
    for schema in schemas:
        lines.append(schema.to_prompt_string())
    lines.append("")
    lines.append(SYSTEM_PROMPT)
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "build_system_prompt"]
