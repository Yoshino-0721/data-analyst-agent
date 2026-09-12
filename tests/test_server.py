"""服务层测试。

**全部用桩**：不启动真实容器、不调用真实 API。
接口这一层的职责只是「接上传 / 跑循环 / 结构化返回」，
验证它不该需要真的跑一次模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import server as server_module
from src.agent.tools import ToolRuntime
from src.session import Session
from tests.conftest_agent import (
    ScriptedClient,
    StubExecutor,
    assistant_text,
    assistant_with_tools,
    error_result,
    ok_result,
    tool_call,
)


@pytest.fixture
def client(monkeypatch, tmp_path):
    """每个测试用独立的 storage 目录，避免相互污染。"""
    monkeypatch.setattr(server_module, "session", Session(tmp_path / "session"))
    return TestClient(server_module.app)


@pytest.fixture
def loaded_client(client, monkeypatch, tmp_path):
    """已上传一份数据、且 LLM/执行器都换成桩的客户端。"""
    resp = client.post(
        "/api/upload",
        files=[
            (
                "files",
                (
                    "销售.csv",
                    "地区,销售额\n华东,100\n".encode("utf-8"),
                    "text/csv",
                ),
            )
        ],
    )
    assert resp.status_code == 200, resp.text
    return client


def stub_clients(responses, results):
    return (
        lambda: ScriptedClient(responses),
        lambda: StubExecutor(results),
    )


# ------------------------------------------------------------------ 基础


class TestHealth:
    def test_reports_configuration(self, client):
        data = client.get("/api/health").json()
        assert data["ok"] is True
        assert "has_api_key" in data
        assert "executor_ok" in data

    def test_health_never_raises_without_api_key(self, client, monkeypatch):
        """缺 Key 时健康检查仍要返回 200 —— 页面要能打开并把原因显示出来。"""
        monkeypatch.setattr(server_module.settings, "api_key", "", raising=False)
        assert client.get("/api/health").status_code == 200

    def test_lists_loaded_files(self, loaded_client):
        data = loaded_client.get("/api/health").json()
        assert [f["name"] for f in data["files"]] == ["销售.csv"]
        assert data["files"][0]["rows"] == 1


class TestIndex:
    def test_serves_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "私人数据分析师" in resp.text


# ------------------------------------------------------------------ 上传


class TestUpload:
    def test_rejects_unsupported_type(self, client):
        resp = client.post(
            "/api/upload", files=[("files", ("x.pdf", b"%PDF", "application/pdf"))]
        )
        assert resp.status_code == 400
        assert "不支持的文件类型" in resp.json()["detail"]

    def test_returns_schema_summary(self, loaded_client):
        data = loaded_client.get("/api/health").json()
        file_info = data["files"][0]
        assert file_info["path"] == "/data/销售.csv", "必须是容器内路径"
        assert [c["name"] for c in file_info["columns"]] == ["地区", "销售额"]

    def test_replaces_previous_files(self, loaded_client):
        loaded_client.post(
            "/api/upload", files=[("files", ("新.csv", b"a\n1\n", "text/csv"))]
        )
        data = loaded_client.get("/api/health").json()
        assert [f["name"] for f in data["files"]] == ["新.csv"]


# ------------------------------------------------------------------ 提问


class TestAsk:
    def test_requires_uploaded_data(self, client):
        resp = client.post("/api/ask", json={"question": "多少行？"})
        assert resp.status_code == 400
        assert "上传" in resp.json()["detail"]

    def test_rejects_empty_question(self, loaded_client):
        assert loaded_client.post("/api/ask", json={"question": ""}).status_code == 422

    def test_happy_path_returns_answer_and_steps(self, loaded_client, monkeypatch):
        client_stub, executor_stub = stub_clients(
            [
                assistant_with_tools(tool_call("run_python", '{"code": "print(1)"}')),
                assistant_text("答案：华东 100。"),
            ],
            [ok_result("华东 100")],
        )
        monkeypatch.setattr(server_module, "get_client", client_stub)
        monkeypatch.setattr(server_module, "get_executor", executor_stub)

        data = loaded_client.post("/api/ask", json={"question": "哪个地区最高？"}).json()
        assert data["answer"] == "答案：华东 100。"
        assert data["terminated_by"] == "answer"
        assert len(data["steps"]) == 1

    def test_generated_code_is_returned_verbatim(self, loaded_client, monkeypatch):
        """代码要原样带给前端 —— 这是用户信任的来源，不能只给摘要。"""
        code = "import pandas as pd\ndf = pd.read_csv('/data/销售.csv')\nprint(df)"
        client_stub, executor_stub = stub_clients(
            [
                assistant_with_tools(tool_call("run_python", '{"code": "x"}')),
                assistant_text("ok"),
            ],
            [ok_result()],
        )
        monkeypatch.setattr(server_module, "get_client", client_stub)
        monkeypatch.setattr(server_module, "get_executor", executor_stub)

        # 让桩返回真实代码（直接构造 ToolCall 的 arguments）
        import json as _json

        client_stub = lambda: ScriptedClient(  # noqa: E731
            [
                assistant_with_tools(
                    tool_call("run_python", _json.dumps({"code": code}))
                ),
                assistant_text("ok"),
            ]
        )
        monkeypatch.setattr(server_module, "get_client", client_stub)

        data = loaded_client.post("/api/ask", json={"question": "q"}).json()
        assert data["steps"][0]["calls"][0]["code"] == code

    def test_error_outcome_carries_status_and_traceback(self, loaded_client, monkeypatch):
        client_stub, executor_stub = stub_clients(
            [
                assistant_with_tools(tool_call("run_python", '{"code": "bad"}')),
                assistant_text("我失败了"),
            ],
            [error_result(stderr="KeyError: 地区")],
        )
        monkeypatch.setattr(server_module, "get_client", client_stub)
        monkeypatch.setattr(server_module, "get_executor", executor_stub)

        data = loaded_client.post("/api/ask", json={"question": "q"}).json()
        outcome = data["steps"][0]["outcomes"][0]
        assert outcome["status"] == "RUNTIME_ERROR"
        assert "KeyError" in outcome["stderr"]
        assert outcome["hint"], "hint 要单独带出去，前端要展示"

    def test_reports_missing_api_key_as_503(self, loaded_client, monkeypatch):
        def boom():
            raise RuntimeError("缺少 ZHIPUAI_API_KEY。请在环境变量或 .env 中配置后再启动。")

        monkeypatch.setattr(server_module, "get_client", boom)
        resp = loaded_client.post("/api/ask", json={"question": "q"})
        assert resp.status_code == 503
        assert "ZHIPUAI_API_KEY" in resp.json()["detail"]

    def test_no_host_path_leaks_to_frontend(self, loaded_client, monkeypatch, tmp_path):
        """给前端的 payload 里不能有宿主路径 —— 前端会上报错误、贴截图，
        宿主路径泄漏出去和泄漏进 Prompt 一样糟。"""
        client_stub, executor_stub = stub_clients(
            [
                assistant_with_tools(tool_call("run_python", '{"code": "x"}')),
                assistant_text("ok"),
            ],
            [ok_result("不管怎样先输出点东西")],
        )
        monkeypatch.setattr(server_module, "get_client", client_stub)
        monkeypatch.setattr(server_module, "get_executor", executor_stub)

        raw = loaded_client.post("/api/ask", json={"question": "q"}).text
        assert str(tmp_path) not in raw


# ------------------------------------------------------------------ 产物


class TestArtifact:
    def test_serves_existing_artifact(self, client, monkeypatch, tmp_path):
        session = Session(tmp_path / "s2")
        monkeypatch.setattr(server_module, "session", session)
        chart = session.artifacts_dir / "chart.png"
        chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)

        resp = client.get("/api/artifact/chart.png")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")

    def test_missing_artifact_is_404(self, client):
        assert client.get("/api/artifact/nope.png").status_code == 404

    @pytest.mark.parametrize(
        "name",
        ["../../.env", "..%2F..%2F.env", "subdir/../../../etc/passwd", ".."],
    )
    def test_path_traversal_is_blocked(self, client, monkeypatch, tmp_path, name):
        """前端传来的文件名是不可信输入 —— 越界必须在服务端拦住。"""
        session = Session(tmp_path / "s3")
        monkeypatch.setattr(server_module, "session", session)
        # 在 storage 之外放一个"机密文件"，穿越成功就能读到它
        secret = tmp_path / ".env"
        secret.write_text("API_KEY=super-secret", encoding="utf-8")

        resp = client.get(f"/api/artifact/{name}")
        assert resp.status_code in (404, 400), f"{name} 竟然返回 {resp.status_code}"
        assert b"super-secret" not in resp.content


class TestReset:
    def test_clears_files(self, loaded_client):
        loaded_client.post("/api/reset")
        data = loaded_client.get("/api/health").json()
        assert data["files"] == []


# ------------------------------------------------------------------ 会话层


class TestSession:
    def test_rejects_duplicate_names(self, tmp_path):
        """同名文件在容器里会相互覆盖，必须在入口拦下。"""
        from src.schema.extractor import SchemaError

        session = Session(tmp_path / "s4")
        with pytest.raises(SchemaError, match="重名"):
            session.replace_files([("a.csv", b"x\n1\n"), ("a.csv", b"y\n2\n")])

    def test_failed_upload_keeps_previous_data(self, tmp_path):
        """一个新传坏文件不该把用户原有的数据集搞没。"""
        from src.schema.extractor import SchemaError

        session = Session(tmp_path / "s5")
        session.replace_files([("good.csv", b"x\n1\n2\n")])
        with pytest.raises(SchemaError):
            session.replace_files([("broken.xlsx", b"not an excel file")])
        assert [f.name for f in session.files] == ["good.csv"]
        assert session.schemas, "原有 Schema 也要保留"

    def test_run_dirs_are_unique(self, tmp_path):
        session = Session(tmp_path / "s6")
        assert session.new_run_dir() != session.new_run_dir()

    def test_artifact_path_rejects_outside(self, tmp_path):
        session = Session(tmp_path / "s7")
        assert session.artifact_path("../../.env") is None
        assert session.artifact_path("missing.png") is None

    def test_cleanup_failure_does_not_break_upload(self, tmp_path, monkeypatch):
        """清理动作失败绝不能让上传失败。

        真机上踩到过：运行环境给删除操作挂了安全钩子，钩子判定需要人工确认时
        直接抛 `SystemExit`（BaseException，逃得过 `ignore_errors=True`），
        结果一次成功的上传被撤回动作带崩成 500。

        数据已经落盘了，留几个孤儿文件只是不整洁，不是错误。
        """
        import pathlib

        import src.session as session_module

        session = Session(tmp_path / "s8")
        session.replace_files([("a.csv", b"x\n1\n")])

        def boom(*args, **kwargs):
            raise SystemExit(1)

        monkeypatch.setattr(session_module.shutil, "rmtree", boom)
        monkeypatch.setattr(pathlib.Path, "unlink", boom)

        schemas = session.replace_files([("b.csv", b"y\n2\n")])
        assert [s.file_name for s in schemas] == ["b.csv"]
        assert [f.name for f in session.files] == ["b.csv"]
