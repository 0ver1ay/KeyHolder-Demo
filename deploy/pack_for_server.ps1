# KeyHolder — pack source tree for Linux Mint server (build + deploy on host).
# Run from repo root:  powershell -File deploy/pack_for_server.ps1

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$StagingRoot = Join-Path $RepoRoot '_package_staging'
$PackageDir = Join-Path $StagingRoot 'KeyHolder'
$ArchivePath = Join-Path $RepoRoot 'deploy\KeyHolder-server.zip'

$includeDirs = @('config', 'controllers', 'db', 'deploy', 'scripts', 'services', 'views')
$includeFiles = @(
    'main_admin.py',
    'main_user.py',
    'requirements.txt',
    'config.cfg',
    'rfid.txt',
    'readme_rfid.txt',
    '.gitattributes'
)
# deploy/requirements-linux.txt is inside deploy/ directory copy

if (Test-Path $StagingRoot) {
    Remove-Item -Recurse -Force $StagingRoot
}
New-Item -ItemType Directory -Path $PackageDir -Force | Out-Null

function Copy-TreeFiltered {
    param(
        [string]$Source,
        [string]$Destination
    )
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        $name = $_.Name
        if ($name -in @('__pycache__', '.env', '_package_staging', 'KeyHolder-server.zip')) {
            return
        }
        $target = Join-Path $Destination $name
        if ($_.PSIsContainer) {
            Copy-TreeFiltered -Source $_.FullName -Destination $target
        }
        elseif ($name -notmatch '\.pyc$') {
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    }
}

foreach ($dir in $includeDirs) {
    $src = Join-Path $RepoRoot $dir
    if (-not (Test-Path $src)) {
        throw "Missing required directory: $dir"
    }
    Copy-TreeFiltered -Source $src -Destination (Join-Path $PackageDir $dir)
}

foreach ($file in $includeFiles) {
    $src = Join-Path $RepoRoot $file
    if (-not (Test-Path $src)) {
        throw "Missing required file: $file"
    }
    Copy-Item -LiteralPath $src -Destination (Join-Path $PackageDir $file) -Force
}

# Unix line endings for shell scripts (important on Mint).
Get-ChildItem -Path $PackageDir -Recurse -Filter '*.sh' -File | ForEach-Object {
    $text = [System.IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n" -replace "`r", "`n"
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($_.FullName, $text, $utf8)
}

$readme = @'
KeyHolder — архив для сервера (Linux Mint)

Распаковка:
  unzip KeyHolder-server.zip
  cd KeyHolder

Дальше по инструкции:
  deploy/КАК_УСТАНОВИТЬ.md

Кратко:
  sed -i 's/\r$//' deploy/*.sh
  sudo bash deploy/install_system_deps.sh
  # перезайти в систему
  cp deploy/.env.example deploy/.env
  # база по умолчанию на порту 5433 (DB_PORT в deploy/.env)
  docker compose -f deploy/docker-compose.yml up -d
  bash deploy/build_linux.sh
  cp deploy/config.deploy.cfg dist/AdminApp/config.cfg
  cp deploy/config.deploy.cfg dist/UserApp/config.cfg
  bash deploy/run_admin.sh
  bash deploy/run_user.sh

Демо-данные (опционально):
  bash deploy/seed_demo_36.sh

RFID-симулятор (разработка):
  python3 scripts/rfid_simulator.py
'@
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path $PackageDir 'deploy\SERVER_README.txt'), $readme, $utf8)

if (Test-Path $ArchivePath) {
    Remove-Item -Force $ArchivePath
}
Compress-Archive -Path $PackageDir -DestinationPath $ArchivePath -CompressionLevel Optimal

Remove-Item -Recurse -Force $StagingRoot

$sizeMb = [math]::Round((Get-Item $ArchivePath).Length / 1MB, 2)
Write-Host "Created: $ArchivePath ($sizeMb MB)"
