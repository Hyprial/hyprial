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

  `hyprial ps` is a read against whatever daemon already runs. The GUI steps
  (skip them with -SkipGui) start `hyprial gui` on a separate isolated home under
  the same temporary root; that launch starts a bootstrap daemon for the isolated
  home only, and the script stops exactly that daemon with `daemon stop --json`.

  Usage:
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1 -Tag nightly
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1 -KeepArtifacts
    powershell -ExecutionPolicy Bypass -File test-wheel-on-windows.ps1 -SkipGui

  Exit codes (same table as the installer, so the numbers mean the same thing):
    0   every step PASS
    1   a step FAILed
    2   NOT EVALUABLE: a prerequisite is missing; nothing was tested

  The GUI acceptance steps cover the portable runtime. The single-instance lock
  uses the kernel ProcessFileLock and --stdin-stop does not require
  signal.pthread_kill. They mirror packaging/clients/verify_runtime.py with a
  scrubbed environment whose HOME, USERPROFILE, HYPRIAL_HOME,
  HARNESS_STATE_DIR and TEMP live under the isolated root, network isolation,
  and a throwaway node id. The isolated home starts a bootstrap daemon, which
  is stopped with daemon stop --json after the GUI closes. -SkipGui omits these
  steps.
#>

param(
  [string]$Tag = "stable",
  [switch]$KeepArtifacts,
  [switch]$SkipGui
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

function Get-IsolatedProcesses([string]$marker) {
  @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains($marker) })
}
function Test-Alive([int]$id) {
  return [bool](Get-Process -Id $id -ErrorAction SilentlyContinue)
}
function Start-Gui([string]$exePath, [hashtable]$environment, [string]$cwd) {
  $info = New-Object System.Diagnostics.ProcessStartInfo
  $info.FileName = $exePath
  $info.Arguments = "gui --no-open --json --port 0 --stdin-stop"
  $info.UseShellExecute = $false
  $info.RedirectStandardInput = $true
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  $info.WorkingDirectory = $cwd
  $info.EnvironmentVariables.Clear()
  foreach ($key in $environment.Keys) { $info.EnvironmentVariables[$key] = $environment[$key] }
  $process = [System.Diagnostics.Process]::Start($info)
  $stderrTask = $process.StandardError.ReadToEndAsync()
  Add-Member -InputObject $process -NotePropertyName StderrTask -NotePropertyValue $stderrTask -Force
  return $process
}
function Get-StreamResult($task, [int]$milliseconds = 2000) {
  if (-not $task) { return "" }
  try {
    if ($task.Wait($milliseconds)) { return [string]$task.GetAwaiter().GetResult() }
  } catch { }
  return ""
}
function Format-StreamTail([string]$text, [int]$maxChars = 600) {
  if (-not $text) { return "" }
  $flat = ($text.Trim() -replace "`r?`n", " | ")
  if ($flat.Length -le $maxChars) { return $flat }
  return "..." + $flat.Substring($flat.Length - $maxChars)
}
function Read-FirstLine($process, [int]$seconds) {
  $task = $process.StandardOutput.ReadLineAsync()
  Add-Member -InputObject $process -NotePropertyName FirstLineTask -NotePropertyValue $task -Force
  if ($task.Wait($seconds * 1000)) {
    $restTask = $process.StandardOutput.ReadToEndAsync()
    Add-Member -InputObject $process -NotePropertyName RestStdoutTask -NotePropertyValue $restTask -Force
    return $task.GetAwaiter().GetResult()
  }
  return $null
}
function Get-RecordedDaemonPids([string]$stateDir) {
  $found = @()
  foreach ($name in @("daemon.json", "bootstrap.json")) {
    $path = Join-Path $stateDir $name
    if (Test-Path $path) {
      try {
        $value = (Get-Content -Raw -Path $path | ConvertFrom-Json).pid
        if ($value -is [int] -or $value -is [long]) { if ($found -notcontains [int]$value) { $found += [int]$value } }
      } catch { }
    }
  }
  return $found
}

if ($SkipGui) {
  Say "INFO" "gui" "skipped on request (-SkipGui)"
} else {
  $guiRoot = Join-Path $root "g"
  $guiEnv = @{}
  foreach ($name in @("u", "h", "s", "t")) { New-Item -ItemType Directory -Force -Path (Join-Path $guiRoot $name) | Out-Null }
  foreach ($key in @("SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS")) {
    $value = [Environment]::GetEnvironmentVariable($key)
    if ($value) { $guiEnv[$key] = $value }
  }
  $guiEnv["PATH"] = (Join-Path $venv "Scripts") + ";" + [Environment]::GetEnvironmentVariable("SystemRoot") + "\System32"
  $guiEnv["HOME"] = (Join-Path $guiRoot "u")
  $guiEnv["USERPROFILE"] = (Join-Path $guiRoot "u")
  $guiEnv["HYPRIAL_HOME"] = (Join-Path $guiRoot "h")
  $guiEnv["HARNESS_STATE_DIR"] = (Join-Path $guiRoot "s")
  $guiEnv["TEMP"] = (Join-Path $guiRoot "t")
  $guiEnv["TMP"] = (Join-Path $guiRoot "t")
  $guiEnv["HYPRIAL_NETWORK_ISOLATED"] = "1"
  $guiEnv["HYPRIAL_USAGE_DISABLE"] = "1"
  $guiEnv["HYPRIAL_PEER_DISCOVERY"] = "0"
  $guiEnv["HYPRIAL_ZENOH_LISTEN"] = ""
  $guiEnv["HYPRIAL_ZENOH_CONNECT"] = ""
  $guiEnv["HYPRIAL_NODE_ID"] = "verify-windows-" + ([guid]::NewGuid().ToString("N").Substring(0, 12))
  $guiEnv["NO_PROXY"] = "127.0.0.1,localhost,::1"
  $guiCwd = Join-Path $guiRoot "u"
  $stateDir = Join-Path $guiRoot "s"

  $gui = Start-Gui $exe $guiEnv $guiCwd
  $line = Read-FirstLine $gui 120
  $record = $null
  if ($line) { try { $record = $line | ConvertFrom-Json } catch { $record = $null } }
  if ($record -and $record.ok -eq $true -and $record.record_version -eq 1 -and $record.url) {
    Say "PASS" "gui_record" "v1 record, url=$($record.url) frontend_pid=$($record.frontend_pid)"
  } else {
    $stderrTail = Format-StreamTail (Get-StreamResult $gui.StderrTask 2000)
    Say "FAIL" "gui_record" ("first stdout line: " + [string]$line + " ; stderr tail: " + $stderrTail)
  }

  if ($record -and $record.ok -eq $true) {
    $second = Start-Gui $exe $guiEnv $guiCwd
    $secondOutTask = $second.StandardOutput.ReadToEndAsync()
    if ($second.WaitForExit(90000)) {
      $secondOut = (Get-StreamResult $secondOutTask 5000).Trim()
      $secondErr = Format-StreamTail (Get-StreamResult $second.StderrTask 5000)
      $secondCode = $null
      try { $secondCode = (($secondOut -split "`r?`n" | Select-Object -Last 1) | ConvertFrom-Json).code } catch { }
      if ($second.ExitCode -ne 0 -and $secondCode -eq "GUI_RUNTIME_BUSY") {
        Say "PASS" "gui_single_instance" "second instance exit=$($second.ExitCode) code=GUI_RUNTIME_BUSY"
      } else {
        Say "FAIL" "gui_single_instance" "second instance exit=$($second.ExitCode) code=$secondCode stdout=$secondOut stderr=$secondErr"
      }
    } else {
      try { $second.Kill() } catch { }
      $secondErr = Format-StreamTail (Get-StreamResult $second.StderrTask 2000)
      Say "FAIL" "gui_single_instance" "second instance still running after 90 s (the runtime lock did not refuse it); stderr=$secondErr"
    }
  } else {
    Say "NOT-EVALUABLE" "gui_single_instance" "no first instance to contend with"
  }

  $recordPids = @()
  if ($record) {
    foreach ($candidate in @($record.frontend_pid, $record.backend_pid)) {
      if ($candidate -is [int] -or $candidate -is [long]) { $recordPids += [int]$candidate }
    }
  }
  if (-not $gui.HasExited) {
    $gui.StandardInput.Close()
    if ($gui.WaitForExit(60000)) {
      $rest = Get-StreamResult $gui.RestStdoutTask 5000
      $stderrTail = Format-StreamTail (Get-StreamResult $gui.StderrTask 5000)
      if ($gui.ExitCode -eq 0 -and ($rest -replace "\s", "") -notmatch '"ok":false') {
        Say "PASS" "gui_stdin_stop" "closing stdin ended the GUI with exit 0"
      } else {
        Say "FAIL" "gui_stdin_stop" "exit=$($gui.ExitCode) stdout after record: $rest ; stderr tail: $stderrTail"
      }
    } else {
      try { $gui.Kill() } catch { }
      $stderrTail = Format-StreamTail (Get-StreamResult $gui.StderrTask 2000)
      Say "FAIL" "gui_stdin_stop" "GUI still running 60 s after its stdin closed; stderr tail: $stderrTail"
    }
  } else {
    $stderrTail = Format-StreamTail (Get-StreamResult $gui.StderrTask 2000)
    Say "FAIL" "gui_stdin_stop" "GUI had already exited with $($gui.ExitCode) before stdin was closed; stderr tail: $stderrTail"
  }

  Start-Sleep -Seconds 2
  $leftRecord = @($recordPids | Where-Object { Test-Alive $_ })
  $daemonPids = @(Get-RecordedDaemonPids $stateDir)
  $leftOther = @(Get-IsolatedProcesses $guiRoot | Where-Object { $daemonPids -notcontains [int]$_.ProcessId })
  if ($leftRecord.Count -eq 0 -and $leftOther.Count -eq 0) {
    Say "PASS" "gui_children" "record pids [$($recordPids -join ', ')] gone; only the home's daemon [$($daemonPids -join ', ')] still runs"
  } else {
    Say "FAIL" "gui_children" ("left running: record=[" + ($leftRecord -join ', ') + "] other=[" + (($leftOther | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join '; ') + "]")
  }

  if ($daemonPids.Count -gt 0) {
    $stopOut = ""
    Push-Location $guiCwd
    $saved = @{}
    foreach ($key in $guiEnv.Keys) { $saved[$key] = [Environment]::GetEnvironmentVariable($key); [Environment]::SetEnvironmentVariable($key, $guiEnv[$key]) }
    try { $stopOut = (Invoke-Native $exe @("daemon", "stop", "--json")) } finally {
      foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key]) }
      Pop-Location
    }
    $stopExit = $script:NativeExit
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and @($daemonPids | Where-Object { Test-Alive $_ }).Count -gt 0) { Start-Sleep -Milliseconds 200 }
    $stillDaemon = @($daemonPids | Where-Object { Test-Alive $_ })
    if ($stopExit -eq 0 -and $stillDaemon.Count -eq 0) {
      Say "PASS" "gui_daemon_stop" "daemon stop exit 0; daemon [$($daemonPids -join ', ')] gone"
    } else {
      Say "FAIL" "gui_daemon_stop" "daemon stop exit=$stopExit still=[$($stillDaemon -join ', ')] out=$stopOut"
      foreach ($id in $stillDaemon) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
  } else {
    Say "INFO" "gui_daemon_stop" "no daemon record under $stateDir"
  }
  $leftAfter = @(Get-IsolatedProcesses $guiRoot)
  if ($leftAfter.Count -eq 0) {
    Say "PASS" "gui_isolated_clean" "no process references $guiRoot"
  } else {
    Say "FAIL" "gui_isolated_clean" (($leftAfter | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join '; ')
    foreach ($process in $leftAfter) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
  }
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
