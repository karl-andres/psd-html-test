# Recipient send spot-check (delivery-truth rung). Sends the .oft to a recipient (default: self),
# waits for the DELIVERED copy to arrive, and verifies every bound URL survived into the WIRE
# HTMLBody -- the artifact a recipient's client actually decodes, which Outlook regenerates from
# RTF at send time ([MS-OXCMAIL]). Stage 5 (link-travel verify) only checks the STORED template
# body; this closes the final gap.
#
# SAFETY: this ACTUALLY SENDS EMAIL. Run only against an address you control. It also fires any
# remote image/tracking URLs the email embeds, from this machine's IP. Attaches to a running
# Outlook if present and only quits an instance it started (never the operator's own).
param(
    [Parameter(Mandatory = $true)][string]$OftPath,
    [Parameter(Mandatory = $true)][string]$To,
    [Parameter(Mandatory = $true)][string]$LinksReport,   # bundle/links_report.json ({bound:[{url}]})
    [string]$OutDir = ".",
    [int]$TimeoutSec = 180
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$oftFull = (Resolve-Path -LiteralPath $OftPath).Path
$report = Get-Content -LiteralPath $LinksReport -Raw | ConvertFrom-Json
$bound = @($report.bound)
$token = "OFTSEND-" + ([guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$olFolderInbox = 6
$olFolderSentMail = 5

$outlook = $null
$outlookCreated = $false
$mail = $null
$result = [ordered]@{ token = $token; to = $To; sent = $false; source = $null; verified = $false; missing = @() }
try {
    try {
        $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
    } catch {
        $outlook = New-Object -ComObject Outlook.Application
        $outlookCreated = $true
    }
    $ns = $outlook.GetNamespace("MAPI")

    $mail = $outlook.CreateItemFromTemplate($oftFull)
    $mail.Subject = $token          # unique needle so we can find the delivered copy unambiguously
    $mail.To = $To
    Write-Output "SENDING '$token' -> $To ..."
    $mail.Send()
    $result.sent = $true

    # Force the send/receive cycle so a queued Outbox item actually transmits (a freshly
    # COM-created Outlook may otherwise leave it sitting in the Outbox). SyncObjects.Start()
    # is the documented trigger; wrapped because a headless profile may expose none.
    function Start-SyncAll { try { for ($si = 1; $si -le $ns.SyncObjects.Count; $si++) { $ns.SyncObjects.Item($si).Start() } } catch {} }
    Start-SyncAll

    # Poll the delivered copy: prefer the Inbox (true wire HTML that traversed the server); fall
    # back to Sent Items (Outlook's own send-time serialization) if inbound delivery is slow.
    $inbox = $ns.GetDefaultFolder($olFolderInbox)
    $sent = $ns.GetDefaultFolder($olFolderSentMail)
    $filter = "[Subject] = '$token'"
    $found = $null
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-SyncAll
        Start-Sleep -Seconds 3
        $hitIn = $inbox.Items.Restrict($filter)
        if ($hitIn.Count -gt 0) { $found = $hitIn.Item(1); $result.source = "inbox (delivered wire HTML)"; break }
        $hitSent = $sent.Items.Restrict($filter)
        if ($hitSent.Count -gt 0 -and -not $found) { $found = $hitSent.Item(1); $result.source = "sent items (send-time serialization)" }
    }
    if (-not $found) {
        # Sent copy is better than nothing if inbound never arrived within the window.
        $hitSent = $sent.Items.Restrict($filter)
        if ($hitSent.Count -gt 0) { $found = $hitSent.Item(1); $result.source = "sent items (inbound not delivered in ${TimeoutSec}s)" }
    }
    if (-not $found) { throw "delivered copy '$token' not found in Inbox or Sent within ${TimeoutSec}s" }

    $wire = [string]$found.HTMLBody
    [IO.File]::WriteAllText((Join-Path $OutDir "received_wire_htmlbody.html"), $wire, [Text.Encoding]::UTF8)
    Write-Output ("WIRE HTML source: " + $result.source + " (" + $wire.Length + " chars)")

    # Same match rule as stage-5 link-travel verify: entity-decode the wire body, match the full
    # quoted attribute value so a base URL is not masked by a longer one.
    $wireDecoded = [System.Net.WebUtility]::HtmlDecode($wire)
    $missing = @()
    foreach ($b in $bound) {
        $u = [string]$b.url
        $quoted = '"' + $u + '"'
        if (-not ($wire.Contains($quoted) -or $wireDecoded.Contains($quoted))) { $missing += $u }
    }
    $result.missing = $missing
    $result.verified = ($missing.Count -eq 0)
    ($result | ConvertTo-Json -Depth 6) | Set-Content -Path (Join-Path $OutDir "send_verify_report.json") -Encoding UTF8

    if ($missing.Count -gt 0) {
        $missing | ForEach-Object { Write-Output ("  MISSING IN WIRE HTML: " + $_) }
        throw "send verify FAILED -- $($missing.Count)/$($bound.Count) URL(s) did not survive into the wire HTML"
    }
    Write-Output ("SEND VERIFY OK: " + $bound.Count + "/" + $bound.Count + " links survived into the wire HTML")
} finally {
    if ($mail) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($mail) | Out-Null } catch {} }
    if ($outlook -and $outlookCreated) { try { $outlook.Quit() } catch {} }
    if ($outlook) { try { [Runtime.InteropServices.Marshal]::ReleaseComObject($outlook) | Out-Null } catch {} }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
