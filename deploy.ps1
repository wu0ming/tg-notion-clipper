# tg-notion-clipper 一键部署到 Google Cloud Run（Windows PowerShell 5.1 / 7 均可）
# 用法：双击 deploy.bat，或在本目录执行 powershell -ExecutionPolicy Bypass -File deploy.ps1
# 步骤：读取 .env → 生成 Webhook 密钥 → 启用 API → 写入 Secret Manager（5/6 免费额度内）
#       → 授权服务账号 → 部署（1Gi 内存、含 ffmpeg/yt-dlp）→ 镜像清理策略 → 绑定 Telegram Webhook → 预算告警
param([switch]$NoPause)

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$Service = "tg-notion-clipper"

function Fail($msg) { Write-Host "`n[失败] $msg" -ForegroundColor Red; if (-not $NoPause) { Read-Host "按回车退出" }; exit 1 }
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
# 注意：PowerShell 内置别名 gc = Get-Content 优先级高于同名函数，不能命名为 Gc
function Run-Gcloud { & gcloud @args; if ($LASTEXITCODE -ne 0) { Fail "gcloud $($args -join ' ')" } }

function Sync-Secret($name, $value) {
    $tmp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($tmp, $value, (New-Object Text.UTF8Encoding $false))   # 无换行、无 BOM
        & gcloud secrets describe $name --project $Project --format="value(name)" 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            Run-Gcloud secrets create $name --project $Project --replication-policy automatic --data-file $tmp
            Write-Host "  创建 $name"
        } else {
            $current = (& gcloud secrets versions access latest --secret $name --project $Project 2>$null) -join "`n"
            if ($current -ne $value) {
                Run-Gcloud secrets versions add $name --project $Project --data-file $tmp
                $versions = & gcloud secrets versions list $name --project $Project --filter="state=enabled" --sort-by="~createTime" --format="value(name.basename())"
                $versions | Select-Object -Skip 1 | ForEach-Object { & gcloud secrets versions destroy $_ --secret $name --project $Project --quiet 1>$null }
                Write-Host "  更新 $name（旧版本已销毁）"
            } else { Write-Host "  $name 未变化，跳过" }
        }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

# ---------- 1. 读取 .env ----------
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Fail "已生成 .env，请先填写后再运行" }
$cfg = @{}
Get-Content ".env" -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
}
foreach ($k in "TELEGRAM_BOT_TOKEN","ALLOWED_USER_ID","NOTION_TOKEN","GCP_PROJECT_ID") {
    if (-not $cfg[$k]) { Fail ".env 缺少 $k" }
}
$Project = $cfg["GCP_PROJECT_ID"]
$Region = if ($cfg["GCP_REGION"]) { $cfg["GCP_REGION"] } else { "us-central1" }
$Python = if (Test-Path "$PSScriptRoot\.venv\Scripts\python.exe") { "$PSScriptRoot\.venv\Scripts\python.exe" } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { $null }

foreach ($k in "NOTION_DB_ARCHIVE","NOTION_DB_CONFIG") {
    if (-not $cfg[$k]) {
        if ($cfg["NOTION_PARENT_PAGE_ID"] -and $Python) {
            Step "Notion 数据库未配置，自动创建"
            & $Python setup_notion.py; if ($LASTEXITCODE -ne 0) { Fail "setup_notion.py 运行失败" }
            Get-Content ".env" -Encoding UTF8 | ForEach-Object {
                if ($_ -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
            }
            break
        } else { Fail ".env 缺少 $k（或填写 NOTION_PARENT_PAGE_ID 让脚本自动建库）" }
    }
}

# ---------- 2. Webhook 密钥 ----------
if (-not $cfg["WEBHOOK_SECRET"]) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $cfg["WEBHOOK_SECRET"] = ([Convert]::ToBase64String($bytes)) -replace '[+/=]', ''
    $raw = [IO.File]::ReadAllText("$PSScriptRoot\.env")
    $raw = [regex]::Replace($raw, '(?m)^WEBHOOK_SECRET=.*$', "WEBHOOK_SECRET=$($cfg['WEBHOOK_SECRET'])")
    if ($raw -notmatch '(?m)^WEBHOOK_SECRET=') { $raw = $raw.TrimEnd() + "`r`nWEBHOOK_SECRET=$($cfg['WEBHOOK_SECRET'])`r`n" }
    [IO.File]::WriteAllText("$PSScriptRoot\.env", $raw, (New-Object Text.UTF8Encoding $false))
    Write-Host "已生成 WEBHOOK_SECRET 并写入 .env"
}

# ---------- 3. gcloud 环境 ----------
Step "检查 gcloud"
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    foreach ($dir in "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
                     "${env:ProgramFiles(x86)}\Google\Cloud SDK\google-cloud-sdk\bin",
                     "$env:ProgramFiles\Google\Cloud SDK\google-cloud-sdk\bin") {
        if (Test-Path "$dir\gcloud.cmd") { $env:Path = "$dir;$env:Path"; break }
    }
}
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Fail "未安装 Google Cloud CLI：https://cloud.google.com/sdk/docs/install 安装后执行 gcloud auth login"
}
$account = (& gcloud auth list --filter="status:ACTIVE" --format="value(account)")
if (-not $account) { Write-Host "未登录，打开浏览器登录…"; Run-Gcloud auth login }
Run-Gcloud config set project $Project
Step "启用 Cloud Run / Artifact Registry / Secret Manager / Cloud Build API"
Run-Gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com --project $Project

# ---------- 4. Secret Manager（核心独立密钥 + CLIPPER_SECRETS 合并包，总计 <= 5 个免费版本） ----------
Step "写入 Secret Manager"
$secretMappings = @()
foreach ($name in "TELEGRAM_BOT_TOKEN","WEBHOOK_SECRET","NOTION_TOKEN","GEMINI_API_KEY") {
    if ($cfg[$name]) {
        Sync-Secret $name $cfg[$name]
        $secretMappings += "${name}=${name}:latest"
    }
}

$bundle = [ordered]@{}
foreach ($k in "GEMINI_API_KEY_FREE","JINA_API_KEY","FIRECRAWL_API_KEY","TAVILY_API_KEY","ZENROWS_API_KEY","GITHUB_TOKEN","REDDIT_CLIENT_SECRET","CLIPPER_MASTER_KEY","INGEST_SECRET","BROWSER_CLIP_SECRET","USER_CONFIGS","USER_CONFIGS_FILE") {
    if ($cfg[$k]) {
        if ($k -eq "USER_CONFIGS_FILE" -and (Test-Path $cfg[$k])) {
            $bundle["USER_CONFIGS"] = [IO.File]::ReadAllText((Resolve-Path $cfg[$k]), [Text.UTF8Encoding]::UTF8).Trim()
        } else {
            $bundle[$k] = $cfg[$k]
        }
    }
}
if ($bundle.Count -gt 0) {
    $bundleJson = ConvertTo-Json -InputObject $bundle -Compress -Depth 6
    Sync-Secret "CLIPPER_SECRETS" $bundleJson
    $secretMappings += "CLIPPER_SECRETS_JSON=CLIPPER_SECRETS:latest"
}

Step "授权默认服务账号（读取密钥 + 源码构建）"
$num = (& gcloud projects describe $Project --format="value(projectNumber)")
$sa = "$num-compute@developer.gserviceaccount.com"
foreach ($role in "roles/secretmanager.secretAccessor","roles/run.builder") {
    & gcloud projects add-iam-policy-binding $Project --member "serviceAccount:$sa" --role $role --condition=None --quiet 1>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "  [警告] 授予 $role 失败，如部署报权限错误请在 IAM 控制台手动授予" -ForegroundColor Yellow }
}

# ---------- 5. 部署 ----------
Step "构建并部署 Cloud Run（约 3~6 分钟）"
$envFile = Join-Path $env:TEMP "tg-notion-clipper-env.yaml"
$envLines = @()
foreach ($k in "ALLOWED_USER_ID","NOTION_DB_ARCHIVE","NOTION_DB_CONFIG","NOTION_DB_INBOX","NOTION_PAGE_TEMPLATE","GLOBAL_SUGGESTED_TAGS","GEMINI_MODEL","GEMINI_FALLBACK_MODEL","GEMINI_COMPLEX_MODEL","GEMINI_SERVICE_TIER","REDDIT_CLIENT_ID","REDDIT_USERNAME","GITHUB_COMMENT_BUDGET","GITHUB_COMMENT_LIMIT","ALBUM_SILENCE_SECONDS","ALBUM_MAX_WAIT_SECONDS","JINA_TOKEN_BUDGET","JINA_RETAIN_IMAGES","VIDEO_MAX_KEYFRAMES","VIDEO_MAX_DURATION_SEC","VIDEO_INLINE_MAX_BYTES","MONTHLY_BUDGET_USD","QUOTA_POLICY") {
    if ($cfg[$k]) {
        $quoted = ConvertTo-Json -InputObject ([string]$cfg[$k]) -Compress
        $envLines += "${k}: $quoted"
    }
}
[IO.File]::WriteAllLines($envFile, $envLines, (New-Object Text.UTF8Encoding $false))
$secretArg = $secretMappings -join ","
Run-Gcloud run deploy $Service --source . --project $Project --region $Region `
    --allow-unauthenticated --memory 1Gi --cpu 1 --min-instances 0 --max-instances 1 `
    --concurrency 10 --timeout 300 --cpu-throttling `
    --env-vars-file $envFile --set-secrets $secretArg --quiet
Remove-Item $envFile -Force -ErrorAction SilentlyContinue
$url = (& gcloud run services describe $Service --project $Project --region $Region --format="value(status.url)")
if (-not $url) { Fail "未获取到服务地址" }
Write-Host "服务地址：$url" -ForegroundColor Green

# 清理已合并进 CLIPPER_SECRETS 的旧独立密钥，保证活跃 Secret 版本总数 <= 5（低于 6 个免费上限）
foreach ($legacy in "JINA_API_KEY","FIRECRAWL_API_KEY","GITHUB_TOKEN","REDDIT_CLIENT_SECRET") {
    & gcloud secrets describe $legacy --project $Project --format="value(name)" 1>$null 2>$null
    if ($LASTEXITCODE -eq 0) {
        & gcloud secrets delete $legacy --project $Project --quiet 1>$null
        if ($LASTEXITCODE -eq 0) { Write-Host "  已合并至 CLIPPER_SECRETS 并清理旧密钥槽位：$legacy" }
    }
}

# ---------- 6. 镜像清理策略（防 Artifact Registry 超出 0.5GB 免费额度） ----------
Step "配置镜像清理策略（保留最新 2 个）"
& gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy --project $Project --location $Region --policy cleanup-policy.json --no-dry-run --quiet 1>$null
if ($LASTEXITCODE -ne 0) { Write-Host "  [警告] 清理策略设置失败，可稍后在 Artifact Registry 控制台手动配置" -ForegroundColor Yellow }

# ---------- 7. 预算告警（可选） ----------
if ($cfg["GCP_BILLING_ACCOUNT"]) {
    $Budget = if ($cfg["GCP_BUDGET_AMOUNT"]) { $cfg["GCP_BUDGET_AMOUNT"] } else { "80HKD" }
    $BudgetName = "$Service-budget"
    Step "创建预算告警（$Budget，10%/50%/90%/100% 提醒，只统计本项目）"
    & gcloud services enable billingbudgets.googleapis.com --project $Project 1>$null
    $existing = (& gcloud billing budgets list --billing-account $cfg["GCP_BILLING_ACCOUNT"] --format="value(displayName)" 2>$null) -join "`n"
    if ($existing -match [regex]::Escape($BudgetName)) {
        Write-Host "  预算「$BudgetName」已存在，跳过"
    } else {
        & gcloud billing budgets create --billing-account $cfg["GCP_BILLING_ACCOUNT"] --display-name $BudgetName `
            --budget-amount $Budget --threshold-rule percent=0.1 --threshold-rule percent=0.5 `
            --threshold-rule percent=0.9 --threshold-rule percent=1.0 --filter-projects "projects/$Project" 1>$null
        if ($LASTEXITCODE -ne 0) { Write-Host "  [警告] 预算创建失败，请在控制台 结算 → 预算和提醒 手动创建（金额需用港币）" -ForegroundColor Yellow }
        else { Write-Host "  已创建预算「$BudgetName」" }
    }
}

# ---------- 8. 绑定 Telegram Webhook ----------
Step "绑定 Telegram Webhook"
if ($Python) { & $Python set_webhook.py $url } else { Write-Host "未找到 Python，请手动运行：python set_webhook.py $url" -ForegroundColor Yellow }

Write-Host "`n部署完成。在 Telegram 给 Bot 发一条链接试试。查看日志：gcloud run services logs read $Service --region $Region --limit 50" -ForegroundColor Green
if (-not $NoPause) { Read-Host "按回车退出" }
