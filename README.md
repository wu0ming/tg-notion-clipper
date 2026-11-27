# tg-notion-clipper

一个可部署在 Google Cloud Run 上的 **Telegram → Gemini → Notion** 个人知识剪藏助手。

转发链接、文字、截图、语音或 PDF 到 Telegram Bot，就能把素材、批注、标签和摘要归档到 Notion。遇到页面受限或 AI 暂不可用时，机器人保留可用的原始内容和错误说明，供后续整理。

## V2.3 架构与上线状态

[![tg-notion-clipper V2.3 剪藏流程与上线状态](docs/preview.png)](https://wu0ming.github.io/tg-notion-clipper/)

[打开交互图](https://wu0ming.github.io/tg-notion-clipper/) · [备用预览](https://htmlpreview.github.io/?https://github.com/wu0ming/tg-notion-clipper/blob/main/docs/index.html) · [生产发布提交](https://github.com/wu0ming/tg-notion-clipper/commit/771e0a5c52c4462b6b24d38f54c61716009e9ded)

2026-11-27，首批 V2.3 改动部署到 Cloud Run，已验证健康检查、带密钥的 Telegram Webhook、Notion 库读取和 Flash-Lite 用量返回。代码合并回归通过 82 项离线测试。真实 Telegram 消息写入隔离 Notion 库的全链路验收仍待完成。

| 状态 | 能力 | 下一步 |
| --- | --- | --- |
| 已实现（待线上实测） | [#5 相册聚合、知识单元拆分与跨来源增量合并](https://github.com/wu0ming/tg-notion-clipper/issues/5)、[#2/#4 视频文件与视频链接按需拆解管线](https://github.com/wu0ming/tg-notion-clipper/issues/4)、[#6 L0/Flex/公私分流路由](https://github.com/wu0ming/tg-notion-clipper/issues/6)、[#11 六类实体模板](https://github.com/wu0ming/tg-notion-clipper/issues/11)、[#12 交互纠偏/FTS5/防死链/MCP/Markdown 镜像](https://github.com/wu0ming/tg-notion-clipper/issues/12)、[#3/#13 多用户隔离、持久队列与统一 `/ingest`](https://github.com/wu0ming/tg-notion-clipper/issues/13) | 完成云端端到端实测、挂载只读 `GITHUB_TOKEN` 与按需配置 `USER_CONFIGS` / `yt-dlp` + `ffmpeg` |
| 待配置 | GitHub Discussions 官方 GraphQL 路由已部署，当前服务尚未挂载只读 `GITHUB_TOKEN` | [启用并验证 Discussions](#启用-github-discussions) |
| 可选配置 | Jina 预算可通过 `JINA_TOKEN_BUDGET` 启用；`GEMINI_SERVICE_TIER=flex` 启用半价推理；`GEMINI_API_KEY_FREE` 启用纯公开链接免费分流 | 对照成本、延迟和提取质量 |

## 核心特性

- **链接抓取**（[`router.py`](router.py)）
  - **GitHub**：读取仓库、文件、分支、Issue、PR、Discussion 和 Release。归档保留可取得的原文和评论链接，模型使用筛选后的重点评论。公开 REST 接口可匿名读取，Discussion 的 GraphQL 接口需要 `GITHUB_TOKEN`。
  - **X / Twitter**：FxTwitter API（支持正文、引用推文、X 长文 Article、图片）→ 通用读取链
  - **V2EX**：官方话题 & 评论 API（主帖 + 前 20 条评论）→ 通用读取链
  - **微信公众号**：模拟微信客户端直连解析（修复 `data-src` 懒加载、代码块行号错位、`wsrv.nl` 图片代理绕过防盗链、识别已删除/违规/小绿书）→ 通用多渠道兜底
  - **Reddit**：官方 OAuth（可选）→ 公开 JSON → 通用读取链
  - **通用网页**：优先消耗月度刷新免费额度的抓取链 **`Tavily Extract → Firecrawl → ZenRows → Jina Reader → 直连 HTML`**
  - **Gemini 联网**：常规渠道耗尽后，由 URL Context 读取原始链接。只有 Gemini 确认读取成功时才归档其提取结果，无法核实的链接进入收件箱。
- **媒体归档**（[`telegram_media.py`](telegram_media.py)）
  - 图片识别文字，语音和音频转写，PDF 提取文字与要点。Telegram Bot API 下载上限为 20 MB，Notion 能接收的附件大小取决于工作区套餐。
- **来源与个人批注**（[`telegram_source.py`](telegram_source.py)）
  - 保存转发来源和原始配文。直接发送链接或媒体时，可用「批注：」标记个人批注，普通附言显示为「随附说明」。回复同一聊天内自己已归档的原消息，也可用「批注：」追加到已有页面。
- **Notion 动态分类配置**（[`category_config.py`](category_config.py)）
  - 在「分类配置」表中编辑分类和建议标签，5 分钟内刷新，也可以发 `/reload` 立即刷新。建议标签按语义匹配，消息里手动写的 `#标签` 优先保留。
- **URL 预查重 & 幂等写入**（[`app.py`](app.py) & [`notion_writer.py`](notion_writer.py)）
  - 纯链接在抓取和分类前查询 Notion，同一实例内的并发纯链接按规范化 URL 去重。带新说明或转发原文的同链接按消息处理，暂时可能建独立页。跨来源合并与相册聚合仍在 [#5](https://github.com/wu0ming/tg-notion-clipper/issues/5) 跟踪。
- **Gemini 分析与留档**（[`gemini_processor.py`](gemini_processor.py)）
  - 普通剪藏优先走 Flash-Lite，精读和较大 PDF 可优先用 Flash。普通正文送模型时保留约 5,500 字的首尾，归档按照原文写入规则处理。分析仍不可用时，以 `AI_PENDING` 状态保存原始素材。
- **逐条进度**（[`app.py`](app.py)）
  - 每条消息显示处理状态，最终回执引用原消息并附 Notion 链接。
- **低成本 Serverless 部署**（[`deploy.ps1`](deploy.ps1) & [`Dockerfile`](Dockerfile)）
  - 按请求计费，支持缩容到零。部署脚本支持 Secret Manager 和镜像清理策略。

---

## 快速开始

### 浏览器剪藏

1. 在 Chrome 或 Vivaldi 的 Tampermonkey 中打开[安装脚本](https://raw.githubusercontent.com/wu0ming/tg-notion-clipper/main/browser/tg-notion-clipper.user.js)。
2. 私聊 Bot 并发送 `/clipkey`。在脚本菜单选择「配置配对码」，粘贴 Bot 返回的配对码。配对码有效期为 180 天。
3. 打开网页，点右下角「剪藏」选择整页正文或选区。可以编辑批注，也可以多次选择段落并点「保存选区批注」，最后随整页发送。油猴菜单还有「一键剪藏整页」和「一键剪藏选区」。

浏览器把 Markdown、原始地址和批注发送到 Cloud Run 的 `POST /clip`。Notion 父页保留来源、摘要和批注，完整文章排版在可点击的「剪藏原文」子页面。浏览器也可以独立下载 Markdown。脚本配对码只用于提交剪藏。

自部署时，将脚本的 `endpoint` 和 `@connect` 修改为自己的服务域名。现有 Notion 归档表可增加 `ClipKey` 文本列，同一网页可以保存不同选区，完全相同的剪藏可查重。单次提交支持 18 万字符 Markdown 和 8,000 字符批注，JSON 总大小上限为 480 KiB。

脚本内嵌了 [MarkDownload](https://github.com/deathau/markdownload/tree/7e8cc1a2156a5b413db133677641c3f8a23cd39d) 使用的 Mozilla Readability、Turndown 和 GFM 扩展。依赖版本和许可证保留在脚本中。

### 1. 准备环境与密钥

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，填写核心配置：
- `TELEGRAM_BOT_TOKEN`：通过 `@BotFather` 创建 Bot 获取
- `GEMINI_API_KEY`：Google AI Studio API Key，开启摘要、分类、联网和多模态分析。留空时仍可归档可用的原始内容。
- 普通剪藏默认用 `gemini-3.5-flash-lite`。较大的 PDF、视频及以「精读」或「深挖」发起的请求优先用备用模型，可用 `GEMINI_COMPLEX_MODEL` 覆盖。现有部署显式设置的 `GEMINI_MODEL` 仍优先于默认值。
- `gemini_usage` 日志记录每次调用的模型、耗时和 API 返回的 Token 用量，便于上线后比较成本。
- `NOTION_TOKEN`：Notion Internal Integration Token（并将目标父页面授权给该 Integration）
- `NOTION_PARENT_PAGE_ID`：用于自动创建「归档总表」与「分类配置」表的 Notion 父页面 ID

可选抓取渠道密钥（按月刷新免费额度优先）：
- `TAVILY_API_KEY`：Tavily Extract（免费版每月 1,000 credits，约 5,000 页）
- `FIRECRAWL_API_KEY`：Firecrawl（免费版每月 1,000 credits）
- `ZENROWS_API_KEY`：ZenRows（免费版每月 5,000 credits，支持 JS 渲染重试）
- `JINA_API_KEY`：Jina Reader（提升限速，作为兜底）
- `JINA_TOKEN_BUDGET`：可选，建议从 6000 开始对照测试。超出预算时 Jina 返回失败并继续尝试直连，页面图片链接随正文保留。
- 本地可先运行 `npm install --no-save --package-lock=false defuddle linkedom`，再设置 `DEFUDDLE_ENABLED=1`。通用网页先尝试本地提取，失败或提取内容过短时走现有抓取链。默认 Cloud Run 镜像只包含 Python。
- `GITHUB_TOKEN`：GitHub 只读令牌，可提高公开 API 请求配额，并启用令牌有权读取的 Discussions。

### 2. 初始化 Notion 数据库与 Telegram 白名单

```bash
# 一键创建 Notion「归档总表」和「分类配置」表并回填 .env
python setup_notion.py

# 给 Bot 发送任意消息后运行，自动获取并写入 ALLOWED_USER_ID
python setup_telegram.py
```

已部署的实例可继续使用原有 Notion 数据库。可以手动添加以下可选字段，或在新工作区运行 `python setup_notion.py --with-inbox` 创建相应结构。

| 设置 | 用途 |
| --- | --- |
| `NOTION_DB_INBOX` | 可选收件箱，采用与归档总表相同的列结构。留空时，失败素材进入归档总表。 |
| `InputType`、`FetchTier`（单选） | 在归档页表格中展示输入类型与抓取层级。 |
| `TelegramKey`（文本） | 保存 Telegram 消息与 Notion 页的映射，用于幂等写入与回复追加批注。 |
| `ClipKey`（文本） | 浏览器剪藏的内容去重。同一网页的不同选区仍可分别保存。 |
| `建议标签`（多选或文本） | 在「分类配置」中配置命中该分类时优先参考的标签。`GLOBAL_SUGGESTED_TAGS` 可设置跨分类标签。 |

### 3. 本地测试与试跑

```bash
# 运行离线单元测试
python -m unittest discover -s tests -p 'test_*.py' -v

# 运行联网体检（验证各抓取渠道、Gemini、Notion 连通性）
python tests/live_check.py

# 本地长轮询试跑（无需公网域名）
python local_poll.py
```

### 4. 一键部署到 Google Cloud Run

前提：已安装 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) 并执行过 `gcloud auth login`，且 `.env` 中已填写 `GCP_PROJECT_ID`。

- **Windows**：双击 [`deploy.bat`](deploy.bat) 或运行 `powershell -ExecutionPolicy Bypass -File deploy.ps1`
- 部署脚本会自动完成：生成 `WEBHOOK_SECRET` → 启用 GCP API → 写入 Secret Manager → 构建部署 Cloud Run → 设置镜像清理策略 → 绑定 Telegram Webhook。

### 启用 GitHub Discussions

公开仓库、Issue、PR、Release 的 REST 接口可以匿名使用。仓库 Discussions 路由（`github.com/owner/repo/discussions/数字`）通过 GitHub GraphQL 读取主帖、已标记答案和评论，需要 `GITHUB_TOKEN`。没有可用令牌时走现有网页抓取回退。

1. 在 [GitHub Fine-grained tokens](https://github.com/settings/personal-access-tokens/new) 创建有期限的只读令牌。仅剪藏公开仓库可先用公开仓库读取权限。需要访问特定仓库时，按实际归属限定仓库范围，并只授予所需的读取权限。如果 GitHub 返回权限不足，按 API 提示核对权限。 [GitHub GraphQL 鉴权说明](https://docs.github.com/en/graphql/guides/forming-calls-with-graphql)
2. 在该 GCP 项目的 Secret Manager 创建 `GITHUB_TOKEN`，只在该页面的密钥值字段粘贴令牌。将 `roles/secretmanager.secretAccessor` 授予运行剪藏服务的服务账号，权限范围设在这一条 Secret 上。 [Cloud Run 密钥配置说明](https://docs.cloud.google.com/run/docs/configuring/services/secrets)
3. 把指定 Secret 版本映射为 Cloud Run 的 `GITHUB_TOKEN` 环境变量，先发 0% 正式流量的候选修订版。验证主帖、答案或评论，以及实际抓取渠道后，再切换流量。

也可在本地 `.env` 配好同一令牌并运行以下只读检查。它只调用 GitHub 官方接口，失败返回非零退出码。

```bash
python tests/live_check.py --github-discussion https://github.com/vercel/next.js/discussions/48110
```

---

## Telegram 指令

| 指令 | 作用 |
| :--- | :--- |
| 发送任意链接 | 自动抓取、AI 分类整理并归档到 Notion |
| `链接 + 批注：你的内容` | 按个人批注归档。未标记的附言保存在「随附说明」 |
| 回复自己已归档的原消息，发送 `批注：你的内容` | 追加到原 Notion 页面。归档总表和独立收件箱需要 `TelegramKey` 文本列 |
| 发送纯文本 | 作为 `Note`（随手记）自动生成标题、标签并归档 |
| 发送图片、相册、语音、音频、视频或 PDF | 识别文字/转写/画面，相册支持 `#合并` / `#拆分` 智能分组，保存批注和可上传的原附件 |
| `#标签` | 将指定标签优先加入归档 |
| `/find 关键词` | 使用本地 SQLite FTS5 检索标题、标签、摘要与 Notion 链接 |
| `/recent [数量]` | 查看最近归档条目 |
| `/stats` | 查看本周归档统计、模型 Token 与估算费用、标签纠偏建议 |
| `/retag <page_id> 标签...` | 修改归档页面标签并记录纠偏建议 |
| `/categories` | 查看当前启用的分类列表 |
| `/reload` | 立即强制从 Notion「分类配置」表刷新分类 |
| `/clipkey` | 私聊获取浏览器剪藏配对码 |
| `/id` | 查看当前 Telegram 数字 ID |
| `/help` | 查看使用说明 |

| 原消息表情 | 处理状态 |
| --- | --- |
| 👀 | 已收到，正在处理 |
| 👌 | 已归档 |
| 👍 | 已收录，跳过重复写入 |
| 🤔 | 使用联网补救或已降级留档 |
| 👎 | 归档失败，可重发原消息 |

---

## 项目结构

| 文件 | 说明 |
| :--- | :--- |
| `app.py` | FastAPI 入口：Webhook 鉴权、白名单、去重、指令与回调、`/clip`、统一 `/ingest` 与 `/jobs/run` |
| `album_aggregator.py` | Telegram 图文相册静默窗口聚合与规则/轻量多模态知识单元拆分 |
| `video_pipeline.py` | 共享 `VideoAsset` 管线：YouTube/B站字幕优先与按需 `yt-dlp`/`ffmpeg` 关键帧图文步骤 |
| `clip_store.py` | SQLite WAL 本地索引（FTS5 检索、相册状态、用量/额度、纠偏记录、持久任务队列） |
| `sinks.py` | `BaseSink` 写入端抽象与 `NotionSink` |
| `link_checker.py` / `markdown_mirror.py` / `mcp_server.py` | 增量防死链巡检与公开快照、增量 Markdown 镜像、租户隔离 MCP stdio 服务 |
| `router.py` | 多平台 URL 路由与降级抓取引擎 |
| `gemini_processor.py` | Gemini 结构化分类、L0/L1/L2 路由、Flex/公私分流、六类实体字段与多模态分析 |
| `telegram_media.py` | Telegram 附件（含视频与 PDF 文本层）识别与有大小限制的流式下载 |
| `telegram_source.py` | 转发来源、原文与个人批注的识别 |
| `category_config.py` | 读取并缓存 Notion「分类配置」表（按用户库隔离，支持 `rules.yaml` 热加载） |
| `notion_writer.py` | Notion REST API（`2025-09-03`）写入、多附件/视频上传、100-block 分片与跨来源增量合并 |
| `utils.py` | 纯函数工具：URL 提取/归一化、项目主键提取、微信图片代理、六类实体模板组装 |
| `config.py` | 环境变量与 `.env` 加载 |
| `rules.yaml` | 初始分类种子与兜底规则 |
| `setup_notion.py` / `sync_categories.py` / `setup_telegram.py` | 初始化与分类同步脚本 |
| `local_poll.py` / `set_webhook.py` | 本地长轮询调试与 Webhook 管理 |
| `deploy.bat` / `deploy.ps1` / `Dockerfile` | Cloud Run 一键部署与容器配置 |
| `tests/` | 离线单元测试与联网体检脚本 |

## License

[MIT](LICENSE)
