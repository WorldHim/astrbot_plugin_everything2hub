"""真实网络下验证「上游有新提交」的 pull + 变化检测路径。"""

import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "e2h_plugin", ROOT / "main.py", submodule_search_locations=[str(ROOT)]
)
mod = importlib.util.module_from_spec(spec)
sys.modules["e2h_plugin"] = mod
spec.loader.exec_module(mod)
gc = sys.modules["e2h_plugin.git_client"]

BASE = ROOT / "data" / "pullcheck"
if BASE.exists():
    shutil.rmtree(BASE)
DATA = BASE / "plugin_data" / "astrbot_plugin_everything2hub"
DATA.mkdir(parents=True)

repo = gc.GitRepo(DATA / "repo", "PCL-Community/PCL2Hub", "main", depth=2, timeout=300)


class E:
    def __init__(self, text="cave", admin=False):
        self.message_str = text
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1"

    def plain_result(self, t):
        return ("plain", t)

    def chain_result(self, c):
        return ("chain", c)

    def is_admin(self):
        return True

    def get_platform_name(self):
        return "aiocqhttp"


async def main():
    head = await repo.clone()
    print("clone depth2 HEAD:", head[:12])
    import subprocess

    await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(repo.workdir), "reset", "--hard", "HEAD~1"],
        check=True,
        capture_output=True,
    )
    old = await repo.head()
    old_files = await repo.list_files()
    print("rolled back to:", old[:12], "files:", len(old_files))

    cfg = {
        "repo": "PCL-Community/PCL2Hub",
        "branch": "main",
        "exclude_dirs": ["del"],
        "send_mode": "url",
        "cooldown_seconds": 0,
        "clone_depth": 2,
        "pull_interval_seconds": 0,
        "git_timeout_seconds": 300,
        "recent_limit": 5,
    }
    mod.StarTools.get_data_dir = classmethod(lambda cls, name=None: DATA)
    plugin = mod.Everything2HubPlugin(context=None, config=cfg)
    plugin._head_sha = old
    plugin._files = old_files

    await plugin._sync(force=True)
    print("after pull HEAD:", plugin._head_sha[:12])
    print("diff:", plugin._last_diff.summary() if plugin._last_diff else None)
    if plugin._last_diff:
        print("  added:", plugin._last_diff.added[:3])
        print("  removed:", plugin._last_diff.removed[:3])
        print("  changed:", plugin._last_diff.changed[:3])
    text = plugin._change_text()
    print("change text first lines:", " | ".join(text.splitlines()[:6]))
    out = [x async for x in plugin.cave(E("cave httpcat"))]
    print(
        "send after pull:",
        out[0][0],
        out[0][1][0].file[:70] if out[0][0] == "chain" else out[0][1],
    )
    await plugin.terminate()


asyncio.run(main())
print("PULL-CHANGE OK")
