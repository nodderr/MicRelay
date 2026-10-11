"""Command-line entry point: python -m phonemic (add --gui for the desktop window)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__, audio_out, netutil
from .engine import Engine, EngineSettings, EngineStatus
from .logs import configure_logging

log = logging.getLogger("phonemic")

FIREWALL_HINT_AFTER_S = 60.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="phonemic",
        description="Use your phone's microphone as a virtual microphone over local Wi-Fi.",
    )
    parser.add_argument("--gui", action="store_true", help="open the desktop window instead")
    parser.add_argument("--port", type=int, default=8443, help="HTTPS port (default 8443)")
    parser.add_argument("--host", help="LAN IP/hostname to advertise (default: auto-detect)")
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    parser.add_argument("--list-devices", action="store_true", help="list output devices and exit")
    parser.add_argument("--device", help="output device index or name substring")
    parser.add_argument(
        "--target-ms", type=float, default=30.0,
        help="jitter-buffer target in ms (default 30; raise it if audio crackles)",
    )
    parser.add_argument("--gain", type=float, default=0.0, help="output gain in dB (default 0)")
    parser.add_argument("--null-output", action="store_true", help="no audio device (testing)")
    parser.add_argument("--record", type=Path, metavar="OUT.WAV", help="also write what is played to a WAV file")
    parser.add_argument("--setup-linux", action="store_true", help="create the phonemic_mic virtual source (pactl) and exit")
    parser.add_argument("--config-dir", type=Path, help="where the certificate and token live")
    parser.add_argument("--new-token", action="store_true", help="generate a new URL token (invalidates old QR codes)")
    parser.add_argument("--run-seconds", type=float, help="exit automatically after N seconds (testing)")
    parser.add_argument("--log-file", type=Path, help="also write the log to this file")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"phonemic {__version__}")
    args = parser.parse_args(argv)
    if not 5.0 <= args.target_ms <= 1000.0:
        parser.error("--target-ms must be between 5 and 1000")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def settings_from_args(args: argparse.Namespace) -> EngineSettings:
    return EngineSettings(
        port=args.port,
        host=args.host,
        bind=args.bind,
        device=args.device,
        target_ms=args.target_ms,
        gain_db=args.gain,
        null_output=args.null_output,
        record=args.record,
        config_dir=args.config_dir,
        new_token=args.new_token,
        verbose=args.verbose,
    )


def format_status(status: EngineStatus) -> str:
    """One-line status shown every second."""
    server, stats = status.server, status.stats
    if server is None or stats is None or server.state == "waiting":
        return "[phonemic] waiting for phone..."
    if server.state == "connecting":
        return f"[phonemic] phone {server.peer} connecting..."
    rtt = f"{server.rtt_ms:.1f} ms" if server.rtt_ms is not None else "-"
    latency = f"{status.latency_ms:.0f} ms" if status.latency_ms is not None else "-"
    return (
        f"[phonemic] {server.state} {server.device} | {server.sample_rate} Hz"
        f" | buf {stats.buffer_ms:5.1f} ms (target {stats.target_ms:.0f})"
        f" | underruns {stats.underruns} | lost {stats.lost} late {stats.late}"
        f" | RTT {rtt} | est. latency {latency} | drift {stats.drift_ppm:+.0f} ppm"
    )


def firewall_hint(port: int) -> str:
    if sys.platform == "win32":
        fix = (
            "Windows: Settings > Privacy & security > Windows Security > Firewall > "
            "'Allow an app through firewall' and tick 'Private' for Python,\n"
            "and make sure your Wi-Fi network profile is set to Private."
        )
    elif sys.platform == "darwin":
        fix = "macOS: System Settings > Network > Firewall > Options: allow incoming connections for Python."
    else:
        fix = f"Linux: e.g. 'sudo ufw allow {port}/tcp' and allow UDP from your LAN (WebRTC uses random UDP ports)."
    return (
        "No phone has connected yet. Check that the phone is on the same Wi-Fi (not guest/isolated),\n"
        f"that the URL opens, and that the firewall allows Python (TCP {port} + UDP).\n{fix}"
    )


def print_banner(url: str, output_description: str) -> None:
    print()
    try:
        print(netutil.render_qr(url))
    except Exception as exc:  # QR is a convenience; never fail start-up for it
        log.warning("Could not render QR code: %s", exc)
    print(f"\n  Open on your phone: {url}")
    print(f"  Output: {output_description}")
    print("  First visit: accept the certificate warning (Safari: Show Details > visit this website;")
    print("  Chrome: Advanced > Proceed). Then tap Start. Press Ctrl+C here to quit.\n", flush=True)


def run_cli(args: argparse.Namespace) -> int:
    engine = Engine(settings_from_args(args))
    engine.start()
    try:
        status = engine.status()
        if not status.virtual_device:
            print(f"\n{audio_out.virtual_mic_hint()}\n")
            log.warning("Playing to '%s' for now", status.output_description)
        print_banner(engine.url, status.output_description)
        started = time.monotonic()
        hinted = False
        while True:
            time.sleep(1.0)
            status = engine.status()
            if not status.running:
                log.error("Engine stopped unexpectedly: %s", status.error)
                return 1
            print(format_status(status), flush=True)
            elapsed = time.monotonic() - started
            ever_connected = status.server is not None and status.server.ever_connected
            if not hinted and not ever_connected and elapsed >= FIREWALL_HINT_AFTER_S:
                print(f"\n{firewall_hint(args.port)}\n", flush=True)
                hinted = True
            if args.run_seconds is not None and elapsed >= args.run_seconds:
                return 0
    except KeyboardInterrupt:
        print("\nBye.")
        return 0
    finally:
        engine.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.gui:
        from .gui import main as gui_main

        gui_argv = ["--port", str(args.port)] if args.port != 8443 else []
        if args.config_dir:
            gui_argv += ["--config-dir", str(args.config_dir)]
        if args.null_output:
            gui_argv.append("--null-output")
        if args.verbose:
            gui_argv.append("--verbose")
        return gui_main(gui_argv)
    configure_logging(args.verbose, args.log_file)
    try:
        if args.list_devices:
            audio_out.print_devices()
            return 0
        if args.setup_linux:
            return audio_out.setup_linux()
        return run_cli(args)
    except KeyboardInterrupt:
        print("\nBye.")
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
