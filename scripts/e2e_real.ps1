<#
  ============================================================================
  ⚠️  真实闭环脚本 —— 只在本地手动运行，**绝不进 CI**
  ============================================================================
  会做三件"有副作用"的事，别在你不希望发生的时候跑它：
    1) 向**真实的 storage/** 写数据：建 SQLite 用户库、JWT 密钥、按用户隔离的
       数据与索引目录（都会留下真实文件，虽然都在 .gitignore 里）
    2) 调用**真实 LLM**，产生费用（每个项目 1 次真实提问）
    3) 启动/关闭真实服务进程（只按自己记录的 PID 关闭，不按进程名杀）

  它的定位：把"测试全绿"变成"确实能用"。常驻的 smoke.ps1 是离线、便宜、
  可反复跑的回归；本脚本跑一次真实闭环，补上 smoke 结构上做不到的四件事 ——
    ① 随机管理员口令路径（不设 ADMIN_PASSWORD）：日志横幅 → 强制首登改密
    ② 真实上传真实文件
    ③ 真实提问（真调 LLM）
    ④ 运行期文件确实被创建（app.db / secret.key / 用户目录）

  口令策略：**脚本里没有任何明文口令**。
    · 管理员口令：只从启动日志的横幅里解析（服务端随机生成）
    · 注册/建号用的口令：脚本运行时随机生成，用完即弃
  这样它将来进了仓库也不会把口令写进 Git 历史。

  前置：目标项目的 storage/app.db **必须不存在**（要验证的正是"空库 → 建库"）。
  若已存在，先把 app.db 及 -wal/-shm 挪走再跑 —— 脚本会明确拒绝而不是猜口令。

  用法: powershell -ExecutionPolicy Bypass -File scripts/e2e_real.ps1 -Project p1
  ============================================================================
#>
param([Parameter(Mandatory = $true)][ValidateSet('p1', 'p2')][string]$Project)

$ErrorActionPreference = 'Stop'
$PY = if ($env:DSH_PYTHON) { $env:DSH_PYTHON } else { 'C:\Users\YoshinoCiallo\.workbuddy\binaries\python\envs\default\Scripts\python.exe' }
$Curl = 'C:\Windows\system32\curl.exe'
$Tmp = Join-Path $env:TEMP 'dsh-e2e'

if ($Project -eq 'p1') {
  $Root = Split-Path $PSScriptRoot -Parent
  $Port = 8000
  $HealthPath = '/health'
  $UploadPath = '/upload'
  $AskPath = '/query'
  $ListPath = '/api/documents'
  $UploadSrc = Join-Path $Root 'data\FX.pdf'
  $Question = '这份文档主要讲了什么？请用一句话回答。'
  $UserDirs = @((Join-Path $Root 'storage\manifests'), (Join-Path $Root 'data\users'))
} else {
  $Root = Split-Path $PSScriptRoot -Parent
  $Port = 8123
  $HealthPath = '/api/health'
  $UploadPath = '/api/upload'
  $AskPath = '/api/ask'
  $ListPath = '/api/datasets'
  $UploadSrc = Join-Path $Root 'storage\session\data\Inhouse Lab Test Summary June 2024.xlsx'
  $Question = '这份数据有多少行、多少列？请用一句话回答。'
  $UserDirs = @((Join-Path $Root 'storage\session\users'))
}
$Base = "http://127.0.0.1:$Port"
if (-not (Test-Path $Tmp)) { New-Item -ItemType Directory -Path $Tmp -Force | Out-Null }
$LogOut = Join-Path $Tmp "$Project.out.log"
$LogErr = Join-Path $Tmp "$Project.err.log"
$RespFile = Join-Path $Tmp "$Project.resp.txt"
$CurlErr = Join-Path $Tmp "$Project.curl.err"
$BodyJson = Join-Path $Tmp "$Project.body.json"

$script:Pass = 0; $script:Fail = 0
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
  if ($Ok) { $script:Pass++; Write-Host ("  ok    " + $Name) }
  else { $script:Fail++; Write-Host ("  FAIL  " + $Name + "  ->  " + $Detail) }
}

# 随机强口令：满足服务端策略（>=12 位，含大小写 / 数字 / 符号）。脚本里不留任何明文。
function New-StrongPassword {
  $upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'.ToCharArray()
  $lower = 'abcdefghijkmnopqrstuvwxyz'.ToCharArray()
  $digit = '23456789'.ToCharArray()
  $sym = '!@#$%^&*-_=+?'.ToCharArray()
  $chars = @()
  $chars += ($upper | Get-Random -Count 4)
  $chars += ($lower | Get-Random -Count 4)
  $chars += ($digit | Get-Random -Count 4)
  $chars += ($sym | Get-Random -Count 4)
  $all = $upper + $lower + $digit + $sym
  $chars += ($all | Get-Random -Count 4)
  return (-join ($chars | Get-Random -Count $chars.Count))
}

function Http {
  param([string]$Method, [string]$Path, $Body, [string]$Token, [string[]]$Form)
  # 预删响应文件：否则请求失败时会读到**上一次**的响应体，把"脚本坏了"误判成"服务端坏了"
  if (Test-Path $RespFile) { Remove-Item $RespFile -Force }
  if (Test-Path $CurlErr) { Remove-Item $CurlErr -Force }
  $cargs = @('-s', '-S', '-o', $RespFile, '-w', '%{http_code}', '-X', $Method, "$Base$Path")
  if ($Token) { $cargs += @('-H', "Authorization: Bearer $Token") }
  if ($null -ne $Body) {
    # JSON 必须走文件：直接把 JSON 当原生参数传给 curl.exe 会被 PS 5.1 打坏（→ 422）。
    # 用 **UTF-8 无 BOM**：问题里有中文，ASCII 会把它们变成 '?'。
    $jsonText = $Body | ConvertTo-Json -Depth 12 -Compress
    [System.IO.File]::WriteAllText($BodyJson, $jsonText, (New-Object System.Text.UTF8Encoding($false)))
    $cargs += @('-H', 'Content-Type: application/json', '--data-binary', "@$BodyJson")
  }
  if ($Form) { foreach ($f in $Form) { $cargs += @('-F', $f) } }
  $stdout = "$(& $Curl @cargs 2> $CurlErr)"
  if ($stdout) { $stdout = $stdout.Trim() }
  $curlExit = $LASTEXITCODE
  $code = 0
  if ($stdout -match '^\d+$') { $code = [int]$stdout }
  $raw = ''
  if (Test-Path $RespFile) { $raw = "$(Get-Content $RespFile -Raw -Encoding UTF8)" }
  $json = $null
  if ($raw) { try { $json = $raw | ConvertFrom-Json } catch { $json = $null } }
  $errText = ''
  if (Test-Path $CurlErr) { $errText = "$(Get-Content $CurlErr -Raw -Encoding UTF8)" }
  $snip = "$raw"
  if ($snip.Length -gt 200) { $snip = $snip.Substring(0, 200) }
  if ($errText) { $snip = "$snip | curl($curlExit): $errText" }
  return [pscustomobject]@{ Status = $code; Json = $json; Raw = $snip; CurlExit = $curlExit }
}

Write-Host ""
Write-Host "=== 真实服务闭环：$Project（$Root，端口 $Port）===" -ForegroundColor Cyan

# ---------------- 0. 前置 ----------------
$busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
Check "前置：端口 $Port 当前无监听" (-not $busy) "已被 PID $($busy.OwningProcess) 占用"
Check "前置：上传源文件存在（$([System.IO.Path]::GetFileName($UploadSrc))）" (Test-Path $UploadSrc) $UploadSrc
$dbPath = Join-Path $Root 'storage\app.db'
$freshDb = -not (Test-Path $dbPath)
Check "前置：用户库不存在（要验证的正是「空库 -> 建库」）" $freshDb "$dbPath 已存在 —— 先把 app.db 及 -wal/-shm 挪走再跑"
if (-not $freshDb) { Write-Host "拒绝在已有用户库上运行（脚本不含明文口令，无法登录既有账号）" -ForegroundColor Yellow; exit 2 }
if (-not (Test-Path $UploadSrc)) { Write-Host "缺上传源文件，无法继续" -ForegroundColor Yellow; exit 2 }

$UploadAscii = Join-Path $Tmp ("$Project-upload" + [System.IO.Path]::GetExtension($UploadSrc))
Copy-Item -LiteralPath $UploadSrc -Destination $UploadAscii -Force
Check "前置：上传副本已就位（ASCII 路径）" (Test-Path $UploadAscii) $UploadAscii

$git = 'C:\Users\YoshinoCiallo\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe'
$head = "$(& $git -C $Root rev-parse --short HEAD)"
if ($head) { $head = $head.Trim() }
Write-Host "  （起点 HEAD = $head）"

# ---------------- 1. 起服务（故意不设 ADMIN_PASSWORD） ----------------
Remove-Item Env:ADMIN_PASSWORD -ErrorAction SilentlyContinue
$env:ALLOW_REGISTRATION = 'true'   # 本次要验证自助注册路径；投产默认 false 不变
$env:PYTHONIOENCODING = 'utf-8'
if (Test-Path $LogOut) { Remove-Item $LogOut -Force }
if (Test-Path $LogErr) { Remove-Item $LogErr -Force }

$proc = Start-Process -FilePath $PY -ArgumentList @('-m', 'uvicorn', 'src.server:app', '--port', "$Port") `
  -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput $LogOut -RedirectStandardError $LogErr
Write-Host "  服务 PID = $($proc.Id)（日志：$LogErr）"

try {
  $up = $false
  for ($i = 0; $i -lt 80; $i++) {
    Start-Sleep -Milliseconds 500
    try {
      $c = "$(& $Curl @('-s', '-o', 'NUL', '-w', '%{http_code}', "$Base$HealthPath"))"
      if ($c.Trim() -eq '200') { $up = $true; break }
    } catch { }
    if ($proc.HasExited) { break }
  }
  Check "服务启动（$HealthPath 200）" $up "进程已退出=$($proc.HasExited)"
  if (-not $up) {
    Write-Host "--- 启动日志尾部 ---" -ForegroundColor Yellow
    if (Test-Path $LogErr) { Get-Content $LogErr -Encoding UTF8 -Tail 25 | ForEach-Object { "   " + $_ } }
    throw "服务没起来"
  }

  $suffix = (Get-Date -Format 'HHmmss')

  # ---------------- 2. 管理员：随机口令路径（口令只从横幅拿，脚本不预置） ----------------
  $log = ''
  foreach ($f in @($LogErr, $LogOut)) { if (Test-Path $f) { $log += "$(Get-Content $f -Raw -Encoding UTF8)" } }
  $flat = $log -replace '\s+', ''
  $adminPwd = $null
  if ($flat -match '口令：([^\r\n该]+)') { $adminPwd = $Matches[1] }
  Check "空库启动 -> 日志打出管理员初始口令横幅（随机口令路径）" ([bool]$adminPwd) "没解析到『口令：』"
  Check "横幅里说明了首登必须改密" ($flat -match '首次登录后必须先改密') "横幅文案里没有强制改密说明"
  if (-not $adminPwd) { throw "拿不到管理员口令" }

  $login = Http 'POST' '/api/auth/login' @{ account = 'admin'; password = $adminPwd }
  Check "管理员用随机口令登录 -> 200" ($login.Status -eq 200) "HTTP $($login.Status) $($login.Raw)"
  Check "该账号 must_change_password=true" ($login.Json.user.must_change_password -eq $true) "$($login.Raw)"
  $adminTok = $login.Json.token
  $gated = Http 'GET' $ListPath $null $adminTok
  Check "改密前访问业务接口 -> 403（闸门生效）" ($gated.Status -eq 403) "HTTP $($gated.Status)"

  $chg = Http 'POST' '/api/auth/change-password' @{ old_password = $adminPwd; new_password = (New-StrongPassword) } $adminTok
  Check "管理员改密 -> 200 且返回新 token" ($chg.Status -eq 200 -and $chg.Json.token) "HTTP $($chg.Status) $($chg.Raw)"
  $adminTok2 = $chg.Json.token
  $stale = Http 'GET' $ListPath $null $adminTok
  Check "改密后旧 token 立即失效 -> 401" ($stale.Status -eq 401) "HTTP $($stale.Status)"
  $ungated = Http 'GET' $ListPath $null $adminTok2
  Check "用新 token 访问业务接口 -> 200（闸门解除）" ($ungated.Status -eq 200) "HTTP $($ungated.Status)"

  # ---------------- 3. 自助注册（口令随机生成，用完即弃） ----------------
  $aliceName = "alice$suffix"
  $alicePwd = New-StrongPassword
  $reg = Http 'POST' '/api/auth/register' @{ username = $aliceName; email = "$aliceName@example.com"; password = $alicePwd }
  Check "自助注册 $aliceName -> 200 + token" ($reg.Status -eq 200 -and $reg.Json.token) "HTTP $($reg.Status) $($reg.Raw)"
  $aliceTok = $reg.Json.token

  # ---------------- 4. 管理员建号（一次性口令 → 强制改密） ----------------
  $bobName = "bob$suffix"
  $created = Http 'POST' '/api/admin/users' @{ username = $bobName; email = "$bobName@example.com"; role = 'user' } $adminTok2
  Check "管理员建号 $bobName -> 200 且返回一次性口令" ($created.Status -eq 200 -and $created.Json.password) "HTTP $($created.Status) $($created.Raw)"
  $bobPwd = $created.Json.password
  $bobLogin = Http 'POST' '/api/auth/login' @{ account = $bobName; password = $bobPwd }
  Check "新账号用一次性口令登录 -> must_change_password=true" ($bobLogin.Status -eq 200 -and $bobLogin.Json.user.must_change_password -eq $true) "HTTP $($bobLogin.Status)"
  $bobChg = Http 'POST' '/api/auth/change-password' @{ old_password = $bobPwd; new_password = (New-StrongPassword) } $bobLogin.Json.token
  Check "新账号改密 -> 200 + 新 token" ($bobChg.Status -eq 200 -and $bobChg.Json.token) "HTTP $($bobChg.Status)"
  $bobTok = $bobChg.Json.token

  # ---------------- 5. 真实上传 ----------------
  $up1 = Http 'POST' $UploadPath $null $aliceTok @("files=@$UploadAscii")
  Check "alice 真实上传 $([System.IO.Path]::GetFileName($UploadAscii)) -> 200" ($up1.Status -eq 200) "HTTP $($up1.Status) $($up1.Raw)"
  $listed = Http 'GET' $ListPath $null $aliceTok
  $n = 0
  if ($listed.Json) { foreach ($k in @('documents', 'datasets', 'files')) { if ($listed.Json.$k) { $n = @($listed.Json.$k).Count; break } } }
  Check "上传后列表里能看到（$n 条）" ($n -ge 1) "HTTP $($listed.Status) $($listed.Raw)"

  # ---------------- 6. 真实提问（真调 LLM，会产生费用） ----------------
  Write-Host "  真实提问中（会调 LLM，请稍候）..." -ForegroundColor Yellow
  $anonAsk = Http 'POST' $AskPath @{ question = $Question }
  Check "匿名提问 -> 401（未登录打不动 LLM）" ($anonAsk.Status -eq 401) "HTTP $($anonAsk.Status) $($anonAsk.Raw)"
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $asked = Http 'POST' $AskPath @{ question = $Question } $aliceTok
  $sw.Stop()
  Check "alice 真实提问 -> 200（耗时 $([math]::Round($sw.Elapsed.TotalSeconds,1))s）" ($asked.Status -eq 200) "HTTP $($asked.Status) $($asked.Raw)"
  $answer = ''
  if ($asked.Json -and $asked.Json.answer) { $answer = [string]$asked.Json.answer }
  Check "回答非空（长度 $($answer.Length)）" ($answer.Length -gt 0) "$($asked.Raw)"
  if ($answer) {
    $peek = $answer; if ($peek.Length -gt 90) { $peek = $peek.Substring(0, 90) }
    Write-Host "  回答节选：$peek" -ForegroundColor DarkGray
  }
  $citeCount = 0
  if ($asked.Json -and $asked.Json.citations) { $citeCount = @($asked.Json.citations).Count }
  Write-Host "  引用条数：$citeCount" -ForegroundColor DarkGray

  $sess = Http 'GET' '/api/sessions' $null $aliceTok
  $sid = $null
  if ($sess.Json -and $sess.Json.sessions) { $sid = @($sess.Json.sessions)[0].id }
  Check "真实提问落了会话（session id=$sid）" ([bool]$sid) "$($sess.Raw)"

  # ---------------- 7. 越权尝试 ----------------
  if ($sid) {
    $x1 = Http 'GET' "/api/sessions/$sid/messages" $null $bobTok
    Check "bob 读 alice 的会话消息 -> 404（不泄露存在性）" ($x1.Status -eq 404) "HTTP $($x1.Status) $($x1.Raw)"
    $x3 = Http 'DELETE' "/api/sessions/$sid" $null $bobTok
    Check "bob 删 alice 的会话 -> 404" ($x3.Status -eq 404) "HTTP $($x3.Status)"
  }
  $x2 = Http 'GET' '/api/sessions' $null $bobTok
  $bobIds = @()
  if ($x2.Json -and $x2.Json.sessions) { $bobIds = @($x2.Json.sessions | ForEach-Object { $_.id }) }
  Check "bob 的会话列表里没有 alice 的会话" (-not ($bobIds -contains $sid)) "bob 看到了 $($bobIds -join ',')"

  # ---------------- 8. 运行期数据被真实创建 ----------------
  Check "storage/app.db 存在且非空" ((Test-Path $dbPath) -and (Get-Item $dbPath).Length -gt 0) $dbPath
  $key = Join-Path $Root 'storage\secret.key'
  Check "storage/secret.key 已创建（JWT 签名密钥）" (Test-Path $key) $key
  foreach ($d in $UserDirs) { Check "按用户隔离的目录：$(Split-Path $d -Leaf)" (Test-Path $d) $d }
  $users = Http 'GET' '/api/admin/users' $null $adminTok2
  $un = 0
  if ($users.Json -and $users.Json.users) { $un = @($users.Json.users).Count }
  Check "管理员看得到 $un 个用户（>=3）" ($un -ge 3) "$($users.Raw)"
}
catch {
  Write-Host "脚本异常：$($_.Exception.Message)" -ForegroundColor Red
  $pos = "$($_.InvocationInfo.PositionMessage)"
  Write-Host "  出错位置：$($pos.Trim())" -ForegroundColor Red
  throw
}
finally {
  # 只关自己起的那个 PID —— 绝不按进程名杀（AGENTS.md §一）
  if ($proc -and -not $proc.HasExited) {
    Write-Host "  关闭本次启动的服务 PID=$($proc.Id)" -ForegroundColor DarkGray
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 600
    $still = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($still) { Write-Host "  警告：端口 $Port 仍被 PID $($still.OwningProcess) 占用" -ForegroundColor Yellow }
  }
}

Write-Host ""
Write-Host "通过 $script:Pass 项，失败 $script:Fail 项" -ForegroundColor $(if ($script:Fail) { 'Red' } else { 'Green' })
if ($script:Fail) { exit 1 }
Write-Host "真实闭环通过（$Project）" -ForegroundColor Green
exit 0
