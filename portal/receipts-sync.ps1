<#
  Unscatter receipts sync — copies receipts waiting in Finance into your OneDrive folder on this computer.

  Files only ever go inside a client folder:  <OneDrive>\Clients\<client>\<folder set in Finance>\<YYYY-MM>\
  (anything outside Clients\<client>\ is refused).
  OneDrive then uploads them by itself; Finance never gets write access to your OneDrive.

  First run:   powershell -ExecutionPolicy Bypass -File receipts-sync.ps1
               (asks for the receipts sync key from Finance → Receipts → "Receipts sync on your computer";
                the key is saved encrypted for your Windows user only, in %APPDATA%\Unscatter)
  New key:     ... -File receipts-sync.ps1 -SetKey
  Other OneDrive folder:  ... -OneDrive "C:\business\OneDrive - Unscatter"
  Scheduled (Task Scheduler):  ... -File receipts-sync.ps1 -NoPause
#>
param(
  [string]$Url = "https://finance.unscatterllc.com",
  [string]$OneDrive = "",
  [switch]$SetKey,
  [switch]$NoPause   # for Task Scheduler: don't wait for Enter at the end
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$dir = Join-Path $env:APPDATA "Unscatter"
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
$keyFile = Join-Path $dir "receipts-sync.key"
$logFile = Join-Path $dir "receipts-sync.log"
function Log($msg) { $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg; Add-Content -Path $logFile -Value $line; Write-Host $line }

if ($SetKey -or -not (Test-Path $keyFile)) {
  $secure = Read-Host "Paste the receipts sync key from Finance" -AsSecureString
  $secure | Export-Clixml -Path $keyFile   # encrypted with Windows DPAPI: only this Windows user can read it
}
$secure = Import-Clixml -Path $keyFile
$key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$headers = @{ Authorization = "Bearer $key" }

if (-not $OneDrive) { $OneDrive = if ($env:OneDriveCommercial) { $env:OneDriveCommercial } else { $env:OneDrive } }
if (-not $OneDrive -or -not (Test-Path $OneDrive)) { throw "OneDrive folder not found. Run again with -OneDrive ""C:\path\to\OneDrive""." }
$root = (Resolve-Path $OneDrive).Path

function Get-Pending {
  try { return Invoke-RestMethod -Uri "$Url/api/receipts/pending" -Headers $headers -Method Get }
  catch {
    $code = 0; if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    if ($code -eq 401) { return "refused" }
    Log "Couldn't reach Finance: $($_.Exception.Message)"; exit 1
  }
}
$list = Get-Pending
if ($list -eq "refused") {
  # the saved key is out of date (making a new key in Finance replaces the old one): ask for the current one once
  Log "The saved sync key was refused (a newer key was made in Finance)."
  $secure = Read-Host "Paste the CURRENT receipts sync key from Finance (Receipts -> Receipts sync on your computer)" -AsSecureString
  $secure | Export-Clixml -Path $keyFile
  $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  $headers = @{ Authorization = "Bearer $key" }
  $list = Get-Pending
  if ($list -eq "refused") { Log "That key was refused too. In Finance make a new key, then run this again."; exit 1 }
}
# documents already in OneDrive whose folder changed in Finance (e.g. Vendor\Year layout): move them on this computer
function Test-ClientFolder($f) {
  $p = ([string]$f).Split("/")
  return -not ($p.Count -lt 3 -or $p[0] -ne "Clients" -or ($p | Where-Object { $_ -eq "" -or $_ -eq "." -or $_ -eq ".." -or $_ -match '[:*?"<>|]' }))
}
$moved = 0
try { $mv = Invoke-RestMethod -Uri "$Url/api/receipts/moves" -Headers $headers -Method Get } catch { $mv = $null }
foreach ($m in @($mv.moves)) {
  if (-not $m) { continue }
  $srcRel = [string]$m.src; $folder = [string]$m.folder
  if (-not (Test-ClientFolder ([IO.Path]::GetDirectoryName($srcRel) -replace "\\", "/")) -or -not (Test-ClientFolder $folder)) { Log "Skipped move #$($m.id): outside Clients\<client>\"; continue }
  $src = Join-Path $root ($srcRel -replace "/", "\")
  if (-not (Test-Path $src)) { Log "Can't move #$($m.id): $srcRel isn't on this computer (yet)"; continue }
  $target = Join-Path $root ($folder -replace "/", "\")
  if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
  $name = ([string]$m.filename) -replace '[\\/:*?"<>|]', ''
  if (-not $name) { $name = [IO.Path]::GetFileName($src) }
  $dest = Join-Path $target $name
  $base = [IO.Path]::GetFileNameWithoutExtension($name); $ext = [IO.Path]::GetExtension($name); $n = 2
  while (Test-Path $dest) { $name = "$base ($n)$ext"; $dest = Join-Path $target $name; $n++ }
  try {
    Move-Item -Path $src -Destination $dest
    $body = @{ path = "$folder/$name" } | ConvertTo-Json
    Invoke-RestMethod -Uri "$Url/api/receipts/$($m.id)/moved" -Headers $headers -Method Post -ContentType "application/json" -Body $body | Out-Null
    Log "Moved $srcRel -> $folder/$name"; $moved++
    $old = Split-Path $src -Parent   # remove the old folder if it's now empty (only inside Clients\)
    if ((Test-Path $old) -and -not (Get-ChildItem -Path $old -Force | Select-Object -First 1)) { Remove-Item -Path $old -Force }
  } catch { Log "Problem moving #$($m.id): $($_.Exception.Message)" }
}
if ($moved) { Log "Moved $moved document(s) into the new folders." }

$items = @($list.receipts)
if (-not $items.Count) { Log "Nothing waiting."; if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }; exit 0 }

$done = 0
foreach ($r in $items) {
  $folder = [string]$r.folder
  $parts = $folder.Split("/")
  if ($parts.Count -lt 3 -or $parts[0] -ne "Clients" -or ($parts | Where-Object { $_ -eq "" -or $_ -eq "." -or $_ -eq ".." -or $_ -match '[:*?"<>|]' })) {
    Log "Skipped #$($r.id): folder '$folder' is outside Clients\<client>\"; continue
  }
  $name = ([string]$r.filename) -replace '[\\/:*?"<>|]', ''
  if (-not $name) { $name = "receipt-$($r.id)" }
  $target = Join-Path $root ($folder -replace "/", "\")
  if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
  $tmp = Join-Path $env:TEMP ("receipt-" + $r.id + ".part")
  try {
    Invoke-WebRequest -Uri "$Url/api/receipts/$($r.id)/file" -Headers $headers -OutFile $tmp -UseBasicParsing
    $hash = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash.ToLower()
    if ($r.sha256 -and $hash -ne $r.sha256) { Log "Skipped #$($r.id): download didn't match (try again later)"; Remove-Item $tmp -Force; continue }
    $dest = Join-Path $target $name
    $base = [IO.Path]::GetFileNameWithoutExtension($name); $ext = [IO.Path]::GetExtension($name); $n = 2
    while ((Test-Path $dest) -and ((Get-FileHash -Path $dest -Algorithm SHA256).Hash.ToLower() -ne $hash)) {
      $name = "$base ($n)$ext"; $dest = Join-Path $target $name; $n++
    }
    if (Test-Path $dest) { Remove-Item $tmp -Force } else { Move-Item -Path $tmp -Destination $dest }
    $body = @{ path = "$folder/$name" } | ConvertTo-Json
    Invoke-RestMethod -Uri "$Url/api/receipts/$($r.id)/filed" -Headers $headers -Method Post -ContentType "application/json" -Body $body | Out-Null
    Log "Filed $folder/$name"
    $done++
  } catch {
    Log "Problem with #$($r.id): $($_.Exception.Message)"
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
  }
}
Log "Done: $done of $($items.Count) receipt(s) copied into OneDrive."
if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
