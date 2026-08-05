# Builds two versioned zips straight from the project root, plus the local
# runnable copy:
#   JellyStat Dist\script.jellystat-<version>.zip      - installable Kodi addon
#   JellyStat Git\script.jellystat-<version>-src.zip   - GitHub-bound source
#                                                        (addon + repo housekeeping)
#   JellyStat App\                                     - mirror of the Dist zip
#                                                        contents
# Kodi requires the install zip to be named <addon.id>-<version>.zip with the
# addon folder at the zip root, so the Dist zip keeps that naming instead of
# the usual <projectname>-v<version>.zip.
#
# If a build.conf sits beside this script with a RELEASE_COPY_WINDOWS line,
# both zips are copied to that folder as well, and a failed copy fails the
# build. That file is local only, so the archive location is not recorded
# here.
#
# Run from the repo root:  powershell -File build-zip.ps1
#   -SkipCopy   build only, leave the archive folder alone
param([switch]$SkipCopy)

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$addonId = 'script.jellystat'
$version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
$distDir = Join-Path $root 'JellyStat Dist'
$gitDir  = Join-Path $root 'JellyStat Git'
$appDir  = Join-Path $root 'JellyStat App'
$stage   = Join-Path $env:TEMP 'jellystat-build'

# addon.xml is what Kodi actually reads - refuse to build if it and VERSION
# have drifted apart.
$addonXml     = [xml](Get-Content (Join-Path $root "$addonId\addon.xml") -Raw)
$addonVersion = $addonXml.addon.version
if ($addonVersion -ne $version) {
    throw "VERSION ($version) does not match $addonId\addon.xml ($addonVersion) - update one of them."
}

# Excluded from BOTH zips (no secret files in this project - tokens live in
# Kodi's runtime settings, never in the source).
# Every dot folder is dropped, so version control and editor state stay out of
# the snapshot without this list needing to name each one.
$excludeFiles = @('*.zip', '*.7z', '*.tmp')
$dotDirs      = @(Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name.StartsWith('.') } |
                  ForEach-Object { $_.FullName })
$excludeDirs  = @('JellyStat Dist', 'JellyStat Git', 'JellyStat App',
                  '__pycache__', '@eaDir', '$RECYCLE.BIN') + $dotDirs

# Kept out of the src zip only. That zip is the GitHub-bound snapshot, so it
# must hold exactly what may be published: no release tooling, and above all
# nothing naming the release share, whose address must never reach GitHub.
$localOnly    = @('publish-github.sh', 'push-source.sh', 'publish.conf',
                  'GITHUB-RELEASE-GUIDE.md', '.publish-allow',
                  'Github repository', 'build.conf', 'build-zip.py')

foreach ($d in @($distDir, $gitDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }

# Compress-Archive on PowerShell 5.1 writes backslash entry names, which
# breaks addon installs on Linux/Android Kodi - zip manually with forward
# slashes instead.
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

function Build-Zip {
    param([string]$StageName, [string]$SourceDir, [string]$StageSubDir, [string]$ZipPath,
          [string[]]$ExtraExcludeFiles = @())

    $target = Join-Path $stage $StageName
    if ($StageSubDir) { $target = Join-Path $target $StageSubDir }
    $xf = @($excludeFiles) + $ExtraExcludeFiles
    robocopy $SourceDir $target /E /XF @xf /XD @excludeDirs /NFL /NDL /NJH | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed staging $StageName (exit $LASTEXITCODE)" }

    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    $stageRoot = Join-Path $stage $StageName
    $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, 'Create')
    try {
        foreach ($file in Get-ChildItem $stageRoot -Recurse -File) {
            $entryName = $file.FullName.Substring($stageRoot.Length + 1) -replace '\\', '/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $file.FullName, $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    } finally {
        $zip.Dispose()
    }

    $size = [math]::Round((Get-Item $ZipPath).Length / 1KB, 1)
    Write-Output "Built: $ZipPath ($size KB)"
}

# Dist zip: the addon folder only, at the zip root (Kodi install format).
$distZip = Join-Path $distDir ("$addonId-$version.zip")
Build-Zip -StageName 'deploy' -SourceDir (Join-Path $root $addonId) `
    -StageSubDir $addonId `
    -ZipPath $distZip

# App folder: exact mirror of the Dist zip contents.
# /MIR removes anything not in the stage - never hand-edit this folder.
robocopy (Join-Path $stage 'deploy') $appDir /MIR /NFL /NDL /NJH | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed mirroring $appDir (exit $LASTEXITCODE)" }
Write-Output "Mirrored: $appDir"

# Git zip: everything from the root - addon source plus repo housekeeping
# (README, spec/example docs, VERSION, .gitignore, this script).
$srcZip = Join-Path $gitDir ("$addonId-$version-src.zip")
Build-Zip -StageName 'src' -SourceDir $root -StageSubDir '' `
    -ZipPath $srcZip -ExtraExcludeFiles $localOnly

# Belt and braces: refuse to keep a src zip that picked up a local only file.
$srcNames = [System.IO.Compression.ZipFile]::OpenRead($srcZip)
try {
    $leaked = $srcNames.Entries | Where-Object { $localOnly -contains (Split-Path $_.FullName -Leaf) }
} finally {
    $srcNames.Dispose()
}
if ($leaked) {
    Remove-Item $srcZip -Force
    throw "local only files reached the src zip: $(($leaked | ForEach-Object { $_.FullName }) -join ', ')"
}

Remove-Item $stage -Recurse -Force

# Archive copy: every zip this build produced also goes to the folder named in
# build.conf, so no build exists only on this machine. A destination that is
# configured but unreachable is an error, never a silent skip.
$confFile = Join-Path $root 'build.conf'
if (-not $SkipCopy -and (Test-Path $confFile)) {
    $archive = (Get-Content $confFile |
                Where-Object { $_ -match '^\s*RELEASE_COPY_WINDOWS\s*=' } |
                ForEach-Object { ($_ -replace '^\s*RELEASE_COPY_WINDOWS\s*=\s*', '').Trim('"').Trim() } |
                Select-Object -Last 1)
    if ($archive) {
        if (-not (Test-Path $archive)) {
            New-Item -ItemType Directory -Path $archive -Force | Out-Null
        }
        foreach ($zip in @($distZip, $srcZip)) {
            $target = Join-Path $archive (Split-Path $zip -Leaf)
            Copy-Item $zip $target -Force
            if ((Get-Item $target).Length -ne (Get-Item $zip).Length) {
                throw "copy of $(Split-Path $zip -Leaf) to $archive is the wrong size"
            }
            Write-Output "Copied: $target"
        }
    }
} elseif ($SkipCopy) {
    Write-Output "Skipped the archive copy (-SkipCopy)"
}
