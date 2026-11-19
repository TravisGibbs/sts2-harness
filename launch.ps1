# Starts (or stops) Slay the Spire 2 for the harness.
#   .\launch.ps1                    headless: no window, nothing takes focus
#   .\launch.ps1 -Windowed          normal window, so you can watch the agent play (instance 0 only)
#   .\launch.ps1 -Stop              close the game cleanly through the bridge
#   .\launch.ps1 -Instance 2        an extra headless game on port 15527+2 with its own user data
#   .\launch.ps1 -Stop -Instance 2  close only that one
#   .\launch.ps1 -Stop -All         close instance 0 and every extra instance
#   .\launch.ps1 -Instance 2 -Render -Movie C:\path\run.avi
#                                   an extra instance that really draws, for filming: it runs on a
#                                   separate Windows desktop that is never shown, so its window can't
#                                   appear or take focus; -Movie records every frame (and the game's
#                                   audio, which then never reaches the speakers) with Godot's Movie Maker
#
# Steam must be running. The game is started directly rather than through
# Steam so that --headless can be passed; SteamAppId tells Steamworks which
# game it is, which is what keeps your normal Steam profile and saves in use.
#
# Instances. Instance 0 is the ordinary game: your real user data, port 15527.
# Instance n >= 1 listens on 15527+n and keeps its user data (settings, saves,
# logs) under .instances\<n>\appdata, a copy of %APPDATA%\SlayTheSpire2 made the
# first time that instance is launched. The game finds its user data through
# the APPDATA environment variable, so only that one process is redirected and
# the real folder is never written to. Steam Cloud is not redirected: every
# instance still mirrors its saves into the same Steam "modded" cloud slot.
param(
    [switch]$Windowed,
    [switch]$Stop,
    [switch]$All,
    [int]$Instance = 0,
    [string]$GameDir = 'C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2',
    [int]$Port = 0,
    [int]$TimeoutSeconds = 120,
    [switch]$Render,
    [string]$Movie = '',
    [int]$Fps = 30,
    [string]$RenderSize = '1280x720'
)

$ErrorActionPreference = 'Stop'
$BasePort = 15527
$ProcessName = 'SlayTheSpire2'
$instancesRoot = Join-Path $PSScriptRoot '.instances'

if ($Instance -lt 0) { throw '-Instance must be 0 or greater.' }
if ($All -and -not $Stop) { throw '-All only applies to -Stop.' }
if ($Windowed -and $Instance -ne 0) { throw '-Windowed is only for instance 0; extra instances are always headless.' }
if ($Render -and $Instance -lt 1) { throw '-Render is only for extra instances (-Instance 1 or more).' }
if ($Movie -and -not $Render) { throw '-Movie needs -Render.' }

# Starts a process on a separate desktop in this session. Nothing on that desktop is ever shown,
# and a window there can't become the foreground window of the desktop the user is looking at.
function Start-OnHiddenDesktop([string]$exe, [string]$commandLine, [string]$dir) {
    if (-not ('SpireBridgeDesktop' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SpireBridgeDesktop {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO { public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError; }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateDesktop(string name, IntPtr dev, IntPtr mode, int flags, uint access, IntPtr sa);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string app, string cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags,
        IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    public static int Start(string desktop, string exe, string cmdline, string dir) {
        // Opens the desktop if it exists. It lasts while a handle to it or a thread on it does.
        IntPtr d = CreateDesktop(desktop, IntPtr.Zero, IntPtr.Zero, 0, 0x10000000, IntPtr.Zero);
        if (d == IntPtr.Zero) throw new Exception("CreateDesktop failed: " + Marshal.GetLastWin32Error());
        var si = new STARTUPINFO(); si.cb = Marshal.SizeOf(si); si.lpDesktop = "WinSta0\\" + desktop;
        PROCESS_INFORMATION pi;
        if (!CreateProcess(exe, cmdline, IntPtr.Zero, IntPtr.Zero, false, 0, IntPtr.Zero, dir, ref si, out pi))
            throw new Exception("CreateProcess failed: " + Marshal.GetLastWin32Error());
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        return pi.dwProcessId;
    }
}
'@
    }
    $id = [SpireBridgeDesktop]::Start('SpireBridgeRender', $exe, $commandLine, $dir)
    return Get-Process -Id $id
}

function Get-InstancePort([int]$n) {
    if ($n -eq $Instance -and $Port -gt 0) { return $Port }
    return $BasePort + $n
}
function Get-InstanceDir([int]$n) { return (Join-Path $instancesRoot "$n") }
function Get-PidFile([int]$n) { return (Join-Path (Get-InstanceDir $n) 'pid') }

function Test-Bridge([int]$port) {
    try { return (Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 2).Content }
    catch { return $null }
}

# The process recorded for an instance, or $null when it is gone. The start
# time is checked as well as the id, so a recycled process id is never trusted.
function Get-TrackedProcess([int]$n) {
    $file = Get-PidFile $n
    if (-not (Test-Path $file)) { return $null }
    $lines = @(Get-Content $file)
    $id = 0
    if ($lines.Count -lt 1 -or -not [int]::TryParse($lines[0], [ref]$id)) { return $null }
    $proc = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.ProcessName -ne $ProcessName) { return $null }
    if ($lines.Count -ge 2) {
        try { if ($proc.StartTime.ToUniversalTime().ToString('o') -ne $lines[1]) { return $null } } catch { return $null }
    }
    return $proc
}

function Get-TrackedInstances {
    if (-not (Test-Path $instancesRoot)) { return @() }
    $found = @()
    foreach ($dir in Get-ChildItem $instancesRoot -Directory) {
        $n = 0
        if ([int]::TryParse($dir.Name, [ref]$n) -and (Test-Path (Join-Path $dir.FullName 'pid'))) { $found += $n }
    }
    return $found
}

# Game processes that no instance's pid file accounts for.
function Get-UntrackedProcesses {
    $tracked = @()
    foreach ($n in Get-TrackedInstances) {
        $p = Get-TrackedProcess $n
        if ($p) { $tracked += $p.Id }
    }
    return @(Get-Process $ProcessName -ErrorAction SilentlyContinue | Where-Object { $tracked -notcontains $_.Id })
}

function Stop-Instances([int[]]$numbers) {
    # Ask every one to quit first, then wait for them together.
    $pending = @()
    foreach ($n in $numbers) {
        $port = Get-InstancePort $n
        $proc = Get-TrackedProcess $n
        $answering = [bool](Test-Bridge $port)
        if (-not $proc -and $n -eq 0) {
            # Instance 0 may have been started by Steam or by an older launch.ps1, with no pid
            # file. Only when a single unaccounted game process exists is it safe to say which.
            $untracked = Get-UntrackedProcesses
            if ($untracked.Count -eq 1) { $proc = $untracked[0] }
            elseif ($untracked.Count -gt 1) {
                Write-Host "Instance 0: $($untracked.Count) untracked $ProcessName processes; will not force-stop any of them."
            }
        }
        if (-not $proc -and -not $answering) {
            Write-Host "Instance ${n}: not running."
            Remove-Item (Get-PidFile $n) -ErrorAction SilentlyContinue
            continue
        }
        if ($answering) {
            try { Invoke-WebRequest -Uri "http://127.0.0.1:$port/act" -Method Post -Body 'quit' -UseBasicParsing -TimeoutSec 10 | Out-Null } catch { }
        }
        $pending += [pscustomobject]@{ N = $n; Port = $port; Proc = $proc; Asked = $answering }
    }

    $deadline = (Get-Date).AddSeconds(20)
    foreach ($item in $pending) {
        $n = $item.N
        if ($item.Proc) {
            # A game that was never asked (bridge not up) has nothing to wait for.
            while ($item.Asked -and (Get-Date) -lt $deadline -and -not $item.Proc.HasExited) { Start-Sleep -Milliseconds 300 }
            if ($item.Proc.HasExited) { Write-Host "Instance ${n}: closed." }
            else {
                Stop-Process -Id $item.Proc.Id -Force -Confirm:$false
                Write-Host "Instance ${n}: did not exit on its own; stopped process $($item.Proc.Id)."
            }
        }
        else {
            # Answering, but which process it is cannot be told: wait, never guess at a pid to kill.
            while ((Get-Date) -lt $deadline -and (Test-Bridge $item.Port)) { Start-Sleep -Milliseconds 300 }
            if (Test-Bridge $item.Port) { Write-Host "Instance ${n}: still answering on port $($item.Port) and its process is unknown; close it by hand." }
            else { Write-Host "Instance ${n}: closed." }
        }
        Remove-Item (Get-PidFile $n) -ErrorAction SilentlyContinue
    }
}

if ($Stop) {
    if ($All) { Stop-Instances (@(0) + @(Get-TrackedInstances | Where-Object { $_ -ne 0 }) | Sort-Object) }
    else { Stop-Instances @($Instance) }
    return
}

# ---- launch

$instPort = Get-InstancePort $Instance
$health = Test-Bridge $instPort
if ($health) { Write-Host "Already running: $health"; return }

$existing = Get-TrackedProcess $Instance
if (-not $existing -and $Instance -eq 0) { $existing = Get-UntrackedProcesses | Select-Object -First 1 }
if ($existing) {
    throw "Slay the Spire 2 (instance $Instance, process $($existing.Id)) is already running but the bridge is not answering on port $instPort. Close it and retry."
}
if (-not (Get-Process steam -ErrorAction SilentlyContinue)) { throw 'Steam is not running. Start Steam first.' }

$exe = Join-Path $GameDir "$ProcessName.exe"
if (-not (Test-Path $exe)) { throw "Game not found at '$exe'. Pass -GameDir." }

$instDir = Get-InstanceDir $Instance
New-Item -ItemType Directory -Force $instDir | Out-Null

$appData = $null
if ($Instance -ge 1) {
    # Ask the shell for the real folder: $env:APPDATA could itself have been redirected.
    $realUserDir = Join-Path ([Environment]::GetFolderPath('ApplicationData')) $ProcessName
    $appData = Join-Path $instDir 'appdata'
    $userDir = Join-Path $appData $ProcessName
    if (-not (Test-Path $userDir)) {
        if (-not (Test-Path $realUserDir)) { throw "No game user data at '$realUserDir'. Run the game once (and enable mods) before starting extra instances." }
        # Read-only on the source. Timestamps are kept, which is what the game's cloud sync
        # compares; the copy carries settings.save (the "mods enabled" consent) and the profiles.
        robocopy $realUserDir $userDir /E /XD logs shader_cache sentry vulkan /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying the user data to '$userDir' failed (robocopy exit $LASTEXITCODE)." }
        Write-Host "Instance ${Instance}: user data seeded at $userDir"
    }
}

# The child inherits this process's environment, so set what it needs and put
# everything back afterwards: this script may be running in the user's own shell.
$saved = @{ SteamAppId = $env:SteamAppId; SteamGameId = $env:SteamGameId; SPIREBRIDGE_PORT = $env:SPIREBRIDGE_PORT; APPDATA = $env:APPDATA; SPIREBRIDGE_NO_CLOUD = $env:SPIREBRIDGE_NO_CLOUD;
            SPIREBRIDGE_RENDER = $env:SPIREBRIDGE_RENDER; SPIREBRIDGE_RENDER_SIZE = $env:SPIREBRIDGE_RENDER_SIZE }
try {
    $env:SteamAppId = '2868840'
    $env:SteamGameId = '2868840'
    $env:SPIREBRIDGE_PORT = "$instPort"
    # Extra instances keep their saves local: they all share one Steam Cloud slot otherwise.
    if ($appData) { $env:APPDATA = $appData; $env:SPIREBRIDGE_NO_CLOUD = '1' }
    else { Remove-Item Env:\SPIREBRIDGE_NO_CLOUD -ErrorAction SilentlyContinue }
    Remove-Item Env:\SPIREBRIDGE_RENDER, Env:\SPIREBRIDGE_RENDER_SIZE -ErrorAction SilentlyContinue
    if ($Render) {
        $env:SPIREBRIDGE_RENDER = '1'
        $env:SPIREBRIDGE_RENDER_SIZE = $RenderSize
        $cmd = "`"$exe`" --windowed --resolution $RenderSize"
        if ($Movie) {
            $moviePath = [IO.Path]::GetFullPath($Movie)
            New-Item -ItemType Directory -Force (Split-Path $moviePath) | Out-Null
            $cmd += " --write-movie `"$moviePath`" --fixed-fps $Fps"
        }
        $proc = Start-OnHiddenDesktop $exe $cmd $GameDir
    }
    elseif ($Windowed) { $proc = Start-Process -FilePath $exe -WorkingDirectory $GameDir -PassThru }
    else { $proc = Start-Process -FilePath $exe -ArgumentList '--headless' -WorkingDirectory $GameDir -WindowStyle Hidden -PassThru }
}
finally {
    foreach ($name in $saved.Keys) {
        if ($null -eq $saved[$name]) { Remove-Item "Env:\$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:\$name" $saved[$name] }
    }
}

$started = ''
try { $started = $proc.StartTime.ToUniversalTime().ToString('o') } catch { }
Set-Content -Path (Get-PidFile $Instance) -Value @("$($proc.Id)", $started) -Encoding ascii

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    $health = Test-Bridge $instPort
    if ($health) { Write-Host $health; return }
    if ($proc.HasExited) {
        Remove-Item (Get-PidFile $Instance) -ErrorAction SilentlyContinue
        throw "The game (instance $Instance) exited during startup. See its log: $(if ($appData) { Join-Path $appData "$ProcessName\logs\godot.log" } else { Join-Path $env:APPDATA "$ProcessName\logs\godot.log" })"
    }
    Start-Sleep -Milliseconds 1000
}
throw "The bridge did not answer on port $instPort within $TimeoutSeconds s. Is the SpireBridge mod installed (.\build.ps1) and mod loading enabled in the game?"
