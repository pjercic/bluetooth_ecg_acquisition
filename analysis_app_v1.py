"""
HRV Block Analysis (v1)

Analyzes RR-interval recordings (<session>.csv) split into blocks by labelled events
(<session>_events.csv), per UserID and for the group, using neurokit2.

RR file columns   : Timestamp, UserID, HeartRate_bpm, RR_Interval_ms[, Event]
Events file columns: Timestamp, Event[, Label]

Each event marks the START of a block named by its Label. Data before the first event is
discarded; the last block ends at the last RR sample.

Metrics
  Sliding window (default 120 s, step 10 s): RMSSD, SDNN, ln(HF)
  Per block (first 60 s skipped as settling): RMSSD, SDNN, HF, ln(HF), HF peak frequency,
  SampEn, mean HR, beats, % corrected beats
"""

import sys
import asyncio
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import neurokit2 as nk

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QMessageBox, QTableWidget, QTableWidgetItem,
                             QHeaderView, QFileDialog, QDoubleSpinBox,
                             QComboBox, QTabWidget, QFormLayout)
from qasync import QEventLoop, asyncSlot

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT

warnings.filterwarnings("ignore", module="neurokit2")

# ==============================================================================
# Analysis settings & constants
# ==============================================================================

HF_BAND = (0.15, 0.40)          # Hz
INTERP_FS = 4.0                 # Hz, RR tachogram resampling for spectral analysis
MIN_DUR_TIME_S = 20.0           # minimum segment duration for RMSSD/SDNN
MIN_DUR_HF_S = 60.0             # minimum segment duration for HF
MIN_BEATS_SAMPEN = 100          # minimum beats for Sample Entropy
GAP_TOLERANCE_S = 3.0           # timestamp gap (beyond RR) that splits the recording

WINDOW_METRICS = ["RMSSD", "SDNN", "LnHF"]
METRIC_LABELS = {
    "RMSSD": "RMSSD (ms)",
    "SDNN": "SDNN (ms)",
    "LnHF": "ln HF (ln ms²)",
    "SampEn": "SampEn",
}
BLOCK_COLUMNS = ["User", "Block", "Start_s", "End_s", "Duration_s", "Beats", "Corrected_pct",
                 "MeanHR_bpm", "RMSSD", "SDNN", "HF", "LnHF", "HF_PeakFreq_Hz", "SampEn"]
CHANGE_METRICS = ["MeanHR_bpm", "RMSSD", "SDNN", "HF", "LnHF", "SampEn"]


@dataclass
class Settings:
    window_s: float = 120.0
    step_s: float = 10.0
    settle_s: float = 60.0
    artifact_pct: float = 5.0


@dataclass
class UserSeries:
    user: str
    t: np.ndarray            # corrected beat times (s, relative to first event)
    rr: np.ndarray           # corrected RR intervals (ms)
    orig_t: np.ndarray       # original beat times (s)
    flagged_t: np.ndarray    # times of beats flagged/corrected by artifact correction


@dataclass
class Results:
    session_start: pd.Timestamp
    blocks: List[Tuple[str, float, float]]                 # (label, start_s, end_s)
    event_times: List[float]
    window_centers: np.ndarray
    windows: Dict[str, pd.DataFrame] = field(default_factory=dict)  # user -> per-window df
    group_windows: pd.DataFrame = None
    block_table: pd.DataFrame = None
    change_table: pd.DataFrame = None
    group_table: pd.DataFrame = None
    warnings: List[str] = field(default_factory=list)


# ==============================================================================
# Loading
# ==============================================================================

def load_events(path: str) -> pd.DataFrame:
    ev = pd.read_csv(path)
    ev.columns = [c.strip() for c in ev.columns]
    if "Timestamp" not in ev.columns:
        raise ValueError("Events file must have a 'Timestamp' column.")
    ev["Timestamp"] = pd.to_datetime(ev["Timestamp"])
    if "Label" not in ev.columns or ev["Label"].isna().all():
        ev["Label"] = ev.get("Event", pd.Series([f"Block{i + 1}" for i in range(len(ev))]))
    ev["Label"] = ev["Label"].fillna(ev.get("Event")).astype(str)
    ev = ev.sort_values("Timestamp").reset_index(drop=True)
    if ev.empty:
        raise ValueError("Events file contains no events.")
    return ev


def load_rr(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    for col in ("Timestamp", "UserID", "RR_Interval_ms"):
        if col not in df.columns:
            raise ValueError(f"RR file is missing column '{col}'.")
    df = df[df["UserID"].astype(str).str.upper() != "EVENT"]
    df = df.dropna(subset=["RR_Interval_ms"]).copy()
    df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    df["RR_Interval_ms"] = df["RR_Interval_ms"].astype(float)
    df["UserID"] = df["UserID"].astype(str)
    return df.sort_values("Timestamp").reset_index(drop=True)


# ==============================================================================
# Artifact correction
# ==============================================================================

def merge_extra_beats(peaks: np.ndarray, ratio: float = 0.7, half_win: int = 5) -> np.ndarray:
    """Pre-pass before Kubios for gross detection errors.
    Extra beats: removes a peak when its interval is shorter than `ratio` x local median RR,
    choosing the neighbour merge whose sum is closest to the median.
    Missed beats: splits intervals of ~k x local median into k equal intervals.
    Complements Kubios, whose adaptive thresholds miss clustered artifacts."""
    p = list(int(x) for x in peaks)
    changed = True
    while changed and len(p) > 3:
        changed = False
        rr = np.diff(p)
        for i in range(len(rr)):
            lo, hi = max(0, i - half_win), min(len(rr), i + half_win + 1)
            med = np.median(rr[lo:hi])
            if rr[i] < ratio * med:
                prev_sum = rr[i] + rr[i - 1] if i > 0 else np.inf
                next_sum = rr[i] + rr[i + 1] if i + 1 < len(rr) else np.inf
                if not np.isfinite(prev_sum) and not np.isfinite(next_sum):
                    break
                # merging with previous removes peak i; with next removes peak i+1
                del p[i if abs(prev_sum - med) <= abs(next_sum - med) else i + 1]
                changed = True
                break

    # Missed beats: an interval of ~k x local median (k >= 2) is split into k equal intervals
    rr = np.diff(p)
    out = [p[0]]
    for i in range(len(rr)):
        lo, hi = max(0, i - half_win), min(len(rr), i + half_win + 1)
        med = np.median(rr[lo:hi])
        k = int(round(rr[i] / med)) if med > 0 else 1
        if k >= 2 and abs(rr[i] / k - med) < (1 - ratio) * med:
            out.extend(int(round(p[i] + rr[i] * j / k)) for j in range(1, k))
        out.append(p[i + 1])
    return np.asarray(out, dtype=int)


def correct_user(user: str, udf: pd.DataFrame, t0: pd.Timestamp) -> UserSeries:
    """Kubios artifact correction (neurokit2) on each continuous segment of a user's RR series.

    Each RR timestamp marks the end of its interval. Beat times inside a segment are
    reconstructed by cumulative sum of RR, anchored at the segment's first timestamp.
    """
    ts = (udf["Timestamp"] - t0).dt.total_seconds().to_numpy()
    rr = udf["RR_Interval_ms"].to_numpy()

    # Split into segments at transmission gaps
    breaks = [0]
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] > rr[i] / 1000.0 + GAP_TOLERANCE_S:
            breaks.append(i)
    breaks.append(len(ts))

    out_t, out_rr, orig_t, flagged = [], [], [], []
    for a, b in zip(breaks[:-1], breaks[1:]):
        seg_rr = rr[a:b]
        if len(seg_rr) == 0:
            continue
        base = ts[a] - seg_rr[0] / 1000.0                     # time of the segment's first peak
        peaks = np.concatenate([[0], np.cumsum(np.round(seg_rr)).astype(int)])
        orig_t.append(base + peaks[1:] / 1000.0)
        clean = merge_extra_beats(peaks)
        if len(clean) > 10:
            try:
                _, k = nk.signal_fixpeaks(clean, sampling_rate=1000, iterative=True, method="Kubios")
                clean = np.asarray(k, dtype=int)
            except Exception:
                pass
        # Corrected beats = original peaks removed/moved + peaks inserted (symmetric difference)
        changed = np.setxor1d(peaks[1:], clean[1:])
        flagged.append(base + changed / 1000.0)
        out_t.append(base + clean[1:] / 1000.0)
        out_rr.append(np.diff(clean).astype(float))

    cat = lambda xs: np.concatenate(xs) if xs else np.array([])
    return UserSeries(user, cat(out_t), cat(out_rr), cat(orig_t), cat(flagged))


# ==============================================================================
# Metrics
# ==============================================================================

def hf_power(t: np.ndarray, rr: np.ndarray) -> Tuple[float, float]:
    """HF power (ms²) and HF peak frequency (Hz) from Welch PSD of the 4 Hz resampled tachogram."""
    if len(rr) < 4 or t[-1] - t[0] < MIN_DUR_HF_S:
        return np.nan, np.nan
    x = np.arange(t[0], t[-1], 1.0 / INTERP_FS)
    y = nk.signal_interpolate(t, rr, x_new=x, method="cubic")
    y = nk.signal_detrend(y, order=1)
    psd = nk.signal_psd(y, sampling_rate=INTERP_FS, method="welch", min_frequency=0.04,
                        max_frequency=0.5, normalize=False)
    sel = (psd["Frequency"] >= HF_BAND[0]) & (psd["Frequency"] < HF_BAND[1])
    if sel.sum() < 2:
        return np.nan, np.nan
    f, p = psd["Frequency"][sel].to_numpy(), psd["Power"][sel].to_numpy()
    return float(np.trapezoid(p, f)), float(f[np.argmax(p)])


def time_metrics(t: np.ndarray, rr: np.ndarray) -> Tuple[float, float]:
    if len(rr) < 5 or t[-1] - t[0] < MIN_DUR_TIME_S:
        return np.nan, np.nan
    r = nk.hrv_time({"RRI": rr, "RRI_Time": t - t[0]}, sampling_rate=1000)
    return float(r["HRV_RMSSD"].iloc[0]), float(r["HRV_SDNN"].iloc[0])


def sample_entropy(rr: np.ndarray) -> float:
    if len(rr) < MIN_BEATS_SAMPEN:
        return np.nan
    val, _ = nk.entropy_sample(rr, dimension=2, tolerance=0.2 * np.std(rr, ddof=1))
    return float(val) if np.isfinite(val) else np.nan


def segment(us: UserSeries, a: float, b: float):
    m = (us.t >= a) & (us.t < b)
    n_orig = int(((us.orig_t >= a) & (us.orig_t < b)).sum())
    n_flag = int(((us.flagged_t >= a) & (us.flagged_t < b)).sum())
    pct = 100.0 * n_flag / n_orig if n_orig else np.nan
    return us.t[m], us.rr[m], pct


# ==============================================================================
# Main analysis
# ==============================================================================

def analyze(rr_path: str, events_path: str, s: Settings) -> Results:
    ev = load_events(events_path)
    df = load_rr(rr_path)
    if df.empty:
        raise ValueError("RR file contains no RR intervals.")

    t0 = ev["Timestamp"].iloc[0]
    event_times = [(x - t0).total_seconds() for x in ev["Timestamp"]]
    users = list(dict.fromkeys(df["UserID"]))
    series = {u: correct_user(u, df[df["UserID"] == u], t0) for u in users}

    end_all = max(us.t[-1] for us in series.values() if len(us.t))
    bounds = event_times + [end_all]
    blocks = [(ev["Label"].iloc[i], bounds[i], bounds[i + 1]) for i in range(len(ev))]

    half = s.window_s / 2.0
    centers = np.arange(half, end_all - half + 1e-9, s.step_s) if end_all > s.window_s else np.array([])
    res = Results(t0, blocks, event_times, centers)

    # --- Sliding windows ---
    for u, us in series.items():
        rows = []
        for c in centers:
            a, b = c - half, c + half
            tt, rr, pct = segment(us, a, b)
            rmssd, sdnn = time_metrics(tt, rr) if len(tt) else (np.nan, np.nan)
            hf, _ = hf_power(tt, rr) if len(tt) else (np.nan, np.nan)
            straddle = any(a < e < b for e in event_times[1:])
            rows.append({"Center_s": c, "Beats": len(rr), "Corrected_pct": pct,
                         "RMSSD": rmssd, "SDNN": sdnn,
                         "LnHF": np.log(hf) if hf and hf > 0 else np.nan,
                         "Straddles_event": straddle,
                         "Bad_quality": bool(pct > s.artifact_pct) if np.isfinite(pct) else True})
        res.windows[u] = pd.DataFrame(rows)

    # Group mean ± SD of good, non-straddling-agnostic windows (straddling kept, styled in plot)
    grp = {"Center_s": centers}
    for m in WINDOW_METRICS:
        stack = []
        for wdf in res.windows.values():
            v = wdf[m].to_numpy(dtype=float).copy() if len(wdf) else np.full(len(centers), np.nan)
            if len(wdf):
                v[wdf["Bad_quality"].to_numpy()] = np.nan
            stack.append(v)
        arr = np.vstack(stack) if stack else np.empty((0, len(centers)))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            grp[f"{m}_mean"] = np.nanmean(arr, axis=0) if arr.size else np.array([])
            grp[f"{m}_sd"] = np.nanstd(arr, axis=0, ddof=1) if arr.shape[0] > 1 else np.full(len(centers), np.nan)
            grp[f"{m}_n"] = np.sum(np.isfinite(arr), axis=0) if arr.size else np.array([])
    res.group_windows = pd.DataFrame(grp)
    if len(centers) and "Straddles_event" in next(iter(res.windows.values()), pd.DataFrame()):
        res.group_windows["Straddles_event"] = next(iter(res.windows.values()))["Straddles_event"].values

    # --- Per-block metrics ---
    brows = []
    for u, us in series.items():
        for label, a, b in blocks:
            a2 = a + s.settle_s
            if a2 >= b:
                res.warnings.append(f"{u} / {label}: block shorter than settling period ({s.settle_s:.0f} s).")
            tt, rr, pct = segment(us, a2, b)
            dur = float(tt[-1] - tt[0]) if len(tt) > 1 else 0.0
            rmssd, sdnn = time_metrics(tt, rr) if len(tt) else (np.nan, np.nan)
            hf, pf = hf_power(tt, rr) if len(tt) else (np.nan, np.nan)
            se = sample_entropy(rr)
            if len(tt) and dur < MIN_DUR_HF_S:
                res.warnings.append(f"{u} / {label}: {dur:.0f} s analysed - too short for HF.")
            if len(rr) and len(rr) < MIN_BEATS_SAMPEN:
                res.warnings.append(f"{u} / {label}: {len(rr)} beats - too few for SampEn.")
            if np.isfinite(pct) and pct > s.artifact_pct:
                res.warnings.append(f"{u} / {label}: {pct:.1f}% beats corrected (> {s.artifact_pct:.0f}%).")
            brows.append({"User": u, "Block": label, "Start_s": a2, "End_s": b, "Duration_s": dur,
                          "Beats": len(rr), "Corrected_pct": pct,
                          "MeanHR_bpm": 60000.0 / np.mean(rr) if len(rr) else np.nan,
                          "RMSSD": rmssd, "SDNN": sdnn, "HF": hf,
                          "LnHF": np.log(hf) if hf and hf > 0 else np.nan,
                          "HF_PeakFreq_Hz": pf, "SampEn": se})
    res.block_table = pd.DataFrame(brows, columns=BLOCK_COLUMNS)

    # --- Change from first block (baseline) ---
    base_label = blocks[0][0]
    crows = []
    for u in users:
        ub = res.block_table[res.block_table["User"] == u].set_index("Block")
        for label, _, _ in blocks[1:]:
            row = {"User": u, "Contrast": f"{label} - {base_label}"}
            for m in CHANGE_METRICS:
                row[f"d_{m}"] = ub.at[label, m] - ub.at[base_label, m]
            crows.append(row)
    res.change_table = pd.DataFrame(crows, columns=["User", "Contrast"] + [f"d_{m}" for m in CHANGE_METRICS])

    # --- Group summary (mean ± SD) ---
    grows = []
    for label, _, _ in blocks:
        sub = res.block_table[res.block_table["Block"] == label]
        row = {"Row": label, "N": int(sub["RMSSD"].notna().sum())}
        for m in CHANGE_METRICS + ["HF_PeakFreq_Hz", "Corrected_pct"]:
            row[f"{m}_mean"] = sub[m].mean()
            row[f"{m}_sd"] = sub[m].std(ddof=1)
        grows.append(row)
    for contrast in res.change_table["Contrast"].unique() if len(res.change_table) else []:
        sub = res.change_table[res.change_table["Contrast"] == contrast]
        row = {"Row": contrast, "N": int(sub["d_RMSSD"].notna().sum())}
        for m in CHANGE_METRICS:
            row[f"{m}_mean"] = sub[f"d_{m}"].mean()
            row[f"{m}_sd"] = sub[f"d_{m}"].std(ddof=1)
        grows.append(row)
    res.group_table = pd.DataFrame(grows)
    return res


# ==============================================================================
# Plotting
# ==============================================================================

BLOCK_COLORS = ["#4C72B0", "#55A868", "#C44E52", "#8172B2", "#CCB974", "#64B5CD"]


def draw_results(fig: Figure, res: Results, show_user: Optional[str]):
    """show_user=None -> all users + group; otherwise a single user."""
    fig.clear()
    metrics = WINDOW_METRICS + ["SampEn"]
    axes = fig.subplots(len(metrics), 1, sharex=True,
                        gridspec_kw={"height_ratios": [3, 3, 3, 1.8]})
    users = list(res.windows.keys()) if show_user is None else [show_user]
    cmap = matplotlib.colormaps["tab10"]
    ucolor = {u: cmap(i % 10) for i, u in enumerate(res.windows.keys())}
    to_min = lambda sec: np.asarray(sec, dtype=float) / 60.0

    for ax, m in zip(axes, metrics):
        # Block shading + event lines
        for i, (label, a, b) in enumerate(res.blocks):
            ax.axvspan(to_min(a), to_min(b), color=BLOCK_COLORS[i % len(BLOCK_COLORS)], alpha=0.07, lw=0)
            ax.axvline(to_min(a), color="0.3", lw=0.8, ls="--")
            if ax is axes[0]:
                ax.text(to_min(a), 1.01, f" {label}", transform=ax.get_xaxis_transform(),
                        fontsize=9, va="bottom", ha="left", fontweight="bold")

        # Sliding-window traces (not for SampEn)
        if m in WINDOW_METRICS:
            for u in users:
                wdf = res.windows[u]
                if wdf.empty:
                    continue
                x, y = to_min(wdf["Center_s"]), wdf[m].to_numpy(dtype=float)
                lw, alpha = (0.9, 0.55) if show_user is None else (1.6, 1.0)
                ax.plot(x, y, color=ucolor[u], lw=lw, alpha=0.18)              # faded full trace
                clean = y.copy()
                clean[(wdf["Straddles_event"] | wdf["Bad_quality"]).to_numpy()] = np.nan
                ax.plot(x, clean, color=ucolor[u], lw=lw, alpha=alpha,
                        label=u if ax is axes[0] else None)
                bad = wdf["Bad_quality"].to_numpy() & np.isfinite(y)
                if bad.any():
                    ax.plot(x[bad], y[bad], "x", color="0.5", ms=4)
            if show_user is None and res.group_windows is not None and len(res.group_windows):
                g = res.group_windows
                x = to_min(g["Center_s"])
                mu, sd = g[f"{m}_mean"].to_numpy(dtype=float), g[f"{m}_sd"].to_numpy(dtype=float)
                ax.fill_between(x, mu - sd, mu + sd, color="k", alpha=0.10, lw=0)
                mu_clean = mu.copy()
                if "Straddles_event" in g:
                    mu_clean[g["Straddles_event"].to_numpy(dtype=bool)] = np.nan
                ax.plot(x, mu, color="k", lw=2.2, alpha=0.25)
                ax.plot(x, mu_clean, color="k", lw=2.2, label="Group mean ± SD" if ax is axes[0] else None)

        # Per-block points (marker + bar spanning the analysed block)
        bt = res.block_table
        for u in users:
            ub = bt[bt["User"] == u]
            for _, r in ub.iterrows():
                if not np.isfinite(r[m]):
                    continue
                xa, xb = to_min(r["Start_s"]), to_min(r["End_s"])
                ax.hlines(r[m], xa, xb, color=ucolor[u], lw=2.5, alpha=0.8)
                ax.plot((xa + xb) / 2, r[m], "o", color=ucolor[u], ms=6, mec="white", mew=0.8)
        if show_user is None and len(bt):
            for label, a, b in res.blocks:
                sub = bt[bt["Block"] == label][m].dropna()
                if len(sub) == 0:
                    continue
                xa = to_min(a + (bt[bt["Block"] == label]["Start_s"].iloc[0] - a))
                xm = (xa + to_min(b)) / 2
                sd = sub.std(ddof=1) if len(sub) > 1 else 0.0
                ax.errorbar(xm, sub.mean(), yerr=sd, fmt="D", color="k", ms=7, capsize=5,
                            lw=1.5, zorder=5)

        ax.set_ylabel(METRIC_LABELS[m])
        ax.grid(alpha=0.25)

    axes[-1].set_xlabel("Time since first event (min)")
    handles, labels = axes[0].get_legend_handles_labels()
    if handles:
        axes[0].legend(handles, labels, loc="upper right", fontsize=8, ncol=min(4, len(labels)))
    title = "HRV over time - all users" if show_user is None else f"HRV over time - {show_user}"
    fig.suptitle(title, fontsize=12)
    fig.tight_layout()


# ==============================================================================
# GUI
# ==============================================================================

class AnalysisApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HRV Block Analysis (neurokit2)")
        self.resize(1200, 900)
        self.results: Optional[Results] = None
        self.init_ui()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)

        # --- Files ---
        main.addWidget(QLabel("--- Step 1: Select Data Files ---"))
        self.rr_input = QLineEdit()
        self.rr_input.setPlaceholderText("RR intervals CSV, e.g. t5.csv")
        rr_btn = QPushButton("📂 RR File...")
        rr_btn.clicked.connect(self.browse_rr)
        row = QHBoxLayout(); row.addWidget(self.rr_input); row.addWidget(rr_btn)
        main.addLayout(row)

        self.ev_input = QLineEdit()
        self.ev_input.setPlaceholderText("Events CSV, e.g. t5_events.csv (auto-filled)")
        ev_btn = QPushButton("📂 Events File...")
        ev_btn.clicked.connect(self.browse_events)
        row = QHBoxLayout(); row.addWidget(self.ev_input); row.addWidget(ev_btn)
        main.addLayout(row)

        # --- Settings ---
        main.addWidget(QLabel("--- Step 2: Settings ---"))
        form_row = QHBoxLayout()
        form = QFormLayout()
        self.win_spin = self._spin(30, 600, 120, " s")
        self.step_spin = self._spin(1, 120, 10, " s")
        form.addRow("Sliding window:", self.win_spin)
        form.addRow("Window step:", self.step_spin)
        form2 = QFormLayout()
        self.settle_spin = self._spin(0, 300, 60, " s")
        self.art_spin = self._spin(0, 100, 5, " %")
        form2.addRow("Skip at block start:", self.settle_spin)
        form2.addRow("Max corrected beats:", self.art_spin)
        form_row.addLayout(form); form_row.addLayout(form2); form_row.addStretch()
        main.addLayout(form_row)

        # --- Actions ---
        act = QHBoxLayout()
        self.analyze_btn = QPushButton("▶ ANALYZE")
        self.analyze_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        self.analyze_btn.clicked.connect(self.run_analysis)
        self.export_btn = QPushButton("💾 Export Results")
        self.export_btn.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold; padding: 10px;")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_results)
        act.addWidget(self.analyze_btn); act.addWidget(self.export_btn)
        main.addLayout(act)

        self.status_label = QLabel("Select an RR file and an events file.")
        self.status_label.setStyleSheet("color: #666; font-style: italic;")
        self.status_label.setWordWrap(True)
        main.addWidget(self.status_label)

        # --- Results tabs ---
        self.tabs = QTabWidget()
        plot_tab = QWidget()
        pl = QVBoxLayout(plot_tab)
        sel = QHBoxLayout()
        sel.addWidget(QLabel("Show:"))
        self.user_combo = QComboBox()
        self.user_combo.currentIndexChanged.connect(self.redraw)
        sel.addWidget(self.user_combo); sel.addStretch()
        pl.addLayout(sel)
        self.figure = Figure(figsize=(11, 8))
        self.canvas = FigureCanvasQTAgg(self.figure)
        pl.addWidget(NavigationToolbar2QT(self.canvas, self))
        pl.addWidget(self.canvas)
        self.tabs.addTab(plot_tab, "📈 Plot")

        self.block_tbl = QTableWidget()
        self.change_tbl = QTableWidget()
        self.group_tbl = QTableWidget()
        self.tabs.addTab(self.block_tbl, "Per-block (users)")
        self.tabs.addTab(self.change_tbl, "Change from baseline")
        self.tabs.addTab(self.group_tbl, "Group summary")
        main.addWidget(self.tabs, stretch=1)

    def _spin(self, lo, hi, val, suffix):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi); sp.setValue(val); sp.setSuffix(suffix); sp.setDecimals(0)
        return sp

    def browse_rr(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select RR CSV", "", "CSV files (*.csv)")
        if path:
            self.rr_input.setText(path)
            guess = path[:-4] + "_events.csv"
            import os
            if os.path.exists(guess):
                self.ev_input.setText(guess)

    def browse_events(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Events CSV", "", "CSV files (*.csv)")
        if path:
            self.ev_input.setText(path)

    @asyncSlot()
    async def run_analysis(self):
        rr_path, ev_path = self.rr_input.text().strip(), self.ev_input.text().strip()
        if not rr_path or not ev_path:
            QMessageBox.warning(self, "Missing Files", "Please select both an RR file and an events file.")
            return
        s = Settings(self.win_spin.value(), self.step_spin.value(),
                     self.settle_spin.value(), self.art_spin.value())
        self.analyze_btn.setEnabled(False)
        self.analyze_btn.setText("Analyzing... please wait")
        self.status_label.setText("Running artifact correction and HRV analysis...")
        try:
            self.results = await asyncio.to_thread(analyze, rr_path, ev_path, s)
        except Exception as e:
            QMessageBox.critical(self, "Analysis Error", f"Analysis failed:\n{e}")
            self.status_label.setText(f"❌ Analysis failed: {e}")
            return
        finally:
            self.analyze_btn.setEnabled(True)
            self.analyze_btn.setText("▶ ANALYZE")

        res = self.results
        self.user_combo.blockSignals(True)
        self.user_combo.clear()
        self.user_combo.addItem("All users + group")
        self.user_combo.addItems(list(res.windows.keys()))
        self.user_combo.blockSignals(False)
        self.redraw()
        self.fill_table(self.block_tbl, res.block_table)
        self.fill_table(self.change_tbl, res.change_table)
        self.fill_table(self.group_tbl, res.group_table)
        self.export_btn.setEnabled(True)

        blocks = ", ".join(f"{l} ({(b - a) / 60:.1f} min)" for l, a, b in res.blocks)
        msg = f"✅ {len(res.windows)} users | Blocks: {blocks} | {len(res.window_centers)} windows"
        if res.warnings:
            msg += f" | ⚠️ {len(res.warnings)} warnings"
            QMessageBox.warning(self, "Analysis Warnings", "\n".join(res.warnings[:30]) +
                                ("\n..." if len(res.warnings) > 30 else ""))
        self.status_label.setText(msg)

    def redraw(self):
        if not self.results:
            return
        idx = self.user_combo.currentIndex()
        user = None if idx <= 0 else self.user_combo.currentText()
        draw_results(self.figure, self.results, user)
        self.canvas.draw_idle()

    @staticmethod
    def fill_table(tbl: QTableWidget, df: pd.DataFrame):
        tbl.clear()
        tbl.setRowCount(len(df)); tbl.setColumnCount(len(df.columns))
        tbl.setHorizontalHeaderLabels(list(df.columns))
        for i, row in enumerate(df.itertuples(index=False)):
            for j, v in enumerate(row):
                if isinstance(v, (float, np.floating)):
                    text = "" if not np.isfinite(v) else f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
                else:
                    text = str(v)
                tbl.setItem(i, j, QTableWidgetItem(text))
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    def export_results(self):
        if not self.results:
            return
        prefix, _ = QFileDialog.getSaveFileName(self, "Export prefix (files get suffixes)",
                                                self.rr_input.text()[:-4] + "_hrv", "All files (*)")
        if not prefix:
            return
        prefix = prefix[:-4] if prefix.lower().endswith(".csv") else prefix
        res = self.results
        try:
            written = []
            for name, df in (("blocks", res.block_table), ("change", res.change_table),
                             ("group", res.group_table)):
                df.to_csv(f"{prefix}_{name}.csv", index=False); written.append(f"{prefix}_{name}.csv")
            win = pd.concat([w.assign(User=u) for u, w in res.windows.items()], ignore_index=True)
            win.to_csv(f"{prefix}_windows.csv", index=False); written.append(f"{prefix}_windows.csv")
            res.group_windows.to_csv(f"{prefix}_group_windows.csv", index=False)
            written.append(f"{prefix}_group_windows.csv")
            for ext in ("png", "pdf"):
                self.figure.savefig(f"{prefix}_plot.{ext}", dpi=200, bbox_inches="tight")
                written.append(f"{prefix}_plot.{ext}")
            QMessageBox.information(self, "Export Complete", "Written:\n" + "\n".join(written))
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Export failed:\n{e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = AnalysisApp()
    window.show()

    with loop:
        loop.run_forever()
