import asyncio
import json
import tempfile
from pathlib import Path

import yaml
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.filter.platform_adapter_type import ADAPTER_NAME_2_TYPE
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
print("schema keys:", len(schema))
cfg = AstrBotConfig(Path(tempfile.mkdtemp()) / "cfg.json", schema=schema)
print(
    "config entries:", len(cfg), "| repo:", cfg["repo"], "| depth:", cfg["clone_depth"]
)
for key in (
    "repo_url",
    "git_path",
    "send_mode",
    "exclude_dirs",
    "pull_interval_seconds",
):
    assert key in cfg, key
assert "aiocqhttp" in ADAPTER_NAME_2_TYPE and "qq_official" in ADAPTER_NAME_2_TYPE
SpecifierSet(">=4.9.0")
meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
print("metadata:", meta["name"], meta["version"], meta["support_platforms"])

from astrbot.api.message_components import Image

tmpimg = Path(tempfile.mkdtemp()) / "x.png"
tmpimg.write_bytes(b"PNG" + b"0" * 200)
img = Image.fromFileSystem(str(tmpimg))
path = asyncio.run(img.convert_to_file_path())
b64 = asyncio.run(img.convert_to_base64())
url_img = Image.fromURL(
    "https://cdn.jsdelivr.net/gh/PCL-Community/PCL2Hub@main/http/403.gif"
)
print("local image ->", Path(path).suffix, len(b64) > 100, img.file[:7])
print("url image ok:", url_img.file.startswith("https://"))
print("ALL OK")
