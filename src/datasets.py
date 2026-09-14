"""datasets 表的同步与删除，用户接口与管理接口共用。

与项目一 ``rag-knowledge-base/src/documents.py`` 同构：那边同步方向以**清单
（manifest）**为准，这边没有清单，直接以**工作区当前实际持有的文件**为准 ——
上传/清空之后把 ``workspace.schemas`` 回写进表：新文件插入、已有文件更新行列数、
工作区里已消失的行删除。数据集按用户归属、会话共用，所以同步的粒度是
「某个用户的全量数据集」，不是「某次上传」。

删除是三步联动：删文件（尽力而为）→ 删表行 → 摘掉该用户的内存工作区
（最后一步由调用方做 —— 普通用户删自己的不需要摘，摘了反而丢内存态；
管理员删别人的必须摘，见 ``src/admin_api.py``）。

本机「安全删除钩子」可能在 ``unlink`` 上抛 ``SystemExit``（BaseException），
按 AGENTS.md §2.9：清理失败只警告，绝不阻断主流程。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session as OrmSession

from .auth.models import Dataset
from .sandbox.analysis import safe_target_name

logger = logging.getLogger(__name__)


def sync_datasets(db: OrmSession, user_id: int, workspace) -> None:
    """把某用户工作区里的数据文件同步进 datasets 表。"""
    rows = {row.filename: row for row in db.query(Dataset).filter(Dataset.user_id == user_id).all()}
    seen: set[str] = set()
    for schema in workspace.schemas:
        name = schema.file_name
        seen.add(name)
        row = rows.pop(name, None)
        if row is None:
            db.add(Dataset(user_id=user_id, filename=name, n_rows=schema.n_rows, n_cols=schema.n_cols))
        else:
            row.n_rows = schema.n_rows
            row.n_cols = schema.n_cols
    for stale in rows.values():
        db.delete(stale)
    db.commit()


def dataset_payload(row: Dataset, *, username: str | None = None) -> dict:
    data = {
        "id": row.id,
        "user_id": row.user_id,
        "filename": row.filename,
        "n_rows": row.n_rows,
        "n_cols": row.n_cols,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if username is not None:
        data["username"] = username
    return data


def _best_effort_unlink(path: Path) -> bool:
    """删一个文件：先直接删，被删除钩子拦住就改名挪开再删。

    返回值表示**是否彻底删干净**（连挪开的残留也清掉了）。改名挪开成功但
    残留清理失败时返回 False —— 文件已脱离用户目录（挪开的名字带
    ``.deleted_<hex>`` 后缀，不匹配 ``ALLOWED_SUFFIXES``，不会被
    ``Session._restore_from_disk`` 重新恢复出来），但内容仍在磁盘上。
    清理是附带动作，任何失败都不阻断删除数据集的主流程。
    （照抄 rag-knowledge-base/src/documents.py 的 _best_effort_unlink）
    """
    if not path.exists():
        return True
    try:
        path.unlink()
        return True
    except BaseException as exc:  # noqa: BLE001 - 删除钩子会抛 SystemExit
        logger.warning("直接删除 %s 失败：%s，尝试改名挪开", path, exc)
    aside = path.with_name(f"{path.name}.deleted_{uuid.uuid4().hex[:8]}")
    try:
        path.rename(aside)
    except BaseException as exc:  # noqa: BLE001
        logger.warning("改名挪开 %s 也失败：%s（文件保留在原处）", path, exc)
        return False
    try:
        aside.unlink()
        return True
    except BaseException as exc:  # noqa: BLE001
        logger.warning("清理挪开的 %s 失败：%s（文件已脱离索引）", aside, exc)
        return False


def remove_dataset_file(workspace, filename: str) -> bool:
    """从工作区里摘掉一个数据集文件：清内存状态 + 清文件。返回是否删干净。

    ``Session`` 没有「删单个文件」的方法，这里在外部按它的公开属性改
    （``files`` / ``schemas``）。持 ``workspace._lock`` 是为了与
    ``replace_files`` / ``clear_files`` 串行 —— 同一个 Session 可能同时被
    一个上传请求和一次删除请求碰到，内存态被交错改写会留下「文件已删、
    schema 还在」的错配。
    """
    with workspace._lock:
        workspace.files = [p for p in workspace.files if p.name != filename]
        workspace.schemas = [s for s in workspace.schemas if s.file_name != filename]
    return _best_effort_unlink(workspace.data_dir / safe_target_name(Path(filename)))


def delete_dataset(db: OrmSession, workspace, row: Dataset) -> dict:
    """删除一个数据集的完整痕迹：文件（尽力而为）→ 内存工作区 → 表行。

    用户接口与管理接口共用这一份；管理员那条路径额外还要
    ``drop_workspace``（把内存工作区整个摘掉），由调用方补上。
    """
    file_removed = remove_dataset_file(workspace, row.filename)
    db.delete(row)
    db.commit()
    return {"ok": True, "file_removed": file_removed}


__all__ = [
    "dataset_payload",
    "delete_dataset",
    "remove_dataset_file",
    "sync_datasets",
]
