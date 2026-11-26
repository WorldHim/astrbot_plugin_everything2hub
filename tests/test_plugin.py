"""插件层测试。

真仓库用本机 git 造一个 bare 上游（不联网），插件会真的 clone / pull，
发送环节的下载与 CDN 回退则打桩，避免依赖网络。
"""

import asyncio
import random
import subprocess

from astrbot.api.message_components import Poke
from astrbot.api.star import StarTools  # noqa: F401 - 供 monkeypatch 使用

from tests.conftest import commit_all, make_upstream

BASE_CONFIG = {
    "repo": "someone/gallery",
    "branch": "main",
    "cdn": "cdn.jsdelivr.net",
    "include_dirs": [],
    "exclude_dirs": ["del"],
    "keyword_include": [],
    "keyword_exclude": [],
    "send_mode": "auto",
    "caption_template": "",
    "cooldown_seconds": 0,
    "recent_limit": 2,
    "clone_depth": 0,
    "pull_interval_seconds": 300,
    "timeout_seconds": 5,
    "max_download_mb": 8,
}


class FakeEvent:
    def __init__(self, text="cave", admin=False, umo="aiocqhttp:FriendMessage:1"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._admin = admin

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def is_admin(self):
        return self._admin

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "10000"

    def get_messages(self):
        return getattr(self, "messages", [])


def run_async(coro):
    return asyncio.run(coro)


def make_plugin(plugin_module, monkeypatch, tmp_path, *, upstream=None, **overrides):
    config = dict(BASE_CONFIG)
    config.update(overrides)
    if upstream is not None:
        config["repo_url"] = str(upstream)
    monkeypatch.setattr(
        plugin_module.StarTools,
        "get_data_dir",
        classmethod(lambda cls, name=None: tmp_path),
    )
    return plugin_module.Everything2HubPlugin(context=None, config=config)


def run(plugin, event, handler="cave"):
    async def go():
        return [item async for item in getattr(plugin, handler)(event)]

    return asyncio.run(go())


def local_files(plugin):
    return [f.path for f in plugin._files]


def candidates(plugin, keyword=None):
    return [f.path for f in run_async(plugin._candidates(keyword))]


def test_help_command(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path)
    out = run(plugin, FakeEvent("cave 帮助"))
    assert out[0][0] == "plain"
    assert "回声洞用法" in out[0][1]


def test_clone_then_send_local_image(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, upstream=upstream)

    out = run(plugin, FakeEvent("cave"))
    assert out[0][0] == "chain"
    chain = out[0][1]
    assert len(chain) == 1
    assert type(chain[0]).__name__ == "Image"
    assert chain[0].file.startswith("file://")
    assert local_files(plugin) == [
        "/ad-closenn/甲辰年.png",
        "/del/old.png",
        "/http/403.gif",
        "/httpcat/205.jpg",
    ]
    assert candidates(plugin) == [
        "/ad-closenn/甲辰年.png",
        "/http/403.gif",
        "/httpcat/205.jpg",
    ]
    assert plugin._head_sha


def test_cave_without_repo_asks_config(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, repo="")
    out = run(plugin, FakeEvent("cave"))
    assert len(out) == 1
    assert "尚未配置图库仓库" in out[0][1]


def test_poke_disabled_sends_nothing(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, upstream=upstream)
    run_async(plugin._sync())
    out = run(plugin, FakeEvent("poke"), handler="on_poke")
    assert out == []


def test_poke_enabled_sends_image(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        upstream=upstream,
        poke_enabled=True,
    )
    run_async(plugin._sync())
    event = FakeEvent("poke")
    event.messages = [Poke(id="10000", qq="10000")]
    out = run(plugin, event, handler="on_poke")
    assert len(out) == 1
    assert type(out[0][1][0]).__name__ == "Image"


def test_poke_not_targeting_bot_ignored(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        upstream=upstream,
        poke_enabled=True,
    )
    run_async(plugin._sync())
    event = FakeEvent("poke")
    event.messages = [Poke(id="999", qq="999")]
    out = run(plugin, event, handler="on_poke")
    assert out == []


def test_poke_filter_matches_only_bot_target(plugin_module):
    flt = plugin_module.PokeBotFilter()

    class Ev:
        platform = "aiocqhttp"
        self_id = "10000"

        def get_platform_name(self):
            return self.platform

        def get_self_id(self):
            return self.self_id

        def get_messages(self):
            return self.messages

    ev = Ev()
    ev.messages = [Poke(id="10000", qq="10000")]
    assert flt.filter(ev, None) is True
    ev.messages = [Poke(id="999", qq="999")]
    assert flt.filter(ev, None) is False
    ev.messages = []
    assert flt.filter(ev, None) is False
    ev.platform = "qq_official"
    ev.messages = [Poke(id="10000", qq="10000")]
    assert flt.filter(ev, None) is False


def test_no_git_available_reports_friendly_error(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, git_path="/nope/git"
    )
    out = run(plugin, FakeEvent("cave"))
    assert out[0][0] == "plain"
    assert "图库暂时不可用" in out[0][1]


def test_local_file_missing_falls_back_to_download(
    plugin_module, monkeypatch, tmp_path
):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, upstream=upstream)
    run_async(plugin._sync())

    for f in plugin._files:
        target = plugin_module.local_image_path(plugin.repo_dir, f.path)
        if target.is_file():
            target.unlink()

    downloaded = []

    async def fake_download(url):
        downloaded.append(url)
        path = tmp_path / "dl.png"
        path.write_bytes(b"png")
        return path

    plugin._download = fake_download
    out = run(plugin, FakeEvent("cave"))
    assert downloaded and downloaded[0].startswith("https://cdn.jsdelivr.net/gh/")
    assert plugin._head_sha in downloaded[0]
    assert out[0][1][0].file.startswith("file://")


def test_download_failure_falls_back_to_url(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, upstream=upstream)
    run_async(plugin._sync())
    for f in plugin._files:
        target = plugin_module.local_image_path(plugin.repo_dir, f.path)
        if target.is_file():
            target.unlink()

    async def fake_download(url):
        return None

    plugin._download = fake_download
    out = run(plugin, FakeEvent("cave"))
    chain = out[0][1]
    assert type(chain[0]).__name__ == "Image"
    assert chain[0].file.startswith("https://cdn.jsdelivr.net/gh/")


def test_url_send_mode_skips_download(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    called = []

    async def fake_download(url):
        called.append(url)

    plugin._download = fake_download
    out = run(plugin, FakeEvent("cave"))
    assert called == []
    assert out[0][1][0].file.startswith("https://cdn.jsdelivr.net/gh/")


def test_caption_is_prepended(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        upstream=upstream,
        caption_template="{name} @ {repo} {commit}",
        send_mode="url",
    )
    out = run(plugin, FakeEvent("cave"))
    chain = out[0][1]
    assert type(chain[0]).__name__ == "Plain"
    assert "someone/gallery" in chain[0].text
    assert plugin._head_sha[:12] in chain[0].text


def test_keyword_filters_candidates(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    out = run(plugin, FakeEvent("cave httpcat"))
    assert out[0][0] == "chain"
    assert "httpcat" in out[0][1][0].file


def test_keyword_without_match(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    out = run(plugin, FakeEvent("cave 不存在的关键词"))
    assert out[0][0] == "plain"
    assert "没有找到" in out[0][1]


def test_cooldown_blocks_second_call(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        upstream=upstream,
        cooldown_seconds=60,
        send_mode="url",
    )
    first = run(plugin, FakeEvent("cave"))
    assert first[0][0] == "chain"
    second = run(plugin, FakeEvent("cave"))
    assert second[0][0] == "plain"
    assert "冷却中" in second[0][1]


def test_refresh_requires_admin(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    out = run(plugin, FakeEvent("cave 刷新"))
    assert out[0][0] == "plain"
    assert "管理员" in out[0][1]


def test_refresh_pulls_new_commits(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    run(plugin, FakeEvent("cave"))
    before = plugin._head_sha
    commit_all(
        tmp_path / "up" / "work",
        {"http/403.gif": b"v2", "new/extra.png": b"extra"},
        "upstream change",
    )
    subprocess.run(
        ["git", "push", "-q", str(upstream), "main"],
        cwd=str(tmp_path / "up" / "work"),
        check=True,
    )
    out = run(plugin, FakeEvent("cave 刷新", admin=True))
    assert out[0][0] == "plain"
    assert "图库已更新" in out[0][1]
    assert plugin._head_sha != before
    assert "/new/extra.png" in candidates(plugin)
    assert plugin._last_diff is not None
    assert plugin._last_diff.added == ["/new/extra.png"]
    assert plugin._last_diff.changed == ["/http/403.gif"]


def test_change_text_reports_diff(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    run(plugin, FakeEvent("cave"))
    first = run(plugin, FakeEvent("cave 变化"))
    assert first[0][0] == "plain"
    assert "还没有发生过图库更新" in first[0][1]

    commit_all(tmp_path / "up" / "work", {"http/403.gif": b"v2"}, "tweak")
    subprocess.run(
        ["git", "push", "-q", str(upstream), "main"],
        cwd=str(tmp_path / "up" / "work"),
        check=True,
    )
    run(plugin, FakeEvent("cave 刷新", admin=True))
    second = run(plugin, FakeEvent("cave 变化"))
    assert "上次更新：新增 0、删除 0、内容变化 1" in second[0][1]


def test_index_survives_restart(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    run(plugin, FakeEvent("cave"))
    head = plugin._head_sha

    again = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, send_mode="url"
    )
    run_async(again._sync())
    assert again._head_sha == head
    assert candidates(again) == [
        "/ad-closenn/甲辰年.png",
        "/http/403.gif",
        "/httpcat/205.jpg",
    ]


def test_recent_limit_avoids_repeats(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        upstream=upstream,
        send_mode="url",
        recent_limit=2,
    )
    plugin._rng = random.Random(7)
    run_async(plugin._sync())
    seen = []
    for _ in range(2):
        plugin._last_sent.clear()
        out = run(plugin, FakeEvent("cave"))
        assert out[0][0] == "chain"
        seen.append(out[0][1][0].file)
    assert len(set(seen)) == 2


def test_terminate_closes_session(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, upstream=upstream)

    async def go():
        session = await plugin._http()
        await plugin.terminate()
        return session.closed, plugin._session

    closed, session = asyncio.run(go())
    assert closed is True
    assert session is None


def test_token_from_config_reaches_git_repo(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="s3cr3t")
    repo = plugin._git_repo()
    assert repo.has_token is True
    assert plugin._private_mode() is True


def test_token_from_env_when_config_empty(plugin_module, monkeypatch, tmp_path):
    monkeypatch.setenv(plugin_module.GIT_TOKEN_ENV, "from-env")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="")
    assert plugin._git_token() == "from-env"
    assert plugin._git_repo().has_token is True


def test_token_user_default_and_override(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="t")
    assert plugin._git_repo().token_user == "x-access-token"
    plugin2 = make_plugin(
        plugin_module, monkeypatch, tmp_path, git_token="t", git_token_user="oauth2"
    )
    assert plugin2._git_repo().token_user == "oauth2"


def test_no_token_keeps_public_mode(plugin_module, monkeypatch, tmp_path):
    monkeypatch.delenv(plugin_module.GIT_TOKEN_ENV, raising=False)
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="")
    assert plugin._private_mode() is False
    assert plugin._git_repo().has_token is False


def test_private_mode_skips_cdn(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, git_token="t", send_mode="url"
    )
    assert plugin._send_cdn() is False


def test_private_mode_never_builds_cdn_url(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="t")
    assert plugin._cdn_url(plugin_module.HubFile("/a.png", "1")) == ""


def test_private_mode_uses_api_download(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, git_token="t"
    )
    run_async(plugin._sync())
    for f in plugin._files:
        target = plugin_module.local_image_path(plugin.repo_dir, f.path)
        if target.is_file():
            target.unlink()

    seen = []

    async def fake_download(url, **kwargs):
        seen.append(url)
        return tmp_path / "dl.png"

    plugin._download = fake_download
    out = run(plugin, FakeEvent("cave"))
    assert seen and seen[0].startswith(
        "https://api.github.com/repos/someone/gallery/contents/"
    )
    assert plugin._head_sha in seen[0]
    assert out[0][1][0].file.startswith("file://")


def test_private_mode_falls_back_to_plain_text_not_jsdelivr(
    plugin_module, monkeypatch, tmp_path
):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, git_token="t"
    )
    run_async(plugin._sync())
    for f in plugin._files:
        target = plugin_module.local_image_path(plugin.repo_dir, f.path)
        if target.is_file():
            target.unlink()

    async def no_download(url, **kwargs):
        return None

    plugin._download = no_download
    out = run(plugin, FakeEvent("cave"))
    assert out[0][0] == "plain"
    assert "jsdelivr" not in repr(out)


def test_private_mode_caption_uses_blob_url(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        git_token="t",
        caption_template="{url}",
    )
    file = plugin_module.HubFile("/a b/c.png", "1")
    plugin._head_sha = "abc123"
    assert plugin._blob_url(file.path) == (
        "https://github.com/someone/gallery/blob/abc123/a%20b/c.png"
    )
    assert plugin._caption(file, "") == plugin._blob_url(file.path)


def test_public_mode_still_uses_cdn(plugin_module, monkeypatch, tmp_path):
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, upstream=upstream)
    run_async(plugin._sync())
    assert plugin._cdn_url(plugin._files[0]).startswith("https://cdn.jsdelivr.net/gh/")
    assert plugin._blob_url(plugin._files[0].path) == ""


def test_api_download_sends_auth_header(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="s3cr3t")
    plugin._head_sha = "abc123"
    url = plugin._api_url("/a.png")
    headers = plugin._download_headers(url)
    assert headers["Authorization"] == "Bearer s3cr3t"
    assert headers["Accept"] == "application/vnd.github.raw"
    assert plugin._download_headers("https://cdn.jsdelivr.net/gh/x") == {}


def test_api_content_type_is_accepted(plugin_module, monkeypatch, tmp_path):
    """GitHub Contents API 的 Content-Type 是 application/vnd.github.raw，
    不是 image/*，不能被当成「非图片内容」拒掉。"""
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="t")
    plugin._head_sha = "abc123"
    assert plugin._is_image_response(
        "application/vnd.github.raw; charset=utf-8", plugin._api_url("/a.png")
    )


def test_cdn_non_image_content_type_still_rejected(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="t")
    plugin._head_sha = "abc123"
    assert not plugin._is_image_response(
        "text/html", plugin._api_url("/a.png")
    )
    assert not plugin._is_image_response(
        "application/vnd.github.raw", "https://cdn.jsdelivr.net/gh/x/a.png"
    )


def test_git_error_text_never_contains_token(plugin_module, monkeypatch, tmp_path):
    """401 之类的报错文本里不能回显 token。"""
    plugin = make_plugin(plugin_module, monkeypatch, tmp_path, git_token="sup3rs3cret")
    repo = plugin._git_repo()
    err = repo._detail(128, "fatal: Authentication failed for 'https://github.com/a/b'", "克隆图库仓库")
    assert "sup3rs3cret" not in str(err)
    assert "sup3rs3cret" not in repr(repo._env())


def test_token_not_in_url_or_config_file(plugin_module, monkeypatch, tmp_path):
    """token 只能走环境变量，不能进 URL，也不能被 git 写进 .git/config。"""
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        git_token="sup3rs3cret",
        repo_url="https://example.com/owner/repo.git",
    )
    repo = plugin._git_repo()
    assert "sup3rs3cret" not in repo.url
    assert repo._env()["GIT_CONFIG_VALUE_0"].count("Authorization") == 1
    assert (
        repo._env()["GIT_CONFIG_KEY_0"]
        == "http.https://example.com/.extraheader"
    )


def test_local_upstream_skips_token_injection(plugin_module, monkeypatch, tmp_path):
    """本地路径上游不需要鉴权，注入反而会污染 git 环境。"""
    upstream = make_upstream(tmp_path / "up")
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, upstream=upstream, git_token="sup3rs3cret"
    )
    repo = plugin._git_repo()
    assert repo._env().get("GIT_CONFIG_VALUE_0") is None


def test_api_and_blob_urls_require_github_host(plugin_module, monkeypatch, tmp_path):
    """镜像不是 GitHub 时不能把 token 发给 api.github.com。"""
    plugin = make_plugin(
        plugin_module,
        monkeypatch,
        tmp_path,
        git_token="t",
        repo_url="https://gitcode.com/owner/gallery.git",
    )
    assert plugin._is_github_source() is False
    assert plugin._api_url("/a.png") == ""
    assert plugin._blob_url("/a.png") == ""


def test_api_url_works_for_default_github_source(plugin_module, monkeypatch, tmp_path):
    plugin = make_plugin(
        plugin_module, monkeypatch, tmp_path, git_token="t", repo_url=""
    )
    assert plugin._is_github_source() is True
    assert plugin._api_url("/a.png").startswith(
        "https://api.github.com/repos/someone/gallery/contents/a.png"
    )
