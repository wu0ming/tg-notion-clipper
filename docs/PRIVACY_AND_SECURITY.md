# 项目隐私与安全规范（公开仓库发布准则）

> **适用范围**：`tg-notion-clipper` 公开 Git 仓库、`deploy.ps1` 容器构建上下文及所有测试/文档文件。  
> **核心原则**：本项目代码与通用文档对公网开放，**任何真实密钥、真实部署标识、个人账号信息及私人剪藏内容不得进入 Git 跟踪文件或提交历史**。

---

## 一、禁止入库的信息分级清单

### 1. 严禁入库（高危凭证 · P0）
一旦进入提交历史必须立即轮换/吊销并重写 Git 历史：
- **API 密钥与令牌**：`TELEGRAM_BOT_TOKEN`、`GEMINI_API_KEY`、`GEMINI_API_KEY_FREE`、`NOTION_TOKEN`、`GITHUB_TOKEN`（`github_pat_*` / `ghp_*`）、`TAVILY_API_KEY`、`FIRECRAWL_API_KEY`、`ZENROWS_API_KEY`、`JINA_API_KEY`、`REDDIT_CLIENT_SECRET`
- **加密主密钥与签名密钥**：`WEBHOOK_SECRET`、`INGEST_SECRET`、`BROWSER_CLIP_SECRET`、`CLIPPER_MASTER_KEY`、Fernet 密文（`enc:gAAAAA...`）
- **用户私密数据库映射**：含真实用户数字 ID、Token 或数据库 ID 的 `USER_CONFIGS` / `users.json`

### 2. 禁止进入公开仓库（个人部署标识与隐私数据 · P1）
应仅存于本地 `.env` 或被 `.gitignore` 排除的本地文档中：
- **个人身份与账号标识**：真实 Telegram 数字 ID（`ALLOWED_USER_ID`）、个人邮箱、手机号、家庭住址、私有群/频道链接
- **真实云端与数据库标识**：
  - 真实 GCP 项目 ID（如 `gen-lang-client-*`）、项目编号、结算账号 ID
  - 个人专属 Cloud Run 服务域名（例如 `https://<service>-<hash>-uc.a.run.app`；公开脚本如 `browser/tg-notion-clipper.user.js` 必须使用占位域名 `your-cloud-run-service.run.app`）
  - 真实 Notion 页面/数据库 ID（`NOTION_PARENT_PAGE_ID`、`NOTION_DB_ARCHIVE`、`NOTION_DB_CONFIG`、`NOTION_DB_INBOX`、`NOTION_PAGE_TEMPLATE` 及任何实测剪藏页 UUID）
- **真实个人剪藏原文与测试日志**：用户的真实备忘、批注原文、图片/视频真实识别日志

---

## 二、文件隔离与脱敏规则

1. **本地专属文档强制 `.gitignore`**：
   - 个人部署进度、差距分析、实测页面链接记录统一放在被 `.gitignore` 忽略的文件中（如 `docs/方案与进度.md`、`docs/PROGRESS_AND_GAP.md`、`docs/方案总览.html`），**只保留在本地项目文件夹**，禁止 `git add` 进公开仓库。
2. **多用户配置单独文件**：
   - 团队/多用户配置写入 `.gitignore` 忽略的 `users.json`，通过 `.env` 的 `USER_CONFIGS_FILE=users.json` 由 `deploy.ps1` 加密打包进 GCP Secret Manager（`CLIPPER_SECRETS`），不得写入 `README.md` 或源码。
3. **测试与示例必须使用伪造数据（Mock/Placeholder）**：
   - 单元测试（`tests/test_*.py`）不得引用真实 Notion Page UUID、真实 Telegram ID 或用户真实剪藏语句，统一使用 `mockpage000000000000000000000001`、`111`/`222`、`https://example.org/...` 等虚构占位值。
   - 用户端脚本与配置模板（`browser/tg-notion-clipper.user.js`、`.env.example`）一律留空或使用占位域名 `your-cloud-run-service.run.app`。
4. **容器与构建上下文隔离**：
   - 保持 `.gcloudignore` 与 `.dockerignore` 排除 `.env`、`users.json`、`data/`、`*.db*`、`docs/`、`tests/`，只将运行所需的纯代码文件显式列在 `Dockerfile` 的 `COPY` 中。

---

## 三、提交与推送前自检命令

每次准备 `git commit` / `git push` 之前，确认：
1. `git status --short` 中没有 `.env`、`users.json`、`docs/PROGRESS_AND_GAP.md`、`docs/方案与进度.md` 或 `data/*.db`。
2. 暂存区无密钥或真实 ID 特征：
   ```powershell
   git diff --cached | Select-String -Pattern "AIza|AQ\.|github_pat_|ntn_|tvly-|fc-|jina_|gAAAAA|tjbkbd|gen-lang-client|42[256]bbbaf"
   ```
   若有任何命中，**立即中止提交并脱敏**；若已误推至公开历史，必须立即轮换对应密钥并使用历史清理工具抹除后强推。
