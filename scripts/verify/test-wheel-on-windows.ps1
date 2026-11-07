#Requires -Version 5.1
<#
  test-wheel-on-windows.ps1

  Answers ONE question on a real Windows machine:
    "does the win_amd64 wheel install, and does the product run?"

  THIS IS NOT AN INSTALLER, AND IT DOES NOT TOUCH YOUR EXISTING INSTALL.

  The machine running this may already have a working hyprial. So every write is
  redirected into a temporary directory:

  A plain venv under %TEMP%\hyprial-wheel-test-<stamp>\venv, and the entry point
  is invoked by explicit path from %TEMP%\...\venv\Scripts. The bare `hyprial`
  command is never used for the wheel under test, uv's tool directory is never
  redirected, and nothing that was already installed is replaced or removed.

  (An earlier version redirected UV_TOOL_DIR instead. On Windows 11 / uv 0.11.2
  that produced a tool environment with no Scripts\python.exe, so the generated
  launcher failed with "uv trampoline failed to canonicalize script path" -- and
  it also changed what `uv tool list` reported, which contaminated the
  production-untouched comparison with the test's own environment.)

  It also records the existing install's fingerprint before and after, so that
  "nothing was disturbed" is a measurement rather than a promise.

  It does not start or stop a daemon. `hyprial ps` is a read.

  Usage:
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1 -Tag nightly
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1 -KeepArtifacts

  Exit codes (same table as the installer, so the numbers mean the same thing):
    0   every step PASS
    1   a step FAILed
    2   NOT EVALUABLE: a prerequisite is missing; nothing was tested
#>

param(
  [string]$Tag = "stable",
  [switch]$KeepArtifacts
)

# PS 5.1 turns a native command's stderr into a terminating error when this is
# "Stop" (measured on Windows 11 / PowerShell 5.1.26100: `uv`'s harmless
# "--no-project was provided" warning aborted the whole run). So: Continue, and
# every native call is checked through $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

function Invoke-Native {
  # Returns stdout as one string and sets $script:NativeExit. stderr is captured
  # separately so a warning can be reported without becoming fatal.
  param([string]$Exe, [string[]]$Arguments)
  $out = & $Exe @Arguments 2>&1
  $script:NativeExit = $LASTEXITCODE
  $text = ($out | Out-String)
  return $text.Trim()
}

$script:failures = 0
$script:unevaluable = 0

function Say([string]$kind, [string]$what, [string]$detail) {
  # Kind first so the output is greppable; the detail carries the value, because a
  # verdict without its number is not a reading.
  Write-Output ("{0,-14} {1,-26} {2}" -f $kind, $what, $detail)
  if ($kind -eq "FAIL") { $script:failures++ }
  if ($kind -eq "NOT-EVALUABLE") { $script:unevaluable++ }
}

$root = Join-Path $env:TEMP ("hyprial-wheel-test-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$script:root = $root

function Finish {
  # One exit path. An early `exit N` used to bypass cleanup, and the first real
  # run left its directory behind because of it (reported: two of them).
  param([int]$Code)
  if ($KeepArtifacts) {
    Say "INFO" "cleanup" "kept on purpose: $script:root"
  } else {
    try {
      Remove-Item -Recurse -Force $script:root -ErrorAction Stop
      Say "PASS" "cleanup" "removed $script:root"
    } catch {
      Say "FAIL" "cleanup" "could not remove $script:root : $($_.Exception.Message) -- delete it by hand"
    }
  }
  exit $Code
}
$work = Join-Path $root "download"
New-Item -ItemType Directory -Force -Path $work | Out-Null

Say "INFO" "started_at" ((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"))
Say "INFO" "tag" $Tag
Say "INFO" "isolated_root" $root
Say "INFO" "powershell" $PSVersionTable.PSVersion.ToString()
Say "INFO" "os" ((Get-CimInstance Win32_OperatingSystem).Caption + " / " + [Environment]::OSVersion.Version)
Say "INFO" "os_arch_env" "$($env:PROCESSOR_ARCHITECTURE) (wow64: $($env:PROCESSOR_ARCHITEW6432))"

# ------------------------------------------------------ fingerprint what exists
# The pre-existing install is the thing at risk. Record it before touching
# anything, so the after-state can be compared instead of trusted.
$preVersion = "absent"
$preTools = "absent"
if (Get-Command hyprial -ErrorAction SilentlyContinue) {
  $preVersion = ((Invoke-Native "hyprial" @("version")) -split "`r?`n" | Select-Object -First 1)
  if ($script:NativeExit -ne 0) { $preVersion = "exit=$($script:NativeExit)" }
  $prePath = (Get-Command hyprial).Source
  Say "INFO" "existing_hyprial" "$prePath :: $preVersion"
} else {
  Say "INFO" "existing_hyprial" "not on PATH (the test does not need it)"
}
function Get-ToolList {
  # "No tools installed" goes to stderr with a non-zero exit. That is the empty
  # list, not a failure; the first run recorded it as `unavailable: exit=1` and
  # printed a NativeCommandError, which is noise the reader has to discount.
  $text = (Invoke-Native "uv" @("tool", "list"))
  $code = $script:NativeExit
  if ($text -match "No tools installed") { return "(none)" }
  if ($code -ne 0) { return "unavailable: exit=$code" }
  return ($text -replace "`r?`n", " ; ")
}
$preTools = (Get-ToolList)
Say "INFO" "uv_tool_list_before" $preTools

# ---------------------------------------------------------------- prerequisite
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "NOT-EVALUABLE" "uv" "uv is not on PATH"
  Finish 2
}
Say "PASS" "uv" ((& uv --version) -join " ")

# The interpreter's platform, not the machine's: on ARM64 Windows an x64 Python can
# only accept win_amd64, and the machine's architecture would point at a wheel that
# does not exist (same trap as macOS + Rosetta).
# Ask uv which interpreter it would use, then ask that interpreter. Falling back to
# uv's own build triple is honest: it states the architecture of the python uv
# prefers, which is exactly what decides whether win_amd64 can be installed.
# stderr is merged into the captured text, so keep the last line that looks like an
# executable path; a warning line is not a path.
$pyFindRaw = (Invoke-Native "uv" @("python", "find"))
$pyFindExit = $script:NativeExit
$pyExe = ($pyFindRaw -split "`r?`n" | Where-Object { $_ -match '\.exe$' } | Select-Object -Last 1)
if (-not $pyExe) { $pyExe = ($pyFindRaw -split "`r?`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 1) }
$pyPlat = ""
if ($pyFindExit -eq 0 -and $pyExe) {
  $pyPlat = (Invoke-Native $pyExe @("-c", "import sysconfig;print(sysconfig.get_platform())"))
  if ($script:NativeExit -ne 0) { $pyPlat = "" }
}
if (-not $pyPlat) {
  $uvTriple = (Invoke-Native "uv" @("--version"))
  if ($uvTriple -match "(x86_64|aarch64|i686)-pc-windows") {
    $pyPlat = "uv-triple:$($Matches[0])"
    Say "INFO" "python_platform" "$pyPlat (from the uv build itself; the interpreter could not be asked)"
  } else {
    Say "NOT-EVALUABLE" "python_platform" "neither uv python find nor uv --version gave a platform"
    Finish 2
  }
} else {
  Say "PASS" "python_platform" $pyPlat
}

# -------------------------------------------------------------------- selection
$sumsUrl = "https://github.com/Hyprial/hyprial/releases/download/$Tag/SHA256SUMS"
$sumsFile = Join-Path $work "SHA256SUMS"
try {
  Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsFile -UseBasicParsing
} catch {
  Say "NOT-EVALUABLE" "sums_download" "$sumsUrl : $($_.Exception.Message)"
  Finish 2
}
$sumsSha = (Get-FileHash -Algorithm SHA256 -Path $sumsFile).Hash.ToLower()
Say "PASS" "sums_download" "$sumsUrl ($((Get-Item $sumsFile).Length) bytes) sha256=$sumsSha"

$lines = @(Get-Content -Path $sumsFile | Where-Object { $_.Trim() -ne "" })
Say "INFO" "sums_entries" "$($lines.Count)"

# There is no win_arm64 wheel. Saying so is part of the answer.
# Field-based, not regex-on-the-whole-line: the first real run proved the point.
# `\swin_amd64\.whl$` requires whitespace before win_amd64, but the filename is
# `...-py3-none-win_amd64.whl` -- a hyphen. Zero matches, and the probe exited
# NOT-EVALUABLE on a selection bug rather than a missing wheel.
$candidates = @($lines | Where-Object { (($_ -split '\s+')[1]) -like '*-win_amd64.whl' })
if ($candidates.Count -eq 0) {
  Say "NOT-EVALUABLE" "candidate" ("SHA256SUMS has no win_amd64 entry; published: " + (($lines | ForEach-Object { ($_ -split '\s+')[1] }) -join ", "))
  Finish 2
}
if ($candidates.Count -gt 1) {
  # Two matches is not "take the first": that turns an ambiguity into a silent answer.
  Say "FAIL" "candidate" "$($candidates.Count) win_amd64 entries, ambiguous: $($candidates -join ' | ')"
  Finish 1
}
$expectedSha = ($candidates[0] -split '\s+')[0].ToLower()
$fileName = ($candidates[0] -split '\s+')[1]
Say "PASS" "candidate" "$fileName expected_sha256=$expectedSha"

# The literal '+' in the local version segment must be percent-encoded in the URL.
$encoded = $fileName.Replace('+', '%2B')
$wheelUrl = "https://github.com/Hyprial/hyprial/releases/download/$Tag/$encoded"
$wheelFile = Join-Path $work $fileName
try {
  Invoke-WebRequest -Uri $wheelUrl -OutFile $wheelFile -UseBasicParsing
} catch {
  Say "FAIL" "wheel_download" "$wheelUrl : $($_.Exception.Message)"
  Finish 1
}
$actualSha = (Get-FileHash -Algorithm SHA256 -Path $wheelFile).Hash.ToLower()
$size = (Get-Item $wheelFile).Length
if ($actualSha -ne $expectedSha) {
  Say "FAIL" "wheel_hash" "expected=$expectedSha actual=$actualSha bytes=$size"
  Finish 1
}
Say "PASS" "wheel_hash" "sha256=$actualSha bytes=$size"

# ------------------------------------------------- ISOLATED install (no prod)
# A plain venv under %TEMP%, not uv's tool layer. Two measured reasons:
#   1. With UV_TOOL_DIR redirected, uv 0.11.2 on Windows 11 produced a tool env
#      with no Scripts\python.exe, and the generated hyprial.exe failed with
#      "uv trampoline failed to canonicalize script path". The wheel was fine
#      (hash PASS, dist-info version 0.5.4+g029f56d); the launcher was not.
#   2. Redirecting the tool dir also changed what `uv tool list` reports, so my
#      own before/after comparison saw the test's environment as if it were
#      production. The measurement was contaminated by the thing being measured.
# A venv avoids both: uv's tool list is untouched, and nothing is launcher-mediated.
$venv = Join-Path $root "venv"
& uv venv $venv *> $null
if ($LASTEXITCODE -ne 0) {
  Say "FAIL" "venv_create" "uv venv exit=$LASTEXITCODE under $venv"
  Finish 1
}
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Say "FAIL" "venv_create" "no Scripts\python.exe in $venv"
  Finish 1
}
Say "PASS" "venv_create" $venvPy

& uv pip install --python $venvPy $wheelFile *> $null
if ($LASTEXITCODE -ne 0) {
  Say "FAIL" "wheel_install" "uv pip install exit=$LASTEXITCODE"
  Finish 1
}
Say "PASS" "wheel_install" "exit=0 (into the isolated venv)"

$exe = Join-Path $venv "Scripts\hyprial.exe"
if (-not (Test-Path $exe)) { $exe = Join-Path $venv "Scripts\hyprial-script.py" }
if (-not (Test-Path $exe)) {
  Say "FAIL" "binary_present" "no hyprial entry point under $venv\Scripts"
  Finish 1
}
Say "PASS" "binary_present" $exe

# ----------------------------------------------------------------- three readings
$distInfo = Get-ChildItem -Path $venv -Recurse -Filter "hyprial-*.dist-info" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
if ($distInfo) {
  $installedVersion = $distInfo.Name -replace '^hyprial-', '' -replace '\.dist-info$', ''
  Say "PASS" "installed_version" "$installedVersion (from dist-info, not from the URL)"
} else {
  Say "NOT-EVALUABLE" "installed_version" "no dist-info under $venv"
}

# `hyprial --version` does not exist and still exits 0 with empty stdout.
$versionOut = (Invoke-Native $exe @("version"))
if ($script:NativeExit -eq 0 -and $versionOut.Length -gt 0) {
  Say "PASS" "hyprial_version" "exit=0 non-empty"
  Write-Output ($versionOut -split "`r?`n" | Select-Object -First 8)
} else {
  Say "FAIL" "hyprial_version" "exit=$($script:NativeExit) length=$($versionOut.Length)"
}

# Read-only. This talks to whatever daemon is already there; it starts nothing.
$psOut = (Invoke-Native $exe @("ps"))
Say "INFO" "ps_exit" "$($script:NativeExit)"
Write-Output (($psOut -split "`r?`n" | Select-Object -First 10) -join "`n")
if ($psOut -match "running:\s*true") {
  Say "PASS" "daemon_visible" "an existing daemon answered (nothing was started by this test)"
} else {
  Say "INFO" "daemon_visible" "no daemon answered; that is not a wheel failure, only a missing round trip"
}

# ------------------------------------------------------------------- no pollution
$postTools = (Get-ToolList)
if ($postTools -eq $preTools) {
  Say "PASS" "prod_untouched" "uv tool list is unchanged"
} else {
  Say "FAIL" "prod_untouched" "before=[$preTools] after=[$postTools]"
}
$postVersion = if (Get-Command hyprial -ErrorAction SilentlyContinue) {
  ((Invoke-Native "hyprial" @("version")) -split "`r?`n" | Select-Object -First 1)
} else { "absent" }
if ($postVersion -eq $preVersion) {
  Say "PASS" "prod_version" "existing hyprial version unchanged ($postVersion)"
} else {
  Say "FAIL" "prod_version" "before=[$preVersion] after=[$postVersion]"
}

# ---------------------------------------------------------------- cleanup + verdict
# Only the isolated directory is removed. --force and uninstall are never pointed
# at the real tool directory.
Say "INFO" "finished_at" ((Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ"))
if ($script:failures -gt 0) { Say "VERDICT" "result" "FAIL ($script:failures failing step(s)); paste everything above"; Finish 1 }
if ($script:unevaluable -gt 0) { Say "VERDICT" "result" "NOT-EVALUABLE ($script:unevaluable step(s) could not run); paste everything above"; Finish 2 }
Say "VERDICT" "result" "PASS on $Tag for $pyPlat; production install untouched; paste everything above"
Finish 0
