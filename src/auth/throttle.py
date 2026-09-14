"""登录失败节流：连续失败到阈值就短暂锁定该账号。

为什么需要：``/api/auth/login`` 原先没有任何失败成本，可以无限次试口令。
管理员账号一旦被撞开就是全站数据（含所有人的文档与会话），所以至少要给
暴力破解加一个时间成本。

刻意做成**进程内内存**实现，不引 Redis、不落库：

- 本站定位是单机自部署，一个进程就够；
- 落库要建表、要迁移、要清理过期行，为一个节流引入这些不划算。

代价必须说清楚：**多 worker / 多实例部署时各自计数，阈值会变成 N 倍**。
真要多实例共享节流，那是"引入共享存储"的决策，不该由这里悄悄替运维决定。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from src.config import settings

_lock = threading.Lock()

# 内存上限：账号名来自请求体，是不可信输入。没有上限的话，
# 攻击者每请求换一个用户名就能把这张表撑爆（内存 DoS）。
_MAX_TRACKED_ACCOUNTS = 10_000
# 触顶时一次回收到这个水位：避免"每插入一条就淘汰一条"的抖动。
# 回收本身是 O(n log n)，摊销到每 1000 次插入才做一次。
_PRUNE_TARGET = _MAX_TRACKED_ACCOUNTS * 9 // 10


@dataclass
class _State:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = 0.0


_states: dict[str, _State] = {}


def _key(account: str) -> str:
    """归一化账号名。

    去空白 + 转小写：否则 ``"Admin "`` 与 ``"admin"`` 会被当成两个账号，
    攻击者只要变一个字符就能把计数器绕过去。
    """
    return (account or "").strip().lower()


def _prune_locked() -> None:
    """调用方须已持锁。把表回收到 ``_PRUNE_TARGET`` 以下。

    **这是本模块唯一的硬性内存保证**，所以淘汰条件必须真的能命中。
    原实现只清「已解锁**且** ``failures == 0``」的条目 —— 可攻击者每次换用户名时，
    新条目的 ``failures`` 恒 >= 1、永不满足条件，表照样无界增长。

    现在的策略是两遍：

    1. 先清「已解锁且没有失败计数」的条目（最没有保留价值的一批）；
    2. 仍然超水位，就按 ``last_seen`` 从旧到新淘汰 —— **包括处于锁定中的条目**。

    第 2 步的取舍要写明：极端洪泛下，最旧的那些锁可能被提前淘汰（节流退化为
    "尽力而为"）。这是刻意的选择 —— 宁可"锁可能提前失效"，也不能"表无限涨"：
    前者只是这段时间里少挡几次暴力破解，后者会把整个进程拖垮。
    """

    if len(_states) < _MAX_TRACKED_ACCOUNTS:
        return

    now = time.monotonic()
    for key in [k for k, s in _states.items() if s.locked_until <= now and not s.failures]:
        _states.pop(key, None)

    if len(_states) > _PRUNE_TARGET:
        excess = len(_states) - _PRUNE_TARGET
        oldest = sorted(_states.items(), key=lambda item: item[1].last_seen)[:excess]
        for key, _ in oldest:
            _states.pop(key, None)


def tracked_accounts() -> int:
    """当前被跟踪的账号数（内存上限的健康度指标）。"""
    with _lock:
        return len(_states)


def locked_seconds_remaining(account: str) -> int:
    """该账号还要锁多少秒；``0`` 表示没被锁。"""
    with _lock:
        state = _states.get(_key(account))
        if state is None:
            return 0
        now = time.monotonic()
        state.last_seen = now
        remaining = state.locked_until - now
        return int(remaining) + 1 if remaining > 0 else 0


def record_failure(account: str) -> int:
    """记一次失败；若这次触发了锁定，返回锁定时长（秒），否则返回 0。"""
    with _lock:
        _prune_locked()
        state = _states.setdefault(_key(account), _State())
        state.last_seen = time.monotonic()
        state.failures += 1
        threshold = max(1, int(settings.login_max_failures))
        if state.failures >= threshold:
            lock_seconds = max(1, int(settings.login_lock_seconds))
            state.locked_until = time.monotonic() + lock_seconds
            # 锁定期满后从 0 重新计数：否则"刚解锁就又满"，
            # 正常用户输错一次会被立刻再锁一轮。
            state.failures = 0
            return lock_seconds
        return 0


def record_success(account: str) -> None:
    """登录成功即清零，避免"昨天错了 3 次"影响今天。"""
    with _lock:
        _states.pop(_key(account), None)


def reset() -> None:
    """清空全部计数（**测试隔离用**，运维侧不需要手动调用）。"""
    with _lock:
        _states.clear()


__all__ = [
    "locked_seconds_remaining",
    "record_failure",
    "record_success",
    "reset",
    "tracked_accounts",
]
