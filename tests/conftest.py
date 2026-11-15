"""把插件目录当作一个包加载，便于测试相对导入。"""

import asyncio
import importlib.util
import inspect
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "e2h_plugin"

# 测试用的图库仓库内容：目录 + 文件名覆盖了中英文、空格、多层目录
FIXTURE_FILES = {
    "ad-closenn/甲辰年.png": b"png-ad",
    "http/403.gif": b"gif-403",
    "del/old.png": b"png-deleted",
    "docs/readme.md": b"# not an image",
    "httpcat/205.jpg": b"jpg-205",
}


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: 由本文件里的 hook 直接运行协程测试")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """不装 pytest-asyncio 也能跑 async def 测试。"""
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    argnames = pyfuncitem._fixtureinfo.argnames
    kwargs = {name: pyfuncitem.funcargs[name] for name in argnames}
    asyncio.run(pyfuncitem.obj(**kwargs))
    return True


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin_module():
    return _load_package()


@pytest.fixture(scope="session")
def git_client(plugin_module):
    return sys.modules[f"{PACKAGE}.git_client"]


def git_available() -> bool:
    return shutil.which("git") is not None


requires_git = pytest.mark.skipif(not git_available(), reason="需要本机安装 git")


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            # 隔离用户/系统 git 配置，避免受本机代理、签名等设置影响
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
        text=True,
        env=env,
    )
    return proc.stdout


def make_upstream(root: Path, files: dict[str, bytes] | None = None) -> Path:
    """造一个 bare 仓库，作为 git clone 的上游。"""
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "-q", "-b", "main")
    commit_all(work, files or FIXTURE_FILES)
    bare = root / "upstream.git"
    _git(work, "clone", "-q", "--bare", str(work), str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    return bare


def commit_all(work: Path, files: dict[str, bytes], message: str = "update") -> None:
    for rel, data in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
