r"""Cross-check an xpman ``events.csv`` against the BioSemi ``.bdf`` it was recorded into, and
emit ONE self-contained interactive HTML report for fast "do both systems agree?" verification.

This closes the loop that the manual-hardware scripts leave open: ``analyze_verification_run.py``
reports the xpman *software* side (per-frame flip jitter, dropped frames, requested-vs-achieved
frequency); the ``.bdf`` is the *hardware* ground truth (what the amplifier recorded on its Photo
light-sensor and Status trigger channels). This module reads BOTH, aligns their independent clocks
via the shared trigger sequence, and produces:

  * the photodiode trace with every trigger overlaid, zoomable/pannable, each trigger CODE a
    separately-toggleable series (base/oddball/distractor/go/nogo/baseline/familiarization);
  * the numbers: dropped frames + inter-flip jitter (xpman), a per-code trigger codebook reconciled
    between the two systems, hardware command->photon latency (Status vs Photo), pulse width, and
    the cross-system clock fit (slope ~1.0 + residual jitter = the real integration check);
  * green/amber pass-fail chips so a lab user sees at a glance whether the integration is sound.

Public API: :func:`generate_report`. Also runnable as a CLI (``python -m
xpman.verification.integration_report --bdf ... --events-csv ... --out report.html``) and, with a
file-picker GUI, as the packaged ``xpman-verify`` app (see :mod:`xpman.verification.gui`).

See ``docs/integration_verifier.md``, ``docs/verification_protocol.md`` and
``docs/lab_test_tutorial.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from xpman.core.verification_report import build_verification_report

#: Loaded from a CDN only when no local Plotly is bundled/passed. Kept on the CSP-friendly jsDelivr
#: npm path; the packaged app bundles Plotly instead (see :func:`default_plotly_path`) so reports
#: are fully offline.
_CDN_PLOTLY = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.27.0/plotly.min.js"


def default_plotly_path() -> "Path | None":
    """Locate a bundled ``plotly.min.js`` so reports are self-contained offline. Checks the
    PyInstaller unpack dir first (``sys._MEIPASS``, set in the packaged ``xpman-verify`` exe), then
    this package's ``assets/`` folder. Returns ``None`` when none is present (the report then falls
    back to the CDN, which needs internet when opened)."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates += [Path(meipass) / "plotly.min.js",
                       Path(meipass) / "xpman" / "verification" / "assets" / "plotly.min.js"]
    candidates.append(Path(__file__).parent / "assets" / "plotly.min.js")
    return next((c for c in candidates if c.is_file()), None)


# --------------------------------------------------------------------------- BDF parsing
def read_bdf(path: Path) -> dict:
    """Parse a BioSemi BDF header and decode ONLY the Photo + Status channels (the two we need),
    24-bit little-endian signed. Returns fs, duration, and the two decoded int arrays.

    Raises ``ValueError`` (not ``SystemExit``) on a bad/empty file so GUI callers can show it."""
    raw = path.read_bytes()
    hdr = raw[:256]

    def s(a: int, b: int) -> str:
        return hdr[a:b].decode("latin-1").strip()

    try:
        n_bytes_header = int(s(184, 192))
        n_rec_field = int(s(236, 244))
        rec_dur = float(s(244, 252))
        ns = int(s(252, 256))
    except ValueError as exc:
        raise ValueError(f"{path.name} is not a readable BDF (bad header fields): {exc}") from exc

    off = 256
    labels = [raw[off + i * 16 : off + (i + 1) * 16].decode("latin-1").strip() for i in range(ns)]
    off += 16 * ns + 80 * ns + 8 * ns  # labels, transducer, physical dim
    off += 8 * ns * 4  # phys min/max, dig min/max
    off += 80 * ns  # prefiltering
    nsamp = [int(raw[off + i * 8 : off + (i + 1) * 8].decode("latin-1")) for i in range(ns)]

    rec_bytes = sum(nsamp) * 3
    data = raw[n_bytes_header:]
    n_rec = n_rec_field if n_rec_field > 0 else len(data) // rec_bytes
    if n_rec <= 0:
        raise ValueError(
            f"{path.name} has no data records (header-only, {len(raw)} bytes). In ActiView: "
            "Start File, record through the whole run with live data streaming, then Stop File."
        )

    def decode(label: str) -> np.ndarray:
        idx = labels.index(label)
        n = nsamp[idx]
        start_in_rec = sum(nsamp[:idx]) * 3
        out = np.empty(n_rec * n, dtype=np.int32)
        pos = 0
        for r in range(n_rec):
            base = r * rec_bytes + start_in_rec
            b = np.frombuffer(data[base : base + n * 3], dtype=np.uint8).reshape(n, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - (1 << 24), v)
            out[pos : pos + n] = v
            pos += n
        return out

    if "Status" not in labels:
        raise ValueError(f"{path.name} has no 'Status' channel (labels: {labels[-4:]}).")
    photo_label = next((lab for lab in ("Photo", "Erg1", "EXG1") if lab in labels), None)
    fs = nsamp[labels.index("Status")] / rec_dur
    return {
        "fs": fs,
        "n_channels": ns,
        "duration_s": n_rec * rec_dur,
        "status": decode("Status"),
        "photo": decode(photo_label) if photo_label else None,
        "photo_label": photo_label,
    }


# --------------------------------------------------------------------------- events.csv
def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "event_type" not in reader.fieldnames:
            raise ValueError(f"{path.name} is not an xpman events.csv (missing an event_type column).")
        for row in reader:
            events.append(
                {
                    "timestamp": float(row["timestamp"]),
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
                }
            )
    return events


def resolve_refresh_hz(events: list[dict], override: "float | None") -> float:
    if override is not None:
        return override
    for e in events:
        if e["event_type"] == "refresh_rate_measured":
            v = (e.get("payload") or {}).get("refresh_rate_hz")
            if v:
                return float(v)
    return 60.0


# --------------------------------------------------------------------------- signal analysis
def status_events(status: np.ndarray, fs: float) -> dict:
    """Rising edges of the low-8-bit trigger code, the code at each, pulse widths and ITIs."""
    codes = status & 0xFF
    nz = codes != 0
    rising = np.where((~nz[:-1]) & (nz[1:]))[0] + 1
    falling = np.where((nz[:-1]) & (~nz[1:]))[0] + 1
    edge_codes = codes[rising] if len(rising) else np.array([], dtype=int)

    widths = []
    fi = 0
    for r in rising:
        while fi < len(falling) and falling[fi] <= r:
            fi += 1
        if fi < len(falling):
            widths.append((falling[fi] - r) / fs * 1000)
    return {
        "times_s": (rising / fs),
        "codes": edge_codes,
        "pulse_ms": np.array(widths),
        "iti_ms": (np.diff(rising) / fs * 1000) if len(rising) > 1 else np.array([]),
    }


def photo_edges(photo: np.ndarray, fs: float) -> "tuple[np.ndarray, float, float]":
    """Onset edges of the light-sensor square wave (both directions across an adaptive midpoint)."""
    ph = photo.astype(np.float64)
    lo, hi = np.percentile(ph, 5), np.percentile(ph, 95)
    mid = (lo + hi) / 2
    above = ph > mid
    edges = np.where(above[:-1] != above[1:])[0] + 1
    return (edges / fs), lo, hi


def align_clocks(xpman_t: np.ndarray, bdf_t: np.ndarray) -> dict:
    """Fit bdf_t ~= a*xpman_t + b over the shared trigger sequence (paired in order over the
    overlapping count), with one round of outlier rejection. Slope a ~ 1 and a tiny residual mean
    the PC and BioSemi clocks agree -- the core integration check."""
    n = min(len(xpman_t), len(bdf_t))
    if n < 2:
        return {"ok": False, "n_paired": n}
    x = xpman_t[:n]
    y = bdf_t[:n]
    a, b = np.polyfit(x, y, 1)
    resid = y - (a * x + b)
    mad = np.median(np.abs(resid - np.median(resid))) or 1e-9
    keep = np.abs(resid - np.median(resid)) < 6 * mad
    if keep.sum() >= 2:
        a, b = np.polyfit(x[keep], y[keep], 1)
        resid = y - (a * x + b)
    return {
        "ok": True,
        "n_paired": int(n),
        "slope": float(a),
        "offset_s": float(b),
        "resid_sd_ms": float(np.std(resid[keep] * 1000)),
        "resid_max_ms": float(np.max(np.abs(resid * 1000))),
        "count_match": len(xpman_t) == len(bdf_t),
        "n_xpman": int(len(xpman_t)),
        "n_bdf": int(len(bdf_t)),
    }


def robust_latency(status_t: np.ndarray, photo_t: np.ndarray, window_ms: float = 60.0) -> dict:
    """Status-rising minus nearest Photo edge, ms (+ = trigger after light). Rejects onsets whose
    photo edge was missed (they mispair to a far edge) by keeping |lat - median| < window."""
    if len(status_t) == 0 or len(photo_t) == 0:
        return {"ok": False}
    lat = np.array([(t - photo_t[np.argmin(np.abs(photo_t - t))]) * 1000 for t in status_t])
    med = np.median(lat)
    clean = lat[np.abs(lat - med) < window_ms]
    return {
        "ok": True,
        "median_ms": float(med),
        "clean_mean_ms": float(np.mean(clean)) if len(clean) else float("nan"),
        "clean_sd_ms": float(np.std(clean)) if len(clean) else float("nan"),
        "n_clean": int(len(clean)),
        "n_total": int(len(lat)),
    }


# --------------------------------------------------------------------------- xpman trigger map
#: Colour per semantic trigger kind (shared by the BDF code series and the xpman overlay lanes).
LABEL_COLORS = {
    "base": "#3b82f6", "oddball": "#f43f5e", "distractor": "#f59e0b",
    "go": "#22c55e", "nogo": "#a855f7", "baseline start": "#14b8a6", "baseline stop": "#0ea5a3",
    "familiarization start": "#eab308", "familiarization stop": "#ca8a04", "trigger": "#60a5fa",
    "unlabelled (segment/reserved?)": "#94a3b8",
}
#: Segment-boundary TTL pulses: (semantic label, payload field holding the code). xpman records the
#: code in these markers, so they reconcile per-code like any onset trigger. Older event logs
#: (pre-fix) have no code field -- those fall back to a timestamp-only overlay lane (see collect).
MARKER_EVENTS = {
    "baseline_start": ("baseline start", "start_trigger_code"),
    "baseline_end": ("baseline stop", "stop_trigger_code"),
    "familiarization_start": ("familiarization start", "start_trigger_code"),
    "familiarization_end": ("familiarization stop", "stop_trigger_code"),
}


def collect_xpman_triggers(events: list[dict]) -> "tuple[list[tuple[float, int, str]], dict[str, list[float]]]":
    """Pull EVERY trigger-bearing xpman event, not just base/oddball.

    Returns:
        coded: (timestamp, code, label) for events that carry an explicit port code --
            ``trigger_sent`` (base/oddball, incl. resolved dual-stream/reserved codes),
            ``distractor_onset``/``go_nogo_onset`` (their ``trigger_code``), and the baseline /
            familiarization start-stop markers (their ``start``/``stop_trigger_code``).
        markers: {label: [timestamps]} only for OLDER event logs whose baseline/familiarization
            markers predate the code being recorded -- overlaid by time so they still show.
    """
    coded: list[tuple[float, int, str]] = []
    markers: dict[str, list[float]] = {}
    for e in events:
        et = e["event_type"]
        p = e.get("payload") or {}
        ts = e["timestamp"]
        if et == "trigger_sent" and p.get("code") is not None:
            lab = "oddball" if p.get("is_oddball") else ("base" if p.get("is_oddball") is False else "trigger")
            coded.append((ts, int(p["code"]), lab))
        elif et == "distractor_onset" and p.get("trigger_code") is not None:
            coded.append((ts, int(p["trigger_code"]), "distractor"))
        elif et == "go_nogo_onset" and p.get("trigger_code") is not None:
            coded.append((ts, int(p["trigger_code"]), "go" if p.get("kind") == "go" else "nogo"))
        elif et in MARKER_EVENTS:
            label, code_field = MARKER_EVENTS[et]
            code = p.get(code_field)
            if code is not None:
                coded.append((ts, int(code), label))  # xpman records the code -> fully reconciled
            else:
                markers.setdefault(label, []).append(ts)  # older log / no trigger configured
    return coded, markers


# --------------------------------------------------------------------------- HTML report
def _chip(ok: "bool | None") -> str:
    return {True: "ok", False: "bad", None: "na"}[ok]


def build_report_html(payload: dict, cards: list[dict], codebook: list[dict], plotly_js: "str | None") -> str:
    plotly_tag = (
        f"<script>{plotly_js}</script>" if plotly_js else f'<script src="{_CDN_PLOTLY}"></script>'
    )
    cards_html = "\n".join(
        f'<div class="card {_chip(c["ok"])}"><div class="cv">{c["value"]}</div>'
        f'<div class="cl">{c["label"]}</div><div class="cs">{c.get("sub","")}</div></div>'
        for c in cards
    )
    rows = "\n".join(
        f'<tr class="{_chip(r["ok"])}"><td>{r["code"]}</td><td>{r["meaning"]}</td>'
        f'<td>{r["n_bdf"]}</td><td>{r["n_xpman"]}</td><td>{r["note"]}</td></tr>'
        for r in codebook
    )
    codebook_html = (
        '<h2>Trigger codebook — every code seen, reconciled BDF ↔ xpman</h2>'
        '<table class="codebook"><thead><tr><th>Code</th><th>Meaning</th>'
        '<th>BDF (Status)</th><th>xpman (events)</th><th>Note</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )
    data_json = json.dumps(payload)
    return (
        _HEAD
        + plotly_tag
        + _STYLE
        + "</head><body>"
        + f'<h1>xpman ↔ BioSemi integration report</h1><div class="sub">{payload["title"]}</div>'
        + f'<div class="cards">{cards_html}</div>'
        + '<div class="legendhint">Click a series in the legend to toggle it. Drag to zoom, '
        + "double-click to reset. Box/lasso in the modebar.</div>"
        + '<div id="plot"></div>'
        + codebook_html
        + f"<script>const DATA = {data_json};</script>"
        + _APP_JS
        + "</body></html>"
    )


_HEAD = "<!doctype html><html lang='en'><head><meta charset='utf-8'>" \
        "<meta name='viewport' content='width=device-width,initial-scale=1'>" \
        "<title>xpman ↔ BioSemi integration</title>"

_STYLE = """<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--tx:#e6e8ee;--mut:#9aa3b2;
--ok:#22c55e;--okbg:#0f2a1a;--bad:#f43f5e;--badbg:#2a0f16;--na:#64748b;--nabg:#1b2027;--ac:#c026d3;}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--tx);
font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--mut);margin-bottom:18px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--na);
border-radius:10px;padding:12px 14px}
.card.ok{border-left-color:var(--ok)}.card.bad{border-left-color:var(--bad)}
.cv{font-size:19px;font-weight:650}.cl{color:var(--mut);font-size:12px;margin-top:2px}
.cs{color:var(--na);font-size:11px;margin-top:3px}
.legendhint{color:var(--mut);font-size:12px;margin:4px 0 8px}
#plot{width:100%;height:560px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
h2{font-size:15px;margin:22px 0 8px}
table.codebook{border-collapse:collapse;width:100%;font-size:13px;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
table.codebook th,table.codebook td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line)}
table.codebook th{color:var(--mut);font-weight:600;font-size:12px}
table.codebook tr.ok td:first-child{box-shadow:inset 3px 0 var(--ok)}
table.codebook tr.bad td:first-child{box-shadow:inset 3px 0 var(--bad)}
table.codebook tr.bad td{color:#fecdd3}
</style>"""

_APP_JS = """<script>
const D = DATA;
const palette=['#3b82f6','#f43f5e','#f59e0b','#22c55e','#a855f7','#14b8a6','#f97316','#ec4899'];
const t0 = D.photo_t0, dt = 1.0/D.fs;
const px = D.photo.map((_,i)=>t0 + i*dt);
const traces = [];
if (D.photo.length) traces.push({x:px, y:D.photo, type:'scattergl', mode:'lines',
  name:'Photo (light)', line:{color:'#c026d3',width:1}, hoverinfo:'x+y'});
function vlines(times, name, color, y0, y1, dash){
  const xs=[], ys=[];
  for(const t of times){ xs.push(t,t,null); ys.push(y0,y1,null); }
  return {x:xs,y:ys,type:'scattergl',mode:'lines',name,line:{color,width:1.2,dash:dash||'solid'},
          hoverinfo:'name',connectgaps:false};
}
let ci=0;
for(const c of D.bdf_codes){
  const col = (D.label_colors[c.label]) || palette[ci%palette.length];
  traces.push(vlines(c.times, 'BDF '+c.code+(c.label?' — '+c.label:'')+' ×'+c.times.length, col, 0, 1.06));
  ci++;
}
for(const g of D.xpman_lanes){
  traces.push({...vlines(g.times, 'xpman '+g.name+' ×'+g.times.length, g.color||'#94a3b8', -0.06, 1.0, 'dot'),
    visible:'legendonly'});
}
if (D.photo_edges && D.photo_edges.length){
  traces.push({...vlines(D.photo_edges, 'Photo onset edges ×'+D.photo_edges.length, '#64748b', 0, 1.06),
    visible:'legendonly', line:{color:'#64748b',width:0.7}});
}
const layout={paper_bgcolor:'#171a21',plot_bgcolor:'#171a21',font:{color:'#e6e8ee'},
  margin:{l:50,r:16,t:10,b:40},
  xaxis:{title:'BioSemi time (s)',gridcolor:'#262b36',rangeslider:{thickness:0.06},zeroline:false},
  yaxis:{title:'normalised',gridcolor:'#262b36',range:[-0.12,1.14],zeroline:false},
  legend:{bgcolor:'rgba(0,0,0,0)',font:{size:11}},hovermode:'closest',showlegend:true};
Plotly.newPlot('plot',traces,layout,{responsive:true,displaylogo:false,
  modeBarButtonsToRemove:['select2d','lasso2d']});
if(D.default_view){ Plotly.relayout('plot',{'xaxis.range':D.default_view}); }
</script>"""


def generate_report(
    bdf_path: "str | Path",
    events_csv_path: "str | Path",
    out_path: "str | Path",
    *,
    plotly_js_path: "str | Path | None" = None,
    refresh_rate_hz: "float | None" = None,
    max_photo_points: int = 200000,
) -> dict:
    """Build the interactive integration report and write it to ``out_path``.

    ``plotly_js_path`` inlines a local ``plotly.min.js`` for a fully offline report; when ``None``
    a bundled copy is used if present (:func:`default_plotly_path`), else the CDN. Returns a summary
    dict of the headline numbers (also useful for the GUI to display without re-parsing the HTML).
    Raises ``ValueError`` on unreadable input.
    """
    bdf_path, events_csv_path, out_path = Path(bdf_path), Path(events_csv_path), Path(out_path)
    for p in (bdf_path, events_csv_path):
        if not p.is_file():
            raise ValueError(f"not a file: {p}")

    bdf = read_bdf(bdf_path)
    fs = bdf["fs"]
    st = status_events(bdf["status"], fs)
    events = read_events(events_csv_path)
    refresh_hz = resolve_refresh_hz(events, refresh_rate_hz)
    report = build_verification_report(events, nominal_frame_period_s=1.0 / refresh_hz)

    coded, markers = collect_xpman_triggers(events)
    code_labels: dict[int, set[str]] = defaultdict(set)
    code_counts: Counter = Counter()
    lane_times: dict[str, list[float]] = defaultdict(list)
    for ts, code, lab in coded:
        code_labels[code].add(lab)
        code_counts[code] += 1
        lane_times[lab].append(ts)
    logged_codes = set(code_labels)

    xt = np.array(sorted(t for t, _, _ in coded))
    bt = (
        np.sort(st["times_s"][np.isin(st["codes"], list(logged_codes))])
        if len(st["times_s"]) and logged_codes
        else np.array([])
    )
    align = align_clocks(xt, bt)
    lat = robust_latency(st["times_s"], *(photo_edges(bdf["photo"], fs)[:1])) if bdf["photo"] is not None else {"ok": False}

    def to_bdf(ts: np.ndarray) -> np.ndarray:
        return (align["slope"] * ts + align["offset_s"]) if align.get("ok") else ts

    photo_norm: list[float] = []
    photo_edges_t: list[float] = []
    fs_eff = fs
    if bdf["photo"] is not None:
        ph = bdf["photo"].astype(np.float64)
        step = max(len(ph) // max_photo_points, 1)
        lo, hi = np.percentile(ph, 2), np.percentile(ph, 98)
        rng = (hi - lo) or 1.0
        photo_norm = ((ph[::step] - lo) / rng).clip(-0.1, 1.1).round(4).tolist()
        pe, _, _ = photo_edges(bdf["photo"], fs)
        photo_edges_t = pe.round(4).tolist()
        fs_eff = fs / step

    bdf_codes = []
    for code in sorted(set(st["codes"].tolist())):
        labs = code_labels.get(int(code))
        label = "/".join(sorted(labs)) if labs else "unlabelled (segment/reserved?)"
        bdf_codes.append({"code": int(code), "label": label,
                          "times": st["times_s"][st["codes"] == code].round(4).tolist()})

    xpman_lanes = []
    for lab, times in list(lane_times.items()) + list(markers.items()):
        xpman_lanes.append({"name": lab, "color": LABEL_COLORS.get(lab, "#94a3b8"),
                            "times": to_bdf(np.array(times)).round(4).tolist()})

    codebook = []
    for code in sorted(set(st["codes"].tolist()) | logged_codes):
        n_bdf_c = int(np.sum(st["codes"] == code)) if len(st["codes"]) else 0
        n_xp = int(code_counts.get(code, 0))
        labs = code_labels.get(code)
        if labs:
            codebook.append({"code": code, "meaning": "/".join(sorted(labs)), "n_bdf": n_bdf_c,
                             "n_xpman": n_xp, "ok": n_bdf_c == n_xp,
                             "note": "match" if n_bdf_c == n_xp else f"Δ={n_bdf_c - n_xp} (BDF−xpman)"})
        else:
            codebook.append({"code": code, "meaning": "— (unmatched)", "n_bdf": n_bdf_c,
                             "n_xpman": 0, "ok": None,
                             "note": "on BDF Status but not matched to any xpman-logged trigger code "
                                     "— an older event log, or a manual/external trigger on the line"})

    default_view = None
    if len(st["times_s"]):
        c = float(st["times_s"][int(len(st["times_s"]) * 0.85)])
        default_view = [max(0, c - 3), c + 3]

    fi = report.flip_interval
    payload = {
        "title": f"{bdf_path.name}  vs  {events_csv_path.name}   |   {bdf['n_channels']} ch @ "
                 f"{fs:.0f} Hz, {bdf['duration_s']:.0f} s   |   refresh {refresh_hz:.2f} Hz",
        "fs": fs_eff, "photo_t0": 0.0, "photo": photo_norm, "photo_edges": photo_edges_t,
        "bdf_codes": bdf_codes, "xpman_lanes": xpman_lanes, "label_colors": LABEL_COLORS,
        "default_view": default_view,
    }

    n_bdf = int(len(st["times_s"]))
    n_logged_bdf = int(np.sum(np.isin(st["codes"], list(logged_codes)))) if (n_bdf and logged_codes) else 0
    n_unlabelled = n_bdf - n_logged_bdf
    cards: list[dict] = []
    if fi.mean_interval_s is not None:
        cards.append({"label": "Dropped frames (xpman)", "value": f"{fi.n_outliers}",
                      "sub": f"of {fi.n_flips} flips (>1.5× frame)", "ok": fi.n_outliers == 0})
        cards.append({"label": "Inter-flip jitter (xpman)", "value": f"{fi.stddev_interval_s*1000:.2f} ms",
                      "sub": f"mean {fi.mean_interval_s*1000:.2f} ms · {fi.empirical_refresh_hz:.2f} Hz",
                      "ok": fi.stddev_interval_s*1000 < 2.0})
    cards.append({"label": "Triggers reconciled", "value": f"{n_logged_bdf} / {len(coded)}",
                  "sub": "BDF (logged codes) / xpman coded events"
                         + (f" · +{n_unlabelled} BDF unlabelled" if n_unlabelled else ""),
                  "ok": n_logged_bdf == len(coded)})
    if align.get("ok"):
        # align_clocks pairs the two trigger sequences BY POSITION over min(len). If the counts
        # differ at all (a dropped/extra edge), every pair after the discrepancy is mismatched and
        # the fitted slope/residual are meaningless -- so flag it loudly and fail the slope/residual
        # cards rather than showing a falsely-green "aligned" when the pairing can't be trusted.
        counts_agree = align.get("count_match", True)
        if not counts_agree:
            cards.append({"label": "⚠ Trigger counts differ", "value": f"{align['n_bdf']} vs {align['n_xpman']}",
                          "sub": "BDF vs xpman — pairing is by position, so the alignment below is "
                                 "unreliable (a dropped/extra trigger shifts every later pair)",
                          "ok": False})
        cards.append({"label": "Clock alignment slope", "value": f"{align['slope']:.6f}",
                      "sub": f"{(align['slope']-1.0)*1e6:+.0f} ppm PC↔BioSemi drift"
                             + ("" if counts_agree else " · counts differ, unreliable"),
                      "ok": counts_agree and abs(align["slope"] - 1.0) < 1e-3})
        cards.append({"label": "Cross-system residual", "value": f"{align['resid_sd_ms']:.2f} ms",
                      "sub": f"max {align['resid_max_ms']:.2f} ms · n={align['n_paired']}"
                             + ("" if counts_agree else " · counts differ, unreliable"),
                      "ok": counts_agree and align["resid_sd_ms"] < 2.0})
    if st["pulse_ms"].size:
        pw = st["pulse_ms"]
        cards.append({"label": "Pulse width (BDF)", "value": f"{pw.mean():.2f} ms",
                      "sub": f"sd {pw.std():.2f} · MMBT-S ~8 ms", "ok": 5.0 <= pw.mean() <= 12.0})
    if lat.get("ok"):
        cards.append({"label": "Cmd→photon latency (BDF)", "value": f"{lat['clean_mean_ms']:.1f} ms",
                      "sub": f"jitter {lat['clean_sd_ms']:.2f} ms · {lat['n_clean']}/{lat['n_total']} paired",
                      "ok": abs(lat["clean_mean_ms"]) < 40.0})
    cards.append({"label": "Distinct codes", "value": f"{len(set(st['codes'].tolist()) | logged_codes)}",
                  "sub": "on BDF ∪ xpman — see codebook below", "ok": None})

    if plotly_js_path is None:
        plotly_js_path = default_plotly_path()
    plotly_js = Path(plotly_js_path).read_text(encoding="utf-8") if plotly_js_path else None
    html = build_report_html(payload, cards, codebook, plotly_js)
    out_path.write_text(html, encoding="utf-8")

    return {
        "out_path": str(out_path),
        "size_mb": out_path.stat().st_size / 1e6,
        "n_channels": bdf["n_channels"], "fs": fs, "duration_s": bdf["duration_s"],
        "n_bdf_triggers": n_bdf, "n_distinct_codes": len(set(st["codes"].tolist()) | logged_codes),
        "n_xpman_coded": len(coded), "n_flips": fi.n_flips, "n_dropped": fi.n_outliers,
        "reconciled": n_logged_bdf == len(coded), "n_unlabelled": n_unlabelled,
        "codebook": codebook,
        "align": align if align.get("ok") else None,
        "latency": lat if lat.get("ok") else None,
        "offline": plotly_js is not None,
    }


def main(argv: "list[str] | None" = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bdf", type=Path, required=True)
    ap.add_argument("--events-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("integration_report.html"))
    ap.add_argument("--plotly-js", type=Path, default=None, help="Local plotly.min.js to inline (offline report).")
    ap.add_argument("--refresh-rate-hz", type=float, default=None)
    ap.add_argument("--max-photo-points", type=int, default=200000, help="Decimate Photo above this many samples.")
    args = ap.parse_args(argv)
    try:
        s = generate_report(args.bdf, args.events_csv, args.out, plotly_js_path=args.plotly_js,
                            refresh_rate_hz=args.refresh_rate_hz, max_photo_points=args.max_photo_points)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Wrote {s['out_path']}  ({s['size_mb']:.1f} MB, {'offline' if s['offline'] else 'CDN Plotly'})")
    print(f"  BDF: {s['n_channels']} ch @ {s['fs']:.0f} Hz, {s['duration_s']:.0f}s, "
          f"{s['n_bdf_triggers']} triggers ({s['n_distinct_codes']} distinct codes)")
    print(f"  xpman: {s['n_xpman_coded']} coded triggers, {s['n_flips']} flips, {s['n_dropped']} dropped")
    if s["align"]:
        print(f"  alignment: slope={s['align']['slope']:.6f} residual_sd={s['align']['resid_sd_ms']:.2f}ms")
    if s["latency"]:
        print(f"  latency: {s['latency']['clean_mean_ms']:.1f}ms jitter {s['latency']['clean_sd_ms']:.2f}ms")


if __name__ == "__main__":
    main()
