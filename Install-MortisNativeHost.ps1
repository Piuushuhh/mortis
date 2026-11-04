param(
    [string[]]$ExtensionIds
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project '.venv\Scripts\python.exe'
$hostScript = Join-Path $project 'mortis_native_host.py'
$hostLauncher = Join-Path $project 'MortisNativeHost.exe'
$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw 'LOCALAPPDATA is not set for this Windows user.'
}
$mortisHome = Join-Path $localAppData 'Mortis'
$hostName = 'com.mortis.page_bridge'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Existing virtual environment was not found: $python"
}
if (-not (Test-Path -LiteralPath $hostScript)) {
    throw "Mortis native host was not found: $hostScript"
}
if (-not (Test-Path -LiteralPath $hostLauncher)) {
    throw "Mortis Native Messaging launcher was not found: $hostLauncher"
}

if (-not $ExtensionIds -or $ExtensionIds.Count -eq 0) {
    $rawIds = Read-Host 'Enter extension ID(s) from brave://extensions, chrome://extensions, and/or edge://extensions, separated by commas'
    $ExtensionIds = @($rawIds -split ',')
}
$ids = @($ExtensionIds | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
if ($ids.Count -eq 0) { throw 'At least one extension ID is required.' }
foreach ($id in $ids) {
    if ($id -notmatch '^[a-p]{32}$') {
        throw "Invalid Chromium extension ID: $id"
    }
}

$origins = @($ids | ForEach-Object { "chrome-extension://$_/" } | Sort-Object -Unique)
New-Item -ItemType Directory -Path $mortisHome -Force | Out-Null
$manifestPath = Join-Path $mortisHome 'native_host_manifest.json'
$allowlistPath = Join-Path $mortisHome 'allowed_extensions.json'

$hostManifest = [ordered]@{
    name = $hostName
    description = 'Authenticated local bridge for the Mortis desktop spider.'
    path = $hostLauncher
    type = 'stdio'
    allowed_origins = $origins
}
$allowlist = [ordered]@{ allowed_origins = $origins }
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($manifestPath, ($hostManifest | ConvertTo-Json -Depth 5), $utf8NoBom)
[System.IO.File]::WriteAllText($allowlistPath, ($allowlist | ConvertTo-Json -Depth 5), $utf8NoBom)

$registryPaths = @(
    "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$hostName",
    "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$hostName",
    "HKCU:\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\$hostName"
)
foreach ($registryPath in $registryPaths) {
    New-Item -Path $registryPath -Force | Out-Null
    Set-Item -Path $registryPath -Value $manifestPath
    $registeredManifest = (Get-Item -LiteralPath $registryPath).GetValue('')
    if ($registeredManifest -ne $manifestPath) {
        throw "Native Messaging registry registration did not persist at $registryPath"
    }
}

Write-Host 'Mortis Native Messaging host registered for Chrome, Edge, and Brave.' -ForegroundColor Cyan
Write-Host "Host manifest: $manifestPath"
Write-Host "Allowed extension origin(s): $($origins -join ', ')"
Write-Host 'Restart Brave, Chrome, and Edge after loading the unpacked extension.'
