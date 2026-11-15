"""在真实 astrbot 容器镜像里跑插件：真 git clone + 两个平台的消息链序列化。"""

import asyncio
import json
import shutil
import sys
from pathlib import Path

PLUGIN_SRC = Path("/tmp/e2h_repo")
DATA = Path("/AstrBot/data")
PLUGINS = DATA / "plugins"
DEST = PLUGINS / "astrbot_plugin_everything2hub"

if DEST.exists():
    shutil.rmtree(DEST)
shutil.copytree(
    PLUGIN_SRC,
    DEST,
    ignore=shutil.ignore_patterns(
        ".git", "data", "__pycache__", ".pytest_cache", ".ruff_cache"
    ),
)
sys.path.insert(0, str(DATA.parent))

print(
    "git in container:",
    __import__("subprocess")
    .run(["git", "--version"], capture_output=True, text=True)
    .stdout.strip(),
)

mod = __import__("data.plugins.astrbot_plugin_everything2hub.main", fromlist=["main"])
gc = sys.modules["data.plugins.astrbot_plugin_everything2hub.git_client"]
print(
    "plugin loaded:", mod.Everything2HubPlugin.__name__, "| repo module:", gc.__name__
)


class E:
    def __init__(self, text="cave", admin=False, platform="aiocqhttp"):
        self.message_str = text
        self.unified_msg_origin = f"{platform}:GroupMessage:123456"
        self._admin = admin
        self._platform = platform

    def plain_result(self, t):
        return ("plain", t)

    def chain_result(self, c):
        return ("chain", c)

    def is_admin(self):
        return self._admin

    def get_platform_name(self):
        return self._platform


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
    "git_timeout_seconds": 240,
    "timeout_seconds": 30,
}

from astrbot.core.star.star_tools import StarTools

orig = StarTools.get_data_dir
StarTools.get_data_dir = classmethod(
    lambda cls, name=None: DATA / "plugin_data" / (name or "x")
)
plugin = mod.Everything2HubPlugin(context=None, config=cfg)


async def step(ev):
    return [x async for x in plugin.cave(ev)]


async def main():
    try:
        out = await step(E("cave"))
        kind, chain = out[0]
        print("SEND:", kind, [type(c).__name__ for c in chain])
        img = next(c for c in chain if type(c).__name__ == "Image")
        p = await img.convert_to_file_path()
        print("image path:", Path(p).name, Path(p).stat().st_size, "bytes")
        b64 = await img.convert_to_base64()
        print("aiocqhttp base64 head:", b64[:24], "len:", len(b64))

        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
            QQOfficialMessageEvent,
        )

        mc = MessageChain(chain=chain)
        parsed = await QQOfficialMessageEvent._parse_to_qqofficial(mc)
        print("qq_official parse:", type(parsed).__name__)
        dumped = json.dumps(parsed, ensure_ascii=False, default=str)
        print(
            "qq_official payload keys:",
            [k for k in json.loads(dumped)][:8]
            if dumped.startswith("{")
            else dumped[:120],
        )

        out2 = await step(E("cave help", platform="qq_official"))
        print("HELP(last line):", out2[0][1].splitlines()[-1])

        repo = gc.GitRepo(
            DATA / "plugin_data" / "astrbot_plugin_everything2hub" / "repo",
            "PCL-Community/PCL2Hub",
            "main",
        )
        files = await repo.list_files()
        print("FILES:", len(files), "HEAD:", (await repo.head())[:12])
        changed, _head = await repo.pull()
        print("PULL changed:", changed)

        out3 = await step(E("cave 变化"))
        print("DIFF:", out3[0][1].splitlines()[0])
    finally:
        await plugin.terminate()


asyncio.run(main())
print("CONTAINER E2E OK")
