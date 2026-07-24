---
name: Email Builder Distribution PRD
overview: Write a PRD markdown doc covering how to distribute the unified Email Builder (Jon's Figma→HTML + Kai's PSD→HTML→OFT) to the company via Intercept OS, with a phased M365-native OFT strategy and a stakeholder open-questions register.
todos:
  - id: write-prd
    content: Write docs/DISTRIBUTION-PRD.md with the structure above (architecture diagram, phased plan, options matrix, open-questions register)
    status: pending
isProject: false
---

# Email Builder Distribution PRD

Deliverable: one PRD markdown doc at `docs/DISTRIBUTION-PRD.md` in this workspace, ready to share for the Francis/Adrian regroup.

## Decisions locked during grilling (encoded as the PRD's recommendation)

- **Scope**: both tools, one unified "Email Builder" entry; Intercept OS is a launcher, so the builder is a separately hosted web app with a tile linking out.
- **OFT strategy — phased**:
  - Phase 1: Windows 365 Cloud PC (or Azure Windows VM) with classic Outlook as the OFT worker, running Kai's proven COM pipeline behind a job queue (concurrency 1, long-lived Outlook, watchdog — matches Kai's own roadmap item). M365-native: existing Outlook licensing, Entra service account, Intune-managed.
  - In parallel: MsgKit (free) / Aspose (paid) fidelity spike — library-written OFT compared against COM-produced OFT on real campaigns; switch only if designers sign off.
  - EML export added as a cheap extra output in the Python service (Claudia/Daphne asked for it). Power Automate route documented as **rejected** (produces EML not OFT, misses CID images, needs premium license for what stdlib does).
- **Hosting (Phase 1, pending security review)**: HTML services on Render (Jon's `DEPLOY.md` already targets it, team familiarity); OFT worker on the Microsoft side; Azure Storage Queue/Service Bus bridges them. Consolidate-to-Azure noted as the alternative.
- **Artifacts**: outputs (HTML zip, `email.oft`, capture proof, QA reports) delivered to the *HTML Email Builder* SharePoint site; Teams notification on completion via Graph.
- **Figma→OFT** (Jon's HTML through Kai's convert/link-verify/capture back half): Phase 2, after conformance adaptation.

## PRD document structure

1. Summary, goals, non-goals (no copy approval, CDN, ESP load, scheduling — per Jon's boundaries)
2. Background: today's workflow (outsourced HTML/OFT coding), the two tools, OFT mandate
3. Users and workflows (designer / operator / campaign team)
4. Target architecture with mermaid diagram: Intercept OS tile → unified web UI → Figma factory service + PSD pipeline service → queue → Windows OFT worker → SharePoint + Teams
5. Options considered for OFT (COM worker, MsgKit, Aspose, Graph/Power Automate — with the rejection rationale)
6. Required engineering work:
   - Kai's tool: web intake API around `psd_html` (upload PSD + links), web port of the link-editor form (reuses `src/psd_html/link_scaffold.py` slot discovery), worker-ize the PowerShell orchestration as a queue consumer, harden trust model for uploads (size limits, temp cleanup, fonts on worker)
   - Jon's tool: deploy per `docs/DEPLOY.md`, agree artifact/output contract
   - New: thin unified UI (shape = open question), queue + storage plumbing, Entra SSO
7. Phases with exit criteria (Phase 0 pilot on real campaigns → Phase 1 rollout → Phase 2 Figma→OFT + library decision)
8. Security and data handling (client PSDs off-tenant on Render, campaign URLs/merge fields at rest, tracking-beacon note from Kai's briefing)
9. **Open questions register** with owners:
   - Front-end shape: one new UI vs extend Jon's app vs two tiles — Francis/Adrian/Jon
   - Client data constraints — may veto Render — legal/account leads
   - IT infra inventory (Azure subscription, Render account, W365 licenses) — IT
   - Volume/concurrency numbers — Shivani
   - Jon: OFT support plans, willingness to conform HTML to Word-safe rules — Jon
   - Ownership/maintenance post-distribution — Francis
   - Designer UX set (operators, SOP willingness, fail-message format, dealbreakers) — Claudia/Daphne
   - Fidelity sign-off criteria for the library spike — design leads
   - Intercept OS tile process/owner — platform owner
10. Success metrics and kill criteria

