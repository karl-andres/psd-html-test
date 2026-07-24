<#
.SYNOPSIS
  Standalone HTML -> OFT converter (V1). Given an HTML file and its local asset files, produces
  a faithful .oft with images cid-embedded. No QA gate, no console, no web service, no
  bundle-zip intake -- just convert. The operator runs this directly; Invoke-PsdToOft.ps1 also
  calls it as pipeline stage 4.

.DESCRIPTION
  Thin CLI wrapper around CreativeQaOftQa.psm1 (ConvertTo-CreativeQaOft), in this same folder.

  Migrated 2026-07-16 from Reference\Creative QA System\Code\bin\Convert-HtmlToOft.ps1 into this
  tool's grammar\ folder so the OFT leg (this converter) and the HTML leg (the rest of the
  pipeline) live in one tool.

  (a) Fidelity is bounded by the HTML being authored to the Word-engine spec (see
      AUTHORING_REQUIREMENTS.md in this folder). This converter is faithful to whatever HTML it
      is given, but it cannot fix browser-only HTML (flex/grid layout, CSS backgrounds/gradients
      Word cannot render, JS, live SVG, etc). Author the source HTML to the Word-engine LCD
      first; this tool will not rescue HTML that was not.
  (b) Always verify the result by opening the produced .oft in CLASSIC Outlook (not new Outlook,
      not Outlook on the web) -- classic Outlook's Word rendering engine is the actual target
      this converter is built for.
  (c) The PropertyAccessor cid-embedding idiom used here is proven GO by a spike that still
      lives in the Creative QA System repo: Reference\Creative QA System\spikes\oft_qa\
      Invoke-OftCidEmbedSpike.ps1 (full Outlook-process-restart round-trip of
      PR_ATTACH_CONTENT_ID / PR_ATTACH_MIME_TAG / PR_ATTACHMENT_HIDDEN). Run that GO/NO-GO spike
      first on any new machine before trusting this converter's output.

.PARAMETER HtmlPath
  Path to the source HTML file. Mandatory.

.PARAMETER OutputPath
  Path to write the .oft file. Defaults to HtmlPath with its extension changed to .oft.

.PARAMETER AssetsRoot
  Root directory local image references (<img src=...>, inline style url(...)) resolve against.
  Defaults to HtmlPath's own directory.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File Convert-HtmlToOft.ps1 -HtmlPath C:\design\email.html

.NOTES
  ASCII-only. PowerShell 5.1+. Needs desktop (classic) Outlook installed -- COM automation, not
  run in CI. Exit 0 on success, 1 on error.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$HtmlPath,

    [string]$OutputPath = "",

    [string]$AssetsRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Import-Module (Join-Path -Path $PSScriptRoot -ChildPath "CreativeQaOftQa.psm1") -Force -ErrorAction Stop

try {
    if ([string]::IsNullOrWhiteSpace($OutputPath)) {
        $htmlFull = [System.IO.Path]::GetFullPath($HtmlPath)
        $OutputPath = [System.IO.Path]::ChangeExtension($htmlFull, ".oft")
    }

    $convertArgs = @{ HtmlPath = $HtmlPath; OutputOftPath = $OutputPath }
    if (-not [string]::IsNullOrWhiteSpace($AssetsRoot)) { $convertArgs["AssetsRoot"] = $AssetsRoot }

    $result = ConvertTo-CreativeQaOft @convertArgs

    Write-Host ""
    Write-Host "OFT written: $($result.OftPath)" -ForegroundColor Green
    Write-Host "Embedded assets: $($result.AssetCount)"
    $warnings = @($result.Warnings)
    if ($warnings.Count -gt 0) {
        Write-Host "Warnings ($($warnings.Count)):" -ForegroundColor Yellow
        foreach ($w in $warnings) { Write-Host "  - $w" -ForegroundColor Yellow }
    }
    else {
        Write-Host "Warnings: none"
    }
    Write-Host ""
    Write-Host "Verify by opening the .oft in classic Outlook before shipping it." -ForegroundColor Cyan
    exit 0
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
