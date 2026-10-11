# phonemic: step-by-step instructions

This guide takes you from nothing to using your phone as a microphone in Zoom, Discord or OBS. It takes about 10 minutes the first time and about 10 seconds after that.

For the full option list, troubleshooting table and technical details, see [README.md](README.md).

---

## What you need

- A laptop running Windows, macOS or Linux with **Python 3.10 or newer**.
- An iPhone (Safari) or Android phone (Chrome).
- Both devices on the **same Wi-Fi network**. A 5 GHz network is best. Guest networks usually block this.

---

## Part 1: One-time setup on the laptop

### Step 1: Install Python

Check whether you already have it. Open a terminal (on Windows, PowerShell) and run:

```
python --version
```

If it shows 3.10 or higher, skip ahead to Step 2. Otherwise:

- **Windows:** install Python from <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** in the installer.
- **macOS:** run `brew install python`, or use the installer from python.org.
- **Linux:** run `sudo apt install python3 python3-venv libportaudio2 pulseaudio-utils`, or your distribution's equivalent.

### Step 2: Install phonemic

**Windows (the easy way):** in the project folder, double-click **`setup.bat`**. It installs everything and creates a **phonemic** shortcut, with a green microphone icon, on your Desktop and in the Start Menu. When it says *Done!*, press a key to close it.

> Received a `phonemic-windows.zip` instead of this folder? Then you don't need Python at all. Unzip it anywhere and double-click `phonemic.exe`. The first time, Windows SmartScreen will warn you because the app isn't signed: click **More info → Run anyway**. Then skip ahead to Step 3.

**Windows (manually, in PowerShell):**

```powershell
cd C:\Files\Projects\Personal\MicRelay
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

**macOS / Linux:**

```bash
cd /path/to/phonemic
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

> In the rest of this guide, `PY` means `.venv\Scripts\python` on Windows and `.venv/bin/python` on macOS/Linux.

### Step 3: Install a virtual microphone

Apps can only choose a "microphone" that the operating system knows about. A virtual cable gives you one: phonemic plays audio into one end, and apps record from the other end.

**Windows: VB-Cable (let phonemic do it)**

1. Start phonemic (Step 5). If no virtual mic is installed yet, a yellow banner appears. Click **Install virtual mic**, then **OK**.
2. phonemic downloads VB-Cable (free donationware from vb-audio.com) and opens its installer. Click **Yes** on the Windows prompt.
3. In the VB-Cable window, click **Install Driver**. When it says it's done, click **OK** and close the window.
4. phonemic switches to the cable by itself and says *"The virtual mic is ready"*. If the banner says *"Restart Windows to finish"* instead, restart, then open phonemic again.

From a terminal, `PY -m phonemic --install-cable` does the same.

To do it by hand: download VB-Cable from <https://vb-audio.com/Cable/>, unzip it, right-click `VBCABLE_Setup_x64.exe`, choose **Run as administrator**, click **Install Driver**, then reboot.

**macOS: BlackHole 2ch**

1. Run `brew install blackhole-2ch`, or download it from <https://existential.audio/blackhole/>.
2. If macOS asks you to allow the audio driver, allow it.

**Linux: PulseAudio / PipeWire**

1. Run `PY -m phonemic --setup-linux`.
2. You need to do this once per login. To make it permanent, see the README.

### Step 4: Check that phonemic sees the cable

```
PY -m phonemic --list-devices
```

The cable should appear with a `*` next to it. On Windows it shows as **CABLE Input**, and on macOS as **BlackHole 2ch**. On Linux, look for a line saying the `phonemic` sink will be used.

If you use the phonemic window, you can skip this check. Once the window is open (Step 5), the cable appears with a ★ in its **Output** list. If the window shows the yellow *"No virtual microphone yet"* banner, the cable isn't installed yet.

---

## Part 2: Connect your phone (first time)

### Step 5: Start phonemic

**With the window (recommended):** double-click the **phonemic** shortcut on your Desktop. On macOS/Linux, run `PY -m phonemic.gui`. The window opens with a **QR code** and starts the server immediately.

**From a terminal:** run `PY -m phonemic`. The terminal shows the QR code and a URL like `https://192.168.1.11:8443/?t=...`. Leave the terminal open.

**Windows:** a firewall dialog may pop up the first time. It will name *pythonw.exe*, *python.exe* or *phonemic.exe*, depending on how you started phonemic. Tick **Private networks** and click **Allow access**. If you don't, the phone can't connect.

### Step 6: Open the page on your phone

1. Point the phone's camera at the QR code and tap the link that appears. You can also type the URL into Safari or Chrome.
2. You will see a **certificate warning**. This is expected, because phonemic uses its own self-signed certificate. The page is only reachable on your Wi-Fi.
   - **iPhone (Safari):** tap **Show Details**, then **visit this website**, then **Visit Website**.
   - **Android (Chrome):** tap **Advanced**, then **Proceed to … (unsafe)**.

   You only have to do this once. If your laptop's IP address changes, you will be asked again.

### Step 7: Tap Start

1. Tap the big green **Start** button.
2. Allow **microphone access** when asked.
3. The status pill at the top turns green and shows **Live**, and the level meter moves when you speak.

On the laptop, the window's status turns green (**Connected — iPhone Safari**, for example), and the level meter moves when you speak. In the terminal version, the status line changes to `connected`.

### Step 8: Select the microphone in your app

| App | Where to set it | Choose (Windows / macOS / Linux) |
|---|---|---|
| Zoom | Settings → Audio → Microphone | CABLE Output / BlackHole 2ch / phonemic_mic |
| Discord | User Settings → Voice & Video → Input Device | same |
| OBS | Sources → + → Audio Input Capture | same |
| Teams | Settings → Devices → Microphone | same |
| Windows itself | Settings → System → Sound → Input | CABLE Output |

On Windows the names are confusing: phonemic plays into **CABLE Input**, and your apps listen to **CABLE Output**.

You're done. Talk into your phone.

---

## Part 3: Everyday use

1. On the laptop, double-click the **phonemic** shortcut, or run `PY -m phonemic`.
2. On the phone, open the bookmarked page, or scan the QR code again. The URL stays the same between runs.
3. Tap **Start**.

While you're streaming:
- **Keep the phone's screen on and the page open.** Browsers stop the microphone when the screen locks or you switch apps. The page tries to keep the screen awake. On iPhone you can also set *Settings → Display & Brightness → Auto-Lock → Never* while you use it.
- Tap **Mute** on the phone to mute without disconnecting.
- If Wi-Fi drops briefly, the page reconnects by itself.

To stop, tap **Stop** on the phone, and close the phonemic window. In the terminal version, press **Ctrl+C**.

---

## Part 4: Adjusting quality and latency

**In the window**, everything applies immediately while you stream and is remembered for next time:
- **Buffer slider:** move it right if you hear crackling, or if *Underruns*/*Late* keep increasing. Move it left for less delay.
- **Gain slider:** makes the voice louder or quieter (in dB).
- **Output list:** pick a different device. *Automatic* uses the virtual cable.

**In the terminal version**, watch the status line for a minute while you talk:

```
[phonemic] connected iPhone Safari | 48000 Hz | buf  30.1 ms (target 30) | underruns 0 | lost 0 late 0 | RTT 6.2 ms | est. latency 45 ms | drift +12 ppm
```

| What you see or hear | What to do |
|---|---|
| `underruns` or `late` keep increasing, or you hear crackling | Use a bigger buffer: `PY -m phonemic --target-ms 50`. |
| Everything stays at 0 and you want less delay | Use a smaller buffer: `PY -m phonemic --target-ms 20`. |
| Too quiet / too loud | Use `--gain 6` or `--gain -6` (in dB). You can also tick **Auto gain control** on the phone. |
| Background noise or echo | Tick **Noise suppression** or **Echo cancellation** on the phone. These add a little delay. |
| Wrong output device chosen | Run `--list-devices`, then pick one with `--device "CABLE Input"` or `--device 23`. |

Options can be combined, for example `PY -m phonemic --target-ms 40 --gain 3`.

---

## If something doesn't work

| Problem | Fix |
|---|---|
| The page doesn't load on the phone | Check both devices are on the same Wi-Fi (not a guest network). Allow Python through the firewall. On Windows, also set your Wi-Fi network profile to **Private**. phonemic prints a firewall hint after 60 seconds if nothing connects. |
| The page loads but stays on "Connecting…" | The firewall is blocking UDP. Allow Python on private networks. |
| The URL shows the wrong IP (VPN, WSL, virtual adapters) | Find the laptop's Wi-Fi IP and run `PY -m phonemic --host 192.168.x.y`. |
| "Access denied: rescan the QR code" | Scan the QR code currently shown in the terminal. |
| "Microphone permission was denied" | **iPhone:** Settings → Safari → Microphone → Allow. **Android:** tap the lock icon in the address bar → Permissions → Microphone. |
| The app hears nothing | Make sure the app's microphone is set to **CABLE Output**, **BlackHole 2ch** or **phonemic_mic**, and that the phone shows **Live** and isn't muted. |
| "No virtual microphone yet" / "No virtual cable found" | Install the cable (Step 3). On Windows, click **Install virtual mic** in the window. Until then, phonemic plays to your speakers. |
| "Installing VB-Cable didn't work" | If you clicked **No** on the Windows prompt, click **Try again**. If the download failed, check your internet connection, or install it by hand (Step 3). |
| Port 8443 is already in use | Run `PY -m phonemic --port 9443`. |

---

## Optional: check the installation without a phone

This runs a simulated phone against the real server and checks that a clean tone arrives:

```
PY -m pytest
```

All tests should pass. They take about a minute.
