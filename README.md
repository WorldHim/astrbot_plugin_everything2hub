# astrbot_plugin_everything2hub（回声洞）

从任意把图片放在 git 仓库里的图库中随机抽一张图发送的 AstrBot 插件。

- 指令：`cave`，别名 `回声洞`，遵守 AstrBot 全局 wake_prefix 与 @ 唤醒。
- 平台：`aiocqhttp`、`qq_official`。单条消息只发一个 `Image` 段。
- 图库用 git 维护：首次 `git clone`，之后按间隔 `git pull`，发送时直接读本地文件。
- 变化检测：每次 pull 后对比 `git ls-files -s` 的 blob sha，得到新增、删除、内容变化；HEAD 没变时跳过。
- 发送：优先发本地文件；缺失时回退 jsDelivr 直链。

## 安装

1. 把本仓库放进 AstrBot 的 `data/plugins/` 目录（目录名随意，但建议保持 `astrbot_plugin_everything2hub`）。
2. 在 WebUI 的「插件管理」里重载插件，或在控制台重启 AstrBot。
3. 打开插件配置按需调整（见下表）。

### 前置要求：git

AstrBot 运行环境里必须能执行 `git` 命令，并能访问 GitHub 或 `repo_url` 指定的镜像地址。

- 找不到 git 时，`cave` 会提示「图库暂时不可用」，日志里会写明 `找不到 git 命令`。
- git 不在 PATH 时，把 `git_path` 填成绝对路径。
- 克隆受限时，用 `repo_url` 指定镜像地址。

依赖：除 git 外只用到 AstrBot 自带的 `aiohttp`，无需额外安装。

## 用法

| 指令 | 说明 |
| --- | --- |
| `cave` | 随机抽一张图 |
| `cave 关键词` | 只在路径或文件名包含关键词的图里抽 |
| `cave 变化` | 查看图库自上次更新以来新增/删除/改图的情况 |
| `cave 刷新` | 立即 git pull 并刷新索引（仅管理员） |
| `cave 帮助` | 显示说明 |

> 前缀以 AstrBot 全局 `wake_prefix` 为准（默认 `/`），所以默认是 `/cave`。私聊是否需要前缀由 AstrBot 的 `friend_message_needs_wake_prefix` 决定。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `repo` | string | 必填 | 图库仓库，`owner/name` 形式 |
| `branch` | string | `main` | clone / pull 使用的分支或 tag |
| `repo_url` | string | 空 | 自定义克隆地址（镜像、私有仓库），留空则用 `https://github.com/<repo>.git` |
| `git_path` | string | `git` | git 可执行文件，不在 PATH 时填绝对路径 |
| `git_token` | string | 空 | 访问私有仓库的令牌；留空表示公开仓库，也可用环境变量 `E2H_GIT_TOKEN` 提供 |
| `git_token_user` | string | `x-access-token` | 令牌对应的用户名，GitHub 用默认值，GitLab 填 `oauth2` |
| `clone_depth` | int | `1` | 克隆深度；`1` 是浅克隆最省流量，`0` 表示完整克隆 |
| `pull_interval_seconds` | int | `300` | 两次抽图之间至少隔这么久才 pull，`0` 表示每次都 pull |
| `git_timeout_seconds` | int | `120` | 单次 clone / pull 的超时 |
| `send_mode` | string | `auto` | `auto` 发本地文件（推荐）；`url` 只发 jsDelivr 直链 |
| `cdn` | string | `cdn.jsdelivr.net` | 回退直链使用的节点，可选 fastly / gcore / testingcf |
| `include_dirs` | list | `[]` | 只抽取这些顶层目录，留空表示整仓 |
| `exclude_dirs` | list | `[]` | 排除这些顶层目录，留空不排除 |
| `keyword_include` | list | `[]` | 路径必须包含的任一关键字 |
| `keyword_exclude` | list | `[]` | 路径包含任一关键字就跳过 |
| `caption_template` | text | 空 | 图片附带的文字，支持 `{name}` `{path}` `{url}` `{repo}` `{branch}` `{commit}` |
| `cooldown_seconds` | int | `10` | 同一会话内的冷却，`0` 表示不限制 |
| `recent_limit` | int | `30` | 记住最近 N 张避免重复，`0` 表示不避重 |
| `timeout_seconds` | int | `20` | 回退下载时的超时 |
| `max_download_mb` | float | `8` | 回退下载的大小上限，超限则发直链 |
| `image_cache_limit` | int | `200` | 回退下载的缓存张数上限，超出删除最旧的，`0` 表示不限制 |

运行时数据都放在 `data/plugin_data/astrbot_plugin_everything2hub/`：

```
data/plugin_data/astrbot_plugin_everything2hub/
├── repo/                 # git 工作区（图库本体）
├── index-<repo>@<分支>.json  # 上一次的文件列表 + HEAD，用来算变化
└── images/               # 回退下载的图片缓存
```

不会往插件目录里写任何东西。

## 为什么用 git 判断图片变化

一次提交就是整个仓库快照的指纹：

- `git rev-parse HEAD` 给出当前快照的 commit sha。**HEAD 没变，任何一张图都不可能变**，于是这次连文件列表都不用看。
- HEAD 变了，`git ls-files -s` 列出每个文件的 **blob sha（文件内容的 sha1）**。把它和上一次的列表做集合比较，就能精确分出：
  - 新增：这次有、上次没有的路径；
  - 删除：上次有、这次没有的路径；
  - 内容变化：路径相同但 blob sha 不同的文件（同一个文件名换了图）。
- 结果写进日志，也可以用 `cave 变化` 查看。

因为是真实克隆，发送时直接读工作区里的文件，不依赖 CDN 是否可用；直链只在本地文件缺失时作为兜底。

## 换成别的图库

任何把图片放在仓库里的 GitHub 仓库都能用：把 `repo` 改成 `owner/name`、`branch` 改成对应分支即可。

## 私有仓库

私有图库只要填一枚只读令牌就能用，推荐按下面的最小权限方式准备：

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**，只勾选目标仓库的 **Contents: Read**。
2. 把令牌填进 `git_token`。不想写在配置文件里的话，改成设置环境变量 `E2H_GIT_TOKEN`（配置项留空时插件会读它）。
3. 如果图库在 GitLab，把 `git_token_user` 改成 `oauth2`。

几点与实际行为相关的说明：

- 令牌是通过 git 的 `http.<仓库域名>.extraheader` 在进程环境里注入的，**不会拼进 clone 地址，也不会被写进 `.git/config`**；作用域限定在目标域名，换镜像或跳转都带不走。
- 插件配置本身不加密，`git_token` 在配置文件里是**明文**；如果在意这一点，用环境变量。
- 私有仓库的内容 jsDelivr 取不到，所以启用令牌后插件会关掉直链模式：发图依赖本地克隆，本地文件缺失时改走 GitHub Contents API（带令牌）下载；`{url}` 占位符会变成 GitHub 网页地址。
- 已经填了 `repo_url` 的镜像地址照旧生效，令牌会按那个地址的域名注入。

也可以继续保持老办法：提前把仓库 clone 到 `data/plugin_data/astrbot_plugin_everything2hub/repo`，插件发现 `.git` 已存在就不会重新克隆。

## 开发

```bash
python -m pytest -q     # 单元测试：用本地 bare 仓库模拟上游，不联网、不需要 GitHub
python -m ruff check .  # 代码风格
```

- `git_client.py`：与 AstrBot 解耦的 git 层（clone / pull / ls-files / diff / 过滤 / 随机 / 索引缓存），可单独复用。
- `main.py`：事件处理与发送，只依赖 `git_client` 与 AstrBot API。

测试用 `tests/conftest.py` 里的 `make_upstream()` 现场造一个 bare 仓库当上游，因此 clone、pull、变化检测、发送回退这些路径都是真跑一遍的。

## 致谢与许可

- 图库模板：[PCL2Hub](https://github.com/PCL-Community/PCL2Hub)（仓库内容版权归各投稿者所有，本插件只做随机抽取与转发）。
- 直链兜底：[jsDelivr](https://www.jsdelivr.com/)。
- 插件框架：[AstrBot](https://github.com/AstrBotDevs/AstrBot)。

本仓库代码以 GNU AGPL-3.0 许可发布，见 [LICENSE](LICENSE)。
