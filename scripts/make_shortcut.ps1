<#
.SYNOPSIS
    Creates "phonemic" shortcuts that open the desktop window without a console.

.DESCRIPTION
    Points at .venv\Scripts\pythonw.exe -m phonemic.gui in this project, with the
    phonemic icon. By default a shortcut is placed on the Desktop and in the
    Start Menu; pass -Destination to put a single shortcut somewhere else.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
#>
param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$icon = Join-Path $root "phonemic\assets\phonemic.ico"

if (-not (Test-Path $pythonw)) {
    Write-Error "Missing $pythonw - run setup.bat first."
}

function New-PhonemicShortcut([string]$folder) {
    if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
    $path = Join-Path $folder "phonemic.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "-m phonemic.gui"
    $link.WorkingDirectory = $root
    $link.IconLocation = "$icon,0"
    $link.Description = "Use your phone as a microphone"
    $link.Save()
    Write-Host "Created shortcut: $path"
}

if ($Destination) {
    New-PhonemicShortcut $Destination
} else {
    New-PhonemicShortcut ([Environment]::GetFolderPath("Desktop"))
    New-PhonemicShortcut ([Environment]::GetFolderPath("Programs"))
}
