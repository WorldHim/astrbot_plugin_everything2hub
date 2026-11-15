"""git_client 的单元测试：解析、过滤、随机、索引缓存，以及真实 git 交互。"""

import json

import pytest

from tests.conftest import commit_all, make_upstream


def test_parse_ls_files_keeps_images_only(git_client):
    raw = (
        b"100644 aaaa 0\t/a.png\x00"
        b"100644 bbbb 0\t/b.md\x00"
        b"100644 cccc 0\t/del/c.GIF\x00"
    )
    files = git_client.parse_ls_files(raw)
    assert [f.path for f in files] == ["/a.png", "/del/c.GIF"]
    assert files[0].sha == "aaaa"
    assert files[0].name == "a.png"


def test_parse_ls_files_tolerates_garbage(git_client):
    raw = b"garbage-without-tab\x00\x00100644 aa 0\t/x.jpg\x00"
    files = git_client.parse_ls_files(raw)
    assert [f.path for f in files] == ["/x.jpg"]


def test_parse_ls_files_can_include_non_images(git_client):
    files = git_client.parse_ls_files(b"100644 aa 0\t/b.md\x00", only_images=False)
    assert [f.path for f in files] == ["/b.md"]


def test_git_url_requires_owner_slash_name(git_client):
    assert git_client.git_url("PCL-Community/PCL2Hub") == (
        "https://github.com/PCL-Community/PCL2Hub.git"
    )
    with pytest.raises(ValueError):
        git_client.git_url("PCL2Hub")


def test_diff_listings_reports_added_removed_changed(git_client):
    old = [git_client.HubFile("/a.png", "1"), git_client.HubFile("/b.png", "2")]
    new = [git_client.HubFile("/b.png", "9"), git_client.HubFile("/c.png", "3")]
    diff = git_client.diff_listings(old, new)
    assert diff.added == ["/c.png"]
    assert diff.removed == ["/a.png"]
    assert diff.changed == ["/b.png"]
    assert diff.summary() == "新增 1、删除 1、内容变化 1"


def test_diff_listings_without_diff(git_client):
    files = [git_client.HubFile("/a.png", "1")]
    diff = git_client.diff_listings(files, list(files))
    assert not diff.any
    assert diff.summary() == "无变化"


def test_filter_files_dir_and_keyword_rules(git_client):
    files = [
        git_client.HubFile("/ad-closenn/甲辰年.png", "1"),
        git_client.HubFile("/del/ad-closenn/dead.png", "2"),
        git_client.HubFile("/http/403.gif", "3"),
        git_client.HubFile("/httpcat/205.jpg", "4"),
    ]
    kept = git_client.filter_files(files, exclude_dirs=["del"])
    assert [f.path for f in kept] == [
        "/ad-closenn/甲辰年.png",
        "/http/403.gif",
        "/httpcat/205.jpg",
    ]

    only_dir = git_client.filter_files(files, include_dirs=["http"])
    assert [f.path for f in only_dir] == ["/http/403.gif"]

    by_keyword = git_client.filter_files(files, keyword_include=["HTTP"])
    assert [f.path for f in by_keyword] == [
        "/http/403.gif",
        "/httpcat/205.jpg",
    ]

    excluded = git_client.filter_files(files, keyword_exclude=["httpcat"])
    assert "/httpcat/205.jpg" not in [f.path for f in excluded]

    extra = git_client.filter_files(files, extra_keyword="甲辰")
    assert [f.path for f in extra] == ["/ad-closenn/甲辰年.png"]

    empty = git_client.filter_files(files, extra_keyword="不存在的关键字")
    assert empty == []


def test_build_cdn_url_quotes_every_segment(git_client):
    url = git_client.build_cdn_url(
        "PCL-Community/PCL2Hub", "main", "/ad-closenn/甲辰年.png", "cdn.jsdelivr.net"
    )
    assert url.startswith("https://cdn.jsdelivr.net/gh/PCL-Community/PCL2Hub@main/")
    assert " " not in url
    assert "%E7%94%B2" in url


def test_pick_random_avoids_recent(git_client):
    files = [git_client.HubFile(f"/{i}.png", str(i)) for i in range(4)]
    rng = __import__("random").Random(0)
    seen = set()
    for _ in range(20):
        chosen = git_client.pick_random(files, recent=["/0.png", "/1.png"], rng=rng)
        seen.add(chosen.path)
    assert seen == {"/2.png", "/3.png"}


def test_pick_random_falls_back_when_all_recent(git_client):
    files = [git_client.HubFile("/only.png", "1")]
    chosen = git_client.pick_random(files, recent=["/only.png"])
    assert chosen.path == "/only.png"


def test_pick_random_empty(git_client):
    assert git_client.pick_random([]) is None


def test_update_recent_trims(git_client):
    recent = ["/a.png", "/b.png", "/c.png"]
    assert git_client.update_recent(recent, "/a.png", 2) == ["/a.png", "/b.png"]
    assert git_client.update_recent(recent, "/d.png", 2) == ["/d.png", "/a.png"]
    assert git_client.update_recent([], "/x.png", 0) == []


def test_local_image_path_blocks_traversal(git_client, tmp_path):
    root = tmp_path / "repo"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.png").write_bytes(b"1")
    assert git_client.local_image_path(root, "/a/x.png").is_file()
    with pytest.raises(ValueError):
        git_client.local_image_path(root, "/../outside.png")


def test_short_id_and_url_key(git_client):
    assert git_client.short_id("abcdef1234567890") == "abcdef123456…"
    assert git_client.short_id("abc") == "abc"
    assert len(git_client.url_key("https://example.com/a.png")) == 16


def test_index_store_roundtrip(git_client, tmp_path):
    store = git_client.IndexStore(tmp_path)
    files = [git_client.HubFile("/a.png", "sha-a")]
    store.save("owner/repo", "main", files, commit="deadbeef")
    state = store.read("owner/repo", "main")
    assert state is not None
    assert state.commit == "deadbeef"
    assert state.files == files


def test_index_store_ignores_broken_file(git_client, tmp_path):
    store = git_client.IndexStore(tmp_path)
    path = store._file("owner/repo", "main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.read("owner/repo", "main") is None
    path.write_text(json.dumps({"files": "nope"}), encoding="utf-8")
    assert store.read("owner/repo", "main") is None


@pytest.mark.asyncio
async def test_gitrepo_clone_list_and_pull(git_client, tmp_path):
    bare = make_upstream(tmp_path / "up")
    workdir = tmp_path / "plugin_data" / "repo"
    repo = git_client.GitRepo(
        workdir, "owner/repo", "main", url_override=str(bare), depth=1
    )
    assert not repo.is_cloned()

    head = await repo.clone()
    assert head
    assert repo.is_cloned()

    files = await repo.list_files()
    assert [f.path for f in files] == [
        "/ad-closenn/甲辰年.png",
        "/del/old.png",
        "/http/403.gif",
        "/httpcat/205.jpg",
    ]
    assert all(len(f.sha) == 40 for f in files)
    local = git_client.local_image_path(workdir, "/ad-closenn/甲辰年.png")
    assert local.read_bytes() == b"png-ad"

    changed, same = await repo.pull()
    assert changed is False
    assert same == head

    work = tmp_path / "up" / "work"
    commit_all(
        work,
        {"http/403.gif": b"gif-403-v2", "new/extra.png": b"png-extra"},
        "update upstream",
    )
    git_client.subprocess.run(
        ["git", "push", "-q", str(bare), "main"], cwd=str(work), check=True
    )
    changed, new_head = await repo.pull()
    assert changed is True
    assert new_head != head
    files2 = await repo.list_files()
    paths = [f.path for f in files2]
    assert "/new/extra.png" in paths
    old_gif = next(f for f in files if f.path == "/http/403.gif")
    new_gif = next(f for f in files2 if f.path == "/http/403.gif")
    assert old_gif.sha != new_gif.sha
    diff = git_client.diff_listings(files, files2)
    assert diff.added == ["/new/extra.png"]
    assert diff.changed == ["/http/403.gif"]


@pytest.mark.asyncio
async def test_gitrepo_head_empty_before_clone(git_client, tmp_path):
    repo = git_client.GitRepo(tmp_path / "none", "owner/repo", "main")
    assert await repo.head() == ""
    with pytest.raises(git_client.GitError):
        await repo.list_files()
