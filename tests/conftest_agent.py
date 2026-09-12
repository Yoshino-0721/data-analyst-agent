"""Agent 层测试用的桩对象。

这个文件不叫 conftest.py 是为了让桩对象**可被显式导入**：
测试里 `from tests.conftest_agent import ScriptedClient`，
一眼能看出这个 client 是假的，而不是 pytest 偷偷注入的魔法。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Sequence

from src.llm.client import AssistantMessage, ToolCall
from src.sandbox.executor import ExecutionRequest, ExecutionResult, ExecStatus


@dataclass
class ScriptedClient:
    """按脚本返回预设响应的假客户端，并记录每一次调用。

    记录调用参数很重要 —— 很多断言验的是「**发给模型的消息长什么样**」，
    比如最后一步有没有把 tools 摘掉、hint 是不是在末尾。
    """

    responses: list[AssistantMessage]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AssistantMessage:
        self.calls.append(
            {
                "messages": copy.deepcopy(list(messages)),
                "tools": None if tools is None else list(tools),
            }
        )
        if not self.responses:
            raise AssertionError("ScriptedClient 的响应脚本已用完 —— 检查循环步数")
        return self.responses.pop(0)

    @property
    def last_call(self) -> dict[str, Any]:
        return self.calls[-1]

    @property
    def call_count(self) -> int:
        return len(self.calls)


@dataclass
class StubExecutor:
    """按预设结果依次返回的执行器，并记录收到的请求。"""

    results: list[ExecutionResult]
    requests: list[ExecutionRequest] = field(default_factory=list)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        if not self.results:
            raise AssertionError("StubExecutor 的结果已用完 —— 检查循环步数")
        return self.results.pop(0)

    def describe(self) -> str:
        return "StubExecutor（无隔离，仅测试用）"

    def available(self) -> tuple[bool, str]:
        return True, ""


def ok_result(stdout: str = "答案：42", artifacts: tuple = ()) -> ExecutionResult:
    return ExecutionResult(
        status=ExecStatus.OK,
        stdout=stdout,
        stderr="",
        artifacts=artifacts,
        exit_code=0,
        duration_ms=10,
    )


def error_result(
    stderr: str = '  File "script.py", line 3\n    df["地区"]\nKeyError: 地区',
    summary: str = "KeyError: 地区",
    hint: str = "先用 get_schema 确认列名与实际数据一致",
    status: ExecStatus = ExecStatus.RUNTIME_ERROR,
) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        stdout="",
        stderr=stderr,
        error_summary=summary,
        hint=hint,
        exit_code=1,
        duration_ms=10,
    )


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def assistant_with_tools(*calls: ToolCall, content: str = "") -> AssistantMessage:
    return AssistantMessage(content=content, tool_calls=list(calls))


def assistant_text(content: str) -> AssistantMessage:
    return AssistantMessage(content=content)
