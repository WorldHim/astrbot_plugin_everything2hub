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
