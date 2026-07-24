# Reopen a .oft via Outlook COM and dump the stored HTMLBody (the Word-re-serialized artifact).
param(
    [Parameter(Mandatory = $true)][string]$OftPath,
    [Parameter(Mandatory = $true)][string]$OutHtml
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$outlook = $null
$outlookCreated = $false
$item = $null
try {
    # Attach to a running Outlook if present; only CREATE (and later quit) our own instance so a
    # started process is never orphaned and the operator's own Outlook is never torn down.
    try {
        $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
    } catch {
        $outlook = New-Object -ComObject Outlook.Application
        $outlookCreated = $true
    }
    # CreateItemFromTemplate resolves a relative path against Outlook's OWN working directory, not
    # the shell's -- a relative $OftPath throws DirectoryNotFoundException. Always hand it an
    # absolute path (matches Extract-OftBundle's Resolve-Path).
    $oftFull = (Resolve-Path -LiteralPath $OftPath).Path
    $item = $outlook.CreateItemFromTemplate($oftFull)
    $body = $item.HTMLBody
    [System.IO.File]::WriteAllText($OutHtml, $body, [System.Text.Encoding]::UTF8)
    Write-Output "stored chars: $($body.Length)"
    Write-Output "background-image: $([regex]::Matches($body, 'background-image').Count)"
    Write-Output "min-height: $([regex]::Matches($body, 'min-height').Count)"
    Write-Output "height= attr: $([regex]::Matches($body, 'height=').Count)"
    Write-Output "bgcolor: $([regex]::Matches($body, 'bgcolor').Count)"
    Write-Output "img tags: $([regex]::Matches($body, '<img').Count)"
    Write-Output "cid refs: $([regex]::Matches($body, 'cid:').Count)"
    Write-Output "mso-line-height-rule: $([regex]::Matches($body, 'mso-line-height-rule').Count)"
    Write-Output "attachments: $($item.Attachments.Count)"
} finally {
    if ($item) {
        try { $item.Close(1) | Out-Null } catch {}  # olDiscard
        try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($item) | Out-Null } catch {}
    }
    if ($outlook -and $outlookCreated) { try { $outlook.Quit() } catch {} }
    if ($outlook) { try { [System.Runtime.InteropServices.Marshal]::ReleaseComObject($outlook) | Out-Null } catch {} }
}
