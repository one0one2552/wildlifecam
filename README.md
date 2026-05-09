# O·W·L — Wildlife Camera System

**O·W·L** (Outdoor Wildlife Logger) is a Raspberry Pi 4 wildlife trap camera built around the IMX708 Camera Module 3 Wide NoIR. A PIR sensor triggers H.264 video recordings with a configurable pre-event ring buffer. Everything is accessible through a browser-based web UI with no app or special client required.

---

## Hardware

| Component | Details |
|---|---|
| Raspberry Pi 4 | Main compute board |
| Camera Module 3 Wide NoIR (IMX708) | 12 MP sensor, no IR-cut filter |
| PIR sensor | GPIO 17 (active high) |
| IR relay / floodlight | GPIO 18 (active high) |

---

## Features

- **Trap mode** — Continuous H.264 ring buffer. PIR trigger saves the pre-event buffer and records until no motion is detected for `post_event_seconds`. Re-triggering the PIR extends the recording window automatically.
- **Live stream** — On-demand MJPEG preview in the browser. Auto-reverts to Trap mode after a configurable watchdog timeout.
- **Camera settings** — All controls (autofocus, AWB, exposure, contrast, saturation, image flip, HDR) take effect immediately; no page reload or save needed.
- **Trap timing** — Pre- and post-event buffer durations are configurable through the Settings page.
- **NoIR tuning** — Loads `imx708_wide_noir.json` for correct Greyworld AWB on the NoIR sensor.
- **Recording library** — Dashboard with playback, download, bulk delete, and ZIP export.
- **NAS upload** — Automatic or manual upload to a Samba/SMB share with size verification.
- **Storage guard** — Deletes oldest recordings when free disk space drops below a threshold.
- **WiFi management** — Scan and connect to networks via nmcli from the Settings page.
- **System health** — CPU/RAM usage, temperature and voltage peaks tracked since last reboot.
- **User management** — Multiple accounts with admin / viewer roles (admin-only panel).
- **Event-driven recording list** — Dashboard refreshes the recording list only when new files appear (no wasted polling).

---

## Project Structure

```
wildlife-cam-pi4/
├── config.yaml             Main configuration (persisted on every settings change)
├── requirements.txt        Python dependencies
├── src/
│   ├── main.py             Entry point — wires all managers together, starts Flask
│   ├── camera_manager.py   State machine: TRAP / RECORDING / LIVE / STOPPING
│   ├── gpio_manager.py     PIR polling thread and relay control (lgpio)
│   ├── storage_manager.py  Recording list, disk guard, NAS upload queue
│   ├── web_server.py       All Flask routes, HTTP Basic Auth, settings persistence
│   ├── static/             CSS, JS, logo assets
│   └── templates/
│       ├── index.html      Dashboard (recording library, status bar)
│       ├── stream.html     Live MJPEG stream + camera settings panel
│       └── settings.html   Trap timing, NAS, WiFi, health, users, logs
├── recordings/             Default local recording storage (MP4)
└── logs/                   Rotating application log (wildlife_cam.log)
```

---

## How the Software Works

### Startup sequence (`main.py`)

1. Logging is configured — rotating file handler (`logs/wildlife_cam.log`) and stdout (captured by systemd).
2. `config.yaml` is loaded.
3. **`StorageManager`** starts its background threads: disk-guard (checks free space every 60 s) and the NAS upload queue.
4. **`CameraManager`** initialises and enters Trap Mode — Picamera2 starts encoding into a `CircularOutput` ring buffer.
5. **`GPIOManager`** starts its PIR polling thread (50 ms interval, 300 ms debounce).
6. `cam_mgr._relay_callback = gpio_mgr.set_relay` wires the IR floodlight relay so it turns on automatically when a recording starts and off when it ends.
7. The **Flask** app is started on the configured host/port (default `0.0.0.0:8080`).

### Camera state machine (`camera_manager.py`)

```
        ┌────────────────────────────────────────────┐
        │                                            │
        ▼                                            │
┌──────────────┐   PIR fires        ┌───────────────┴──────┐
│  TRAP mode   │──────────────────► │    RECORDING         │
│  (ring buf)  │                    │  flush pre-event buf │
│              │◄───────────────────│  record until idle   │
└──────────────┘  idle ≥ post_secs  └──────────────────────┘
        │                 ▲
  browser opens           │ browser closes /
  /video_feed             │ /api/stream/stop / watchdog
        │                 │
        ▼                 │
┌──────────────┐          │
│  LIVE mode   │──────────┘
│  (MJPEG)     │
└──────────────┘
```

**TRAP**: Picamera2 runs a `CircularOutput` at 1920×1080/30 fps. The ring buffer retains the last `pre_event_seconds` of video at all times.

**RECORDING**: Triggered by `GPIOManager.on_motion` → `CameraManager.trigger_recording()`. The ring buffer is flushed to an `.h264` file, recording continues until the PIR has been silent for `post_event_seconds`. Each new PIR event during a recording resets the silence timer, extending the clip. After recording, `ffmpeg` wraps the raw bitstream into an `.mp4` container.

**LIVE**: When a browser opens `/video_feed`, the camera is torn down and restarted with a MJPEG encoder at 1280×720/15 fps. Frames are written to a thread-safe `MjpegOutput` buffer. A `threading.Timer` watchdog reverts to Trap mode if no client reads from the stream for `watchdog_seconds`.

**Transform (flip)**: `hflip`/`vflip` are libcamera config-time parameters — they cannot be changed via `set_controls()`. When flip settings change, the web server calls `camera_manager.restart_camera()` in a background thread, which runs `_teardown_camera()` + `_setup_trap_camera()` under the lock. This is separate from `stop()`/`start()` which would permanently set the `_stop_event`.

### Settings flow

1. The browser slider/checkbox sends a `PATCH`/`POST` to `/api/settings` after a 400 ms debounce (so rapid slider movement only produces one request).
2. `web_server._update_camera_settings()` validates and clamps each field, updates `_cfg` in memory, then calls:
   - `camera_manager.update_config(_cfg)` — rebuilds `_cam_controls` and calls `set_controls()` on the running camera (takes effect next frame).
   - `storage_manager.update_config(_cfg)` — for NAS/storage-related changes.
   - `_save_config(_cfg, config_path)` — atomically writes `config.yaml`.
3. Transform changes additionally trigger `camera_manager.restart_camera()` (background thread, 300 ms delay so the HTTP response returns first).

### Web request lifecycle

All routes require **HTTP Basic Auth**. The `_check_password` helper compares the supplied password against the bcrypt hash stored in `config.yaml`. The `/logout` route always returns `401 Unauthorized`, which causes the browser to clear its cached credentials — the only reliable way to "log out" of HTTP Basic Auth.

The Flask app runs in `threaded=True` mode so multiple API calls can be served concurrently. All camera access is protected by `CameraManager._lock`, so concurrent API calls cannot corrupt the camera state.

### NAS upload pipeline (`storage_manager.py`)

1. When a recording finishes, `CameraManager` calls `storage_mgr.handle_new_recording(path)`.
2. If `nas.enabled = true`, the file is pushed onto an `asyncio`-style queue (actually a `threading.Queue`) processed by a dedicated NAS upload thread.
3. The upload thread connects to the SMB share via `smbprotocol`, streams the file, then verifies the remote file size matches the local size.
4. If `delete_after_upload = true`, the local file is removed after successful verification.
5. Each recording's upload status is tracked in memory and exposed via `/api/recordings` so the dashboard can show a badge.

### Event-driven recording refresh (`index.html`)

The dashboard polls `/api/status` every 5 seconds. The status response includes `rec_latest_mtime` — the modification time of the newest recording file. The browser compares this against the last known value; only when it changes does it reload the recording list via `/api/recordings`. This avoids unnecessary list fetches while still showing new recordings promptly.

---

## Configuration (`config.yaml`)

```yaml
camera:
  af_mode: 2             # 0=Manual, 1=Auto, 2=Continuous
  af_range: 0            # 0=Normal, 1=Macro, 2=Full
  analogue_gain: 0.0     # 0=auto, 1–16=manual gain
  awb_mode: 0            # 0=Auto, 1=Incandescent, 2=Tungsten, 3=Fluorescent,
                         # 4=Indoor, 5=Daylight, 6=Cloudy, 7=Custom
  contrast: 1.6
  exposure_time: 0       # µs; 0=auto
  hdr: true
  hflip: false           # horizontal image flip (requires camera restart)
  vflip: false           # vertical image flip (requires camera restart)
  lens_position: 1.0     # diopters (manual AF only, af_mode: 0)
  night_vision: false    # disables AWB, sets ColourGains: [1.0, 1.0] for pure IR
  saturation: 0.6
  tuning_file: imx708_wide_noir.json   # Greyworld AWB for NoIR sensor

trap:
  framerate: 30
  pre_event_seconds: 3     # ring buffer length before PIR trigger
  post_event_seconds: 30   # idle time (no PIR) before recording stops
  resolution: [1920, 1080]

live:
  framerate: 15
  jpeg_quality: 85
  resolution: [1280, 720]
  watchdog_seconds: 180    # revert to Trap if stream is abandoned

pir:
  gpio_pin: 17
  pull_down: true

relay:
  active_high: true
  gpio_pin: 18

server:
  host: 0.0.0.0
  port: 8080
  users:
    - username: admin
      password_hash: <bcrypt hash>
      role: admin

storage:
  recordings_path: /recordings
  min_free_mb: 500      # delete oldest recordings below this threshold
  halt_free_mb: 100     # stop recording if critically low
  nas:
    enabled: false
    server: 192.168.1.x
    share: sharename
    remote_path: /wildlife/
    username: user
    password: pass
```

---

## Installation

### 1. System dependencies

```bash
sudo apt update
sudo apt install -y python3-picamera2 ffmpeg smbclient network-manager
```

> **Note:** Use the system `python3-picamera2` package — do **not** install via pip, as it requires system-level libcamera integration.

### 2. Clone and create virtualenv

```bash
cd /home/one0one
git clone <repo-url> wildlife-cam-pi4
cd wildlife-cam-pi4
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
```

The `--system-site-packages` flag is required so the virtualenv can see the system `picamera2` and `libcamera` packages.

### 3. Create recordings directory

```bash
sudo mkdir -p /recordings
sudo chown $USER:$USER /recordings
```

### 4. Set admin password

```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
```

Paste the output into `config.yaml` under `server.users[0].password_hash`.

### 5. Run as a systemd service

Create `/etc/systemd/system/wildlife-cam.service`:

```ini
[Unit]
Description=O.W.L. Wildlife Camera
After=network.target

[Service]
Type=simple
User=one0one
WorkingDirectory=/home/one0one/wildlife-cam-pi4/src
ExecStart=/home/one0one/wildlife-cam-pi4/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wildlife-cam.service
```

View logs:

```bash
sudo journalctl -u wildlife-cam -f
```

---

## Web Interface

Access the UI at `http://<pi-ip>:8080`. Log in with the credentials from `config.yaml`.

### Dashboard (`/`)

- Status bar: CPU temperature, voltage, disk usage, camera state
- Recording library with per-file upload-status badges, inline playback, download
- Bulk actions: select all / delete / download as ZIP / upload to NAS
- Relay (IR floodlight) manual toggle
- Manual PIR trigger button

### Live Stream (`/stream`)

MJPEG preview plus a camera settings panel. All controls apply **immediately** on change (400 ms debounce) — no save button needed for the preview. The **Speichern** button persists current values to `config.yaml`.

| Control | Description |
|---|---|
| Autofocus mode | Manual / Auto / Continuous |
| AF range | Normal / Macro / Full |
| Lens position | Diopters (manual focus only) |
| AWB mode | Auto / Incandescent / Tungsten / … |
| Contrast / Saturation | 0–32, default 1.0 |
| Analogue gain | 0=auto, 1–16 |
| Shutter speed | µs, 0=auto |
| HDR | Toggle |
| Night vision | Disables AWB for pure IR imaging |
| H-Flip / V-Flip | Mirror the image (requires brief camera restart) |

A focus range reference table is shown below the lens position slider.

### Settings (`/settings`)

| Section | Description |
|---|---|
| Trap Timing | Pre-event buffer length and post-event idle timeout |
| NAS / Samba | Server, share, credentials, enable/disable auto-upload |
| WiFi | Scan for networks and connect via nmcli |
| Eigenes Passwort | Change the current user's password |
| Benutzerverwaltung | Create and delete user accounts (admin only) |
| System Health | CPU, RAM, temperature and voltage with peak tracking |
| Anwendungslog | Live tail of `wildlife_cam.log` |

---

## AWB and NoIR Camera Notes

The Camera Module 3 Wide NoIR has no IR-cut filter. Without a tuning file the AWB algorithm sees IR light and produces inaccurate colours in daylight. Loading `imx708_wide_noir.json` activates the **Greyworld AWB** algorithm tuned for this sensor, as recommended by Raspberry Pi.

For pure IR night-vision (IR illuminator, no visible light) enable **Night Vision** mode in the settings — this disables AWB entirely and sets `ColourGains: [1.0, 1.0]`.

If you use the standard Camera Module 3 (with IR-cut filter), set `tuning_file: imx708_wide.json` in `config.yaml`.

---

## GPIO Wiring

| Signal | GPIO (BCM) | Notes |
|---|---|---|
| PIR output | 17 | Input with pull-down; active high |
| Relay control | 18 | Active high by default |

