# tg-notion-clipper

一个部署在 Google Cloud Run 上的 **Telegram → Gemini → Notion** 个人与多用户知识剪藏助手。

转发链接、文字、截图、相册、语音、视频或 PDF 到 Telegram Bot（或使用浏览器油猴脚本 / `/ingest` 接口），就能把素材、批注、标签、实体字段和结构化摘要归档到 Notion。遇到页面受限或 AI 暂不可用时，机器人保留可用的原始内容、附件和状态说明。

## 架构与功能概览

[![tg-notion-clipper V2.3 剪藏流程与上线状态](docs/preview.png)](https://wu0ming.github.io/tg-notion-clipper/)

[打开交互图](https://wu0ming.github.io/tg-notion-clipper/) · [备用预览](https://htmlpreview.github.io/?https://github.com/wu0ming/tg-notion-clipper/blob/main/docs/index.html)

| 状态 | 能力 | 说明 |
| :--- | :--- | :--- |
| 已部署可用 | Cloud Run 容器化运行时 | 内置 `ffmpeg`、`yt-dlp`、`youtube-transcript-api` 与 `cryptography`（`1Gi` 内存，按请求计费，自动缩容到零） |
| 已部署可用 | [#5 相册聚合与全图视觉识别](https://github.com/wu0ming/tg-notion-clipper/issues/5) | 支持静默窗口聚合、`#合并`/`#拆分`/`#独立` 分组、多图逐张视觉识别（含异步晚到补图独立识别）与 `ProjectKey` 跨来源合并 |
| 已部署可用 | [#2/#4 视频文件与视频链接流水线](https://github.com/wu0ming/tg-notion-clipper/issues/4) | Telegram 视频直接分析/超大自动 `ffmpeg` 抽帧；YouTube/B站优先免费读字幕，附带 `动作/拆解/步骤/截图` 等指令按需抽关键帧 |
| 已部署可用 | [#6 L0 路由 / Flex / 公私分流](https://github.com/wu0ming/tg-notion-clipper/issues/6) | 本地零开销路由模型层级；`GEMINI_SERVICE_TIER=flex` 支持 5 折推理及超时自动回退 `standard`；支持公私 Key 分流 |
| 已部署可用 | [#11 六类实体模板与 PDF 文本层](https://github.com/wu0ming/tg-notion-clipper/issues/11) | 支持开源项目、研究论文、软件工具、行业研报、视频教程与通用六类结构化实体字段；PDF 优先提取本地文本层 |
| 已部署可用 | [#12 交互纠偏、检索与无状态回退](https://github.com/wu0ming/tg-notion-clipper/issues/12) | 消息内联按钮、`#标签` 显式保留、`/find` / `/recent` / `/retag` / `/stats`，SQLite 热索引冷启动后自动回退 Notion 云端查询 |
| 按需配置 | 外部增强凭证与多用户模式（[#3](https://github.com/wu0ming/tg-notion-clipper/issues/3) / [#9](https://github.com/wu0ming/tg-notion-clipper/issues/9)） | `GITHUB_TOKEN`（GitHub Discussions GraphQL）、`REDDIT_CLIENT_*`（Reddit OAuth）、`USER_CONFIGS`（多用户隔离库）按需填写后由 `deploy.ps1` 自动加密打包同步 |

---

## 核心特性

- **多平台链接抓取与降级**（[`router.py`](router.py)）
  - **GitHub**：读取仓库、文件、分支、Issue、PR、Discussion 和 Release（保留完整原文，智能筛评精选评论送模型）。公开 REST 可匿名读取；私有库或 Discussions GraphQL 需要只读 `GITHUB_TOKEN`。
  - **X / Twitter & V2EX & 微信公众号**：FxTwitter API（含长文与引文）、V2EX 官方 API、微信文章客户端直连解析与 `wsrv.nl` 图片代理。
  - **Reddit**：官方 OAuth（可选）→ 公开 JSON → 通用抓取链。
  - **YouTube / Bilibili**：优先读取字幕、章节与简介（零视频下载）；用户明确要求动作/步骤/关键帧拆解时，按需调用容器内预装的 `yt-dlp` + `ffmpeg` 截帧图文整理。
  - **通用网页抓取链**：优先免费月度额度渠道 **`Tavily Extract → Firecrawl → ZenRows → Jina Reader（支持 JINA_TOKEN_BUDGET 预算）→ 直连 HTML → Gemini URL Context 联网兜底`**。
- **媒体与相册识别**（[`album_aggregator.py`](album_aggregator.py) & [`gemini_processor.py`](gemini_processor.py)）
  - 单图/多图/相册：对多图相册中的**每一张图片**都会执行独立的视觉识别与 OCR 提取，再做整体合并归类；即使因 Telegram Webhook 投递延迟导致第二张图晚到，也会自动补充视觉识别并更新 `InputType=Album`。
  - 语音/音频转写，PDF 优先提取纯文本层降本，视频（含 `>18MB` 超大视频 `ffmpeg` 抽帧）自动分析并上传原文件/视频块到 Notion（Bot 下载上限 20MB）。
- **来源隔离与个人批注可见性**（[`telegram_source.py`](telegram_source.py) & [`notion_writer.py`](notion_writer.py)）
  - 转发他人消息自动隔离「Telegram 转发原文」与「转发来源元数据」，防止他人文本伪造个人批注。
  - 直接发送消息/链接/媒体时：以 `批注：` 开头的文字显示在页面顶部 `📝 个人批注` 高亮块；未写 `批注：` 前缀的随附文字也会以顶部可见的 `💬 随附说明（来源未确认）` 高亮块展示，不会隐藏到折叠目录中。回复自己已归档的原消息发送 `批注：...` 可增量追加。
- **无状态 Serverless 持久化设计**（[`app.py`](app.py) & [`clip_store.py`](clip_store.py)）
  - Cloud Run 缩容到零或冷启动会清空临时 `/tmp` SQLite 缓存，但归档页以 Notion 数据库为准：`/find`、`/recent`、`/retag`、Telegram 内联按钮（`🔍 精读升级` / `🏷️ 改标签` / `📝 追加批注` / `🔄 重抓`）和晚到相册补充均支持直接从 Notion 实时查询与恢复。
- **Google Secret Manager 5 槽位安全同步**（[`deploy.ps1`](deploy.ps1) & [`config.py`](config.py)）
  - `deploy.ps1` 将核心 4 个密钥与 1 个合并加密 JSON 包 `CLIPPER_SECRETS` 存入 Secret Manager，始终控制在 **<= 5 个活跃版本（低于 GCP 免费层 6 个上限）**，不会把第三方 API Key 或多用户 Notion Token 暴露在明文环境变量 YAML 中。

---

## 快速配置与部署

### 1. 准备本地环境与核心配置

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

复制 [`.env.example`](.env.example) 为 `.env`，填写核心字段：

| 变量名 | 说明 | 获取方式 |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Telegram 机器人令牌 | [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USER_ID` | 允许使用机器人的 Telegram 数字 ID（多人英文逗号分隔） | 先运行 `setup_telegram.py` 或私聊 Bot 发 `/id` |
| `GEMINI_API_KEY` | Gemini API 密钥 | [Google AI Studio](https://aistudio.google.com/apikey) |
| `NOTION_TOKEN` | 主 Notion Internal Integration Token | [Notion Integrations](https://www.notion.so/my-integrations) |
| `NOTION_PARENT_PAGE_ID` | 已连接上述 Integration 的 Notion 父页面 ID/链接 | Notion 页面链接末尾 32 位字符 |
| `GCP_PROJECT_ID` | Google Cloud 项目 ID | [GCP Console](https://console.cloud.google.com/) |

初始化 Notion 数据库与 Telegram 白名单：

```bash
# 创建「归档总表」与「分类配置」并回填 .env（加 --with-inbox 可额外创建独立收件箱）
python setup_notion.py

# 若从旧版升级已有 Notion 归档表，无损补齐 EntityType / ProjectKey / AlbumKey 等新列与选项：
python setup_notion.py --upgrade

# 给 Bot 发送任意消息后运行，自动获取并写入 ALLOWED_USER_ID：
python setup_telegram.py
```

---

### 2. 一键部署到 Google Cloud Run

前提：已安装 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) 并执行过 `gcloud auth login`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File deploy.ps1
```

部署脚本会自动完成：
1. 生成或复用 `WEBHOOK_SECRET`
2. 启用 Cloud Run / Artifact Registry / Secret Manager / Cloud Build API
3. 将核心密钥与 `CLIPPER_SECRETS` 合并包写入 Secret Manager（总活跃版本 `<= 5/6`，保持免费额度）
4. 基于 [`Dockerfile`](Dockerfile) 构建含 `ffmpeg`、`yt-dlp`、`youtube-transcript-api`、`cryptography` 的容器（`1Gi` 内存、`min-instances=0`、`max-instances=1`）
5. 配置 Artifact Registry 镜像清理策略（保留最新 2 个版本防超 0.5GB 免费空间）及可选预算告警
6. 自动运行 [`set_webhook.py`](set_webhook.py) 绑定 Telegram Webhook。

---

### 3. 进阶配置指南

#### A. 多用户隔离配置（Multi-User Setup）

当团队、家人或多位朋友共用同一个机器人与 Cloud Run 实例时，可开启按 Telegram ID 隔离的独立 Notion 知识库、分类规则、预算上限与配额策略：

> **重要提醒**：一旦配置了 `USER_CONFIGS`，系统进入严格多用户模式，未知用户不会回退写入全局 `NOTION_DB_ARCHIVE`。**管理员本人也必须在 `USER_CONFIGS`（与 `ALLOWED_USER_ID`）中包含一条记录**。

1. **获取所有用户的 Telegram 数字 ID**：让每位用户私聊机器人发送 `/id`（新用户也可发 `/id` 获取自己的数字 ID）。
2. **创建并授权各用户的 Notion 数据库**：
   - **情况 1（同一 Notion 工作区、同一个 Integration）**：让新用户创建一个 Notion 页面并 Connect 你的 Integration，然后运行：
     ```bash
     python setup_notion.py --user-id 222222222 --parent-page <新用户页面ID> --with-inbox
     ```
     脚本会自动创建专属「归档总表」「分类配置」「归档收件箱」并打印 JSON 片段。
   - **情况 2（不同 Notion 工作区 / 各自独立的 Integration Token）**：先在 `.env` 里用新用户的 Token 临时运行 `setup_notion.py --user-id ...`，或对方直接提供其已建好的数据库 ID 和 Integration Token（`ntn_...`）。
3. **可选：使用 `CLIPPER_MASTER_KEY` 加密存储第三方 `notion_token`**：
   - 生成 Fernet 主密钥（如尚未生成）：
     ```bash
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
   - 写入 `.env` 的 `CLIPPER_MASTER_KEY=...` 后，加密各用户的 `notion_token`：
     ```bash
     python -c "from config import encrypt_secret; print(encrypt_secret('ntn_对方Token'))"
     ```
4. **配置 `ALLOWED_USER_ID` 与 `USER_CONFIGS`（或 `USER_CONFIGS_FILE`）**：
   - 在 `.env` 中列出全部允许的用户 ID：
     ```env
     ALLOWED_USER_ID=111111111,222222222
     ```
   - 编写 `USER_CONFIGS`（推荐写到 `.gitignore` 忽略的 `users.json` 后设 `USER_CONFIGS_FILE=users.json`，部署时 `deploy.ps1` 会自动读取并加密存入 Cloud Run `CLIPPER_SECRETS`）：
     ```json
     {
       "111111111": {
         "archive_db": "<主用户归档总表ID>",
         "config_db": "<主用户分类配置表ID>",
         "role": "admin",
         "monthly_budget_usd": 5.0,
         "quota_policy": "downgrade"
       },
       "222222222": {
         "archive_db": "<用户2归档总表ID>",
         "config_db": "<用户2分类配置表ID>",
         "inbox_db": "<可选收件箱ID>",
         "notion_token": "enc:gAAAAAB...",
         "role": "member",
         "monthly_budget_usd": 1.0,
         "quota_policy": "stop"
       }
     }
     ```
   - **字段说明**：
     - `notion_token`：省略时默认复用全局 `NOTION_TOKEN`；填写 `ntn_...` 或 `enc:...` 可实现跨工作区彻底隔离。
     - `monthly_budget_usd`：每月估算模型消费上限（达到 80% 时在回执中提醒）；设为 `0` 表示不限制。
     - `quota_policy`：`downgrade`（超额后关闭付费复杂模型回退/降级）或 `stop`（超额后暂停 AI，仅以 `AI_PENDING` 保存原始素材）。
5. **重新运行 `deploy.ps1`** 即可生效；同一套规则同时隔离 Telegram Webhook、浏览器 `/clipkey`、`/ingest`、`/find` 和 `mcp_server.py`。

#### B. 成本控制与抓取额度优化

| 变量名 | 建议值 | 作用 |
| :--- | :--- | :--- |
| `GEMINI_SERVICE_TIER` | `flex` | 开启 Gemini 5 折半价推理，超时或不可用时自动无缝回退 `standard` |
| `GEMINI_API_KEY_FREE` | 独立免费项目 Key | 仅对无批注、无私密媒体的纯公开链接走免费层 Key，降低付费账单；私密消息仍走 `GEMINI_API_KEY` |
| `JINA_TOKEN_BUDGET` | `6000` | 限制每次 Jina Reader 的输出 Token 预算，保护一次性额度 |
| `JINA_RETAIN_IMAGES` | `none`（默认 `all`） | 仅抓取纯文字文章时可设 `none` 节省流量与 Token |
| `TAVILY_API_KEY` / `FIRECRAWL_API_KEY` / `ZENROWS_API_KEY` | 免费版 Key | 按月刷新免费额度优先于一次性 Jina 额度使用 |

#### C. 启用 GitHub Discussions（可选外部只读 Token）

公开仓库、Issue、PR、Release 通过匿名 REST 读取；读取 GitHub Discussions（GraphQL）需要 `GITHUB_TOKEN`：
1. 在 [GitHub Fine-grained tokens](https://github.com/settings/personal-access-tokens/new) 创建**有有效期且仅勾选公开仓库/只读权限**的 Token。
2. 写入 `.env` 的 `GITHUB_TOKEN=github_pat_...`。
3. 本地运行只读检测：`python tests/live_check.py --github-discussion https://github.com/vercel/next.js/discussions/48110`，确认通过后运行 `deploy.ps1`（会安全打包入 `CLIPPER_SECRETS`，不额外占用 Secret 槽位）。

#### D. 视频分析参数与 Cloud Run 运行边界

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `VIDEO_MAX_KEYFRAMES` | `8` | 指令型视频步骤拆解单次最大截帧数 |
| `VIDEO_MAX_DURATION_SEC` | `1800` | 允许触发 `yt-dlp` 抽帧的链接视频最长时长（秒） |
| `VIDEO_INLINE_MAX_SECONDS` | `45` | 超过该时长的 Telegram 直传视频自动由容器 `ffmpeg` 抽帧分析，大幅降低视频输入 Token |
| `VIDEO_INLINE_MAX_BYTES` | `18874368` (18MB) | 超过该大小的 Telegram 视频自动由容器内 `ffmpeg` 抽帧分析，避免整片传输超限 |

- **多模态 Token 降本策略**：视频直传与相册二次综合默认启用 `MEDIA_RESOLUTION_LOW`（约降低 65%–76% 媒体输入 Token），单图/相册逐图 OCR 使用 `MEDIA_RESOLUTION_MEDIUM`（约降低 51%），普通短视频默认走 `gemini-3.5-flash-lite` + `flex` 推理；仅明确带 `精读` / `深挖` / `步骤` / `拆解` 等指令或大文件才升级复杂模型与更高分辨率。

- **YouTube IP 限制提示**：Cloud Run 出口为 Google 机房 IP，无需下载的视频字幕抓取通常可正常获取，但若触发 YouTube 对 `yt-dlp` 的机房人机验证，管线会自动以 `PARTIAL` 状态保留可用字幕与时间戳章节。B站公开视频与 Telegram 本地上传视频不受该限制。
- **网页提取提示**：Cloud Run 镜像无需配置 Node.js / `DEFUDDLE_ENABLED`，内置 6 级 Python 提取链已经完整覆盖。

#### E. 浏览器剪藏、统一 `/ingest` 与本地辅助工具

- **浏览器脚本剪藏（Tampermonkey）**：安装 [`browser/tg-notion-clipper.user.js`](browser/tg-notion-clipper.user.js)，在 Telegram 私聊 Bot 发送 `/clipkey` 获取 180 天签名配对码并填入脚本；自部署需修改脚本 `endpoint` 与 `@connect` 为你的 Cloud Run 域名。
- **统一 `/ingest` 与任务队列**：任何外部客户端（快捷指令、RSS 触发器）可携带 `Authorization: Bearer <配对码>` 或 `X-Ingest-Secret: <INGEST_SECRET>`（未设时回退 `WEBHOOK_SECRET`）调用 `POST /ingest`，支持 `async_queue=true` 入队及 `POST /jobs/run` 消费。
- **本地离线工具**（操作本地 SQLite `data/clipper.db` 或导出目录）：
  - `python mcp_server.py`：面向 Claude Desktop / Cursor / Antigravity 的隔离型 MCP stdio 检索与追加批注服务
  - `python markdown_mirror.py --user-id <TG_ID> --out ./mirror`：增量导出本地 Obsidian / Markdown 双链镜像
  - `python link_checker.py --user-id <TG_ID>`：巡检历史归档链接可用性并保存本地快照

---

## Telegram 指令与状态

| 指令 | 作用 |
| :--- | :--- |
| 发送任意链接 | 自动抓取、实体提取、AI 分类并归档到 Notion（附快捷按钮） |
| `链接/附件 + 文字说明`（本人直发） | 自动识别为本人直发，无论是否带 `批注：` 前缀，均以顶部黄色高亮 `📝 个人批注` Callout 强调录入并高优指导分类；转发消息仍严格隔离在 `Telegram 转发原文` / `Telegram 转发来源` |
| 回复自己已归档原消息发 `批注：内容` | 增量追加到原 Notion 页面（依赖归档表 `TelegramKey` 列） |
| 发送图片/相册 | 逐张执行视觉识别；相册支持 `#合并` / `#拆分` / `#独立` 智能分组 |
| 发送视频或视频链接 | 视频文件直传多模态或超大抽帧；视频链接附 `步骤`/`拆解`/`截图` 等触发关键帧图文步骤 |
| 发送 PDF / 语音 / 纯文本 | PDF 优先提取文本层，语音自动转写，纯文本按 `Note` 归档 |
| `#标签` | 优先保留指定标签 |
| `/find 关键词` · `/recent [N]` | 检索本地 FTS5 索引；Cloud Run 冷启动后自动回退 Notion 云端检索 |
| `/retag <page_id> 标签...` | 修改归档页面标签并记录纠偏建议（同样支持冷启动云端回退） |
| `/stats` | 查看近 7 天归档统计、模型 Token 与估算费用、高频标签纠偏建议 |
| `/categories` · `/reload` | 查看当前分类或立即刷新 Notion「分类配置」 |
| `/clipkey` · `/id` · `/help` | 获取浏览器剪藏配对码、查看本人数字 ID、查看帮助 |

---

## 本地测试

```bash
# 运行离线单元测试（Mock 全部外部请求）
python -m unittest discover -s tests -p "test_*.py" -v

# 运行联网体检（验证实际抓取渠道、Gemini、Notion 连通性）
python tests/live_check.py
```

---

## 项目结构

| 文件 | 说明 |
| :--- | :--- |
| `app.py` | FastAPI 入口：Webhook 鉴权、多用户上下文、`/clip`、`/ingest`、`/jobs/run`、指令与 Notion 云端回退 |
| `album_aggregator.py` | Telegram 相册静默窗口聚合与知识单元智能拆分 |
| `video_pipeline.py` | 共享 `VideoAsset` 管线：字幕优先与 `yt-dlp`/`ffmpeg` 关键帧图文步骤 |
| `clip_store.py` | SQLite WAL 本地索引（FTS5 检索、相册状态、用量/额度、纠偏记录、持久任务队列） |
| `router.py` | 多平台 URL 路由与降级抓取引擎 |
| `gemini_processor.py` | Gemini 结构化分类、L0/Flex/公私分流路由、逐图相册视觉识别与六类实体模板 |
| `notion_writer.py` | Notion REST API（`2025-09-03`）读写、高亮批注、多附件/视频上传、100-block 分片与云端查询回退 |
| `telegram_media.py` / `telegram_source.py` | Telegram 媒体识别/流式下载（含 PDF 文本层）与转发来源隔离 |
| `sinks.py` / `link_checker.py` / `markdown_mirror.py` / `mcp_server.py` | 写入抽象、防死链巡检、Obsidian 镜像导出与 MCP 服务 |
| `config.py` / `utils.py` / `category_config.py` | 配置及 `CLIPPER_SECRETS_JSON` 加载、多用户上下文、纯函数工具与动态分类缓存 |
| `setup_notion.py` / `sync_categories.py` / `setup_telegram.py` | 初始化建库、`--upgrade` 升级与多用户 `--user-id` 配置工具 |
| `deploy.bat` / `deploy.ps1` / `Dockerfile` | Cloud Run 容器镜像构建（含 `ffmpeg` / `yt-dlp`）与 5 槽位 Secret Manager 部署 |

## License

本项目自本次许可变更起，对项目自身有权授权的代码采用 [PolyForm Noncommercial License 1.0.0](LICENSE)（源码公开，仅限非商业用途）。

- 允许个人非商业使用、学习、研究、修改与按许可证分发。
- 未获得权利人另行授权，不得用于商业用途，包括商业产品、收费服务及商业经营中的内部使用（许可证明确允许的机构及用途除外）。商业授权请通过本仓库联系维护者。
- 第三方组件继续适用各自的许可证，包括浏览器剪藏脚本内的 MIT 和 Apache-2.0 组件；原版权与许可声明不变。
- 以前已按 MIT 发布的版本所获得的权利不受追溯影响，见 [历史 MIT 声明](docs/legacy-mit-notice.txt)。

The project-owned code is source-available under [PolyForm Noncommercial 1.0.0](LICENSE). Commercial use requires separate authorization except as permitted by the license. Third-party licenses and previously granted MIT rights remain unaffected. This is not an OSI-approved open-source license.
