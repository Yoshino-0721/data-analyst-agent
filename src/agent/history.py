"""历史消息裁剪。

这里唯一的难点是**配对关系不能断**。

OpenAI 兼容接口要求：一条 `role="tool"` 消息必须有对应的、紧邻其前的
assistant 消息里的 `tool_call_id`，对不上会直接报
`tool call id not found` —— 整个请求 400，连重试的机会都没有。

所以裁剪的最小单位不是「一条消息」，而是**一个不可分割的块**：
    一次 assistant(tool_calls) + 它触发的全部 tool 消息
要么整块留，要么整块删。从中间切开必然产生孤儿 tool 消息。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SUMMARY_TEMPLATE = "（为控制上下文长度，已省略前面 {count} 轮对话。数据文件与列信息见上方系统提示。）"


def _size(message: dict[str, Any]) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(message))


def split_into_blocks(messages: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], int]:
    """把消息流切成不可分割的块。

    每个块要么是「assistant(tool_calls) + 其全部 tool 消息」，
    要么是单独一条 user/assistant 普通消息。

    返回 (块列表, 被丢弃的孤儿 tool 消息数)。
    孤儿 tool 消息（前面没有带 tool_calls 的 assistant）无法与任何 id 配对，
    回传必然报错，只能丢弃 —— 出现它说明调用方已经把历史切坏了。
    """
    blocks: list[list[dict[str, Any]]] = []
    pending: list[dict[str, Any]] | None = None
    orphans = 0

    for message in messages:
        role = message.get("role")

        if role == "assistant" and message.get("tool_calls"):
            if pending is not None:
                blocks.append(pending)
            pending = [message]
            continue

        if role == "tool":
            if pending is None:
                orphans += 1
                continue
            pending.append(message)
            continue

        if pending is not None:
            blocks.append(pending)
            pending = None
        blocks.append([message])

    if pending is not None:
        blocks.append(pending)

    return blocks, orphans


def trim_history(
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 12_000,
    keep_turns: int = 4,
) -> list[dict[str, Any]]:
    """按字符预算裁剪历史，同时保证工具调用链的完整性。

    策略：
      1. system 消息**永远完整保留**（里面是数据 Schema，丢了模型就开始瞎猜列名）；
      2. **第一条 user 消息也永远保留** —— 理由见下方注释，这条是硬性要求；
      3. 最后 keep_turns 个块无论如何都留 —— 越近的上下文越重要；
      4. 再往前按预算加，超了就停；
      5. 被丢掉的内容用一句话摘要替代，而不是凭空消失
         （凭空消失会让模型以为自己从没试过别的方案，于是再试一遍）。

    Args:
        messages: 完整消息列表，第一条应为 system。
        max_chars: 非 system 部分的字符预算。
        keep_turns: 强制保留的最后 N 个块。
    """
    if not messages:
        return []

    # 开头的连续 system 消息
    system: list[dict[str, Any]] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        system.append(messages[index])
        index += 1
    rest = messages[index:]

    if not rest:
        return system

    blocks, orphans = split_into_blocks(rest)
    if orphans:
        logger.warning("裁剪时发现 %d 条无法配对的 tool 消息，已丢弃", orphans)

    # 第一条 user 消息**不参与预算裁剪**。
    #
    # 它不是普通的一条历史 —— 它是「用户到底要什么」的唯一载体。
    # 一旦被裁掉，剩下的消息会以 assistant(tool_calls) 开头，整个 messages
    # 里再没有任何 user 角色。多数厂商（含智谱）要求 messages 必须由 user
    # 开场，否则直接拒绝：实测报 `400 code 1214 messages 参数非法`，
    # 而且这个错误发生在**多轮之后**，看起来跟裁剪毫无关系，极难定位。
    pinned: list[dict[str, Any]] = []
    if blocks and blocks[0] and blocks[0][0].get("role") == "user":
        pinned = blocks.pop(0)

    # 从后往前累积：最近的 keep_turns 个块无条件保留，再往前的按预算
    kept: list[list[dict[str, Any]]] = []
    used = 0

    for block in reversed(blocks):
        block_size = sum(_size(message) for message in block)
        if len(kept) >= keep_turns and used + block_size > max_chars:
            break
        kept.append(block)
        used += block_size

    kept.reverse()

    dropped = len(blocks) - len(kept)
    result: list[dict[str, Any]] = list(system)
    if dropped > 0:
        result.append({"role": "system", "content": SUMMARY_TEMPLATE.format(count=dropped)})
    result.extend(pinned)
    for block in kept:
        result.extend(block)
    return result


__all__ = ["split_into_blocks", "trim_history"]
