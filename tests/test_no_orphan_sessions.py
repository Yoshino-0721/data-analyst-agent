"""失败请求不得在库里留下孤儿会话（C2 复核清单 2.1）。

原先的顺序是"先建会话并 commit，再检查工作区/客户端"，于是"没上传数据就提问"
这种必然失败的请求会在库里留下一条**没有任何消息**的会话行。
顺序调整后：前置条件全部检查完、问答真的成功了，才把会话与消息一起落库。
"""

from __future__ import annotations

from src import server as server_module
from tests.conftest import upload_csv
from tests.conftest_agent import (
    ScriptedClient,
    StubExecutor,
    assistant_text,
    ok_result,
)


def _stub_success(monkeypatch, answer: str = "答案：100") -> None:
    monkeypatch.setattr(
        server_module, "get_client", lambda: ScriptedClient([assistant_text(answer)])
    )
    monkeypatch.setattr(server_module, "get_executor", lambda: StubExecutor([ok_result()]))


def test_ask_without_data_leaves_no_session(api):
    """没上传数据就提问：必然 400 —— 原先会先建会话再报错。"""
    response = api.post("/api/ask", json={"question": "还没上传数据"})

    assert response.status_code == 400, response.text
    assert api.get("/api/sessions").json()["sessions"] == []


def test_missing_api_key_leaves_no_session(api, monkeypatch):
    upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")

    def boom():
        raise RuntimeError("缺少 ZHIPUAI_API_KEY")

    monkeypatch.setattr(server_module, "get_client", boom)

    response = api.post("/api/ask", json={"question": "会失败"})

    assert response.status_code == 503, response.text
    assert api.get("/api/sessions").json()["sessions"] == []


def test_repeated_failures_leave_nothing(api):
    for _ in range(3):
        api.post("/api/ask", json={"question": "又失败一次"})

    assert api.get("/api/sessions").json()["sessions"] == []


def test_success_still_creates_the_session(api, monkeypatch):
    """反向对照：成功路径必须照常落库。"""
    upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
    _stub_success(monkeypatch)

    response = api.post("/api/ask", json={"question": "华东多少"})

    assert response.status_code == 200, response.text
    sid = response.json()["session_id"]
    messages = api.get(f"/api/sessions/{sid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_failure_does_not_touch_an_existing_session(api, monkeypatch):
    upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
    _stub_success(monkeypatch)
    sid = api.post("/api/ask", json={"question": "先成功一次"}).json()["session_id"]

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module, "get_client", boom)
    failed = api.post("/api/ask", json={"question": "这次失败", "session_id": sid})
    assert failed.status_code == 503, failed.text

    messages = api.get(f"/api/sessions/{sid}/messages").json()["messages"]
    assert [m["content"] for m in messages] == ["先成功一次", "答案：100"]
    assert len(api.get("/api/sessions").json()["sessions"]) == 1
