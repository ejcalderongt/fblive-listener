param(
    [Parameter(Mandatory=$false)]
    [string]$LiveId,

    [Parameter(Mandatory=$false)]
    [string]$LiveUrl,

    [Parameter(Mandatory=$false)]
    [string]$SessionKey = "ac4cc282c27945ce",

    [Parameter(Mandatory=$false)]
    [string]$ApiUrl = "https://gabolive.replit.app",

    [Parameter(Mandatory=$false)]
    [string]$Platform = "facebook",

    [Parameter(Mandatory=$false)]
    [int]$WsPort = 8765,

    [Parameter(Mandatory=$false)]
    [switch]$UseCDP,

    [Parameter(Mandatory=$false)]
    [string]$CdpUrl = "http://127.0.0.1:9222",

    [Parameter(Mandatory=$false)]
    [switch]$UseCurrentTab,

    [Parameter(Mandatory=$false)]
    [switch]$Headless
)

$ErrorActionPreference = "Stop"

# Ejecutar relativo a este script
Set-Location -Path $PSScriptRoot

function Resolve-LiveUrl {
    param([string]$Id, [string]$Url)

    if ($Url -and $Url.Trim().Length -gt 0) {
        return $Url.Trim()
    }

    if ($Id -and $Id.Trim().Length -gt 0) {
        return "https://www.facebook.com/724698819/videos/$($Id.Trim())"
    }

    throw "Debes pasar -LiveId o -LiveUrl"
}

function Test-TcpPortFree {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    return -not $conn
}

function Stop-PortProcess {
    param([int]$Port)

    $conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
             Select-Object -ExpandProperty OwningProcess -Unique

    if ($conns) {
        foreach ($pid in $conns) {
            try {
                Stop-Process -Id $pid -Force -ErrorAction Stop
                Write-Host "[INFO] Proceso en puerto $Port detenido (PID $pid)"
            } catch {
                Write-Host "[WARN] No pude detener PID $pid en puerto $Port: $($_.Exception.Message)"
            }
        }
        Start-Sleep -Seconds 1
    }
}

$liveUrl = Resolve-LiveUrl -Id $LiveId -Url $LiveUrl
$workspace = $PSScriptRoot
$listenerScript = Join-Path $workspace "fb_live_listener.py"
$bridgeScript = Join-Path $workspace "liveshopgt_bridge.py"

if (-not (Test-Path $listenerScript)) { throw "No existe: $listenerScript" }
if (-not (Test-Path $bridgeScript)) { throw "No existe: $bridgeScript" }

Write-Host "============================================================"
Write-Host " Live Listener Launcher"
Write-Host "============================================================"
Write-Host "[INFO] URL Live: $liveUrl"
Write-Host "[INFO] SessionKey: $SessionKey"
Write-Host "[INFO] API: $ApiUrl"
Write-Host "[INFO] WS: ws://127.0.0.1:$WsPort"
Write-Host "[INFO] Plataforma: $Platform"

# Limpieza de puertos
Stop-PortProcess -Port $WsPort

# Construir args listener
$listenerArgs = @(
    $listenerScript,
    "--url", $liveUrl,
    "--port", $WsPort
)

if ($UseCDP) {
    $listenerArgs += @("--cdp-url", $CdpUrl)
    if ($UseCurrentTab) {
        $listenerArgs += "--use-current-tab"
    }
}

if ($Headless) {
    $listenerArgs += "--headless"
}

# Arrancar listener
Write-Host "[INFO] Iniciando listener..."
$listenerProc = Start-Process -FilePath "python" -ArgumentList $listenerArgs -WorkingDirectory $workspace -PassThru -WindowStyle Normal

# Esperar que abra WS
$maxWait = 20
$ok = $false
for ($i = 1; $i -le $maxWait; $i++) {
    Start-Sleep -Seconds 1
    $wsConn = Get-NetTCPConnection -LocalPort $WsPort -State Listen -ErrorAction SilentlyContinue
    if ($wsConn) {
        $ok = $true
        break
    }
    if ($listenerProc.HasExited) {
        break
    }
}

if (-not $ok) {
    if ($listenerProc.HasExited) {
        throw "Listener terminó prematuramente (exit=$($listenerProc.ExitCode)). Revisa esa ventana."
    }
    throw "Listener no abrió ws://127.0.0.1:$WsPort en $maxWait s."
}

Write-Host "[INFO] Listener activo (PID $($listenerProc.Id))."

# Arrancar bridge
$bridgeArgs = @(
    $bridgeScript,
    "--session-key", $SessionKey,
    "--api-url", $ApiUrl,
    "--ws-url", "ws://127.0.0.1:$WsPort",
    "--platform", $Platform
)

Write-Host "[INFO] Iniciando bridge..."
$bridgeProc = Start-Process -FilePath "python" -ArgumentList $bridgeArgs -WorkingDirectory $workspace -PassThru -WindowStyle Normal

Write-Host "[OK] Listener + Bridge iniciados"
Write-Host "[OK] Listener PID: $($listenerProc.Id)"
Write-Host "[OK] Bridge   PID: $($bridgeProc.Id)"
Write-Host ""
Write-Host "Para detener:"
Write-Host "  Stop-Process -Id $($listenerProc.Id),$($bridgeProc.Id) -Force"
