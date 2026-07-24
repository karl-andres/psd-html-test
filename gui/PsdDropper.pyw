"""PsdDropper: a small drag-and-drop front end for the PSD -> .OFT pipeline.

Drop a .psd file onto the window (or click to browse); it runs
grammar/Invoke-PsdToOft.ps1 -Psd <file> -OutDir <psd's folder>/out, streaming the
5-stage pipeline log (emit -> gate -> validate -> convert -> link-verify) live. The GUI
never passes -Capture, so Invoke-PsdToOft.ps1's stage 6 (capture) is not invoked from here;
link-verify (stage 5) only runs when a link manifest is bound.

The link manifest field is separate from the PSD picker on purpose: manifests are commonly
named/located independently of their PSD, so auto-detect is only ever a convenience default,
never the only way in -- Browse/type any path, or Clear to run with no links bound. The enforced
naming convention is "<psd-stem>.links.json" (see the authoring SOP): when that exact file
exists, auto-detect uses it silently, no prompt. Anything else is treated as a real mismatch,
never silently guessed: exactly one differently-named "*.links.json" in the same folder gets a
3-way choice (use it / run without links / cancel -- see _ask_use_skip_or_cancel), each option
spelled out in plain language plus a pointer to the authoring SOP; more than one candidate gets
a warning + a picker dialog scoped to that folder. See resolve_manifest_interactive below --
both the main window and the link editor share it.

"Edit links..." opens a plain form (LinkEditorDialog below) -- one row per real button/image
the tool found in the PSD, a text box next to each for the URL. No JSON is ever shown to the
person filling it in: discovery (src/psd_html/link_scaffold.py) and saving both happen through
that module's functions, called directly in-process (no subprocess -- this is pure Python, no
Outlook COM involved), so the round trip is: open the dialog, type URLs, Save, drop the PSD.

Run with: pythonw gui/PsdDropper.pyw   (no console window)
Requires: pip install -e ".[gui]"      (installs tkinterdnd2)
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None
    DND_FILES = None

TOOL_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_SCRIPT = TOOL_ROOT / "grammar" / "Invoke-PsdToOft.ps1"
SOP_PATH = TOOL_ROOT / "docs" / "PSD-for-HTML_Authoring-SOP.md"

IDLE_BG = "#f4f4f4"
BUSY_BG = "#fff6cc"
PASS_BG = "#dff0d8"
FAIL_BG = "#f2dede"

_ROLE_LABELS = {"cta": "Button", "button": "Button", "image": "Image", "graphic": "Image/graphic"}


def _ask_use_skip_or_cancel(parent: tk.Misc, expected_name: str, candidate: Path) -> str:
    """Modal 3-way choice for a manifest whose name doesn't match the PSD. A stock
    messagebox.askyesno only has two buttons, which can't distinguish "run with no links bound"
    from "stop, I need to go author one first" -- both are real, different outcomes, so this is a
    small custom Toplevel instead. Returns "use" | "skip" | "cancel"; closing the window (the [X]
    button) counts as "cancel"."""
    result = {"choice": "cancel"}
    dlg = tk.Toplevel(parent)
    dlg.title("No manifest named after the PSD")
    dlg.resizable(False, False)
    dlg.transient(parent)

    tk.Label(
        dlg,
        text=(
            f"Could not find a manifest named after the PSD ({expected_name}).\n\n"
            f"Found a different one in the same folder instead: {candidate.name}\n\n"
            "Not sure which to pick? Use \"Edit links...\" to author link URLs directly, or see "
            f"the authoring SOP:\n{SOP_PATH}"
        ),
        justify="left", anchor="w", wraplength=440,
    ).pack(fill="x", padx=16, pady=(16, 10))

    button_row = tk.Frame(dlg)
    button_row.pack(fill="x", padx=16, pady=(0, 16))

    def _choose(choice: str) -> None:
        result["choice"] = choice
        dlg.destroy()

    tk.Button(button_row, text=f"Yes, use {candidate.name}", command=lambda: _choose("use")).pack(fill="x", pady=2)
    tk.Button(
        button_row, text="Run without links -- no button or image will be clickable",
        command=lambda: _choose("skip"),
    ).pack(fill="x", pady=2)
    tk.Button(button_row, text="Cancel", command=lambda: _choose("cancel")).pack(fill="x", pady=2)

    dlg.protocol("WM_DELETE_WINDOW", lambda: _choose("cancel"))
    dlg.grab_set()
    parent.wait_window(dlg)
    return result["choice"]


def resolve_manifest_interactive(parent: tk.Misc, psd_path: Path) -> tuple[Path | None, str]:
    """(manifest_path_or_None, log_line). Must be called on the Tk MAIN thread only -- it can pop
    a messagebox/filedialog/Toplevel. The enforced convention is exact-stem
    "<psd-stem>.links.json"; when that file exists it's used with no prompt, no matter what else
    sits in the folder. Anything else is a real naming mismatch and is NEVER silently guessed,
    even when there's only one candidate: that's exactly the shape of two designers/PSDs
    accidentally colliding on one folder, so it always gets an explicit human decision -- a 3-way
    use/skip/cancel choice for a single candidate (see _ask_use_skip_or_cancel), or a warning + a
    picker dialog when there's more than one. A "cancel" log_line is prefixed "CANCELLED:" so a
    caller that means "stop the whole action" (the main window's pre-run check) can tell that
    apart from an ordinary "proceed with no links bound" outcome; the link editor doesn't need
    the distinction and just treats both the same (it's the tool you'd go author links in
    anyway)."""
    from psd_html.link_scaffold import find_manifest_near_psd

    canonical, others = find_manifest_near_psd(psd_path)
    if canonical is not None:
        return canonical, f"link manifest: {canonical} (matches the PSD's name)"

    expected_name = psd_path.with_suffix("").with_suffix(".links.json").name
    if len(others) == 1:
        candidate = others[0]
        choice = _ask_use_skip_or_cancel(parent, expected_name, candidate)
        if choice == "use":
            return candidate, f"link manifest: {candidate} (confirmed -- name did not match the PSD)"
        if choice == "skip":
            return None, f"link manifest: none -- found {candidate.name} but chose to run without links"
        return None, (
            f'CANCELLED: no link manifest chosen -- use "Edit links..." or see {SOP_PATH.name} '
            "to author one first"
        )

    if len(others) > 1:
        names = "\n".join(c.name for c in others)
        messagebox.showwarning(
            "Multiple link manifests found",
            f"Found {len(others)} link manifests in this folder, none named after the PSD "
            f"({expected_name}):\n\n{names}\n\nChoose which one to use.",
        )
        chosen = filedialog.askopenfilename(
            initialdir=str(psd_path.parent), title="Choose a link manifest",
            filetypes=[("Link manifest", "*.links.json"), ("All files", "*.*")],
        )
        if chosen:
            return Path(chosen), f"link manifest: {chosen} (picked from {len(others)} candidates)"
        messagebox.showinfo(
            "No manifest chosen",
            "Running without links -- no button or image in this PSD will be clickable.\n\n"
            'Not sure which to pick, or need to author new links? Use "Edit links..." in this '
            f"window, or see the authoring SOP:\n{SOP_PATH}",
        )
        return None, f"link manifest: none -- {len(others)} candidates found, none picked"

    return None, "link manifest: none -- no link manifest next to the PSD"


class LinkEditorDialog(tk.Toplevel):
    """One text box per real button/image the pipeline found in `psd_path`, pre-filled from
    whatever manifest already exists at the save target. Saving calls
    link_scaffold.manifest_from_form and writes the file directly -- the designer/producer never
    sees or edits JSON."""

    def __init__(self, parent: tk.Misc, psd_path: Path, on_saved) -> None:
        super().__init__(parent)
        self.title(f"Edit links -- {psd_path.name}")
        self.geometry("720x580")
        self.minsize(560, 400)
        self.psd_path = psd_path
        self.out_path = psd_path.with_suffix("").with_suffix(".links.json")
        self.on_saved = on_saved
        self.slot_vars: dict[str, tk.StringVar] = {}
        self.region_vars: dict[str, tk.StringVar] = {}
        self.inline_entries: list[dict] = []

        self.status = tk.Label(self, text="Reading the PSD for buttons and links...", font=("Segoe UI", 10))
        self.status.pack(pady=40)

        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._discover, daemon=True).start()
        self.after(80, self._poll_discovery)

    # --- discovery (background thread -- runs the real emit(), can take a moment) ------------

    def _discover(self) -> None:
        # Background thread: only the slow, dialog-free PSD/emit work happens here. Resolving
        # WHICH manifest file to load/save can pop a messagebox/filedialog (see
        # resolve_manifest_interactive), and Tkinter dialogs are only safe from the main thread --
        # that step happens in _poll_discovery below, after this thread's result comes back.
        try:
            from psd_html.link_scaffold import categorize_regions, discover_link_candidates

            _tree, regions = discover_link_candidates(str(self.psd_path))
            slot_candidates, region_candidates = categorize_regions(regions)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the dialog, not swallowed
            self._queue.put(("error", str(exc)))
            return
        self._queue.put(("ok", slot_candidates, region_candidates))

    def _poll_discovery(self) -> None:
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_discovery)
            return
        if item[0] == "error":
            self.status.config(text=f"Could not read this PSD:\n{item[1]}")
            return
        _, slot_candidates, region_candidates = item

        # Main thread from here on -- safe to prompt. A manifest doesn't have to be named after
        # its PSD, but a mismatch is a real thing to confirm/choose, never guess (see
        # resolve_manifest_interactive); only when it resolves to a real path do we load AND
        # save back to THAT file instead of a fresh canonically-named one.
        found_path, found_msg = resolve_manifest_interactive(self, self.psd_path)
        if found_path is not None:
            self.out_path = found_path

        existing_slots: dict = {}
        existing_regions: dict = {}
        existing_inline: list = []
        if self.out_path.is_file():
            try:
                existing = json.loads(self.out_path.read_text(encoding="utf-8"))
                existing_slots = existing.get("slots") or {}
                existing_regions = existing.get("regions") or {}
                existing_inline = existing.get("inline") or []
            except (OSError, json.JSONDecodeError):
                pass  # an unreadable/malformed existing file just means nothing pre-fills

        self._build_form(slot_candidates, region_candidates, existing_slots, existing_regions, existing_inline, found_msg)

    # --- form -----------------------------------------------------------------------------

    def _build_form(self, slot_candidates, region_candidates, existing_slots, existing_regions, existing_inline, found_msg) -> None:
        self.status.destroy()

        tk.Label(
            self, text=f"{found_msg}\nSaving to: {self.out_path}",
            font=("Segoe UI", 8), fg="#555555", anchor="w", wraplength=680, justify="left",
        ).pack(fill="x", padx=10, pady=(6, 0))

        container = tk.Frame(self)
        container.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        if slot_candidates:
            tk.Label(inner, text="Buttons / labeled links", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
            for c in slot_candidates:
                slot = c["link_slot"]
                label = f"{slot}  ({_ROLE_LABELS.get(c.get('role'), c.get('role'))})"
                self._add_row(inner, label, self.slot_vars, slot, existing_slots.get(slot, ""))
        else:
            tk.Label(inner, text="No named buttons found -- see the authoring SOP, step 5.", font=("Segoe UI", 9)).pack(anchor="w")

        if region_candidates:
            tk.Label(inner, text="Images / icons (optional links)", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 4))
            for c in region_candidates:
                rid = c["region_id"]
                label = c.get("alt") or f"{_ROLE_LABELS.get(c.get('role'), c.get('role'))} {rid}"
                self._add_row(inner, label, self.region_vars, rid, existing_regions.get(rid, ""))

        tk.Label(inner, text="Inline citation links (optional)", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 4))
        tk.Label(
            inner,
            text="Pick the exact phrase this link wraps around (or type your own), then its URL:",
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        pick_row = tk.Frame(inner)
        pick_row.pack(fill="x", pady=(2, 2))
        self.inline_match_var = tk.StringVar()
        ttk.Combobox(pick_row, textvariable=self.inline_match_var, width=40).pack(side="left", fill="x", expand=True)
        self.inline_url_var = tk.StringVar()
        tk.Entry(pick_row, textvariable=self.inline_url_var, width=28).pack(side="left", padx=(6, 6))
        tk.Button(pick_row, text="Add", command=self._add_inline).pack(side="left")

        self.inline_listbox = tk.Listbox(inner, height=4)
        self.inline_listbox.pack(fill="x", pady=(4, 2))
        tk.Button(inner, text="Remove selected", command=self._remove_inline).pack(anchor="e")

        for item in existing_inline:
            match, url = item.get("match", ""), item.get("url", "")
            if match and url:
                self.inline_entries.append({"match": match, "url": url})
                self.inline_listbox.insert("end", f"{match}  ->  {url}")

        save_bar = tk.Frame(self)
        save_bar.pack(fill="x", padx=10, pady=10)
        tk.Button(save_bar, text="Save", command=self._save).pack(side="right")
        tk.Button(save_bar, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))

    def _add_row(self, parent, label: str, var_dict: dict, key: str, initial: str) -> None:
        row = tk.Frame(parent)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, width=40, anchor="w", wraplength=320, justify="left", font=("Segoe UI", 9)).pack(side="left")
        var = tk.StringVar(value=initial)
        tk.Entry(row, textvariable=var, font=("Segoe UI", 9)).pack(side="left", fill="x", expand=True, padx=(6, 0))
        var_dict[key] = var

    def _add_inline(self) -> None:
        match = self.inline_match_var.get().strip()
        url = self.inline_url_var.get().strip()
        if not match or not url:
            return
        self.inline_entries.append({"match": match, "url": url})
        self.inline_listbox.insert("end", f"{match}  ->  {url}")
        self.inline_match_var.set("")
        self.inline_url_var.set("")

    def _remove_inline(self) -> None:
        sel = self.inline_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.inline_listbox.delete(idx)
        del self.inline_entries[idx]

    def _save(self) -> None:
        from psd_html.link_scaffold import manifest_from_form

        slot_values = {k: v.get() for k, v in self.slot_vars.items()}
        region_values = {k: v.get() for k, v in self.region_vars.items()}
        manifest = manifest_from_form(slot_values, region_values, self.inline_entries)
        try:
            self.out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Save failed", f"Could not write {self.out_path}:\n{exc}")
            return
        self.on_saved(self.out_path)
        self.destroy()


class PsdDropperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PSD -> .OFT")
        self.root.geometry("640x460")
        self.root.minsize(480, 340)

        self._queue: queue.Queue = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._busy = False
        self._psd_path: Path | None = None
        self.manifest_var = tk.StringVar()

        self.drop_zone = tk.Label(
            root,
            text="Drop a .psd file here\n(or click to browse)",
            font=("Segoe UI", 13),
            bg=IDLE_BG,
            fg="#333333",
            relief="ridge",
            borderwidth=2,
            height=4,
            cursor="hand2",
        )
        self.drop_zone.pack(fill="x", padx=10, pady=(10, 6))
        self.drop_zone.bind("<Button-1>", lambda _e: self._browse())

        manifest_row = tk.Frame(root)
        manifest_row.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(manifest_row, text="Link manifest (optional):", font=("Segoe UI", 9)).pack(side="left")
        self.manifest_entry = tk.Entry(manifest_row, textvariable=self.manifest_var, font=("Segoe UI", 9))
        self.manifest_entry.pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(manifest_row, text="Browse...", command=self._browse_manifest).pack(side="left")
        tk.Button(manifest_row, text="Clear", command=lambda: self.manifest_var.set("")).pack(side="left", padx=(4, 0))
        tk.Button(manifest_row, text="Edit links...", command=self._open_link_editor).pack(side="left", padx=(4, 0))

        self.status_label = tk.Label(root, text="Idle.", anchor="w", font=("Segoe UI", 9))
        self.status_label.pack(fill="x", padx=10)

        self.log = ScrolledText(root, height=16, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        if TkinterDnD is not None:
            self.drop_zone.drop_target_register(DND_FILES)
            self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)
        else:
            self._log_line(
                "tkinterdnd2 is not installed -- drag-and-drop is disabled; use "
                "'pip install -e \".[gui]\"' from Tools/PSD-HTML, or click the box to browse."
            )

        self.root.after(80, self._poll_queue)

    # --- input handling --------------------------------------------------------------------

    def _on_drop(self, event) -> None:
        paths = self.root.tk.splitlist(event.data)
        psd_path = next((p for p in paths if p.lower().endswith(".psd")), None)
        if psd_path is None:
            messagebox.showerror("Not a PSD", "Drop a single .psd file.")
            return
        self._psd_path = Path(psd_path)
        self._start_run(self._psd_path)

    def _browse(self) -> None:
        if self._busy:
            return
        chosen = filedialog.askopenfilename(
            title="Choose a PSD", filetypes=[("Photoshop files", "*.psd"), ("All files", "*.*")]
        )
        if chosen:
            self._psd_path = Path(chosen)
            self._start_run(self._psd_path)

    def _browse_manifest(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Choose a link manifest", filetypes=[("Link manifest", "*.json"), ("All files", "*.*")]
        )
        if chosen:
            self.manifest_var.set(chosen)

    def _open_link_editor(self) -> None:
        psd_path = self._psd_path
        if psd_path is None:
            chosen = filedialog.askopenfilename(
                title="Choose a PSD to edit links for",
                filetypes=[("Photoshop files", "*.psd"), ("All files", "*.*")],
            )
            if not chosen:
                return
            psd_path = Path(chosen)
        if not psd_path.is_file():
            messagebox.showerror("Not found", f"File does not exist:\n{psd_path}")
            return

        def _on_saved(out_path: Path) -> None:
            self.manifest_var.set(str(out_path))
            self.status_label.config(text=f"Saved links: {out_path}", bg=PASS_BG)

        LinkEditorDialog(self.root, psd_path, _on_saved)

    # --- run management ---------------------------------------------------------------------

    def _resolve_manifest(self, psd_path: Path) -> tuple[Path | None, str]:
        """(manifest_path_or_None, log_line). An explicit path in the field always wins -- it
        is validated (fail loud, never silently dropped) rather than falling through to
        auto-detect, which would silently reintroduce the "no links bound" gap this exists to
        fix. Only when the field is EMPTY does this fall back to resolve_manifest_interactive
        (exact-name match used silently; any mismatch confirmed/picked via dialog, never
        guessed), so a user who has never touched the field still gets the exact-match case for
        free and an explicit choice for everything else."""
        typed = self.manifest_var.get().strip()
        if typed:
            typed_path = Path(typed)
            if not typed_path.is_file():
                return None, f"ERROR: link manifest not found: {typed_path}"
            return typed_path, f"link manifest: {typed_path} (from the field)"

        found, msg = resolve_manifest_interactive(self.root, psd_path)
        if found is not None:
            self.manifest_var.set(str(found))
        elif not msg.startswith("CANCELLED"):
            msg += " -- CTA/link regions will NOT be clickable in this run"
        return found, msg

    def _start_run(self, psd_path: Path) -> None:
        if self._busy:
            messagebox.showinfo("Busy", "A run is already in progress.")
            return
        if not psd_path.is_file():
            messagebox.showerror("Not found", f"File does not exist:\n{psd_path}")
            return

        manifest, manifest_log = self._resolve_manifest(psd_path)
        if manifest is None and manifest_log.startswith("ERROR"):
            messagebox.showerror("Link manifest", manifest_log)
            return
        if manifest is None and manifest_log.startswith("CANCELLED"):
            # The user picked "Cancel" on the naming-mismatch prompt -- that means "stop, I need
            # to go author a manifest first" (see _ask_use_skip_or_cancel), not "run anyway with
            # no links bound", so the run itself must not start.
            self.status_label.config(text="Cancelled -- no link manifest chosen.", bg=IDLE_BG)
            self._log_line(manifest_log)
            return

        out_dir = psd_path.parent / "out"
        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PIPELINE_SCRIPT),
            "-Psd", str(psd_path), "-OutDir", str(out_dir),
        ]
        if manifest is not None:
            cmd += ["-LinkManifest", str(manifest)]

        self._set_busy(True, f"Running: {psd_path.name} -> {out_dir}")
        self._clear_log()
        self._log_line(manifest_log)
        # SAFETY: display-only log line for the user, not something executed. The actual
        # subprocess.Popen call below receives `cmd` as a plain argv list with the default
        # shell setting (disabled), so arguments are passed directly to the child process with
        # no shell/string parsing involved -- a PSD path containing spaces or special
        # characters cannot inject anything.
        self._log_line("$ " + " ".join(cmd))

        thread = threading.Thread(target=self._run_pipeline, args=(cmd, out_dir), daemon=True)
        thread.start()

    def _run_pipeline(self, cmd: list, out_dir: Path) -> None:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(TOOL_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self._queue.put(("done", 1, out_dir, f"Could not launch PowerShell: {exc!r}"))
            return
        self._proc = proc
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            self._queue.put(("line", raw_line.decode("utf-8", errors="replace").rstrip("\n")))
        proc.wait()
        self._queue.put(("done", proc.returncode, out_dir, None))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == "line":
                    self._log_line(item[1])
                else:
                    _, code, out_dir, err = item
                    self._on_finished(code, out_dir, err)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _on_finished(self, code: int, out_dir: Path, err: str | None) -> None:
        self._proc = None
        self._set_busy(False)
        if err:
            self._log_line(err)
            self.status_label.config(text=f"FAILED: {err}", bg=FAIL_BG)
            messagebox.showerror("Failed", err)
            return
        if code == 0:
            self.status_label.config(text=f"PASS -- wrote {out_dir}", bg=PASS_BG)
            self._log_line(f"\n=== DONE (exit 0) -- {out_dir} ===")
        else:
            self.status_label.config(text=f"FAILED (exit {code}) -- see log", bg=FAIL_BG)
            self._log_line(f"\n=== FAILED (exit {code}) ===")

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        if busy:
            self.drop_zone.config(bg=BUSY_BG, text="Running...")
            self.status_label.config(text=message or "Running...", bg=BUSY_BG)
        else:
            self.drop_zone.config(bg=IDLE_BG, text="Drop a .psd file here\n(or click to browse)")

    # --- log widget -------------------------------------------------------------------------

    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _log_line(self, line: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main() -> int:
    if not PIPELINE_SCRIPT.is_file():
        # Fail loud before even opening a window -- a silently-missing pipeline script would
        # otherwise only surface as a cryptic PowerShell error after the first drop.
        import tkinter.messagebox as mb

        root = tk.Tk()
        root.withdraw()
        mb.showerror("PsdDropper", f"Pipeline script not found:\n{PIPELINE_SCRIPT}")
        return 1

    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    PsdDropperApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
