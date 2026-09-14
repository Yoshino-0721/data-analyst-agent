"""全局配置。

配置只有两个来源：环境变量，以及可选的仓库根 `.env`（**不入库**）。
这里刻意不引入 python-dotenv —— 解析一个 `K=V` 文件用不上一个依赖，
而少一个依赖就少一个供应链风险。

缺 API Key 时由 `require_api_key()` 抛 RuntimeError：**配置类错误绝不静默降级**。
它和「代码写错了」「执行超时」是完全不同性质的失败，模型改不了，
必须由人来处理 —— 混在一起只会让人对着模型调半天。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox.executor import ExecutorConfig

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-5.2"


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析，只处理 `K=V`，不覆盖已存在的环境变量。"""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """一次运行所需的全部配置。"""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    proxy: str = ""
    """形如 http://127.0.0.1:7897。需要走代理时设置（httpx 会读它）。"""

    # --- 循环行为 ---
    agent_max_steps: int = 8
    """工具调用轮数上限。用尽后摘掉工具再问一次，强制收尾。"""

    max_repeat_failures: int = 3
    """连续多少次**同一处**错误就强制收尾。防撞墙。"""

    history_max_chars: int = 12_000
    """历史消息的字符预算。Schema 文本本身就有几百到几千字符，
    预算给太紧会把关键上下文裁掉。"""

    history_keep_turns: int = 4
    """无论如何都要完整保留的最后 N 轮（每轮 = 一次工具调用链）。"""

    # --- 执行器（与 sandbox.factory.RuntimeSettings 协议对齐）---
    executor: str = "docker"
    env: str = "dev"
    executor_config: ExecutorConfig = field(default_factory=ExecutorConfig)

    # --- 路径 ---
    storage_root: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent / "storage"
    )
    """运行期落盘目录（SQLite 库 storage/app.db、JWT 密钥 storage/secret.key）。
    注意与 server.STORAGE_ROOT（storage/session，单次运行现场）区分开。
    测试里一律由夹具指向 tmp 目录，绝不让用例碰真实 storage/。"""

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Settings:
        _load_dotenv(dotenv_path or Path(__file__).resolve().parent.parent / ".env")
        return cls(
            api_key=os.environ.get("ZHIPUAI_API_KEY", "").strip(),
            base_url=os.environ.get("ZHIPU_BASE_URL", DEFAULT_BASE_URL).strip()
            or DEFAULT_BASE_URL,
            model=os.environ.get("CHAT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            proxy=os.environ.get("PROXY_URL", "").strip(),
            agent_max_steps=_read_int("AGENT_MAX_STEPS", 8),
            max_repeat_failures=_read_int("MAX_REPEAT_FAILURES", 3),
            history_max_chars=_read_int("HISTORY_MAX_CHARS", 12_000),
            history_keep_turns=_read_int("HISTORY_KEEP_TURNS", 4),
            executor=os.environ.get("EXECUTOR", "docker").strip() or "docker",
            env=os.environ.get("APP_ENV", "dev").strip() or "dev",
            storage_root=Path(
                os.environ.get("STORAGE_DIR", "").strip()
                or str(Path(__file__).resolve().parent.parent / "storage")
            ),
        )

    def require_api_key(self) -> str:
        """取 API Key，缺失直接抛 RuntimeError。

        不返回空串、不降级、不打日志了事 —— 缺 Key 属于「环境没配好」，
        模型再怎么改代码都解决不了，必须让人看见。
        """
        if not self.api_key:
            raise RuntimeError(
                "缺少 ZHIPUAI_API_KEY。请在环境变量或仓库根的 .env 中配置：\n"
                "  ZHIPUAI_API_KEY=你的Key\n"
                "申请地址：https://bigmodel.cn/usercenter/apikeys"
            )
        return self.api_key


# 全局单例：auth 包（与项目一 rag-knowledge-base 同构）内部按
# `from src.config import settings` 取存储路径，所以配置模块必须导出一个实例，
# 而不能只在 server.py 里现造一个。server.py 仍持有自己的 `settings`，
# 字段语义与默认值与这里完全一致。
settings = Settings.from_env()


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "Settings", "settings"]
