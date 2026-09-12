"""Function Calling 格式探测脚本。

用途：**在写循环代码之前**，先用一次真实调用确认智谱返回的 tool_calls
结构到底长什么样。字段名猜错（比如把 arguments 写成 parameters）会让整层
返工，而这类信息只能问 API，不能靠读文档脑补。

跑法（Key 通过环境变量给，不落盘）：
    ZHIPUAI_API_KEY=xxx python scripts/probe_function_calling.py

可选环境变量：
    PROXY_URL    形如 http://127.0.0.1:7897，需要走代理时设置
    CHAT_MODEL   覆盖默认模型
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from openai import OpenAI  # noqa: E402

BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-5.2"

# 用一个最简单的工具，把注意力集中在**返回结构**而不是业务逻辑上
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市今天的天气。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 北京"},
                },
                "required": ["city"],
            },
        },
    }
]


def build_client() -> OpenAI:
    api_key = os.environ.get("ZHIPUAI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("请先设置环境变量 ZHIPUAI_API_KEY（不要把 Key 写进仓库）")

    kwargs: dict = {"timeout": 60.0}
    proxy = os.environ.get("PROXY_URL", "").strip()
    if proxy:
        kwargs["http_client"] = httpx.Client(proxy=proxy, timeout=60.0)
    return OpenAI(api_key=api_key, base_url=BASE_URL, **kwargs)


def main() -> int:
    client = build_client()
    model = os.environ.get("CHAT_MODEL", DEFAULT_MODEL)

    print(f"模型：{model}")
    print("=" * 60)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "北京今天天气怎么样？"}],
        tools=TOOLS,
        tool_choice="auto",
    )

    message = response.choices[0]
    print(f"finish_reason = {message.finish_reason}")
    print()

    assistant = message.message
    print("=== assistant 消息的关键字段 ===")
    print(f"role        : {assistant.role}")
    print(f"content     : {assistant.content!r}")
    tool_calls = getattr(assistant, "tool_calls", None)
    print(f"tool_calls  : {tool_calls!r}")
    print()

    if not tool_calls:
        print("⚠️ 模型没有返回 tool_calls —— 该模型可能不支持 Function Calling，")
        print("   或需要换用支持工具调用的模型。换 CHAT_MODEL 再试一次。")
        return 1

    print("=== 逐个 tool_call 拆解 ===")
    for index, call in enumerate(tool_calls):
        print(f"[{index}] id   = {call.id!r}")
        print(f"[{index}] type = {call.type!r}")
        print(f"[{index}] function.name      = {call.function.name!r}")
        arguments = call.function.arguments
        print(f"[{index}] function.arguments = {arguments!r}")
        print(f"[{index}] arguments 类型      = {type(arguments).__name__}")
        try:
            parsed = json.loads(arguments)
            print(f"[{index}] 解析后              = {parsed}")
        except json.JSONDecodeError as exc:
            print(f"[{index}] ⚠️ arguments 不是合法 JSON：{exc}")
        print()

    print("=== 原始响应（节选）===")
    try:
        raw = response.model_dump()
        raw["choices"][0]["message"]["content"] = str(raw["choices"][0]["message"].get("content"))[:80]
        print(json.dumps(raw, ensure_ascii=False, indent=2)[:1500])
    except Exception as exc:  # noqa: BLE001
        print(f"（无法序列化原始响应：{exc}）")

    print()
    print("结论：若上面能看到 function.arguments 且是合法 JSON 字符串，")
    print("      则本项目按 OpenAI 标准格式解析即可（tools.py 已按此实现）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
