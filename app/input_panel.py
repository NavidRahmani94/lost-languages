"""
Input & Mixer section of the Lost Languages app.

* choose the microphone (every input of every Windows audio API) and see its specs
* live oscilloscope + dB meter with peak hold, and a plain-language diagnosis
* Windows status: microphone privacy permissions, mute state, input volume,
  every capture endpoint and whether it is active / disabled / unplugged
* output mixer for the performance (melody, breath, drone, voice grains, reverb, master)
"""
import json
import math
import os
import sys
import threading

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

ACCENT = "#d9534f"
GOOD = "#5cb85c"
WARN = "#e0a84f"
MUTED_TXT = "#8d8796"
PANEL = "#1f1d24"
LINE = "#2e2b35"

MIX_CHANNELS = [("tone", "Melody"), ("noise", "Breath"), ("drone", "Drone"),
                ("grain", "Voice"), ("reverb", "Reverb"), ("master", "Master")]
MIX_DEFAULT = {"tone": 1.0, "noise": 1.0, "drone": 1.0, "grain": 1.0, "reverb": 0.38, "master": 1.0}


def dbfs(x):
    return 20 * math.log10(max(float(x), 1e-9))


# =============================================================================
#  Windows system audio (Core Audio via pycaw) + privacy registry
# =============================================================================
class WinAudio:
    """Mute + volume of the Windows default microphone (Core Audio via pycaw).

    COM pointers are created once and kept alive for the whole session: letting
    Python garbage-collect them from another thread (e.g. the audio callback)
    crashes the process, and pycaw's GetAllDevices() is unstable on some systems,
    so the endpoint list comes from PowerShell instead (see endpoints()).
    """

    def __init__(self):
        self.ok = False
        self.err = ""
        self._vol = None
        self._keep = []
        self._eps = None
        self._eps_busy = False
        if sys.platform != "win32":
            self.err = "System mute/volume is only available on Windows."
            return
        try:
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            self._AU, self._IEV, self._CLS, self._cast, self._P = (
                AudioUtilities, IAudioEndpointVolume, CLSCTX_ALL, cast, POINTER)
            self.ok = True
        except Exception as e:  # noqa: BLE001
            self.err = f"Install 'pycaw' to see mute/volume ({e.__class__.__name__})."

    def _endpoint(self):
        if self._vol is None:
            dev = self._AU.GetMicrophone()
            if dev is None:
                return None
            iface = dev.Activate(self._IEV._iid_, self._CLS, None)
            self._vol = self._cast(iface, self._P(self._IEV))
            self._keep.append((dev, iface, self._vol))       # never released
        return self._vol

    def reset(self):
        """Re-acquire the default microphone (after the user changes devices)."""
        self._vol = None

    def status(self):
        vol = self._endpoint()
        if vol is None:
            return None
        try:
            import sounddevice as sd
            name = sd.query_devices(kind="input")["name"]
        except Exception:  # noqa: BLE001
            name = "Default microphone"
        return dict(name=name, muted=bool(vol.GetMute()), volume=float(vol.GetMasterVolumeLevelScalar()))

    def set_mute(self, m):
        vol = self._endpoint()
        if vol is not None:
            vol.SetMute(int(bool(m)), None)

    def set_volume(self, v):
        vol = self._endpoint()
        if vol is not None:
            vol.SetMasterVolumeLevelScalar(float(min(1, max(0, v))), None)

    def endpoints(self, refresh=False):
        """Capture endpoints and their state, read with PowerShell in a background thread.
        Returns the last known list (or None while the first query is running)."""
        if (self._eps is None or refresh) and not self._eps_busy and sys.platform == "win32":
            self._eps_busy = True
            threading.Thread(target=self._query_endpoints, daemon=True).start()
        return self._eps

    def _query_endpoints(self):
        import subprocess
        cmd = ("Get-PnpDevice -Class AudioEndpoint | Select-Object FriendlyName,Status,InstanceId "
               "| ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                               timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            rows = json.loads(r.stdout or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            out = []
            for d in rows:
                iid = (d.get("InstanceId") or "").upper()
                if "{0.0.1." not in iid:                     # capture endpoints only
                    continue
                st = d.get("Status") or ""
                state = {"OK": "Active", "Unknown": "NotPresent", "Error": "Disabled",
                         "Degraded": "Problem"}.get(st, st or "?")
                out.append((d.get("FriendlyName") or "?", state))
            self._eps = sorted(out, key=lambda x: x[1] != "Active")
        except Exception as e:  # noqa: BLE001
            self._eps = [(f"(could not list devices: {e.__class__.__name__})", "?")]
        self._eps_busy = False


def privacy_status():
    """Windows microphone privacy switches -> list of (label, value or None)."""
    if sys.platform != "win32":
        return []
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    rows = [("Microphone access (device)", winreg.HKEY_LOCAL_MACHINE, ""),
            ("Microphone access (account)", winreg.HKEY_CURRENT_USER, ""),
            ("Desktop apps", winreg.HKEY_CURRENT_USER, r"\NonPackaged")]
    # the Microsoft Store Python is a packaged app with its own switch
    exe = sys.executable.lower()
    if "windowsapps" in exe or "pythonsoftwarefoundation" in exe:
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
            i = 0
            while True:
                sub = winreg.EnumKey(k, i)
                i += 1
                if sub.lower().startswith("pythonsoftwarefoundation"):
                    rows.append((f"Python ({sub.split('_')[0].split('.')[-1]})", winreg.HKEY_CURRENT_USER, "\\" + sub))
        except OSError:
            pass
    out = []
    for label, hive, sub in rows:
        try:
            k = winreg.OpenKey(hive, base + sub)
            out.append((label, winreg.QueryValueEx(k, "Value")[0]))
        except OSError:
            out.append((label, None))
    return out


# =============================================================================
#  Widgets
# =============================================================================
class LevelMeter(QtWidgets.QWidget):
    """Horizontal dBFS meter (-60..0) with RMS bar and peak-hold line."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(54)
        self.rms_db = -90
        self.peak_db = -90
        self.hold_db = -90
        self.hold_t = 0

    def set_levels(self, rms_db, peak_db):
        self.rms_db, self.peak_db = rms_db, peak_db
        if peak_db >= self.hold_db or self.hold_t > 45:
            self.hold_db, self.hold_t = peak_db, 0
        else:
            self.hold_t += 1
            if self.hold_t > 45:
                self.hold_db -= 1.5
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(14, 2, -14, -20)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(24, 22, 29))
        p.drawRoundedRect(r, 5, 5)

        def x_of(d):
            return r.x() + r.width() * (min(0, max(-60, d)) + 60) / 60

        grad = QtGui.QLinearGradient(r.topLeft(), r.topRight())
        grad.setColorAt(0.0, QtGui.QColor("#3c8f5a"))
        grad.setColorAt(0.70, QtGui.QColor("#5cb85c"))
        grad.setColorAt(0.85, QtGui.QColor("#e0c84f"))
        grad.setColorAt(1.0, QtGui.QColor("#d9534f"))
        inner = r.adjusted(3, 3, -3, -3)
        # peak (thin, translucent) and rms (solid)
        p.setBrush(QtGui.QBrush(grad))
        p.setOpacity(0.35)
        p.drawRoundedRect(QtCore.QRectF(inner.x(), inner.y(), max(0, x_of(self.peak_db) - inner.x()), inner.height()), 3, 3)
        p.setOpacity(1)
        p.drawRoundedRect(QtCore.QRectF(inner.x(), inner.y() + inner.height() * 0.25,
                                        max(0, x_of(self.rms_db) - inner.x()), inner.height() * 0.5), 3, 3)
        # peak hold
        if self.hold_db > -60:
            p.setPen(QtGui.QPen(QtGui.QColor("white"), 2))
            x = x_of(self.hold_db)
            p.drawLine(QtCore.QPointF(x, inner.y()), QtCore.QPointF(x, inner.bottom()))
        # scale
        p.setPen(QtGui.QColor(MUTED_TXT))
        p.setFont(QtGui.QFont("Segoe UI", 7))
        for d in (-60, -48, -36, -24, -18, -12, -6, -3, 0):
            x = x_of(d)
            p.drawLine(QtCore.QPointF(x, r.bottom() + 2), QtCore.QPointF(x, r.bottom() + 6))
            p.drawText(QtCore.QRectF(x - 14, r.bottom() + 6, 28, 12), Qt.AlignCenter, str(d))
        p.end()


class Scope(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.data = np.zeros(2048, dtype=np.float32)

    def set_data(self, d):
        self.data = d
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1))
        p.setBrush(QtGui.QColor(24, 22, 29))
        p.drawRoundedRect(r, 6, 6)
        mid = r.center().y()
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 18), 1))
        for f in (-0.5, 0.5):
            p.drawLine(QtCore.QPointF(r.x(), mid + f * r.height() / 2), QtCore.QPointF(r.right(), mid + f * r.height() / 2))
        p.drawLine(QtCore.QPointF(r.x(), mid), QtCore.QPointF(r.right(), mid))
        d = self.data
        n = len(d)
        if n < 2:
            return
        step = max(1, n // int(r.width()))
        path = QtGui.QPainterPath()
        for i in range(0, n, step):
            x = r.x() + r.width() * i / n
            y = mid - float(np.clip(d[i], -1, 1)) * (r.height() / 2 - 4)
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        p.setPen(QtGui.QPen(QtGui.QColor("#f0c27b"), 1.4))
        p.drawPath(path)
        p.setPen(QtGui.QColor(MUTED_TXT))
        p.setFont(QtGui.QFont("Segoe UI", 8))
        p.drawText(r.adjusted(8, 4, -8, -4), Qt.AlignTop | Qt.AlignLeft, "input waveform")
        p.end()


def card(title):
    box = QtWidgets.QFrame()
    box.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(box)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    lbl = QtWidgets.QLabel(title, objectName="section")
    lay.addWidget(lbl)
    return box, lay


def badge(text, color):
    return (f"<span style='background:{color}; color:white; padding:2px 8px; border-radius:4px;'>"
            f"&nbsp;{text}&nbsp;</span>")


# =============================================================================
#  The panel
# =============================================================================
class InputMixerPanel(QtWidgets.QWidget):
    mixChanged = QtCore.Signal(str, float)

    def __init__(self, settings_path):
        super().__init__()
        self.settings_path = settings_path
        self.settings = self._load()
        self.win = WinAudio()
        self.stream = None
        self.lock = threading.Lock()
        self.ring = np.zeros(4096, dtype=np.float32)
        self.blk_rms = 0.0
        self.blk_peak = 0.0
        self.raw_peak_1s = 0.0
        self._peaks = []
        self.devices = []
        self._build()
        self.refresh_devices()
        self.refresh_system()
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self._ui_tick)
        self.ui_timer.start(33)
        self.sys_timer = QtCore.QTimer(self)
        self.sys_timer.timeout.connect(self.refresh_system)
        self.sys_timer.start(1500)

    # ---- settings -----------------------------------------------------------
    def _load(self):
        try:
            s = json.load(open(self.settings_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            s = {}
        s.setdefault("device", None)
        s.setdefault("channel", "mix")
        s.setdefault("gain_db", 0.0)
        s.setdefault("mix", dict(MIX_DEFAULT))
        for k, v in MIX_DEFAULT.items():
            s["mix"].setdefault(k, v)
        return s

    def save(self):
        try:
            json.dump(self.settings, open(self.settings_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass

    # ---- UI -----------------------------------------------------------------
    def _build(self):
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)

        # --- input device -----------------------------------------------------
        dev_card, d = card("INPUT DEVICE")
        row = QtWidgets.QHBoxLayout()
        self.dev_combo = QtWidgets.QComboBox()
        self.dev_combo.currentIndexChanged.connect(self._device_changed)
        row.addWidget(self.dev_combo, 1)
        rb = QtWidgets.QPushButton("↻")
        rb.setFixedWidth(36)
        rb.setToolTip("Rescan audio devices")
        rb.clicked.connect(self.refresh_devices)
        row.addWidget(rb)
        d.addLayout(row)
        self.specs = QtWidgets.QLabel()
        self.specs.setTextFormat(Qt.RichText)
        self.specs.setStyleSheet("font-size:9pt; line-height:150%")
        d.addWidget(self.specs)

        d.addWidget(QtWidgets.QLabel("CHANNEL", objectName="section"))
        chrow = QtWidgets.QHBoxLayout()
        self.ch_btns = {}
        for key, txt in (("mix", "L + R"), ("left", "Left"), ("right", "Right")):
            b = QtWidgets.QPushButton(txt, checkable=True)
            b.clicked.connect(lambda _=False, k=key: self._set_channel(k))
            chrow.addWidget(b)
            self.ch_btns[key] = b
        d.addLayout(chrow)
        self._set_channel(self.settings["channel"], restart=False)

        d.addWidget(QtWidgets.QLabel("INPUT GAIN (software)", objectName="section"))
        grow = QtWidgets.QHBoxLayout()
        self.gain = QtWidgets.QSlider(Qt.Horizontal)
        self.gain.setRange(-12, 36)
        self.gain.setValue(int(self.settings["gain_db"]))
        self.gain_lbl = QtWidgets.QLabel()
        self.gain_lbl.setFixedWidth(60)
        self.gain.valueChanged.connect(self._gain_changed)
        grow.addWidget(self.gain, 1)
        grow.addWidget(self.gain_lbl)
        d.addLayout(grow)
        self._gain_changed(self.gain.value())
        d.addStretch(1)
        self.test_btn = QtWidgets.QPushButton("▶  Test microphone", checkable=True)
        self.test_btn.setObjectName("testbtn")
        self.test_btn.setFixedHeight(40)
        self.test_btn.toggled.connect(self._toggle_test)
        d.addWidget(self.test_btn)

        # --- signal -------------------------------------------------------------
        sig_card, s = card("SIGNAL")
        self.scope = Scope()
        s.addWidget(self.scope, 1)
        self.meter = LevelMeter()
        s.addWidget(self.meter)
        nums = QtWidgets.QHBoxLayout()
        self.rms_lbl = QtWidgets.QLabel("RMS  — dBFS")
        self.peak_lbl = QtWidgets.QLabel("Peak  — dBFS")
        self.clip_lbl = QtWidgets.QLabel("")
        nums.setSpacing(28)
        for w in (self.rms_lbl, self.peak_lbl):
            w.setStyleSheet("font-size:11pt; font-family:Consolas, monospace")
            nums.addWidget(w)
        nums.addStretch(1)
        nums.addWidget(self.clip_lbl)
        s.addLayout(nums)
        self.diag = QtWidgets.QLabel("Press “Test microphone” to listen to the input.")
        self.diag.setWordWrap(True)
        self.diag.setTextFormat(Qt.RichText)
        self.diag.setStyleSheet("font-size:11pt; padding:6px 0")
        s.addWidget(self.diag)

        # --- windows ---------------------------------------------------------------
        win_card, w = card("WINDOWS MICROPHONE")
        self.sys_name = QtWidgets.QLabel("—")
        self.sys_name.setWordWrap(True)
        w.addWidget(self.sys_name)
        mrow = QtWidgets.QHBoxLayout()
        self.mute_badge = QtWidgets.QLabel()
        self.mute_badge.setTextFormat(Qt.RichText)
        mrow.addWidget(self.mute_badge)
        mrow.addStretch(1)
        self.mute_btn = QtWidgets.QPushButton("Unmute")
        self.mute_btn.clicked.connect(self._toggle_mute)
        mrow.addWidget(self.mute_btn)
        w.addLayout(mrow)
        vrow = QtWidgets.QHBoxLayout()
        vrow.addWidget(QtWidgets.QLabel("Volume"))
        self.sys_vol = QtWidgets.QSlider(Qt.Horizontal)
        self.sys_vol.setRange(0, 100)
        self.sys_vol.sliderReleased.connect(lambda: self._set_sys_vol(self.sys_vol.value()))
        self.sys_vol_lbl = QtWidgets.QLabel("—")
        self.sys_vol_lbl.setFixedWidth(40)
        vrow.addWidget(self.sys_vol, 1)
        vrow.addWidget(self.sys_vol_lbl)
        w.addLayout(vrow)
        w.addWidget(QtWidgets.QLabel("PERMISSIONS", objectName="section"))
        self.perm_lbl = QtWidgets.QLabel()
        self.perm_lbl.setTextFormat(Qt.RichText)
        self.perm_lbl.setStyleSheet("font-size:9pt")
        w.addWidget(self.perm_lbl)
        w.addWidget(QtWidgets.QLabel("CAPTURE DEVICES", objectName="section"))
        self.endp_lbl = QtWidgets.QLabel()
        self.endp_lbl.setTextFormat(Qt.RichText)
        self.endp_lbl.setWordWrap(True)
        self.endp_lbl.setStyleSheet("font-size:9pt")
        w.addWidget(self.endp_lbl)
        w.addStretch(1)
        hint = QtWidgets.QLabel("Tip: many laptops have a microphone-mute key (F4/F8 with a crossed-out mic).")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{MUTED_TXT}; font-size:8pt")
        w.addWidget(hint)

        # --- output mixer ------------------------------------------------------------
        mix_card, m = card("OUTPUT MIXER  ·  performance")
        frow = QtWidgets.QHBoxLayout()
        frow.setSpacing(18)
        self.faders = {}
        for key, name in MIX_CHANNELS:
            col = QtWidgets.QVBoxLayout()
            val = QtWidgets.QLabel()
            val.setAlignment(Qt.AlignCenter)
            val.setStyleSheet(f"color:{MUTED_TXT}; font-size:8pt")
            f = QtWidgets.QSlider(Qt.Vertical)
            f.setRange(0, 150 if key != "reverb" else 100)
            f.setValue(int(self.settings["mix"][key] * 100))
            f.setMinimumHeight(110)
            f.valueChanged.connect(lambda v, k=key, l=val: self._fader(k, v, l))
            self._fader(key, f.value(), val, emit=False)
            lab = QtWidgets.QLabel(name)
            lab.setAlignment(Qt.AlignCenter)
            col.addWidget(val)
            col.addWidget(f, 1, Qt.AlignHCenter)
            col.addWidget(lab)
            frow.addLayout(col)
            self.faders[key] = f
        frow.addStretch(1)
        reset = QtWidgets.QPushButton("Reset")
        reset.clicked.connect(self._reset_mix)
        frow.addWidget(reset, 0, Qt.AlignBottom)
        m.addLayout(frow)

        grid.addWidget(dev_card, 0, 0)
        grid.addWidget(sig_card, 0, 1)
        grid.addWidget(win_card, 0, 2)
        grid.addWidget(mix_card, 1, 0, 1, 3)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 5)
        grid.setColumnStretch(2, 3)
        grid.setRowStretch(0, 5)
        grid.setRowStretch(1, 2)

    # ---- devices ------------------------------------------------------------------
    def refresh_devices(self):
        if getattr(self, "win", None) is not None and self.win.ok:
            self.win.reset()
            self.win.endpoints(refresh=True)
        try:
            import sounddevice as sd
        except Exception as e:  # noqa: BLE001
            self.specs.setText(f"<span style='color:{ACCENT}'>Audio library unavailable: {e}</span>")
            self.test_btn.setEnabled(False)
            return
        was = self.test_btn.isChecked()
        self.stop_test()
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001
            pass
        self.devices = []
        self.dev_combo.blockSignals(True)
        self.dev_combo.clear()
        try:
            default_in = sd.default.device[0]
            for i, dv in enumerate(sd.query_devices()):
                if dv["max_input_channels"] < 1:
                    continue
                api = sd.query_hostapis(dv["hostapi"])["name"].replace("Windows ", "")
                self.devices.append(dict(index=i, name=dv["name"], api=api, info=dv))
                star = "  ★" if i == default_in else ""
                self.dev_combo.addItem(f"{api} · {dv['name']}{star}")
        except Exception as e:  # noqa: BLE001
            self.specs.setText(f"<span style='color:{ACCENT}'>Audio system error: {e}</span>")
        # restore the saved choice, else the default input
        pick = 0
        saved = self.settings.get("device")
        for k, dv in enumerate(self.devices):
            if saved and dv["name"] == saved.get("name") and dv["api"] == saved.get("api"):
                pick = k
                break
            if not saved and dv["index"] == sd.default.device[0]:
                pick = k
        self.dev_combo.blockSignals(False)
        if self.devices:
            self.dev_combo.setCurrentIndex(pick)
            self._device_changed(pick)
        if was:
            self.test_btn.setChecked(True)

    def current(self):
        k = self.dev_combo.currentIndex()
        return self.devices[k] if 0 <= k < len(self.devices) else None

    def _device_changed(self, k):
        dv = self.current()
        if not dv:
            return
        info = dv["info"]
        self.settings["device"] = dict(name=dv["name"], api=dv["api"])
        self.save()
        self.specs.setText(
            f"<table cellspacing=3>"
            f"<tr><td style='color:{MUTED_TXT}'>Audio API</td><td>&nbsp;{dv['api']}</td></tr>"
            f"<tr><td style='color:{MUTED_TXT}'>Channels</td><td>&nbsp;{info['max_input_channels']}</td></tr>"
            f"<tr><td style='color:{MUTED_TXT}'>Sample rate</td><td>&nbsp;{info['default_samplerate']:.0f} Hz</td></tr>"
            f"<tr><td style='color:{MUTED_TXT}'>Latency</td><td>&nbsp;{info['default_low_input_latency'] * 1000:.1f} – "
            f"{info['default_high_input_latency'] * 1000:.1f} ms</td></tr>"
            f"<tr><td style='color:{MUTED_TXT}'>Device #</td><td>&nbsp;{dv['index']}</td></tr></table>")
        right_ok = info["max_input_channels"] >= 2
        self.ch_btns["right"].setEnabled(right_ok)
        self.ch_btns["mix"].setEnabled(right_ok)
        if not right_ok:
            self._set_channel("left", restart=False)
        if self.test_btn.isChecked():
            self.stop_test()
            self.start_test()

    def _set_channel(self, key, restart=True):
        self.settings["channel"] = key
        for k, b in self.ch_btns.items():
            b.setChecked(k == key)
        self.save()

    def _gain_changed(self, v):
        self.settings["gain_db"] = float(v)
        self.gain_lbl.setText(f"{v:+d} dB")
        self.save()

    # recording parameters for the main window
    def record_params(self):
        dv = self.current()
        if not dv:
            return None, 44100, 1, "left", 1.0
        nch = min(2, dv["info"]["max_input_channels"])
        return (dv["index"], int(dv["info"]["default_samplerate"]), nch,
                self.settings["channel"], 10 ** (self.settings["gain_db"] / 20))

    @staticmethod
    def pick_channel(indata, mode):
        if indata.shape[1] == 1 or mode == "left":
            return indata[:, 0]
        if mode == "right":
            return indata[:, 1]
        return indata[:, :2].mean(axis=1)

    # ---- live test ------------------------------------------------------------------
    def _toggle_test(self, on):
        if on:
            self.start_test()
        else:
            self.stop_test()

    def start_test(self):
        try:
            import sounddevice as sd
        except Exception as e:  # noqa: BLE001
            self.diag.setText(str(e))
            return
        dev, sr, nch, mode, _ = self.record_params()
        self._peaks = []

        def cb(indata, frames, t, status):
            raw = self.pick_channel(indata, self.settings["channel"])
            g = 10 ** (self.settings["gain_db"] / 20)
            x = raw * g
            with self.lock:
                n = len(x)
                if n >= len(self.ring):
                    self.ring[:] = x[-len(self.ring):]
                else:
                    self.ring = np.roll(self.ring, -n)
                    self.ring[-n:] = x
                self.blk_rms = float(np.sqrt(np.mean(x ** 2)))
                self.blk_peak = float(np.abs(x).max())
                self._peaks.append(float(np.abs(raw).max()))
                self._peaks = self._peaks[-int(sr / max(1, frames)):]   # ~1 s of blocks

        try:
            self.stream = sd.InputStream(device=dev, samplerate=sr, channels=nch, dtype="float32", callback=cb)
            self.stream.start()
            self.test_btn.setText("■  Stop test")
        except Exception as e:  # noqa: BLE001
            self.stream = None
            self.test_btn.blockSignals(True)
            self.test_btn.setChecked(False)
            self.test_btn.blockSignals(False)
            self.diag.setText(f"<span style='color:{ACCENT}'>Could not open this input:</span> {e}")

    def stop_test(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None
        self.test_btn.blockSignals(True)
        self.test_btn.setChecked(False)
        self.test_btn.blockSignals(False)
        self.test_btn.setText("▶  Test microphone")

    def feed_external(self, block, gain):
        """The main window passes recording blocks here so the meter moves while recording."""
        x = block * gain
        with self.lock:
            n = len(x)
            self.ring = np.roll(self.ring, -n)
            self.ring[-n:] = x[-len(self.ring):]
            self.blk_rms = float(np.sqrt(np.mean(x ** 2)))
            self.blk_peak = float(np.abs(x).max())
            self._peaks.append(float(np.abs(block).max()))
            self._peaks = self._peaks[-40:]

    def _ui_tick(self):
        if self.stream is None and not self._peaks:
            return
        with self.lock:
            data = self.ring.copy()
            rms, peak = self.blk_rms, self.blk_peak
            raw1s = max(self._peaks) if self._peaks else 0.0
        self.scope.set_data(data)
        r_db, p_db = dbfs(rms), dbfs(peak)
        self.meter.set_levels(r_db, p_db)
        self.rms_lbl.setText(f"RMS {r_db:6.1f} dBFS" if r_db > -120 else "RMS   −∞ dBFS")
        self.peak_lbl.setText(f"Peak {p_db:6.1f} dBFS" if p_db > -120 else "Peak  −∞ dBFS")
        self.clip_lbl.setText(badge("CLIP", ACCENT) if peak >= 0.99 else "")
        self.clip_lbl.setTextFormat(Qt.RichText)
        sysmuted = getattr(self, "_sys_muted", None)
        if raw1s < 1e-4:
            why = "Windows has this microphone <b>muted</b>." if sysmuted else \
                  "The mic is probably muted by a <b>hardware key</b> or the driver, or disabled."
            self.diag.setText(f"{badge('NO SIGNAL', ACCENT)}&nbsp; The input is digital silence. {why}")
        elif dbfs(raw1s) < -50:
            self.diag.setText(f"{badge('VERY LOW', WARN)}&nbsp; Barely any sound — speak closer, "
                              f"raise the Windows volume or the gain.")
        elif peak >= 0.99:
            self.diag.setText(f"{badge('CLIPPING', ACCENT)}&nbsp; Too loud — lower the gain.")
        else:
            self.diag.setText(f"{badge('GOOD', GOOD)}&nbsp; The microphone is working.")
        if self.stream is None:
            self._peaks = self._peaks[1:]

    # ---- windows status ----------------------------------------------------------------
    def refresh_system(self):
        if not self.isVisible() and getattr(self, "_sys_done", False):
            return
        self._sys_done = True
        # permissions
        rows = privacy_status()
        if rows:
            html = "<table cellspacing=2>"
            for label, v in rows:
                col, txt = (GOOD, "✔ Allowed") if v == "Allow" else (ACCENT, "✖ Denied") if v == "Deny" \
                    else (MUTED_TXT, "– not set")
                html += f"<tr><td>{label}</td><td style='color:{col}'>&nbsp;&nbsp;{txt}</td></tr>"
            self.perm_lbl.setText(html + "</table>")
        else:
            self.perm_lbl.setText(f"<span style='color:{MUTED_TXT}'>Not available on this system.</span>")
        # mute / volume / endpoints
        if not self.win.ok:
            self.sys_name.setText(f"<span style='color:{MUTED_TXT}'>{self.win.err}</span>")
            self.mute_btn.setEnabled(False)
            self.sys_vol.setEnabled(False)
            return
        try:
            st = self.win.status()
        except Exception as e:  # noqa: BLE001
            st = None
            self.sys_name.setText(f"<span style='color:{ACCENT}'>Core Audio error: {e}</span>")
        if st:
            self._sys_muted = st["muted"]
            self.sys_name.setText(f"Default: <b>{st['name']}</b>")
            self.mute_badge.setText(badge("MUTED", ACCENT) if st["muted"] else badge("ON", GOOD))
            self.mute_btn.setText("Unmute" if st["muted"] else "Mute")
            if not self.sys_vol.isSliderDown():
                self.sys_vol.setValue(int(round(st["volume"] * 100)))
            self.sys_vol_lbl.setText(f"{st['volume'] * 100:.0f}%")
        elif st is None and self.win.ok:
            self.sys_name.setText(f"<span style='color:{ACCENT}'>Windows reports no default microphone.</span>")
        try:
            eps = self.win.endpoints()
            if eps is None:
                self.endp_lbl.setText(f"<span style='color:{MUTED_TXT}'>Scanning…</span>")
                return
            html = ""
            for name, state in eps:
                col = GOOD if state == "Active" else WARN if state in ("Disabled", "Unplugged") else MUTED_TXT
                html += f"<span style='color:{col}'>●</span> {name} — <span style='color:{col}'>{state}</span><br>"
            self.endp_lbl.setText(html or f"<span style='color:{ACCENT}'>No capture devices found.</span>")
        except Exception as e:  # noqa: BLE001
            self.endp_lbl.setText(f"<span style='color:{MUTED_TXT}'>({e})</span>")

    def _toggle_mute(self):
        try:
            st = self.win.status()
            self.win.set_mute(not st["muted"])
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Microphone", str(e))
        self.refresh_system()

    def _set_sys_vol(self, v):
        try:
            self.win.set_volume(v / 100)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Microphone", str(e))
        self.refresh_system()

    # ---- mixer ---------------------------------------------------------------------------
    def _fader(self, key, v, lbl, emit=True):
        val = v / 100
        lbl.setText(f"{val * 100:.0f}%" if key == "reverb" else
                    ("−∞ dB" if val <= 0 else f"{dbfs(val):+.1f} dB"))
        self.settings["mix"][key] = val
        if emit:
            self.save()
            self.mixChanged.emit(key, val)

    def _reset_mix(self):
        for k, f in self.faders.items():
            f.setValue(int(MIX_DEFAULT[k] * 100))

    def mix(self):
        return dict(self.settings["mix"])

    def hideEvent(self, e):
        self.stop_test()
        super().hideEvent(e)
