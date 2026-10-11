"""Windows: fetch VB-Cable from vb-audio.com and open its installer.

VB-Cable is donationware by VB-Audio (www.vb-cable.com). Its licence does not
allow building it into another program's installer, so phonemic only does the
fetching: it downloads the official driver pack, checks the setup program is
signed by VB-Audio, and opens that setup program (after the Windows admin
prompt). The user clicks "Install Driver" in VB-Audio's own window.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PACK_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
HOMEPAGE = "https://vb-audio.com/Cable/"
SIGNER = "BUREL VINCENT"  # VB-Audio's code-signing certificate subject
DOWNLOAD_TIMEOUT_S = 30
MAX_PACK_BYTES = 50 * 1024 * 1024
ERROR_CANCELLED = 1223  # the user said "No" on the admin prompt

Progress = Callable[[str], None]


class InstallCancelled(Exception):
    """The user declined the Windows admin prompt."""


def supported() -> bool:
    return sys.platform == "win32"


def setup_exe_name() -> str:
    return "VBCABLE_Setup_x64.exe" if platform.machine().endswith("64") else "VBCABLE_Setup.exe"


def download_pack(dest: Path, progress: Progress | None = None, url: str = PACK_URL) -> Path:
    """Download and unpack the driver pack into dest; returns the setup program."""
    say = progress or (lambda _msg: None)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "VBCABLE_Driver_Pack.zip"
    say("Downloading VB-Cable…")
    request = urllib.request.Request(url, headers={"User-Agent": "phonemic"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response, open(archive, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(64 * 1024):
                done += len(chunk)
                if done > MAX_PACK_BYTES:
                    raise RuntimeError("The VB-Cable download is unexpectedly large.")
                out.write(chunk)
                if total:
                    say(f"Downloading VB-Cable… {100 * done // total}%")
    except OSError as exc:
        raise RuntimeError(f"Could not download VB-Cable: {exc}") from exc

    if not zipfile.is_zipfile(archive):
        raise RuntimeError("The VB-Cable download is not a valid zip file.")
    folder = dest / "VBCABLE_Driver_Pack"
    shutil.rmtree(folder, ignore_errors=True)
    with zipfile.ZipFile(archive) as pack:
        pack.extractall(folder)  # extractall drops absolute and ".." paths
    archive.unlink(missing_ok=True)
    setup = folder / setup_exe_name()
    if not setup.is_file():
        raise RuntimeError(f"The VB-Cable download does not contain {setup.name}.")
    return setup


def verify_signature(exe: Path) -> None:
    """Refuse to run the setup program unless VB-Audio's signature is valid."""
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:PHONEMIC_EXE; "
        "Write-Output $s.Status; Write-Output $s.SignerCertificate.Subject"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PHONEMIC_EXE": str(exe)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = result.stdout.strip().splitlines()
    status = lines[0].strip() if lines else ""
    subject = lines[1] if len(lines) > 1 else ""
    log.info("VB-Cable setup signature: %s, %s", status, subject)
    if status != "Valid" or SIGNER not in subject:
        raise RuntimeError(
            f"{exe.name} is not signed by VB-Audio (signature: {status or 'unknown'}), so it was not run."
        )


def run_elevated(exe: Path, cancel: threading.Event | None = None, verb: str = "runas") -> int:
    """Start exe with the Windows admin prompt and wait until it exits (-1 if cancelled)."""
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = verb
    info.lpFile = str(exe)
    info.lpDirectory = str(exe.parent)
    info.nShow = 1  # SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise InstallCancelled("The Windows admin prompt was declined.")
        raise RuntimeError(f"Could not start {exe.name}: {ctypes.FormatError(error)}")
    if not info.hProcess:
        return 0
    try:
        while kernel32.WaitForSingleObject(info.hProcess, 500) != 0:  # 0 = WAIT_OBJECT_0
            if cancel is not None and cancel.is_set():
                return -1
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


def install(dest: Path, progress: Progress | None = None, cancel: threading.Event | None = None) -> None:
    """Download VB-Cable, check it and run its setup program; returns when setup closes."""
    if not supported():
        raise RuntimeError("VB-Cable is Windows-only.")
    say = progress or (lambda _msg: None)
    setup = download_pack(dest, say)
    say("Checking the download…")
    verify_signature(setup)
    say("Click Yes on the Windows prompt, then “Install Driver” in the VB-Cable window…")
    code = run_elevated(setup, cancel)
    log.info("VB-Cable setup exited with code %s", code)
