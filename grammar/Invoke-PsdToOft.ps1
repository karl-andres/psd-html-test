<#
.SYNOPSIS
  The full pipeline, one command: PSD -> Grammar G HTML bundle -> .OFT -> (optional) classic
  Outlook capture. Fails loud at every certified gate.

.DESCRIPTION
  Steps:
    1. EMIT     psd_html emits the G-dialect HTML bundle (fixed-px tables, bgcolor fills,
                height-attr floors, cid-ready assets, data-region anchors). -LinkManifest
                binds every hyperlink deterministically (slots/regions/inline; see PIPELINE.md).
    2. GATE     per-region fidelity gate vs the PSD design (Chromium proxy; 0 fail required
                unless -SkipGate).
    3. VALIDATE Grammar G conformance (the hub backstop: tables-only grammar, no
                background-image, no SVG, DPI-lock present...).
    4. CONVERT  Convert-HtmlToOft.ps1 (Outlook COM, cid-embeds incl. VML srcs) -> .OFT.
    5. LINKS    (-LinkManifest) verify every manifest URL survived INTO THE .OFT's stored
                HTMLBody -- proves the links travel, not just that they were authored.
    6. CAPTURE  (-Capture) full-length render in classic Outlook via
                Invoke-OftPaintCapture.ps1 -- the Word-engine truth artifact.

.NOTES
  ASCII-only. PowerShell 5.1+. Steps 4-6 need classic Outlook (COM). Exit 0 = OFT produced
  (and captured, if requested) with all gates green.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Psd,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [ValidateSet("hybrid", "live", "raster")][string]$Policy = "hybrid",
    [string]$LinkManifest,
    [int]$EmailIndex = 0,
    [switch]$SkipGate,
    [switch]$Capture
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$toolRoot = Split-Path -Parent $PSScriptRoot                       # ...\Tools\PSD-HTML
$converter = Join-Path $PSScriptRoot "Convert-HtmlToOft.ps1"       # local -- the OFT leg lives in this tool now
$bundle = Join-Path $OutDir "bundle"
New-Item -ItemType Directory -Force $OutDir | Out-Null

Push-Location $toolRoot
try {
    Write-Output "=== 1/6 EMIT ($Policy) ==="
    $emitArgs = @("-m", "psd_html.cli", "emit", $Psd, "-o", $bundle, "--policy", $Policy, "--email-index", $EmailIndex)
    if ($LinkManifest) { $emitArgs += @("--links", $LinkManifest) }
    python @emitArgs | Select-Object -Last 2
    if ($LASTEXITCODE -ne 0) { throw "emit failed" }

    if ($LinkManifest) {
        # An unbound manifest URL is a dead link the designer promised -- fail before convert.
        $linksReport = Get-Content (Join-Path $bundle "links_report.json") -Raw | ConvertFrom-Json
        if ($linksReport.unbound.Count -gt 0) {
            $linksReport.unbound | ForEach-Object { Write-Output ("  UNBOUND: " + $_.kind + " '" + $_.key + "' -> " + $_.url) }
            throw "link manifest has $($linksReport.unbound.Count) unbound URL(s) -- fix links.json or the PSD copy"
        }
        Write-Output ("links bound: " + $linksReport.bound.Count)
    }

    Write-Output "=== 2/6 FIDELITY GATE ==="
    if ($SkipGate) {
        Write-Output "SKIPPED (-SkipGate)"
    } else {
        python -m psd_html.cli gate $Psd $bundle
        if ($LASTEXITCODE -ne 0) { throw "fidelity gate FAILED -- bundle does not match the design (see gate_report.json)" }
    }

    Write-Output "=== 3/6 GRAMMAR G VALIDATION ==="
    $py = "import sys, json; from psd_html.conformance_validator import validate_bundle; " +
          "r = validate_bundle(sys.argv[1]); print(json.dumps({'pass': r['pass'], 'violations': [v['type'] for v in r['violations']]})); " +
          "sys.exit(0 if r['pass'] else 1)"
    python -c $py $bundle
    if ($LASTEXITCODE -ne 0) { throw "Grammar G conformance FAILED" }
} finally {
    Pop-Location
}

Write-Output "=== 4/6 CONVERT -> OFT ==="
$oft = Join-Path $OutDir "email.oft"
& powershell -NoProfile -ExecutionPolicy Bypass -File $converter -HtmlPath (Join-Path $bundle "index.html") -OutputPath $oft
if ($LASTEXITCODE -ne 0) { throw "CONVERT failed -- usually an Outlook/environment issue on THIS machine (classic Outlook missing, not closed, or blocking automation), not a problem with your PSD. Make sure classic Outlook is installed, then re-run." }

if ($LinkManifest) {
    Write-Output "=== 5/6 LINK TRAVEL VERIFY ==="
    # Reopen the .OFT via Outlook COM and assert every bound URL survived into the stored
    # HTMLBody. This is the artifact a recipient's client decodes -- authoring-side presence
    # alone proves nothing about travel.
    $storedPath = Join-Path $OutDir "stored_htmlbody_linkcheck.html"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "Dump-OftHtmlBody.ps1") `
        -OftPath $oft -OutHtml $storedPath
    if ($LASTEXITCODE -ne 0) { throw "LINK VERIFY could not reopen the .OFT in Outlook -- usually an Outlook/environment issue on THIS machine, not a problem with your PSD. Make sure classic Outlook is installed and closed, then re-run." }
    $stored = Get-Content $storedPath -Raw
    # Word entity-encodes characters in the stored href in more forms than just &amp; (&#38;, &#x26;
    # for '&', and other named/numeric entities). Decode the stored body ONCE and match against both
    # the raw and decoded forms, instead of hand-normalizing a single entity -- a surviving but
    # entity-encoded URL was otherwise false-flagged as MISSING.
    $storedDecoded = [System.Net.WebUtility]::HtmlDecode($stored)
    $report = Get-Content (Join-Path $bundle "links_report.json") -Raw | ConvertFrom-Json
    $missing = @()
    foreach ($b in $report.bound) {
        # Match the URL as a COMPLETE quoted attribute value (href="URL") so a genuinely unbound URL
        # that is a prefix/substring of a surviving one (base vs deep path, tracking params) is still
        # flagged -- the closing quote is what prevents a base URL matching inside a longer one.
        $u = [string]$b.url
        $quoted = '"' + $u + '"'
        if (-not ($stored.Contains($quoted) -or $storedDecoded.Contains($quoted))) { $missing += $u }
    }
    if ($missing.Count -gt 0) {
        $missing | ForEach-Object { Write-Output ("  MISSING IN OFT: " + $_) }
        throw "link travel verify FAILED -- $($missing.Count) URL(s) did not survive into the stored HTMLBody"
    }
    Write-Output ("LINKS VERIFIED IN OFT: " + $report.bound.Count + "/" + $report.bound.Count)
}

if ($Capture) {
    Write-Output "=== 6/6 CLASSIC OUTLOOK CAPTURE ==="
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "Invoke-OftPaintCapture.ps1") `
        -OftPath $oft -OutDir (Join-Path $OutDir "capture") -PaintDelayMs 4000
    if ($LASTEXITCODE -ne 0) { throw "CAPTURE failed -- usually an Outlook/environment issue on THIS machine (Outlook not rendering, or display scaling), not a problem with your PSD. The .OFT was already produced; capture is the proof step. Re-run, or open $oft in Outlook to eyeball it." }
}
Write-Output "PIPELINE COMPLETE -> $oft"
