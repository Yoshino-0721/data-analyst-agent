"""端到端演示：真实 LLM + 真实 Docker 沙箱 + 真实 Schema。

跑法（需要 Docker 与 ZHIPUAI_API_KEY）：
    python scripts/demo_agent.py

可选：
    PROXY_URL=http://127.0.0.1:7897   需要代理时设置
    DEMO_QUESTION="..."               换一个问题

这一步的价值在于：单测用的全是桩，**只有真跑一次才能证明整条链路是通的**
—— 模型真的会调用工具、代码真的在容器里执行、报错真的能驱动它改。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src.agent.loop import run_agent  # noqa: E402
from src.agent.tools import ToolRuntime  # noqa: E402
from src.config import Settings  # noqa: E402
from src.llm.client import ZhipuClient  # noqa: E402
from src.sandbox.factory import build_executor  # noqa: E402
from src.schema.extractor import extract_schema  # noqa: E402

QUESTION = "哪个地区的销售额最高？最高是多少？请顺便画一张各地区销售额的柱状图。"


def make_sample_csv(directory: Path) -> Path:
    """造一份有地区、销售额、数量的销售数据。"""
    target = directory / "销售数据.csv"
    frame = pd.DataFrame(
        {
            "地区": ["华东", "华南", "华北", "华东", "华南", "华北"] * 40,
            "销售额": [1200, 800, 1500, 300, 2200, 110] * 40,
            "数量": [3, 2, 5, 1, 8, 2] * 40,
        }
    )
    frame.loc[frame.index[::7], "销售额"] *= 2  # 制造一些波动
    frame.to_csv(target, index=False)
    return target


def main() -> int:
    import os

    settings = Settings.from_env()
    workspace = Path(tempfile.mkdtemp(prefix="daa-demo-"))
    data_file = make_sample_csv(workspace)
    artifact_dir = workspace / "artifacts"
    artifact_dir.mkdir(exist_ok=True)

    print("=" * 70)
    print(f"数据文件：{data_file.name}")
    schema = extract_schema(data_file)
    print(schema.to_prompt_string())
    print("=" * 70)

    client = ZhipuClient(
        api_key=settings.require_api_key(),
        model=settings.model,
        base_url=settings.base_url,
        proxy=settings.proxy,
    )
    executor = build_executor(settings)

    def make_run_dir() -> Path:
        target = workspace / f"run_{uuid4().hex[:8]}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    runtime = ToolRuntime(
        executor=executor,
        schemas=[schema],
        data_files=[data_file],
        artifact_dir=artifact_dir,
        run_dir_factory=make_run_dir,
        timeout_seconds=60.0,
    )

    question = os.environ.get("DEMO_QUESTION", QUESTION)
    print(f"\n问题：{question}\n")

    result = run_agent(
        question,
        client=client,
        runtime=runtime,
        schemas=[schema],
        max_steps=settings.agent_max_steps,
        max_repeat_failures=settings.max_repeat_failures,
    )

    print("-" * 70)
    for step in result.steps:
        for call, outcome in zip(step.tool_calls, step.outcomes):
            print(f"\n[step {step.index}] {call.name}  ->  {outcome.status}")
            code = call.parsed_arguments().get("code", "")
            if code:
                preview = code.strip().splitlines()[:6]
                print("  代码预览：" + " | ".join(line.strip() for line in preview))
            print("  回填：")
            for line in outcome.text.splitlines()[:12]:
                print(f"    {line}")

    print("-" * 70)
    print(f"\n收尾方式：{result.terminated_by}")
    if result.artifacts:
        print(f"产物：{', '.join(result.artifacts)}  （目录：{artifact_dir}）")
    print(f"\n回答：\n{result.answer}")
    print(f"\n工作区：{workspace}")

    if "--keep" not in sys.argv:
        shutil.rmtree(workspace, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
