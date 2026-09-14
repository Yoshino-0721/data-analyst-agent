"""服务层测试。

**全部用桩**：不启动真实容器、不调用真实 API。
接口这一层的职责只是「接上传 / 跑循环 / 结构化返回」，
验证它不该需要真的跑一次模型。

多用户改造后业务接口都要登录态：``api`` 夹具（tests/conftest.py 里）是「现注册
alice 并自动带 token 的客户端包装」，用例里不必到处写 auth 头；``loaded_client``
在此之上再传一份小 CSV。权限与隔离本身的用例在 ``tests/test_isolation.py``。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import server as server_module
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
def client():
    """匿名 TestClient（公开接口 + 全局异常兜底用例用）。

    `raise_server_exceptions=False`：TestClient 默认会把服务端异常再抛一遍，
    但我们就是要测「全局异常处理器是否把它拍平成 JSON」，所以关掉重抛。
    本模块的 ``api`` / ``loaded_client`` 由 conftest 提供，它们请求的 ``client``
    会解析到这份模块级定义。
    """
    return TestClient(server_module.app, raise_server_exceptions=False)


@pytest.fixture
def loaded_client(api):
    """已上传一份数据、且 LLM/执行器都换成桩的客户端。

    上传走真实的 ``POST /api/upload``（Schema 提取是本地的 pandas，真实跑没问题）。
    """
    resp = api.post(
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
    return api


def stub_clients(responses, results):
    return (
        lambda: ScriptedClient(responses),
        lambda: StubExecutor(results),
    )


def _stub_agent(monkeypatch, responses, results) -> None:
    client_stub, executor_stub = stub_clients(responses, results)
    monkeypatch.setattr(server_module, "get_client", client_stub)
    monkeypatch.setattr(server_module, "get_executor", executor_stub)


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

    def test_public_health_leaks_no_user_data(self, loaded_client):
        """公开接口不能带任何用户数据：文件清单与运行目录都挪去了 /api/workspace。"""
        data = loaded_client.client.get("/api/health").json()
        assert "files" not in data, f"公开健康检查泄露了文件清单：{data}"
        assert "runs_dir" not in data, f"公开健康检查泄露了运行目录：{data}"

    def test_lists_loaded_files(self, loaded_client):
        data = loaded_client.get("/api/workspace").json()
        assert [f["name"] for f in data["files"]] == ["销售.csv"]
        assert data["files"][0]["rows"] == 1

    def test_workspace_requires_login(self, client):
        assert client.get("/api/workspace").status_code == 401


class TestIndex:
    def test_serves_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "私人数据分析师" in resp.text


# ------------------------------------------------------------------ 上传


class TestUpload:
    def test_rejects_unsupported_type(self, api):
        resp = api.post(
            "/api/upload", files=[("files", ("x.pdf", b"%PDF", "application/pdf"))]
        )
        assert resp.status_code == 400, resp.text
        assert "不支持的文件类型" in resp.json()["detail"]

    def test_returns_schema_summary(self, loaded_client):
        data = loaded_client.get("/api/workspace").json()
        file_info = data["files"][0]
        assert file_info["path"] == "/data/销售.csv", "必须是容器内路径"
        assert [c["name"] for c in file_info["columns"]] == ["地区", "销售额"]

    def test_replaces_previous_files(self, loaded_client):
        loaded_client.post(
            "/api/upload", files=[("files", ("新.csv", b"a\n1\n", "text/csv"))]
        )
        data = loaded_client.get("/api/workspace").json()
        assert [f["name"] for f in data["files"]] == ["新.csv"]

    def test_upload_registers_datasets_row(self, loaded_client):
        """上传后 datasets 表里要有对应行 —— 表是可查询索引，工作区是真相。"""
        rows = loaded_client.get("/api/datasets").json()["datasets"]
        assert [r["filename"] for r in rows] == ["销售.csv"]
        assert rows[0]["n_rows"] == 1
        assert rows[0]["n_cols"] == 2


# ------------------------------------------------------------------ 提问


class TestAsk:
    def test_requires_uploaded_data(self, api):
        resp = api.post("/api/ask", json={"question": "多少行？"})
        assert resp.status_code == 400, resp.text
        assert "上传" in resp.json()["detail"]

    def test_rejects_empty_question(self, loaded_client):
        assert loaded_client.post("/api/ask", json={"question": ""}).status_code == 422

    def test_happy_path_returns_answer_and_steps(self, loaded_client, monkeypatch):
        _stub_agent(
            monkeypatch,
            [
                assistant_with_tools(tool_call("run_python", '{"code": "print(1)"}')),
                assistant_text("答案：华东 100。"),
            ],
            [ok_result("华东 100")],
        )

        data = loaded_client.post("/api/ask", json={"question": "哪个地区最高？"}).json()
        assert data["answer"] == "答案：华东 100。"
        assert data["terminated_by"] == "answer"
        assert len(data["steps"]) == 1
        assert data["session_id"] >= 1, "问答必须落进一个会话"

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
        _stub_agent(
            monkeypatch,
            [
                assistant_with_tools(tool_call("run_python", '{"code": "bad"}')),
                assistant_text("我失败了"),
            ],
            [error_result(stderr="KeyError: 地区")],
        )

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
        _stub_agent(
            monkeypatch,
            [
                assistant_with_tools(tool_call("run_python", '{"code": "x"}')),
                assistant_text("ok"),
            ],
            [ok_result("不管怎样先输出点东西")],
        )

        raw = loaded_client.post("/api/ask", json={"question": "q"}).text
        assert str(tmp_path) not in raw


# ------------------------------------------------------------------ 产物


class TestArtifact:
    def test_serves_existing_artifact(self, loaded_client):
        chart = loaded_client.workspace.artifacts_dir / "chart.png"
        chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)

        resp = loaded_client.get("/api/artifact/chart.png")
        assert resp.status_code == 200, resp.text
        assert resp.content.startswith(b"\x89PNG")

    def test_missing_artifact_is_404(self, api):
        assert api.get("/api/artifact/nope.png").status_code == 404

    def test_artifact_without_token_is_401(self, client):
        assert client.get("/api/artifact/chart.png").status_code == 401

    @pytest.mark.parametrize(
        "name",
        ["../../.env", "..%2F..%2F.env", "subdir/../../../etc/passwd", ".."],
    )
    def test_path_traversal_is_blocked(self, api, tmp_path, name):
        """前端传来的文件名是不可信输入 —— 越界必须在服务端拦住。"""
        # 在 storage 之外放一个"机密文件"，穿越成功就能读到它
        secret = tmp_path / ".env"
        secret.write_text("API_KEY=super-secret", encoding="utf-8")

        resp = api.get(f"/api/artifact/{name}")
        assert resp.status_code in (404, 400), f"{name} 竟然返回 {resp.status_code}"
        assert b"super-secret" not in resp.content


class TestReset:
    def test_clears_files(self, loaded_client):
        loaded_client.post("/api/reset")
        data = loaded_client.get("/api/workspace").json()
        assert data["files"] == []

    def test_reset_clears_datasets_table(self, loaded_client):
        assert loaded_client.get("/api/datasets").json()["datasets"] != []
        loaded_client.post("/api/reset")
        assert loaded_client.get("/api/datasets").json()["datasets"] == []


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

    def test_restores_files_after_restart(self, tmp_path):
        """服务重启后，磁盘上已有的数据要自动恢复回会话。

        不恢复的话用户会看到「请先上传数据文件」—— 而他明明刚传过。
        这种「数据消失了」的错觉比真的丢数据还让人困惑。
        """
        first = Session(tmp_path / "s9")
        first.replace_files([("a.csv", b"x\n1\n2\n")])

        # 模拟进程重启：同一目录重新构造 Session
        second = Session(tmp_path / "s9")
        assert [f.name for f in second.files] == ["a.csv"]
        assert [s.file_name for s in second.schemas] == ["a.csv"]
        assert second.schemas[0].n_rows == 2

    def test_restore_skips_hidden_temp_files(self, tmp_path):
        """上传中途的隐藏临时文件不该被当成用户数据恢复出来。"""
        session = Session(tmp_path / "s10")
        session.replace_files([("a.csv", b"x\n1\n")])
        (session.data_dir / ".incoming_abc_broken.csv").write_text("x", encoding="utf-8")

        again = Session(tmp_path / "s10")
        assert [f.name for f in again.files] == ["a.csv"]

    def test_restore_skips_corrupt_files(self, tmp_path):
        """磁盘上有读不了的文件时，只跳过它，不能让服务起不来。"""
        session = Session(tmp_path / "s11")
        session.replace_files([("good.csv", b"x\n1\n")])
        (session.data_dir / "broken.xlsx").write_bytes(b"not an excel file")

        again = Session(tmp_path / "s11")
        assert [f.name for f in again.files] == ["good.csv"]

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


# ------------------------------------------------------------------ 全局异常兜底


class TestUncaughtExceptionHandler:
    """任何逃过接口 try/except 的异常都必须以 JSON 返回 —— 否则浏览器会
    把 21 字节的 "Internal Server Error" 当 JSON 解析，然后报一段
    莫名其妙的「Unexpected token 'I', "Internal S"...」。真机踩过。
    """

    def test_unhandled_exception_returns_json_not_plain_text(self, loaded_client, monkeypatch):
        def boom(*args, **kwargs):
            # ValueError 不会被 ask() 里的 except RuntimeError 抓住 —— 必须确认
            # 它能一路冒到全局处理器，最终落到 JSON 而不是 Starlette 的纯文本 500。
            raise ValueError("boom from deep inside the loop")

        # 必须把 executor / client 也塞成桩，否则 ask() 会先在 get_executor 上 503。
        # 我们只想验证「运行循环里抛了非 RuntimeError」这一条路径。
        class _FakeExecutor:
            def available(self):
                return True, ""

        monkeypatch.setattr(server_module, "get_executor", lambda: _FakeExecutor())
        monkeypatch.setattr(server_module, "get_client", lambda: object())
        monkeypatch.setattr("src.server.run_agent", boom)

        resp = loaded_client.post("/api/ask", json={"question": "随便问"})
        # 关键：必须是 application/json，不能是 text/plain
        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert "detail" in body, f"响应里没有 detail 字段：{body}"
        assert "boom" in body["detail"], f"没把异常信息透出去：{body}"

    def test_validation_error_is_also_json(self, loaded_client):
        """pydantic 校验失败也要走 JSON，否则前端拿到的错误信息是 fragment HTML。"""
        resp = loaded_client.post("/api/ask", json={"question": ""})  # min_length=1
        assert resp.headers["content-type"].startswith("application/json")
