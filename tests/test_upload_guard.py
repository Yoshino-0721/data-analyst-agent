"""上传体积上限：**两道闸门**都必须生效（C2 复核清单 3.7）。

只测 ``Content-Length`` 那条快路径是不够的 —— 它由客户端提供，可以伪造；
在 ``Transfer-Encoding: chunked`` 下甚至根本不存在。所以测试分两层：

- **快路径**：用真实请求打端点（TestClient 一定会带 Content-Length）；
- **兜底路径**：直接驱动 ``read_within_limit``，喂一个"声称不大、实际很大"的上传
  对象 —— 这才是伪造头 / chunked 场景真正会走到的那道门。

另外上限是**整次请求**的预算：多个文件共享同一份，否则传 N 个"各自刚好不超"的
文件仍然能写满磁盘。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src import upload_guard
from src.config import settings

LIMIT = 1024  # 1 KiB：用小文件就能触界
TOO_BIG = b"x" * (LIMIT * 5)


class FakeRequest:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}


class FakeUpload:
    """只提供 ``.file.read(size)`` 的最小上传替身。

    ``read_within_limit`` 现在是**同步**实现，读的是 ``upload.file``
    （真实的 ``UploadFile.file`` 是底层 SpooledTemporaryFile），
    所以把 ``file`` 指回自身即可。
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.file = self

    def read(self, size: int = -1) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


@pytest.fixture()
def tiny_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size", LIMIT)
    return LIMIT


class TestLimitComesFromSettings:
    def test_read_per_call(self, monkeypatch):
        """每次现读 settings —— 否则 monkeypatch 不生效，测试就成了摆设。"""
        monkeypatch.setattr(settings, "max_upload_size", 12345)
        assert upload_guard.limit_bytes() == 12345


class TestContentLengthGate:
    def test_missing_header_passes_through(self, tiny_limit):
        """没有 Content-Length（chunked）不能在这里拒绝 —— 交给读取兜底。"""
        upload_guard.enforce_content_length(FakeRequest())

    def test_unparsable_header_passes_through(self, tiny_limit):
        upload_guard.enforce_content_length(FakeRequest({"content-length": "not-a-number"}))

    def test_exactly_at_limit_is_allowed(self, tiny_limit):
        upload_guard.enforce_content_length(FakeRequest({"content-length": str(LIMIT)}))

    def test_over_limit_is_rejected(self, tiny_limit):
        with pytest.raises(HTTPException) as exc:
            upload_guard.enforce_content_length(
                FakeRequest({"content-length": str(LIMIT + 1)})
            )
        assert exc.value.status_code == 413
        assert "上限" in exc.value.detail


class TestCumulativeReadGate:
    def test_within_budget_returns_full_content(self, tiny_limit):
        data = b"y" * (LIMIT - 1)
        assert upload_guard.read_within_limit(FakeUpload(data), LIMIT) == data

    def test_over_budget_is_rejected(self, tiny_limit):
        """**伪造 Content-Length 或 chunked 时真正生效的就是这道门。**"""
        with pytest.raises(HTTPException) as exc:
            upload_guard.read_within_limit(FakeUpload(b"x" * (LIMIT + 1)), LIMIT)
        assert exc.value.status_code == 413

    def test_zero_budget_is_rejected_immediately(self, tiny_limit):
        with pytest.raises(HTTPException) as exc:
            upload_guard.read_within_limit(FakeUpload(b"x"), 0)
        assert exc.value.status_code == 413

    def test_content_spanning_chunks_is_complete(self, monkeypatch):
        """分片读取：内容跨多个 CHUNK_SIZE 也要一字不差。"""
        budget = upload_guard.CHUNK_SIZE * 3
        monkeypatch.setattr(settings, "max_upload_size", budget)
        data = b"z" * (upload_guard.CHUNK_SIZE * 2 + 7)

        assert upload_guard.read_within_limit(FakeUpload(data), budget) == data

    def test_shared_budget_across_files(self, tiny_limit):
        """多文件共享预算：第二个文件会把总量顶过上限并触发 413。"""
        first = FakeUpload(b"a" * (LIMIT - 10))
        remaining = LIMIT
        upload_guard.read_within_limit(first, remaining)  # 读掉大半
        remaining -= LIMIT - 10

        with pytest.raises(HTTPException) as exc:
            upload_guard.read_within_limit(FakeUpload(b"b" * 50), remaining)
        assert exc.value.status_code == 413


class TestUploadEndpoint:
    """真实请求打端点：快路径必须真的拦下来。"""

    UPLOAD = "/api/upload"

    def test_over_limit_request_is_413(self, api, tiny_limit):
        response = api.post(
            self.UPLOAD, files={"files": ("big.csv", TOO_BIG, "text/csv")}
        )

        assert response.status_code == 413, response.text
        assert "上限" in response.json()["detail"]

    def test_rejected_upload_leaves_no_dataset(self, api, tiny_limit):
        api.post(self.UPLOAD, files={"files": ("big.csv", TOO_BIG, "text/csv")})

        assert api.get("/api/datasets").json()["datasets"] == []

    def test_within_limit_still_uploads(self, api, tiny_limit):
        """反向对照：别把正常上传一起拦掉。"""
        response = api.post(
            self.UPLOAD, files={"files": ("ok.csv", b"a,b\n1,2\n", "text/csv")}
        )

        assert response.status_code == 200, response.text
        assert [f["name"] for f in response.json()["files"]] == ["ok.csv"]
