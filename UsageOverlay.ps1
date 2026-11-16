#Requires -Version 7
# Always-visible Claude/Codex weekly allowance badges on every monitor's taskbar.
# Reads the local usage-data.json written by collect.py (no network, no
# listener). Placement: the empty taskbar gap nearest the clock; if no gap fits,
[CmdletBinding()]
param(
    [ValidateSet('Taskbar', 'TopRight')][string]$Position = 'Taskbar',
    [ValidateRange(10, 600)][int]$DataSeconds = 30,
    [string]$DataPath = (Join-Path $PSScriptRoot 'usage-data.json'),
    [string]$PageUrl = ''
)
$ErrorActionPreference = 'Stop'

Add-Type @'
using System; using System.Runtime.InteropServices;
public static class OverlayNative {
    [DllImport("user32.dll")] public static extern IntPtr SetProcessDpiAwarenessContext(IntPtr v);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr GetShellWindow();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
    public struct RECT { public int L, T, R, B; }
}
'@
# Per-monitor DPI awareness so all geometry below is in physical pixels.
[OverlayNative]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
[System.Windows.Forms.Application]::EnableVisualStyles()

$mutex = [System.Threading.Mutex]::new($false, 'Local\AIUsageWidget-Overlay')
if (-not $mutex.WaitOne(0)) { exit 0 }

$UIA = [System.Windows.Automation.AutomationElement]
$Key = [System.Drawing.Color]::FromArgb(255, 1, 2, 3)           # transparency key
$Pill = [System.Drawing.Color]::FromArgb(255, 32, 32, 32)
$LabelColor = [System.Drawing.Color]::FromArgb(255, 200, 200, 200)
$Grey = [System.Drawing.Color]::FromArgb(255, 130, 130, 130)
$StaleMinutes = 120

# ---- data -------------------------------------------------------------------
function Get-Prop($el, [string]$name) {
    if ($el.ValueKind -ne 'Object') { return $null }
    $v = [System.Text.Json.JsonElement]::new()
    if ($el.TryGetProperty($name, [ref]$v) -and $v.ValueKind -ne 'Null') { return $v }
    $null
}
function Get-Num($el, [string]$name) {
    $v = Get-Prop $el $name
    if ($v -and $v.ValueKind -eq 'Number') { return $v.GetDouble() } $null
}
function Get-Stamp($el, [string]$name) {
    $v = Get-Prop $el $name
    $t = [DateTimeOffset]::MinValue
    if ($v -and $v.ValueKind -eq 'String' -and [DateTimeOffset]::TryParse($v.GetString(), [ref]$t)) { return $t } $null
}

# Same acceptance rules as usage.html allowanceFor(): weekly window, observation
# inside the current window, reset still in the future; newest observation wins.
function Select-Weekly($root, [string]$tool, [DateTimeOffset]$now) {
    $best = $null
    $hosts = Get-Prop $root 'hosts'
    if (-not $hosts) { return $null }
    foreach ($h in $hosts.EnumerateObject()) {
        $snap = Get-Prop $h.Value 'snapshot'
        $win = Get-Prop (Get-Prop $snap 'windows') $tool
        $cands = [System.Collections.Generic.List[object]]::new()
        $a = Get-Prop (Get-Prop $win 'allowances') 'weekly'
        if ($a) { $cands.Add(@{ m = (Get-Num $a 'window_minutes'); u = (Get-Num $a 'used_percent'); r = (Get-Num $a 'resets_at'); o = (Get-Stamp $a 'observed_at') }) }
        $w = Get-Prop $win 'weekly'
        if ($w) { $cands.Add(@{ m = 10080; u = (Get-Num $w 'used_percent'); r = (Get-Num $w 'resets_at'); o = (Get-Stamp $w 'observed_at') }) }
        if ($tool -eq 'codex' -and ($sessions = Get-Prop $snap 'sessions')) {
            foreach ($s in $sessions.EnumerateArray()) {
                $tv = Get-Prop $s 'tool'
                if (-not $tv -or $tv.GetString() -ne 'codex') { continue }
                $quota = Get-Prop $s 'quota'
                foreach ($k in 'primary', 'secondary') {
                    if ($q = Get-Prop $quota $k) { $cands.Add(@{ m = (Get-Num $q 'window_minutes'); u = (Get-Num $q 'used_percent'); r = (Get-Num $q 'resets_at'); o = (Get-Stamp $s 'quota_at') }) }
                }
            }
        }
        foreach ($c in $cands) {
            if ($c.m -ne 10080 -or $null -eq $c.u -or $c.u -lt 0 -or $c.u -gt 100 -or $null -eq $c.r -or $null -eq $c.o) { continue }
            $reset = [DateTimeOffset]::FromUnixTimeSeconds([long]$c.r)
            if ($c.o -gt $now.AddSeconds(30) -or $c.o -ge $reset -or $c.o -lt $reset.AddMinutes(-10080) -or $reset -le $now) { continue }
            if (-not $best -or $c.o -gt $best.Observed) { $best = @{ Used = $c.u; Reset = $reset; Observed = $c.o } }
        }
    }
    $best
}

$script:Usage = @{ claude = $null; codex = $null }
function Update-Usage {
    try {
        $fs = [System.IO.File]::Open($DataPath, 'Open', 'Read', 'ReadWrite, Delete')
        try { $doc = [System.Text.Json.JsonDocument]::Parse($fs) } finally { $fs.Dispose() }
        try {
            $now = [DateTimeOffset]::UtcNow
            $script:Usage = @{ claude = (Select-Weekly $doc.RootElement 'claude' $now); codex = (Select-Weekly $doc.RootElement 'codex' $now) }
        } finally { $doc.Dispose() }
    } catch {
        # File mid-rewrite or temporarily unreadable: keep the last good values.
    }
}

function Get-UsageColor([double]$used) {
    if ($used -ge 80) { return [System.Drawing.Color]::FromArgb(255, 248, 81, 73) }
    if ($used -ge 50) { return [System.Drawing.Color]::FromArgb(255, 230, 170, 40) }
    [System.Drawing.Color]::FromArgb(255, 63, 185, 80)
}

function Get-Line([string]$label, $u) {
    if (-not $u -or $u.Reset -le [DateTimeOffset]::UtcNow) { return @{ Label = $label; Value = '--'; Color = $Grey; Reset = '' } }
    $stale = ([DateTimeOffset]::UtcNow - $u.Observed).TotalMinutes -gt $StaleMinutes
    @{ Label = $label; Value = ('{0:0}%' -f $u.Used); Color = ($stale ? $Grey : (Get-UsageColor $u.Used)); Reset = (Format-ResetIn $u.Reset) }
}

# ---- placement --------------------------------------------------------------
$OccupiedPattern = '^(ToggleButton|Taskbar\.TaskListButtonAutomationPeer|SystemTray\.)'
function Get-Taskbars {
    $cond = [System.Windows.Automation.OrCondition]::new(
        [System.Windows.Automation.PropertyCondition]::new($UIA::ClassNameProperty, 'Shell_TrayWnd'),
        [System.Windows.Automation.PropertyCondition]::new($UIA::ClassNameProperty, 'Shell_SecondaryTrayWnd'))
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    foreach ($tb in $UIA::RootElement.FindAll('Children', $cond)) {
        $r = $tb.Current.BoundingRectangle
        $busy = [System.Collections.Generic.List[object]]::new()
        $stack = [System.Collections.Generic.Stack[object]]::new()
        $stack.Push(@($tb, 0))
        while ($stack.Count) {
            $e, $d = $stack.Pop()
            $ch = $walker.GetFirstChild($e)
            while ($ch) {
                if ($ch.Current.ClassName -match $OccupiedPattern) {
                    $b = $ch.Current.BoundingRectangle
                    if ($b.Width -gt 0) { $busy.Add(@($b.Left, $b.Right)) }
                } elseif ($d -lt 3) { $stack.Push(@($ch, $d + 1)) }
                $ch = $walker.GetNextSibling($ch)
            }
        }
        [pscustomobject]@{ Rect = $r; Busy = @($busy | Sort-Object { $_[0] }) }
    }
}

# Gaps between occupied taskbar items, rightmost (nearest the clock) first.
function Get-Gaps($tb) {
    $gaps = @(); $x = $tb.Rect.Left
    foreach ($b in $tb.Busy) { if ($b[0] -gt $x) { $gaps += , @($x, $b[0]) }; $x = [Math]::Max($x, $b[1]) }
    if ($tb.Rect.Right -gt $x) { $gaps += , @($x, $tb.Rect.Right) }
    [array]::Reverse($gaps); $gaps
}

function Measure-Badge([int]$fontPx, [bool]$withReset) {
    $font = [System.Drawing.Font]::new('Segoe UI Semibold', $fontPx, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
    $lw = [System.Windows.Forms.TextRenderer]::MeasureText('Claude', $font).Width
    $vw = [System.Windows.Forms.TextRenderer]::MeasureText('100%', $font).Width
    $rw = $withReset ? [System.Windows.Forms.TextRenderer]::MeasureText('resets 6d 23h', $font).Width : 0
    $pad = [int]($fontPx * 0.4); $gap = [int]($fontPx * 0.25)
    $resetGap = $withReset ? [int]($fontPx * 0.8) : 0
    @{ Font = $font; LabelW = $lw; ValueW = $vw; ResetW = $rw; ResetGap = $resetGap; Pad = $pad
       Width = $pad + $lw + $gap + $vw + $resetGap + $rw + $pad }
}

function Format-ResetIn([DateTimeOffset]$reset) {
    $t = $reset - [DateTimeOffset]::UtcNow
    if ($t.TotalMinutes -lt 1) { return 'resetting' }
    if ($t.TotalHours -ge 24) { return 'resets {0}d {1}h' -f $t.Days, $t.Hours }
    if ($t.TotalMinutes -ge 60) { return 'resets {0}h {1}m' -f [int][Math]::Floor($t.TotalHours), $t.Minutes }
    'resets {0}m' -f $t.Minutes
}

# ---- windows ----------------------------------------------------------------
$script:Overlays = [System.Collections.Generic.List[object]]::new()
$menu = [System.Windows.Forms.ContextMenuStrip]::new()
$menu.Items.Add('Refresh now', $null, { Update-Usage; Update-Layout; Invoke-Repaint }) | Out-Null
$menu.Items.Add('Exit', $null, { [System.Windows.Forms.Application]::Exit() }) | Out-Null

function New-Overlay {
    $f = [System.Windows.Forms.Form]::new()
    $f.FormBorderStyle = 'None'; $f.ShowInTaskbar = $false; $f.TopMost = $true
    $f.StartPosition = 'Manual'; $f.AutoScaleMode = 'None'
    $f.BackColor = $Key; $f.TransparencyKey = $Key
    $f.Text = 'AI Usage Widget'
    $f.Add_DpiChanged({ param($s, $e) $e.Cancel = $true })
    $f.Add_Paint({ param($s, $e) Show-Badge $s $e.Graphics })
    $f.Add_MouseUp({ param($s, $e)
        if ($e.Button -eq 'Right') { $menu.Show($s, $e.Location) }
    })
    $f.Show()
    $f
}

function Set-ExStyle($form, [bool]$clickThrough) {
    # WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED (+ WS_EX_TRANSPARENT)
    $ex = [OverlayNative]::GetWindowLong($form.Handle, -20) -bor 0x80 -bor 0x08000000 -bor 0x80000
    $ex = $clickThrough ? ($ex -bor 0x20) : ($ex -band -bnot 0x20)
    [OverlayNative]::SetWindowLong($form.Handle, -20, $ex) | Out-Null
}

function Show-Badge($form, $g) {
    $m = $form.Tag
    if (-not $m) { return }
    $g.SmoothingMode = 'None'
    $brush = [System.Drawing.SolidBrush]::new($Pill)
    $g.FillRectangle($brush, 0, 0, $form.Width, $form.Height); $brush.Dispose()
    $lines = @((Get-Line 'Claude' $script:Usage.claude), (Get-Line 'Codex' $script:Usage.codex))
    $lineH = [int]($form.Height / 2)
    for ($i = 0; $i -lt 2; $i++) {
        $y = $i * $lineH
        $lr = [System.Drawing.Rectangle]::new($m.Pad, $y, $m.LabelW, $lineH)
        $vEnd = $form.Width - $m.Pad - $m.ResetW - $m.ResetGap
        $vr = [System.Drawing.Rectangle]::new($vEnd - $m.ValueW, $y, $m.ValueW, $lineH)
        [System.Windows.Forms.TextRenderer]::DrawText($g, $lines[$i].Label, $m.Font, $lr, $LabelColor, $Pill, 'Left, VerticalCenter, NoPadding')
        [System.Windows.Forms.TextRenderer]::DrawText($g, $lines[$i].Value, $m.Font, $vr, $lines[$i].Color, $Pill, 'Right, VerticalCenter, NoPadding')
        if ($m.ResetW) {
            $rr = [System.Drawing.Rectangle]::new($form.Width - $m.Pad - $m.ResetW, $y, $m.ResetW, $lineH)
            [System.Windows.Forms.TextRenderer]::DrawText($g, $lines[$i].Reset, $m.Font, $rr, $Grey, $Pill, 'Left, VerticalCenter, NoPadding')
        }
    }
}

function Update-Layout {
    $targets = @()
    if ($Position -eq 'Taskbar') {
        try { $bars = @(Get-Taskbars) } catch { $bars = @() }
    } else { $bars = @() }
    foreach ($screen in [System.Windows.Forms.Screen]::AllScreens) {
        $sb = $screen.Bounds
        $tb = $bars | Where-Object { $c = $_.Rect; $c.Left -ge $sb.Left - 8 -and $c.Right -le $sb.Right + 8 -and $c.Top -ge $sb.Top - 8 -and $c.Bottom -le $sb.Bottom + 8 } | Select-Object -First 1
        $placed = $null
        if ($tb -and $tb.Rect.Width -gt $tb.Rect.Height) {
            $h = [int]$tb.Rect.Height
            $margin = [int]($h * 0.12); $side = [int]($h * 0.05)
            # Largest legible font that fits a gap, with the reset column if
            # possible; shrink, then drop the reset column, before giving up.
            foreach ($withReset in $true, $false) {
                for ($fp = [int]($h * 0.30); $fp -ge [int]($h * 0.18) -and -not $placed; $fp--) {
                    $m = Measure-Badge $fp $withReset
                    foreach ($gap in (Get-Gaps $tb)) {
                        if ($gap[1] - $gap[0] -ge $m.Width + 2 * $side) {
                            $bh = $h - 2 * $margin
                            $placed = @{ X = [int]($gap[1] - $side - $m.Width); Y = [int]$tb.Rect.Top + $margin; W = $m.Width; H = $bh; M = $m; Click = $false }
                            break
                        }
                    }
                }
            }
        }
        if (-not $placed) {
            # Fallback: top-right corner of the work area; click-through so it never
            # blocks a maximized window's caption buttons beneath it.
            $wa = $screen.WorkingArea
            $h = [int]([Math]::Max(48, $wa.Height * 0.045))
            $m = Measure-Badge ([int]($h * 0.34)) $true
            $placed = @{ X = $wa.Right - $m.Width - [int]($h * 0.25); Y = $wa.Top + [int]($h * 0.9); W = $m.Width; H = $h; M = $m; Click = $true }
        }
        $targets += , $placed
    }
    while ($script:Overlays.Count -lt $targets.Count) { $script:Overlays.Add((New-Overlay)) }
    while ($script:Overlays.Count -gt $targets.Count) { $script:Overlays[-1].Close(); $script:Overlays.RemoveAt($script:Overlays.Count - 1) }
    for ($i = 0; $i -lt $targets.Count; $i++) {
        $f = $script:Overlays[$i]; $t = $targets[$i]
        $f.Tag = $t.M
        Set-ExStyle $f $t.Click
        $f.Bounds = [System.Drawing.Rectangle]::new($t.X, $t.Y, $t.W, $t.H)
        $f.Invalidate()
    }
}

function Invoke-Repaint { foreach ($f in $script:Overlays) { $f.Invalidate() } }

# Keep above the taskbar (which re-raises itself when clicked); hide on a
# monitor whose foreground window is fullscreen (games, video).
function Assert-Topmost {
    $fg = [OverlayNative]::GetForegroundWindow()
    $full = $null
    if ($fg -ne [IntPtr]::Zero -and $fg -ne [OverlayNative]::GetShellWindow()) {
        $cls = [System.Text.StringBuilder]::new(64); [OverlayNative]::GetClassName($fg, $cls, 64) | Out-Null
        if ($cls.ToString() -notin 'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd') {
            $r = [OverlayNative+RECT]::new()
            if ([OverlayNative]::GetWindowRect($fg, [ref]$r)) {
                $s = [System.Windows.Forms.Screen]::FromHandle($fg).Bounds
                if ($r.L -le $s.Left -and $r.T -le $s.Top -and $r.R -ge $s.Right -and $r.B -ge $s.Bottom) { $full = $s }
            }
        }
    }
    foreach ($f in $script:Overlays) {
        $hide = $full -and $full.Contains($f.Bounds.Location)
        if ($hide -and $f.Visible) { $f.Hide() } elseif (-not $hide -and -not $f.Visible) { $f.Show() }
        # HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE
        if (-not $hide) { [OverlayNative]::SetWindowPos($f.Handle, [IntPtr](-1), 0, 0, 0, 0, 0x13) | Out-Null }
    }
}

Update-Usage
Update-Layout

$dataTimer = [System.Windows.Forms.Timer]::new(); $dataTimer.Interval = $DataSeconds * 1000
$dataTimer.Add_Tick({ Update-Usage; Invoke-Repaint }); $dataTimer.Start()
$layoutTimer = [System.Windows.Forms.Timer]::new(); $layoutTimer.Interval = 15000
$layoutTimer.Add_Tick({ Update-Layout }); $layoutTimer.Start()
$topTimer = [System.Windows.Forms.Timer]::new(); $topTimer.Interval = 1000
$topTimer.Add_Tick({ Assert-Topmost }); $topTimer.Start()
[Microsoft.Win32.SystemEvents]::add_DisplaySettingsChanged({ Update-Layout })

try { [System.Windows.Forms.Application]::Run() }
finally { $mutex.ReleaseMutex() }
