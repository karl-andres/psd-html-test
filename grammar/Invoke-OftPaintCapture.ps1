<#
.SYNOPSIS
  OFT render-capture spike (hardened): convert-open an .oft in classic Outlook, screenshot the
  live Word-engine render of the Inspector BODY via a deterministic, forgery-proof HWND, plus an
  optional (-AlsoExportPdf, default off) side-by-side ExportAsFixedFormat PDF and the technical-gate
  readbacks. Go/no-go for the self-host render linchpin.

.DESCRIPTION
  Hardened per the build-properly research:
   - HWND acquisition is DETERMINISTIC and forgery-proof (three identity locks), driven from the
     COM Inspector we own - never FindWindow+GetForegroundWindow:
       1. caption lock: the frame is the single 'rctrl_renwnd32' whose caption carries a stamped
          OFTQA-<guid> (rules out the Explorer, which shares the class);
       2. process lock: that frame's PID must belong to an OUTLOOK process;
       3. COM lock: among the frame's '_WwG' children, the one whose Word.Document (via
          AccessibleObjectFromWindow OBJID_NATIVEOM) carries the 'OftQaToken' Word Variable == guid.
     No match -> THROW (never fall back).
   - Capture is the BODY (_WwG), not the Inspector chrome: PrintWindow(frame, PW_RENDERFULLCONTENT)
     into a device-pixel DIB, cropped to the _WwG rect; the black-frame detector is the arbiter.
   - True device pixels: SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2) on the capture thread;
     REQUIRES the worker display pinned to 96 DPI / 100% so device px == authored px -- CHECKED and warned, NOT enforced (see the GetDpiForWindow guard).
   - Positive capture-health beyond near-all-black; emits render_status in {ok, render_capture_failed,
     inconclusive} and ALWAYS writes qa_events.json (so engine_wrap does not misread a capture
     failure as a crash). Exit codes 0/1/3 only.

  Make-or-break question this measures: does capture return REAL pixels (not black/stale) in the
  TRUE production regime - autologon PHYSICAL CONSOLE with a signed virtual display present, through
  an RDP-disconnect (use `tscon <id> /dest:console`) and an idle window. See README.

.NOTES
  ASCII-only. PowerShell 5.1+. SPIKE code. For the real worker: reuse a long-lived Outlook (do not
  Quit per job), enforce queue concurrency=1, and Stop-Process OUTLOOK -Force on a watchdog timeout
  (Outlook is not a child of PowerShell, so process-tree reaping cannot reap it).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $OftPath,
    [string] $OutDir = (Join-Path (Split-Path -Parent $OftPath) 'capture'),
    [int]    $PaintDelayMs = 1500,
    [switch] $AlsoExportPdf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class OftCap {
    public delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr h, StringBuilder s, int max);
    [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int max);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr h);
    [DllImport("oleacc.dll")] public static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object ppv);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

    static string ClassOf(IntPtr h){ var sb=new StringBuilder(256); GetClassName(h, sb, 256); return sb.ToString(); }
    static string TextOf(IntPtr h){ int n=GetWindowTextLength(h); var sb=new StringBuilder(n+2); GetWindowText(h, sb, n+2); return sb.ToString(); }

    // Frame lock: single top-level 'rctrl_renwnd32' whose caption contains the stamped guid.
    public static IntPtr FindFrame(string guidSub){
        IntPtr found = IntPtr.Zero;
        EnumWindows((h,l)=>{
            if(ClassOf(h)=="rctrl_renwnd32" && TextOf(h).IndexOf(guidSub, StringComparison.OrdinalIgnoreCase)>=0){ found=h; return false; }
            return true;
        }, IntPtr.Zero);
        return found;
    }
    public static uint PidOf(IntPtr h){ uint p; GetWindowThreadProcessId(h, out p); return p; }
    public static IntPtr[] FindWwg(IntPtr frame){
        var list=new List<IntPtr>();
        EnumChildWindows(frame, (h,l)=>{ if(ClassOf(h)=="_WwG") list.Add(h); return true; }, IntPtr.Zero);
        return list.ToArray();
    }
}
"@

# PER_MONITOR_AWARE_V2 = (HANDLE)-4. Per-thread override (process manifest of powershell.exe is fixed).
$prevDpiCtx = [OftCap]::SetThreadDpiAwarenessContext([IntPtr](-4))

if (-not (Test-Path $OftPath)) { throw "OFT not found: $OftPath" }
if (-not (Test-Path $OutDir))  { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$PW_RENDERFULLCONTENT = [uint32]2
$OBJID_NATIVEOM = [uint32]4294967280   # 0xFFFFFFF0
$IID_IDispatch = [Guid]'00020400-0000-0000-C000-000000000046'
$olEditorWord = 4
$guid = [Guid]::NewGuid().ToString('N')
$token = "OFTQA-$guid"

$outlook = $null; $mail = $null; $inspector = $null
$result = [ordered]@{
    oft = $OftPath; timestampUtc = (Get-Date).ToUniversalTime().ToString('o'); token = $token
    isWordMail = $null; editorType = $null; messageClass = $null; attachmentCount = $null
    framePid = $null; frameProcess = $null; wwgCount = $null; tokenVerified = $false
    dpiForWindow = $null; capturePng = $null; captureIsBlack = $null; blackRatio = $null
    lumaRange = $null; renderStatus = 'render_capture_failed'; pdf = $null; htmlBodyDump = $null; warnings = @()
}

function Write-Artifacts {
    param($res, $status, $sev, $msg)
    $res.renderStatus = $status
    ($res | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $OutDir 'capture_report.json') -Encoding UTF8
    # ALWAYS emit qa_events.json (spike-shaped) so engine_wrap does not misread a capture failure as a crash.
    $events = [ordered]@{
        schema = 'oftqa-spike/1'; render_status = $status
        events = @( [ordered]@{ check = 'oft_render_capture'; status = $status; severity = $sev; message = $msg;
                                evidence = [ordered]@{ capture = $res.capturePng; blackRatio = $res.blackRatio; lumaRange = $res.lumaRange; tokenVerified = $res.tokenVerified } } )
    }
    ($events | ConvertTo-Json -Depth 8) | Set-Content -Path (Join-Path $OutDir 'qa_events.json') -Encoding UTF8
}

$outlookCreated = $false
try {
    # Attach to a running Outlook if present; only CREATE (and later quit) our own instance so
    # capture never tears down the operator's Outlook (and their unsaved drafts).
    try {
        $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
    } catch {
        $outlook = New-Object -ComObject Outlook.Application
        $outlookCreated = $true
    }
    # Absolute path required: CreateItemFromTemplate resolves a relative path against Outlook's own
    # working directory, not the shell's (relative -> DirectoryNotFoundException).
    $mail = $outlook.CreateItemFromTemplate((Resolve-Path -LiteralPath $OftPath).Path)
    $result.messageClass    = $mail.MessageClass
    $result.attachmentCount = $mail.Attachments.Count
    $mail.Subject = $token   # caption lock

    $result.htmlBodyDump = Join-Path $OutDir 'stored_htmlbody.html'
    Set-Content -Path $result.htmlBodyDump -Value $mail.HTMLBody -Encoding UTF8

    $inspector = $mail.GetInspector
    $inspector.Display($false)
    try { $inspector.WindowState = 2 } catch {}  # olNormalWindow - resizable; SetWindowPos below stretches it past the screen
    Start-Sleep -Milliseconds $PaintDelayMs

    $result.isWordMail = [bool]$inspector.IsWordMail()
    $result.editorType = [int]$inspector.EditorType
    if ($result.editorType -ne $olEditorWord -or -not $result.isWordMail) {
        Write-Artifacts $result 'render_capture_failed' 'error' "Inspector is not the Word editor (EditorType=$($result.editorType), IsWordMail=$($result.isWordMail))."
        throw "Not the Word editor; capture would not reflect the Word engine."
    }

    # COM lock: stamp the guid as a Word document Variable (metadata; no render/reflow), AFTER Display.
    $wordDoc = $inspector.WordEditor
    $wordDoc.Variables.Add('OftQaToken', $guid) | Out-Null

    # 1) frame by caption-guid; 2) assert its PID is an OUTLOOK process
    $frame = [OftCap]::FindFrame($token)
    if ($frame -eq [IntPtr]::Zero -or -not [OftCap]::IsWindow($frame)) { throw "Inspector frame not found by caption guid." }
    $result.framePid = [int][OftCap]::PidOf($frame)
    $result.frameProcess = (Get-Process -Id $result.framePid).ProcessName
    if ($result.frameProcess -ne 'OUTLOOK') { throw "Frame PID $($result.framePid) is '$($result.frameProcess)', not OUTLOOK." }

    # Stretch the Inspector far past the screen: PrintWindow(PW_RENDERFULLCONTENT) renders the
    # window's full logical surface (offscreen included), so a 3000px-tall window captures most of
    # the email in one shot; the scroll-stitch loop below pages any overflow for taller emails.
    [void][OftCap]::SetWindowPos($frame, [IntPtr]::Zero, 0, 0, 1100, 3000, 0x0004)  # SWP_NOZORDER
    Start-Sleep -Milliseconds 1200

    # 3) COM lock: among _WwG children, pick the one whose Word.Document carries our token
    $wwgs = [OftCap]::FindWwg($frame)
    $result.wwgCount = $wwgs.Count
    $bodyHwnd = [IntPtr]::Zero
    foreach ($w in $wwgs) {
        $obj = $null; $iid = $IID_IDispatch
        $hr = [OftCap]::AccessibleObjectFromWindow($w, $OBJID_NATIVEOM, [ref]$iid, [ref]$obj)
        if ($hr -ne 0 -or $null -eq $obj) { continue }
        try {
            $val = $obj.Document.Variables.Item('OftQaToken').Value
            if ($val -eq $guid) { $bodyHwnd = $w; $result.tokenVerified = $true; break }
        } catch { continue }
    }
    if ($bodyHwnd -eq [IntPtr]::Zero) { throw "No _WwG child returned the OftQaToken; refusing to capture (wrong-window guard)." }

    $result.dpiForWindow = [int][OftCap]::GetDpiForWindow($bodyHwnd)
    if ($result.dpiForWindow -ne 96) { $result.warnings += "Display not at 100% (DPI=$($result.dpiForWindow)); pin the worker to 96 DPI so device px == authored px." }

    # Capture the frame with the DirectComposition-safe flag, crop to the body (_WwG) rect.
    $fr = New-Object OftCap+RECT; [void][OftCap]::GetWindowRect($frame, [ref]$fr)
    $br = New-Object OftCap+RECT; [void][OftCap]::GetWindowRect($bodyHwnd, [ref]$br)
    $fw = $fr.Right - $fr.Left; $fh = $fr.Bottom - $fr.Top
    if ($fw -le 0 -or $fh -le 0) { throw "Bad frame rect ($fw x $fh)." }

    $frameBmp = New-Object System.Drawing.Bitmap($fw, $fh)
    $g = [System.Drawing.Graphics]::FromImage($frameBmp); $hdc = $g.GetHdc()
    $ok = [OftCap]::PrintWindow($frame, $hdc, $PW_RENDERFULLCONTENT)
    $g.ReleaseHdc($hdc); $g.Dispose()
    if (-not $ok) { $result.warnings += "PrintWindow(frame) returned false." }

    $cropX = [Math]::Max(0, $br.Left - $fr.Left); $cropY = [Math]::Max(0, $br.Top - $fr.Top)
    $cropW = [Math]::Min($br.Right - $br.Left, $fw - $cropX); $cropH = [Math]::Min($br.Bottom - $br.Top, $fh - $cropY)
    if ($cropW -le 0 -or $cropH -le 0) { $bmp = $frameBmp; $result.warnings += "Body rect degenerate; kept full frame." }
    else {
        $rectObj = New-Object System.Drawing.Rectangle($cropX, $cropY, $cropW, $cropH)
        $bmp = $frameBmp.Clone($rectObj, $frameBmp.PixelFormat)
        $frameBmp.Dispose()
    }

    $result.capturePng = Join-Path $OutDir 'capture.png'
    $bmp.Save($result.capturePng, [System.Drawing.Imaging.ImageFormat]::Png)

    # SCROLL-STITCH: page the Word body down and re-capture, so tall emails are seen end to end.
    for ($pg = 2; $pg -le 6; $pg++) {
        # Drag the caret down through the story -- Word scrolls the caret into view, which pages
        # the body even when WM_VSCROLL/LargeScroll are ignored by the Inspector's _WwG.
        try { [void]$wordDoc.Application.Selection.MoveDown(5, 30) } catch { Write-Warning "Scroll-stitch stopped at page $pg (MoveDown failed): $($_.Exception.Message)"; break }  # 5 = wdLine
        Start-Sleep -Milliseconds 600
        $pbmp = New-Object System.Drawing.Bitmap($fw, $fh)
        $pg2 = [System.Drawing.Graphics]::FromImage($pbmp); $phdc = $pg2.GetHdc()
        [void][OftCap]::PrintWindow($frame, $phdc, $PW_RENDERFULLCONTENT)
        $pg2.ReleaseHdc($phdc); $pg2.Dispose()
        if ($cropW -gt 0 -and $cropH -gt 0) {
            $prect = New-Object System.Drawing.Rectangle($cropX, $cropY, $cropW, $cropH)
            $pcrop = $pbmp.Clone($prect, $pbmp.PixelFormat); $pbmp.Dispose()
        } else { $pcrop = $pbmp }
        $pcrop.Save((Join-Path $OutDir ("capture_p{0}.png" -f $pg)), [System.Drawing.Imaging.ImageFormat]::Png)
        $pcrop.Dispose()
    }

    # Health: black-frame ratio AND luminance range (uniform frame = degraded/half-painted -> inconclusive).
    $w = $bmp.Width; $h = $bmp.Height
    $black = 0; $samples = 0; $lmin = 255; $lmax = 0
    $stepY = [Math]::Max(1, [int]($h / 60)); $stepX = [Math]::Max(1, [int]($w / 60))
    for ($y = 0; $y -lt $h; $y += $stepY) {
        for ($x = 0; $x -lt $w; $x += $stepX) {
            $px = $bmp.GetPixel($x, $y); $samples++
            if ($px.R -lt 8 -and $px.G -lt 8 -and $px.B -lt 8) { $black++ }
            $l = [int](0.299*$px.R + 0.587*$px.G + 0.114*$px.B)
            if ($l -lt $lmin) { $lmin = $l }; if ($l -gt $lmax) { $lmax = $l }
        }
    }
    $bmp.Dispose()
    $result.blackRatio = if ($samples) { [math]::Round($black / $samples, 4) } else { 1 }
    $result.lumaRange = $lmax - $lmin

    if ($result.blackRatio -gt 0.98) {
        $result.captureIsBlack = $true
        Write-Artifacts $result 'render_capture_failed' 'error' "Capture is black ($([math]::Round($result.blackRatio*100))%) - self-host linchpin FAILS in this session regime."
        Write-Host "RESULT: BLACK-FRAME FAIL" -ForegroundColor Red; exit 1
    }
    $result.captureIsBlack = $false
    if ($result.lumaRange -lt 12) {
        Write-Artifacts $result 'inconclusive' 'warning' "Capture is near-uniform (luma range=$($result.lumaRange)) - degraded/half-painted; INCONCLUSIVE, not a pass."
        Write-Host "RESULT: INCONCLUSIVE (uniform capture)" -ForegroundColor Yellow; exit 1
    }

    if ($AlsoExportPdf) {
        # The capture already succeeded above; isolate the PDF export so a PDF fault is reported as
        # a PDF-export problem (warning) and does not unwind to the capture-failure catch, which
        # would misreport a healthy capture as 'render_capture_failed'.
        $wdExportFormatPDF = 17; $wdExportOptimizeForOnScreen = 1
        $result.pdf = Join-Path $OutDir 'exportfixed.pdf'
        try {
            $wordDoc.ExportAsFixedFormat($result.pdf, $wdExportFormatPDF, $false, $wdExportOptimizeForOnScreen)
        } catch {
            $result.pdf = $null
            $result.warnings += "PDF export (ExportAsFixedFormat) failed: $($_.Exception.Message)"
        }
    }

    Write-Artifacts $result 'ok' 'info' "Body capture OK (blackRatio=$($result.blackRatio), lumaRange=$($result.lumaRange), tokenVerified=$($result.tokenVerified))."
    $result.warnings | ForEach-Object { Write-Host "WARN: $_" -ForegroundColor Yellow }
    Write-Host "RESULT: capture OK -> $($result.capturePng)" -ForegroundColor Green
    exit 0
}
catch {
    Write-Host "ENGINE ERROR: $($_.Exception.Message)" -ForegroundColor Red
    try { if ($result.renderStatus -eq 'render_capture_failed' -and -not (Test-Path (Join-Path $OutDir 'qa_events.json'))) { Write-Artifacts $result 'render_capture_failed' 'error' $_.Exception.Message } } catch { Write-Warning "Last-ditch capture artifact write failed: $($_.Exception.Message)" }
    exit 3
}
finally {
    if ($inspector) { try { $inspector.Close(1) } catch {} }  # 1 = olDiscard
    if ($mail)      { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($mail) | Out-Null } catch {} }
    if ($inspector) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($inspector) | Out-Null } catch {} }
    if ($outlook -and $outlookCreated) { try { $outlook.Quit() } catch {} }
    if ($outlook)   { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($outlook) | Out-Null } catch {} }
    if ($prevDpiCtx -ne [IntPtr]::Zero) { [void][OftCap]::SetThreadDpiAwarenessContext($prevDpiCtx) }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}

