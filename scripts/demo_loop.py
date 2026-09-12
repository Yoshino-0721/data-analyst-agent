"""沙箱闭环演示：模拟「模型写错 → 拿到结构化反馈 → 自己修好」的过程。

不用真实的 LLM，用一段写死的「模型第一版常犯的错」来演示这套错误
回传机制的价值：**回传给模型的东西决定了它能不能修对。**

运行：
    python scripts/demo_loop.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sandbox.executor import ExecutionRequest  # noqa: E402
from src.sandbox.factory import LOCAL, build_executor  # noqa: E402


class Settings:
    """最小配置 —— 真实实现会从 .env 读。"""

    executor = LOCAL
    env = "dev"
    from src.sandbox.executor import ExecutorConfig

    executor_config = ExecutorConfig(keep_recent_runs=5)


def show(step: str, code: str, result) -> None:
    print(f"\n{'=' * 70}\n【{step}】\n{'-' * 70}")
    print("--- 模型写的代码 ---")
    print(code)
    print("\n--- 回填给模型的结果（to_tool_payload）---")
    payload = result.to_tool_payload()
    for key, value in payload.items():
        print(f"  {key}: {value}")


def make_sample_csv(path: Path) -> Path:
    """生成一份样例数据，让脚本 clone 下来就能跑，不依赖仓库里的数据。"""
    path.write_text(
        "city,amount\nBeijing,120\nShanghai,340\nGuangzhou,210\nBeijing,80\nShanghai,150\n",
        encoding="utf-8",
    )
    return path


def main() -> None:
    executor = build_executor(Settings())
    print(f"执行器：{executor.describe()}")

    with tempfile.TemporaryDirectory() as tmp:
        runs = Path(tmp) / "runs"
        data_file = make_sample_csv(Path(tmp) / "销售.csv")

        # ---- 第一轮：模型猜错了列名（真实场景里最常见的错误）----
        code_v1 = "import pandas as pd\ndf = pd.read_csv('/data/销售.csv')\nprint(df.groupby('城市')['金额'].sum())"
        result = executor.execute(
            ExecutionRequest(
                code=code_v1,
                work_dir=runs / "turn1",
                data_files=[data_file],
                timeout_seconds=10,
            )
        )
        show("第 1 轮：列名猜错", code_v1, result)

        # ---- 第二轮：模型用了个低效实现，超时 ----
        code_v2 = "import time\nprint('开始处理')\ntime.sleep(20)"
        result = executor.execute(
            ExecutionRequest(code=code_v2, work_dir=runs / "turn2", timeout_seconds=1.5)
        )
        show("第 2 轮：低效实现超时", code_v2, result)

        # ---- 第三轮：终于写对（本地模式下用相对路径读数据）----
        # 注：Docker 模式下数据挂在 /data/<文件名>，本地模式数据在工作目录，
        # 用相对路径。真实编排层由 Schema 提取阶段把实际路径告诉模型。
        code_v3 = (
            "import csv, collections\n"
            "totals = collections.Counter()\n"
            "with open('销售.csv', encoding='utf-8') as fh:\n"
            "    for row in csv.DictReader(fh):\n"
            "        totals[row['city']] += int(row['amount'])\n"
            "for city, total in totals.items():\n"
            "    print(f'{city}: {total}')\n"
            "open('summary.txt', 'w', encoding='utf-8').write(str(dict(totals)))\n"
        )
        result = executor.execute(
            ExecutionRequest(
                code=code_v3,
                work_dir=runs / "turn3",
                data_files=[data_file],
                artifact_dir=Path(tmp) / "artifacts",
                timeout_seconds=10,
            )
        )
        show("第 3 轮：修正后成功", code_v3, result)

    print(f"\n{'=' * 70}")
    print("要点：三轮回传的 status 各不相同（RUNTIME_ERROR / TIMEOUT / OK），")
    print("hint 分别指向「核对列名」与「减少数据量」—— 模型拿到什么信号，")
    print("就会往什么方向改。把它们压成一句「执行失败」，下一轮就只能瞎试。")


if __name__ == "__main__":
    main()
