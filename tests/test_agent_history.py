"""历史裁剪测试。

核心断言只有一个：**裁剪后不能出现配对不上的 tool 消息**。
一旦出现，API 会直接 400（`tool call id not found`），整轮对话失败。
"""

from __future__ import annotations

from src.agent.history import split_into_blocks, trim_history


def _assistant_with_tools(*ids: str, prefix: str = "call") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"{prefix}_{i}", "type": "function",
             "function": {"name": "run_python", "arguments": '{"code": "print(1)"}'}}
            for i in ids
        ],
    }


def _tool(call_id: str, content: str = "x") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _system(text: str = "system") -> dict:
    return {"role": "system", "content": text}


def assert_paired(messages: list[dict]) -> None:
    """断言所有 tool 消息都能找到对应的 assistant tool_call_id。"""
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            pending = {call["id"] for call in message["tool_calls"]}
        elif role == "tool":
            assert message["tool_call_id"] in pending, (
                f"孤儿 tool 消息：{message['tool_call_id']}，"
                f"当前未匹配的 id 为 {pending}"
            )


class TestSplitIntoBlocks:
    def test_assistant_and_tools_form_one_block(self):
        messages = [_assistant_with_tools("a", "b"), _tool("call_a"), _tool("call_b")]
        blocks, orphans = split_into_blocks(messages)
        assert len(blocks) == 1
        assert len(blocks[0]) == 3
        assert orphans == 0

    def test_plain_messages_are_separate_blocks(self):
        messages = [_user("a"), _user("b")]
        blocks, _ = split_into_blocks(messages)
        assert len(blocks) == 2

    def test_orphan_tool_is_dropped(self):
        """没有前导 assistant 的 tool 消息无法配对，只能丢弃。"""
        blocks, orphans = split_into_blocks([_tool("call_x")])
        assert orphans == 1
        assert blocks == []

    def test_multiple_chains(self):
        messages = [
            _assistant_with_tools("a"),
            _tool("call_a"),
            _user("继续"),
            _assistant_with_tools("b"),
            _tool("call_b"),
        ]
        blocks, orphans = split_into_blocks(messages)
        assert orphans == 0
        # [assistant+tool] [user] [assistant+tool]
        assert len(blocks) == 3


class TestTrimHistory:
    def test_system_is_always_kept(self):
        messages = [_system("重要提示")] + [_user("x" * 100) for _ in range(20)]
        result = trim_history(messages, max_chars=50, keep_turns=1)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "重要提示"

    def test_never_breaks_tool_pairs(self):
        """这是本文件最重要的一条断言。"""
        messages = [_system()]
        for step in range(6):
            messages.append(_assistant_with_tools(str(step)))
            messages.append(_tool(f"call_{step}", "输出" * 200))

        result = trim_history(messages, max_chars=300, keep_turns=2)
        assert_paired(result)

    def test_keeps_recent_turns(self):
        messages = [_system()]
        for step in range(5):
            messages.append(_user(f"第{step}轮"))
        result = trim_history(messages, max_chars=1, keep_turns=2)
        contents = [m.get("content", "") for m in result if m.get("role") == "user"]
        assert "第4轮" in contents
        assert "第3轮" in contents

    def test_dropped_content_is_summarized(self):
        """凭空消失会让模型以为自己没试过别的方案，于是再试一遍。"""
        messages = [_system()] + [_user(f"第{i}轮" + "x" * 200) for i in range(10)]
        result = trim_history(messages, max_chars=200, keep_turns=1)
        summaries = [m for m in result if m["role"] == "system"]
        assert len(summaries) >= 2
        assert "省略" in summaries[-1]["content"]

    def test_empty_history(self):
        assert trim_history([]) == []

    def test_only_system(self):
        assert trim_history([_system("s")]) == [_system("s")]

    def test_result_never_starts_with_tool_message(self):
        messages = [_system()]
        for step in range(4):
            messages.append(_assistant_with_tools(str(step)))
            messages.append(_tool(f"call_{step}", "y" * 300))
        result = trim_history(messages, max_chars=100, keep_turns=1)
        # 第一条非 system 消息如果是 tool，回传必然报错
        non_system = [m for m in result if m.get("role") != "system"]
        if non_system:
            assert non_system[0]["role"] != "tool"

    def test_generous_budget_keeps_everything(self):
        messages = [_system(), _user("a"), _assistant_with_tools("1"), _tool("call_1")]
        result = trim_history(messages, max_chars=100_000, keep_turns=10)
        assert len(result) == len(messages)
