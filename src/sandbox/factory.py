"""执行器策略工厂。

安全上最关键的一点：**策略只由配置决定，模型输出没有任何途径影响它。**

如果让模型（甚至是被执行的代码）能间接选择执行器，整套隔离就是纸糊的 ——
一段代码只要能说服模型「这次用本地模式跑吧」，它就跳出了所有容器约束。
所以这里读的是进程启动时的配置，函数对模型一无所知。
"""

from __future__ import annotations

import logging
from typing import Protocol

from .docker_executor import DockerExecutor
from .executor import Executor, ExecutorConfig

logger = logging.getLogger(__name__)


class RuntimeSettings(Protocol):
    """工厂需要的最小配置面 —— 抽成 Protocol 是为了让调用方能塞桩对象进来。"""

    executor: str
    """'docker' 或 'local'。"""

    env: str
    """运行环境名。只有 'dev' 才允许使用本地执行器。"""

    executor_config: ExecutorConfig


DOCKER = "docker"
LOCAL = "local"


def build_executor(settings: RuntimeSettings) -> Executor:
    """按配置构造执行器。

    Docker 是默认值，也是唯一可用于实际部署的实现；local 仅供本地调试，
    且必须显式声明。这里宁可启动失败，也不悄悄降级到一个没有隔离的方案。
    """
    if settings.executor == LOCAL:
        from .local_executor import LocalSubprocessExecutor

        if settings.env != "dev":
            raise RuntimeError(
                f"LOCAL 执行器无隔离（可读写全盘、可出网），"
                f"仅允许在 APP_ENV=dev 下使用；当前 APP_ENV={settings.env!r}。"
            )
        logger.warning("正在启用 [UNSAFE] LocalSubprocessExecutor —— 仅限本地调试")
        return LocalSubprocessExecutor(settings.executor_config, env_name=settings.env)

    if settings.executor != DOCKER:
        raise ValueError(
            f"未知的 EXECUTOR={settings.executor!r}，可选值为 {DOCKER!r} / {LOCAL!r}"
        )

    executor = DockerExecutor(settings.executor_config)
    logger.info("已启用 %s", executor.describe())
    return executor


__all__ = ["DOCKER", "LOCAL", "RuntimeSettings", "build_executor"]
