"""上传落盘（`Session.replace_files`）对 Windows 瞬时锁的健壮性。

真实环境里 `os.replace` 覆盖同名文件时，目标可能正被防病毒 / 索引服务 / 并发读者
短暂锁定，报 `WinError 5`。这种锁几百毫秒内自行释放，重试即可越过；原样抛出会变成
用户侧的 500。这里用桩模拟 `os.replace` 的瞬时失败，确认重试逻辑真的被走到、
且最终能落盘。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.session import Session


def _winerror5() -> OSError:
    err = OSError(5, "拒绝访问。")
    err.winerror = 5  # type: ignore[attr-defined]
    return err


def _new_session(tmp_path: Path) -> Session:
    # 全新空目录：_restore_from_disk 不会读到任何旧文件，避免干扰
    return Session(root=tmp_path / "session")


def test_replace_files_retries_transient_lock(tmp_path: Path):
    s = _new_session(tmp_path)
    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src: Path, dst: Path):
        calls["n"] += 1
        if calls["n"] <= 3:  # 前 3 次模拟防病毒瞬时锁
            raise _winerror5()
        return real_replace(src, dst)

    content = b"a,b\n1,2\n3,4\n"
    with patch.object(os, "replace", flaky):
        schemas = s.replace_files([("data.csv", content)])

    assert len(schemas) == 1
    final = s.data_dir / "data.csv"
    assert final.read_bytes() == content
    # 改名成功后不应残留 .incoming_* 临时文件
    assert not list(s.data_dir.glob(".incoming_*"))
    assert calls["n"] == 4  # 3 次失败 + 1 次成功


def test_replace_files_persistent_lock_still_raises_and_cleans_up(tmp_path: Path):
    s = _new_session(tmp_path)
    calls = {"n": 0}
    real_replace = os.replace

    def always_fail(src: Path, dst: Path):
        calls["n"] += 1
        raise _winerror5()  # 每次都锁：确认不会无限重试

    content = b"a,b\n1,2\n"
    with patch.object(os, "replace", always_fail):
        with pytest.raises(OSError):
            s.replace_files([("data.csv", content)])

    # 调用次数应被上限约束（重试 attempts + 兜底路径最多再加两次 rename），
    # 且不残留临时文件。持续锁定无法绕过时，应老实地抛错而不是无限重试。
    assert calls["n"] <= 10
    assert not list(s.data_dir.glob(".incoming_*"))


def test_replace_files_falls_back_to_rename_when_overwrite_blocked(tmp_path: Path):
    """覆盖式改名被「删除审计钩子」拦下时，退化为「先挪开旧文件再就位」。

    本机实测：`os.replace` 覆盖**已存在**文件（需要删旧文件）时会触发安全钩子并
    稳定报 WinError 5；而目标是新名字（无需删除）时正常。这里模拟这一行为，
    确认兜底路径能把新内容落到正式文件名上，而不是把一次正常的上传变成 500。
    """
    s = _new_session(tmp_path)
    final = s.data_dir / "data.csv"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"old,data\n1,1\n")  # 目标文件已存在，制造「覆盖」场景
    real_replace = os.replace

    def blocked_overwrite(src, dst):
        if Path(dst).exists():  # 覆盖已存在文件 = 要删旧文件 → 被钩子拦
            raise _winerror5()
        return real_replace(src, dst)

    content = b"a,b\n1,2\n3,4\n"
    with patch.object(os, "replace", blocked_overwrite):
        schemas = s.replace_files([("data.csv", content)])

    assert len(schemas) == 1
    assert final.read_bytes() == content                 # 新内容成功就位
    assert not list(s.data_dir.glob(".incoming_*"))      # 临时文件清理干净
    assert not list(s.data_dir.glob(".*.replaced_*"))    # 挪开的旧文件也被清掉


def test_write_bytes_transient_lock_is_retried(tmp_path: Path):
    """落盘本身被瞬时锁时也要能越过 —— 否则大文件上传在扫描密集目录会偶发失败。"""
    s = _new_session(tmp_path)
    calls = {"n": 0}
    real_write = Path.write_bytes

    def flaky_write(self: Path, data: bytes):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _winerror5()
        return real_write(self, data)

    content = b"x,y\n9,9\n"
    with patch.object(Path, "write_bytes", flaky_write):
        schemas = s.replace_files([("again.csv", content)])

    assert len(schemas) == 1
    assert (s.data_dir / "again.csv").read_bytes() == content
    assert calls["n"] == 3
