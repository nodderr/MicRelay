# phonemic

Use your phone's microphone as a microphone on your laptop over your local Wi-Fi. It works on iPhone (Safari) and Android (Chrome) and needs no app on the phone.

The laptop runs a small Python server. The phone opens a web page served by that server and sends raw PCM audio over a WebRTC data channel. The laptop puts the audio through a jitter buffer and plays it into a virtual audio cable. Zoom, Discord, OBS and other apps then pick that cable as their microphone.

```
phone mic ─► AudioWorklet (10 ms Int16 chunks) ─► WebRTC data channel (unordered, no retransmits)
          ─► laptop jitter buffer (reorder, loss → silence, drift-corrected resampling)
          ─► sounddevice ─► virtual cable ─► Zoom / Discord / OBS
```

Typical latency on 5 GHz Wi-Fi is 40–60 ms end to end with the default 30 ms buffer.

## One-click app (Windows)

1. Double-click **`setup.bat`** once. It creates a Python environment, installs everything, and puts a **phonemic** shortcut (with icon) on your Desktop and in the Start Menu.
2. From then on, double-click the **phonemic** shortcut, or `phonemic.bat` in this folder. A window opens and the server starts immediately. There's no console window.

```
┌─ phonemic ──────────────── ● Connected — iPhone Safari ┐
│  [QR code]   Scan with your phone  https://…  [Copy]   │
│  Level ▕██████████░░░░▏                                │
│  Latency 45 ms · Buffer 30 ms · RTT 6 ms               │
│  Output [Automatic (virtual cable) ▾]  → CABLE Input…  │
│  Buffer ──●──── 30 ms     Gain ────●── 0 dB            │
│                 [ Stop server ]                        │
└────────────────────────────────────────────────────────┘
```

The window shows:
- the QR code and link;
- the connection state and the phone model;
- latency, buffer, RTT, underruns and lost/late packets;
- a level meter of what your apps hear.

You can switch the output device and adjust the buffer and gain while streaming. Your choices are remembered. If no virtual cable is installed, a banner offers **Install virtual mic** (Windows), which sets up VB-Cable for you (see [below](#windows-vb-cable)). If no phone has connected after a minute, a firewall hint appears. **Open log folder** shows `phonemic.log`.

On macOS and Linux the same window opens with `python -m phonemic.gui` (or `python -m phonemic --gui`).

### Standalone `.exe` (no Python needed)

`build_exe.bat` uses PyInstaller to build `dist\phonemic\phonemic.exe` and `dist\phonemic-windows.zip` (about 57 MB). Unzip it on any Windows PC and double-click `phonemic.exe`.
- **Size:** it's a folder build (about 140 MB unzipped, most of it FFmpeg and numpy). That makes it start faster and trip antivirus less often than a single-file exe.
- **First launch:** the exe isn't code-signed, so Windows SmartScreen will say *"Windows protected your PC"*. Click **More info → Run anyway**. The firewall prompt will name `phonemic.exe`; allow it on private networks.
- **Command-line use:** the exe accepts the same options as below, e.g. `phonemic.exe --list-devices`. It has no console, so add `--log-file out.txt` to see output.

## Install (command line, any OS)

You need Python 3.10 or newer.

```bash
git clone <this repo> phonemic && cd phonemic
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

On Linux you also need PortAudio and `pactl`, for example `sudo apt install libportaudio2 pulseaudio-utils`. `pactl` works with PipeWire too.

## Quick start

1. **Install a virtual cable** once. See [Virtual microphone setup](#virtual-microphone-setup) below.
2. **Run** `python -m phonemic` on the laptop. Scan the QR code it prints with the phone's camera. The phone must be on the same Wi-Fi.
3. **Tap Start** on the phone. The first time, accept the certificate warning and allow microphone access. Then select the virtual cable as the microphone in your app.

Keep the phone's screen on while streaming. The page tries to keep it awake automatically.

## Running

```
python -m phonemic [options]
```

| Option | Meaning |
|---|---|
| `--port 8443` | HTTPS port. |
| `--host IP` | IP or hostname to advertise in the URL and certificate. Default: auto-detected LAN IP. |
| `--bind 0.0.0.0` | Address to listen on. |
| `--list-devices` | List output devices. Virtual cables are marked with `*`. |
| `--device NAME_OR_ID` | Output device by index, or by part of its name, e.g. `--device "CABLE Input"`. |
| `--target-ms 30` | Jitter-buffer target in ms (see [Tuning](#tuning---target-ms)). |
| `--gain 0` | Output gain in dB, e.g. `--gain 6`. |
| `--null-output` | Use no audio device. Audio is consumed in real time (for testing). |
| `--record out.wav` | Also write exactly what is played to a WAV file. |
| `--install-cable` | Windows: download VB-Cable, open its installer, check the new device, and exit. |
| `--setup-linux` | Create the `phonemic_mic` virtual microphone (PulseAudio/PipeWire) and exit. |
| `--config-dir DIR` | Where the certificate and token are stored. |
| `--new-token` | Generate a new URL token. Old QR codes and bookmarks stop working. |
| `--run-seconds N` | Exit automatically after N seconds (for testing). |
| `--log-file FILE` | Also write the log to a file. |
| `--gui` | Open the desktop window instead. |
| `--verbose` | Debug logging. |

The status line, printed once per second, looks like this:

```
[phonemic] connected iPhone Safari | 48000 Hz | buf  30.1 ms (target 30) | underruns 0 | lost 0 late 0 | RTT 6.2 ms | est. latency 45 ms | drift +12 ppm
```

The fields are:
- **buf**: how long audio waits in the jitter buffer on average. This is the value the controller holds at the target.
- **lost**: packets that never arrived.
- **late**: packets that arrived after their audio had already been played.
- **underruns**: times the buffer ran dry and had to refill.
- **est. latency**: RTT/2 + buf + the output device's latency. The phone's own capture and packetisation add about 10–20 ms more.
- **drift**: the resampling correction applied to cancel the clock difference between the phone and the sound card.

The certificate and the URL token are stored in a config directory and reused. That means the phone's one-time certificate exception and a bookmarked URL keep working across runs:
- Windows: `%APPDATA%\phonemic`
- macOS: `~/Library/Application Support/phonemic`
- Linux: `~/.config/phonemic`

The certificate is only regenerated if your LAN IP changes. The window's settings (`settings.json`) and its log (`phonemic.log`) are kept in the same folder. The command line and the window share the certificate and token, so the phone's bookmark works with both.

## Virtual microphone setup

### Windows: VB-Cable

Windows only lists a microphone that comes from an audio driver, and only Microsoft-signed drivers load, so phonemic can't create one itself. It uses VB-Cable, a free (donationware) signed driver by VB-Audio.

1. Install it, using any one of these:
   - In the window, click **Install virtual mic** on the yellow banner.
   - Run `python -m phonemic --install-cable`.
   - Download it from <https://vb-audio.com/Cable/>, unzip it, run `VBCABLE_Setup_x64.exe` as administrator, and click **Install Driver**.

   The first two download the official driver pack from vb-audio.com into `%APPDATA%\phonemic\vbcable`. They check that the setup program is signed by VB-Audio, then open it. Click **Yes** on the Windows admin prompt, then **Install Driver**. VB-Cable's licence doesn't allow it to be built into another installer, so phonemic opens VB-Audio's own setup window rather than installing silently. phonemic then rescans the audio devices and switches to the cable. If Windows hasn't added the device yet, restart Windows. To uninstall, run the same setup program again and click **Remove Driver**.
2. Run `python -m phonemic`. It automatically plays into **"CABLE Input"**, preferring the low-latency WASAPI version of the device.
3. In Zoom, Discord, OBS and other apps, choose **"CABLE Output"** as the microphone.

### macOS: BlackHole 2ch

1. Install BlackHole 2ch from <https://existential.audio/blackhole/> or with `brew install blackhole-2ch`.
2. Run `python -m phonemic`. It automatically plays into **"BlackHole 2ch"**.
3. In your apps, choose **"BlackHole 2ch"** as the microphone.

Rogue Amoeba Loopback devices, or any device with "Loopback" in its name, are also detected.

### Linux: PulseAudio or PipeWire

1. Run `python -m phonemic --setup-linux` once per login. It uses `pactl` to create a null sink `phonemic` and a virtual source **`phonemic_mic`** that is remapped from the sink's monitor.
2. Run `python -m phonemic`. It sends audio to the `phonemic` sink through the `pulse` device, using `PULSE_SINK`.
3. In your apps, choose **`phonemic_mic`** as the microphone.

To make the setup permanent, add the two `load-module` lines that `--setup-linux` prints to `~/.config/pulse/default.pa` (PulseAudio). On PipeWire, run `--setup-linux` from your session's autostart instead.

If no virtual cable is found, phonemic plays to your default output device so you can still hear it, and prints these setup instructions.

## Firewall

The phone connects to the laptop on **TCP 8443** (the page and signalling). The audio uses **UDP on random high ports** (WebRTC). Both must be allowed on your private network. If no phone has connected after 60 seconds, phonemic prints a firewall hint.

- **Windows:** when the "Windows Defender Firewall has blocked some features" dialog appears, allow **Python** on **Private networks**. If you missed it, go to *Windows Security → Firewall & network protection → Allow an app through firewall* and tick **Private** for `python.exe`. Your Wi-Fi network profile must be set to **Private**, not Public.
- **macOS:** *System Settings → Network → Firewall → Options*: allow incoming connections for Python.
- **Linux (ufw):** `sudo ufw allow 8443/tcp` and allow UDP from your LAN, for example `sudo ufw allow from 192.168.0.0/16 proto udp`.

Guest Wi-Fi networks and many public networks block devices from talking to each other ("client isolation"). Use your normal home network, or your phone's hotspot with the laptop connected to it.

## Troubleshooting

| Problem | What to do |
|---|---|
| **Certificate warning** on the phone | This is expected: the certificate is self-signed. **Safari:** *Show Details → visit this website → Visit Website*. **Chrome:** *Advanced → Proceed to …*. You only need to do this once per certificate. It comes back if your laptop's IP changes. |
| **Phone can't connect** / the page doesn't load | Check that both devices are on the same Wi-Fi and not a guest network. Check the firewall (see above). Try `--host <laptop IP>` if the wrong network interface was detected, e.g. with a VPN or WSL/Hyper-V adapters. If the page loads but stays on "Connecting…", UDP is being blocked. |
| **"Access denied: rescan the QR code"** | The token changed (for example after `--new-token`). Scan the new QR code. |
| **Crackling or clicks** | Look at `underruns` and `late` in the status line. If they keep increasing, raise the buffer: `--target-ms 50`. Also move closer to the router, use 5 GHz, or turn off Bluetooth on the phone. If `drift` sits at ±5000 ppm, the clocks differ by more than the correction allows. That's rare; please report it. |
| **High latency** | Lower `--target-ms`, e.g. to 20. Use a WASAPI device on Windows (`--list-devices`; it is chosen automatically when available). Avoid Bluetooth headphones as the monitor output. Leave echo cancellation, noise suppression and auto gain **off** on the phone, because each adds processing delay. |
| **Mic stops when the screen locks** | Browsers stop the microphone when the page is hidden. Keep the screen on and the page in front. The page uses the Wake Lock API where available. On iPhone you can also set *Settings → Display & Brightness → Auto-Lock → Never* while streaming. When you return to the page, it reconnects automatically. Tap Start again if the system stopped the mic. |
| **"Microphone permission was denied"** | **iPhone:** *Settings → Safari → Microphone → Allow*, or *aA → Website Settings → Microphone*. **Android Chrome:** tap the lock icon in the address bar, then *Permissions → Microphone*. |
| **"Couldn't open … so phonemic is using … instead"** | The preferred output wouldn't start, e.g. a monitor's HDMI audio while the monitor sleeps or is on another input. phonemic tries the same device through the other Windows audio systems, then the virtual cable, the default outputs and finally any other output, rather than stopping. Install the virtual cable, or pick a working device in the **Output** list. |
| Audio too quiet or too loud | Use `--gain 6` or `--gain -6` (dB), or tick *Auto gain control* on the phone. |

## Tuning `--target-ms`

`--target-ms` is how long each packet waits in the buffer before it is played. It is the main trade-off between latency and robustness. A packet that arrives later than the target, relative to the average, misses its slot and is heard as a short gap.

- Start with the default of **30**.
- Watch `underruns` and `late` for a minute while you talk. If they stay at 0, try **20** or even **15**. On a quiet 5 GHz network with an iPhone, 15–20 ms often works.
- If they keep increasing, raise the target in steps of 10 until they stop. Busy 2.4 GHz networks or a far-away router may need 50–80.

The clock-drift controller keeps the buffer at the target for hours. Its correction is limited to ±0.5 % and shows as `drift` in the status line. The resulting pitch change is far too small to hear.

## How it works

- **Signalling** is a single HTTPS `POST /offer` (SDP in, SDP out), checked against the token from the QR code. WebSocket is deliberately not used, because iOS Safari often refuses `wss://` connections to self-signed certificates. Only one phone is connected at a time; a new connection replaces the old one, and the old phone is told so.
- **Transport** uses two WebRTC data channels:
  - `audio` (unordered, `maxRetransmits=0`): each message is a little-endian `uint32 seq`, a `uint32 sample position`, then 10 ms of mono Int16 PCM.
  - `ctl` (reliable JSON): `hello` (sample rate, chunk size, user agent), `ping`/`pong` for RTT, `mute` and `stop`.
- **Phone page**: the mic is opened with echo cancellation, noise suppression and AGC off by default. An inline AudioWorklet cuts the 128-sample render blocks into 10 ms chunks. If the data channel backs up, chunks are dropped rather than queued, so latency never builds up on the phone.
- **Jitter buffer** (`phonemic/jitter.py`): a ring buffer indexed by absolute sample position. Reordering is handled for free. A lost packet becomes silence of exactly the right length, and a packet that arrives after its audio has played is dropped. A PI controller steers the measured per-packet buffering delay to the target by adjusting the resampling ratio, which also converts the phone's rate (44.1 or 48 kHz) to the output device's rate using linear interpolation.
- **Output**: a `sounddevice` stream with ~10 ms blocks and `latency='low'`. Mono is copied to every output channel, and the audio callback does no blocking work.

## Testing

```bash
pytest
```

- `tests/test_jitter.py` simulates a phone feeding the buffer and checks:
  - steady input has no underruns;
  - a ±300 ppm clock offset over 10 simulated minutes stays within ±10 ms of the target (in practice within about ±2.5 ms);
  - with 5 % loss plus reordering, every played sample is either correct or silence for a lost packet, so nothing is shifted, dropped or duplicated;
  - the 44.1 → 48 kHz resampler keeps a 1 kHz tone (checked by FFT, spurious content < −50 dB);
  - late-packet, overrun, concealment and underrun behaviour, and counter wrap-around.
- `tests/test_e2e.py` starts the real server with `--null-output --record` and connects `tests/fake_phone.py`, which uses the same signalling and data channels as the browser. It then checks that the recording holds a clean 440 Hz tone, and that the tone survives loss and jitter.
- `tests/test_engine.py` checks the engine behind both front-ends: start/stop/restart, live buffer and gain changes, level metering, status reporting, and a clear error when the port is already in use.
- `tests/test_vbcable.py` checks the VB-Cable helper: unpacking, rejecting broken downloads, refusing a setup program not signed by VB-Audio, and the window's install flow (offer → progress → switch to the cable, a declined admin prompt, and "restart Windows"). Set `PHONEMIC_NETWORK_TESTS=1` to also download the real pack and check its signature.
- `tests/test_gui.py` opens the real window, connects the fake phone and checks that the window shows "Connected" with stats. It also moves the sliders, stops the server, and checks that settings are saved and validated.

`scripts/make_icon.py` regenerates the icons in `phonemic/assets/`.

To run the end-to-end check by hand:

```bash
python -m phonemic --null-output --record out.wav --run-seconds 15
# in a second terminal, using the URL printed by the server:
python tests/fake_phone.py "https://<ip>:8443/?t=<token>" --seconds 8 [--loss 0.05] [--jitter-ms 20] [--rate 44100]
python tests/fake_phone.py --check out.wav      # prints CLEAN TONE or NOT CLEAN
```

## Limits

phonemic only works on a local network, not over the internet. It has no Opus codec, no Bluetooth support and no native phone apps. Raw PCM uses about 0.8 Mbit/s, which is trivial on Wi-Fi.
