# Generates icon.ico (16..256 px) and icon.png for Binance Price Viewer:
# a yellow rounded square with a bold "B" and a blue "Py" badge.
# Run:  powershell -ExecutionPolicy Bypass -File make_icon.ps1
Add-Type -AssemblyName System.Drawing

$dir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$sizes = 16, 24, 32, 48, 64, 128, 256

function New-RoundRect([single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = 2 * $r
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

function Get-FontFamily {
    foreach ($name in "Segoe UI Black", "Arial Black", "Segoe UI", "Arial") {
        try { return New-Object System.Drawing.FontFamily $name } catch {}
    }
}

function Draw-Icon([int]$s) {
    $bmp = New-Object System.Drawing.Bitmap $s, $s, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.PixelOffsetMode   = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)
    $fam = Get-FontFamily

    # Background: Binance-yellow rounded square with a subtle vertical gradient
    $m = [single]([Math]::Max(0.5, $s * 0.03))
    $bg = New-RoundRect $m $m ($s - 2 * $m) ($s - 2 * $m) ([single]($s * 0.2))
    $rect = New-Object System.Drawing.RectangleF 0, 0, $s, $s
    $grad = New-Object System.Drawing.Drawing2D.LinearGradientBrush $rect,
        ([System.Drawing.Color]::FromArgb(255, 252, 213, 53)), ([System.Drawing.Color]::FromArgb(255, 240, 185, 11)), 90
    $g.FillPath($grad, $bg)

    # Big "B"
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = [System.Drawing.StringAlignment]::Center
    $fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
    $bPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $bRect = New-Object System.Drawing.RectangleF ([single]($s * -0.02)), ([single]($s * -0.04)), ([single]($s * 0.82)), ([single]($s * 0.9))
    $bPath.AddString("B", $fam, [int][System.Drawing.FontStyle]::Bold, [single]($s * 0.78), $bRect, $fmt)
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 24, 26, 32))), $bPath)

    # "Py" badge (Python blue pill, yellow text) in the bottom-right corner
    $bw = [single]($s * 0.56); $bh = [single]($s * 0.40)
    $bx = [single]($s - $bw - $s * 0.04); $by = [single]($s - $bh - $s * 0.04)
    $pill = New-RoundRect $bx $by $bw $bh ([single]($bh * 0.32))
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 55, 118, 171))), $pill)
    if ($s -ge 32) {
        $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(255, 24, 26, 32)), ([single]($s * 0.025))
        $g.DrawPath($pen, $pill)
    }
    $pyPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $pyRect = New-Object System.Drawing.RectangleF $bx, ([single]($by + $bh * 0.02)), $bw, $bh
    $pyPath.AddString("Py", $fam, [int][System.Drawing.FontStyle]::Bold, [single]($bh * 0.82), $pyRect, $fmt)
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 255, 212, 59))), $pyPath)

    $g.Dispose()
    return $bmp
}

# Render every size to PNG bytes
$pngs = @()
foreach ($s in $sizes) {
    $bmp = Draw-Icon $s
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $pngs += , $ms.ToArray()
    if ($s -eq 256) { $bmp.Save((Join-Path $dir "icon.png"), [System.Drawing.Imaging.ImageFormat]::Png) }
    $bmp.Dispose()
}

# Pack into a .ico (PNG-compressed entries, supported since Windows Vista)
$out = New-Object System.IO.MemoryStream
$w = New-Object System.IO.BinaryWriter $out
$w.Write([UInt16]0); $w.Write([UInt16]1); $w.Write([UInt16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $s = $sizes[$i]; $len = $pngs[$i].Length
    $dim = if ($s -ge 256) { 0 } else { $s }
    $w.Write([byte]$dim); $w.Write([byte]$dim); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([UInt16]1); $w.Write([UInt16]32); $w.Write([UInt32]$len); $w.Write([UInt32]$offset)
    $offset += $len
}
foreach ($p in $pngs) { $w.Write($p) }
$w.Flush()
[System.IO.File]::WriteAllBytes((Join-Path $dir "icon.ico"), $out.ToArray())
Write-Output "Wrote icon.ico ($($sizes -join ', ') px) and icon.png"
