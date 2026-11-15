"""回声洞插件：cave 指令从 git 图库仓库随机抽一张图发送。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import Image, Plain, Poke
from astrbot.api.star import Context, Star, StarTools

from .git_client import (
    CDN_HOSTS,
    GitError,
    GitRepo,
    HubFile,
    IndexStore,
    ListingDiff,
    build_cdn_url,
    diff_listings,
    filter_files,
    local_image_path,
    pick_random,
    short_id,
    update_recent,
    url_key,
)

PLUGIN_NAME = "astrbot_plugin_everything2hub"
POKE_PLATFORMS = {"aiocqhttp"}


class PokeBotFilter(CustomFilter):
    """命中「戳一戳机器人」的通知事件（仅 aiocqhttp）。"""

    def __init__(self, raise_error: bool = False, **_) -> None:
        super().__init__(raise_error=raise_error)

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        if event.get_platform_name() not in POKE_PLATFORMS:
            return False
        return any(
            isinstance(seg, Poke) and seg.target_id() == str(event.get_self_id())
            for seg in event.get_messages()
        )

USER_AGENT = (
    "astrbot-plugin-everything2hub/2.0 "
    "(+https://github.com/WorldHim/astrbot_plugin_everything2hub)"
)


class Everything2HubPlugin(Star):
    """从指定图库仓库随机抽图并发送。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.repo_dir = self.data_dir / "repo"
        self.store = IndexStore(self.data_dir)
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._recent: dict[str, list[str]] = {}
        self._last_sent: dict[str, float] = {}
        self._rng = random.Random()
        self._head_sha: str = ""
        self._last_pull: float = 0.0
        self._last_diff: ListingDiff | None = None
        self._files: list[HubFile] = []

    def _cfg(self, key: str, default):
        value = self.config.get(key, default)
        if value is None or value == "":
            return default
        return value

    def _repo(self) -> str:
        return str(self._cfg("repo", "") or "").strip().strip("/")

    def _branch(self) -> str:
        return str(self._cfg("branch", "main")).strip() or "main"

    def _cdn_host(self) -> str:
        host = str(self._cfg("cdn", CDN_HOSTS[0])).strip()
        return host or CDN_HOSTS[0]

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=float(self._cfg("timeout_seconds", 20)))

    def _git_repo(self) -> GitRepo:
        return GitRepo(
            self.repo_dir,
            self._repo(),
            self._branch(),
            git_path=str(self._cfg("git_path", "git")),
            depth=int(self._cfg("clone_depth", 1) or 0),
            timeout=float(self._cfg("git_timeout_seconds", 120)),
            url_override=str(self._cfg("repo_url", "") or ""),
        )

    def _send_cdn(self) -> bool:
        return str(self._cfg("send_mode", "auto")).lower() == "url"

    async def _sync(self, force: bool = False) -> None:
        """确保本地仓库存在并且是最近的提交。"""
        if not self._repo():
            raise GitError("尚未配置图库仓库")
        interval = float(self._cfg("pull_interval_seconds", 300) or 0)
        if (
            not force
            and self._files
            and interval > 0
            and time.monotonic() - self._last_pull < interval
        ):
            return

        async with self._lock:
            if (
                not force
                and self._files
                and interval > 0
                and time.monotonic() - self._last_pull < interval
            ):
                return

            repo = self._git_repo()
            try:
                if not repo.is_cloned():
                    head = await repo.clone()
                    logger.info(
                        "图库仓库已克隆到 %s（HEAD %s）", repo.workdir, short_id(head)
                    )
                else:
                    changed, head = await repo.pull()
                    if changed:
                        logger.info("图库仓库已更新到 %s", short_id(head))
                    else:
                        logger.debug("图库仓库无更新（%s）", short_id(head))
                files = await repo.list_files()
            except GitError:
                if self._files:
                    logger.warning("图库同步失败，继续使用上次的文件列表")
                    self._last_pull = time.monotonic()
                    return
                raise

            before = list(self._files)
            if before and before != files:
                self._remember_diff(before, files)
            self._files = files
            self._head_sha = head
            self._last_pull = time.monotonic()
            self.store.save(self._repo(), self._branch(), files, commit=head)
            logger.info(
                "图库索引已更新：%d 张图片（HEAD %s）", len(files), short_id(head)
            )

    def _remember_diff(self, old: list[HubFile], new: list[HubFile]) -> None:
        diff = diff_listings(old, new)
        self._last_diff = diff
        if diff.any:
            logger.info("图库有更新：%s", diff.summary())
            if diff.added:
                logger.info("新增图片示例：%s", "、".join(diff.added[:5]))
            if diff.removed:
                logger.info("删除图片示例：%s", "、".join(diff.removed[:5]))
            if diff.changed:
                logger.info("内容变化图片示例：%s", "、".join(diff.changed[:5]))

    async def _candidates(self, keyword: str | None) -> list[HubFile]:
        try:
            await self._sync()
            files = self._files
        except GitError as exc:
            logger.warning("同步图库失败，尝试使用本地索引：%s", exc)
            state = self.store.read(self._repo(), self._branch())
            files = state.files if state else []
            if not files:
                raise
        return filter_files(
            files,
            include_dirs=self._cfg("include_dirs", []),
            exclude_dirs=self._cfg("exclude_dirs", []),
            keyword_include=self._cfg("keyword_include", []),
            keyword_exclude=self._cfg("keyword_exclude", []),
            extra_keyword=keyword,
        )

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout(),
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    def _cdn_url(self, file: HubFile) -> str:
        version = self._head_sha or self._branch()
        try:
            return build_cdn_url(self._repo(), version, file.path, self._cdn_host())
        except ValueError:
            return ""

    async def _download(self, url: str) -> Path | None:
        """直链下载兜底：本地工作区里没有这张图时用。"""
        if not url:
            return None
        target = (
            self.data_dir
            / "images"
            / f"{url_key(url)}{Path(url.split('?', 1)[0]).suffix.lower() or '.png'}"
        )
        if target.is_file() and target.stat().st_size > 0:
            return target
        max_bytes = int(float(self._cfg("max_download_mb", 8)) * 1024 * 1024)
        session = await self._http()
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not ctype.startswith("image/"):
                    logger.warning("图片链接返回了非图片内容（%s）：%s", ctype, url)
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        logger.warning(
                            "图片超过大小上限 %d 字节，回退直链：%s", max_bytes, url
                        )
                        return None
                    chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            return target
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.warning("下载图片失败，回退为直链发送：%s（%s）", url, exc)
            return None

    def _cooldown_left(self, umo: str) -> float:
        cooldown = float(self._cfg("cooldown_seconds", 10) or 0)
        if cooldown <= 0:
            return 0.0
        last = self._last_sent.get(umo, 0.0)
        left = cooldown - (time.monotonic() - last)
        return max(0.0, left)

    def _caption(self, file: HubFile, url: str) -> str:
        template = str(self._cfg("caption_template", "") or "")
        if not template:
            return ""
        try:
            return template.format(
                name=file.name,
                path=file.path.lstrip("/"),
                url=url,
                repo=self._repo(),
                branch=self._branch(),
                commit=self._head_sha or self._branch(),
            )
        except (KeyError, IndexError, ValueError):
            return template

    def _change_text(self) -> str:
        if not self._repo():
            return "尚未配置图库仓库，请在插件配置里填写 repo（owner/name 形式）。"
        state = self.store.read(self._repo(), self._branch())
        head = self._head_sha or (state.commit if state else "")
        lines = [
            "回声洞图库变化：",
            f"· 当前版本：{short_id(head) if head else '未知'}",
            "· 同步时间："
            + (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state.updated_at))
                if state
                else "尚未建立"
            ),
        ]
        diff = self._last_diff
        if diff is None:
            lines.append("· 本次启动后还没有发生过图库更新，无差异可报。")
        elif not diff.any:
            lines.append("· 上次更新：无变化。")
        else:
            lines.append(f"· 上次更新：{diff.summary()}")
            for label, items in (
                ("新增", diff.added),
                ("删除", diff.removed),
                ("变化", diff.changed),
            ):
                if items:
                    preview = "、".join(items[:5])
                    more = f" 等 {len(items)} 个" if len(items) > 5 else ""
                    lines.append(f"  - {label}：{preview}{more}")
        return "\n".join(lines)

    def _help_text(self) -> str:
        repo = f"{self._repo()}@{self._branch()}" if self._repo() else "未配置"
        return (
            "回声洞用法：\n"
            "· cave —— 随机抽一张图\n"
            "· cave 关键词 —— 只在路径/文件名含关键词的图里抽\n"
            "· cave 变化 —— 查看图库自上次更新以来新增/删除/改图的情况\n"
            "· cave 刷新 —— （管理员）立即 git pull 并刷新索引\n"
            "· cave 帮助 —— 显示本说明\n"
            f"当前图库：{repo}（{len(self._files)} 张）"
        )

    @filter.command("cave", alias={"回声洞"})
    async def cave(self, event: AstrMessageEvent):
        """从回声洞随机抽一张图。用法：cave [关键词]"""
        raw = (event.message_str or "").strip()
        parts = raw.split(maxsplit=1)
        keyword = parts[1].strip() if len(parts) > 1 else ""

        if keyword in {"help", "帮助", "?", "？"}:
            yield event.plain_result(self._help_text())
            return

        if keyword in {"变化", "diff", "更新"}:
            yield event.plain_result(self._change_text())
            return

        if keyword in {"刷新", "refresh", "update"}:
            if not event.is_admin():
                yield event.plain_result("只有管理员可以刷新图库仓库。")
                return
            try:
                await self._sync(force=True)
            except GitError as exc:
                logger.error("刷新图库仓库失败：%s", exc)
                yield event.plain_result(f"刷新失败：{exc}")
                return
            diff = self._last_diff
            tail = f"（{diff.summary()}）" if diff is not None else ""
            yield event.plain_result(
                f"图库已更新，共 {len(self._files)} 张图片{tail}。"
            )
            return

        async for ret in self._draw_and_send(event, keyword):
            yield ret

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.custom_filter(PokeBotFilter)
    async def on_poke(self, event: AstrMessageEvent):
        """戳一戳机器人时随机抽一张图。"""
        if not self._cfg("poke_enabled", False):
            return
        if not PokeBotFilter().filter(event, None):
            return
        async for ret in self._draw_and_send(event, ""):
            yield ret

    async def _draw_and_send(
        self, event: AstrMessageEvent, keyword: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        umo = event.unified_msg_origin
        if not self._repo():
            yield event.plain_result(
                "尚未配置图库仓库，请在插件配置里填写 repo（owner/name 形式）。"
            )
            return
        left = self._cooldown_left(umo)
        if left > 0:
            yield event.plain_result(f"回声洞冷却中，请 {left:.0f} 秒后再试。")
            return

        try:
            candidates = await self._candidates(keyword or None)
        except Exception as exc:  # noqa: BLE001 - 任何失败都只提示用户重试
            logger.error("获取图库失败：%s", exc)
            yield event.plain_result("图库暂时不可用，请稍后再试。")
            return

        if not candidates:
            tip = (
                f"没有找到匹配「{keyword}」的图片。"
                if keyword
                else "图库里没有任何可用图片。"
            )
            yield event.plain_result(tip)
            return

        chosen = pick_random(
            candidates,
            recent=self._recent.get(umo, []),
            rng=self._rng,
        )
        if chosen is None:
            yield event.plain_result("抽图失败，请稍后再试。")
            return

        cdn_url = self._cdn_url(chosen)
        chain = []
        caption = self._caption(chosen, cdn_url)
        if caption:
            chain.append(Plain(caption))

        sent_local = False
        if not self._send_cdn():
            path: Path | None = None
            try:
                candidate = local_image_path(self.repo_dir, chosen.path)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    path = candidate
            except (OSError, ValueError) as exc:
                logger.warning("读取本地图片失败：%s", exc)
            if path is None:
                path = await self._download(cdn_url)
            if path is not None:
                chain.append(Image.fromFileSystem(str(path)))
                sent_local = True

        if not sent_local:
            if not cdn_url:
                yield event.plain_result("图片发送失败，请稍后再试。")
                return
            chain.append(Image.fromURL(cdn_url))

        self._last_sent[umo] = time.monotonic()
        self._recent[umo] = update_recent(
            self._recent.get(umo, []),
            chosen.path,
            int(self._cfg("recent_limit", 30) or 0),
        )
        if len(self._last_sent) > 512:
            now = time.monotonic()
            self._last_sent = {
                k: v for k, v in self._last_sent.items() if now - v < 86400
            }

        logger.info(
            "回声洞发送：%s（关键词=%s，平台=%s，HEAD=%s）",
            chosen.path,
            keyword,
            event.get_platform_name(),
            short_id(self._head_sha),
        )
        yield event.chain_result(chain)

    async def terminate(self) -> None:
        """插件卸载时关闭 HTTP 会话。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
