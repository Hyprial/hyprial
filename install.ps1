# Public Windows amd64 wheel installer. HYPRIAL_DL_BASE is for isolated tests.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$temp = $null
$receiptTemp = $null
$oldUnmanagedInstall = $env:UV_UNMANAGED_INSTALL
try {
    $track = $env:HYPRIAL_TRACK
    if ($null -eq $track) { $track = 'stable' }
    if ($track -cnotmatch '^(stable|nightly|internal)$') {
        throw "Invalid HYPRIAL_TRACK: $track (use stable, nightly or internal)"
    }
    $base = $env:HYPRIAL_DL_BASE
    if ($null -eq $base) { $base = 'https://dl.hyprial.com' }
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or $arch -ne 'AMD64') {
        throw "Unsupported platform: Windows amd64 required (architecture: $arch)"
    }
    # Windows PowerShell uses .NET defaults that can otherwise select old TLS.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('hyprial-install-' + [Guid]::NewGuid())
    New-Item -ItemType Directory -Path $temp | Out-Null
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $uvDir = $env:UV_INSTALL_DIR
        if (-not $uvDir) { $uvDir = Join-Path $HOME '.local/bin' }
        Write-Output "Installing uv from https://astral.sh/uv/install.ps1 into $uvDir (no shell profile changes)"
        $uvScript = Join-Path $temp 'uv-install.ps1'
        try {
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvScript -TimeoutSec 120
            $env:UV_UNMANAGED_INSTALL = $uvDir
            & ([ScriptBlock]::Create([IO.File]::ReadAllText($uvScript)))
            $env:PATH = "$uvDir;$env:PATH"
        } catch {
            throw 'uv bootstrap failed; see https://docs.astral.sh/uv/'
        }
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'uv bootstrap finished but uv is unavailable; see https://docs.astral.sh/uv/'
        }
    }
    $url = $base.TrimEnd('/') + '/wheel/' + $track
    $sumsPath = Join-Path $temp 'SHA256SUMS'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$url/SHA256SUMS" -OutFile $sumsPath -TimeoutSec 120
    } catch {
        $response = $_.Exception.PSObject.Properties['Response']
        if ($response -and $response.Value -and [int]$response.Value.StatusCode -eq 404) {
            throw "SHA256SUMS missing for track $track (HTTP 404); ask infra-ops to mirror this track."
        }
        throw "SHA256SUMS download failed for track $track"
    }
    $candidates = @(
        foreach ($line in (Get-Content -LiteralPath $sumsPath)) {
            if ($line -cmatch '^([0-9a-fA-F]{64})\s+\*?(hyprial-[0-9][A-Za-z0-9.!+_]*-py3-none-win_amd64\.whl)\s*$') {
                [PSCustomObject]@{ Hash = $Matches[1]; Name = $Matches[2] }
            }
        }
    )
    if ($candidates.Count -ne 1) {
        throw "Expected exactly one wheel for Windows amd64 in track $track; found $($candidates.Count)"
    }
    $wheel = $candidates[0].Name
    $wheelPath = Join-Path $temp $wheel
    $encodedWheel = [Uri]::EscapeDataString($wheel)
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$url/$encodedWheel" -OutFile $wheelPath -TimeoutSec 300
    } catch {
        throw "Wheel download failed: $wheel"
    }
    $actual = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash
    if ($actual -ne $candidates[0].Hash) {
        throw "SHA-256 mismatch: $wheel (nothing installed)"
    }
    Write-Output "Installing verified $wheel from track $track with uv tool install --force"
    & uv tool install --force $wheelPath
    if ($LASTEXITCODE -ne 0) { throw 'uv tool install failed' }
    $version = $wheel -replace '^hyprial-', '' -replace '-py3-none-.*$', ''
    $installedList = (& uv tool list) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read installed version with uv tool list' }
    $installed = ''
    if ($installedList -match '(?m)^hyprial v(\S+)\s*$') { $installed = $Matches[1] }
    if ($installed -cne $version) {
        throw "Installed version mismatch: selected $version, installed $installed"
    }
    $config = $env:XDG_CONFIG_HOME
    if (-not $config) { $config = Join-Path $HOME '.config' }
    $receiptDir = Join-Path $config 'hyprial'
    New-Item -ItemType Directory -Force -Path $receiptDir | Out-Null
    $receipt = @{ schema = 1; install = @{
        track = $track; method = 'wheel'; installed_at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        selected_version = $version; installed_version = $installed; wheel_version = $installed
        wheel_asset_name = $wheel; wheel_sha256 = $actual.ToLowerInvariant()
        wheel_sha256_index = $candidates[0].Hash.ToLowerInvariant(); wheel_sha256_match = 'true'
        digest_verification = 'pass'; degraded = 'false'
    } } | ConvertTo-Json -Depth 3 -Compress
    $receiptPath = Join-Path $receiptDir 'install.json'
    $receiptTemp = Join-Path $receiptDir ('.install-' + [Guid]::NewGuid())
    [IO.File]::WriteAllText($receiptTemp, $receipt, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $receiptTemp -Destination $receiptPath -Force
    Write-Output "Installed hyprial $installed"
    Write-Output "Receipt: $receiptPath"
    Write-Output 'If hyprial is not on PATH, use uv tool update-shell, then open a new terminal.'
} catch {
    Write-Error "hyprial install: $($_.Exception.Message)" -ErrorAction Continue
    exit 1
} finally {
    $env:UV_UNMANAGED_INSTALL = $oldUnmanagedInstall
    if ($receiptTemp -and (Test-Path -LiteralPath $receiptTemp)) {
        Remove-Item -LiteralPath $receiptTemp -Force
    }
    if ($temp -and (Test-Path -LiteralPath $temp)) {
        Remove-Item -LiteralPath $temp -Recurse -Force
    }
}
