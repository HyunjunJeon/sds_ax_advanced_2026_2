# GJC Coding Agent Installer for Windows (standalone binary, no Bun required)
# Usage: irm https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.ps1 | iex
#
# Or with options:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.ps1)))
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.ps1))) -Channel nightly
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.ps1))) -Ref v0.15.0
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.ps1))) -Source
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Yeachan-Heo/gajae-code/main/scripts/install.ps1))) -Source -Ref main

param(
    [switch]$Source,
    [switch]$Binary,
    [ValidateSet("stable", "nightly")]
    [string]$Channel = "stable",
    [string]$Ref
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 runs on .NET Framework, which may still default to
# TLS 1.0; GitHub requires TLS 1.2+, so opt in explicitly.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$Repo = "Yeachan-Heo/gajae-code"
$Package = "@gajae-code/coding-agent"
$InstallDir = if ($env:GJC_INSTALL_DIR) { $env:GJC_INSTALL_DIR } else { "$env:LOCALAPPDATA\gjc" }
$GithubApi = if ($env:GJC_GITHUB_API) { $env:GJC_GITHUB_API } else { "https://api.github.com" }
$GithubReleases = if ($env:GJC_GITHUB_RELEASES) { $env:GJC_GITHUB_RELEASES } else { "https://github.com/$Repo/releases/download" }
$MinimumBunVersion = "1.3.14"
$BinarySha256Asset = "gajae-release-binaries.sha256"
$BinaryManifestAsset = "gajae-release-binaries-v1.json"
$UserAgent = "gjc-install"

function Test-TrustedGithubUri {
    param([string]$Uri)
    try {
        $parsed = [Uri]$Uri
        return $parsed.Scheme -eq "https" -and ($parsed.Host -eq "api.github.com" -or $parsed.Host -eq "github.com")
    } catch {
        return $false
    }
}

function Assert-OfficialGithubOrigins {
    $api = $GithubApi.TrimEnd("/")
    $releases = $GithubReleases.TrimEnd("/")
    $expectedReleases = "https://github.com/$Repo/releases/download"
    if ($api -ne "https://api.github.com") {
        throw "GJC_GITHUB_API must be https://api.github.com (got $GithubApi)."
    }
    if ($releases -ne $expectedReleases) {
        throw "GJC_GITHUB_RELEASES must be $expectedReleases (got $GithubReleases)."
    }
}

function Get-GithubHeaders {
    param([string]$Uri)
    $headers = @{
        "User-Agent" = $UserAgent
        "Accept" = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    $token = $env:GITHUB_TOKEN
    if (-not $token) { $token = $env:GH_TOKEN }
    if ($token -and (Test-TrustedGithubUri $Uri)) {
        $headers["Authorization"] = "Bearer $token"
    }
    return $headers
}

function Test-SafeReleaseTag {
    param([string]$Tag)
    if (-not $Tag) { return $false }
    if ($Tag -match '[/\\;|&`$]|(\.\.)') { return $false }
    return $Tag -match '^v[A-Za-z0-9][A-Za-z0-9._-]*$'
}

function Test-StableReleaseTag {
    param([string]$Tag)
    return $Tag -match '^v\d+\.\d+\.\d+$'
}

function Test-NightlyReleaseTag {
    param([string]$Tag)
    return $Tag -match '^v\d+\.\d+\.\d+-nightly\.\d+\.\d+\.g[0-9a-f]+$'
}

function Test-ReleaseTag {
    param([string]$Tag)
    return (Test-StableReleaseTag $Tag) -or (Test-NightlyReleaseTag $Tag)
}

function Get-WindowsBinaryName {
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    if ($arch -eq "ARM64") {
        throw "Unsupported architecture: ARM64. Prebuilt Windows binaries are published for x64 only."
    }
    if ($arch -eq "x86") {
        throw "Unsupported architecture: x86. Prebuilt Windows binaries are published for x64 only."
    }
    if ($arch -and $arch -ne "AMD64") {
        throw "Unsupported architecture: $arch. Prebuilt Windows binaries are published for x64 only."
    }
    return "gjc-windows-x64.exe"
}

function Test-BunInstalled {
    try {
        $null = Get-Command bun -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-BunVersion {
    try {
        $versionText = (bun --version 2>$null)
        if (-not $versionText) {
            return $null
        }

        $clean = $versionText.Trim().Split("-")[0]
        return [version]$clean
    } catch {
        return $null
    }
}

function Test-BunVersion {
    param([string]$MinimumVersion)

    $currentVersion = Get-BunVersion
    if (-not $currentVersion) {
        return $false
    }

    return $currentVersion -ge [version]$MinimumVersion
}

function Assert-BunVersion {
    param([string]$MinimumVersion)

    if (-not (Test-BunVersion $MinimumVersion)) {
        $current = Get-BunVersion
        $currentText = if ($current) { $current.ToString() } else { "unknown" }
        throw @"
Bun $MinimumVersion or newer is required for -Source. Current version: $currentText.
Install or upgrade Bun yourself: https://bun.sh/docs/installation
This installer never downloads Bun.
"@
    }
}

function Test-GitInstalled {
    try {
        $null = Get-Command git -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Test-GitLfsInstalled {
    try {
        $null = Get-Command git-lfs -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Find-BashShell {
    $gitBash = "C:\Program Files\Git\bin\bash.exe"
    if (Test-Path $gitBash) {
        return $gitBash
    }

    try {
        $bashCmd = Get-Command bash.exe -ErrorAction Stop
        return $bashCmd.Source
    } catch {
        return $null
    }
}

function Configure-BashShell {
    try {
        $settingsDir = Join-Path $env:USERPROFILE ".gjc\agent"
        $settingsFile = Join-Path $settingsDir "settings.json"

        if (Test-Path $settingsFile) {
            try {
                $existingSettings = Get-Content $settingsFile -Raw | ConvertFrom-Json
                if ($existingSettings.shellPath) {
                    Write-Host "Bash shell already configured: $($existingSettings.shellPath)" -ForegroundColor Cyan
                    return
                }
            } catch {
                # Invalid JSON, we'll overwrite it
            }
        }

        $bashPath = Find-BashShell

        if ($bashPath) {
            Write-Host "Found bash shell: $bashPath" -ForegroundColor Cyan

            if (-not (Test-Path $settingsDir)) {
                New-Item -ItemType Directory -Force -Path $settingsDir | Out-Null
            }

            $settings = @{}
            if (Test-Path $settingsFile) {
                try {
                    $parsed = Get-Content $settingsFile -Raw | ConvertFrom-Json
                    if ($parsed) {
                        foreach ($prop in $parsed.PSObject.Properties) {
                            $settings[$prop.Name] = $prop.Value
                        }
                    }
                } catch {
                    $settings = @{}
                }
            }

            $settings["shellPath"] = $bashPath
            $settings | ConvertTo-Json -Depth 10 | Set-Content $settingsFile -Encoding UTF8
            Write-Host "Configured shell path in $settingsFile" -ForegroundColor Green
        } else {
            Write-Host ""
            Write-Host "No bash shell found." -ForegroundColor Yellow
            Write-Host "  GJC requires a bash shell on Windows. Options:" -ForegroundColor Yellow
            Write-Host "    1. Install Git for Windows: https://git-scm.com/download/win" -ForegroundColor Yellow
            Write-Host "    2. Use WSL, Cygwin, or MSYS2" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  After installing, you can set a custom path in:" -ForegroundColor Yellow
            Write-Host "    $settingsFile" -ForegroundColor Yellow
            Write-Host '    { "shellPath": "C:\\path\\to\\bash.exe" }' -ForegroundColor Yellow
        }
    } catch {
        Write-Host "Could not configure bash shell: $_" -ForegroundColor Yellow
    }
}

function Get-FileSha256Lower {
    param([string]$Path)
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ChecksumForAsset {
    param(
        [string]$SumsPath,
        [string]$AssetName
    )
    foreach ($line in Get-Content -Path $SumsPath) {
        $parts = $line.Trim() -split '\s+'
        if ($parts.Count -ge 2) {
            $name = $parts[1].TrimStart('*').TrimStart('.', '\', '/')
            $name = Split-Path -Leaf $name
            if ($name -eq $AssetName) {
                return $parts[0].ToLowerInvariant()
            }
        }
    }
    return $null
}

function Resolve-ReleaseTag {
    $headers = Get-GithubHeaders -Uri "$GithubApi/"
    if ($Ref) {
        if (-not (Test-ReleaseTag $Ref)) {
            throw "Invalid -Ref '$Ref'. Expected a GitHub release tag like v0.15.0 or v0.15.0-nightly.1.1.gabc."
        }
        Write-Host "Fetching release $Ref..."
        try {
            $Release = Invoke-RestMethod -Uri "$GithubApi/repos/$Repo/releases/tags/$Ref" -Headers $headers
        } catch {
            throw "Release tag not found: $Ref`nFor branch/commit source installs, use -Source with -Ref and an existing Bun."
        }
        return $Release.tag_name
    }

    if ($Channel -eq "nightly") {
        Write-Host "Fetching latest nightly GitHub prerelease..."
        $releases = Invoke-RestMethod -Uri "$GithubApi/repos/$Repo/releases?per_page=40" -Headers $headers
        foreach ($candidate in $releases) {
            if ($candidate.prerelease -eq $true -and $candidate.draft -eq $false -and (Test-SafeReleaseTag $candidate.tag_name) -and $candidate.tag_name -match '^v\d+\.\d+\.\d+-nightly\.\d+\.\d+\.g[0-9a-f]+$') {
                return $candidate.tag_name
            }
        }
        throw "The nightly channel has no published GitHub prerelease yet; it is populated by the scheduled nightly workflow."
    }

    Write-Host "Fetching latest stable GitHub release..."
    $Release = Invoke-RestMethod -Uri "$GithubApi/repos/$Repo/releases/latest" -Headers $headers
    return $Release.tag_name
}

function Get-WebExceptionStatus {
    param($ErrorRecord)
    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    } catch {
        return 0
    }
}

function Assert-Checksum {
    param(
        [string]$Tag,
        [string]$AssetName,
        [string]$DownloadedPath
    )

    $sumsUrl = "$GithubReleases/$Tag/$BinarySha256Asset"
    $sumsTmp = Join-Path $InstallDir (".gjc.sha256." + [System.Guid]::NewGuid().ToString("N"))
    $manifestUrl = "$GithubReleases/$Tag/$BinaryManifestAsset"
    $manifestTmp = Join-Path $InstallDir (".gjc.manifest." + [System.Guid]::NewGuid().ToString("N"))
    try {
        $sumsMissing = $false
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $sumsUrl -OutFile $sumsTmp -Headers @{ "User-Agent" = $UserAgent }
            $expected = Get-ChecksumForAsset -SumsPath $sumsTmp -AssetName $AssetName
            if (-not $expected -or $expected -notmatch '^[0-9a-f]{64}$') {
                throw "Release checksum file $BinarySha256Asset did not list $AssetName"
            }
            $actual = Get-FileSha256Lower $DownloadedPath
            if ($actual -ne $expected) {
                throw "Checksum mismatch for ${AssetName}: expected $expected, got $actual. Existing install was not changed."
            }
            Write-Host "Verified SHA-256 for $AssetName"
            return
        } catch {
            if ($_.Exception.Message -match "Checksum mismatch|did not list") { throw }
            $status = Get-WebExceptionStatus $_
            if ($status -eq 404) { $sumsMissing = $true }
            else { throw "Integrity asset $BinarySha256Asset could not be fetched (HTTP $status): $_. Existing install was not changed." }
        }

        try {
            Invoke-WebRequest -UseBasicParsing -Uri $manifestUrl -OutFile $manifestTmp -Headers @{ "User-Agent" = $UserAgent }
            $manifest = Get-Content $manifestTmp -Raw | ConvertFrom-Json
            foreach ($entry in $manifest.binaries) {
                if ($entry.name -eq $AssetName) {
                    $expected = [string]$entry.sha256
                    if ($expected -notmatch '^[0-9a-f]{64}$') {
                        throw "Release manifest listed an invalid checksum for $AssetName"
                    }
                    $actual = Get-FileSha256Lower $DownloadedPath
                    if ($actual -ne $expected) {
                        throw "Checksum mismatch for ${AssetName}: expected $expected, got $actual. Existing install was not changed."
                    }
                    Write-Host "Verified SHA-256 for $AssetName from $BinaryManifestAsset"
                    return
                }
            }
            throw "Release manifest $BinaryManifestAsset did not list $AssetName"
        } catch {
            if ($_.Exception.Message -match "Checksum mismatch|invalid checksum|did not list") { throw }
            $status = Get-WebExceptionStatus $_
            if ($status -eq 404 -and $sumsMissing) {
                throw "Release $Tag has no checksum assets. Existing install was not changed."
            }
            throw "Integrity asset $BinaryManifestAsset could not be fetched (HTTP $status): $_. Existing install was not changed."
        }
    } finally {
        Remove-Item -Force $sumsTmp -ErrorAction SilentlyContinue
        Remove-Item -Force $manifestTmp -ErrorAction SilentlyContinue
    }
}

function Assert-InstalledBinary {
    param(
        [string]$ExePath,
        [string]$ExpectedVersion
    )
    if (-not (Test-Path $ExePath)) {
        throw "Installed file is missing: $ExePath"
    }
    $versionOutput = & $ExePath --version 2>$null
    $escaped = [regex]::Escape($ExpectedVersion)
    if (-not $versionOutput -or ($versionOutput -notmatch "(?m)^gjc/$escaped(\s|$)")) {
        throw "Installed binary --version mismatch (expected gjc/$ExpectedVersion, got: $versionOutput)"
    }
    & $ExePath --smoke-test | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed binary --smoke-test failed"
    }
}

function Install-ViaBun {
    Write-Host "Installing from source via existing bun..."
    if ($Ref) {
        if (-not (Test-GitInstalled)) {
            throw "git is required for -Source -Ref"
        }

        $tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gjc-install-" + [System.Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null

        try {
            $repoUrl = "https://github.com/$Repo.git"
            $cloneOk = $false
            try {
                git clone --depth 1 --branch $Ref $repoUrl $tmpRoot | Out-Null
                $cloneOk = $true
            } catch {
                $cloneOk = $false
            }

            if (-not $cloneOk) {
                git clone $repoUrl $tmpRoot | Out-Null
                Push-Location $tmpRoot
                try {
                    git checkout $Ref | Out-Null
                } finally {
                    Pop-Location
                }
            }

            if (Test-GitLfsInstalled) {
                Push-Location $tmpRoot
                try {
                    git lfs pull | Out-Null
                } finally {
                    Pop-Location
                }
            }

            $packagePath = Join-Path $tmpRoot "packages\coding-agent"
            if (-not (Test-Path $packagePath)) {
                throw "Expected package at $packagePath"
            }

            bun install -g $packagePath
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to install from $packagePath via bun"
            }
        } finally {
            Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
        }
    } else {
        bun install -g $Package
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install $Package via bun"
        }
    }

    Write-Host ""
    Write-Host "Installed gjc via bun (development/source mode)" -ForegroundColor Green
    Configure-BashShell
    Write-Host "Run 'gjc' to get started!"
}

function Install-Binary {
    Assert-OfficialGithubOrigins
    $BinaryName = Get-WindowsBinaryName
    $Latest = Resolve-ReleaseTag
    if ($Ref) {
        if (-not (Test-ReleaseTag $Latest)) { throw "Refusing unsafe release tag: $Latest" }
    } elseif ($Channel -eq "nightly") {
        if (-not (Test-NightlyReleaseTag $Latest)) { throw "Refusing non-nightly release tag: $Latest" }
    } else {
        if (-not (Test-StableReleaseTag $Latest)) { throw "Refusing non-stable release tag: $Latest" }
    }
    $ExpectedVersion = $Latest.TrimStart("v")
    Write-Host "Using version: $Latest"

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $lockFile = Join-Path $InstallDir ".gjc-install.lock"
    $lockNonce = [guid]::NewGuid().ToString("N")
    $lockLine = "$PID $lockNonce"
    $lockOwned = $false
    try {
        $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($lockLine + "`n")
        $fs.Write($bytes, 0, $bytes.Length)
        $fs.Dispose()
        $lockOwned = $true
    } catch {
        throw "Another GJC installer is already running in $InstallDir (lock: $lockFile). Remove a leftover lock file only after confirming no installer is running."
    }

    $OutPath = Join-Path $InstallDir "gjc.exe"
    $DownloadTmp = Join-Path $InstallDir (".gjc.download." + [System.Guid]::NewGuid().ToString("N"))
    $BackupPath = Join-Path $InstallDir (".gjc.bak." + [System.Guid]::NewGuid().ToString("N"))
    $hadExisting = $false
    try {
        $existing = Get-Item -LiteralPath $OutPath -Force -ErrorAction SilentlyContinue
        $isReparse = $false
        if ($existing) {
            $isReparse = [bool]($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
            if (-not $isReparse -and $existing.LinkType) { $isReparse = $true }
        }
        if ($isReparse) {
            throw "Refusing to replace symlink $OutPath with a regular binary. Remove the symlink or set GJC_INSTALL_DIR."
        }
        $hadExisting = [bool]$existing
        $BinaryUrl = "$GithubReleases/$Latest/$BinaryName"
        Write-Host "Downloading $BinaryName..."
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $BinaryUrl -OutFile $DownloadTmp -Headers @{ "User-Agent" = $UserAgent }
        } catch {
            Remove-Item -Force $DownloadTmp -ErrorAction SilentlyContinue
            throw "No prebuilt GJC binary was found for windows-x64 in $Latest.`nExpected asset URL: $BinaryUrl`nRe-run with -Source only if you are developing GJC and already have Bun."
        }

        if (-not (Test-Path $DownloadTmp) -or ((Get-Item $DownloadTmp).Length -le 0)) {
            Remove-Item -Force $DownloadTmp -ErrorAction SilentlyContinue
            throw "Downloaded file was empty: $BinaryUrl. Existing install was not changed."
        }

        Assert-Checksum -Tag $Latest -AssetName $BinaryName -DownloadedPath $DownloadTmp

        if ($hadExisting) {
            Copy-Item -Force -LiteralPath $OutPath -Destination $BackupPath
        }
        try {
            Move-Item -Force $DownloadTmp $OutPath
        } catch {
            if (Test-Path $BackupPath) {
                Copy-Item -Force -LiteralPath $BackupPath -Destination $OutPath
                Write-Host "Restored previous gjc binary at $OutPath"
            }
            throw "Failed to publish the downloaded binary. Existing install was preserved if one existed. $_"
        }

        try {
            Assert-InstalledBinary -ExePath $OutPath -ExpectedVersion $ExpectedVersion
        } catch {
            if (Test-Path $BackupPath) {
                Copy-Item -Force -LiteralPath $BackupPath -Destination $OutPath
                Write-Host "Restored previous gjc binary at $OutPath"
            } elseif (-not $hadExisting) {
                Remove-Item -Force $OutPath -ErrorAction SilentlyContinue
            }
            throw "Verification failed; existing install was preserved if one existed. $_"
        }

        Remove-Item -Force $BackupPath -ErrorAction SilentlyContinue

        Write-Host ""
        Write-Host "Installed gjc $ExpectedVersion to $OutPath" -ForegroundColor Green

        $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not $UserPath) { $UserPath = "" }
        $pathParts = @()
        foreach ($part in $UserPath.Split(";")) {
            if ($part -and $part -ne $InstallDir) { $pathParts += $part }
        }
        $newUserPath = (@($InstallDir) + $pathParts) -join ";"
        if ($newUserPath -ne $UserPath) {
            Write-Host "Putting $InstallDir first on PATH..."
            [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
            $env:Path = "$InstallDir;" + $env:Path
            $needsRestart = $true
        }

        Configure-BashShell

        if ($needsRestart) {
            Write-Host "Restart your terminal, then run 'gjc' to get started!"
        } else {
            Write-Host "Run 'gjc' to get started!"
        }
    } finally {
        Remove-Item -Force $DownloadTmp -ErrorAction SilentlyContinue
        if ($lockOwned -and (Test-Path $lockFile)) {
            $still = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($still -eq $lockLine) {
                Remove-Item -Force $lockFile -ErrorAction SilentlyContinue
            }
        }
    }
}

if ($Source -and $Binary) {
    throw "Specify only one of -Source or -Binary."
}

if ($Source) {
    if (-not (Test-BunInstalled)) {
        throw @"
-Source requires an existing Bun $MinimumBunVersion+ on PATH.
This installer never downloads Bun. Install it from https://bun.sh/docs/installation
Ordinary installs should omit -Source and use the prebuilt binary.
"@
    }
    Assert-BunVersion $MinimumBunVersion
    Install-ViaBun
} else {
    Install-Binary
}
