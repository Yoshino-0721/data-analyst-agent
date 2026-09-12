"""Function Calling 主循环。

手写而不是用 LangGraph，是因为这个循环的全部复杂度就在三条约束上，
用框架反而要把这些约束翻译成框架的DSL。三条硬约束：

1. **步数上限**：`AGENT_MAX_STEPS` 用尽后，摘掉 tools 再问一次，**强制**它
   基于已有信息给出总结。模型在工具循环里兜圈子是常态，不给出口就永远出不来。
2. **结果回填走 `to_tool_payload()`**：净化（去宿主路径、去原始命令、
   artifacts 只给文件名）在执行层已经做完，这里直接透传，不重新拼装。
3. **配置类错误照抛**：缺 API Key 是 `RuntimeError`，不捕获、不降级。
   它和「代码写错了」性质完全不同 —— 后者模型能改，前者只能人来处理。

另有两处来自项目一的实测经验，位置很关键：

- **hint 必须拼在回填文本的末尾**，不能另发一条 system 消息。
  约束挂在系统提示里几乎无效，挂在最靠近生成位置的那条消息末尾才有效。
- **同错去重**：连续 3 次撞同一处错误时，把更强的提示追加到 hint 之后
  （仍在末尾），然后强制收尾。否则模型会一直原地重试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..llm.client import AssistantMessage, LLMClient, ToolCall
from ..schema.extractor import DatasetSchema
from .history import trim_history
from .prompt import build_system_prompt
from .tools import RUN_PYTHON, ToolOutcome, ToolRuntime, build_tool_schemas

logger = logging.getLogger(__name__)

REPEAT_ESCALATION = (
    "⚠️ 你已经在**同一个错误**上失败了 {times} 次。不要再重试类似写法 —— "
    "请换一种完全不同的实现思路；如果涉及列名，先调用 get_schema 确认真实列名再写。"
)

FORCED_FINISH_INSTRUCTION = (
    "已达到工具调用轮数上限。请**不要再调用工具**，"
    "直接基于上面已经拿到的输出回答用户的问题；"
    "如果信息不足，就明确说明缺什么、以及你尝试过哪些方法。"
)


@dataclass
class StepRecord:
    """一轮工具调用的留痕，用于前端展示与事后排查。"""

    index: int
    tool_calls: list[ToolCall]
    outcomes: list[ToolOutcome] = field(default_factory=list)


@dataclass
class AgentResult:
    """一次提问的最终结果。"""

    answer: str
    steps: list[StepRecord] = field(default_factory=list)
    terminated_by: str = "answer"
    """answer（正常回答）/ max_steps（步数用尽）/ repeated_error（防撞墙强制收尾）"""

    artifacts: tuple[str, ...] = ()
    """过程中产出的文件名。"""


def run_agent(
    question: str,
    *,
    client: LLMClient,
    runtime: ToolRuntime,
    schemas: Sequence[DatasetSchema] = (),
    max_steps: int = 8,
    max_repeat_failures: int = 3,
    history_max_chars: int = 12_000,
    history_keep_turns: int = 4,
    run_dir_factory: Callable[[], Path] | None = None,
) -> AgentResult:
    """跑一轮「提问 → 工具调用 → 观察 → 修正 → 回答」的闭环。

    Args:
        question: 用户的自然语言问题。
        client: LLM 客户端（协议，测试里塞桩即可）。
        runtime: 工具运行时，持有执行器与数据文件。
        schemas: 已提取的数据 Schema，会写进系统提示。
        max_steps: 工具调用轮数上限。
        max_repeat_failures: 连续同一错误几次就强制收尾。
    """
    if max_steps < 1:
        raise ValueError(f"max_steps 必须 ≥ 1，当前为 {max_steps}")

    if run_dir_factory is not None:
        runtime.run_dir_factory = run_dir_factory

    tools = build_tool_schemas()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(schemas)},
        {"role": "user", "content": question},
    ]

    steps: list[StepRecord] = []
    artifacts: list[str] = []
    consecutive_failures: list[str] = []  # 记录最近的失败指纹

    for index in range(max_steps):
        messages = trim_history(
            messages, max_chars=history_max_chars, keep_turns=history_keep_turns
        )
        response: AssistantMessage = client.chat(messages, tools=tools)

        # 没有工具调用 = 模型认为可以回答了
        if not response.wants_tools:
            return AgentResult(
                answer=response.content,
                steps=steps,
                terminated_by="answer",
                artifacts=tuple(artifacts),
            )

        record = StepRecord(index=index, tool_calls=list(response.tool_calls))
        # 注意：tool_calls 里的 arguments 保持原始 JSON 字符串回填，
        # 不做 loads/dumps 往返（见 llm/client.py 的说明）
        messages.append(_assistant_message(response))

        hit_repeat = False
        for call in response.tool_calls:
            outcome = runtime.execute(call)
            record.outcomes.append(outcome)
            artifacts.extend(outcome.artifacts)

            # 记录失败指纹：只累计**连续**的失败，一旦成功就清零
            if outcome.fingerprint:
                if consecutive_failures and consecutive_failures[-1] == outcome.fingerprint:
                    consecutive_failures.append(outcome.fingerprint)
                else:
                    consecutive_failures = [outcome.fingerprint]
            elif call.name == RUN_PYTHON:
                consecutive_failures = []

            text = outcome.text
            if len(consecutive_failures) >= max_repeat_failures:
                hit_repeat = True
                # 升级提示追加在 hint **之后** —— 它比常规 hint 更强，
                # 放在最后一句才最容易被模型读到
                text = f"{text}\n{REPEAT_ESCALATION.format(times=len(consecutive_failures))}"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": text,
                }
            )

        steps.append(record)

        if hit_repeat:
            logger.warning("连续 %d 次同一错误，强制收尾", len(consecutive_failures))
            return _force_finish(
                client, messages, steps, artifacts, terminated_by="repeated_error"
            )

    logger.warning("AGENT_MAX_STEPS=%d 用尽，摘掉工具强制收尾", max_steps)
    return _force_finish(client, messages, steps, artifacts, terminated_by="max_steps")


def _assistant_message(response: AssistantMessage) -> dict[str, Any]:
    """把归一化消息还原成 API 需要的格式。

    arguments 原样保留字符串：项目一实测过，先 loads 再 dumps 会在
    模型输出不是严格 JSON 时破坏原样，导致下一轮解析失败。
    """
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in response.tool_calls
        ],
    }


def _force_finish(
    client: LLMClient,
    messages: list[dict[str, Any]],
    steps: list[StepRecord],
    artifacts: list[str],
    *,
    terminated_by: str,
) -> AgentResult:
    """摘掉工具再问一次，逼模型基于已有信息收尾。

    这是循环唯一的**兜底出口**。没有它，模型会在工具调用里无限打转。
    """
    closing = list(messages) + [
        {"role": "user", "content": FORCED_FINISH_INSTRUCTION}
    ]
    final = client.chat(closing, tools=None)
    return AgentResult(
        answer=final.content,
        steps=steps,
        terminated_by=terminated_by,
        artifacts=tuple(artifacts),
    )


__all__ = ["AgentResult", "StepRecord", "run_agent"]
