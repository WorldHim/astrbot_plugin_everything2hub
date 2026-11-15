"""模拟 AstrBot 的导入方式（data.plugins.<dir>.main）验证相对导入可用。"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dsh_plugin_import_"))
    pkg = tmp / "data" / "plugins" / "astrbot_plugin_everything2hub"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    if pkg.exists():
        shutil.rmtree(pkg)
    shutil.copytree(
        REPO_ROOT, pkg, ignore=shutil.ignore_patterns(".git", "data", "__pycache__")
    )
    sys.path.insert(0, str(tmp))
    mod = __import__(
        "data.plugins.astrbot_plugin_everything2hub.main", fromlist=["main"]
    )
    assert hasattr(mod, "Everything2HubPlugin"), "插件主类缺失"
    gc = __import__(
        "data.plugins.astrbot_plugin_everything2hub.git_client", fromlist=["git_client"]
    )
    assert gc.IMAGE_SUFFIXES
    import inspect

    src = inspect.getsource(mod)
    assert "filter.command" not in src.split("class Everything2HubPlugin")[0]
    work = tmp / "up_work"
    work.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        }
    )
    run = lambda *a, cwd=work: subprocess.run(
        ["git", *a], cwd=str(cwd), check=True, capture_output=True, env=env
    )
    run("init", "-q", "-b", "main")
    (work / "echo" / "a.png").parent.mkdir(parents=True)
    (work / "echo" / "a.png").write_bytes(b"png-a")
    (work / "echo" / "b.jpg").write_bytes(b"jpg-b")
    (work / "echo" / "note.md").write_text("x", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    bare = tmp / "upstream.git"
    run("clone", "-q", "--bare", str(work), str(bare))
    run("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)

    import asyncio

    repo = gc.GitRepo(tmp / "work", "o/r", "main", url_override=str(bare), depth=1)

    async def go():
        head = await repo.clone()
        files = await repo.list_files()
        return head, files

    head, files = asyncio.run(go())
    assert head and [f.path for f in files] == ["/echo/a.png", "/echo/b.jpg"], files
    print("导入与 clone 验证通过：", head[:12], [f.path for f in files])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
