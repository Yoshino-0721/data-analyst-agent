"""会话与消息持久化测试。

问答的落库承诺有三条，一条都不能少：

1. **user / assistant 两条消息都要进库**（不是只在响应里返回）；
2. **执行轨迹（``steps``）与产物列表随 assistant 消息一起进 ``meta``** ——
   这是管理员事后复盘「模型到底跑了什么」的唯一来源，丢了就再也找不回来；
3. **重启后还在**（新建一个 ``TestClient``，并把内存工作区注册表清空）。

标题取首问前 50 字、删会话级联删消息也在这里守住。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import server as server_module
from src import workspaces
from src.auth import db as auth_db
from src.auth.models import Message
from tests.conftest import auth_headers, upload_csv
from tests.conftest_agent import (
    ScriptedClient,
    StubExecutor,
    assistant_text,
    assistant_with_tools,
    error_result,
    ok_result,
    tool_call,
)

QUESTION = "华东的销售额是多少？"


def stub_agent(monkeypatch, results, *, answer: str = "答案：42") -> None:
    """把 LLM 与执行器都换成桩：先调一次 run_python，再收尾给答案。"""
    monkeypatch.setattr(
        server_module,
        "get_client",
        lambda: ScriptedClient(
            [
                assistant_with_tools(tool_call("run_python", '{"code": "print(42)"}')),
                assistant_text(answer),
            ]
        ),
    )
    monkeypatch.setattr(server_module, "get_executor", lambda: StubExecutor(results))


@pytest.fixture
def asked(api, monkeypatch, tmp_path):
    """alice 传了一份数据、问了一次 —— 返回 (带 token 的客户端, 响应体)。

    执行结果里的 artifacts 是 **Path**（执行层产出的原始形态），回填给模型与
    前端时才被净化成纯文件名 —— 这里按真实契约传 Path，才能验到那一步净化。
    """
    upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
    stub_agent(
        monkeypatch, [ok_result("42", artifacts=(tmp_path / "deep" / "chart.png",))]
    )
    response = api.post("/api/ask", json={"question": QUESTION})
    assert response.status_code == 200, response.text
    return api, response.json()


# ---------------------------------------------------------------- 消息落库


class TestMessagesPersisted:
    def test_user_and_assistant_messages_are_stored(self, asked):
        api, data = asked
        body = api.get(f"/api/sessions/{data['session_id']}/messages").json()

        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][0]["content"] == QUESTION
        assert body["messages"][1]["content"] == "答案：42"

    def test_assistant_meta_carries_full_trace(self, asked):
        """meta 里必须是**完整**轨迹：缺了代码或执行结果，复盘就没有意义。"""
        api, data = asked
        messages = api.get(f"/api/sessions/{data['session_id']}/messages").json()["messages"]
        meta = messages[1]["meta"]

        assert meta is not None, "assistant 消息必须带 meta"
        assert meta["terminated_by"] == "answer"
        assert meta["artifacts"] == ["chart.png"]
        assert meta["steps"] == data["steps"], "meta 里的轨迹要与当次返回给前端的一致"
        assert meta["steps"][0]["calls"][0]["code"] == "print(42)"
        assert meta["steps"][0]["outcomes"][0]["status"] == "OK"
        assert meta["steps"][0]["outcomes"][0]["stdout"] == "42"

    def test_user_message_has_no_meta(self, asked):
        api, data = asked
        messages = api.get(f"/api/sessions/{data['session_id']}/messages").json()["messages"]
        assert messages[0]["meta"] is None

    def test_intermediate_failure_trace_is_also_kept(self, api, monkeypatch):
        """失败的那一步也要进 meta —— 只留成功步骤等于把排查现场删了。"""
        upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
        monkeypatch.setattr(
            server_module,
            "get_client",
            lambda: ScriptedClient(
                [
                    assistant_with_tools(tool_call("run_python", '{"code": "bad"}')),
                    assistant_with_tools(
                        tool_call("run_python", '{"code": "good"}', "call_2")
                    ),
                    assistant_text("答案：100"),
                ]
            ),
        )
        monkeypatch.setattr(
            server_module,
            "get_executor",
            lambda: StubExecutor(
                [error_result(stderr="KeyError: 地区"), ok_result("后来改对了")]
            ),
        )

        data = api.post("/api/ask", json={"question": QUESTION}).json()
        messages = api.get(f"/api/sessions/{data['session_id']}/messages").json()["messages"]
        statuses = [o["status"] for s in messages[1]["meta"]["steps"] for o in s["outcomes"]]
        assert statuses == ["RUNTIME_ERROR", "OK"]
        assert "KeyError" in messages[1]["meta"]["steps"][0]["outcomes"][0]["stderr"]

    def test_second_question_appends_to_same_session(self, api, monkeypatch):
        upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
        stub_agent(monkeypatch, [ok_result()])
        sid = api.post("/api/ask", json={"question": "第一问"}).json()["session_id"]

        stub_agent(monkeypatch, [ok_result()])
        api.post("/api/ask", json={"question": "第二问", "session_id": sid})

        messages = api.get(f"/api/sessions/{sid}/messages").json()["messages"]
        assert [m["content"] for m in messages] == [
            "第一问",
            "答案：42",
            "第二问",
            "答案：42",
        ]


# ---------------------------------------------------------------- 重启后仍在


class TestPersistenceAcrossRestart:
    def test_messages_survive_restart(self, asked):
        """重启后消息仍在：清掉内存工作区 + 换一个全新的 ASGI 应用实例。"""
        _api, data = asked
        sid = data["session_id"]

        workspaces.reset_registry()  # 内存态归零，只能从 SQLite 读
        restarted = TestClient(server_module.app)

        login = restarted.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        )
        assert login.status_code == 200, login.text
        headers = auth_headers(login.json()["token"])

        body = restarted.get(f"/api/sessions/{sid}/messages", headers=headers).json()
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][1]["meta"]["steps"], "重启后轨迹也要还在"


# ---------------------------------------------------------------- 标题


class TestSessionTitle:
    def test_title_defaults_to_first_question(self, api, monkeypatch):
        upload_csv(api, "销售.csv")
        stub_agent(monkeypatch, [ok_result()])

        api.post("/api/ask", json={"question": QUESTION})

        sessions = api.get("/api/sessions").json()["sessions"]
        assert sessions[0]["title"] == QUESTION

    def test_long_question_is_truncated_to_50_chars(self, api, monkeypatch):
        upload_csv(api, "销售.csv")
        stub_agent(monkeypatch, [ok_result()])

        question = "很长的提问" * 20  # 100 字
        api.post("/api/ask", json={"question": question})

        title = api.get("/api/sessions").json()["sessions"][0]["title"]
        assert title == question[:50]
        assert len(title) == 50

    def test_explicit_title_is_not_overwritten(self, api, monkeypatch):
        upload_csv(api, "销售.csv")
        sid = api.post("/api/sessions", json={"title": "我的分析"}).json()["id"]
        stub_agent(monkeypatch, [ok_result()])

        api.post("/api/ask", json={"question": QUESTION, "session_id": sid})

        titles = {s["id"]: s["title"] for s in api.get("/api/sessions").json()["sessions"]}
        assert titles[sid] == "我的分析"


# ---------------------------------------------------------------- 会话 CRUD


class TestSessionCrud:
    def test_new_session_has_no_messages(self, api):
        sid = api.post("/api/sessions", json=None).json()["id"]
        body = api.get(f"/api/sessions/{sid}/messages").json()
        assert body["messages"] == []
        assert body["session"]["title"] == "新会话"

    def test_rename(self, api):
        sid = api.post("/api/sessions", json=None).json()["id"]
        renamed = api.patch(f"/api/sessions/{sid}", json={"title": "改名了"})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "改名了"

    def test_rename_rejects_empty_title(self, api):
        sid = api.post("/api/sessions", json=None).json()["id"]
        assert api.patch(f"/api/sessions/{sid}", json={"title": ""}).status_code == 422

    def test_delete_cascades_messages(self, asked):
        api, data = asked
        sid = data["session_id"]

        assert api.delete(f"/api/sessions/{sid}").json()["ok"] is True
        assert api.get(f"/api/sessions/{sid}/messages").status_code == 404
        assert api.get("/api/sessions").json()["sessions"] == []

        # 不只是「归属校验挡住了」—— 消息行确实被级联删掉了
        factory = auth_db.get_session_factory()
        with factory() as db:
            assert db.query(Message).filter_by(session_id=sid).all() == []

    def test_blank_question_is_400(self, api):
        upload_csv(api, "销售.csv")
        response = api.post("/api/ask", json={"question": "   "})
        assert response.status_code == 400, response.text
        assert "不能为空" in response.json()["detail"]

    def test_ask_without_session_id_creates_one(self, api, monkeypatch):
        upload_csv(api, "销售.csv")
        stub_agent(monkeypatch, [ok_result()])

        sid = api.post("/api/ask", json={"question": QUESTION}).json()["session_id"]
        assert sid >= 1
        assert [s["id"] for s in api.get("/api/sessions").json()["sessions"]] == [sid]
