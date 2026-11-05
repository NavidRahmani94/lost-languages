#!/usr/bin/env python3
"""
LOST LANGUAGES — desktop app
============================
One window. Everything else (prosody analysis, the SuperCollider sound engine,
the calligraphy renderer) runs hidden inside it.

    python app/lost_languages_app.py

If SuperCollider is installed it is started silently in the background and used
as the sound engine; otherwise a built-in Python synthesiser takes over.
"""

import glob
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))
import lost_languages as engine  # noqa: E402  (analysis + scheduling)
sys.path.insert(0, HERE)
from input_panel import InputMixerPanel  # noqa: E402

ARCHIVE = os.path.join(ROOT, "archive")
SCD = os.path.join(ROOT, "supercollider", "lost_languages.scd")
REPLY_PORT = 57300
SR_REC = 44100
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
DIR_KEYS = ["ltr", "rtl", "vert"]

PAPER = QtGui.QColor(242, 236, 224)
UI_BG = "#16151a"
UI_PANEL = "#1f1d24"
UI_LINE = "#2e2b35"
UI_TEXT = "#e9e4da"
UI_MUTED = "#8d8796"
ACCENT = "#d9534f"
INK = "#3b2a4a"


def note_name(m):
    m = int(round(m))
    return f"{NOTE_NAMES[m % 12]}{m // 12 - 1}"


def safe_label(s):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s.strip()) or "voice"
    return s[:40]


# =============================================================================
#  SOUND ENGINES
# =============================================================================
class SuperColliderEngine(QtCore.QObject):
    """Starts sclang + lost_languages.scd hidden in the background."""
    status = QtCore.Signal(str, bool)          # message, is_supercollider_ready

    def __init__(self):
        super().__init__()
        self.proc = None
        self.port = None
        self.client = None
        self.ready = False
        self._osc_server = None
        self._log = None

    @staticmethod
    def find_sclang():
        found = shutil.which("sclang")
        if found:
            return found
        bases = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.expanduser("~")]
        for b in bases:
            hits = sorted(glob.glob(os.path.join(b, "SuperCollider*", "sclang.exe")), reverse=True)
            if hits:
                return hits[0]
        mac = "/Applications/SuperCollider.app/Contents/MacOS/sclang"
        return mac if os.path.exists(mac) else None

    def start(self):
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer

        exe = self.find_sclang()
        if not exe:
            self.status.emit("SuperCollider not found — using built-in synth", False)
            return
        try:
            d = Dispatcher()
            d.map("/ll/ready", self._on_ready)
            # port 0 = let the OS pick a free port (Windows reserves some ranges)
            self._osc_server = ThreadingOSCUDPServer(("127.0.0.1", 0), d)
            reply_port = self._osc_server.server_address[1]
            threading.Thread(target=self._osc_server.serve_forever, daemon=True).start()
        except OSError as e:
            self.status.emit(f"Network error ({e}) — using built-in synth", False)
            return
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.bind(("127.0.0.1", 0))
            server_port = so.getsockname()[1]
        env = dict(os.environ, LL_REPLY_PORT=str(reply_port), LL_SERVER_PORT=str(server_port))

        self.status.emit("Starting SuperCollider…", False)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._log = open(os.path.join(ROOT, "sclang_log.txt"), "w", encoding="utf-8", errors="replace")
        try:
            self.proc = subprocess.Popen([exe, SCD], cwd=os.path.dirname(exe),
                                         stdin=subprocess.DEVNULL, stdout=self._log,
                                         stderr=subprocess.STDOUT, creationflags=flags, env=env)
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"Could not start SuperCollider ({e}) — built-in synth", False)
            return
        QtCore.QTimer.singleShot(40000, self._timeout)

    def _on_ready(self, addr, port):
        from pythonosc.udp_client import SimpleUDPClient
        self.port = int(port)
        self.client = SimpleUDPClient("127.0.0.1", self.port)
        self.ready = True
        self.status.emit(f"SuperCollider ready", True)

    def _timeout(self):
        if not self.ready:
            self.status.emit("SuperCollider did not answer — using built-in synth", False)

    def send(self, addr, args):
        if self.ready:
            self.client.send_message(addr, args)

    def stop(self):
        if self.ready:
            try:
                self.client.send_message("/ll/quit", [])
            except Exception:  # noqa: BLE001
                pass
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        # scsynth may outlive sclang on Windows
        if sys.platform == "win32" and self.ready:
            subprocess.call(["taskkill", "/F", "/IM", "scsynth.exe"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if self._osc_server:
            self._osc_server.shutdown()


def render_builtin(sched, wav_path, sr=44100, mix=None):
    """Built-in synth: renders the whole performance offline (same design as the
    SuperCollider engine) so it can be played with sounddevice."""
    from scipy.signal import fftconvolve
    import soundfile as sf

    mixd = dict(mix or {})

    def lv(k, d=1.0):
        return float(mixd.get(k, d))
    T = sched[0][3][1]
    root = sched[0][3][2]
    n = int((T + 4) * sr)
    out = np.zeros((n, 2))

    def add(sig, t0, pan):
        i = int(t0 * sr)
        j = min(n, i + len(sig))
        if j <= i:
            return
        l, r = math.cos((pan + 1) * math.pi / 4), math.sin((pan + 1) * math.pi / 4)
        out[i:j, 0] += sig[: j - i] * l
        out[i:j, 1] += sig[: j - i] * r

    rng = np.random.default_rng()
    for t, target, addr, args in sched:
        if addr == "/ll/note":
            midi, amp, dur, bright, pan = args
            f = 440 * 2 ** ((midi - 69) / 12)
            L = dur * 2.2 + 0.05
            tt = np.arange(int(L * sr)) / sr
            env = np.minimum(tt / 0.008, 1) * np.exp(-tt * 4 / L * 1.2)
            menv = np.exp(-tt / (dur * 0.4 + 1e-3))
            mod = np.sin(2 * np.pi * f * 2 * tt) * (0.3 + bright * 2.5) * menv
            sig = np.sin(2 * np.pi * f * tt + mod) * 0.7 + np.sin(np.pi * f * tt) * 0.25
            add(sig * env * amp * lv('tone'), t, pan)
        elif addr == "/ll/noise":
            amp, cf, dur, pan = args
            m = int(dur * sr)
            noise = rng.normal(0, 1, m)
            spec = np.fft.rfft(noise)
            fr = np.fft.rfftfreq(m, 1 / sr)
            spec *= np.exp(-((np.log2(fr + 1) - np.log2(max(cf, 400))) ** 2) / 0.3)
            sig = np.fft.irfft(spec, m)
            sig /= np.abs(sig).max() + 1e-9
            add(sig * np.hanning(m) * amp * lv('noise'), t, pan)

    # drone
    f0 = 440 * 2 ** ((root - 24 - 69) / 12)
    tt = np.arange(int((T + 3) * sr)) / sr
    env = np.clip(tt / 2.5, 0, 1) * np.clip((T + 3 - tt) / 3, 0, 1)
    drone = sum(a * np.sin(2 * np.pi * f0 * k * tt) for k, a in
                [(1, .4), (1.003, .4), (1.5, .2), (2, .15), (2.997, .08)])
    add(drone * env * 0.07 * lv('drone'), 0, 0)

    # granular voice
    try:
        voice, vsr = sf.read(sched[0][3][0], always_2d=True)
        voice = voice[:, 0]
        for _ in range(int(T * 28)):
            pos = rng.uniform(0, 1)
            when = pos * T
            g = int(rng.uniform(0.06, 0.18) * vsr)
            s0 = int(pos * max(1, len(voice) - g))
            grain = voice[s0:s0 + g] * np.hanning(min(g, len(voice[s0:s0 + g])))
            if vsr != sr:
                grain = np.interp(np.linspace(0, len(grain), int(len(grain) * sr / vsr)),
                                  np.arange(len(grain)), grain)
            add(grain * 0.18 * lv('grain'), when, rng.uniform(-0.8, 0.8))
    except Exception:  # noqa: BLE001
        pass

    # reverb
    ir_len = int(2.2 * sr)
    ir = rng.normal(0, 1, (ir_len, 2)) * np.exp(-np.arange(ir_len) / sr * 3.2)[:, None]
    wet = np.stack([fftconvolve(out[:, c], ir[:, c])[:n] for c in range(2)], 1)
    wet /= np.abs(wet).max() + 1e-9
    level = np.abs(out).max() + 1e-9
    wet *= level
    rv = lv('reverb', 0.38)
    mix = out * (1 - rv * 0.6) + wet * rv * 0.9
    mix *= 0.85 / max(level, np.abs(mix).max()) * lv('master')
    sf.write(wav_path, mix.astype(np.float32), sr)
    return mix.astype(np.float32), sr


# =============================================================================
#  CALLIGRAPHY CANVAS  (port of the Processing sketch)
# =============================================================================
class CalligraphyCanvas(QtWidgets.QWidget):
    W, H = 1600, 900

    def __init__(self):
        super().__init__()
        self.setMinimumSize(560, 315)
        self.page = QtGui.QImage(self.W, self.H, QtGui.QImage.Format_ARGB32_Premultiplied)
        self.margin, self.lineH, self.colW = 90, 135, 125
        self.direction = 1
        self.line = -1
        self.label = ""
        self.writing = False
        self.pen = QtCore.QPointF(0, 0)
        self.prev = None
        self.prevW = 0
        self.lastPitch = 0.5
        self.hueShift = 0
        self.frame = 0
        self.overlay = ""
        self.clear()

    # ---- page management ---------------------------------------------------
    def clear(self):
        self.page.fill(PAPER)
        p = QtGui.QPainter(self.page)
        p.setPen(Qt.NoPen)
        rnd = random.Random(7)
        for _ in range(14000):
            p.setBrush(QtGui.QColor(0, 0, 0, rnd.randint(3, 10)))
            r = rnd.uniform(0.5, 1.8)
            p.drawEllipse(QtCore.QPointF(rnd.uniform(0, self.W), rnd.uniform(0, self.H)), r, r)
        p.end()
        self.line = -1
        self.update()

    def fade(self):
        p = QtGui.QPainter(self.page)
        c = QtGui.QColor(PAPER)
        c.setAlpha(195)
        p.fillRect(0, 0, self.W, self.H, c)
        p.end()
        self.line = -1

    def max_lines(self):
        if self.direction == 2:
            return int((self.W - 2 * self.margin) / self.colW)
        return int((self.H - 2 * self.margin - 30) / self.lineH)

    def place(self, prog, pitch):
        off = (pitch - 0.5) * self.lineH * 0.9
        m = self.margin
        if self.direction == 0:
            return QtCore.QPointF(m + (self.W - 2 * m) * prog, m + (self.line + 0.5) * self.lineH - off)
        if self.direction == 1:
            return QtCore.QPointF(self.W - m - (self.W - 2 * m) * prog, m + (self.line + 0.5) * self.lineH - off)
        base = self.W - m - (self.line + 0.5) * self.colW
        return QtCore.QPointF(base + off * self.colW / self.lineH, m + (self.H - 2 * m - 30) * prog)

    # ---- events from the performer ----------------------------------------
    def start(self, direction, label):
        self.direction = direction
        self.label = label
        self.line += 1
        if self.line >= self.max_lines():
            self.fade()
            self.line = 0
        self.prev = None
        self.lastPitch = 0.5
        self.writing = True
        self.hueShift = random.uniform(-20, 20)
        self.seeds = [random.uniform(0, 100) for _ in range(16)]

    def end(self):
        self.writing = False
        self.prev = None
        p = QtGui.QPainter(self.page)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        f = QtGui.QFont("Segoe UI", 15)
        f.setItalic(True)
        p.setFont(f)
        p.setPen(QtGui.QColor(60, 50, 70, 150))
        pt = self.place(0, 0.5)
        fm = QtGui.QFontMetrics(f)
        w = fm.horizontalAdvance(self.label)
        if self.direction == 0:
            p.drawText(QtCore.QPointF(pt.x() - w - 16, pt.y() + 6), self.label)
        elif self.direction == 1:
            p.drawText(QtCore.QPointF(pt.x() + 16, pt.y() + 6), self.label)
        else:
            p.drawText(QtCore.QPointF(pt.x() - w / 2, pt.y() - 14), self.label)
        p.end()
        self.update()

    def frame_event(self, prog, pitch, energy, bright, voiced):
        self.frame += 1
        self.lastPitch = pitch if voiced > 0.5 else self.lastPitch + (0.5 - self.lastPitch) * 0.05
        pt = self.place(prog, self.lastPitch)
        self.pen = pt
        active = energy > 0.05
        w = 2 + energy ** 0.8 * 30
        hue = (225 + bright * 120 + self.hueShift) % 360
        p = QtGui.QPainter(self.page)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        if active and voiced > 0.5:
            if self.prev is not None:
                d = math.hypot(pt.x() - self.prev.x(), pt.y() - self.prev.y())
                if d < self.lineH * 0.25:
                    self._brush(p, self.prev, self.prevW, pt, w, hue, bright)
        elif active:
            self._spray(p, pt, energy, hue)
        p.end()
        self.prev = pt if active else None
        self.prevW = w

    def onset(self, energy, voiced):
        if voiced < 0.5 or energy < 0.35:
            return
        p = QtGui.QPainter(self.page)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        size = 2.5 + energy * 6
        for _ in range(3):
            cx = self.pen.x() + random.uniform(-size, size)
            cy = self.pen.y() + random.uniform(-size, size)
            col = QtGui.QColor.fromHsvF(((230 + self.hueShift) % 360) / 360, 0.5, 0.15, 0.45)
            p.setBrush(col)
            poly = QtGui.QPolygonF()
            ph = random.uniform(0, 6.28)
            for k in range(13):
                a = k / 13 * 2 * math.pi
                r = size * (0.6 + 0.4 * math.sin(a * 3 + ph) * 0.5 + random.uniform(0, 0.3))
                poly.append(QtCore.QPointF(cx + math.cos(a) * r, cy + math.sin(a) * r))
            p.drawPolygon(poly)
        p.end()

    def _brush(self, p, a, w1, b, w2, hue, bright):
        nx, ny = math.cos(math.pi / 4), math.sin(math.pi / 4)
        n = 14
        for i in range(n):
            f = i / (n - 1) - 0.5
            j1 = 0.17 * math.sin(self.seeds[i] + self.frame * 0.05)
            j2 = 0.17 * math.sin(self.seeds[i] + (self.frame + 1) * 0.05)
            alpha = (55 + 35 * (1 - abs(f) * 2)) / 100
            col = QtGui.QColor.fromHsvF(hue / 360, 0.35 + bright * 0.3, 0.18 + bright * 0.12, alpha)
            pen = QtGui.QPen(col, random.uniform(1.0, 2.6))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(a.x() + nx * (f + j1) * w1, a.y() + ny * (f + j1) * w1),
                       QtCore.QPointF(b.x() + nx * (f + j2) * w2, b.y() + ny * (f + j2) * w2))
        if random.random() < 0.015 * w2 / 10:
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor.fromHsvF(hue / 360, 0.45, 0.25, 0.1))
            r = w2 * random.uniform(0.2, 0.45)
            p.drawEllipse(QtCore.QPointF(b.x() + random.uniform(-w2, w2) * 0.5,
                                         b.y() + random.uniform(-w2, w2) * 0.5), r, r)

    def _spray(self, p, pt, energy, hue):
        p.setPen(Qt.NoPen)
        for _ in range(int(4 + energy * 30)):
            a = random.uniform(0, 2 * math.pi)
            r = random.uniform(2, 6 + energy * 24)
            p.setBrush(QtGui.QColor.fromHsvF(hue / 360, 0.3, 0.25, random.uniform(0.15, 0.45)))
            s = random.uniform(0.7, 2.4)
            p.drawEllipse(QtCore.QPointF(pt.x() + math.cos(a) * r, pt.y() + math.sin(a) * r), s, s)

    # ---- painting --------------------------------------------------------
    def target_rect(self):
        r = self.rect().adjusted(0, 0, 0, 0)
        scale = min(r.width() / self.W, r.height() / self.H)
        w, h = self.W * scale, self.H * scale
        return QtCore.QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h), scale

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        tr, scale = self.target_rect()
        path = QtGui.QPainterPath()
        path.addRoundedRect(tr, 10, 10)
        p.setClipPath(path)
        p.drawImage(tr, self.page)
        if self.writing:
            x = tr.x() + self.pen.x() * scale
            y = tr.y() + self.pen.y() * scale
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(217, 83, 79, int(110 + 80 * math.sin(time.time() * 8))))
            p.drawEllipse(QtCore.QPointF(x, y), 5, 5)
        if self.overlay:
            p.fillRect(tr, QtGui.QColor(22, 21, 26, 120))
            f = QtGui.QFont("Segoe UI", max(18, int(64 * scale)))
            f.setWeight(QtGui.QFont.Weight.Light)
            p.setFont(f)
            p.setPen(QtGui.QColor(255, 255, 255, 235))
            p.drawText(tr, Qt.AlignCenter, self.overlay)
        p.setClipping(False)
        p.setPen(QtGui.QColor(90, 80, 100, 160))
        f = QtGui.QFont("Segoe UI", 9)
        f.setItalic(True)
        p.setFont(f)
        p.drawText(QtCore.QRectF(tr.x() + 14, tr.bottom() - 26, tr.width() - 28, 20),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   "every language has a music before it has a meaning")
        p.end()

    def save_png(self, path):
        self.page.save(path)


# =============================================================================
#  CONVERSION VIEW  (speech -> music, with a moving playhead)
# =============================================================================
class ConversionView(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(220)
        self.a = None
        self.T = 1
        self.stretch = 1
        self.play_t = None
        self.live_level = []

    def set_analysis(self, a, T, stretch):
        self.a, self.T, self.stretch = a, T, stretch
        self.play_t = None
        self.live_level = []
        self.update()

    def set_play(self, t):
        self.play_t = t
        self.update()

    def push_level(self, v):
        self.a = None
        self.live_level.append(v)
        self.live_level = self.live_level[-400:]
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect())
        p.fillRect(r, QtGui.QColor(UI_PANEL))
        lab_w = 92
        speech = QtCore.QRectF(r.x() + lab_w, r.y() + 14, r.width() - lab_w - 18, r.height() * 0.42)
        music = QtCore.QRectF(r.x() + lab_w, speech.bottom() + 18, r.width() - lab_w - 18,
                              r.bottom() - speech.bottom() - 32)

        small = QtGui.QFont("Segoe UI", 9)
        bold = QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.DemiBold)
        p.setFont(bold)
        p.setPen(QtGui.QColor(UI_TEXT))
        p.drawText(QtCore.QRectF(16, speech.y(), lab_w - 20, 20), Qt.AlignLeft, "SPEECH")
        p.drawText(QtCore.QRectF(16, music.y(), lab_w - 20, 20), Qt.AlignLeft, "MUSIC")
        p.setFont(small)
        p.setPen(QtGui.QColor(UI_MUTED))
        p.drawText(QtCore.QRectF(16, speech.y() + 20, lab_w - 20, 60), Qt.AlignLeft | Qt.TextWordWrap,
                   "pitch contour\nenergy\nsyllables")
        p.drawText(QtCore.QRectF(16, music.y() + 20, lab_w - 20, 60), Qt.AlignLeft | Qt.TextWordWrap,
                   "notes in the\nvoice's mode")

        for box in (speech, music):
            p.setPen(QtGui.QPen(QtGui.QColor(UI_LINE), 1))
            p.setBrush(QtGui.QColor(24, 22, 29))
            p.drawRoundedRect(box, 6, 6)

        if self.a is None:
            if self.live_level:                      # recording: live level trace
                pts = self.live_level
                path = QtGui.QPainterPath()
                mid = speech.center().y()
                for i, v in enumerate(pts):
                    x = speech.x() + speech.width() * i / 400
                    path.moveTo(x, mid - v * speech.height() * 0.45)
                    path.lineTo(x, mid + v * speech.height() * 0.45)
                p.setPen(QtGui.QPen(QtGui.QColor(ACCENT), 1.6))
                p.drawPath(path)
            p.setPen(QtGui.QColor(UI_MUTED))
            p.setFont(QtGui.QFont("Segoe UI", 10))
            p.drawText(music, Qt.AlignCenter, "Record a voice to see it turn into music")
            p.end()
            return

        a = self.a
        D = a["duration"]

        def tx(t, box):
            return box.x() + 8 + (box.width() - 16) * (t / D)

        # energy envelope
        t = a["times"]
        e = a["energy"]
        step = max(1, len(t) // 600)
        env = QtGui.QPainterPath()
        base = speech.bottom() - 6
        env.moveTo(tx(t[0], speech), base)
        for i in range(0, len(t), step):
            env.lineTo(tx(t[i], speech), base - e[i] * speech.height() * 0.55)
        env.lineTo(tx(t[-1], speech), base)
        env.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(120, 110, 140, 70))
        p.drawPath(env)

        # syllable markers
        for ev in a["events"]:
            x = tx(ev["t"], speech)
            p.setPen(QtGui.QPen(QtGui.QColor(217, 83, 79, 90) if ev["kind"] == "note"
                                else QtGui.QColor(120, 190, 160, 110), 1, Qt.DashLine))
            p.drawLine(QtCore.QPointF(x, speech.y() + 4), QtCore.QPointF(x, speech.bottom() - 4))

        # pitch contour (voiced parts only)
        v = a["voiced"]
        pc = a["pitch"]
        path = QtGui.QPainterPath()
        drawing = False
        for i in range(len(t)):
            y = speech.bottom() - 8 - pc[i] * (speech.height() - 16)
            if v[i] > 0.5:
                if not drawing:
                    path.moveTo(tx(t[i], speech), y)
                    drawing = True
                else:
                    path.lineTo(tx(t[i], speech), y)
            else:
                drawing = False
        p.setPen(QtGui.QPen(QtGui.QColor("#f0c27b"), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        # notes
        notes = [ev for ev in a["events"] if ev["kind"] == "note"]
        if notes:
            lo = min(ev["midi"] for ev in notes) - 2
            hi = max(ev["midi"] for ev in notes) + 2
            # mode guide lines
            for m in range(int(lo), int(hi) + 1):
                if (m - a["root_out"]) % 12 in a["mode"]:
                    y = music.bottom() - 6 - (m - lo) / (hi - lo) * (music.height() - 12)
                    p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 14), 1))
                    p.drawLine(QtCore.QPointF(music.x() + 4, y), QtCore.QPointF(music.right() - 4, y))
            p.setFont(QtGui.QFont("Segoe UI", 7))
            now = self.play_t / self.stretch if self.play_t is not None else -1
            for ev in notes:
                x = tx(ev["t"], music)
                w = max(8, (music.width() - 16) * ev["dur"] / D)
                y = music.bottom() - 6 - (ev["midi"] - lo) / (hi - lo) * (music.height() - 12)
                lit = ev["t"] <= now < ev["t"] + ev["dur"] * 1.6
                col = QtGui.QColor(ACCENT) if lit else QtGui.QColor(217, 83, 79, 150 if now < ev["t"] else 90)
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QtCore.QRectF(x, y - 4, w, 8), 4, 4)
                if lit or len(notes) < 24:
                    p.setPen(QtGui.QColor(255, 255, 255, 220 if lit else 110))
                    p.drawText(QtCore.QPointF(x, y - 7), note_name(ev["midi"]))
            for ev in a["events"]:
                if ev["kind"] == "noise":
                    x = tx(ev["t"], music)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QtGui.QColor(120, 190, 160, 120))
                    p.drawEllipse(QtCore.QPointF(x, music.bottom() - 8), 3, 3)

        # playhead
        if self.play_t is not None:
            tt = min(self.play_t / self.stretch, D)
            for box in (speech, music):
                x = tx(tt, box)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 200), 1.4))
                p.drawLine(QtCore.QPointF(x, box.y() + 2), QtCore.QPointF(x, box.bottom() - 2))
        p.end()


# =============================================================================
#  PERFORMER  (plays a schedule: canvas + sound + playhead)
# =============================================================================
class Performer(QtCore.QObject):
    finished = QtCore.Signal()

    def __init__(self, canvas, view, sc):
        super().__init__()
        self.get_mix = dict
        self.canvas, self.view, self.sc = canvas, view, sc
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self._tick)
        self.sched = []
        self.i = 0

    def play(self, sched, use_builtin, a):
        self.sched, self.i = sched, 0
        if use_builtin:
            try:
                import sounddevice as sd
                audio, sr = render_builtin(sched, os.path.join(ARCHIVE, "_render.wav"), mix=self.get_mix())
                sd.play(audio, sr)
            except Exception as e:  # noqa: BLE001
                print("built-in synth failed:", e)
        self.clock = QtCore.QElapsedTimer()
        self.clock.start()
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def _tick(self):
        now = self.clock.elapsed() / 1000
        drew = False
        while self.i < len(self.sched) and self.sched[self.i][0] <= now:
            t, target, addr, args = self.sched[self.i]
            self.i += 1
            if target in ("sc", "both"):
                self.sc.send(addr, args)
            if addr == "/ll/start":
                self.canvas.start(args[3], args[4])
            elif addr == "/ll/frame":
                self.canvas.frame_event(*args)
                drew = True
            elif addr == "/ll/onset":
                self.canvas.onset(*args)
            elif addr == "/ll/end":
                self.canvas.end()
                self.timer.stop()
                self.view.set_play(None)
                self.finished.emit()
                return
        if drew or self.canvas.writing:
            self.canvas.update()
        self.view.set_play(now)


# =============================================================================
#  MAIN WINDOW
# =============================================================================
STYLE = f"""
QWidget {{ background: {UI_BG}; color: {UI_TEXT}; font-family: 'Segoe UI', 'Helvetica Neue', sans-serif; font-size: 10pt; }}
#side {{ background: {UI_PANEL}; border-right: 1px solid {UI_LINE}; }}
QLabel#title {{ font-size: 21pt; font-weight: 300; letter-spacing: 1px; background: transparent; }}
QLabel#subtitle {{ color: {UI_MUTED}; font-size: 9pt; font-style: italic; background: transparent; }}
QLabel#section {{ color: {UI_MUTED}; font-size: 8pt; font-weight: 600; letter-spacing: 1.5px; background: transparent; }}
QLabel {{ background: transparent; }}
QLineEdit {{ background: #2a2731; border: 1px solid {UI_LINE}; border-radius: 6px; padding: 7px 9px; font-size: 11pt; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QPushButton {{ background: #2a2731; border: 1px solid {UI_LINE}; border-radius: 6px; padding: 7px 10px; }}
QPushButton:hover {{ background: #34303c; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton:disabled {{ color: #5c5765; }}
QPushButton#record {{ background: {ACCENT}; border: none; border-radius: 28px; font-size: 12pt; font-weight: 600; color: white; padding: 0; }}
QPushButton#record:hover {{ background: #e4645f; }}
QPushButton#record:disabled {{ background: #4a3536; color: #a08c8c; }}
QSlider::groove:horizontal {{ height: 4px; background: {UI_LINE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QListWidget {{ background: #1a1820; border: 1px solid {UI_LINE}; border-radius: 6px; padding: 4px; }}
QListWidget::item {{ padding: 5px; border-radius: 4px; }}
QListWidget::item:selected {{ background: #3a2f3a; }}
QProgressBar {{ background: #2a2731; border: none; border-radius: 3px; height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
#info {{ color: {UI_TEXT}; font-size: 11pt; background: transparent; }}
#card {{ background: {UI_PANEL}; border: 1px solid {UI_LINE}; border-radius: 10px; }}
#card QLabel {{ background: transparent; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {UI_MUTED}; padding: 8px 18px; margin-right: 6px;
               border-bottom: 2px solid transparent; font-size: 10.5pt; }}
QTabBar::tab:selected {{ color: {UI_TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {UI_TEXT}; }}
QComboBox {{ background: #2a2731; border: 1px solid {UI_LINE}; border-radius: 6px; padding: 6px 9px; }}
QComboBox QAbstractItemView {{ background: #2a2731; selection-background-color: #3a2f3a; }}
QSlider::groove:vertical {{ width: 4px; background: {UI_LINE}; border-radius: 2px; }}
QSlider::add-page:vertical {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:vertical {{ background: white; height: 14px; width: 22px; margin: 0 -9px; border-radius: 7px; }}
QPushButton#testbtn:checked {{ background: {ACCENT}; }}
"""


class MainWindow(QtWidgets.QMainWindow):
    analysed = QtCore.Signal(object, object)      # analysis dict or None, error text
    level = QtCore.Signal(float)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lost Languages")
        self.resize(1440, 900)
        os.makedirs(ARCHIVE, exist_ok=True)

        self.sc = SuperColliderEngine()
        self.canvas = CalligraphyCanvas()
        self.view = ConversionView()
        self.performer = Performer(self.canvas, self.view, self.sc)
        self.inputs = InputMixerPanel(os.path.join(ROOT, "settings.json"))
        self.inputs.mixChanged.connect(lambda k, v: self.sc.send("/ll/mix", [k, float(v)]))
        self.performer.get_mix = self.inputs.mix
        self.performer.finished.connect(self._on_performance_end)
        self.queue = []
        self.state = "idle"
        self.direction = 1

        self._build_ui()
        self.sc.status.connect(self._on_engine)
        self.sc.status.connect(lambda _t, ok: ok and [self.sc.send("/ll/mix", [k, float(v)])
                                                       for k, v in self.inputs.mix().items()])
        self.analysed.connect(self._on_analysed)
        self.level.connect(self._on_level)
        self.sc.start()
        threading.Thread(target=self._warm_up, daemon=True).start()
        self._refresh_archive()

    # ---- UI -----------------------------------------------------------------
    def _build_ui(self):
        root = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QtWidgets.QWidget(objectName="side")
        side.setFixedWidth(310)
        s = QtWidgets.QVBoxLayout(side)
        s.setContentsMargins(22, 22, 22, 18)
        s.setSpacing(8)

        s.addWidget(QtWidgets.QLabel("Lost Languages", objectName="title"))
        s.addWidget(QtWidgets.QLabel("every language has a music\nbefore it has a meaning", objectName="subtitle"))
        s.addSpacing(14)

        s.addWidget(QtWidgets.QLabel("YOUR LANGUAGE", objectName="section"))
        self.lang = QtWidgets.QLineEdit()
        self.lang.setPlaceholderText("e.g.  فارسی · italiano · 中文")
        s.addWidget(self.lang)
        s.addSpacing(6)

        s.addWidget(QtWidgets.QLabel("WRITING DIRECTION", objectName="section"))
        row = QtWidgets.QHBoxLayout()
        self.dir_btns = []
        for i, txt in enumerate(["→  LTR", "←  RTL", "↓  Vertical"]):
            b = QtWidgets.QPushButton(txt, checkable=True)
            b.clicked.connect(lambda _=False, k=i: self._set_dir(k))
            row.addWidget(b)
            self.dir_btns.append(b)
        s.addLayout(row)
        self._set_dir(1)
        s.addSpacing(6)

        s.addWidget(QtWidgets.QLabel("RECORDING LENGTH", objectName="section"))
        self.secs, self.secs_lbl = self._slider(s, 3, 15, 8, lambda v: f"{v} s")
        s.addSpacing(10)

        rec_row = QtWidgets.QHBoxLayout()
        self.rec_btn = QtWidgets.QPushButton("●  Record", objectName="record")
        self.rec_btn.setFixedHeight(56)
        self.rec_btn.clicked.connect(self._record_clicked)
        rec_row.addWidget(self.rec_btn)
        s.addLayout(rec_row)
        self.meter = QtWidgets.QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(6)
        s.addWidget(self.meter)
        self.state_lbl = QtWidgets.QLabel("Ready")
        self.state_lbl.setStyleSheet(f"color:{UI_MUTED}")
        s.addWidget(self.state_lbl)
        self.file_btn = QtWidgets.QPushButton("Open an audio file…")
        self.file_btn.clicked.connect(self._open_file)
        s.addWidget(self.file_btn)
        s.addSpacing(10)

        s.addWidget(QtWidgets.QLabel("MELODY", objectName="section"))
        self.exag, _ = self._slider(s, 10, 35, 18, lambda v: f"{v / 10:.1f}× intervals")
        s.addWidget(QtWidgets.QLabel("TEMPO", objectName="section"))
        self.stretch, _ = self._slider(s, 8, 25, 14, lambda v: f"{v / 10:.1f}× slower")
        s.addSpacing(10)

        s.addWidget(QtWidgets.QLabel("ARCHIVE OF VOICES", objectName="section"))
        self.archive = QtWidgets.QListWidget()
        self.archive.itemDoubleClicked.connect(self._replay_item)
        self.archive.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.archive.setTextElideMode(Qt.ElideRight)
        self.archive.setToolTip("Double-click a voice to perform it again")
        s.addWidget(self.archive, 1)
        arow = QtWidgets.QHBoxLayout()
        self.play_all = QtWidgets.QPushButton("▶  Play all")
        self.play_all.clicked.connect(self._play_all)
        arow.addWidget(self.play_all)
        s.addLayout(arow)
        brow = QtWidgets.QHBoxLayout()
        clr = QtWidgets.QPushButton("Clear page")
        clr.clicked.connect(self.canvas.clear)
        brow.addWidget(clr)
        sv = QtWidgets.QPushButton("Save image")
        sv.clicked.connect(self._save_image)
        brow.addWidget(sv)
        s.addLayout(brow)

        eng = QtWidgets.QHBoxLayout()
        self.eng_dot = QtWidgets.QLabel("●")
        self.eng_lbl = QtWidgets.QLabel("Sound engine…")
        self.eng_lbl.setStyleSheet(f"color:{UI_MUTED}; font-size:9pt")
        self.eng_lbl.setWordWrap(True)
        eng.addWidget(self.eng_dot)
        eng.addWidget(self.eng_lbl, 1)
        s.addLayout(eng)
        self._on_engine("Starting sound engine…", False)

        main = QtWidgets.QWidget()
        m = QtWidgets.QVBoxLayout(main)
        m.setContentsMargins(22, 22, 22, 22)
        m.setSpacing(14)
        self.tabs = QtWidgets.QTabWidget()
        studio = QtWidgets.QWidget()
        st = QtWidgets.QVBoxLayout(studio)
        st.setContentsMargins(0, 12, 0, 0)
        st.setSpacing(14)
        st.addWidget(self.canvas, 3)
        self.info = QtWidgets.QLabel("Say something in your mother tongue — a memory, a lullaby, a name.",
                                     objectName="info")
        st.addWidget(self.info)
        st.addWidget(self.view, 1)
        self.tabs.addTab(studio, "✒  Studio")
        mixer = QtWidgets.QWidget()
        mx = QtWidgets.QVBoxLayout(mixer)
        mx.setContentsMargins(0, 12, 0, 0)
        mx.addWidget(self.inputs)
        self.tabs.addTab(mixer, "🎙  Input && Mixer")
        m.addWidget(self.tabs)

        lay.addWidget(side)
        lay.addWidget(main, 1)
        self.setCentralWidget(root)

    def _slider(self, layout, lo, hi, val, fmt):
        row = QtWidgets.QHBoxLayout()
        sl = QtWidgets.QSlider(Qt.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(val)
        lbl = QtWidgets.QLabel(fmt(val))
        lbl.setFixedWidth(100)
        lbl.setStyleSheet(f"color:{UI_MUTED}; font-size:9pt")
        sl.valueChanged.connect(lambda v: lbl.setText(fmt(v)))
        row.addWidget(sl, 1)
        row.addWidget(lbl)
        layout.addLayout(row)
        return sl, lbl

    def _set_dir(self, k):
        self.direction = k
        for i, b in enumerate(self.dir_btns):
            b.setChecked(i == k)

    def _set_state(self, state, text):
        self.state = state
        busy = state != "idle"
        self.state_lbl.setText(text)
        for w in (self.file_btn, self.play_all, self.archive, self.lang):
            w.setEnabled(not busy)
        self.rec_btn.setEnabled(state in ("idle", "recording"))
        self.rec_btn.setText("■  Stop" if state == "recording" else "●  Record")

    def _on_engine(self, text, ok):
        self.eng_lbl.setText(text)
        self.eng_dot.setStyleSheet(f"color:{'#5cb85c' if ok else '#e0a84f'}")

    # ---- background ---------------------------------------------------------
    def _warm_up(self):
        try:
            import librosa  # noqa: F401  (first import is slow: numba)
            y = np.sin(np.linspace(0, 2000, 22050)).astype(np.float32)
            librosa.pyin(y, fmin=80, fmax=400, sr=22050)
        except Exception:  # noqa: BLE001
            pass

    # ---- recording ----------------------------------------------------------
    def _record_clicked(self):
        if self.state == "recording":
            self._rec_stop = True
            return
        if self.state != "idle":
            return
        self._countdown(3)

    def _countdown(self, n):
        if n == 0:
            self.canvas.overlay = ""
            self.canvas.update()
            self._start_recording()
            return
        self._set_state("countdown", "Get ready…")
        self.canvas.overlay = str(n)
        self.canvas.update()
        QtCore.QTimer.singleShot(800, lambda: self._countdown(n - 1))

    def _start_recording(self):
        try:
            import sounddevice as sd
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Microphone", f"sounddevice is not available:\n{e}")
            self._set_state("idle", "Ready")
            return
        self.inputs.stop_test()
        dev, sr, nch, chmode, gain = self.inputs.record_params()
        self._rec_sr = sr
        self._chunks = []
        self._rec_stop = False
        secs = self.secs.value()
        self._set_state("recording", "● Recording — speak now")
        self.canvas.overlay = "speak…"
        self.canvas.update()
        self._rec_t0 = time.time()

        def cb(indata, frames, t, status):
            raw = InputMixerPanel.pick_channel(indata, chmode)
            x = np.clip(raw * gain, -1, 1)
            self._chunks.append(x.copy())
            self.inputs.feed_external(raw, gain)
            self.level.emit(float(np.sqrt(np.mean(x ** 2))))

        try:
            self._stream = sd.InputStream(device=dev, samplerate=sr, channels=nch, dtype="float32", callback=cb)
            self._stream.start()
        except Exception as e:  # noqa: BLE001
            self.canvas.overlay = ""
            QtWidgets.QMessageBox.warning(self, "Microphone", f"Could not open the microphone:\n{e}")
            self._set_state("idle", "Ready")
            return
        self._rec_timer = QtCore.QTimer(self)
        self._rec_timer.timeout.connect(lambda: self._rec_poll(secs))
        self._rec_timer.start(100)

    def _rec_poll(self, secs):
        el = time.time() - self._rec_t0
        self.state_lbl.setText(f"● Recording — {max(0, secs - el):.0f} s left")
        if el >= secs or self._rec_stop:
            self._rec_timer.stop()
            self._stream.stop()
            self._stream.close()
            self.canvas.overlay = ""
            self.canvas.update()
            self.meter.setValue(0)
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(1)
            if np.abs(audio).max() < 2e-3:
                self.tabs.setCurrentIndex(1)
                self.inputs.refresh_system()
                QtWidgets.QMessageBox.warning(
                    self, "No sound from the microphone",
                    "The recording is silent.\n\n"
                    "Open the “Input & Mixer” tab: it shows whether Windows has the\n"
                    "microphone muted, the permissions, and a live level meter.\n\n"
                    "• Check that the microphone is not muted.\n"
                    "• Windows: Settings → Privacy & security → Microphone →\n"
                    "  turn on “Let desktop apps access your microphone”.")
                self._set_state("idle", "Ready")
                return
            import soundfile as sf
            path = os.path.join(ARCHIVE, "_last_take.wav")
            sf.write(path, audio, self._rec_sr)
            self._analyse(path, self._label(), self.direction, archive=True)

    def _on_level(self, v):
        self.meter.setValue(int(min(1, v * 8) * 100))
        self.view.push_level(min(1.0, v * 6))

    def _label(self):
        return self.lang.text().strip() or "voice"

    # ---- analysis -----------------------------------------------------------
    def _open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose a recording", os.path.expanduser("~"),
                                                        "Audio (*.wav *.mp3 *.flac *.ogg *.m4a)")
        if path:
            self._analyse(path, self._label(), self.direction, archive=True)

    def _analyse(self, path, label, direction, archive):
        self._set_state("analysing", "Listening to the music of the voice…")
        self.info.setText("Analysing…")
        exag = self.exag.value() / 10

        def work():
            try:
                a = engine.to_music(engine.analyse(path), exaggerate=exag)
                if archive:
                    a["path"] = os.path.abspath(self._save_archive(path, a, label, direction))
                a["label"], a["direction"] = label, direction
                self.analysed.emit(a, None)
            except Exception as e:  # noqa: BLE001
                self.analysed.emit(None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _save_archive(self, wav, a, label, direction):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(ARCHIVE, f"{stamp}_{safe_label(label)}")
        if wav.lower().endswith(".wav"):
            shutil.copy(wav, base + ".wav")
        else:
            import soundfile as sf
            import librosa
            y, sr = librosa.load(wav, sr=SR_REC)
            sf.write(base + ".wav", y, sr)
        meta = dict(label=label, direction=DIR_KEYS[direction], duration=a["duration"],
                    root_midi=a["root_out"], mode=a["mode"], mode_name=a["mode_name"])
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        return base + ".wav"

    def _on_analysed(self, a, err):
        if err:
            QtWidgets.QMessageBox.warning(self, "Analysis", err)
            self._set_state("idle", "Ready")
            self.info.setText("Try again — speak a little closer to the microphone.")
            self.queue = []
            return
        self._refresh_archive()
        stretch = self.stretch.value() / 10
        sched, T = engine.build_schedule(a, a["direction"], a["label"], stretch)
        self.view.set_analysis(a, T, stretch)
        n_notes = sum(e["kind"] == "note" for e in a["events"])
        root = NOTE_NAMES[a["root_out"] % 12]
        self.info.setText(f"<b>{a['label']}</b> &nbsp;·&nbsp; {a['duration']:.1f} s &nbsp;·&nbsp; "
                          f"{len(a['events'])} syllables ({n_notes} tones) &nbsp;·&nbsp; "
                          f"this voice sings in <b style='color:{ACCENT}'>{root} {a['mode_name']}</b>")
        self._set_state("performing", "▶ Performing…")
        self.performer.play(sched, not self.sc.ready, a)

    def _on_performance_end(self):
        if self.queue:
            nxt = self.queue.pop(0)
            QtCore.QTimer.singleShot(1500, lambda: self._replay_path(nxt))
        else:
            self._set_state("idle", "Ready")

    # ---- archive ------------------------------------------------------------
    def _refresh_archive(self):
        self.archive.clear()
        for js in sorted(glob.glob(os.path.join(ARCHIVE, "*.json")), reverse=True):
            try:
                meta = json.load(open(js, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            stamp = os.path.basename(js)[:15]
            when = f"{stamp[6:8]}/{stamp[4:6]} {stamp[9:11]}:{stamp[11:13]}"
            mode = f"{NOTE_NAMES[meta.get('root_midi', 0) % 12]} {meta.get('mode_name', '')}"
            it = QtWidgets.QListWidgetItem(f"{meta.get('label', '?')}  ·  {mode}  ·  {when}")
            it.setData(Qt.UserRole, js[:-5] + ".wav")
            self.archive.addItem(it)

    def _replay_path(self, wav):
        js = wav[:-4] + ".json"
        label, direction = "voice", self.direction
        if os.path.exists(js):
            meta = json.load(open(js, encoding="utf-8"))
            label = meta.get("label", label)
            d = meta.get("direction", "rtl")
            direction = DIR_KEYS.index(d) if d in DIR_KEYS else int(d)
        self._analyse(wav, label, direction, archive=False)

    def _replay_item(self, item):
        if self.state == "idle":
            self._replay_path(item.data(Qt.UserRole))

    def _play_all(self):
        files = [self.archive.item(i).data(Qt.UserRole) for i in range(self.archive.count())][::-1]
        if not files:
            return
        self.canvas.clear()
        self.queue = files[1:]
        self._replay_path(files[0])

    def _save_image(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save calligraphy", os.path.join(os.path.expanduser("~"), "lost_languages.png"), "PNG (*.png)")
        if path:
            self.canvas.save_png(path)

    def closeEvent(self, e):
        self.performer.stop()
        self.sc.stop()
        super().closeEvent(e)


def main():
    # pythonw has no console: send every error to app_log.txt so problems are visible
    log = open(os.path.join(ROOT, "app_log.txt"), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    import faulthandler
    import traceback
    faulthandler.enable(log)
    sys.excepthook = lambda *e: traceback.print_exception(*e, file=log)
    print("starting", sys.executable, sys.version)
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    if sys.platform == "win32":   # own taskbar button + icon instead of Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LostLanguages.App")
        except Exception:  # noqa: BLE001
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Lost Languages")
    icon = os.path.join(ROOT, "assets", "icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QtGui.QIcon(icon))
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
