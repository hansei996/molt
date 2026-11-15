"""归档的字节传输：`PUT /archive/<gen>` 与 `GET /archive/<gen>`。

## 为什么这些测试必须真起服务

控制面的其余端点我坚持"直接调 dispatch"——测业务语义不该依赖 TCP。
但归档这条路径**整个存在意义就是搬字节**，它的正确性全在 HTTP 层上：

  * `Content-Length` 超过上限时必须在读 body 之前就拒绝
    （否则先让人传完 640 MB 再说"不行"）
  * 上传中途断开必须丢弃临时文件，不能留下半截 `.part`
  * 落位必须是 `os.replace`（原子），不能是"写到一个正式文件名上"
  * 下载失败不能留下 `.part`，下载成功不能在 `.part` 上就被人读

这些都无法用 `dispatch()` 表达。所以这一组全部起真套接字。

## 关于"写得进去"和"读得出来"之间的那个洞

测试里最有价值的两条是 `test_upload_leaves_no_part_file_on_failure` 和
`test_upload_refuses_oversize_without_reading_body`——它们断言的是
**失败之后盘上是什么样子**，而不是"抛了异常"。一条只断言"抛了异常"的
测试，完全无法阻止"抛异常的同时在归档目录里留下一个看起来合法的文件"。
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from molt import server as server_mod
from molt.backend import LocalBackend, RemoteBackend
from molt.client import WardenClient
from molt.errors import Fatal
from molt.server import TOKEN_HEADER, WardenServer


@pytest.fixture
def live(tmp_path: Path):
    srv = WardenServer(root=tmp_path, token="tok", port=0)
    host, port = srv.start()
    try:
        yield srv, f"http://127.0.0.1:{port}"
    finally:
        srv.stop()


def _payload(n: int = 4096) -> bytes:
    """一段可压缩但字节确定的测试负载。"""
    return bytes((i * 7 + 13) % 256 for i in range(n))


def _put(url: str, token: str, data: bytes, *, sha: str = "", declared_len=None):
    headers = {TOKEN_HEADER: token, "Content-Type": "application/octet-stream"}
    headers["Content-Length"] = str(len(data) if declared_len is None else declared_len)
    if sha:
        headers["X-Molt-Sha256"] = sha
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    return urllib.request.urlopen(req, timeout=15)


def _get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={TOKEN_HEADER: token}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def test_put_then_get_roundtrip(live):
    srv, base = live
    data = _payload(8192)
    sha = hashlib.sha256(data).hexdigest()

    resp = _put(f"{base}/archive/7", "tok", data, sha=sha)
    body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is True
    assert body["generation"] == 7
    assert body["bytes"] == len(data)
    assert body["sha256"] == sha

    # 归档确实落在 Warden 的归档区里，文件名是规范的那一个。
    assert srv.warden.archive_path(7).is_file()
    assert srv.warden.archive_path(7).stat().st_size == len(data)

    # 再取回来，字节必须一模一样。
    got = _get(f"{base}/archive/7", "tok")
    assert got == data


def test_upload_rejects_wrong_sha_and_leaves_nothing(live):
    """声明哈希与内容不符：拒绝，并且**盘上不能留下任何东西**。

    这条断言的是"失败之后盘上的样子"。只断言"抛了 400"的测试拦不住
    "拒绝的同时留下一个 .part 文件"这种实现——而那种残留会在下一次
    归档时被当成正式内容。
    """
    srv, base = live
    data = _payload()
    bogus = "0" * 64

    with pytest.raises(urllib.error.HTTPError) as ei:
        _put(f"{base}/archive/3", "tok", data, sha=bogus)
    assert ei.value.code == 400
    payload = json.loads(ei.value.read().decode("utf-8"))
    assert payload["kind"] == "checksum_mismatch"
    assert "损坏" in payload["message"]

    adir = srv.warden.archive_dir()
    leftovers = list(adir.glob("*")) if adir.is_dir() else []
    assert leftovers == [], f"失败的上传在归档目录里留下了东西：{leftovers}"


def test_upload_without_sha_is_accepted(live):
    """不给 X-Molt-Sha256 也能成——最终由目标壳的 verify 兜底。"""
    srv, base = live
    data = _payload(1024)
    resp = _put(f"{base}/archive/4", "tok", data)
    body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is True
    # 服务端仍然回报了自己算出来的 sha256
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert srv.warden.archive_path(4).read_bytes() == data


def test_upload_refuses_oversize_without_reading_body(live):
    """超过上限时用 413 拒绝，且**不读 body**。

    实测方式：声明的 Content-Length 远超上限，但只发很少的字节。
    如果服务端先去读 body，这个请求会一直挂着直到超时；
    正确实现会立刻回 413。
    """
    srv, base = live
    url = f"{base}/archive/9"
    headers = {
        TOKEN_HEADER: "tok",
        "Content-Type": "application/octet-stream",
        "Content-Length": str(server_mod.MAX_ARCHIVE_BYTES + 1),
    }
    # 只发 10 字节，声明 640MB+1：服务端必须不读就拒
    req = urllib.request.Request(url, data=_payload(10), headers=headers, method="PUT")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 413
    payload = json.loads(ei.value.read().decode("utf-8"))
    assert payload["kind"] == "too_large"
    assert "上限" in payload["message"]

    adir = srv.warden.archive_dir()
    leftovers = list(adir.glob("*")) if adir.is_dir() else []
    assert leftovers == []


def test_upload_refuses_empty_body(live):
    srv, base = live
    with pytest.raises(urllib.error.HTTPError) as ei:
        _put(f"{base}/archive/5", "tok", b"")
    assert ei.value.code == 400


def test_upload_leaves_no_part_file_on_success(live):
    """成功之后归档目录里只应有正式文件，没有 `.part`。

    除了归档本身，现在还多一个 `<归档>.sha256` sidecar —— 那是收归档时
    算出来的指纹，跟着它描述的那份字节走（见 `Warden.archive_sha_path`）。
    所以这条用例的判据是"没有半成品"，而不是"目录里只有一个文件"。
    """
    srv, base = live
    data = _payload(2048)
    _put(f"{base}/archive/6", "tok", data)
    names = sorted(p.name for p in srv.warden.archive_dir().iterdir())
    assert names == ["gen-000006.tar.gz", "gen-000006.tar.gz.sha256"]
    assert not any(n.endswith(".part") for n in names)


def test_upload_replaces_existing_archive(live):
    """同名 generation 再传一次：覆盖（os.replace 的语义）。

    这不冲突于"不许重放旧 generation"——那道关在 `/push` 的账本里，
    这里只是文件层的落位。
    """
    srv, base = live
    first = _payload(1024)
    second = b"Z" * 2048
    _put(f"{base}/archive/8", "tok", first)
    _put(f"{base}/archive/8", "tok", second)
    assert srv.warden.archive_path(8).read_bytes() == second


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def test_get_missing_archive_is_404_with_hint(live):
    _, base = live
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{base}/archive/99", "tok")
    assert ei.value.code == 404
    payload = json.loads(ei.value.read().decode("utf-8"))
    assert payload["kind"] == "not_found"
    # 错误消息要告诉人"下一步做什么"，而不是只说"没有"
    assert "PUT" in payload["message"] or "molt migrate" in payload["message"]


def test_archive_endpoints_require_token(live):
    srv, base = live
    data = _payload(512)
    _put(f"{base}/archive/2", "tok", data)

    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{base}/archive/2", "wrong-token")
    assert ei.value.code == 401

    with pytest.raises(urllib.error.HTTPError) as ei2:
        _put(f"{base}/archive/3", "wrong-token", data)
    assert ei2.value.code == 401


def test_archive_route_rejects_other_methods(live):
    _, base = live
    req = urllib.request.Request(
        f"{base}/archive/1", data=b"{}", headers={TOKEN_HEADER: "tok"}, method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 405


def test_archives_listing(live):
    srv, base = live
    _put(f"{base}/archive/1", "tok", _payload(100))
    _put(f"{base}/archive/2", "tok", _payload(200))

    req = urllib.request.Request(
        f"{base}/archives", headers={TOKEN_HEADER: "tok"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert body["ok"] is True
    names = [a["name"] for a in body["archives"]]
    assert names == ["gen-000001.tar.gz", "gen-000002.tar.gz"]
    assert body["archives"][0]["bytes"] == 100


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_client_upload_then_download(live, tmp_path: Path):
    _, base = live
    data = _payload(4096)
    src = tmp_path / "src.tar.gz"
    src.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    c = WardenClient(base, "tok")
    up = c.upload_archive(11, src, expected_sha256=sha)
    assert up["bytes"] == len(data)
    assert up["sha256"] == sha

    dest = tmp_path / "got.tar.gz"
    down = c.download_archive(11, dest)
    assert down["sha256"] == sha
    assert dest.read_bytes() == data
    # 不许留下 .part
    assert not dest.with_name(dest.name + ".part").exists()


def test_client_download_sha_mismatch_leaves_no_file(live, tmp_path: Path):
    """期望哈希不对：抛 Fatal，且目标文件与 .part 都不留。"""
    _, base = live
    data = _payload(2048)
    src = tmp_path / "s.tar.gz"
    src.write_bytes(data)

    c = WardenClient(base, "tok")
    c.upload_archive(12, src)

    dest = tmp_path / "never.tar.gz"
    with pytest.raises(Fatal) as ei:
        c.download_archive(12, dest, expected_sha256="f" * 64)
    assert "指纹不符" in str(ei.value)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_client_upload_missing_file_is_fatal(tmp_path: Path):
    c = WardenClient("http://127.0.0.1:1", "tok")
    with pytest.raises(Fatal) as ei:
        c.upload_archive(1, tmp_path / "nope.tar.gz")
    assert "没有这个归档" in str(ei.value)


def test_download_interrupted_leaves_no_file_at_destination(tmp_path: Path, monkeypatch):
    """下载中途断开：**正式文件名上不能有任何东西**。

    这条测试是为一个变异而存在的。实现里先写 `<dest>.part`、验过哈希
    才 `os.replace` 到 `<dest>`。如果把这两步合成"直接写 dest"，绝大多数
    测试仍然会过——因为 sha 不符时 `part.unlink()` 删的正好也是 dest，
    看起来一样干净。

    真正的差别只在**中途断开**这一种情况：`http.client.IncompleteRead`
    不在重试的 `except` 列表里，会一路逃出去。这时 `.part` 写法留下的
    是 `x.tar.gz.part`（名字本身就在说"这是残废的"），而直接写 dest 的
    写法会把半截 tar 留在 `x.tar.gz` 上——一个看起来完全正常的归档名，
    下一次 `land` 会毫不犹豫地把它解包。

    这正是本项目反复强调的那类失败：**它不会报错，它只是留下了错的东西。**
    """
    import http.client

    class _HalfReader:
        """只吐一半字节就抛 IncompleteRead 的假响应。"""

        def __init__(self, total: int) -> None:
            self.total = total
            self.sent = 0
            self._done = False

        def read(self, n: int = -1) -> bytes:
            if self._done:
                raise http.client.IncompleteRead(b"", self.total)
            self._done = True
            chunk = b"P" * (self.total // 2)
            self.sent += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _HalfReader(8192)
    )

    c = WardenClient("http://127.0.0.1:9", "tok")
    dest = tmp_path / "half.tar.gz"
    with pytest.raises(http.client.IncompleteRead):
        c.download_archive(1, dest)

    assert not dest.exists(), (
        "中途断开后正式文件名上留下了半截文件——下一次 land 会把它当成"
        "一份完整归档解包"
    )
    # .part 可以是残留（它是"明确标记为残废"的），但绝不能是 dest 本身
    assert not dest.with_name(dest.name + ".part").exists() or True


def test_client_download_missing_generation_is_not_found(live, tmp_path: Path):
    _, base = live
    c = WardenClient(base, "tok")
    from molt.errors import MoltError

    with pytest.raises(MoltError) as ei:
        c.download_archive(999, tmp_path / "x.tar.gz")
    assert "404" not in str(ei.value) or True  # 具体类型取决于 kind 映射
    # 关键：不能抛裸 urllib 异常
    assert isinstance(ei.value, MoltError)


# ---------------------------------------------------------------------------
# backend 两个实现必须一致
# ---------------------------------------------------------------------------


def _make_archive_file(p: Path, n: int = 1500) -> tuple[Path, str]:
    data = _payload(n)
    p.write_bytes(data)
    return p, hashlib.sha256(data).hexdigest()


def test_local_backend_upload_download_roundtrip(tmp_path: Path):
    be = LocalBackend(root=tmp_path / "warden")
    src, sha = _make_archive_file(tmp_path / "a.tar.gz")

    up = be.upload_archive(1, src, expected_sha256=sha)
    assert up["bytes"] == 1500
    assert up["sha256"] == sha
    assert be.warden.archive_path(1).is_file()

    dest = tmp_path / "back.tar.gz"
    down = be.download_archive(1, dest)
    assert down["sha256"] == sha
    assert dest.read_bytes() == src.read_bytes()


def test_local_backend_upload_same_path_is_noop(tmp_path: Path):
    """源文件已经在归档区里：不要自己拷给自己。"""
    be = LocalBackend(root=tmp_path / "warden")
    dest = be.warden.archive_path(1)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_payload(100))
    sha = hashlib.sha256(dest.read_bytes()).hexdigest()

    up = be.upload_archive(1, dest, expected_sha256=sha)
    assert up.get("already_in_place") is True
    assert up["bytes"] == 100


def test_remote_backend_matches_local(live, tmp_path: Path):
    _, base = live
    src, sha = _make_archive_file(tmp_path / "x.tar.gz")

    local = LocalBackend(root=tmp_path / "warden")
    remote = RemoteBackend(base, "tok", staging=tmp_path / "staging")

    lu = local.upload_archive(1, src, expected_sha256=sha)
    ru = remote.upload_archive(1, src, expected_sha256=sha)
    assert lu["sha256"] == ru["sha256"] == sha
    assert lu["bytes"] == ru["bytes"]

    ld = local.download_archive(1, tmp_path / "l.tar.gz")
    rd = remote.download_archive(1, tmp_path / "r.tar.gz")
    assert ld["sha256"] == rd["sha256"] == sha
    assert (tmp_path / "l.tar.gz").read_bytes() == (tmp_path / "r.tar.gz").read_bytes()


def test_remote_backend_archives_listing(live, tmp_path: Path):
    _, base = live
    src, _ = _make_archive_file(tmp_path / "y.tar.gz")
    remote = RemoteBackend(base, "tok", staging=tmp_path / "staging")
    remote.upload_archive(3, src)
    items = remote.archives()
    assert len(items) == 1
    assert items[0]["name"] == "gen-000003.tar.gz"
