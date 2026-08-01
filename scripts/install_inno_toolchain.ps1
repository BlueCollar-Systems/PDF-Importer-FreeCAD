[CmdletBinding()]
param(
    [string]$ManifestPath = (Join-Path $PSScriptRoot '..\installer\inno-toolchain-6.7.1.json'),
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$manifestFile = (Resolve-Path -LiteralPath $ManifestPath).Path
$manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
$destinationPath = [IO.Path]::GetFullPath($Destination)
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$compiler = Join-Path $destinationPath 'ISCC.exe'

if (-not (Test-Path -LiteralPath $compiler)) {
    $downloadRoot = if ($env:RUNNER_TEMP) {
        [IO.Path]::GetFullPath($env:RUNNER_TEMP)
    } else {
        [IO.Path]::GetFullPath('C:\TMP')
    }
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $download = Join-Path $downloadRoot ('innosetup-' + $manifest.version + '.exe')
    Invoke-WebRequest -Uri $manifest.source_url -OutFile $download

    $actual = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne [string]$manifest.source_sha256) {
        throw "Inno Setup distribution digest mismatch: expected $($manifest.source_sha256), got $actual"
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $download
    $actualSubject = [string]$signature.SignerCertificate.Subject
    $actualThumbprint = ([string]$signature.SignerCertificate.Thumbprint).ToLowerInvariant()
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Inno Setup Authenticode signature is not valid: $($signature.Status)"
    }
    if ($actualSubject -ne [string]$manifest.source_authenticode_subject) {
        throw "Inno Setup Authenticode subject mismatch: $actualSubject"
    }
    if ($actualThumbprint -ne [string]$manifest.source_authenticode_thumbprint) {
        throw "Inno Setup Authenticode thumbprint mismatch: $actualThumbprint"
    }

    $arguments = @(
        '/PORTABLE=1',
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/SP-',
        ('/DIR=' + $destinationPath)
    )
    $process = Start-Process -FilePath $download -ArgumentList $arguments `
        -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Inno Setup portable installation failed with exit code $($process.ExitCode)"
    }
}

& python (Join-Path $repoRoot 'build_windows_installer.py') `
    --iscc $compiler `
    --toolchain-manifest $manifestFile `
    --verify-toolchain-only
if ($LASTEXITCODE -ne 0) {
    throw 'Pinned Inno Setup toolchain verification failed.'
}

if ($env:GITHUB_ENV) {
    Add-Content -LiteralPath $env:GITHUB_ENV -Value ('INNO_TOOLCHAIN_ROOT=' + $destinationPath)
}
Write-Output ('Verified pinned Inno Setup toolchain: ' + $destinationPath)
