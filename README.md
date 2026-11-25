# tg-notion-clipper

一个轻量级、可零维护部署在 Google Cloud Run 上的 **Telegram → AI 分类整理 → Notion 归档** 个人知识剪藏助手。

在 Telegram 给你的机器人发一条链接（可附带一句批注）或纯文本随手记，它会自动抓取正文、调用 Gemini Structured Outputs 按 Notion 中的动态分类规则生成标题、标签、摘要与核心要点，写入 Notion「归档总表」，并返回带 Notion 链接的即时回执。

---

## 核心特性

- **多平台智能抓取与多级降级**（[`router.py`](router.py)）
  - **X / Twitter**：FxTwitter API（支持正文、引用推文、X 长文 Article、图片）→ 通用读取链
  - **V2EX**：官方话题 & 评论 API（主帖 + 前 20 条评论）→ 通用读取链
  - **微信公众号**：模拟微信客户端直连解析（修复 `data-src` 懒加载、代码块行号错位、`wsrv.nl` 图片代理绕过防盗链、识别已删除/违规/小绿书）→ 通用多渠道兜底
  - **Reddit**：官方 OAuth（可选）→ 公开 JSON → 通用读取链
  - **通用网页**：优先消耗月度刷新免费额度的抓取链 **`Tavily Extract → Firecrawl → ZenRows → Jina Reader → 直连 HTML`**
- **Notion 动态分类配置**（[`category_config.py`](category_config.py)）
  - 单库架构（「归档总表」+「分类配置」表），在 Notion 里增删改分类名、描述、提取重点、标签池，无需改代码或重新部署，5 分钟自动生效（发 `/reload` 立即生效）
- **URL 预查重 & 幂等写入**（[`app.py`](app.py) & [`notion_writer.py`](notion_writer.py)）
  - 自动剥离追踪参数（`utm_*`、`fbclid`、微信会话参数等）并展开短链，在抓取和调用 LLM 前先查 Notion 去重，省时省钱
- **Gemini 结构化输出与容错**（[`gemini_processor.py`](gemini_processor.py)）
  - 使用 Enum 严格约束分类名；60 秒超时保护；遇到 429/503 自动重试并切换备用模型；抓取失败（`FALLBACK`）时禁止模型臆造正文
- **低成本 Serverless 部署**（[`deploy.ps1`](deploy.ps1) & [`Dockerfile`](Dockerfile)）
  - 按请求计费（`--cpu-throttling`），`min-instances=0` 空闲零费用；Secret Manager + Artifact Registry 清理策略 + 可选预算告警

---

## 架构概览

```text
Telegram (手机/桌面)
   │  HTTPS Webhook (X-Telegram-Bot-Api-Secret-Token 鉴权)
   ▼
Google Cloud Run · app.py (FastAPI)
   ├─ 用户白名单 & update_id 去重
   ├─ Notion URL 预查重（命中直接跳过抓取与 AI）
   ├─ router.py ............ 分平台抓取（Tavily → Firecrawl → ZenRows → Jina → 直连）
   ├─ gemini_processor.py .. Gemini 分类 / 标签 / 摘要 / 要点（读取 Notion 分类配置）
   └─ notion_writer.py ..... 写入 Notion「归档总表」（长文自动按 1800 字切片折叠）
   ▼
Telegram 回执：分类 / 标签 / 摘要 / 渠道 / Notion 页面链接
```

---

## 快速开始

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
- `GEMINI_API_KEY`：Google AI Studio API Key
- `NOTION_TOKEN`：Notion Internal Integration Token（并将目标父页面授权给该 Integration）
- `NOTION_PARENT_PAGE_ID`：用于自动创建「归档总表」与「分类配置」表的 Notion 父页面 ID

可选抓取渠道密钥（按月刷新免费额度优先）：
- `TAVILY_API_KEY`：Tavily Extract（免费版每月 1,000 credits，约 5,000 页）
- `FIRECRAWL_API_KEY`：Firecrawl（免费版每月 1,000 credits）
- `ZENROWS_API_KEY`：ZenRows（免费版每月 5,000 credits，支持 JS 渲染重试）
- `JINA_API_KEY`：Jina Reader（提升限速，作为兜底）

### 2. 初始化 Notion 数据库与 Telegram 白名单

```bash
# 一键创建 Notion「归档总表」和「分类配置」表并回填 .env
python setup_notion.py

# 给 Bot 发送任意消息后运行，自动获取并写入 ALLOWED_USER_ID
python setup_telegram.py
```

### 3. 本地测试与试跑

```bash
# 运行离线单元测试
python tests/test_offline.py

# 运行联网体检（验证各抓取渠道、Gemini、Notion 连通性）
python tests/live_check.py

# 本地长轮询试跑（无需公网域名）
python local_poll.py
```

### 4. 一键部署到 Google Cloud Run

前提：已安装 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) 并执行过 `gcloud auth login`，且 `.env` 中已填写 `GCP_PROJECT_ID`。

- **Windows**：双击 [`deploy.bat`](deploy.bat) 或运行 `powershell -ExecutionPolicy Bypass -File deploy.ps1`
- 部署脚本会自动完成：生成 `WEBHOOK_SECRET` → 启用 GCP API → 写入 Secret Manager → 构建部署 Cloud Run → 设置镜像清理策略 → 绑定 Telegram Webhook。

---

## Telegram 指令

| 指令 | 作用 |
| :--- | :--- |
| 发送任意链接 | 自动抓取、AI 分类整理并归档到 Notion |
| `链接 + 批注` | 批注会作为高优先级分类依据并写入 Notion Callout |
| 发送纯文本 | 作为 `Note`（随手记）自动生成标题、标签并归档 |
| `/categories` | 查看当前启用的分类列表 |
| `/reload` | 立即强制从 Notion「分类配置」表刷新分类 |
| `/id` | 查看当前 Telegram 数字 ID |
| `/help` | 查看使用说明 |

---

## 项目结构

| 文件 | 说明 |
| :--- | :--- |
| `app.py` | FastAPI 入口：Webhook 鉴权、白名单、去重、指令处理、结构化日志 |
| `router.py` | 多平台 URL 路由与降级抓取引擎 |
| `gemini_processor.py` | Gemini 结构化分类、摘要、要点与超时/重试处理 |
| `category_config.py` | 读取并缓存 Notion「分类配置」表（5 分钟 TTL，失败兜底 `rules.yaml`） |
| `notion_writer.py` | Notion REST API（`2025-09-03`）写入、URL 查重、长文切片 |
| `utils.py` | 纯函数工具：URL 提取/归一化、微信图片代理、Notion Block 组装 |
| `config.py` | 环境变量与 `.env` 加载 |
| `rules.yaml` | 初始分类种子与兜底规则 |
| `setup_notion.py` / `sync_categories.py` / `setup_telegram.py` | 初始化与分类同步脚本 |
| `local_poll.py` / `set_webhook.py` | 本地长轮询调试与 Webhook 管理 |
| `deploy.bat` / `deploy.ps1` / `Dockerfile` | Cloud Run 一键部署与容器配置 |
| `tests/` | 离线单元测试与联网体检脚本 |

## License

[MIT](LICENSE)
