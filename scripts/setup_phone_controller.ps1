<#
.SYNOPSIS
    Setup JARVIS Phone Controller for LAN access
.DESCRIPTION
    This script helps configure the CHIDVI-556 dashboard for phone control over LAN.
    It finds your LAN IP, generates TLS certificates, and sets the required environment variable.
#>

param(
    [Parameter(Mandatory=$false)]
    [string]$LanIP = "",

    [Parameter(Mandatory=$false)]
    [switch]$ShowOnly
)

# Set working directory to project root
$ProjectRoot = "A:\Show case projects\CHIDVI-556"
Set-Location $ProjectRoot

Write-Host "=== JARVIS Phone Controller LAN Setup ===" -ForegroundColor Cyan
Write-Host ""

# Step 1: Find LAN IP addresses
Write-Host "Step 1: Detecting LAN IP addresses..." -ForegroundColor Yellow
$lanAddresses = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object InterfaceAlias, IPAddress, PrefixLength

if (-not $lanAddresses) {
    Write-Error "No LAN IP addresses found. Are you connected to a network?"
    exit 1
}

$lanAddresses | Format-Table -AutoSize
Write-Host ""

# If no IP provided, let user choose
if (-not $LanIP) {
    if ($lanAddresses.Count -eq 1) {
        $LanIP = $lanAddresses[0].IPAddress
        Write-Host "Auto-selected: $LanIP" -ForegroundColor Green
    } else {
        Write-Host "Multiple LAN interfaces detected. Please choose one:" -ForegroundColor Yellow
        for ($i = 0; $i -lt $lanAddresses.Count; $i++) {
            $addr = $lanAddresses[$i]
            Write-Host "  [$i] $($addr.InterfaceAlias) - $($addr.IPAddress)"
        }
        $choice = Read-Host "Enter number (0-$($lanAddresses.Count-1))"
        if ($choice -match '^\d+$' -and [int]$choice -lt $lanAddresses.Count) {
            $LanIP = $lanAddresses[[int]$choice].IPAddress
        } else {
            Write-Error "Invalid selection"
            exit 1
        }
    }
}

Write-Host "Using LAN IP: $LanIP" -ForegroundColor Green
Write-Host ""

# Validate it's a private IP
$ip = [System.Net.IPAddress]::Parse($LanIP)
$bytes = $ip.GetAddressBytes()
$isPrivate = ($bytes[0] -eq 10) -or
             ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
             ($bytes[0] -eq 192 -and $bytes[1] -eq 168)

if (-not $isPrivate) {
    Write-Error "IP $LanIP is not a private LAN address (must be 10.x, 172.16-31.x, or 192.168.x)"
    exit 1
}

# Check if IP is actually assigned to this machine
$localAddresses = Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress
if ($LanIP -notin $localAddresses) {
    Write-Error "IP $LanIP is not assigned to this machine"
    exit 1
}

# Step 2: Check network profile
Write-Host "Step 2: Checking network profile..." -ForegroundColor Yellow
$profiles = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -eq 'Internet' -or $_.IPv4Connectivity -eq 'LocalNetwork' }
$profiles | Format-Table InterfaceAlias, NetworkCategory, IPv4Connectivity -AutoSize
Write-Host ""

# Step 3: Check port 8000
Write-Host "Step 3: Checking port 8000..." -ForegroundColor Yellow
$port8000 = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
if ($port8000) {
    Write-Host "Port 8000 is in use:" -ForegroundColor Yellow
    $port8000 | Format-Table LocalAddress, State, OwningProcess -AutoSize
    Write-Host ""
    Write-Warning "If CHIDVI is already running, stop it before generating certificates."
} else {
    Write-Host "Port 8000 is free." -ForegroundColor Green
}
Write-Host ""

# Step 4: Check existing certificates
Write-Host "Step 4: Checking existing certificates..." -ForegroundColor Yellow
$certsDir = "$ProjectRoot\config\certs"
$keyPath = "$certsDir\jarvis.key"
$crtPath = "$certsDir\jarvis.crt"
$caPath = "$certsDir\controller-ca.crt"

if (Test-Path $keyPath -and Test-Path $crtPath) {
    Write-Host "Certificates already exist:" -ForegroundColor Green
    Write-Host "  $keyPath"
    Write-Host "  $crtPath"
    if (Test-Path $caPath) {
        Write-Host "  $caPath"
    }
    Write-Host ""
    Write-Host "To regenerate, delete these files first or run the certificate helper manually." -ForegroundColor Yellow
} else {
    Write-Host "No certificates found. Will generate new ones." -ForegroundColor Yellow
}
Write-Host ""

if ($ShowOnly) {
    Write-Host "=== Setup Complete (ShowOnly mode) ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "To run JARVIS with phone controller:"
    Write-Host "  `$env:CHIDVI_CONTROLLER_HOST = '$LanIP'" -ForegroundColor Green
    Write-Host "  `.\.venv\Scripts\python.exe main.py`" -ForegroundColor Green
    Write-Host ""
    Write-Host "Then on your phone:"
    Write-Host "  1. Copy $certsDir\controller-ca.crt to phone"
    Write-Host "  2. Install as CA certificate in Android settings"
    Write-Host "  3. Open https://$LanIP:8000/login"
    Write-Host "  4. In desktop app, click Remote Control > Scan QR > Pair"
    exit 0
}

# Step 5: Generate certificates
Write-Host "Step 5: Generating TLS certificates..." -ForegroundColor Yellow
try {
    & .\.venv\Scripts\python.exe -m dashboard.certificates $LanIP
    Write-Host "Certificates generated successfully!" -ForegroundColor Green
    Write-Host "  CA SHA-256 fingerprint shown above - verify this on your phone." -ForegroundColor Cyan
} catch {
    Write-Error "Certificate generation failed: $_"
    exit 1
}
Write-Host ""

# Step 6: Verify certificates
Write-Host "Step 6: Verifying certificates..." -ForegroundColor Yellow
if (Test-Path $keyPath -and Test-Path $crtPath -and Test-Path $caPath) {
    Write-Host "All certificate files created:" -ForegroundColor Green
    Write-Host "  $keyPath (keep private!)"
    Write-Host "  $crtPath"
    Write-Host "  $caPath (copy to phone)"
} else {
    Write-Error "Some certificate files are missing"
    exit 1
}
Write-Host ""

# Step 7: Check firewall
Write-Host "Step 7: Checking Windows Firewall rules..." -ForegroundColor Yellow
$fwRules = Get-NetFirewallRule -DisplayName '*JARVIS*','*CHIDVI*' -ErrorAction SilentlyContinue
if ($fwRules) {
    Write-Host "Existing firewall rules found:" -ForegroundColor Yellow
    $fwRules | Select-Object DisplayName, Enabled, Profile, Direction, Action | Format-Table -AutoSize
} else {
    Write-Host "No JARVIS/CHIDVI firewall rules found." -ForegroundColor Yellow
    Write-Host "You may need to allow inbound TCP 8000 on Private profile for your subnet." -ForegroundColor Cyan
}
Write-Host ""

# Final instructions
Write-Host "=== SETUP COMPLETE ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "To start JARVIS with phone controller:" -ForegroundColor Green
Write-Host "  `$env:CHIDVI_CONTROLLER_HOST = '$LanIP'" -ForegroundColor White
Write-Host "  `.\.venv\Scripts\python.exe main.py`" -ForegroundColor White
Write-Host ""
Write-Host "On your Android phone:" -ForegroundColor Green
Write-Host "  1. Transfer $caPath to your phone (email, USB, etc.)" -ForegroundColor White
Write-Host "  2. Install as CA certificate: Settings > Security > Install certificate > CA certificate" -ForegroundColor White
Write-Host "  3. Verify the certificate name: 'CHIDVI personal controller CA'" -ForegroundColor White
Write-Host "  4. Open Chrome and go to: https://$LanIP:8000/login" -ForegroundColor White
Write-Host "  5. In JARVIS desktop app: Click 'Remote Control' button" -ForegroundColor White
Write-Host "  6. Scan QR code with phone camera, tap 'Pair with laptop'" -ForegroundColor White
Write-Host ""
Write-Host "Troubleshooting:" -ForegroundColor Yellow
Write-Host "  - If phone can't connect: Check Windows Firewall (allow TCP 8000 on Private profile)" -ForegroundColor White
Write-Host "  - If certificate error: Verify phone clock is correct, CA is installed, IP matches" -ForegroundColor White
Write-Host "  - If 'local-only' error: Ensure CHIDVI_CONTROLLER_HOST is set BEFORE starting JARVIS" -ForegroundColor White
Write-Host "  - The env var must be set in the SAME PowerShell window that runs JARVIS" -ForegroundColor White