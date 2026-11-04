"""
Generates a synthetic 30-minute multi-user RR session for testing analysis_app_v1.py.

Output (same format as t5.csv / t5_events.csv):
  <name>.csv         Timestamp, UserID, HeartRate_bpm, RR_Interval_ms, Event
  <name>_events.csv  Timestamp, Event, Label

Ground truth built in (per user):
  setup (2 min)  : discarded by the analysis
  Baseline (10)  : breathing 0.25 Hz (15/min), moderate RSA
  Meditation (10): slow breathing 0.10 Hz (6/min), large RSA, lower HR
                   -> RMSSD up, HF power DOWN (respiration moves into LF band)
  Discussion (10): irregular faster breathing ~0.30 Hz, small RSA, higher HR
                   -> RMSSD and HF down
A few artifacts (extra/missed beats) are injected so artifact correction is exercised.

Usage: python make_synthetic_session.py [name] [n_users] [seed]
"""

import sys
import csv
from datetime import datetime, timedelta

import numpy as np

SETUP_S, BLOCK_S = 120, 600
BLOCKS = [
    # label,       mean RR (ms), resp freq (Hz), RSA amplitude (ms), noise SD (ms)
    ("Baseline",   900.0, 0.25, 25.0, 10.0),
    ("Meditation", 960.0, 0.10, 80.0, 10.0),
    ("Discussion", 820.0, 0.30, 12.0, 14.0),
]


def generate_user(rng, user_offset_ms):
    """Returns list of (beat_end_time_s, rr_ms) for one user."""
    segments = [("setup", 0.0, SETUP_S, BLOCKS[0])] + [
        (b[0], SETUP_S + i * BLOCK_S, SETUP_S + (i + 1) * BLOCK_S, b) for i, b in enumerate(BLOCKS)]
    total = SETUP_S + len(BLOCKS) * BLOCK_S
    beats, t, phase = [], 0.0, rng.uniform(0, 2 * np.pi)
    slow = 0.0
    while t < total:
        _, _, _, (label, mrr, f, amp, sd) = next(s for s in segments if s[1] <= t < s[2])
        if label == "Discussion":
            f = f * (1 + 0.3 * np.sin(2 * np.pi * t / 37.0))   # irregular speech breathing
        phase += 2 * np.pi * f * (beats[-1][1] / 1000.0 if beats else 1.0)
        slow = 0.98 * slow + rng.normal(0, 4)                 # slow (LF/VLF) drift
        rr = mrr + user_offset_ms + amp * np.sin(phase) + slow + rng.normal(0, sd)
        rr = float(np.clip(rr, 400, 1800))
        t += rr / 1000.0
        beats.append((t, rr))

    # Inject artifacts: extra beats (split) and missed beats (merge)
    out, i = [], 0
    while i < len(beats):
        t_end, rr = beats[i]
        u = rng.random()
        if u < 0.004:                         # extra/false beat -> split interval
            k = rng.uniform(0.25, 0.45)
            out.append((t_end - rr * (1 - k) / 1000.0, rr * k))
            out.append((t_end, rr * (1 - k)))
        elif u < 0.007 and i + 1 < len(beats):  # missed beat -> merge two intervals
            out.append((beats[i + 1][0], rr + beats[i + 1][1]))
            i += 1
        else:
            out.append((t_end, rr))
        i += 1
    return out


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "synthetic_session"
    n_users = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 11, 1, 10, 0, 0)

    rows = []
    for u in range(n_users):
        uid = f"User_{u + 1}"
        start = rng.uniform(0, 2)                       # sensors start a bit apart
        for t_end, rr in generate_user(rng, rng.normal(0, 40)):
            ts = t0 + timedelta(seconds=start + t_end + rng.normal(0, 0.02))
            rows.append((ts, uid, round(60000.0 / rr), round(rr, 1), ""))

    events = []
    for i, (label, *_rest) in enumerate(BLOCKS):
        ts = t0 + timedelta(seconds=SETUP_S + i * BLOCK_S)
        events.append((ts, f"Event{i + 1}", label))
        rows.append((ts, "EVENT", "", "", f"Event{i + 1}"))

    rows.sort(key=lambda r: r[0])
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with open(f"{name}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "UserID", "HeartRate_bpm", "RR_Interval_ms", "Event"])
        for ts, uid, hr, rr, ev in rows:
            w.writerow([fmt(ts), uid, hr, rr, ev])
    with open(f"{name}_events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Event", "Label"])
        for ts, ev, label in events:
            w.writerow([fmt(ts), ev, label])
    print(f"Wrote {name}.csv ({len(rows)} rows, {n_users} users) and {name}_events.csv")


if __name__ == "__main__":
    main()
