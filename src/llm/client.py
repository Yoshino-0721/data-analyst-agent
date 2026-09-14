"""LLM 客户端：把「智谱的返回格式」隔离在这一层。

两层设计：

1. `LLMClient` 是**协议**，不是具体类。循环层只依赖它 —— 测试里塞桩对象即可，
   绝不真调 API。
2. `ZhipuClient` 是唯一实现，负责把 openai SDK 的返回归一化成
   `AssistantMessage` / `ToolCall`。**归一化很重要**：推理模型会多带
   `reasoning_content` 字段，不同模型的 content 可能是 `None` 或 `''`，
   这些差异不应该渗进循环层。

关于 arguments 的一个硬约束（项目一的踩坑经验）：
`tool_call.function.arguments` **必须保持原始 JSON 字符串**回传给模型。
有些实现图省事先 `json.loads` 成 dict、回传时再 dumps，
一旦模型的输出不是严格 JSON（比如尾随逗号），往返一次就破坏了原样，
模型下次就可能解析失败。这里原样保存、原样回填。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """一次工具调用请求。"""

    id: str
    """必须与随后回填的 tool 消息的 tool_call_id 完全一致 —— 对不上 API 会直接报错。"""

    name: str
    arguments: str
    """**原始 JSON 字符串**，不做解析、不做美化。"""

    def parsed_arguments(self) -> dict[str, Any]:
        """按需解析。解析失败返回空 dict，由调用方决定怎么提示模型。"""
        try:
            loaded = json.loads(self.arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


@dataclass
class AssistantMessage:
    """归一化后的助手消息。"""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """循环层唯一依赖的 LLM 接口。"""

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AssistantMessage:
        """发一轮对话。tools 为 None 表示这一轮不允许调用工具（强制收尾用）。"""
        ...


# 回传给 API 时**必须剔除**的字段。
# 推理模型（glm-5.2）会返回 reasoning_content，原样回传轻则浪费 token，
# 重则被服务端判定为非法消息。归一化后的消息本来就没有这些字段，
# 这里再兜一层，防止调用方直接塞了原始 SDK 对象进来。
_STRIPPED_KEYS = {"reasoning_content", "reasoning", "annotations", "audio", "refusal", "function_call"}


def sanitize_messages(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """清掉助手消息里不该回传的字段，并把 None content 归一成空串。"""
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        item = {key: value for key, value in message.items() if key not in _STRIPPED_KEYS}
        if item.get("role") == "assistant" and item.get("content") is None:
            item["content"] = ""
        cleaned.append(item)
    return cleaned


def describe_messages(messages: Sequence[dict[str, Any]]) -> str:
    """把消息序列画成一行行的诊断信息。

    专门为「厂商报 messages 非法但不说哪里非法」这种场景准备：
    逐条列出 role / 键 / tool 配对，肉眼就能看出结构错在哪。
    """
    lines = [f"messages（共 {len(messages)} 条）："]
    open_ids: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role")
        parts = [f"[{index:>2}] {role:<9}"]
        if role == "assistant" and message.get("tool_calls"):
            ids = [call.get("id") for call in message["tool_calls"]]
            open_ids = {str(i) for i in ids}
            parts.append(f"tool_calls={ids}")
            parts.append(f"content={message.get('content')!r}")
        elif role == "tool":
            call_id = str(message.get("tool_call_id"))
            matched = call_id in open_ids
            parts.append(f"tool_call_id={call_id} {'✓' if matched else '✗ 对不上任何 assistant'}")
            content = message.get("content")
            parts.append(f"content_len={len(content) if isinstance(content, str) else 'N/A'}")
        else:
            content = message.get("content")
            parts.append(f"content_len={len(content) if isinstance(content, str) else content!r}")
        parts.append(f"keys={sorted(message)}")
        lines.append("  " + "  ".join(parts))
    return "\n".join(lines)


class ZhipuClient:
    """智谱 GLM 客户端（OpenAI 兼容接口）。

    构造时就校验 API Key —— 缺 Key 属于配置错误，越早炸越好，
    不要在跑了几轮、烧了 token 之后才失败。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://open.bigmodel.cn/api/paas/v4/",
        proxy: str = "",
        timeout: float = 120.0,
        client: Any = None,
    ) -> None:
        # strip 后再判空：.env 里留一行 `ZHIPUAI_API_KEY=` 或只有空格是常见情况，
        # 不 strip 就会拿着一个「看起来有值」的 Key 跑到调用时才 401。
        key = (api_key or "").strip()
        if not key:
            raise RuntimeError(
                "缺少 ZHIPUAI_API_KEY。请在环境变量或 .env 中配置后再启动。"
            )
        self.model = model
        self._timeout = timeout
        # client 参数是为了测试能塞桩对象进来（否则测试要打真实 API）
        self._client = (
            client if client is not None else self._build_client(key, base_url, proxy, timeout)
        )

    @staticmethod
    def _build_client(api_key: str, base_url: str, proxy: str, timeout: float) -> Any:
        import httpx
        from openai import OpenAI

        # max_retries 必须显式给：SDK 默认重试 2 次，一次卡住的调用会变成三次，
        # 成本翻倍、占住线程池的时间也翻倍。失败就让上层看见，由编排层决定怎么办。
        max_retries = 1
        if proxy:
            return OpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=httpx.Client(proxy=proxy, timeout=timeout),
                max_retries=max_retries,
            )
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AssistantMessage:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": sanitize_messages(messages),
        }
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = "auto"

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # 厂商说「messages 参数非法」时，光看异常是查不出所以然的 ——
            # 必须把**结构**打出来：每条消息的 role、键、以及 tool 配对关系。
            # 这几行日志是排查这类问题的唯一线索。
            logger.error(
                "对话请求失败：%s\n%s",
                exc,
                describe_messages(kwargs.get("messages", [])),
            )
            raise

        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in (message.tool_calls or [])
        ]

        # 推理模型的 content 可能是 None，归一成空串，避免下游到处判空
        return AssistantMessage(content=message.content or "", tool_calls=tool_calls)


__all__ = [
    "AssistantMessage",
    "describe_messages",
    "LLMClient",
    "ToolCall",
    "ZhipuClient",
    "sanitize_messages",
]
