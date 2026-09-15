"""按用户隔离的工作区注册表。

``Session`` 本来就是「一个 root 目录的全套状态」，所以多用户**不需要改它** ——
只需要保证每个用户一个 root、一个实例。目录布局：

    storage/session/users/<uid>/{data,artifacts,runs}/

与项目一 ``rag-knowledge-base/src/userspace.py`` 的差别：那边每个用户的资源是
「数据目录 + 清单 + 向量集合」三样散落的路径，所以用一个 frozen dataclass 描述；
这边三样全部由 ``Session`` 从唯一的 root 推导出来，于是注册表直接持有
``Session`` 实例本身 —— 少一层间接，且天然复用 ``Session`` 自带的落盘恢复。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .config import settings
from .schema.extractor import mount_root_for_executor
from .session import Session

logger = logging.getLogger(__name__)

_registry: dict[int, Session] = {}
_lock = threading.Lock()


def user_root(user_id: int) -> Path:
    """该用户的会话根目录。

    刻意**每次现读** ``settings.storage_root``，不做模块级缓存：测试里
    monkeypatch settings 之后必须立刻生效，缓存路径会让「测试过了、线上不生效」。
    """
    return Path(settings.storage_root) / "session" / "users" / str(user_id)


def get_workspace(user_id: int) -> Session:
    """取该用户的工作区；不存在则按自己的目录建一个（Session 自带落盘恢复）。

    建实例时按**当前执行器**传模型侧路径根：容器模式是 `/data/<name>`，
    本地调试模式是裸文件名。与 ``user_root`` 同理，这里每次现读 ``settings``
    —— 模式判断不能缓存，否则测试/切换执行器后给模型的路径还是旧的。
    """
    with _lock:
        workspace = _registry.get(user_id)
        if workspace is None:
            workspace = Session(
                user_root(user_id),
                model_mount_root=mount_root_for_executor(settings.executor),
            )
            _registry[user_id] = workspace
            logger.info("已为用户 %s 创建工作区：%s", user_id, workspace.root)
        return workspace


def drop_workspace(user_id: int) -> None:
    """把该用户的内存工作区摘掉（管理员删掉其数据集后调用，下次访问会重新读盘）。"""
    with _lock:
        _registry.pop(user_id, None)


def reset_registry() -> None:
    """清空注册表（测试隔离用）。"""
    with _lock:
        _registry.clear()


__all__ = ["drop_workspace", "get_workspace", "reset_registry", "user_root"]
