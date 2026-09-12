"""LLM 客户端层测试（全部用桩，绝不真调 API）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.llm.client import (
    AssistantMessage,
    ToolCall,
    ZhipuClient,
    sanitize_messages,
)


def fake_response(content, tool_calls=None, include_reasoning: bool = False):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    if include_reasoning:
        message.reasoning_content = "让我想想……"
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def fake_tool_call(call_id="call_1", name="run_python", arguments='{"code":"print(1)"}'):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


class TestMissingApiKey:
    def test_empty_key_raises_runtime_error(self):
        """缺 Key 属配置错误，必须抛 —— 不能静默降级，也不能等到调用时才炸。"""
        with pytest.raises(RuntimeError, match="ZHIPUAI_API_KEY"):
            ZhipuClient(api_key="", model="glm-5.2")

    def test_runtime_error_not_value_error(self):
        """用 RuntimeError 而非 ValueError：调用方要能区分「配置错」和「参数错」。"""
        with pytest.raises(RuntimeError):
            ZhipuClient(api_key="   ", model="glm-5.2")


class TestNormalization:
    def test_none_content_becomes_empty_string(self):
        """推理模型常返回 content=None，下游不该到处判空。"""
        client = ZhipuClient(
            api_key="k", model="m", client=FakeClient(fake_response(None))
        )
        assert client.chat([{"role": "user", "content": "hi"}]).content == ""

    def test_tool_calls_are_normalized(self):
        client = ZhipuClient(
            api_key="k",
            model="m",
            client=FakeClient(fake_response("", [fake_tool_call()])),
        )
        message = client.chat([{"role": "user", "content": "hi"}])
        assert message.wants_tools
        assert message.tool_calls[0].name == "run_python"
        assert message.tool_calls[0].id == "call_1"

    def test_arguments_stay_raw_string(self):
        """arguments 保持原始字符串 —— 先 loads 再 dumps 会在模型输出
        非严格 JSON 时破坏原样。"""
        raw = '{"code": "print(1)"}'
        client = ZhipuClient(
            api_key="k", model="m", client=FakeClient(fake_response("", [fake_tool_call(arguments=raw)]))
        )
        assert client.chat([{"role": "user", "content": "x"}]).tool_calls[0].arguments is raw

    def test_tools_are_forwarded(self):
        fake = FakeClient(fake_response("hi"))
        client = ZhipuClient(api_key="k", model="glm-5.2", client=fake)
        tools = [{"type": "function", "function": {"name": "x"}}]
        client.chat([{"role": "user", "content": "x"}], tools=tools)
        assert fake.chat.completions.kwargs["tools"] == tools
        assert fake.chat.completions.kwargs["tool_choice"] == "auto"
        assert fake.chat.completions.kwargs["model"] == "glm-5.2"

    def test_tools_none_omits_tools(self):
        """tools=None 是「强制收尾」信号，请求里就不该出现 tools 字段。"""
        fake = FakeClient(fake_response("总结"))
        client = ZhipuClient(api_key="k", model="m", client=fake)
        client.chat([{"role": "user", "content": "x"}], tools=None)
        assert "tools" not in fake.chat.completions.kwargs

    def test_reasoning_content_is_stripped_on_way_out(self):
        """推理模型返回的 reasoning_content 不能原样回传。"""
        client = ZhipuClient(
            api_key="k", model="m", client=FakeClient(fake_response("ok"))
        )
        client.chat([{"role": "assistant", "content": "x", "reasoning_content": "想"}])
        sent = client._client.chat.completions.kwargs["messages"]
        assert all("reasoning_content" not in m for m in sent)


class TestSanitizeMessages:
    def test_strips_reasoning_fields(self):
        result = sanitize_messages(
            [{"role": "assistant", "content": "a", "reasoning_content": "想", "audio": None}]
        )
        assert set(result[0]) == {"role", "content"}

    def test_none_content_becomes_empty(self):
        result = sanitize_messages([{"role": "assistant", "content": None}])
        assert result[0]["content"] == ""

    def test_tool_messages_are_untouched(self):
        message = {"role": "tool", "tool_call_id": "c1", "content": "out"}
        assert sanitize_messages([message]) == [message]


class TestToolCallParsing:
    def test_valid_json(self):
        assert ToolCall(id="1", name="x", arguments='{"a": 1}').parsed_arguments() == {"a": 1}

    def test_invalid_json_returns_empty_dict(self):
        """模型输出不合法 JSON 时不能抛异常 —— 循环还要继续跑。"""
        assert ToolCall(id="1", name="x", arguments="{oops").parsed_arguments() == {}

    def test_empty_arguments(self):
        assert ToolCall(id="1", name="x", arguments="").parsed_arguments() == {}

    def test_non_object_json(self):
        assert ToolCall(id="1", name="x", arguments="[1,2]").parsed_arguments() == {}


class TestAssistantMessage:
    def test_wants_tools_false_without_calls(self):
        assert AssistantMessage(content="hi").wants_tools is False

    def test_wants_tools_true_with_calls(self):
        message = AssistantMessage(content="", tool_calls=[ToolCall("1", "x", "{}")])
        assert message.wants_tools is True
