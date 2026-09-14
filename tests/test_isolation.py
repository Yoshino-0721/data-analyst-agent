"""跨用户隔离测试：会话 / 数据集 / 沙箱挂载 / 权限门槛。

多用户改造的核心承诺是「A 的数据 B 一点都碰不到」，这个承诺的每一层都在这儿
被钉住：

- **接口层**：越权一律 404（不是 403 —— 403 等于告诉对方「这个 id 是存在的」）；
  没带 token 401，伪造 token 401。
- **沙箱层**：提问时挂给执行器的只有本人文件（``ToolRuntime.data_files``）——
  跨用户隔离最终就落在这儿，接口层拦得再严，挂载清单错了也是白搭。
- **管理接口**：普通用户访问 ``/api/admin/*`` 的任何子路径都是 403。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src import server as server_module
from src import workspaces
from src.auth import db as auth_db
from src.auth.models import User
from src.auth.security import create_token
from tests.conftest import auth_headers, upload_csv
from tests.conftest_agent import (
    ScriptedClient,
    StubExecutor,
    assistant_text,
    assistant_with_tools,
    ok_result,
    tool_call,
)

CSV = b"a,b\n1,2\n"


def _tool_script() -> list:
    """一段「先调工具、再给答案」的脚本。

    必须真有一次工具调用才会走到执行器 —— 直接给答案的话执行器一次都不会被
    调用，挂载清单就无从验起（第一次写这组用例时正是这么假绿的）。
    """
    return [
        assistant_with_tools(tool_call("run_python", '{"code": "print(1)"}')),
        assistant_text("ok"),
    ]


# 全部需要登录态的业务接口。上传接口带上文件，避免「先因缺文件 422」混淆判定。
BUSINESS_CALLS = [
    ("get", "/api/workspace", {}),
    ("post", "/api/upload", {"files": [("files", ("a.csv", CSV, "text/csv"))]}),
    ("post", "/api/ask", {"json": {"question": "q"}}),
    ("get", "/api/artifact/chart.png", {}),
    ("post", "/api/reset", {}),
    ("get", "/api/sessions", {}),
    ("post", "/api/sessions", {}),
    ("get", "/api/sessions/1/messages", {}),
    ("patch", "/api/sessions/1", {"json": {"title": "x"}}),
    ("delete", "/api/sessions/1", {}),
    ("get", "/api/datasets", {}),
    ("delete", "/api/datasets/1", {}),
]

ADMIN_CALLS = [
    ("get", "/api/admin/users", {}),
    ("patch", "/api/admin/users/1", {"json": {"role": "user"}}),
    ("post", "/api/admin/users/1/reset-password", {"json": {}}),
    ("get", "/api/admin/sessions", {}),
    ("get", "/api/admin/sessions/1/messages", {}),
    ("delete", "/api/admin/sessions/1", {}),
    ("get", "/api/admin/datasets", {}),
    ("delete", "/api/admin/datasets/1", {}),
    ("post", "/api/admin/system/reset-caches", {}),
    ("get", "/api/admin/system/health", {}),
    ("get", "/api/admin/stats", {}),
]


# ---------------------------------------------------------------- 权限门槛


class TestAuthGates:
    @pytest.mark.parametrize("method,path,kwargs", BUSINESS_CALLS)
    def test_business_endpoints_require_login(self, client, method, path, kwargs):
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, response.text

    @pytest.mark.parametrize("method,path,kwargs", BUSINESS_CALLS)
    def test_garbage_token_is_401(self, client, method, path, kwargs):
        response = getattr(client, method)(
            path, headers=auth_headers("not-a-jwt"), **kwargs
        )
        assert response.status_code == 401, response.text

    @pytest.mark.parametrize("method,path,kwargs", ADMIN_CALLS)
    def test_admin_endpoints_forbidden_for_normal_user(self, api, method, path, kwargs):
        """普通用户对管理接口的**所有**子路径都是 403。"""
        response = getattr(api, method)(path, **kwargs)
        assert response.status_code == 403, response.text

    @pytest.mark.parametrize("method,path,kwargs", ADMIN_CALLS)
    def test_admin_endpoints_require_login(self, client, method, path, kwargs):
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, response.text

    def test_token_signed_with_other_secret_is_401(self, client, alice, monkeypatch):
        """换密钥签的 token 必须被拒 —— 否则任何人都能自签一份管理员身份。"""
        monkeypatch.setenv("JWT_SECRET", "attacker-secret")
        factory = auth_db.get_session_factory()
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "alice"))
            forged = create_token(user)

        monkeypatch.setenv("JWT_SECRET", "unit-test-secret")
        response = client.get("/api/sessions", headers=auth_headers(forged))
        assert response.status_code == 401, response.text


# ---------------------------------------------------------------- 会话隔离


@pytest.fixture
def alice_session(api) -> int:
    response = api.post("/api/sessions", json={"title": "alice 的会话"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


class TestSessionIsolation:
    def test_bob_cannot_read_alice_session(self, bob_api, alice_session):
        response = bob_api.get(f"/api/sessions/{alice_session}/messages")
        assert response.status_code == 404, response.text

    def test_bob_cannot_rename_alice_session(self, bob_api, alice_session):
        response = bob_api.patch(f"/api/sessions/{alice_session}", json={"title": "偷改"})
        assert response.status_code == 404, response.text

    def test_bob_cannot_delete_alice_session(self, api, bob_api, alice_session):
        assert bob_api.delete(f"/api/sessions/{alice_session}").status_code == 404
        # alice 的会话还在
        assert api.get(f"/api/sessions/{alice_session}/messages").status_code == 200

    def test_bob_session_list_excludes_alice(self, bob_api, alice_session):
        ids = [s["id"] for s in bob_api.get("/api/sessions").json()["sessions"]]
        assert alice_session not in ids

    def test_bob_cannot_ask_into_alice_session(self, bob_api, alice_session):
        """带别人的 session_id 提问 → 404。

        这里**故意不上传任何数据**：如果接口先查「有没有数据文件」就会返 400，
        拿到 404 才证明归属校验发生在最前面。
        """
        response = bob_api.post(
            "/api/ask", json={"question": "多少行？", "session_id": alice_session}
        )
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------- 数据集隔离


class TestDatasetIsolation:
    def test_each_user_sees_only_own_datasets(self, api, bob_api):
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")

        alice_rows = api.get("/api/datasets").json()["datasets"]
        bob_rows = bob_api.get("/api/datasets").json()["datasets"]

        assert [r["filename"] for r in alice_rows] == ["alice.csv"]
        assert [r["filename"] for r in bob_rows] == ["bob.csv"]
        assert alice_rows[0]["user_id"] == api.user["id"]

    def test_bob_cannot_delete_alice_dataset(self, api, bob_api):
        upload_csv(api, "alice.csv")
        dataset_id = api.get("/api/datasets").json()["datasets"][0]["id"]

        response = bob_api.delete(f"/api/datasets/{dataset_id}")
        assert response.status_code == 404, response.text
        # 谁也没删掉
        remaining = api.get("/api/datasets").json()["datasets"]
        assert [r["filename"] for r in remaining] == ["alice.csv"]

    def test_workspace_view_is_per_user(self, api, bob_api):
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")

        alice_files = api.get("/api/workspace").json()["files"]
        bob_files = bob_api.get("/api/workspace").json()["files"]

        assert [f["name"] for f in alice_files] == ["alice.csv"]
        assert [f["name"] for f in bob_files] == ["bob.csv"]

    def test_deleting_own_dataset_clears_file_row_and_memory(self, api):
        """用户删自己的数据集：文件 → 内存工作区 → 表行，三步都要落实。"""
        upload_csv(api, "alice.csv")
        dataset_id = api.get("/api/datasets").json()["datasets"][0]["id"]
        workspace = workspaces.get_workspace(api.user["id"])
        assert [p.name for p in workspace.files] == ["alice.csv"]

        response = api.delete(f"/api/datasets/{dataset_id}")

        assert response.status_code == 200, response.text
        assert response.json()["file_removed"] is True
        # 内存工作区当场被剔除（不是等下次重启才生效）
        assert workspace.files == [] and workspace.schemas == []
        assert not (workspace.data_dir / "alice.csv").exists()
        assert api.get("/api/datasets").json()["datasets"] == []


# ---------------------------------------------------------------- 沙箱挂载隔离


class TestSandboxIsolation:
    def test_workspaces_are_distinct_objects_with_disjoint_files(self, api, bob_api):
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")

        alice_ws = workspaces.get_workspace(api.user["id"])
        bob_ws = workspaces.get_workspace(bob_api.user["id"])

        assert alice_ws is not bob_ws
        assert alice_ws.root != bob_ws.root
        assert not (set(alice_ws.files) & set(bob_ws.files))
        assert str(api.user["id"]) in str(alice_ws.root)
        assert str(bob_api.user["id"]) in str(bob_ws.root)

    def test_ask_mounts_only_own_files(self, api, bob_api, monkeypatch):
        """bob 提问时挂进沙箱的只有 bob.csv —— 隔离的最后一米就在这里。"""
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")

        executor = StubExecutor([ok_result("bob 的结果")])
        monkeypatch.setattr(
            server_module, "get_client", lambda: ScriptedClient(_tool_script())
        )
        monkeypatch.setattr(server_module, "get_executor", lambda: executor)

        response = bob_api.post("/api/ask", json={"question": "多少行？"})
        assert response.status_code == 200, response.text

        request = executor.requests[0]
        assert [p.name for p in request.data_files] == ["bob.csv"]
        # 工作目录与产物目录也必须在 bob 自己的根目录下
        assert str(workspaces.user_root(bob_api.user["id"])) in str(request.work_dir)
        assert str(workspaces.user_root(api.user["id"])) not in str(list(request.data_files))

    def test_alice_ask_mounts_only_alice_files(self, api, bob_api, monkeypatch):
        """对称验证一次 —— 只测一个方向容易把「恒定挂某个目录」当成通过。"""
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")

        executor = StubExecutor([ok_result("alice 的结果")])
        monkeypatch.setattr(
            server_module, "get_client", lambda: ScriptedClient(_tool_script())
        )
        monkeypatch.setattr(server_module, "get_executor", lambda: executor)

        assert api.post("/api/ask", json={"question": "多少行？"}).status_code == 200
        assert [p.name for p in executor.requests[0].data_files] == ["alice.csv"]

    def test_artifact_is_scoped_to_owner(self, api, bob_api):
        """产物按人取：alice 的图 bob 拿不到（同名文件也不行）。"""
        alice_ws = workspaces.get_workspace(api.user["id"])
        (alice_ws.artifacts_dir / "chart.png").write_bytes(b"\x89PNG-alice")

        assert api.get("/api/artifact/chart.png").status_code == 200
        assert bob_api.get("/api/artifact/chart.png").status_code == 404
