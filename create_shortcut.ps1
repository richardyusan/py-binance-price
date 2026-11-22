# Creates "Binance Price Viewer" shortcuts (Desktop + Start Menu) that start
# the app through the Windows Python launcher (pyw.exe) with the app icon.
# Run:  powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = (Get-Command pyw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
if (-not $pyw) {
    Write-Error "Python launcher (pyw.exe) not found. Install Python 3 from https://www.python.org/downloads/"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs")
)
foreach ($folder in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $folder "Binance Price Viewer.lnk"))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = "`"$(Join-Path $dir 'binance_viewer.py')`""
    $lnk.WorkingDirectory = $dir
    $lnk.IconLocation = (Join-Path $dir "icon.ico")
    $lnk.Description = "Binance Price Viewer"
    $lnk.Save()
    Write-Output "Created $($lnk.FullName)"
}
