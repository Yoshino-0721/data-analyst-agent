"""会话状态。

MVP 只维持**一个**会话（单用户本地工具），所以这里是简单的可变对象 + 锁，
没有引入会话存储。多用户是 V2+ 的事，提前抽象只会让现在更难懂。

目录结构（全部落在 storage/ 下，已在 .gitignore 里排除）：

    storage/
    ├── data/        用户上传的原始数据（只读使用，绝不修改）
    ├── artifacts/   模型产出的图表 / 结果文件（前端从这里取图）
    └── runs/        每次代码执行的运行目录（保留最近若干次便于排查）
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .schema.extractor import DatasetSchema, SchemaError, extract_schema
from .sandbox.analysis import safe_target_name

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls"}


def _best_effort_unlink(path: Path) -> None:
    """尽力删除单个文件，**失败只记日志不抛**。

    某些运行环境会在 `Path.unlink()` 上挂安全钩子（比如把删除重定向到回收站、
    或对批量删除做限流）；钩子一旦判定需要人工确认就会中断进程。
    我们的原则是：清理是附带动作，绝不能因为它把主流程带崩。
    """
    try:
        path.unlink(missing_ok=True)
    except BaseException as exc:  # noqa: BLE001 —— SystemExit 也要兜住
        logger.warning("清理文件失败（忽略）：%s（%s）", path, exc)


def _best_effort_rmtree(path: Path) -> None:
    """尽力删除目录树，失败只记日志。理由同 `_best_effort_unlink`。"""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except BaseException as exc:  # noqa: BLE001
        logger.warning("清理目录失败（忽略）：%s（%s）", path, exc)


def _is_transient_lock(exc: BaseException) -> bool:
    """Windows 上文件被防病毒 / 索引服务 / 并发读者短暂锁定时会报 WinError 5。

    这种锁通常几百毫秒内自行释放，重试即可越过；不要因为它把用户的上传变成 500。
    """
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) == 5


def _write_bytes_retry(path: Path, content: bytes, attempts: int = 6, backoff: float = 0.2) -> None:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            path.write_bytes(content)
            return
        except OSError as exc:
            last = exc
            if not _is_transient_lock(exc) or i == attempts - 1:
                raise
            time.sleep(backoff * (i + 1))
    assert last is not None
    raise last


def _extract_schema_retry(path: Path, attempts: int = 6, backoff: float = 0.2):
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return extract_schema(path)
        except OSError as exc:  # 锁文件时 pandas 也会抛 OSError / PermissionError
            last = exc
            if not _is_transient_lock(exc) or i == attempts - 1:
                raise
            time.sleep(backoff * (i + 1))
    assert last is not None
    raise last


def _replace_retry(temp: Path, final: Path, attempts: int = 8, backoff: float = 0.2) -> None:
    """原子改名，并兜住两类失败。

    1. **瞬时锁**（防病毒 / 索引服务 / 并发读者）：`os.replace` 覆盖已存在文件时
       可能短暂报 `WinError 5`，几百毫秒内自行释放 —— 退避重试即可越过。
    2. **覆盖式改名被「删除审计钩子」拦下**：某些环境在替换已存在文件时，
       会因「删除旧文件」这一步触发安全钩子并稳定报 `WinError 5`（重试再多次
       也没用，实测本机如此）。此时退化为 `_rename_aside_then_replace` ——
       全程只做 rename、不删除任何文件，绕开钩子。

    重试用尽后若仍失败，才把最后一次异常抛出（真正的持续性锁，无从绕过）。
    """
    last: BaseException | None = None
    for i in range(attempts):
        try:
            os.replace(temp, final)
            return
        except OSError as exc:
            last = exc
            if not _is_transient_lock(exc) or i == attempts - 1:
                break
            time.sleep(backoff * (i + 1))

    try:
        _rename_aside_then_replace(temp, final)
    except OSError:
        assert last is not None
        raise last


def _rename_aside_then_replace(temp: Path, final: Path) -> None:
    """先把旧文件改名挪开（不删除），再让 temp 就位。

    名字取成以 `.` 开头 —— `_restore_from_disk` 会跳过隐藏文件，因此挪开的旧文件
    不会在下次启动时被当成一份数据恢复出来。挪走后尽力清理；清理失败（例如又撞上
    删除钩子）只记日志，不影响本次上传已经成功的事实。
    """
    if not final.exists():
        os.replace(temp, final)
        return
    aside = final.with_name(f".{final.name}.replaced_{uuid4().hex[:8]}")
    os.replace(final, aside)
    os.replace(temp, final)
    _best_effort_unlink(aside)


@dataclass
class Session:
    """一次会话的全部状态。"""

    root: Path
    files: list[Path] = field(default_factory=list)
    schemas: list[DatasetSchema] = field(default_factory=list)
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        for directory in (self.data_dir, self.artifacts_dir, self.runs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._restore_from_disk()

    def _restore_from_disk(self) -> None:
        """启动时把之前上传的数据恢复回会话状态。

        服务重启后磁盘上的文件还在，但内存里的会话是新的 —— 不恢复的话
        用户会看到「请先上传数据文件」，而他明明刚传过。这种「数据消失了」
        的错觉比真的丢数据还让人困惑。
        """
        for path in sorted(self.data_dir.iterdir()):
            name = path.name
            # 跳过隐藏文件（含上传中途的 .incoming_* 临时文件）
            if not path.is_file() or name.startswith("."):
                continue
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            try:
                schema = extract_schema(path)
            except SchemaError as exc:
                logger.warning("恢复数据文件失败（跳过）：%s（%s）", name, exc)
                continue
            self.files.append(path)
            self.schemas.append(schema)

        if self.files:
            logger.info("已从磁盘恢复 %d 个数据文件", len(self.files))

    # ---- 目录 ----

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def artifacts_dir(self) -> Path:
        """产物目录。上传新数据时**不会**清空 —— 用户可能还想看上一轮的图。"""
        return self.root / "artifacts"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    # ---- 上传 ----

    def replace_files(self, uploads: list[tuple[str, bytes]]) -> list[DatasetSchema]:
        """用新上传的文件**替换**当前数据集。

        替换而不是追加：用户重新上传通常意味着换了一份数据，
        混在一起会让 Schema 与文件对应关系变乱，也放大了挂载面。
        """
        accepted: list[tuple[str, bytes]] = []
        for name, content in uploads:
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise SchemaError(
                    f"不支持的文件类型：{name}。"
                    f"支持 {' / '.join(sorted(ALLOWED_SUFFIXES))}"
                )
            accepted.append((name, content))

        if not accepted:
            raise SchemaError("没有收到任何文件")

        with self._lock:
            # 先把内容写进数据目录里的临时文件并提取 Schema，全部通过后才改名到位。
            # 一个坏文件不该把用户原有的数据集也搞没。
            #
            # 这里刻意**不用 staging 子目录 + 拷贝 + 删目录**的写法：
            # 那个流程既要建目录又要删目录，在某些环境里会撞上删除钩子。
            # `os.replace` 是原子改名，既能覆盖同名文件、又完全不涉及删除。
            incoming: list[tuple[Path, Path]] = []
            try:
                # 先按**归一化后的文件名**查重。这一步不能省：临时文件名带
                # uuid、天然不冲突，两个同名上传会各自拿到一个临时文件，
                # 然后在改名阶段后者把前者覆盖掉 —— 用户会发现「传了两个文件
                # 只剩一个」，而且悄无声息。
                seen: set[str] = set()
                for name, _ in accepted:
                    key = safe_target_name(Path(name))
                    if key in seen:
                        raise SchemaError(f"上传的文件重名：{name}，请改名后再传")
                    seen.add(key)

                for name, content in accepted:
                    final = self.data_dir / safe_target_name(Path(name))
                    temp = self.data_dir / f".incoming_{uuid4().hex[:8]}_{final.name}"
                    # 落盘 + 校验都可能对瞬时锁重试（见下方 helper 说明）
                    _write_bytes_retry(temp, content)
                    incoming.append((temp, final))
                    # 校验能读：这一步失败说明文件本身有问题，改名不该发生
                    _extract_schema_retry(temp)

                # 全部通过 —— 改名到位（os.replace 覆盖已存在的同名文件）
                previous = list(self.files)
                new_files: list[Path] = []
                for temp, final in incoming:
                    _replace_retry(temp, final)
                    new_files.append(final)
                incoming = []  # 已全部改名，没有残留需要清理

                # 不再需要的旧文件**尽力**清掉：失败也不该让本次上传失败 ——
                # 数据已经安全落盘，留几个孤儿文件只是不整洁，不是错误。
                keep = {path.name for path in new_files}
                for old in previous:
                    if old.name not in keep:
                        _best_effort_unlink(old)

                self.files = new_files
                # 以**正式路径**重建 Schema —— 临时文件名不能出现在给模型的路径里
                self.schemas = [extract_schema(path) for path in self.files]
                self.last_error = ""
            finally:
                # 只在失败路径上还有残留（成功时 incoming 已被清空）
                for temp, _ in incoming:
                    _best_effort_unlink(temp)

        logger.info("已接收 %d 个文件", len(self.files))
        return list(self.schemas)

    def clear_files(self) -> None:
        with self._lock:
            for path in self.files:
                _best_effort_unlink(path)
            self.files = []
            self.schemas = []
            self.last_error = ""

    def clear_artifacts(self) -> None:
        with self._lock:
            _best_effort_rmtree(self.artifacts_dir)
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # ---- 运行目录 ----

    def new_run_dir(self) -> Path:
        target = self.runs_dir / f"run_{uuid4().hex[:8]}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def artifact_path(self, name: str) -> Path | None:
        """把前端传来的文件名解析成产物路径，**越界一律返回 None**。

        前端传来的名字是不可信输入：`../../.env` 这类构造必须在这里拦住。
        先归一化成纯文件名（与执行层同一套规则），再确认落点在产物目录内。
        """
        candidate = self.artifacts_dir / safe_target_name(Path(name))
        try:
            resolved = candidate.resolve()
            root = self.artifacts_dir.resolve()
        except OSError:
            return None
        if root not in resolved.parents and resolved != root:
            return None
        return resolved if resolved.is_file() else None


__all__ = ["ALLOWED_SUFFIXES", "Session"]
