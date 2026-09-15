"""自助注册 → 管理员审核 → 可使用：这条链路的端到端断言。

产品决定（2026-09-15）：**注册开放，但注册不等于开号** —— 提交后落成 ``pending``，
不发 token、不能登录；管理员在后台点「通过」之后本人才能用注册时设的口令登录。
这样"开门"不会立刻消耗站点共用的 API Key 额度，闸门落在审核那一步。

三种"被挡住"的情形刻意分开说（用户才知道下一步做什么）：
  pending    → 注册申请正在等待管理员审核
  rejected   → 注册申请未通过审核
  active + is_active=False → 账号已被禁用
"""

from __future__ import annotations

from src.config import Settings
from src.auth.models import STATUS_ACTIVE, STATUS_PENDING, STATUS_REJECTED

NEWBIE = {"username": "newbie", "email": "newbie@example.com", "password": "secret123"}


def _register(client, **overrides) -> dict:
    payload = {**NEWBIE, **overrides}
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _login(client, account: str, password: str = "secret123"):
    return client.post("/api/auth/login", json={"account": account, "password": password})


# ---------------------------------------------------------------- 注册即待审核


class TestRegistrationCreatesPendingAccount:
    def test_register_returns_pending_and_no_token(self, client):
        body = _register(client)
        assert body["pending"] is True
        assert "token" not in body, "待审核的账号不该拿到 token"
        assert body["user"]["status"] == STATUS_PENDING
        assert body["user"]["is_active"] is False

    def test_pending_account_cannot_login(self, client):
        _register(client)
        response = _login(client, "newbie")
        assert response.status_code == 403
        assert "等待管理员审核" in response.json()["detail"]

    def test_pending_account_does_not_lock_itself_out_by_retrying(self, client):
        """待审核账号拿正确口令反复登录，不该把自己试进节流锁定（不计失败）。"""
        _register(client)
        for _ in range(6):
            assert _login(client, "newbie").status_code == 403
        # 审核通过后应当立刻能登录 —— 说明前 6 次确实没有累计失败
        assert _login(client, "newbie").status_code == 403  # 还是待审核

    def test_duplicate_registration_still_rejected(self, client):
        _register(client)
        again = client.post("/api/auth/register", json={**NEWBIE, "email": "other@example.com"})
        assert again.status_code == 400
        assert "已被注册" in again.json()["detail"]


# ---------------------------------------------------------------- 管理员审核


class TestAdminApproval:
    def test_approve_then_user_can_login(self, client, admin_api):
        body = _register(client)
        user_id = body["user"]["id"]

        patched = admin_api.patch(f"/api/admin/users/{user_id}", json={"status": STATUS_ACTIVE})
        assert patched.status_code == 200, patched.text
        row = patched.json()
        assert row["status"] == STATUS_ACTIVE
        assert row["is_active"] is True, "通过审核应当顺带启用账号"

        response = _login(client, "newbie")
        assert response.status_code == 200, response.text
        assert response.json()["user"]["status"] == STATUS_ACTIVE
        assert response.json()["token"]

    def test_reject_then_user_is_told_why(self, client, admin_api):
        body = _register(client)
        patched = admin_api.patch(
            f"/api/admin/users/{body['user']['id']}", json={"status": STATUS_REJECTED}
        )
        assert patched.status_code == 200
        assert patched.json()["is_active"] is False

        response = _login(client, "newbie")
        assert response.status_code == 403
        assert "未通过审核" in response.json()["detail"]

    def test_rejected_account_keeps_the_name_reserved(self, client, admin_api):
        """驳回后行保留（可审计），用户名/邮箱继续占位 —— 不能被同名重注册顶掉。"""
        body = _register(client)
        admin_api.patch(
            f"/api/admin/users/{body['user']['id']}", json={"status": STATUS_REJECTED}
        )
        again = client.post("/api/auth/register", json=NEWBIE)
        assert again.status_code == 400

    def test_pending_shows_up_in_admin_list_and_stats(self, client, admin_api):
        body = _register(client)
        users = admin_api.get("/api/admin/users", params={"q": "newbie"}).json()["users"]
        assert [u["status"] for u in users] == [STATUS_PENDING]

        stats = admin_api.get("/api/admin/stats").json()
        assert stats["pending_users"] >= 1

    def test_admin_cannot_reject_themselves(self, client, admin_api):
        """自锁保护要覆盖 status（status=rejected 会连带把自己停用）。"""
        admin_id = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {admin_api.token}"}
        ).json()["id"]
        response = admin_api.patch(f"/api/admin/users/{admin_id}", json={"status": STATUS_REJECTED})
        assert response.status_code == 400
        assert "不能降级、驳回或禁用自己的管理员账号" in response.json()["detail"]

    def test_normal_user_cannot_approve(self, client, api):
        """待审核的账号拿不到 admin 权限 —— 审核入口只对管理员开放。"""
        _register(client)
        response = api.get("/api/admin/users")
        assert response.status_code == 403


# ---------------------------------------------------------------- 开关


class TestRegistrationSwitch:
    def test_default_is_open(self):
        """默认开启自助注册：安全性由审核闸门保证，而不是靠关门。"""
        assert Settings.allow_registration is True

    def test_explicit_false_closes_the_door(self, client, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "allow_registration", False)
        response = client.post("/api/auth/register", json=NEWBIE)
        assert response.status_code == 403
        assert "已关闭自助注册" in response.json()["detail"]
