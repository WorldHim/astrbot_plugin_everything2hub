"""git 图库仓库客户端：克隆、pull、列图、算变化，不依赖 AstrBot。"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")

CDN_HOSTS = (
    "cdn.jsdelivr.net",
    "fastly.jsdelivr.net",
    "gcore.jsdelivr.net",
    "testingcf.jsdelivr.net",
)

NUL = bytes(1)
TAB = bytes((9,))

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GIT_LFS_SKIP_SMUDGE": "1",
    "GCM_INTERACTIVE": "never",
}

# 配置里没填 token 时，从这个环境变量兜底，便于部署时把凭据放在配置之外。
GIT_TOKEN_ENV = "E2H_GIT_TOKEN"


def auth_scope(url: str) -> str:
    """把 clone 地址收敛成 git 的鉴权作用域（scheme://host/）。

    只对 http(s) 生效：本地路径、file:// 上游不需要也不能带鉴权头。
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/"


def token_env(
    token: str,
    url: str,
    *,
    user: str = "x-access-token",
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """构造把 token 注入成 HTTP 鉴权头的环境变量增量。

    用 GIT_CONFIG_COUNT 而不是写 .git/config，token 不落盘；作用域限定到
    scheme://host/，重定向或换镜像都带不走。
    """
    token = (token or "").strip()
    scope = auth_scope(url)
    if not token or not scope:
        return {}
    user = (user or "x-access-token").strip() or "x-access-token"
    credentials = base64.b64encode(f"{user}:{token}".encode()).decode()
    current = str((env or {}).get("GIT_CONFIG_COUNT") or "0")
    try:
        index = int(current)
    except ValueError:
        index = 0
    return {
        "GIT_CONFIG_COUNT": str(index + 1),
        f"GIT_CONFIG_KEY_{index}": f"http.{scope}.extraheader",
        f"GIT_CONFIG_VALUE_{index}": f"Authorization: Basic {credentials}",
    }


class GitError(RuntimeError):
    """git 命令执行失败。"""


@dataclass(frozen=True)
class HubFile:
    """仓库中的一个图片文件。"""

    path: str
    sha: str = ""

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass
class ListingDiff:
    """两份文件列表之间的差异。"""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def summary(self) -> str:
        if not self.any:
            return "无变化"
        return (
            f"新增 {len(self.added)}、删除 {len(self.removed)}、"
            f"内容变化 {len(self.changed)}"
        )


@dataclass(frozen=True)
class IndexState:
    """索引快照：文件列表 + 时间戳 + 产生它的 commit。"""

    files: list[HubFile]
    updated_at: float
    commit: str = ""


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_SUFFIXES)


def _norm_list(values: Iterable[str] | None) -> list[str]:
    """把配置里的列表规整成小写、去空白的字符串列表。"""
    out: list[str] = []
    if not values:
        return out
    if isinstance(values, str):
        values = [values]
    for raw in values:
        if raw is None:
            continue
        item = str(raw).strip().strip("/").strip().lower()
        if item:
            out.append(item)
    return out


def git_url(repo: str) -> str:
    """owner/name 转成 https 克隆地址。"""
    repo = repo.strip().strip("/")
    if "/" not in repo:
        raise ValueError(f"repo 必须是 owner/name 形式，收到: {repo!r}")
    return f"https://github.com/{repo}.git"


def parse_ls_files(output: bytes, *, only_images: bool = True) -> list[HubFile]:
    """解析 git ls-files -s -z 的输出。

    每条形如 "100644 <blob sha> 0<TAB><path>"，记录之间用 NUL 分隔。
    """
    out: list[HubFile] = []
    seen: set[str] = set()
    for chunk in output.split(NUL):
        if not chunk:
            continue
        meta, sep, raw_path = chunk.partition(TAB)
        if not sep:
            continue
        parts = meta.split()
        if len(parts) < 2:
            continue
        sha = parts[1].decode("ascii", "replace")
        path = raw_path.decode("utf-8", "replace").strip()
        if not path:
            continue
        path = "/" + path.lstrip("/")
        if only_images and not is_image(path):
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(HubFile(path=path, sha=sha))
    return out


def diff_listings(old: Sequence[HubFile], new: Sequence[HubFile]) -> ListingDiff:
    """对比两份列表，找出新增 / 删除 / 内容变化（按 blob sha 判断）。"""
    old_map = {f.path: f for f in old}
    new_map = {f.path: f for f in new}
    return ListingDiff(
        added=[p for p in new_map if p not in old_map],
        removed=[p for p in old_map if p not in new_map],
        changed=[
            p
            for p, f in new_map.items()
            if p in old_map and old_map[p].sha and f.sha and old_map[p].sha != f.sha
        ],
    )


def _match_dir(path_lower: str, dirs: Sequence[str]) -> bool:
    for d in dirs:
        if path_lower.startswith("/" + d + "/") or path_lower == "/" + d:
            return True
    return False


def filter_files(
    files: Sequence[HubFile],
    *,
    include_dirs: Iterable[str] | None = None,
    exclude_dirs: Iterable[str] | None = None,
    keyword_include: Iterable[str] | None = None,
    keyword_exclude: Iterable[str] | None = None,
    extra_keyword: str | None = None,
) -> list[HubFile]:
    """按目录/关键字白黑名单过滤。白名单为空表示不限制；黑名单总是生效。"""
    inc_dirs = _norm_list(include_dirs)
    exc_dirs = _norm_list(exclude_dirs)
    inc_words = _norm_list(keyword_include)
    exc_words = _norm_list(keyword_exclude)
    if extra_keyword:
        extra = extra_keyword.strip().lower()
        if extra:
            inc_words.append(extra)

    out: list[HubFile] = []
    for f in files:
        low = f.path.lower()
        if inc_dirs and not _match_dir(low, inc_dirs):
            continue
        if exc_dirs and _match_dir(low, exc_dirs):
            continue
        if inc_words and not any(w in low for w in inc_words):
            continue
        if exc_words and any(w in low for w in exc_words):
            continue
        out.append(f)
    return out


def build_cdn_url(repo: str, version: str, path: str, host: str = CDN_HOSTS[0]) -> str:
    """构造 jsDelivr 直链（只有 send_mode=url 时才用得到）。"""
    repo = repo.strip().strip("/")
    version = (version or "main").strip()
    host = (host or CDN_HOSTS[0]).strip()
    if "/" not in repo:
        raise ValueError(f"repo 必须是 owner/name 形式，收到: {repo!r}")
    encoded = "/".join(quote(seg, safe="") for seg in path.strip("/").split("/"))
    return f"https://{host}/gh/{repo}@{version}/{encoded}"


def pick_random(
    files: Sequence[HubFile],
    *,
    recent: Sequence[str] | None = None,
    rng=None,
) -> HubFile | None:
    """随机抽一张，尽量避开最近发过的。"""
    if not files:
        return None
    import random as random_module

    rng = rng or random_module
    recent_set = set(recent or [])
    pool = [f for f in files if f.path not in recent_set] or list(files)
    return rng.choice(pool)


def update_recent(recent: Sequence[str] | None, chosen: str, limit: int) -> list[str]:
    """更新「最近发送」队列，最多保留 limit 条。"""
    limit = max(0, int(limit))
    items = [p for p in (recent or []) if p != chosen]
    items.insert(0, chosen)
    return items[:limit] if limit else []


def local_image_path(repo_dir: Path, path: str) -> Path:
    """仓库内路径转本地文件路径（拒绝跳出仓库目录的路径）。"""
    root = Path(repo_dir).resolve()
    target = (root / path.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径越界，拒绝访问：{path!r}")
    return target


def short_id(value: str, keep: int = 12) -> str:
    return value[:keep] + "…" if len(value) > keep else value


def url_key(url: str) -> str:
    """给没有 git sha 的场景生成一个稳定短标识。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def find_git(git_path: str | None) -> str:
    """定位 git 可执行文件；找不到抛 GitError。"""
    import shutil

    candidate = (git_path or "").strip() or "git"
    found = shutil.which(candidate)
    if not found:
        raise GitError(
            f"找不到 git 命令（{candidate!r}）。AstrBot 运行环境里需要能执行 git，"
            "或在插件配置里把 git_path 指向 git 可执行文件。"
        )
    return found


class GitRepo:
    """一个本地克隆的图库仓库。"""

    def __init__(
        self,
        workdir: Path,
        repo: str,
        branch: str = "main",
        *,
        git_path: str | None = None,
        depth: int = 1,
        timeout: float = 120.0,
        url_override: str | None = None,
        token: str | None = None,
        token_user: str = "x-access-token",
    ) -> None:
        self.workdir = Path(workdir)
        self.repo = repo.strip().strip("/")
        self.branch = (branch or "main").strip() or "main"
        self.git_path = git_path
        self.depth = max(0, int(depth))
        self.timeout = max(1.0, float(timeout))
        self.url_override = (url_override or "").strip() or None
        self.token = (token or "").strip()
        self.token_user = (token_user or "x-access-token").strip() or "x-access-token"
        self._git: str | None = None

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    @property
    def git(self) -> str:
        if self._git is None:
            self._git = find_git(self.git_path)
        return self._git

    @property
    def url(self) -> str:
        if self.url_override:
            return self.url_override
        return git_url(self.repo)

    def _env(self) -> dict:
        import os

        env = dict(os.environ)
        env.update(GIT_ENV)
        env.update(token_env(self.token, self.url, user=self.token_user, env=env))
        return env

    async def _run(
        self, *args: str, timeout: float | None = None
    ) -> tuple[int, str, str]:
        """在线程里跑 git：不依赖事件循环类型，Windows/Linux 都稳。"""
        import asyncio

        argv = [self.git, *args]
        limit = self.timeout if timeout is None else max(1.0, timeout)
        cwd = str(self.workdir.parent if not self.workdir.exists() else self.workdir)

        def _call() -> tuple[int, str, str]:
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=limit,
                    env=self._env(),
                    cwd=cwd,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise GitError(f"找不到 git 命令：{self.git}") from exc
            except subprocess.TimeoutExpired as exc:
                raise GitError(
                    f"git 执行超时（{limit:.0f}s）：{' '.join(argv[1:4])}"
                ) from exc
            return (
                proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace").strip(),
            )

        return await asyncio.to_thread(_call)

    async def _git_dir_run(self, *args: str, timeout: float | None = None):
        """在仓库目录里执行 git。"""
        return await self._run("-C", str(self.workdir), *args, timeout=timeout)

    def _detail(self, rc: int, err: str, action: str) -> GitError:
        tail = err.splitlines()[-3:] if err else []
        detail = " / ".join(tail) or f"exit code {rc}"
        return GitError(f"{action}失败：{detail}")

    def is_cloned(self) -> bool:
        return (self.workdir / ".git").exists()

    async def head(self) -> str:
        """当前 HEAD 的完整 sha；仓库不存在返回空串。"""
        if not self.is_cloned():
            return ""
        rc, out, _err = await self._git_dir_run("rev-parse", "HEAD")
        if rc != 0:
            return ""
        return out.strip()

    async def clone(self, force: bool = False) -> str:
        """克隆仓库，返回 HEAD。"""
        if self.is_cloned() and not force:
            return await self.head()
        self.workdir.parent.mkdir(parents=True, exist_ok=True)
        if self.workdir.exists() and force:
            import shutil

            shutil.rmtree(self.workdir, ignore_errors=True)
        args = ["clone", "--single-branch", "--branch", self.branch, "--no-tags"]
        if self.depth > 0:
            args += ["--depth", str(self.depth)]
        args += [self.url, str(self.workdir)]
        rc, out, err = await self._run(*args, timeout=max(self.timeout, 300.0))
        if rc != 0 or not self.is_cloned():
            raise self._detail(rc, err or out, "克隆图库仓库")
        return await self.head()

    async def pull(self) -> tuple[bool, str]:
        """拉取最新提交，返回 (是否有更新, 新 HEAD)。"""
        if not self.is_cloned():
            return True, await self.clone()
        before = await self.head()
        args = ["pull", "--ff-only", "--no-tags"]
        if self.depth > 0:
            args += ["--depth", str(self.depth)]
        args += ["origin", self.branch]
        rc, _out, err = await self._git_dir_run(*args, timeout=max(self.timeout, 300.0))
        if rc != 0:
            fetch_args = ["fetch", "--no-tags"]
            if self.depth > 0:
                fetch_args += ["--depth", str(self.depth)]
            fetch_args += ["origin", self.branch]
            rc2, _o2, err2 = await self._git_dir_run(
                *fetch_args, timeout=max(self.timeout, 300.0)
            )
            if rc2 != 0:
                raise self._detail(rc2, err2 or err, "拉取图库仓库")
            rc3, _o3, err3 = await self._git_dir_run("reset", "--hard", "FETCH_HEAD")
            if rc3 != 0:
                raise self._detail(rc3, err3, "重置图库工作区")
        after = await self.head()
        return before != after, after

    async def list_files(self, *, only_images: bool = True) -> list[HubFile]:
        """列出工作区里的文件（带 blob sha）。"""
        if not self.is_cloned():
            raise GitError("图库仓库还没有克隆")
        rc, out, err = await self._git_dir_run("ls-files", "-s", "-z")
        if rc != 0:
            raise self._detail(rc, err, "列出仓库文件")
        return parse_ls_files(out.encode("utf-8", "replace"), only_images=only_images)


class IndexStore:
    """记录上一次看到的文件列表，用来算「图库变了什么」。"""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)

    def _file(self, repo: str, branch: str) -> Path:
        safe = (repo + "@" + branch).replace("/", "__").replace(":", "_")
        return self.cache_dir / f"index-{safe}.json"

    def read(self, repo: str, branch: str) -> IndexState | None:
        import json

        try:
            raw = json.loads(self._file(repo, branch).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        updated = raw.get("updated_at")
        files = raw.get("files")
        if not isinstance(updated, (int, float)) or not isinstance(files, list):
            return None
        parsed: list[HubFile] = []
        for item in files:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                sha = item.get("sha")
                parsed.append(
                    HubFile(path=item["path"], sha=sha if isinstance(sha, str) else "")
                )
        commit = raw.get("commit")
        return IndexState(
            files=parsed,
            updated_at=float(updated),
            commit=commit if isinstance(commit, str) else "",
        )

    def save(
        self, repo: str, branch: str, files: Sequence[HubFile], commit: str = ""
    ) -> None:
        import json

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "repo": repo,
            "branch": branch,
            "commit": commit,
            "files": [{"path": f.path, "sha": f.sha} for f in files],
        }
        target = self._file(repo, branch)
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except OSError:
            tmp.unlink(missing_ok=True)
