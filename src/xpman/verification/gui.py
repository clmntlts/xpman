"""``xpman-verify`` -- a tiny desktop front-end for the integration report.

Pick a BioSemi ``.bdf`` and its xpman ``events.csv``, click *Generate report*, and the
self-contained interactive HTML opens in your browser. Built on the standard-library ``tkinter``
(no PySide6) so the packaged ``xpman-verify.exe`` stays small and fast to build. The heavy lifting
lives in :func:`xpman.verification.integration_report.generate_report`.
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

from xpman.verification.integration_report import default_plotly_path, generate_report

_BG = "#0f1115"
_PANEL = "#171a21"
_TX = "#e6e8ee"
_MUT = "#9aa3b2"
_OK = "#22c55e"
_BAD = "#f43f5e"


def _default_out(bdf: str) -> str:
    p = Path(bdf)
    return str(p.with_name(f"{p.stem}_integration_report.html")) if bdf else ""


def main() -> None:
    # Dual-mode: with command-line args, run headless as the CLI (so the exe is scriptable and
    # CI-testable); with no args, open the file-picker window. See integration_report.main.
    if len(sys.argv) > 1:
        from xpman.verification.integration_report import main as cli_main

        cli_main(sys.argv[1:])
        return

    root = tk.Tk()
    root.title("xpman ↔ BioSemi integration verifier")
    root.configure(bg=_BG)
    root.geometry("720x460")
    root.minsize(600, 400)

    bdf_var = tk.StringVar()
    ev_var = tk.StringVar()
    out_var = tk.StringVar()

    def _lbl(parent, text, **kw):
        return tk.Label(parent, text=text, bg=_BG, fg=_TX, anchor="w", **kw)

    tk.Label(root, text="xpman ↔ BioSemi integration verifier", bg=_BG, fg=_TX,
             font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
    _lbl(root, "Pick the BioSemi .bdf recording and its matching xpman events.csv, then generate "
               "the report.", fg=_MUT, wraplength=680, justify="left").pack(anchor="w", padx=16)

    form = tk.Frame(root, bg=_BG)
    form.pack(fill="x", padx=16, pady=12)
    form.columnconfigure(1, weight=1)

    def _row(r, label, var, browse):
        _lbl(form, label).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 8))
        e = tk.Entry(form, textvariable=var, bg=_PANEL, fg=_TX, insertbackground=_TX,
                     relief="flat", highlightthickness=1, highlightbackground="#262b36")
        e.grid(row=r, column=1, sticky="ew", ipady=4)
        tk.Button(form, text="Browse…", command=browse, bg=_PANEL, fg=_TX, relief="flat",
                  activebackground="#262b36", activeforeground=_TX).grid(row=r, column=2, padx=(8, 0))

    def browse_bdf():
        path = filedialog.askopenfilename(
            title="Select the BioSemi .bdf recording",
            filetypes=[("BioSemi BDF", "*.bdf"), ("All files", "*.*")])
        if path:
            bdf_var.set(path)
            if not out_var.get():
                out_var.set(_default_out(path))

    def browse_ev():
        path = filedialog.askopenfilename(
            title="Select the xpman events.csv",
            filetypes=[("xpman events", "events*.csv"), ("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            ev_var.set(path)

    def browse_out():
        path = filedialog.asksaveasfilename(
            title="Save the HTML report as…", defaultextension=".html",
            filetypes=[("HTML report", "*.html")],
            initialfile=Path(out_var.get()).name if out_var.get() else "integration_report.html")
        if path:
            out_var.set(path)

    _row(0, ".bdf recording", bdf_var, browse_bdf)
    _row(1, "events.csv", ev_var, browse_ev)
    _row(2, "Save report to", out_var, browse_out)

    result = tk.Label(root, text="", bg=_BG, fg=_MUT, anchor="w", justify="left",
                      wraplength=680, font=("Segoe UI", 10))
    result.pack(anchor="w", padx=16, pady=(2, 0))

    log = tk.Text(root, height=8, bg=_PANEL, fg=_MUT, relief="flat", wrap="word",
                  highlightthickness=1, highlightbackground="#262b36", font=("Consolas", 9))
    log.pack(fill="both", expand=True, padx=16, pady=10)

    btns = tk.Frame(root, bg=_BG)
    btns.pack(fill="x", padx=16, pady=(0, 14))
    gen_btn = tk.Button(btns, text="Generate report", bg="#3b82f6", fg="white", relief="flat",
                        activebackground="#2563eb", activeforeground="white", font=("Segoe UI", 10, "bold"),
                        padx=14, pady=6)
    gen_btn.pack(side="left")
    open_btn = tk.Button(btns, text="Open report", bg=_PANEL, fg=_TX, relief="flat",
                         activebackground="#262b36", activeforeground=_TX, padx=12, pady=6, state="disabled")
    open_btn.pack(side="left", padx=8)

    offline = default_plotly_path() is not None
    _lbl(btns, f"Plotly: {'bundled (offline)' if offline else 'CDN (needs internet)'}",
         fg=_MUT).pack(side="right")

    def _logline(msg):
        log.insert("end", msg + "\n")
        log.see("end")

    def _worker(bdf, ev, out):
        try:
            s = generate_report(bdf, ev, out)
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the user, don't crash the UI
            root.after(0, _done, None, str(exc))
            return
        root.after(0, _done, s, None)

    def _done(summary, error):
        gen_btn.config(state="normal", text="Generate report")
        if error:
            result.config(text="✗ " + error, fg=_BAD)
            _logline("ERROR: " + error)
            messagebox.showerror("Could not generate report", error)
            return
        bad = [c for c in summary["codebook"] if c["ok"] is False]
        al = summary.get("align")
        align_ok = al is None or (
            al.get("count_match", True)
            and abs(al.get("slope", 1.0) - 1.0) < 1e-3
            and al.get("resid_sd_ms", 0.0) < 2.0
        )
        ok = summary["reconciled"] and summary["n_dropped"] == 0 and not bad and align_ok
        result.config(
            text=("✓ Report generated — " if ok else "⚠ Report generated (check flagged rows) — ")
                 + f"{summary['n_distinct_codes']} codes, {summary['n_bdf_triggers']} triggers, "
                 f"{summary['n_dropped']} dropped frames"
                 + (f", {len(bad)} code mismatch(es)" if bad else ""),
            fg=_OK if ok else "#f59e0b")
        _logline(f"Wrote {summary['out_path']} ({summary['size_mb']:.1f} MB, "
                 f"{'offline' if summary['offline'] else 'CDN Plotly'})")
        _logline(f"BDF: {summary['n_channels']} ch @ {summary['fs']:.0f} Hz, "
                 f"{summary['duration_s']:.0f}s · reconciled {summary['n_xpman_coded']} xpman ↔ BDF")
        if summary["align"]:
            _logline(f"Clock: slope {summary['align']['slope']:.6f}, "
                     f"residual {summary['align']['resid_sd_ms']:.2f} ms")
        open_btn.config(state="normal", command=lambda: webbrowser.open(Path(summary["out_path"]).as_uri()))
        webbrowser.open(Path(summary["out_path"]).as_uri())

    def on_generate():
        bdf, ev, out = bdf_var.get().strip(), ev_var.get().strip(), out_var.get().strip()
        if not bdf or not ev:
            messagebox.showwarning("Missing files", "Please pick both a .bdf and an events.csv.")
            return
        if not out:
            out = _default_out(bdf)
            out_var.set(out)
        gen_btn.config(state="disabled", text="Generating…")
        result.config(text="Working…", fg=_MUT)
        _logline(f"Generating {Path(out).name} …")
        threading.Thread(target=_worker, args=(bdf, ev, out), daemon=True).start()

    gen_btn.config(command=on_generate)
    root.mainloop()


if __name__ == "__main__":
    main()
