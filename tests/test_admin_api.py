"""管理员后台 API 测试：用户 / 会话 / 数据集 / 系统 四块。

这里守住的重点不是「接口能通」，而是三条容易做错的：

1. **管理员不能把自己锁死**（降级或禁用自己 → 400）；
2. **管理员删数据集必须连带摘掉该用户的内存工作区** —— 不摘的话对方下次
   提问仍会把这个文件挂进沙箱，等于没删；
3. **重置密码后旧密码立刻失效**（哈希真的写回了库，不是只返了个字符串）。
"""

from __future__ import annotations

from src import server as server_module
from src import workspaces
from tests.conftest import upload_csv
from tests.conftest_agent import (
    ScriptedClient,
    StubExecutor,
    assistant_text,
    assistant_with_tools,
    ok_result,
    tool_call,
)


def stub_agent(monkeypatch, results, *, answer: str = "答案：42") -> None:
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


def ask_once(api, monkeypatch, session_id=None, question="华东的销售额是多少？") -> dict:
    stub_agent(monkeypatch, [ok_result("42")])
    payload = {"question": question}
    if session_id is not None:
        payload["session_id"] = session_id
    response = api.post("/api/ask", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- 统计


class TestStats:
    def test_counts_reflect_database(self, api, admin_api, monkeypatch):
        upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
        ask_once(api, monkeypatch)

        stats = admin_api.get("/api/admin/stats").json()

        assert stats["users"] == 2, "默认管理员 + alice"
        assert stats["active_users"] == 2
        assert stats["sessions"] == 1
        assert stats["messages"] == 2, "一问一答两条"
        assert stats["datasets"] == 1
        assert stats["sessions_last_24h"] == 1

    def test_empty_database_counts_are_zero(self, admin_api):
        stats = admin_api.get("/api/admin/stats").json()
        assert stats["users"] == 1, "只有引导出来的默认管理员"
        assert stats["sessions"] == 0
        assert stats["messages"] == 0
        assert stats["datasets"] == 0
        assert stats["sessions_last_24h"] == 0


# ---------------------------------------------------------------- 用户管理


class TestUserManagement:
    def test_list_users(self, api, admin_api):
        users = admin_api.get("/api/admin/users").json()["users"]
        assert [u["username"] for u in users] == ["admin", "alice"]
        assert "password_hash" not in users[0], "绝不能把口令哈希发出去"

    def test_search_by_username_and_email(self, api, admin_api):
        assert [u["username"] for u in admin_api.get("/api/admin/users?q=ali").json()["users"]] == [
            "alice"
        ]
        assert [
            u["username"]
            for u in admin_api.get("/api/admin/users?q=alice@example.com").json()["users"]
        ] == ["alice"]
        assert admin_api.get("/api/admin/users?q=查无此人").json()["users"] == []

    def test_patch_role_and_active(self, api, admin_api):
        alice_id = api.user["id"]

        promoted = admin_api.patch(f"/api/admin/users/{alice_id}", json={"role": "admin"})
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["role"] == "admin"

        disabled = admin_api.patch(f"/api/admin/users/{alice_id}", json={"is_active": False})
        assert disabled.json()["is_active"] is False

    def test_promoted_user_can_reach_admin_api(self, api, admin_api):
        """角色改的是库里的行 —— 已签发的 token 立刻按新角色判权。"""
        admin_api.patch(f"/api/admin/users/{api.user['id']}", json={"role": "admin"})
        assert api.get("/api/admin/stats").status_code == 200

    def test_disabled_user_is_locked_out(self, api, admin_api):
        admin_api.patch(f"/api/admin/users/{api.user['id']}", json={"is_active": False})
        response = api.get("/api/sessions")
        assert response.status_code == 403, response.text
        assert "禁用" in response.json()["detail"]

    def test_admin_cannot_demote_itself(self, admin_api):
        response = admin_api.patch(
            f"/api/admin/users/{admin_api.user['id']}", json={"role": "user"}
        )
        assert response.status_code == 400, response.text
        assert "自己" in response.json()["detail"]

    def test_admin_cannot_disable_itself(self, admin_api):
        response = admin_api.patch(
            f"/api/admin/users/{admin_api.user['id']}", json={"is_active": False}
        )
        assert response.status_code == 400, response.text

    def test_admin_can_still_update_itself_harmlessly(self, admin_api):
        """自锁死保护只针对「降级 / 禁用」，改回 admin 不算。"""
        response = admin_api.patch(
            f"/api/admin/users/{admin_api.user['id']}", json={"role": "admin"}
        )
        assert response.status_code == 200, response.text

    def test_patch_unknown_user_is_404(self, admin_api):
        assert admin_api.patch("/api/admin/users/9999", json={"role": "user"}).status_code == 404

    def test_invalid_role_is_422(self, api, admin_api):
        response = admin_api.patch(f"/api/admin/users/{api.user['id']}", json={"role": "root"})
        assert response.status_code == 422, response.text


# ---------------------------------------------------------------- 重置密码


class TestResetPassword:
    def test_explicit_new_password_takes_effect(self, client, api, admin_api):
        response = admin_api.post(
            f"/api/admin/users/{api.user['id']}/reset-password",
            json={"password": "brand-new-6"},
        )
        assert response.status_code == 200, response.text

        new_login = client.post(
            "/api/auth/login", json={"account": "alice", "password": "brand-new-6"}
        )
        old_login = client.post(
            "/api/auth/login", json={"account": "alice", "password": "secret123"}
        )
        assert new_login.status_code == 200, new_login.text
        assert old_login.status_code == 401, old_login.text

    def test_generated_password_is_returned_once_and_works(self, client, api, admin_api):
        body = admin_api.post(
            f"/api/admin/users/{api.user['id']}/reset-password", json={}
        ).json()
        assert body["ok"] is True
        assert body["username"] == "alice"
        assert len(body["password"]) == 10, body

        login = client.post(
            "/api/auth/login", json={"account": "alice", "password": body["password"]}
        )
        assert login.status_code == 200, login.text

    def test_unknown_user_is_404(self, admin_api):
        assert (
            admin_api.post("/api/admin/users/9999/reset-password", json={}).status_code
            == 404
        )


# ---------------------------------------------------------------- 会话管理


class TestSessionAdmin:
    def test_admin_reads_any_session_with_username_and_trace(self, api, admin_api, monkeypatch):
        upload_csv(api, "销售.csv", "地区,销售额\n华东,100\n")
        asked = ask_once(api, monkeypatch)
        sid = asked["session_id"]

        body = admin_api.get(f"/api/admin/sessions/{sid}/messages").json()

        assert body["username"] == "alice"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        # 管理员看的就是 meta 里那份执行轨迹
        meta = body["messages"][1]["meta"]
        assert meta["steps"] == asked["steps"]
        assert meta["steps"][0]["calls"][0]["code"] == "print(42)"

    def test_session_list_has_username_and_user_filter(self, api, bob_api, admin_api):
        alice_sid = api.post("/api/sessions", json={"title": "alice 的"}).json()["id"]
        bob_sid = bob_api.post("/api/sessions", json={"title": "bob 的"}).json()["id"]

        sessions = admin_api.get("/api/admin/sessions").json()["sessions"]
        by_id = {s["id"]: s for s in sessions}
        assert by_id[alice_sid]["username"] == "alice"
        assert by_id[bob_sid]["username"] == "bob"

        only_alice = admin_api.get(
            f"/api/admin/sessions?user_id={api.user['id']}"
        ).json()["sessions"]
        assert [s["id"] for s in only_alice] == [alice_sid]

    def test_admin_deletes_any_session(self, api, admin_api, monkeypatch):
        upload_csv(api, "销售.csv")
        sid = ask_once(api, monkeypatch)["session_id"]

        assert admin_api.delete(f"/api/admin/sessions/{sid}").json()["ok"] is True
        assert admin_api.get(f"/api/admin/sessions/{sid}/messages").status_code == 404
        assert api.get(f"/api/sessions/{sid}/messages").status_code == 404

    def test_delete_unknown_session_is_404(self, admin_api):
        assert admin_api.delete("/api/admin/sessions/9999").status_code == 404


# ---------------------------------------------------------------- 数据集管理


class TestDatasetAdmin:
    def test_lists_all_datasets_with_username(self, api, bob_api, admin_api):
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")

        rows = admin_api.get("/api/admin/datasets").json()["datasets"]
        assert {r["username"] for r in rows} == {"alice", "bob"}

        only_alice = admin_api.get(f"/api/admin/datasets?user_id={api.user['id']}").json()[
            "datasets"
        ]
        assert [r["filename"] for r in only_alice] == ["alice.csv"]

    def test_delete_drops_the_users_in_memory_workspace(self, api, admin_api):
        """删数据集的最后一米：把该用户的内存工作区也摘掉。"""
        upload_csv(api, "alice.csv", "地区,销售额\n华东,100\n")
        alice_id = api.user["id"]
        dataset_id = api.get("/api/datasets").json()["datasets"][0]["id"]

        # 上传之后内存工作区里确实有这个文件
        assert [p.name for p in workspaces.get_workspace(alice_id).files] == ["alice.csv"]
        assert alice_id in workspaces._registry

        response = admin_api.delete(f"/api/admin/datasets/{dataset_id}")
        assert response.status_code == 200, response.text
        assert response.json()["file_removed"] is True

        assert alice_id not in workspaces._registry, "内存工作区必须被摘掉"
        # 表行没了，用户自己再查也看不到
        assert api.get("/api/datasets").json()["datasets"] == []
        # 重新取到的是从磁盘恢复出来的空工作区（文件真的删了）
        assert workspaces.get_workspace(alice_id).schemas == []

    def test_delete_is_scoped_to_the_row(self, api, bob_api, admin_api):
        upload_csv(api, "alice.csv")
        upload_csv(bob_api, "bob.csv")
        alice_dataset = api.get("/api/datasets").json()["datasets"][0]["id"]

        admin_api.delete(f"/api/admin/datasets/{alice_dataset}")

        assert [r["filename"] for r in bob_api.get("/api/datasets").json()["datasets"]] == [
            "bob.csv"
        ]

    def test_delete_survives_failing_unlink(self, api, admin_api, monkeypatch):
        """删除钩子拦住 unlink 时，删除数据集仍要成功（表行与内存态清掉）。

        AGENTS.md §2.9：清理动作失败只警告，绝不阻断主流程。本机就有这么个
        钩子，会在批量删除时直接抛 SystemExit。
        """
        from src import datasets as datasets_module

        upload_csv(api, "alice.csv", "地区,销售额\n华东,100\n")
        alice_id = api.user["id"]
        dataset_id = api.get("/api/datasets").json()["datasets"][0]["id"]

        def boom(self):
            raise SystemExit("删除钩子")

        monkeypatch.setattr(datasets_module.Path, "unlink", boom, raising=False)

        response = admin_api.delete(f"/api/admin/datasets/{dataset_id}")

        assert response.status_code == 200, response.text
        assert response.json()["file_removed"] is False, "删不干净要如实上报"
        assert api.get("/api/datasets").json()["datasets"] == []
        assert alice_id not in workspaces._registry

    def test_delete_unknown_dataset_is_404(self, admin_api):
        assert admin_api.delete("/api/admin/datasets/9999").status_code == 404


# ---------------------------------------------------------------- 系统操作


class TestSystemEndpoints:
    def test_health_shape(self, admin_api, tmp_path):
        body = admin_api.get("/api/admin/system/health").json()

        assert body["status"] == "ok"
        assert set(body) >= {
            "status",
            "has_api_key",
            "model",
            "executor",
            "executor_ok",
            "storage_root",
        }
        assert body["storage_root"] == str(tmp_path / "storage")

    def test_health_reports_executor_failure(self, admin_api, monkeypatch):
        class _BrokenExecutor:
            def available(self):
                raise RuntimeError("docker 不可达")

        monkeypatch.setattr(server_module, "get_executor", lambda: _BrokenExecutor())

        body = admin_api.get("/api/admin/system/health").json()
        assert body["executor_ok"] is False
        assert "docker" in body["executor_note"]

    def test_reset_caches_is_wired_to_server_hook(self, admin_api, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(server_module, "reset_caches", lambda: calls.append(1))

        response = admin_api.post("/api/admin/system/reset-caches")

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert calls == [1], "必须真的调到 server.reset_caches"

    def test_reset_caches_clears_real_caches(self, admin_api):
        """不打桩跑一次真货：清完缓存后重新取到的执行器是新对象。"""
        first = server_module.get_executor()
        admin_api.post("/api/admin/system/reset-caches")
        second = server_module.get_executor()
        assert first is not second
        assert first.describe() == second.describe()


def test_reset_password_is_one_time(client, admin_h, alice):
    """T7：管理员重置出来的口令是**一次性**的 —— 首登必须改密，改完才放行。

    安全边界在闸门上，不在初始口令的强度上：所以这里不测"口令够不够强"，
    而是测"拿到重置口令的人能不能绕开改密直接用系统"（答案是不能）。
    """
    reset = client.post(
        f"/api/admin/users/{alice['user']['id']}/reset-password", json={}, headers=admin_h
    )
    assert reset.status_code == 200, reset.text
    one_time = reset.json()["password"]

    # ① 用一次性口令登录：能登录（登录本身不受闸门限制），但带着 must_change_password
    login = client.post("/api/auth/login", json={"account": "alice", "password": one_time})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["must_change_password"] is True
    gated_token = login.json()["token"]

    # ② 改密前访问业务接口 -> 403（闸门生效）
    headers = {"Authorization": f"Bearer {gated_token}"}
    assert client.get("/api/sessions", headers=headers).status_code == 403

    # ③ 改密后 -> 放行
    changed = client.post(
        "/api/auth/change-password",
        json={"old_password": one_time, "new_password": "Fresh-After-Reset-7z!Q"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    fresh = {"Authorization": f"Bearer {changed.json()['token']}"}
    assert client.get("/api/sessions", headers=fresh).status_code == 200


def test_reset_password_with_explicit_password_is_also_one_time(client, admin_h, alice):
    """显式指定口令的那条路同样是一次性 —— 否则"管理员设的口令长期有效"这个口子还在。"""
    reset = client.post(
        f"/api/admin/users/{alice['user']['id']}/reset-password",
        json={"password": "brandnew123"},
        headers=admin_h,
    )
    assert reset.status_code == 200, reset.text

    login = client.post("/api/auth/login", json={"account": "alice", "password": "brandnew123"})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["must_change_password"] is True
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/sessions", headers=headers).status_code == 403
