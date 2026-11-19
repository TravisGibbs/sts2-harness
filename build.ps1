# Builds the SpireBridge mod and installs it into the game's mods folder.
#   .\build.ps1                      build + install
#   .\build.ps1 -NoInstall           build only
#   .\build.ps1 -GameDir 'D:\...'    non-default Steam library
param(
    [string]$GameDir = 'C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2',
    [switch]$NoInstall
)

$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'

$dotnet = (Get-Command dotnet -ErrorAction SilentlyContinue).Source
if (-not $dotnet) { $dotnet = 'C:\Program Files\dotnet\dotnet.exe' }

if (-not (Test-Path (Join-Path $GameDir 'data_sts2_windows_x86_64\sts2.dll'))) {
    throw "Slay the Spire 2 not found at '$GameDir'. Pass -GameDir."
}

$project = Join-Path $PSScriptRoot 'mod\SpireBridge.csproj'
& $dotnet build $project -c Release -v q "-p:GameDir=$GameDir"
if ($LASTEXITCODE -ne 0) { throw "build failed" }

if ($NoInstall) { return }

$out = Join-Path $PSScriptRoot 'mod\bin\Release'
$dest = Join-Path $GameDir 'mods\SpireBridge'
New-Item -ItemType Directory -Force $dest | Out-Null
try {
    Copy-Item (Join-Path $out 'SpireBridge.dll') $dest -Force
    Copy-Item (Join-Path $out 'SpireBridge.json') $dest -Force
} catch {
    throw "Could not copy into '$dest'. Close Slay the Spire 2 first (the DLL is locked while the game runs). $_"
}
Write-Host "Installed SpireBridge to $dest"
