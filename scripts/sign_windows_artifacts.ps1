param(
    [Parameter(Mandatory = $true)]
    [string[]]$Files,
    [string]$PfxPath = $env:WINDOWS_SIGNING_PFX_PATH,
    [string]$PfxPassword = $env:WINDOWS_SIGNING_PFX_PASSWORD,
    [string]$Thumbprint = $env:WINDOWS_SIGNING_CERT_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.acs.microsoft.com",
    [switch]$RequireSignature
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $candidate = Get-ChildItem $kitsRoot -Recurse -File -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($candidate) {
        return $candidate.FullName
    }
    throw "signtool.exe was not found. Install the Windows SDK signing tools."
}

$resolvedFiles = foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Signing target does not exist: $file"
    }
    (Resolve-Path -LiteralPath $file).Path
}

$hasPfx = -not [string]::IsNullOrWhiteSpace($PfxPath)
$hasThumbprint = -not [string]::IsNullOrWhiteSpace($Thumbprint)
if (-not $hasPfx -and -not $hasThumbprint) {
    if ($RequireSignature) {
        throw "No Authenticode certificate configured. Set WINDOWS_SIGNING_PFX_PATH or WINDOWS_SIGNING_CERT_THUMBPRINT."
    }
    Write-Warning "Authenticode certificate is not configured; artifacts remain unsigned."
    exit 0
}

$signTool = Find-SignTool
foreach ($file in $resolvedFiles) {
    $arguments = @("sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $TimestampUrl)
    if ($hasPfx) {
        if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) {
            throw "PFX file does not exist: $PfxPath"
        }
        $arguments += @("/f", (Resolve-Path -LiteralPath $PfxPath).Path)
        if (-not [string]::IsNullOrEmpty($PfxPassword)) {
            $arguments += @("/p", $PfxPassword)
        }
    }
    else {
        $arguments += @("/sha1", ($Thumbprint -replace "\s", ""))
    }
    $arguments += $file

    & $signTool @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed for $file with exit code $LASTEXITCODE"
    }
    & $signTool verify /pa /all $file
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode verification failed for $file"
    }
}
