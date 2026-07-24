Set-StrictMode -Version Latest

# OFT-QA V1: standalone HTML -> OFT converter. Offloads a designer's manual OFT re-authoring:
# given an HTML file + its local asset files, produce a faithful .oft with images cid-embedded.
# No QA gate, no console, no web service, no bundle-zip intake in V1 -- just convert.
#
# Migrated 2026-07-16 from Reference\Creative QA System\Code\src\CreativeQaOftQa.psm1 (the "OFT
# leg") into this tool's grammar\ folder, alongside the "HTML leg" (Invoke-PsdToOft.ps1 and the
# rest of the pipeline), so the two are no longer split across separate tools. The Creative QA
# System repo's own Reference\Creative QA System\prds\oft_qa\S1_build_blueprint.md (uncommitted
# there as of the migration) plans to extend a module of this same name/shape with a QA-gate
# layer (Test-CreativeQaOftTechnical / New-CreativeQaOftTechnicalVerdict / Invoke-CreativeQaOftQa)
# -- that plan assumed this file's old location and will need updating (or retiring) before it is
# picked up again.
#
# The PropertyAccessor cid-embedding idiom (schema URIs, Attachments.Add olByValue, SaveAs
# olTemplate=2, COM cleanup) is copied verbatim from the proven GO spike, which still lives in
# the Creative QA System repo:
#   Reference\Creative QA System\spikes\oft_qa\Invoke-OftCidEmbedSpike.ps1
# Run that spike's GO/NO-GO check on a new machine before trusting this module's output.
#
# Standalone by design: this module does not import CreativeQaCore.psm1 or any other engine
# module. V1 needs none of their helpers (no rule packs, no event log, no output-paths writer).

# ---- MAPI PropertyAccessor schema URIs (proptag / type suffix) -- copied from the spike ----
$script:OftQaSchemaContentId = 'http://schemas.microsoft.com/mapi/proptag/0x3712001F'   # PR_ATTACH_CONTENT_ID (PT_UNICODE)
$script:OftQaSchemaMimeTag   = 'http://schemas.microsoft.com/mapi/proptag/0x370E001F'   # PR_ATTACH_MIME_TAG   (PT_UNICODE)
$script:OftQaSchemaHidden    = 'http://schemas.microsoft.com/mapi/proptag/0x7FFE000B'   # PR_ATTACHMENT_HIDDEN (PT_BOOLEAN)

$script:OftQaOlMailItem = 0   # OlItemType.olMailItem
$script:OftQaOlByValue  = 1   # OlAttachmentType.olByValue
$script:OftQaOlTemplate = 2   # OlSaveAsType.olTemplate -- NOTE: 2, not 9 (9 is olMSGUnicode/.msg).
$script:OftQaOlDiscard  = 1   # MailItem.Close olDiscard (the only live user; value 1 also = OlInspectorClose.olDiscard)

function Get-CreativeQaOftMimeFromExtension {
    # Private helper. Maps a file extension to the mime tag stamped on PR_ATTACH_MIME_TAG.
    # Only .png/.jpg/.jpeg/.gif are named by the spec; anything else (e.g. .bmp/.webp) still
    # embeds (SVG is the only hard reject -- see Import-CreativeQaOftHtmlAssets) but falls back
    # to a generic octet-stream tag rather than guessing.
    param([string]$Path)

    $ext = [System.IO.Path]::GetExtension($Path)
    if ([string]::IsNullOrWhiteSpace($ext)) { return "application/octet-stream" }
    switch ($ext.ToLowerInvariant()) {
        ".png"  { return "image/png" }
        ".jpg"  { return "image/jpeg" }
        ".jpeg" { return "image/jpeg" }
        ".gif"  { return "image/gif" }
        default { return "application/octet-stream" }
    }
}

function Import-CreativeQaOftHtmlAssets {
    # Find local image references in $HtmlText (<img src=...>, and a best-effort url(...) inside
    # an inline style attribute). Every reference that resolves to a real file under
    # $AssetsRoot gets a stable cid token; its src/url is rewritten to cid:<token> in the returned
    # HTML. Absolute http(s) URLs (and other off-bundle schemes: data:/cid:/mailto:/tel:/#anchors)
    # are left completely untouched -- they are not local assets to embed.
    #
    # A reference that IS a local-looking path but is missing on disk, resolves outside
    # $AssetsRoot, or ends in .svg is left as-is in the HTML (not embedded) and reported via
    # Write-Warning (per the authoring spec: SVG must be rasterized upstream, this converter
    # does not do it). Callers that need the warning text back (e.g. ConvertTo-CreativeQaOft)
    # capture it with -WarningVariable.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$HtmlText,

        [Parameter(Mandatory = $true)]
        [string]$AssetsRoot
    )

    $assetsRootFull = [System.IO.Path]::GetFullPath($AssetsRoot)
    # Path.GetFullPath does not guarantee a trailing separator; add one so the StartsWith
    # containment check below cannot be fooled by a sibling directory that merely shares the
    # same string prefix (e.g. root "C:\bundle" vs a candidate under "C:\bundle-evil").
    $assetsRootPrefix = $assetsRootFull.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar

    $cidMap = New-Object System.Collections.Generic.List[object]
    $pathToToken = @{}
    $tokenCounter = 0

    # <img ... src="..."> (case-insensitive; single- or double-quoted). Quote characters are
    # hex-escaped (\x22 = ", \x27 = ') so the pattern string needs no fragile PowerShell quoting.
    $imgSrcPattern = '<img\b[^>]*?\bsrc\s*=\s*(?<q>[\x22\x27])(?<url>[^\x22\x27]*)\k<q>'
    # Best-effort url(...) inside an inline style attribute (or anywhere else in the markup).
    # Quotes optional.
    $urlFuncPattern = 'url\(\s*(?<q>[\x22\x27]?)(?<url>[^\x22\x27\)]*)\k<q>\s*\)'
    # VML image references: <v:imagedata src=...> (Word-native picture serialization -- classic
    # Outlook RENDERS the VML branch of a Word-authored picture and ignores its <img> fallback,
    # so leaving these un-rewritten ships a red-X even when the fallback embeds fine; found via
    # the delivered-OFT round-trip 2026-07-13) and <v:fill src=...> (VML image fills).
    $vmlSrcPattern = '<v:(?:imagedata|fill)\b[^>]*?\bsrc\s*=\s*(?<q>[\x22\x27])(?<url>[^\x22\x27]*)\k<q>'

    $regexOptions = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    $rawMatches = New-Object System.Collections.Generic.List[System.Text.RegularExpressions.Match]
    foreach ($m in [regex]::Matches($HtmlText, $imgSrcPattern, $regexOptions)) { $rawMatches.Add($m) }
    foreach ($m in [regex]::Matches($HtmlText, $urlFuncPattern, $regexOptions)) { $rawMatches.Add($m) }
    foreach ($m in [regex]::Matches($HtmlText, $vmlSrcPattern, $regexOptions)) { $rawMatches.Add($m) }

    # Replacements are recorded as (Index, Length, Text) against the URL capture group only (not
    # the whole match) and applied last-to-first below, so rewriting one reference never shifts
    # the recorded position of another reference earlier in the string.
    $replacements = New-Object System.Collections.Generic.List[object]

    foreach ($m in $rawMatches) {
        $urlGroup = $m.Groups['url']
        $raw = $urlGroup.Value
        $trimmed = $raw.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith('#')) { continue }

        $lower = $trimmed.ToLowerInvariant()
        # Off-bundle schemes are never local assets to resolve -- leave every one of these
        # completely untouched. Only http(s) is contractually required by the spec; the rest
        # (data/cid/mailto/tel) mirrors the emitter's own external-prefix list so an
        # already-inlined or already-cid-referenced value is never mistaken for a missing file.
        if ($lower.StartsWith('http://') -or $lower.StartsWith('https://') -or $lower.StartsWith('//') `
                -or $lower.StartsWith('data:') -or $lower.StartsWith('cid:') `
                -or $lower.StartsWith('mailto:') -or $lower.StartsWith('tel:')) {
            continue
        }

        # Strip a query string / fragment before resolving to a file on disk.
        $clean = $trimmed.Split('#')[0]
        $clean = $clean.Split('?')[0]
        $clean = $clean.Trim()
        if ([string]::IsNullOrWhiteSpace($clean)) { continue }

        if ($clean.ToLowerInvariant().EndsWith('.svg')) {
            Write-Warning "SVG asset not embedded (must be rasterized upstream per the Word-engine authoring spec), left as-is: $trimmed"
            continue
        }

        $candidateFull = $null
        $resolvesUnderRoot = $false
        try {
            $relForJoin = $clean.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
            $candidateFull = [System.IO.Path]::GetFullPath((Join-Path -Path $assetsRootFull -ChildPath $relForJoin))
            $resolvesUnderRoot = $candidateFull.StartsWith($assetsRootPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or `
                ($candidateFull -eq $assetsRootFull)
        }
        catch {
            $resolvesUnderRoot = $false
        }

        if (-not $resolvesUnderRoot -or -not (Test-Path -LiteralPath $candidateFull -PathType Leaf)) {
            Write-Warning "Local asset not found under AssetsRoot, left as-is: $trimmed"
            continue
        }

        $pathKey = $candidateFull.ToLowerInvariant()
        if ($pathToToken.ContainsKey($pathKey)) {
            $token = $pathToToken[$pathKey]
        }
        else {
            $tokenCounter++
            $token = "oftimg{0:D4}" -f $tokenCounter
            $pathToToken[$pathKey] = $token
            $mime = Get-CreativeQaOftMimeFromExtension -Path $candidateFull
            $cidMap.Add([pscustomobject][ordered]@{ Token = $token; Path = $candidateFull; Mime = $mime }) | Out-Null
        }

        $replacements.Add([pscustomobject]@{ Index = $urlGroup.Index; Length = $urlGroup.Length; Text = "cid:$token" }) | Out-Null
    }

    $rewritten = $HtmlText
    $orderedReplacements = @($replacements | Sort-Object -Property Index -Descending)
    foreach ($rep in $orderedReplacements) {
        $rewritten = $rewritten.Remove($rep.Index, $rep.Length).Insert($rep.Index, $rep.Text)
    }

    return [pscustomobject][ordered]@{
        RewrittenHtml = $rewritten
        CidMap        = $cidMap.ToArray()
    }
}

function ConvertTo-CreativeQaOft {
    # Convert one HTML file (+ its local assets) into a faithful .oft with images cid-embedded.
    # $AssetsRoot defaults to $HtmlPath's own directory. Opens Outlook via COM, sets HTMLBody
    # (which re-serializes through Word's HTML engine on its own -- see the NOTE below), stamps the
    # proven PropertyAccessor cid triad on each attachment, then SaveAs(OutputOftPath, olTemplate=2).
    # No Inspector is opened.
    #
    # Outlook is a single-instance COM server, so this function OWNS-then-quits carefully: it
    # attaches to an already-running Outlook (the operator's own mail client) when one exists and
    # NEVER quits it; it only calls Quit() on an instance it started itself. This avoids tearing
    # down the operator's open Outlook (and dropping unsaved drafts) -- the hazard the earlier
    # "same house COM idiom" carried.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$HtmlPath,

        [Parameter(Mandatory = $true)]
        [string]$OutputOftPath,

        [string]$AssetsRoot = ""
    )

    if (-not (Test-Path -LiteralPath $HtmlPath -PathType Leaf)) {
        throw "HTML file not found: $HtmlPath"
    }
    $htmlFullPath = [System.IO.Path]::GetFullPath($HtmlPath)

    if ([string]::IsNullOrWhiteSpace($AssetsRoot)) {
        $AssetsRoot = Split-Path -Parent $htmlFullPath
    }
    $assetsRootFull = [System.IO.Path]::GetFullPath($AssetsRoot)

    $htmlText = Get-Content -LiteralPath $htmlFullPath -Raw -Encoding UTF8
    if ($null -eq $htmlText) { $htmlText = "" }

    # Capture Import-CreativeQaOftHtmlAssets' Write-Warning output (missing/SVG assets) so this
    # function can surface it on its own return object instead of leaving it only on the host
    # warning stream. -WarningAction SilentlyContinue suppresses the duplicate host print here;
    # the CLI (Convert-HtmlToOft.ps1) is what prints the warnings to the operator.
    $capturedWarnings = $null
    $assets = Import-CreativeQaOftHtmlAssets -HtmlText $htmlText -AssetsRoot $assetsRootFull `
        -WarningVariable capturedWarnings -WarningAction SilentlyContinue
    $warnings = @($capturedWarnings | ForEach-Object { [string]$_ })

    $outFull = [System.IO.Path]::GetFullPath($OutputOftPath)
    $outParent = Split-Path -Parent $outFull
    if (-not [string]::IsNullOrWhiteSpace($outParent) -and -not (Test-Path -LiteralPath $outParent)) {
        New-Item -ItemType Directory -Force -Path $outParent | Out-Null
    }
    if (Test-Path -LiteralPath $outFull) { Remove-Item -LiteralPath $outFull -Force }

    $outlook = $null
    $outlookCreated = $false
    $mail = $null
    $attachment = $null
    $inspector = $null

    try {
        # Attach to a running Outlook if one exists; only CREATE (and later quit) our own instance.
        try {
            $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
        } catch {
            $outlook = New-Object -ComObject Outlook.Application
            $outlookCreated = $true
        }
        $mail = $outlook.CreateItem($script:OftQaOlMailItem)
        $mail.HTMLBody = $assets.RewrittenHtml

        foreach ($entry in @($assets.CidMap)) {
            $token = [string]$entry.Token
            $assetPath = [string]$entry.Path
            $mime = [string]$entry.Mime

            $attachment = $mail.Attachments.Add($assetPath, $script:OftQaOlByValue)
            if (-not $attachment) { throw "Attachments.Add returned null for: $assetPath" }

            $attachment.PropertyAccessor.SetProperty($script:OftQaSchemaContentId, $token)
            $attachment.PropertyAccessor.SetProperty($script:OftQaSchemaMimeTag, $mime)
            $attachment.PropertyAccessor.SetProperty($script:OftQaSchemaHidden, $true)

            try { [Runtime.InteropServices.Marshal]::ReleaseComObject($attachment) | Out-Null } catch {}
            $attachment = $null
        }

        # NOTE (Opus review): an earlier draft opened the Inspector (Display + Close olDiscard)
        # "so Word normalizes the body before SaveAs". Removed for V1:
        #   - setting HTMLBody already re-serializes through Word's HTML engine (KB 4020759), so
        #     SaveAs writes the Word-normalized body WITHOUT an Inspector;
        #   - the proven GO spike (Invoke-OftCidEmbedSpike.ps1) SaveAs's with no Inspector, so this
        #     is the path we actually validated;
        #   - Display() pops a window and risks an object-model-guard modal in an attended session,
        #     and Close(olDiscard) semantics on a displayed compose item are murky.
        # Re-introduce a WordEditor-based normalize ONLY if a real fidelity gap is observed on a
        # verified template.
        $mail.SaveAs($outFull, $script:OftQaOlTemplate)
        if (-not (Test-Path -LiteralPath $outFull)) { throw "SaveAs did not produce a file at $outFull." }

        return [pscustomobject][ordered]@{
            OftPath    = $outFull
            AssetCount = @($assets.CidMap).Count
            CidMap     = $assets.CidMap
            Warnings   = $warnings
        }
    }
    finally {
        if ($attachment) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($attachment) | Out-Null } catch {} }
        if ($inspector) { try { $inspector.Close($script:OftQaOlDiscard) } catch {} }
        if ($inspector) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($inspector) | Out-Null } catch {} }
        if ($mail) { try { $mail.Close($script:OftQaOlDiscard) } catch {} }
        if ($mail) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($mail) | Out-Null } catch {} }
        # Quit ONLY an instance we started -- never tear down the operator's already-running Outlook.
        if ($outlook -and $outlookCreated) { try { $outlook.Quit() } catch {} }
        if ($outlook) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($outlook) | Out-Null } catch {} }
        [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    }
}

Export-ModuleMember -Function @(
    "Import-CreativeQaOftHtmlAssets",
    "ConvertTo-CreativeQaOft"
)
