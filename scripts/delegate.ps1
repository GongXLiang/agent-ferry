# agent-delegate-verify bootstrap for Windows PowerShell 5.1+ and PowerShell 7.
# Finds Python 3.10+ without needing Python, then runs scripts/delegate.py with every argument.
# Order: AGENT_DELEGATE_VERIFY_PYTHON (must work) -> python pinned in config.json -> PATH
#        (python, python3, py -3) -> conda/mamba, uv, pyenv-win, Scoop -> common folders
#        -> registry (PEP 514). Each candidate is executed once; the Microsoft Store
#        placeholder and anything older than 3.10 are rejected.
# Usage:  & <skill>\scripts\delegate.ps1 <subcommand> [options]
#         & <skill>\scripts\delegate.ps1 --which      (show the choice and rejected candidates)
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less files as ANSI.

$ErrorActionPreference = 'Continue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Main = Join-Path $Here 'delegate.py'
# No double quotes and no percent signs, so it survives PowerShell 5.1 and .bat shims.
$Probe = "import sys;print('.'.join(map(str,sys.version_info[:3])));print(sys.executable)"
$Rejected = New-Object System.Collections.ArrayList
$Seen = @{}

function Test-Python {
    param([string]$Exe, [string[]]$Extra = @(), [string]$Source)
    if (-not $Exe) { return $null }
    $key = ($Exe + ' ' + ($Extra -join ' ')).ToLowerInvariant()
    if ($Seen.ContainsKey($key)) { return $null }
    $Seen[$key] = $true
    if (($Exe -match '[\\/]') -and -not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        return $null
    }
    $out = $null
    try {
        $out = & $Exe @Extra -I -c $Probe 2>$null
    } catch {
        [void]$Rejected.Add("$Exe [$Source]: failed to start")
        return $null
    }
    $code = $LASTEXITCODE
    $lines = @($out | Where-Object { $_ -and $_.ToString().Trim() })
    if ($code -ne 0 -or $lines.Count -lt 2) {
        $why = 'failed to run'
        if ($Exe -match '\\WindowsApps\\') { $why = 'Microsoft Store placeholder, not an installed Python' }
        [void]$Rejected.Add("$Exe [$Source]: $why")
        return $null
    }
    $version = $lines[0].ToString().Trim()
    $parts = $version.Split('.')
    $major = 0; $minor = 0
    if (-not ([int]::TryParse($parts[0], [ref]$major) -and [int]::TryParse($parts[1], [ref]$minor))) {
        [void]$Rejected.Add("$Exe [$Source]: unexpected version output")
        return $null
    }
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
        [void]$Rejected.Add("$Exe [$Source]: Python $version is older than 3.10")
        return $null
    }
    return [pscustomobject]@{ Exe = $Exe; Extra = $Extra; Version = $version; Executable = $lines[1].ToString().Trim(); Source = $Source }
}

function Get-Apps([string]$Name) {
    return @(Get-Command $Name -CommandType Application -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
}

function Find-Python {
    $override = $env:AGENT_DELEGATE_VERIFY_PYTHON
    if ($override) {
        $found = Test-Python -Exe $override -Source 'AGENT_DELEGATE_VERIFY_PYTHON'
        if ($found) { return $found }
        [Console]::Error.WriteLine("[agent-delegate-verify] AGENT_DELEGATE_VERIFY_PYTHON is set but not usable: $override")
        foreach ($line in $Rejected) { [Console]::Error.WriteLine("  $line") }
        exit 9
    }

    $homeDir = $env:AGENT_DELEGATE_VERIFY_HOME
    if (-not $homeDir -and $env:LOCALAPPDATA) { $homeDir = Join-Path $env:LOCALAPPDATA 'agent-delegate-verify' }
    if ($homeDir) {
        $configFile = Join-Path $homeDir 'config.json'
        if (Test-Path -LiteralPath $configFile -PathType Leaf) {
            $pinned = $null
            try { $pinned = (Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json).python } catch { $pinned = $null }
            if ($pinned) {
                $found = Test-Python -Exe $pinned -Source 'config.json (pinned)'
                if ($found) { return $found }
                [Console]::Error.WriteLine("[agent-delegate-verify] pinned Python is not usable: $pinned; searching instead (update it with: config --set-python)")
            }
        }
    }

    foreach ($name in 'python', 'python3') {
        foreach ($exe in (Get-Apps $name)) {
            $found = Test-Python -Exe $exe -Source "PATH ($name)"
            if ($found) { return $found }
        }
    }
    foreach ($exe in (Get-Apps 'py')) {
        $found = Test-Python -Exe $exe -Extra @('-3') -Source 'PATH (py -3)'
        if ($found) { return $found }
    }

    if ($env:CONDA_PREFIX) {
        $found = Test-Python -Exe (Join-Path $env:CONDA_PREFIX 'python.exe') -Source 'CONDA_PREFIX (active conda environment)'
        if ($found) { return $found }
    }
    $bases = New-Object System.Collections.ArrayList
    foreach ($variable in 'CONDA_EXE', 'MAMBA_EXE') {
        $value = [Environment]::GetEnvironmentVariable($variable)
        if ($value) { [void]$bases.Add(@((Split-Path (Split-Path $value -Parent) -Parent), $variable)) }
    }
    if ($env:MAMBA_ROOT_PREFIX) { [void]$bases.Add(@($env:MAMBA_ROOT_PREFIX, 'MAMBA_ROOT_PREFIX')) }
    foreach ($conda in (Get-Apps 'conda' | Select-Object -First 1)) {
        try {
            $base = & $conda info --base 2>$null | Select-Object -First 1
            if ($base) { [void]$bases.Add(@($base.ToString().Trim(), 'conda info --base')) }
        } catch { }
    }
    foreach ($entry in $bases) {
        $base = $entry[0]; $origin = $entry[1]
        $found = Test-Python -Exe (Join-Path $base 'python.exe') -Source "conda base ($origin)"
        if ($found) { return $found }
        foreach ($envDir in @(Get-ChildItem -LiteralPath (Join-Path $base 'envs') -Directory -ErrorAction SilentlyContinue)) {
            $found = Test-Python -Exe (Join-Path $envDir.FullName 'python.exe') -Source "conda env $($envDir.Name) ($origin)"
            if ($found) { return $found }
        }
    }

    foreach ($uv in (Get-Apps 'uv' | Select-Object -First 1)) {
        $previous = $env:UV_PYTHON_DOWNLOADS
        $env:UV_PYTHON_DOWNLOADS = 'never'
        $path = $null
        try { $path = & $uv python find 2>$null | Select-Object -First 1 } catch { $path = $null }
        $env:UV_PYTHON_DOWNLOADS = $previous
        if ($path) {
            $found = Test-Python -Exe $path.ToString().Trim() -Source 'uv python find'
            if ($found) { return $found }
        }
    }

    $pyenvRoots = @($env:PYENV_ROOT, $env:PYENV, (Join-Path $HOME '.pyenv\pyenv-win')) | Where-Object { $_ }
    foreach ($root in $pyenvRoots) {
        foreach ($dir in @(Get-ChildItem -LiteralPath (Join-Path $root 'versions') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            $found = Test-Python -Exe (Join-Path $dir.FullName 'python.exe') -Source 'pyenv-win'
            if ($found) { return $found }
        }
    }

    $scoopCandidates = @($env:SCOOP, (Join-Path $HOME 'scoop'), $env:SCOOP_GLOBAL)
    if ($env:ProgramData) { $scoopCandidates += (Join-Path $env:ProgramData 'scoop') }
    $scoopRoots = @($scoopCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
    foreach ($root in $scoopRoots) {
        foreach ($dir in @(Get-ChildItem -LiteralPath (Join-Path $root 'apps') -Directory -Filter 'python*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            $found = Test-Python -Exe (Join-Path $dir.FullName 'current\python.exe') -Source "Scoop $($dir.Name)"
            if ($found) { return $found }
        }
        foreach ($name in 'miniconda3', 'anaconda3', 'miniforge', 'mambaforge') {
            $found = Test-Python -Exe (Join-Path $root "apps\$name\current\python.exe") -Source "Scoop $name"
            if ($found) { return $found }
        }
    }

    $patterns = @()
    if ($env:LOCALAPPDATA) { $patterns += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe') }
    if ($env:ProgramFiles) { $patterns += (Join-Path $env:ProgramFiles 'Python3*\python.exe') }
    $patterns += 'C:\Python3*\python.exe'
    foreach ($pattern in $patterns) {
        foreach ($file in @(Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            $found = Test-Python -Exe $file.FullName -Source 'common install folder'
            if ($found) { return $found }
        }
    }
    foreach ($root in @($HOME, $env:LOCALAPPDATA, $env:ProgramData) | Where-Object { $_ }) {
        foreach ($name in 'miniconda3', 'anaconda3', 'miniforge3', 'mambaforge') {
            $found = Test-Python -Exe (Join-Path $root "$name\python.exe") -Source "conda default folder $name"
            if ($found) { return $found }
        }
    }

    foreach ($hive in 'HKCU:\Software\Python', 'HKLM:\Software\Python', 'HKLM:\Software\WOW6432Node\Python') {
        foreach ($company in @(Get-ChildItem -Path $hive -ErrorAction SilentlyContinue)) {
            if ($company.PSChildName -eq 'PyLauncher') { continue }
            foreach ($tag in @(Get-ChildItem -Path $company.PSPath -ErrorAction SilentlyContinue)) {
                $install = Get-ItemProperty -LiteralPath (Join-Path $tag.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                if (-not $install) { continue }
                $exe = $install.ExecutablePath
                if (-not $exe -and $install.'(default)') { $exe = Join-Path $install.'(default)' 'python.exe' }
                $found = Test-Python -Exe $exe -Source "registry $($company.PSChildName)\$($tag.PSChildName)"
                if ($found) { return $found }
            }
        }
    }
    return $null
}

$forward = @($args)
$which = ($forward.Count -gt 0 -and $forward[0] -eq '--which')
$found = Find-Python
if (-not $found) {
    [Console]::Error.WriteLine('[agent-delegate-verify] No Python 3.10+ found. Rejected candidates:')
    foreach ($line in $Rejected) { [Console]::Error.WriteLine("  $line") }
    [Console]::Error.WriteLine('Install Python 3.10+ (python.org, conda, uv, Scoop ...) or set AGENT_DELEGATE_VERIFY_PYTHON to its full path.')
    exit 9
}
if ($which) {
    Write-Output ("python=" + (@($found.Exe) + @($found.Extra) -join ' '))
    Write-Output "version=$($found.Version)"
    Write-Output "executable=$($found.Executable)"
    Write-Output "source=$($found.Source)"
    foreach ($line in $Rejected) { Write-Output "rejected=$line" }
    exit 0
}
$env:AGENT_DELEGATE_VERIFY_PYTHON_SOURCE = $found.Source
$extra = @($found.Extra)
if ($MyInvocation.ExpectingInput) {
    $OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $input | & $found.Exe @extra $Main @forward
} else {
    & $found.Exe @extra $Main @forward
}
exit $LASTEXITCODE
