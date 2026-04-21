# LASTENHEFT — Wildlife Cam Pi 4

**Project:** Wildlife Camera System  
**Hardware:** Raspberry Pi 4 (2 GB), Camera Module 3 Wide NoIR (IMX708)  
**OS:** Debian Bookworm (Raspberry Pi OS 64-bit)  
**Revision:** 1.0 — 2026-04-21  
**Status:** Validated ✅

---

## 1. Hardware Validation Results

| Component | Result | Detail |
|-----------|--------|--------|
| Camera IMX708 | ✅ PASS | `/base/soc/i2c0mux/i2c@1/imx708@1a`, 4608×2592, NoIR tuning |
| PDAF AfMode | ✅ PASS | Range (0-2): Manual / Auto / Continuous |
| PDAF LensPosition | ✅ PASS | Range 0.0–32.0 diopters |
| PDAF AfRange | ✅ PASS | Normal / Macro / Full |
| AWB GreyWorld | ✅ PASS | Mode index 1 |
| PIR GPIO17 | ✅ PASS | Pull-down, idle LOW |
| Relay GPIO18 | ✅ PASS | Active-high, toggled successfully |

---

## 2. Functional Requirements

### FR-01 — Trap Mode (Default)
- System MUST be in Trap Mode on boot.
- Camera runs a **3-second RAM ring buffer** at 1920×1080 H.264 via Picamera2 `CircularOutput`.
- PIR sensor (GPIO 17) is polled / interrupt-driven.
- On PIR trigger: save the 3 s pre-event buffer + continue recording for a configurable post-event duration (default 10 s), then save to `/recordings/`.

### FR-02 — Live Mode (Web Stream)
- Triggered exclusively by the Web UI.
- Trap Mode MUST stop completely and release the camera before Live Mode starts.
- MJPEG stream served over HTTP at configurable quality and resolution.
- Watchdog: auto-revert to Trap Mode after **180 s** of streaming or on client disconnect.

### FR-03 — Exclusive Camera Access (State Machine)
- The IMX708 is a **single-owner resource**.
- Only one mode (TRAP or LIVE) may hold the camera at any time.
- Transitions are managed by `CameraManager` which is the single arbiter.

### FR-04 — Autofocus (PDAF)
- Web UI exposes:
  - `AfMode`: Manual / Auto / Continuous
  - `LensPosition`: 0.0–32.0 (manual mode only)
  - `AfRange`: Normal / Macro / Full
- Settings persisted to `config.yaml`.

### FR-05 — NoIR Optimizations
- AWB Greyworld mode selectable via Web UI (index 1 in libcamera).
- Sunlight AWB mode (index 4) also selectable.
- Settings persisted.

### FR-06 — Advanced Camera Controls
- ISO (AnalogueGain), Shutter Speed (ExposureTime), Contrast, Saturation all user-adjustable.
- HDR toggle (enable/disable via `Hdr` libcamera control where supported).

### FR-07 — Relay / Floodlight Control
- GPIO 18 controls an external LED IR floodlight via relay.
- Web UI toggle: ON / OFF.
- Auto-off: relay disabled when reverting from Live to Trap Mode.

### FR-08 — Local Storage
- All recordings saved to `/recordings/` as H.264 `.mp4` container files.
- Filename format: `YYYYMMDD_HHMMSS_<trigger>.mp4`.
- Minimum free disk space guard: warn when < 500 MB, halt recording when < 100 MB.

### FR-09 — NAS Upload
- Background upload via `smbprotocol` to a configurable SMB share.
- Logic: **Local Save → Verify NAS Upload → Delete Local File**.
- On NAS failure: retain local file and retry on next recording cycle.

### FR-10 — Web UI & Authentication
- Served by Flask on port 8080.
- HTTP Basic Authentication (username + hashed password in `config.yaml`).
- Pages: Dashboard, Live Stream, Settings, Recording List.

### FR-11 — Settings Persistence
- All runtime settings (AF mode, AWB, ISO, shutter, relay, NAS credentials) saved to `config.yaml` on every change.

---

## 3. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-01 | Python ≥ 3.11. No blocking I/O on the camera thread. |
| NFR-02 | Ring buffer must not drop frames under normal CPU load. |
| NFR-03 | Transition from Trap → Live and back must complete in < 3 s. |
| NFR-04 | Web server must respond within 500 ms for all non-streaming endpoints. |
| NFR-05 | All secrets (passwords, NAS credentials) stored hashed or encrypted; never in plaintext in logs. |
| NFR-06 | All GPIO must be safely released (cleanup) on SIGTERM / SIGINT. |
| NFR-07 | NAS upload runs in a separate `ThreadPoolExecutor` thread; must not block recording. |
| NFR-08 | Log rotation: max 10 MB per log file, 3 backups. |

---

## 4. Hardware Pinout

```
Raspberry Pi 4 GPIO (BCM numbering)
────────────────────────────────────
GPIO 17  ──── PIR Sensor DATA out (5V tolerant via divider)
               Pull-down enabled in software.
               Trigger level: HIGH = motion detected.

GPIO 18  ──── Relay IN (active HIGH → relay closes → LED ON)
               3.3 V logic drives optocoupler on relay board.

CSI-2   ──── Camera Module 3 Wide NoIR ribbon cable
              Sensor: IMX708, f/1.8, 120° FoV, PDAF
              Tuning: /usr/share/libcamera/ipa/rpi/vc4/imx708_wide_noir.json
```

---

## 5. State Machine — Exclusive Camera Access

```
                        ┌─────────────────────────────┐
          BOOT          │                             │
            │           │         TRAP MODE           │
            ▼           │  ┌────────────────────────┐ │
      ┌──────────┐       │  │ Picamera2 ring buffer  │ │
      │  INIT    │──────▶│  │ PIR GPIO17 active      │ │
      └──────────┘       │  │ 1920×1080 H.264 30fps  │ │
                         │  └────────────────────────┘ │
                         │         │        ▲           │
                         │  PIR    │        │ Watchdog  │
                         │ trigger │        │ timeout   │
                         │         ▼        │ OR client │
                         │  ┌─────────────┐ │ disconnect│
                         │  │  RECORDING  │ │           │
                         │  │  Save .mp4  │ │           │
                         │  │  NAS upload │ │           │
                         │  └──────┬──────┘ │           │
                         │         │        │           │
                         └─────────┼────────┼───────────┘
                                   │        │
                     WEB UI        │        │
                   /stream req     │        │
                                   ▼        │
                         ┌─────────────────────────────┐
                         │                             │
                         │         LIVE MODE           │
                         │  ┌────────────────────────┐ │
                         │  │ Picamera2 MJPEG stream  │ │
                         │  │ PIR GPIO17 inactive     │ │
                         │  │ Relay can be toggled    │ │
                         │  └────────────────────────┘ │
                         │                             │
                         └─────────────────────────────┘

  Transition rules:
  ─────────────────
  TRAP → LIVE    : Web UI /stream endpoint called.
                   CameraManager stops Picamera2, joins encoder thread,
                   restarts in MJPEG mode.
  LIVE → TRAP    : /stop_stream called, watchdog fires (180 s),
                   or last SSE client disconnects.
                   CameraManager stops MJPEG, restarts ring buffer.
  TRAP → RECORD  : PIR GPIO17 goes HIGH. Internal state transition only;
                   camera does NOT restart (buffer is already running).
  RECORD → TRAP  : post_event_duration elapsed with no further PIR trigger.
```

---

## 6. Software Architecture

```
wildlife-cam-pi4/
├── LASTENHEFT.md           ← This document
├── config.yaml             ← Runtime configuration (AF, AWB, NAS, auth)
├── requirements.txt
├── src/
│   ├── main.py             ← Entry point, signal handling, orchestration
│   ├── camera_manager.py   ← State machine, Picamera2, ring buffer, MJPEG
│   ├── gpio_manager.py     ← PIR interrupt, Relay control (lgpio)
│   ├── storage_manager.py  ← Local save, NAS upload, disk guard
│   ├── web_server.py       ← Flask app, Basic Auth, REST API, MJPEG route
│   └── templates/
│       ├── index.html      ← Dashboard + settings form
│       └── stream.html     ← Live stream viewer
├── tests/
│   ├── test_camera.py      ← Camera PDAF/AWB self-test ✅
│   ├── test_pir.py         ← PIR GPIO17 self-test ✅
│   └── test_relay.py       ← Relay GPIO18 self-test ✅
├── recordings/             ← Local H.264 .mp4 files
└── logs/                   ← Rotating log files
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `lgpio` for GPIO (not RPi.GPIO) | lgpio is the recommended library on Bookworm; avoids kernel module issues |
| Picamera2 `CircularOutput` for ring buffer | Native zero-copy pre-event buffer in libcamera |
| Flask + `multipart/x-mixed-replace` for MJPEG | Broadest browser compatibility, no WebRTC complexity |
| `threading.Event` for state transitions | Ensures clean handover of camera resource between threads |
| `smbprotocol` for NAS | Pure-Python SMB2/3 client, no samba dependency |
| YAML for config | Human-readable, supports comments, easy to hand-edit on Pi |

---

## 7. Configuration Schema (`config.yaml`)

```yaml
server:
  host: "0.0.0.0"
  port: 8080
  username: "admin"
  password_hash: "<bcrypt hash>"

camera:
  af_mode: 1          # 0=Manual, 1=Auto, 2=Continuous
  af_range: 0         # 0=Normal, 1=Macro, 2=Full
  lens_position: 1.0  # diopters (manual mode only)
  awb_mode: 0         # 0=Auto, 1=Greyworld, 4=Sunlight
  analogue_gain: 1.0  # ISO proxy
  exposure_time: 0    # 0 = AE auto, else microseconds
  contrast: 1.0
  saturation: 1.0
  hdr: false

trap:
  pre_event_seconds: 3
  post_event_seconds: 10
  resolution: [1920, 1080]
  framerate: 30

live:
  watchdog_seconds: 180
  resolution: [1280, 720]
  framerate: 15
  jpeg_quality: 85

relay:
  gpio_pin: 18
  active_high: true

pir:
  gpio_pin: 17
  pull_down: true

storage:
  recordings_path: "/recordings"
  min_free_mb: 500
  halt_free_mb: 100
  nas:
    enabled: false
    server: "192.168.1.100"
    share: "wildlife"
    username: "pi"
    password: ""
    remote_path: "/"
```

---

*End of Lastenheft*
