"""真联网端到端：用真 PCL2Hub 仓库跑插件的 cave 指令。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]).replace("tests", ""))

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "e2h_plugin", ROOT / "main.py", submodule_search_locations=[str(ROOT)]
)
mod = importlib.util.module_from_spec(spec)
sys.modules["e2h_plugin"] = mod
spec.loader.exec_module(mod)
gc = sys.modules["e2h_plugin.git_client"]

DATA = Path(
    r"D:/astrbot_plugin_everything2hub/data/plugin_data/astrbot_plugin_everything2hub"
)


class E:
    def __init__(self, text="cave", admin=False):
        self.message_str = text
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123456"
        self._admin = admin

    def plain_result(self, t):
        return ("plain", t)

    def chain_result(self, c):
        return ("chain", c)

    def is_admin(self):
        return self._admin

    def get_platform_name(self):
        return "aiocqhttp"


cfg = {
    "repo": "PCL-Community/PCL2Hub",
    "branch": "main",
    "cdn": "cdn.jsdelivr.net",
    "exclude_dirs": ["del"],
    "send_mode": "auto",
    "cooldown_seconds": 0,
    "recent_limit": 30,
    "clone_depth": 1,
    "pull_interval_seconds": 0,
    "git_timeout_seconds": 180,
    "timeout_seconds": 25,
}

orig = mod.StarTools.get_data_dir
mod.StarTools.get_data_dir = classmethod(lambda cls, name=None: DATA)
plugin = mod.Everything2HubPlugin(context=None, config=cfg)


async def step(ev):
    return [x async for x in plugin.cave(ev)]


async def main():
    try:
        help_out = await step(E("cave 帮助"))
        print("HELP:", help_out[0][1].splitlines()[-1])

        for i in range(3):
            out = await step(E("cave"))
            kind, payload = out[0]
            if kind == "chain":
                desc = [type(c).__name__ for c in payload]
                img = next(c for c in payload if type(c).__name__ == "Image")
                size = Path(img.path).stat().st_size if getattr(img, "path", "") else -1
                print(f"SEND{i + 1}: {desc} file={img.file[:80]} bytes={size}")
            else:
                print(f"SEND{i + 1}: {payload}")

        out = await step(E("cave httpcat"))
        img = next(c for c in out[0][1] if type(c).__name__ == "Image")
        print("KEYWORD httpcat:", img.file.split("/gh/")[-1][:90])

        out = await step(E("cave 不存在关键词zzz"))
        print("NO MATCH:", out[0][1])

        out = await step(E("cave 变化"))
        print("DIFF:", out[0][1].replace("\n", " | ")[:200])

        files = await gc.GitRepo(
            DATA / "repo", "PCL-Community/PCL2Hub", "main"
        ).list_files()
        print(
            "REPO FILES:",
            len(files),
            "HEAD:",
            (await gc.GitRepo(DATA / "repo", "PCL-Community/PCL2Hub", "main").head())[
                :12
            ],
        )
        print("UNIQUE blob shas:", len({f.sha for f in files}) == len(files))

        changed, head = await gc.GitRepo(
            DATA / "repo", "PCL-Community/PCL2Hub", "main"
        ).pull()
        print("PULL changed:", changed, head[:12])
    finally:
        await plugin.terminate()


asyncio.run(main())
print("LIVE E2E OK")
