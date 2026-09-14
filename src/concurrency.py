"""接口级并发闸门。

为什么需要：站点的查询端点是同步 ``def``，FastAPI 会把它们放进 anyio 线程池执行
（默认 40 槽）。没有闸门时，几十个并发提问就能把线程池占满 —— 那时连 ``/health``
都要排队；而每次提问还会真花钱调模型，堆请求等于堆成本。

满员时**快速失败 429**，不排队：排队只是把所有人一起拖慢，还让成本继续累积。

为什么不用 ``asyncio.Semaphore``：端点是同步函数、跑在线程里，那里没有事件循环可以
await。这里要的是"限并发"，不是"非阻塞 IO"，``threading.BoundedSemaphore`` 正是这个语义。
"""

from __future__ import annotations

import functools
import os
import threading
from typing import Callable, TypeVar

from fastapi import HTTPException

__all__ = ["AsyncSlot", "limit_concurrency", "slot_count"]

F = TypeVar("F", bound=Callable[..., object])


def slot_count(env_name: str, default: int) -> int:
    """从环境变量读槽位数。

    非法值回退默认；下限是 1 —— 0 会让端点**永久 429**，
    那是比"少限一点并发"严重得多的事故。
    """
    try:
        value = int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return max(1, default)
    return max(1, value)


class AsyncSlot:
    """具名并发槽：非阻塞获取，拿不到就表示满了。"""

    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size
        self._sem = threading.BoundedSemaphore(size)

    def acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()


def limit_concurrency(slot: AsyncSlot, detail: str) -> Callable[[F], F]:
    """给端点套一层并发闸门；满员抛 429。

    必须用 ``functools.wraps`` 保住原函数签名：FastAPI 靠 ``inspect.signature``
    解析 ``Depends(...)`` 之类的参数，签名丢了依赖注入就废了。
    """

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not slot.acquire():
                raise HTTPException(status_code=429, detail=detail)
            try:
                return func(*args, **kwargs)
            finally:
                slot.release()

        return wrapper  # type: ignore[return-value]

    return decorate
