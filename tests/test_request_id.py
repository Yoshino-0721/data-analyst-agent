"""请求 id 的边界（R2 提交 1）。

⚠️ 写 500 相关测试时必须用 ``TestClient(app, raise_server_exceptions=False)``：
默认 ``True`` 会把异常直接抛给测试代码，而不是让你拿到那个 500 响应 ——
真机踩过，别再用默认值写"异常对外呈现"的用例。
"""

from __future__ import annotations

import logging
import re

from src import request_id

UUID12 = re.compile(r"^[0-9a-f]{12}$")


def _health_path(client) -> str:
    """两个项目的健康检查路径不同（p1 是 /health，p2 是 /api/health）。"""
    return "/health" if client.get("/health").status_code != 404 else "/api/health"


def test_valid_inbound_id_is_echoed(client):
    """合法入站头原样回显（含 . 与 _，它们是真实系统的常见字符）。"""
    rid = "req-2026_09.14-abcdef"
    path = _health_path(client)

    resp = client.get(path, headers={request_id.REQUEST_ID_HEADER: rid})

    assert resp.headers.get(request_id.REQUEST_ID_HEADER) == rid


def test_missing_or_invalid_inbound_id_falls_back_to_new(client):
    """没有入站头、或入站头非法 -> 用新生成的 id，且非法内容不得出现在响应里。"""
    path = _health_path(client)

    fresh = client.get(path)
    assert UUID12.match(fresh.headers[request_id.REQUEST_ID_HEADER] or "")

    # 超长（>64）：httpx 允许发送，服务端必须丢弃
    long_value = "a" * 200
    resp = client.get(path, headers={request_id.REQUEST_ID_HEADER: long_value})
    echoed = resp.headers[request_id.REQUEST_ID_HEADER]
    assert echoed != long_value, "超长入站头被回显了"
    assert UUID12.match(echoed or "")
    assert long_value not in resp.text


def test_sanitize_rejects_injection_shaped_values():
    """换行 / 控制字符 / 空串 / 超长一律拒绝 —— 这是防日志注入的唯一一道闸。"""
    for bad in ["abc\ndefg", "abc\r\ndefg", "abc\x00", "abc def", "", "   ", "a" * 65, "中文"]:
        assert request_id.sanitize(bad) is None, bad

    for good in ["abc", "ABC-123", "req_1.2.3", "a" * 64]:
        assert request_id.sanitize(good) == good


def test_log_filter_injects_id_into_every_record():
    """过滤器给每条记录补 request_id —— 业务日志（含同步端点的）才带得上号。"""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    token = request_id._current.set("deadbeef1234")
    try:
        assert request_id.RequestIdFilter().filter(record) is True
        assert record.request_id == "deadbeef1234"
    finally:
        request_id._current.reset(token)

    outside = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    request_id.RequestIdFilter().filter(outside)
    assert outside.request_id == "-"
