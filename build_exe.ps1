# Builds dist\BinancePriceViewer.exe (single file, no console) with PyInstaller
# and a release zip. PyInstaller is installed into an isolated .venv-build.
# Run:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

# Version comes from binance_viewer.py (APP_VERSION = "x.y.z")
$version = (Select-String -Path binance_viewer.py -Pattern '^APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
$parts = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
Write-Output "Building Binance Price Viewer $version"

# 1. Build environment
if (-not (Test-Path .venv-build\Scripts\python.exe)) {
    py -3 -m venv .venv-build
}
$py = Join-Path $dir ".venv-build\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip pyinstaller
if ($LASTEXITCODE) { throw "pip install failed" }

# 2. Windows version resource (shown in the exe's Properties > Details)
New-Item -ItemType Directory -Force build | Out-Null
$year = (Get-Date).Year
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'richardyusan'),
      StringStruct('FileDescription', 'Binance Price Viewer'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'BinancePriceViewer'),
      StringStruct('LegalCopyright', '(c) $year richardyusan'),
      StringStruct('OriginalFilename', 'BinancePriceViewer.exe'),
      StringStruct('ProductName', 'Binance Price Viewer'),
      StringStruct('ProductVersion', '$version'),
      StringStruct('Comments', 'https://github.com/richardyusan/py-binance-price')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content build\version_info.txt -Encoding utf8

# 3. Build
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name BinancePriceViewer `
    --icon icon.ico `
    --version-file build\version_info.txt `
    --add-data "icon.ico;." --add-data "icon.png;." `
    binance_viewer.py
if ($LASTEXITCODE) { throw "PyInstaller failed" }

# 4. Release zip (exe + readme)
$zip = "dist\BinancePriceViewer-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\BinancePriceViewer.exe, README.md -DestinationPath $zip
Get-ChildItem dist | Format-Table Name, @{n = "MB"; e = { "{0:N1}" -f ($_.Length / 1MB) } }
