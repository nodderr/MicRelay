"""Desktop window for phonemic: python -m phonemic.gui (or python -m phonemic --gui).

A small tkinter app on top of the Engine: it starts the server on launch,
shows the QR code, live connection stats and a level meter, and lets you
change the output device, buffer target and gain while streaming.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any, Callable

from . import __version__, audio_out, netutil, vbcable
from .engine import Engine, EngineSettings, EngineStatus
from .logs import configure_logging
from .paths import default_config_dir

log = logging.getLogger("phonemic.gui")

ASSETS = Path(__file__).parent / "assets"
SETTINGS_FILE = "settings.json"
LOG_FILE = "phonemic.log"
REFRESH_MS = 250
FIREWALL_HINT_AFTER_S = 60.0
AUTO_DEVICE = "Automatic (virtual cable)"
BUFFER_RANGE = (10, 150, 5)  # min, max, step (ms)
GAIN_RANGE = (-20, 20, 1)  # dB
STAR = "★ "
DOT = " · "

COLORS = {
    "bg": "#0f1115",
    "panel": "#181b22",
    "line": "#262a33",
    "text": "#e8eaf0",
    "muted": "#8b92a3",
    "accent": "#3ddc97",
    "accent_ink": "#062b1c",
    "warn": "#ffb547",
    "bad": "#ff5d5d",
    "info": "#6aa8ff",
}

CABLE_HELP = {
    "win32": (
        "No virtual microphone yet, so audio plays to your speakers. phonemic can install "
        "the free VB-Cable driver for you; apps then pick “CABLE Output” as their microphone.",
        "Install virtual mic",
        "",
    ),
    "darwin": (
        "No virtual cable found, so audio plays to your speakers. Install BlackHole 2ch "
        "and phonemic will use it automatically. Apps then pick “BlackHole 2ch”.",
        "Get BlackHole",
        "https://existential.audio/blackhole/",
    ),
    "linux": (
        "No virtual microphone found, so audio plays to your speakers. "
        "Create “phonemic_mic” for this login session:",
        "Set up now",
        "",
    ),
}


# ----------------------------------------------------------------- settings
def load_saved_settings(config_dir: Path) -> dict[str, Any]:
    """User choices from the previous session (invalid values are ignored)."""
    try:
        data = json.loads((config_dir / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    saved: dict[str, Any] = {}
    if isinstance(data.get("device"), str) or data.get("device") is None:
        saved["device"] = data.get("device")
    if isinstance(data.get("target_ms"), (int, float)):
        saved["target_ms"] = float(min(max(data["target_ms"], BUFFER_RANGE[0]), BUFFER_RANGE[1]))
    if isinstance(data.get("gain_db"), (int, float)):
        saved["gain_db"] = float(min(max(data["gain_db"], GAIN_RANGE[0]), GAIN_RANGE[1]))
    if isinstance(data.get("port"), int) and 1 <= data["port"] <= 65535:
        saved["port"] = data["port"]
    return saved


def save_settings(config_dir: Path, settings: EngineSettings) -> None:
    data = {
        "device": settings.device,
        "target_ms": settings.target_ms,
        "gain_db": settings.gain_db,
        "port": settings.port,
    }
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / SETTINGS_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save settings: %s", exc)


def device_options() -> list[tuple[str, str | None]]:
    """(label, device spec) pairs for the output menu: one host API, cables starred."""
    options: list[tuple[str, str | None]] = [(AUTO_DEVICE, None)]
    try:
        devices = audio_out.list_output_devices()
    except RuntimeError as exc:
        log.warning("%s", exc)
        return options
    if not devices:
        return options
    best = min(audio_out.hostapi_rank(d.hostapi) for d in devices)
    for dev in devices:
        if audio_out.hostapi_rank(dev.hostapi) == best:
            options.append(((STAR if dev.is_virtual else "") + dev.name, dev.name))
    return options


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------- app
class PhonemicApp:
    """The main window. All engine calls happen on the Tk thread except start()."""

    def __init__(self, root: tk.Tk, settings: EngineSettings, config_dir: Path) -> None:
        self.root = root
        self.settings = settings
        self.config_dir = config_dir
        self.engine: Engine | None = None
        self._start_thread: threading.Thread | None = None
        self._pending_engine: Engine | None = None
        self._start_error: str | None = None
        self._shown_url = ""
        self._level_db = -90.0
        self._closing = False
        # VB-Cable install (Windows): None, "busy", "check", "reboot" or "failed"
        self._cable_state: str | None = None
        self._cable_progress = ""
        self._cable_error: str | None = None
        self._cable_thread: threading.Thread | None = None
        self._cable_cancel = threading.Event()
        self._cable_check_pending = False
        self.px = root.winfo_fpixels("1i") / 96.0  # pixel scale for HiDPI screens

        self._setup_window()
        self._setup_style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.start_engine)
        root.after(REFRESH_MS, self._tick)

    # ------------------------------------------------------------ appearance
    def _setup_window(self) -> None:
        self.root.title("phonemic")
        self.root.configure(bg=COLORS["bg"])
        self.root.resizable(False, False)
        try:
            self._icon = tk.PhotoImage(file=str(ASSETS / "phonemic.png"))
            self.root.iconphoto(True, self._icon)
            if sys.platform == "win32":
                self.root.iconbitmap(default=str(ASSETS / "phonemic.ico"))
        except tk.TclError as exc:
            log.debug("Window icon not set: %s", exc)

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        family = tkfont.nametofont("TkDefaultFont").actual()["family"]
        return (family, size, weight)

    def _setup_style(self) -> None:
        c = COLORS
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=c["bg"], foreground=c["text"], font=self._font(10),
                        bordercolor=c["line"], lightcolor=c["line"], darkcolor=c["line"],
                        troughcolor=c["line"], focuscolor=c["panel"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Card.TFrame", background=c["panel"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure("Card.TLabel", background=c["panel"], foreground=c["text"])
        style.configure("Muted.Card.TLabel", background=c["panel"], foreground=c["muted"], font=self._font(9))
        style.configure("Title.TLabel", font=self._font(16, "bold"))
        style.configure("Footer.TLabel", foreground=c["muted"], font=self._font(9))
        style.configure("Link.TLabel", foreground=c["info"], font=self._font(9, "underline"))
        style.configure("TButton", background=c["line"], foreground=c["text"], borderwidth=0,
                        padding=(12, 6), font=self._font(10))
        style.map("TButton", background=[("active", "#323744"), ("disabled", c["panel"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_ink"],
                        font=self._font(11, "bold"), padding=(16, 9))
        style.map("Accent.TButton", background=[("active", "#5fe6ad"), ("disabled", c["line"])])
        style.configure("Stop.TButton", background=c["line"], foreground=c["text"],
                        font=self._font(11, "bold"), padding=(16, 9))
        style.map("Stop.TButton", background=[("active", "#3a2228")], foreground=[("active", c["bad"])])
        style.configure("Banner.TButton", background=c["warn"], foreground="#2b1d06",
                        font=self._font(9, "bold"), padding=(10, 4))
        style.map("Banner.TButton", background=[("active", "#ffc56e")])
        style.configure("TCombobox", fieldbackground=c["bg"], background=c["line"], foreground=c["text"],
                        arrowcolor=c["text"], selectbackground=c["bg"], selectforeground=c["text"], padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", c["bg"]), ("disabled", c["panel"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Horizontal.TScale", background=c["accent"], troughcolor=c["line"],
                        bordercolor=c["panel"], lightcolor=c["accent"], darkcolor=c["accent"])
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["accent_ink"])

    # ---------------------------------------------------------------- layout
    def _card(self, parent: tk.Misc) -> ttk.Frame:
        card = ttk.Frame(parent, style="Card.TFrame", padding=round(14 * self.px))
        card.pack(fill="x", pady=(0, round(12 * self.px)))
        return card

    def _build(self) -> None:
        pad = round(16 * self.px)
        main = ttk.Frame(self.root, padding=pad)
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x", pady=(0, round(12 * self.px)))
        ttk.Label(header, text="phonemic", style="Title.TLabel").pack(side="left")
        self.pill = tk.Label(header, text="Starting…", font=self._font(9, "bold"),
                             padx=10, pady=3, bg=COLORS["line"], fg=COLORS["muted"])
        self.pill.pack(side="right")

        self._build_connect_card(main)
        self._build_stats_card(main)
        self._build_settings_card(main)

        self.cable_banner = self._banner(main)
        self.firewall_banner = self._banner(main)

        self.toggle_button = ttk.Button(main, text="Stop server", style="Stop.TButton", command=self.toggle)
        self.toggle_button.pack(fill="x", pady=(0, round(10 * self.px)))

        footer = ttk.Frame(main)
        footer.pack(fill="x")
        link = ttk.Label(footer, text="Open log folder", style="Link.TLabel", cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda _e: open_folder(self.config_dir))
        ttk.Label(footer, text=f"v{__version__}", style="Footer.TLabel").pack(side="right")

    def _build_connect_card(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        qr_size = round(176 * self.px)
        self.qr_canvas = tk.Canvas(card, width=qr_size, height=qr_size, bg="white",
                                   highlightthickness=0, bd=0)
        self.qr_canvas.grid(row=0, column=0, rowspan=4, sticky="nw")
        right = round(14 * self.px)
        ttk.Label(card, text="Scan with your phone", style="Card.TLabel",
                  font=self._font(11, "bold")).grid(row=0, column=1, sticky="w", padx=(right, 0))
        self.url_label = ttk.Label(card, text="", style="Muted.Card.TLabel",
                                   wraplength=round(200 * self.px), justify="left")
        self.url_label.grid(row=1, column=1, sticky="w", padx=(right, 0), pady=(4, 6))
        self.copy_button = ttk.Button(card, text="Copy link", command=self.copy_url)
        self.copy_button.grid(row=2, column=1, sticky="w", padx=(right, 0))
        ttk.Label(card, text="First time: accept the certificate\nwarning, then tap Start on the phone.",
                  style="Muted.Card.TLabel", justify="left").grid(row=3, column=1, sticky="sw",
                                                                  padx=(right, 0), pady=(8, 0))

    def _build_stats_card(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        ttk.Label(card, text="Level", style="Muted.Card.TLabel").pack(anchor="w")
        self.meter_width = round(396 * self.px)
        self.meter = tk.Canvas(card, width=self.meter_width, height=round(12 * self.px),
                               bg=COLORS["line"], highlightthickness=0, bd=0)
        self.meter.pack(fill="x", pady=(4, 10))
        self.meter_bar = self.meter.create_rectangle(0, 0, 0, round(12 * self.px), width=0,
                                                     fill=COLORS["accent"])
        self.stats_line1 = ttk.Label(card, text="", style="Card.TLabel")
        self.stats_line1.pack(anchor="w")
        self.stats_line2 = ttk.Label(card, text="", style="Muted.Card.TLabel")
        self.stats_line2.pack(anchor="w", pady=(2, 0))

    def _build_settings_card(self, parent: tk.Misc) -> None:
        card = self._card(parent)
        card.columnconfigure(1, weight=1)
        gap = round(8 * self.px)

        ttk.Label(card, text="Output", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, gap))
        self._device_specs = dict(device_options()) if not self.settings.null_output else {AUTO_DEVICE: None}
        self.device_var = tk.StringVar(value=self._label_for_device(self.settings.device))
        self.device_box = ttk.Combobox(card, textvariable=self.device_var, state="readonly",
                                       values=list(self._device_specs), width=34)
        self.device_box.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.device_box.bind("<<ComboboxSelected>>", self._on_device_selected)
        if self.settings.null_output:
            self.device_box.configure(state="disabled")
        self.output_label = ttk.Label(card, text="", style="Muted.Card.TLabel", justify="left",
                                      wraplength=round(330 * self.px))
        self.output_label.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, gap))

        self.buffer_value = ttk.Label(card, text="", style="Card.TLabel", width=7, anchor="e")
        self.buffer_scale = self._slider(card, 2, "Buffer", BUFFER_RANGE, self.settings.target_ms,
                                         self.buffer_value, self._on_buffer)
        self.gain_value = ttk.Label(card, text="", style="Card.TLabel", width=7, anchor="e")
        self.gain_scale = self._slider(card, 3, "Gain", GAIN_RANGE, self.settings.gain_db,
                                       self.gain_value, self._on_gain)
        self._show_buffer(self.settings.target_ms)
        self._show_gain(self.settings.gain_db)

    def _slider(self, card: ttk.Frame, row: int, name: str, rng: tuple[int, int, int], value: float,
                value_label: ttk.Label, command: Callable[[str], None]) -> ttk.Scale:
        ttk.Label(card, text=name, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        scale = ttk.Scale(card, from_=rng[0], to=rng[1], value=value, orient="horizontal", command=command)
        scale.grid(row=row, column=1, sticky="ew", pady=4)
        scale.bind("<ButtonRelease-1>", lambda _e: self._save())
        value_label.grid(row=row, column=2, sticky="e", padx=(8, 0))
        return scale

    def _banner(self, parent: tk.Misc) -> tk.Frame:
        frame = tk.Frame(parent, bg="#2a2416", highlightbackground="#5c4a22", highlightthickness=1)
        frame.message = tk.Label(frame, text="", bg="#2a2416", fg=COLORS["warn"], justify="left",  # type: ignore[attr-defined]
                                 wraplength=round(400 * self.px), font=self._font(9), anchor="w")
        frame.message.pack(fill="x", padx=10, pady=(8, 4))  # type: ignore[attr-defined]
        frame.button = ttk.Button(frame, style="Banner.TButton")  # type: ignore[attr-defined]
        frame.button.pack(anchor="w", padx=10, pady=(0, 8))  # type: ignore[attr-defined]
        frame.visible = False  # type: ignore[attr-defined]
        return frame

    def _set_banner(self, frame: tk.Frame, text: str | None, button: str = "",
                    action: Callable[[], None] | None = None) -> None:
        if text is None:
            if frame.visible:  # type: ignore[attr-defined]
                frame.pack_forget()
                frame.visible = False  # type: ignore[attr-defined]
            return
        frame.message.configure(text=text)  # type: ignore[attr-defined]
        if action is not None:
            frame.button.configure(text=button, command=action)  # type: ignore[attr-defined]
            frame.button.pack(anchor="w", padx=10, pady=(0, 8))  # type: ignore[attr-defined]
        else:
            frame.button.pack_forget()  # type: ignore[attr-defined]
        if not frame.visible:  # type: ignore[attr-defined]
            frame.pack(fill="x", pady=(0, round(12 * self.px)), before=self.toggle_button)
            frame.visible = True  # type: ignore[attr-defined]

    # --------------------------------------------------------------- engine
    def start_engine(self) -> None:
        if self._start_thread is not None or (self.engine is not None and self.engine.running):
            return
        self._start_error = None
        engine = Engine(self.settings)
        self._pending_engine = engine

        def work() -> None:
            try:
                engine.start()
            except Exception as exc:  # shown in the window
                log.error("Could not start: %s", exc)
                self._start_error = str(exc)

        self._start_thread = threading.Thread(target=work, name="phonemic-start", daemon=True)
        self._start_thread.start()

    def _finish_start(self) -> None:
        """Called on the Tk thread once the start thread is done."""
        self._start_thread = None
        engine, self._pending_engine = self._pending_engine, None
        if self._start_error is None and engine is not None:
            self.engine = engine
            self._draw_qr(engine.url)
            log.info("Ready: %s", engine.url)
        if self._cable_check_pending:
            self._cable_check_pending = False
            self._finish_cable_check()

    def stop_engine(self) -> None:
        if self._start_thread is not None:
            self._start_thread.join()
            self._finish_start()
        if self.engine is not None:
            self.root.configure(cursor="watch")
            self.root.update_idletasks()
            self.engine.stop()
            self.engine = None
            self.root.configure(cursor="")

    def toggle(self) -> None:
        if self.engine is not None and self.engine.running:
            self.stop_engine()
        else:
            self.stop_engine()
            self.start_engine()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cable_cancel.set()
        self._save()
        self.stop_engine()
        self.root.destroy()

    # ------------------------------------------------------------- controls
    def _label_for_device(self, spec: str | None) -> str:
        for label, value in self._device_specs.items():
            if value == spec:
                return label
        return AUTO_DEVICE

    def _on_device_selected(self, _event: object = None) -> None:
        spec = self._device_specs.get(self.device_var.get())
        previous = self.settings.device
        self.settings = replace(self.settings, device=spec)
        if self.engine is not None and self.engine.running:
            try:
                self.engine.set_device(spec)
            except Exception as exc:  # e.g. the device is busy or unplugged
                log.error("Could not switch device: %s", exc)
                messagebox.showerror("phonemic", f"Could not use that device:\n\n{exc}")
                self.settings = replace(self.settings, device=previous)
                self.device_var.set(self._label_for_device(previous))
                return
        self._save()

    def _on_buffer(self, raw: str) -> None:
        low, high, step = BUFFER_RANGE
        value = float(min(max(round(float(raw) / step) * step, low), high))
        if value != self.settings.target_ms:
            self.settings = replace(self.settings, target_ms=value)
            if self.engine is not None:
                self.engine.set_target_ms(value)
        self._show_buffer(value)

    def _on_gain(self, raw: str) -> None:
        low, high, step = GAIN_RANGE
        value = float(min(max(round(float(raw) / step) * step, low), high))
        if value != self.settings.gain_db:
            self.settings = replace(self.settings, gain_db=value)
            if self.engine is not None:
                self.engine.set_gain_db(value)
        self._show_gain(value)

    def _show_buffer(self, value: float) -> None:
        self.buffer_value.configure(text=f"{value:.0f} ms")

    def _show_gain(self, value: float) -> None:
        self.gain_value.configure(text=f"{value:+.0f} dB" if value else "0 dB")

    def _save(self) -> None:
        save_settings(self.config_dir, self.settings)

    def copy_url(self) -> None:
        if self._shown_url:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._shown_url)
            self.copy_button.configure(text="Copied!")
            self.root.after(1500, lambda: self.copy_button.configure(text="Copy link"))

    def _setup_linux(self) -> None:
        if audio_out.setup_linux() != 0:
            messagebox.showerror("phonemic", "Setting up phonemic_mic failed. See the log for details.")
            return
        self.settings = replace(self.settings, device=None)
        self.device_var.set(AUTO_DEVICE)
        self.stop_engine()
        self.start_engine()

    # ------------------------------------------------------ VB-Cable install
    def install_cable(self) -> None:
        if self._cable_thread is not None:
            return
        if not messagebox.askokcancel(
            "Install virtual mic",
            "phonemic will download VB-Cable (free donationware by VB-Audio, vb-audio.com) "
            "and open its installer.\n\n"
            "1. Click Yes on the Windows prompt.\n"
            "2. Click “Install Driver” in the VB-Cable window, then close it.\n\n"
            "phonemic then switches to the new virtual mic by itself.",
        ):
            return
        self._cable_state, self._cable_error = "busy", None
        self._cable_progress = "Starting…"
        dest = self.config_dir / "vbcable"

        def work() -> None:
            try:
                vbcable.install(dest, self._set_cable_progress, self._cable_cancel)
            except vbcable.InstallCancelled:
                self._cable_error = "the Windows admin prompt was declined."
            except Exception as exc:  # shown in the banner
                log.exception("VB-Cable install failed")
                self._cable_error = str(exc)

        self._cable_thread = threading.Thread(target=work, name="vbcable-install", daemon=True)
        self._cable_thread.start()

    def _set_cable_progress(self, message: str) -> None:
        self._cable_progress = message  # read by the Tk thread in _show_banners

    def _finish_cable_install(self) -> None:
        """Called on the Tk thread once the installer has closed."""
        self._cable_thread = None
        if self._cable_error is not None:
            self._cable_state = "failed"
        elif not self._closing:
            self.recheck_cable()

    def recheck_cable(self) -> None:
        """Restart audio with a fresh device list and pick the cable if it is there now."""
        self._cable_state = "check"
        self._cable_progress = "Looking for the new virtual mic…"
        self._set_banner(self.cable_banner, self._cable_progress)  # _refresh skips banners while starting
        self.stop_engine()
        try:
            audio_out.refresh_devices()
        except RuntimeError as exc:
            log.warning("%s", exc)
        self._device_specs = dict(device_options()) if not self.settings.null_output else {AUTO_DEVICE: None}
        self.device_box.configure(values=list(self._device_specs))
        self.settings = replace(self.settings, device=None)
        self.device_var.set(AUTO_DEVICE)
        self._save()
        self._cable_check_pending = True
        self.start_engine()

    def _finish_cable_check(self) -> None:
        if self.engine is not None and self.engine.status().virtual_device:
            self._cable_state = None
            log.info("Virtual cable found after install")
            messagebox.showinfo(
                "phonemic",
                "The virtual mic is ready.\n\n"
                "In Zoom, Discord, OBS and other apps, choose “CABLE Output” as the microphone.",
            )
        else:
            self._cable_state = "reboot"

    def _cable_banner_content(self) -> tuple[str, str, Callable[[], None] | None]:
        state = self._cable_state
        if state in ("busy", "check"):
            return self._cable_progress, "", None
        if state == "reboot":
            return ("VB-Cable is installed, but Windows hasn't added the device yet. Restart "
                    "Windows to finish, then open phonemic again.", "Check again", self.recheck_cable)
        if state == "failed":
            return (f"Installing VB-Cable didn't work: {self._cable_error} You can also install it "
                    f"by hand from {vbcable.HOMEPAGE}", "Try again", self.install_cable)
        text, button, _url = CABLE_HELP["win32"]
        return text, button, self.install_cable

    # --------------------------------------------------------------- display
    def _draw_qr(self, url: str) -> None:
        self._shown_url = url
        self.url_label.configure(text=url)
        self.qr_canvas.delete("all")
        matrix = netutil.qr_matrix(url)
        size = int(self.qr_canvas["width"])
        cell = size // len(matrix)
        offset = (size - cell * len(matrix)) // 2
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    x0, y0 = offset + x * cell, offset + y * cell
                    self.qr_canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill="black", width=0)

    def _clear_qr(self, message: str) -> None:
        self._shown_url = ""
        self.qr_canvas.delete("all")
        size = int(self.qr_canvas["width"])
        self.qr_canvas.create_text(size // 2, size // 2, text=message, fill="#888888",
                                   font=self._font(10), width=size - 20, justify="center")
        self.url_label.configure(text="")

    def _set_pill(self, text: str, color: str) -> None:
        tint = {"accent": "#173a2b", "warn": "#3a2f17", "bad": "#3a1b1b", "info": "#1b2a40"}
        self.pill.configure(text=text, fg=COLORS[color], bg=tint.get(color, COLORS["line"]))

    def _draw_level(self, peak: float) -> None:
        db = 20 * math.log10(peak) if peak > 1e-6 else -90.0
        self._level_db = max(db, self._level_db - 6.0)  # fall back ~24 dB/s
        fraction = min(1.0, max(0.0, (self._level_db + 60.0) / 60.0))
        color = COLORS["bad"] if self._level_db > -1.0 else COLORS["warn"] if self._level_db > -6.0 else COLORS["accent"]
        width = max(int(self.meter.winfo_width()), self.meter_width)
        self.meter.coords(self.meter_bar, 0, 0, round(width * fraction), int(self.meter["height"]))
        self.meter.itemconfigure(self.meter_bar, fill=color)

    def _tick(self) -> None:
        try:
            if self._start_thread is not None and not self._start_thread.is_alive():
                self._finish_start()
            if self._cable_thread is not None and not self._cable_thread.is_alive():
                self._finish_cable_install()
            self._refresh()
        except Exception:  # never let a display glitch kill the refresh loop
            log.exception("Display refresh failed")
        finally:
            if not self._closing:
                self.root.after(REFRESH_MS, self._tick)

    def _refresh(self) -> None:
        if self._start_thread is not None:
            self._set_pill("Starting…", "info")
            return
        engine = self.engine
        if engine is None or not engine.running:
            self._show_stopped(engine)
            return
        status = engine.status()
        self.copy_button.state(["!disabled"])
        self.toggle_button.configure(text="Stop server", style="Stop.TButton")
        self._draw_level(status.level)
        self._show_connection(status)
        output_text = "→ " + status.output_description
        if status.output_warning:
            output_text += "\n" + status.output_warning
        self.output_label.configure(text=output_text,
                                    foreground=COLORS["warn"] if status.output_warning else COLORS["muted"])
        self._show_banners(status)

    def _show_stopped(self, engine: Engine | None) -> None:
        if engine is not None and engine.status().error:  # it stopped by itself
            self._start_error = engine.status().error
        error = self._start_error
        if error:
            self._set_pill("Error", "bad")
            self.stats_line1.configure(text="phonemic could not start.")
            self.stats_line2.configure(text=error)
            self._clear_qr("Not running")
        else:
            self._set_pill("Stopped", "muted")
            self.stats_line1.configure(text="The server is stopped.")
            self.stats_line2.configure(text="Click Start server to continue.")
            self._clear_qr("Stopped")
        self.engine = None
        self.copy_button.state(["disabled"])
        self.toggle_button.configure(text="Start server", style="Accent.TButton")
        self._draw_level(0.0)
        self._set_banner(self.cable_banner, None)
        self._set_banner(self.firewall_banner, None)

    def _show_connection(self, status: EngineStatus) -> None:
        server, stats = status.server, status.stats
        state = server.state if server is not None else "waiting"
        if state == "waiting" or server is None or stats is None:
            self._set_pill("Waiting for phone", "muted")
            self.stats_line1.configure(text="Scan the QR code, then tap Start on your phone.")
            self.stats_line2.configure(text="")
            return
        if state == "connecting":
            self._set_pill("Connecting…", "info")
            self.stats_line1.configure(text=f"Phone {server.peer} is connecting…")
            self.stats_line2.configure(text="")
            return
        if state == "muted":
            self._set_pill("Muted on phone", "warn")
        else:
            self._set_pill(f"Connected — {server.device}", "accent")
        latency = f"{status.latency_ms:.0f} ms" if status.latency_ms is not None else "–"
        rtt = f"{server.rtt_ms:.0f} ms" if server.rtt_ms is not None else "–"
        self.stats_line1.configure(
            text=f"Latency {latency}{DOT}Buffer {stats.buffer_ms:.0f} ms{DOT}RTT {rtt}"
        )
        self.stats_line2.configure(
            text=f"Underruns {stats.underruns}{DOT}Lost {stats.lost}{DOT}Late {stats.late}"
            f"{DOT}{server.sample_rate} Hz"
        )

    def _show_banners(self, status: EngineStatus) -> None:
        busy = self._cable_state in ("busy", "check")
        if not busy and (status.virtual_device or self.settings.device is not None):
            self._set_banner(self.cable_banner, None)
        elif vbcable.supported() and not self.settings.null_output:
            self._set_banner(self.cable_banner, *self._cable_banner_content())
        else:
            key = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
            text, button, url = CABLE_HELP[key]
            action = self._setup_linux if key == "linux" else (lambda: webbrowser.open(url))
            self._set_banner(self.cable_banner, text, button, action)

        connected = status.server is not None and status.server.ever_connected
        if not connected and status.uptime_s >= FIREWALL_HINT_AFTER_S:
            self._set_banner(
                self.firewall_banner,
                "No phone has connected yet. Make sure the phone is on the same Wi-Fi (not a guest "
                "network) and that your firewall allows phonemic/Python on private networks "
                f"(TCP {self.settings.port} and UDP).",
            )
        else:
            self._set_banner(self.firewall_banner, None)


# --------------------------------------------------------------------- main
def _windows_setup() -> None:
    """Crisp text on HiDPI screens and our own taskbar icon/grouping."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("phonemic.app")
    except (AttributeError, OSError):
        pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="phonemic-gui", description="phonemic desktop window")
    parser.add_argument("--port", type=int, help="HTTPS port (default: last used, else 8443)")
    parser.add_argument("--config-dir", type=Path, help="where settings, certificate and log live")
    parser.add_argument("--null-output", action="store_true", help="no audio device (testing)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace, config_dir: Path) -> EngineSettings:
    saved = load_saved_settings(config_dir)
    return EngineSettings(
        port=args.port or saved.get("port", 8443),
        device=saved.get("device"),
        target_ms=saved.get("target_ms", 30.0),
        gain_db=saved.get("gain_db", 0.0),
        null_output=args.null_output,
        config_dir=config_dir,
        verbose=args.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_dir = args.config_dir or default_config_dir()
    configure_logging(args.verbose, config_dir / LOG_FILE)
    log.info("phonemic %s starting (GUI)", __version__)
    _windows_setup()
    root = tk.Tk()
    PhonemicApp(root, build_settings(args, config_dir), config_dir)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
