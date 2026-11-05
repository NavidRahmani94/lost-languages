#!/usr/bin/env python3
"""
LOST LANGUAGES — analysis engine
================================
Every language has a music before it has a meaning.

This script records (or loads) a short spoken sentence, extracts its prosody
(pitch contour, syllable rhythm, energy, brightness) and streams it over OSC:

  * to SuperCollider  -> melody, consonant noise, drone, granular voice
  * to Processing     -> generative calligraphy

Usage
-----
  python lost_languages.py record --seconds 8 --lang fa --dir rtl
  python lost_languages.py file my_voice.wav --lang it --dir ltr
  python lost_languages.py archive            # replay every saved voice
  python lost_languages.py file x.wav --no-osc --plot   # analysis only

OSC protocol (all floats unless noted)
--------------------------------------
  /ll/start  [path:s, duration, root_midi:i, direction:i, label:s]   -> SC + P5
  /ll/note   [midi, amp, dur, bright, pan]                           -> SC
  /ll/noise  [amp, center_hz, dur, pan]                              -> SC
  /ll/frame  [progress, pitch, energy, bright, voiced]               -> P5
  /ll/onset  [energy, voiced]                                        -> P5
  /ll/end    []                                                      -> SC + P5

direction: 0 = left-to-right, 1 = right-to-left, 2 = vertical (top-down)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import numpy as np

SR = 22050          # analysis sample rate
HOP = 256           # ~11.6 ms per frame
DIRECTIONS = {"ltr": 0, "rtl": 1, "vert": 2}

# Candidate modes (semitones above the speaker's centre pitch).
# The voice "chooses" the one that best covers the pitches it actually uses.
MODES = {
    "major pentatonic": [0, 2, 4, 7, 9],
    "minor pentatonic": [0, 3, 5, 7, 10],
    "dorian":           [0, 2, 3, 5, 7, 9, 10],
    "aeolian":          [0, 2, 3, 5, 7, 8, 10],
    "mixolydian":       [0, 2, 4, 5, 7, 9, 10],
    "phrygian":         [0, 1, 3, 5, 7, 8, 10],
    "hijaz":            [0, 1, 4, 5, 7, 8, 10],
    "hirajoshi":        [0, 2, 3, 7, 8],
}

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(HERE, "..", "archive")


# --------------------------------------------------------------------------
# 1. AUDIO INPUT
# --------------------------------------------------------------------------
def record(seconds, out_path, sr=44100):
    """Record from the default microphone and save a mono WAV."""
    import sounddevice as sd
    import soundfile as sf

    print(f"\n  Speak now — {seconds} seconds, in your mother tongue...\n")
    for i in (3, 2, 1):
        print(f"  {i}...")
        time.sleep(0.7)
    print("  ● recording")
    audio = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32")
    sd.wait()
    print("  ■ done")
    sf.write(out_path, audio[:, 0], sr)
    return out_path


# --------------------------------------------------------------------------
# 2. PROSODY ANALYSIS
# --------------------------------------------------------------------------
def analyse(path):
    """Extract frame-level prosody features and syllable events."""
    import librosa
    from scipy.signal import find_peaks, medfilt

    y, sr = librosa.load(path, sr=SR, mono=True)
    y, _ = librosa.effects.trim(y, top_db=35)          # drop leading/trailing silence
    if len(y) < sr * 0.5:
        raise ValueError("Recording is too short or silent.")
    y = y / (np.max(np.abs(y)) + 1e-9)

    # --- frame features -------------------------------------------------
    f0, voiced, _ = librosa.pyin(y, fmin=65, fmax=600, sr=sr,
                                 frame_length=2048, hop_length=HOP)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=HOP)[0]
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=1024, hop_length=HOP)[0]

    n = min(len(f0), len(rms), len(cent))
    f0, voiced, rms, cent = f0[:n], voiced[:n], rms[:n], cent[:n]
    times = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=HOP)
    duration = len(y) / sr

    # energy normalised 0..1 (robust to outliers)
    e_hi = np.percentile(rms, 98) + 1e-9
    energy = np.clip(rms / e_hi, 0, 1)

    # brightness normalised 0..1 (log scale, 500 Hz .. 6 kHz)
    bright = np.clip((np.log2(cent + 1) - np.log2(500)) / (np.log2(6000) - np.log2(500)), 0, 1)

    # pitch in MIDI, cleaned
    midi = np.full(n, np.nan)
    ok = voiced & ~np.isnan(f0)
    midi[ok] = librosa.hz_to_midi(f0[ok])
    if ok.sum() < 5:
        raise ValueError("No voiced speech detected — try speaking closer to the mic.")

    # median-filter voiced pitch to kill octave jumps
    vm = midi[ok]
    midi[ok] = medfilt(vm, kernel_size=5) if len(vm) >= 5 else vm

    m0 = float(np.median(midi[ok]))                     # speaker's centre pitch
    lo, hi = np.percentile(midi[ok], [5, 95])
    span = max(hi - lo, 4.0)

    # continuous contour for visuals: interpolate across unvoiced gaps
    idx = np.arange(n)
    midi_interp = np.interp(idx, idx[ok], midi[ok])
    pitch_norm = np.clip((midi_interp - (lo - span * 0.15)) / (span * 1.3), 0, 1)

    # --- syllables = peaks of smoothed energy ---------------------------
    smooth = np.convolve(energy, np.hanning(7) / np.hanning(7).sum(), mode="same")
    min_gap = int(0.09 * sr / HOP)                      # ≥ 90 ms between syllables
    peaks, _ = find_peaks(smooth, distance=min_gap, prominence=0.08, height=0.12)

    # --- speaker's own mode: the musical mode that best fits the voice ---
    root = int(round(m0))
    rel = np.round(midi[ok] - root).astype(int) % 12
    weights = np.bincount(rel, weights=energy[ok], minlength=12)
    weights = weights / (weights.sum() + 1e-9)
    mode_name, mode = max(MODES.items(),
                          key=lambda kv: sum(weights[pc] for pc in kv[1]) / len(kv[1]) ** 0.35)

    # --- build musical events --------------------------------------------
    events = []
    bounds = [0] + [(peaks[i] + peaks[i + 1]) // 2 for i in range(len(peaks) - 1)] + [n]
    for i, p in enumerate(peaks):
        a, b = bounds[i], bounds[i + 1]
        seg_ok = ok[a:b]
        dur = float(times[min(b, n - 1)] - times[p])
        dur = float(np.clip(dur, 0.08, 1.2))
        amp = float(np.clip(0.06 + 0.3 * energy[p], 0.06, 0.36))
        br = float(bright[a:b].mean())
        if seg_ok.mean() > 0.3:
            m = float(np.median(midi[a:b][seg_ok]))
            events.append(dict(t=float(times[p]), kind="note", raw_midi=m,
                               amp=amp, dur=dur, bright=br, energy=float(energy[p])))
        else:
            events.append(dict(t=float(times[p]), kind="noise", amp=amp * 0.6,
                               center=float(np.mean(cent[a:b])), dur=dur,
                               bright=br, energy=float(energy[p])))

    return dict(path=os.path.abspath(path), duration=duration, times=times,
                pitch=pitch_norm, energy=energy, bright=bright,
                voiced=ok.astype(float), m0=m0, root=root, mode=mode,
                mode_name=mode_name,
                events=events)


# --------------------------------------------------------------------------
# 3. MAPPING SPEECH -> MUSIC
# --------------------------------------------------------------------------
def quantise(m, root, mode):
    """Snap a MIDI value to the nearest note of the speaker's own mode."""
    best, bd = None, 1e9
    base = root + 12 * np.floor((m - root) / 12)
    for octv in (-12, 0, 12):
        for pc in mode:
            c = base + octv + pc
            if abs(c - m) < bd:
                best, bd = c, abs(c - m)
    return int(best)


def to_music(a, exaggerate=1.8, centre=67):
    """Turn speech pitches into notes: exaggerate intervals, move to a
    singing register, quantise to the speaker's mode."""
    shift = 12 * round((centre - a["m0"]) / 12)
    root_out = a["root"] + shift
    for ev in a["events"]:
        if ev["kind"] == "note":
            m = root_out + (ev["raw_midi"] - a["m0"]) * exaggerate
            ev["midi"] = quantise(m, root_out, a["mode"])
    a["root_out"] = root_out
    return a


# --------------------------------------------------------------------------
# 4. OSC STREAMING
# --------------------------------------------------------------------------
class Outputs:
    def __init__(self, host, sc_port, p5_port, enabled=True):
        self.enabled = enabled
        if enabled:
            from pythonosc.udp_client import SimpleUDPClient
            self.sc = SimpleUDPClient(host, sc_port)
            self.p5 = SimpleUDPClient(host, p5_port)

    def send(self, target, addr, args):
        if not self.enabled:
            return
        if target in ("sc", "both"):
            self.sc.send_message(addr, args)
        if target in ("p5", "both"):
            self.p5.send_message(addr, args)


def build_schedule(a, direction, label, stretch):
    """Create a time-sorted list of (time, target, address, args)."""
    T = a["duration"] * stretch
    sched = [(0.0, "both", "/ll/start",
              [a["path"], float(T), int(a["root_out"]), int(direction), str(label)])]

    flip = -1.0 if direction == 1 else 1.0
    for i, t in enumerate(a["times"]):
        sched.append((float(t * stretch), "p5", "/ll/frame",
                      [float(t / a["duration"]), float(a["pitch"][i]),
                       float(a["energy"][i]), float(a["bright"][i]),
                       float(a["voiced"][i])]))

    for ev in a["events"]:
        ts = ev["t"] * stretch
        pan = flip * float(np.interp(ev["t"] / a["duration"], [0, 1], [-0.7, 0.7]))
        if ev["kind"] == "note":
            sched.append((ts, "sc", "/ll/note",
                          [float(ev["midi"]), ev["amp"], ev["dur"] * stretch,
                           ev["bright"], pan]))
            sched.append((ts, "p5", "/ll/onset", [ev["energy"], 1.0]))
        else:
            sched.append((ts, "sc", "/ll/noise",
                          [ev["amp"], ev["center"], ev["dur"] * stretch, pan]))
            sched.append((ts, "p5", "/ll/onset", [ev["energy"], 0.0]))

    sched.append((T + 0.3, "both", "/ll/end", []))
    sched.sort(key=lambda x: x[0])
    return sched, T


def play(sched, out, verbose=False):
    t0 = time.perf_counter()
    for t, target, addr, args in sched:
        wait = t - (time.perf_counter() - t0)
        if wait > 0:
            time.sleep(wait)
        out.send(target, addr, args)
        if verbose and addr != "/ll/frame":
            print(f"  {t:6.2f}s  {addr:10s} {args}")


# --------------------------------------------------------------------------
# 5. ARCHIVE
# --------------------------------------------------------------------------
def save_to_archive(wav_src, a, label, direction):
    import shutil
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(ARCHIVE_DIR, f"{stamp}_{label}")
    if os.path.abspath(wav_src) != os.path.abspath(base + ".wav"):
        shutil.copy(wav_src, base + ".wav")
    meta = dict(label=label, direction=direction, duration=a["duration"],
                root_midi=a["root_out"], mode=a["mode"], mode_name=a["mode_name"],
                notes=[e for e in a["events"]])
    with open(base + ".json", "w") as f:
        json.dump(meta, f, indent=1)
    return base + ".wav"


def describe(a, label):
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    n_notes = sum(e["kind"] == "note" for e in a["events"])
    n_noise = len(a["events"]) - n_notes
    print(f"\n  [{label}]  {a['duration']:.1f}s  ·  {len(a['events'])} syllables "
          f"({n_notes} tones, {n_noise} breaths)")
    print(f"  your voice sings in {names[a['root'] % 12]} {a['mode_name']}  "
          f"({' '.join(names[(a['root'] + p) % 12] for p in a['mode'])})")
    print("  melody: " + " ".join(str(e.get("midi", "~")) for e in a["events"]))


def plot(a, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(3, 1, figsize=(10, 6), sharex=True)
    ax[0].plot(a["times"], a["pitch"]); ax[0].set_ylabel("pitch (norm)")
    ax[1].plot(a["times"], a["energy"]); ax[1].set_ylabel("energy")
    for e in a["events"]:
        ax[1].axvline(e["t"], color="r" if e["kind"] == "note" else "g", alpha=.4)
    ax[2].plot(a["times"], a["bright"]); ax[2].set_ylabel("brightness")
    ax[2].set_xlabel("time (s)")
    fig.tight_layout(); fig.savefig(out_png, dpi=110)
    print(f"  plot saved: {out_png}")


# --------------------------------------------------------------------------
def run_one(path, args, archive=True):
    a = to_music(analyse(path), exaggerate=args.exaggerate)
    describe(a, args.lang)
    if args.plot:
        plot(a, os.path.splitext(path)[0] + "_analysis.png")
    if archive and not args.no_archive:
        a["path"] = os.path.abspath(save_to_archive(path, a, args.lang, args.dir))
    sched, T = build_schedule(a, DIRECTIONS[args.dir], args.lang, args.stretch)
    out = Outputs(args.host, args.sc_port, args.p5_port, enabled=not args.no_osc)
    if not args.no_osc:
        print(f"  ▶ performing ({T:.1f}s)...")
        play(sched, out, verbose=args.verbose)
    return sched


def main():
    p = argparse.ArgumentParser(description="Lost Languages — speech prosody to music & calligraphy")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record from the microphone")
    r.add_argument("--seconds", type=float, default=8)
    f = sub.add_parser("file", help="use an existing audio file")
    f.add_argument("path")
    sub.add_parser("archive", help="replay every saved voice, one after another")

    for sp in (r, f, sub.choices["archive"]):
        sp.add_argument("--lang", default="unknown", help="language label, e.g. fa, it, zh")
        sp.add_argument("--dir", choices=DIRECTIONS.keys(), default="ltr",
                        help="writing direction of the calligraphy")
        sp.add_argument("--stretch", type=float, default=1.4,
                        help="time stretch (>1 = slower, more musical)")
        sp.add_argument("--exaggerate", type=float, default=1.8,
                        help="how much to widen speech intervals")
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--sc-port", type=int, default=57120)
        sp.add_argument("--p5-port", type=int, default=12000)
        sp.add_argument("--no-osc", action="store_true", help="analyse only")
        sp.add_argument("--no-archive", action="store_true")
        sp.add_argument("--plot", action="store_true", help="save an analysis plot")
        sp.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    try:
        if args.cmd == "record":
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            tmp = os.path.join(ARCHIVE_DIR, "_last_take.wav")
            record(args.seconds, tmp)
            run_one(tmp, args)
        elif args.cmd == "file":
            run_one(args.path, args)
        elif args.cmd == "archive":
            wavs = sorted(w for w in os.listdir(ARCHIVE_DIR)
                          if w.endswith(".wav") and not w.startswith("_"))
            if not wavs:
                sys.exit("Archive is empty — record some voices first.")
            for w in wavs:
                meta_path = os.path.join(ARCHIVE_DIR, w[:-4] + ".json")
                if os.path.exists(meta_path):
                    meta = json.load(open(meta_path))
                    args.lang, args.dir = meta["label"], meta["direction"]
                run_one(os.path.join(ARCHIVE_DIR, w), args, archive=False)
                time.sleep(2.0)
    except ValueError as e:
        sys.exit(f"  ✗ {e}")


if __name__ == "__main__":
    main()
