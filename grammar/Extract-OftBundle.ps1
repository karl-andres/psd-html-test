<#
.SYNOPSIS
  Extract an .OFT into an editable HTML bundle: index.html + assets\ with every attachment
  (inline cid images and any others) saved to disk and the HTML's cid: references rewritten to
  relative paths.

.DESCRIPTION
  The inverse of Convert-HtmlToOft.ps1. Together they close the loop:
    human.oft -> Extract-OftBundle -> bundle -> Convert-HtmlToOft -> regenerated.oft
  and the two .OFTs can be captured in classic Outlook and compared render-to-render. Also the
  entry point for editing existing templates as plain HTML.

.NOTES
  ASCII-only. PowerShell 5.1+. Requires classic Outlook (COM). Exit 0 on success.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OftPath,
    [Parameter(Mandatory = $true)][string]$OutDir,
    # diagnostic: 1-based VML-block indexes to LEAVE UNTOUCHED (bisecting normalizer effects)
    [int[]]$SkipNormalizeIndex = @(),
    # OPT-IN: rewrite Word-native VML pictures to plain <img> (Grammar G dialect). Default OFF:
    # the converter now cid-rewrites v:imagedata/v:fill srcs directly, and keeping Word's own
    # VML preserves absolute-object geometry flow normalization cannot reproduce (a floated
    # picture can OVERHANG its table cell -- measured +24px stack shift when flattened to flow).
    # Turn on only when the output must validate against strict Grammar G.
    [switch]$NormalizeVml
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"

New-Item -ItemType Directory -Force $OutDir | Out-Null
$assetsDir = Join-Path $OutDir "assets"
New-Item -ItemType Directory -Force $assetsDir | Out-Null

$outlook = $null
$outlookCreated = $false
$item = $null
try {
    # Attach to a running Outlook if present; only CREATE (and later quit) our own instance.
    try {
        $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
    } catch {
        $outlook = New-Object -ComObject Outlook.Application
        $outlookCreated = $true
    }
    $item = $outlook.CreateItemFromTemplate((Resolve-Path $OftPath).Path)
    $html = $item.HTMLBody
    $count = $item.Attachments.Count
    Write-Output "attachments: $count"
    # Collect cid -> rel here; apply the rewrites AFTER the loop, longest cid first (see below).
    $cidReplacements = @()
    for ($i = 1; $i -le $count; $i++) {
        $att = $item.Attachments.Item($i)
        $cid = $null
        try { $cid = $att.PropertyAccessor.GetProperty($PR_ATTACH_CONTENT_ID) } catch { Write-Warning "Attachment $i - could not read PR_ATTACH_CONTENT_ID (no content-id or COM fault): $($_.Exception.Message)" }
        # SECURITY: reduce to a bare filename. A crafted OFT can carry an attachment whose
        # FileName has path components (..\, absolute, drive) -- used verbatim, SaveAsFile below
        # would write OUTSIDE assetsDir (zip-slip / arbitrary file write). GetFileName strips them.
        $fname = [IO.Path]::GetFileName([string]$att.FileName)
        if ([string]::IsNullOrWhiteSpace($fname)) { $fname = "attachment_$i.bin" }
        # de-collide file names
        $target = Join-Path $assetsDir $fname
        $n = 1
        while (Test-Path $target) {
            $target = Join-Path $assetsDir ("{0}_{1}{2}" -f [IO.Path]::GetFileNameWithoutExtension($fname), $n, [IO.Path]::GetExtension($fname))
            $n++
        }
        $att.SaveAsFile($target)
        $rel = "assets/" + [IO.Path]::GetFileName($target)
        if (-not [string]::IsNullOrWhiteSpace($cid)) {
            $cidReplacements += [pscustomobject]@{ Cid = [string]$cid; Rel = $rel }
            Write-Output "  cid:$cid -> $rel"
        } else {
            Write-Output "  (no content-id) saved $rel"
        }
    }
    # Rewrite cid refs longest cid first: a shorter cid (cid:image1) is a prefix of a longer one
    # (cid:image10), so replacing it first would corrupt the longer ref. Sorting by length desc
    # removes the longer refs before the shorter ones can match inside them.
    foreach ($r in ($cidReplacements | Sort-Object { $_.Cid.Length } -Descending)) {
        $html = $html.Replace("cid:$($r.Cid)", $r.Rel)
    }
    # --- OPT-IN: Normalize Word-native VML picture serialization to Grammar G -------------------
    # Word writes every picture TWICE: a VML branch (<!--[if gte vml 1]><v:shape><v:imagedata
    # src=...></v:shape><![endif]-->, carrying Word-only crop data) plus an <img> fallback in a
    # downlevel-revealed <![if !vml]>...<![endif]> conditional. Word RENDERS the VML branch and
    # ignores the img. DEFAULT PATH (no -NormalizeVml): leave the VML intact -- the converter
    # cid-rewrites v:imagedata/v:fill srcs, and Word's own constructs reproduce Word's own
    # geometry exactly (absolute objects may OVERHANG their cell; flow can't fake that).
    if ($NormalizeVml) {
    Add-Type -AssemblyName System.Drawing

    function Convert-CropFraction([string]$v) {
        if ([string]::IsNullOrWhiteSpace($v)) { return 0.0 }
        return [double]($v.TrimEnd('f')) / 65536.0
    }

    $vmlBlockRe = [regex]'(?s)<!--\[if gte vml 1\]>(.*?)<!\[endif\]-->'
    $fallbackRe = [regex]'(?s)^\s*<!\[if !vml\]>(.*?)<!\[endif\]>'
    $imagedataRe = [regex]'<v:imagedata[^>]*\ssrc="([^"]+)"[^>]*/?>'
    $dimRe = [regex]"width:\s*([\d.]+)pt;.*?height:\s*([\d.]+)pt"
    $cropRe = [regex]'\s(croptop|cropbottom|cropleft|cropright)="([^"]+)"'

    $sb = New-Object System.Text.StringBuilder
    $pos = 0
    $normalized = 0
    $synthesized = 0
    $blockIdx = 0
    foreach ($m in $vmlBlockRe.Matches($html)) {
        $blockIdx++
        [void]$sb.Append($html.Substring($pos, $m.Index - $pos))
        $pos = $m.Index + $m.Length
        if ($SkipNormalizeIndex -contains $blockIdx) {
            [void]$sb.Append($m.Value)
            Write-Output "  block $blockIdx left untouched (SkipNormalizeIndex)"
            continue
        }
        $inner = $m.Groups[1].Value
        $after = $html.Substring($pos)
        $fb = $fallbackRe.Match($after)
        if ($fb.Success) {
            # paired picture: keep the fallback's inner HTML (the working <img>), drop the VML.
            $inner = $fb.Groups[1].Value
            # Word-only geometry trap: the fallback is often <span style='position:absolute;...'>
            # <img></span> -- CSS engines float it like the VML object did, but Word IGNORES
            # position:absolute, so the img falls into flow and its line-box adds height the
            # floating original never contributed (live-caught: +24px above the stats block and
            # a re-triggered mso-border-alt rule after the bullets). Normalize: unwrap the span,
            # transfer its margins onto the img, and force display:block so both engines give it
            # the same flow geometry.
            $absSpan = [regex]::Match($inner, "(?s)^\s*<span[^>]*style='[^']*position:absolute[^']*'[^>]*>(.*)</span>\s*$")
            if ($absSpan.Success) {
                $content = $absSpan.Groups[1].Value
                $mL = 0; $mT = 0
                $mlm = [regex]::Match($fb.Groups[1].Value, "margin-left:\s*(-?[\d.]+)px")
                $mtm = [regex]::Match($fb.Groups[1].Value, "margin-top:\s*(-?[\d.]+)px")
                if ($mlm.Success) { $mL = [double]$mlm.Groups[1].Value }
                if ($mtm.Success) { $mT = [double]$mtm.Groups[1].Value }
                $blockStyle = "display:block;margin-left:$($mL)px;margin-top:$($mT)px;"
                if ($content -match "<img[^>]*\sstyle=") {
                    $content = [regex]::Replace($content, "(<img[^>]*\sstyle=[`"'])", ('$1' + $blockStyle), 1)
                } else {
                    $content = [regex]::Replace($content, "<img\s", "<img style=`"$blockStyle`" ", 1)
                }
                $inner = $content
            }
            [void]$sb.Append($inner)
            $pos += $fb.Length
            $normalized++
            continue
        }
        $idm = $imagedataRe.Match($inner)
        if (-not $idm.Success) {
            # non-picture VML (rects/lines) with no fallback: drop the Word-only branch
            $normalized++
            continue
        }
        # VML-only picture: synthesize an <img>
        $srcRel = $idm.Groups[1].Value
        $dm = $dimRe.Match($inner)
        $wpx = 0; $hpx = 0
        if ($dm.Success) {
            $wpx = [int][Math]::Round([double]$dm.Groups[1].Value * 96.0 / 72.0)
            $hpx = [int][Math]::Round([double]$dm.Groups[2].Value * 96.0 / 72.0)
        }
        $crop = @{ croptop = 0.0; cropbottom = 0.0; cropleft = 0.0; cropright = 0.0 }
        foreach ($cm in $cropRe.Matches($idm.Value)) { $crop[$cm.Groups[1].Value] = Convert-CropFraction $cm.Groups[2].Value }
        $imgSrc = $srcRel
        if (($crop.Values | Measure-Object -Sum).Sum -gt 0 -and $srcRel.StartsWith("assets/")) {
            $srcFile = Join-Path $OutDir ($srcRel -replace "/", "\")
            if (Test-Path $srcFile) {
                $orig = [System.Drawing.Image]::FromFile($srcFile)
                try {
                    $cl = [int]($crop.cropleft * $orig.Width); $ct = [int]($crop.croptop * $orig.Height)
                    $cw = [int]($orig.Width - $cl - $crop.cropright * $orig.Width)
                    $ch = [int]($orig.Height - $ct - $crop.cropbottom * $orig.Height)
                    if ($cw -gt 0 -and $ch -gt 0) {
                        $bmp = New-Object System.Drawing.Bitmap($cw, $ch)
                        $gfx = [System.Drawing.Graphics]::FromImage($bmp)
                        $gfx.DrawImage($orig, (New-Object System.Drawing.Rectangle(0, 0, $cw, $ch)),
                            (New-Object System.Drawing.Rectangle($cl, $ct, $cw, $ch)), [System.Drawing.GraphicsUnit]::Pixel)
                        $gfx.Dispose()
                        $croppedName = "cropped_" + [IO.Path]::GetFileName($srcFile)
                        $bmp.Save((Join-Path $assetsDir $croppedName), [System.Drawing.Imaging.ImageFormat]::Png)
                        $bmp.Dispose()
                        $imgSrc = "assets/$croppedName"
                        Write-Output "  cropped $srcRel -> $imgSrc (crop L$($crop.cropleft.ToString('0.###')) T$($crop.croptop.ToString('0.###')) R$($crop.cropright.ToString('0.###')) B$($crop.cropbottom.ToString('0.###')))"
                    } else {
                        Write-Warning "crop present but not applied for $srcRel - degenerate crop dims ($($cw)x$($ch)); kept uncropped src"
                    }
                } finally { $orig.Dispose() }
            } else {
                Write-Warning "crop present but not applied for $srcRel - source file not found ($srcFile); kept uncropped src"
            }
        }
        $dims = ""
        if ($wpx -gt 0 -and $hpx -gt 0) { $dims = " width=`"$wpx`" height=`"$hpx`"" }
        [void]$sb.Append("<img$dims src=`"$imgSrc`" alt=`"`" style=`"display:block;border:0;`">")
        $synthesized++
    }
    [void]$sb.Append($html.Substring($pos))
    $html = $sb.ToString()
    # NEVER blanket-strip <![endif]> markers: Word HTML carries OTHER downlevel-revealed
    # conditionals (<![if !supportLists]> around list bullets) whose openers we don't touch --
    # orphaning them makes Word (where !supportLists is FALSE) skip everything to EOF looking
    # for a closer that no longer exists (live-caught: the email truncated at the first bullet).
    # The paired unwrap above already consumes exactly its own <![if !vml]>...<![endif]> pair.
    Write-Output "normalized: $normalized VML branches removed/unwrapped, $synthesized img synthesized"
    }

    [IO.File]::WriteAllText((Join-Path $OutDir "index.html"), $html, [Text.Encoding]::UTF8)
    Write-Output "BUNDLE -> $OutDir (index.html + $count assets)"
} finally {
    if ($item) {
        try { $item.Close(1) | Out-Null } catch {}
        try { [Runtime.InteropServices.Marshal]::ReleaseComObject($item) | Out-Null } catch {}
    }
    # Quit ONLY an instance we started -- never the operator's already-running Outlook.
    if ($outlook -and $outlookCreated) { try { $outlook.Quit() } catch {} }
    if ($outlook) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($outlook) | Out-Null } catch {} }
}
