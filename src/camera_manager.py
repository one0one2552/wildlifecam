"""
camera_manager.py — IMX708 Camera State Machine

The CameraManager is the sole owner of the Picamera2 instance.  All
access to the camera hardware is serialised through a threading.Lock.

States
------
TRAP      Picamera2 is running a CircularOutput ring buffer.  Recordings
          are triggered by the PIR sensor via trigger_recording().
RECORDING Sub-state of TRAP.  The circular buffer is flushed to disk and
          the camera continues recording until post_event_seconds of PIR
          silence.  Re-triggering the PIR extends the recording window.
LIVE      Camera is reconfigured as a MJPEG stream for the browser preview.
          A watchdog timer reverts to TRAP if the stream is abandoned.
STOPPING  Transient state while the Picamera2 object is being torn down.

Exposure / AE rules
--------------------
exposure_time == 0, analogue_gain == 0  →  Full auto (AeEnable=True)
exposure_time >  0, analogue_gain == 0  →  Manual shutter, float gain
exposure_time == 0, analogue_gain >  0  →  Auto shutter, fixed gain
exposure_time >  0, analogue_gain >  0  →  Fully manual (AeEnable=False)

When AeEnable is False, the EV compensation (ExposureValue) and AE
metering/exposure mode controls are NOT sent — they have no effect and
can confuse the ISP.

FrameDurationLimits
--------------------
Trap mode: (min_frame_us, max_exposure_us) — allows the AE algorithm
to use longer exposures in low light without flickering from a locked
framerate.  max_exposure_us is configurable (default 200 000 µs = 5 fps).

Live mode: (frame_us, frame_us) — fixed framerate for a stable preview.

HDR
----
Camera Module 3 (IMX708): sensor-level HDR must be toggled via the
IMX708 helper class BEFORE creating the Picamera2 object.  The
HdrMode control has no effect on Pi 4 hardware.

Pi 5: HdrMode is set as a runtime camera control.

Config-time vs run-time settings
----------------------------------
Most camera controls (contrast, gain, AWB, etc.) can be applied at
run-time via set_controls() and take effect on the next frame.
The image Transform (hflip / vflip) is a *configuration-time* parameter
that requires a full camera teardown + restart to take effect.  Use
restart_camera() after updating hflip/vflip in the config.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Optional

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder, JpegEncoder
from picamera2.outputs import CircularOutput, FileOutput  # FileOutput used for MJPEG
from libcamera import Transform

logger = logging.getLogger(__name__)

JPEG_QUALITY_DEFAULT = 85


def _save_pir_graph(
    recording_path: Path,
    pir_log: list,
    trigger_time: float,
    pre_event_seconds: float,
) -> None:
    """Generate a step-plot of the PIR signal and save it as a JPEG alongside the recording.

    The X axis is time relative to the trigger moment (negative = pre-event).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")          # headless — no display needed
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError:
        logger.warning("matplotlib not available — skipping PIR graph")
        return

    if not pir_log:
        logger.debug("PIR log empty — skipping graph")
        return

    times  = [t - trigger_time for t, _ in pir_log]
    values = [v for _, v in pir_log]

    fig, ax = plt.subplots(figsize=(10, 2.8))
    fig.patch.set_facecolor("#0A2540")
    ax.set_facecolor("#0A2540")

    ax.step(times, values, where="post", color="#00E5FF", linewidth=1.5)
    ax.fill_between(times, values, step="post", alpha=0.25, color="#00E5FF")

    # Trigger marker
    ax.axvline(0, color="#FF4444", linewidth=1.2, linestyle="--", label="Trigger")

    # Pre-event shading
    ax.axvspan(times[0], -pre_event_seconds, alpha=0.08, color="#FFFFFF",
               label=f"Pre-event ({pre_event_seconds:.0f}s)")

    ax.set_xlim(times[0], times[-1])
    ax.set_ylim(-0.05, 1.15)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["LOW", "HIGH"], color="#A8D8FF", fontsize=8)
    ax.set_xlabel("Zeit relativ zu Auslösung (s)", color="#A8D8FF", fontsize=8)
    ax.tick_params(colors="#A8D8FF", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#2A4560")

    ts_str = recording_path.stem.replace("_pir", "").replace("_", " ")
    ax.set_title(f"PIR-Signal — {ts_str}", color="#FFFFFF", fontsize=9, pad=6)
    ax.xaxis.set_minor_locator(ticker.MultipleLocator(1))
    ax.grid(True, which="both", color="#2A4560", linewidth=0.5)
    ax.legend(fontsize=7, framealpha=0.2, labelcolor="#A8D8FF")

    graph_path = recording_path.with_suffix(".jpg")
    fig.savefig(str(graph_path), dpi=100, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("PIR graph saved: %s", graph_path)


class CameraState(Enum):
    TRAP = auto()
    LIVE = auto()
    RECORDING = auto()
    STOPPING = auto()


class MjpegOutput(io.BufferedIOBase):
    """Thread-safe frame buffer consumed by the MJPEG Flask route."""

    def __init__(self) -> None:
        self._frame: bytes = b""
        self._condition = threading.Condition()

    def write(self, buf: bytes) -> int:
        with self._condition:
            self._frame = buf
            self._condition.notify_all()
        return len(buf)

    def get_frame(self, timeout: float = 2.0) -> bytes:
        """Block until a new JPEG frame is available, then return it."""
        with self._condition:
            self._condition.wait(timeout)
            return self._frame


class CameraManager:
    """
    Manages the Picamera2 instance lifecycle as a state machine.

    Thread-safety: all public methods acquire _lock before mutating state.
    """

    WATCHDOG_SECONDS = 180
    LIVE_RESOLUTION = (1280, 720)
    TRAP_RESOLUTION = (1920, 1080)
    TRAP_FPS = 30
    LIVE_FPS = 15
    PRE_EVENT_SECONDS = 3
    POST_EVENT_SECONDS = 10
    RECORDINGS_DIR = Path("/recordings")

    def __init__(self, config: dict, on_pir_trigger: Optional[Callable] = None) -> None:
        self._config = config
        self._on_pir_trigger = on_pir_trigger
        self._relay_callback: Optional[Callable[[bool], None]] = None
        # Injected by main.py after both managers are constructed
        self._pir_history_cb: Optional[Callable[[float], list]] = None
        self._lock = threading.Lock()
        self._state = CameraState.STOPPING
        self._cam: Optional[Picamera2] = None
        self._mjpeg_output: Optional[MjpegOutput] = None
        self._watchdog_timer: Optional[threading.Timer] = None
        self._live_clients: int = 0
        self._stop_event = threading.Event()
        self._last_pir_time: float = 0.0

        self.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        self._apply_config(config)

    # ------------------------------------------------------------------ #
    # Config helpers                                                       #
    # ------------------------------------------------------------------ #

    def _apply_config(self, cfg: dict) -> None:
        cam_cfg = cfg.get("camera", {})
        trap_cfg = cfg.get("trap", {})
        live_cfg = cfg.get("live", {})

        self.WATCHDOG_SECONDS = live_cfg.get("watchdog_seconds", 180)
        self.PRE_EVENT_SECONDS = trap_cfg.get("pre_event_seconds", 3)
        self.POST_EVENT_SECONDS = trap_cfg.get("post_event_seconds", 10)
        r = trap_cfg.get("resolution", [1920, 1080])
        self.TRAP_RESOLUTION = tuple(r)
        self.TRAP_FPS = trap_cfg.get("framerate", 30)
        r = live_cfg.get("resolution", [1280, 720])
        self.LIVE_RESOLUTION = tuple(r)
        self.LIVE_FPS = live_cfg.get("framerate", 15)
        self._jpeg_quality = live_cfg.get("jpeg_quality", JPEG_QUALITY_DEFAULT)
        recordings = cfg.get("storage", {}).get("recordings_path", "/recordings")
        self.RECORDINGS_DIR = Path(recordings)
        self.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

        # Tuning file for NoIR cameras — enables Greyworld AWB algorithm
        self._tuning_file: Optional[str] = cam_cfg.get("tuning_file", "imx708_wide_noir.json")

        # Max exposure in µs for trap mode FrameDurationLimits.
        # Allows AE to use long exposures in low light without framerate flicker.
        # Default 200 000 µs = max 5 fps slow-exposure frames.
        self._max_exposure_us: int = int(cam_cfg.get("max_exposure_us", 200_000))

        # HDR state for sensor-level toggling (IMX708 / Camera Module 3)
        self._hdr_enabled: bool = bool(cam_cfg.get("hdr", False))

        # PIR graph feature
        self._pir_graph_enabled: bool = bool(
            cfg.get("pir", {}).get("save_graph", False)
        )

        self._transform = Transform(
            hflip=bool(cam_cfg.get("hflip", False)),
            vflip=bool(cam_cfg.get("vflip", False)),
        )

        self._cam_controls = self._build_controls(cam_cfg)

    def _build_controls(self, cam_cfg: dict) -> dict:
        """
        Build a clean libcamera control dict from the camera config section.

        Rules:
        - Auto/manual exposure are mutually exclusive; mixing them produces flicker.
        - AE metering/mode/EV only apply when AeEnable is True.
        - AwbMode only applies when AwbEnable is True.
        - ColourGains completely disable AWB when set.
        - LensPosition only applies in Manual AF mode.
        """
        controls: dict = {}

        exposure_time = int(cam_cfg.get("exposure_time", 0))   # 0 = auto
        analogue_gain = float(cam_cfg.get("analogue_gain", 0.0))  # 0 = auto
        night_vision  = bool(cam_cfg.get("night_vision", False))

        # ── Exposure / AE ──────────────────────────────────────────────────
        # Libcamera only supports two reliable modes:
        #   a) Full auto  — AeEnable=True, no manual ET or gain override
        #   b) Full manual — AeEnable=False, BOTH ExposureTime AND AnalogueGain set
        # Hybrid modes (manual ET only, or manual gain only) are unreliable:
        #   - manual ET + auto gain: AE is disabled but gain defaults to 1.0 → dark image
        #   - manual gain + auto ET: AE continuously overrides the gain setting
        # Therefore: only enable manual mode when BOTH values are explicitly set.
        manual_shutter = exposure_time > 0
        manual_gain    = analogue_gain > 0.0

        if manual_shutter and manual_gain:
            # Fully manual — AE must be off to avoid fighting user values
            controls["AeEnable"]     = False
            controls["ExposureTime"] = exposure_time
            controls["AnalogueGain"] = analogue_gain

        else:
            # Full auto (covers: both=0, ET-only, gain-only)
            # When only one is set the user likely wants auto; if they want
            # manual they must set both — this is clearly explained in the UI.
            controls["AeEnable"]         = True
            controls["AeMeteringMode"]   = int(cam_cfg.get("ae_metering_mode", 0))
            controls["AeExposureMode"]   = int(cam_cfg.get("ae_exposure_mode", 0))
            controls["AeConstraintMode"] = int(cam_cfg.get("ae_constraint_mode", 0))
            controls["ExposureValue"]    = float(cam_cfg.get("exposure_value", 0.0))
            self._apply_flicker_controls(controls, cam_cfg)

        # ── AWB / White Balance ────────────────────────────────────────────
        if night_vision:
            # Pure IR mode: disable AWB, apply fixed colour gains
            controls["AwbEnable"]   = False
            red_gain  = float(cam_cfg.get("colour_gain_red",  1.0))
            blue_gain = float(cam_cfg.get("colour_gain_blue", 1.0))
            controls["ColourGains"] = (red_gain, blue_gain)
        else:
            awb_mode = int(cam_cfg.get("awb_mode", 0))
            controls["AwbEnable"] = True
            controls["AwbMode"]   = awb_mode

        # ── Image quality ──────────────────────────────────────────────────
        controls["Contrast"]           = float(cam_cfg.get("contrast",    1.0))
        controls["Saturation"]         = float(cam_cfg.get("saturation",  1.0))
        controls["Sharpness"]          = float(cam_cfg.get("sharpness",   1.0))
        controls["Brightness"]         = float(cam_cfg.get("brightness",  0.0))
        controls["NoiseReductionMode"] = int(cam_cfg.get("noise_reduction_mode", 1))

        # ── Autofocus ──────────────────────────────────────────────────────
        # Default: Manual AF (0) — no lens hunting.
        # Continuous AF (2) on IMX708 sweeps the full focal range causing
        # severe image instability that looks like flickering.
        af_mode = int(cam_cfg.get("af_mode", 0))
        controls["AfMode"]  = af_mode
        controls["AfRange"] = int(cam_cfg.get("af_range", 0))
        controls["AfSpeed"] = int(cam_cfg.get("af_speed", 0))

        if af_mode == 0:  # Manual AF — set lens position
            lens_pos = cam_cfg.get("lens_position", 0.0)
            controls["LensPosition"] = float(lens_pos)
        elif af_mode == 1:  # Auto (one-shot) — don't set LensPosition
            pass

        # ── Pi-5 ISP-level HDR (not applicable to Camera Module 3 / Pi 4) ─
        # Sensor-level HDR for IMX708 is handled separately via _set_sensor_hdr().
        # We still try to set the HdrMode control here; on Pi 4 it will be
        # silently rejected by libcamera, which is harmless.
        hdr_val = cam_cfg.get("hdr", False)
        if isinstance(hdr_val, bool):
            hdr_int = 1 if hdr_val else 0  # 0=Off, 1=SingleExposure
        else:
            hdr_int = max(0, min(4, int(hdr_val)))
        controls["HdrMode"] = hdr_int

        return controls

    @staticmethod
    def _apply_flicker_controls(controls: dict, cam_cfg: dict) -> None:
        """
        Add AeFlickerMode / AeFlickerPeriod to *controls* based on config.

        flicker_avoidance_hz: 0=off (default), 50=50 Hz, 60=60 Hz,
                              100=100 Hz, 120=120 Hz.
        Typical values for mains-light flicker: 50 or 60 Hz depending on
        the local power grid (Europe=50, US=60).  Use 100/120 for fixtures
        running at twice the line frequency (e.g. LED strips).
        """
        hz = int(cam_cfg.get("flicker_avoidance_hz", 0))
        if hz > 0:
            controls["AeFlickerMode"]   = 1  # FlickerManual
            controls["AeFlickerPeriod"] = max(1, int(1_000_000 // hz))
        else:
            controls["AeFlickerMode"] = 0  # FlickerOff

    # ------------------------------------------------------------------ #
    # HDR — Sensor-level helper (Camera Module 3 / IMX708)                #
    # ------------------------------------------------------------------ #

    def _set_sensor_hdr(self) -> None:
        """
        Toggle sensor-level HDR on Camera Module 3 (Sony IMX708).

        This must be called BEFORE creating the Picamera2() object.
        On Pi 5 or non-IMX708 cameras this is a no-op (the Pi-5 HDR is
        controlled via the HdrMode runtime control instead).
        """
        try:
            from picamera2.devices.imx708 import IMX708  # type: ignore
            with IMX708(0) as cam:
                cam.set_sensor_hdr_mode(self._hdr_enabled)
            logger.info("IMX708 sensor HDR %s", "enabled" if self._hdr_enabled else "disabled")
        except Exception as exc:
            # Not an IMX708, or driver not available — silently skip
            logger.debug("IMX708 sensor HDR toggle skipped: %s", exc)

    def update_config(self, config: dict) -> None:
        """Hot-reload settings; applies run-time controls to the running camera.

        Note: Transform (hflip/vflip) changes and HDR changes are NOT applied
        here — those require a full camera restart via restart_camera().
        """
        with self._lock:
            old_hdr = self._hdr_enabled
            self._config = config
            self._apply_config(config)
            if self._cam and self._state in (CameraState.TRAP, CameraState.LIVE,
                                             CameraState.RECORDING):
                try:
                    self._cam.set_controls(self._cam_controls)
                except Exception:
                    logger.exception("Failed to apply hot-reload controls")
            if old_hdr != self._hdr_enabled:
                logger.info("HDR changed — a camera restart is required for it to take effect")

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Enter Trap Mode. Call once at startup."""
        self._enter_trap_mode()

    def stop(self) -> None:
        """Release camera and clean up. Call on shutdown."""
        self._stop_event.set()
        self._cancel_watchdog()
        with self._lock:
            self._teardown_camera()
        logger.info("CameraManager stopped.")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> CameraState:
        return self._state

    @property
    def mjpeg_output(self) -> Optional[MjpegOutput]:
        return self._mjpeg_output

    def request_live_mode(self) -> bool:
        """
        Called by the web server when a client requests the MJPEG stream.
        Returns True if Live Mode is now active, False on error.
        """
        with self._lock:
            if self._state == CameraState.LIVE:
                self._live_clients += 1
                self._reset_watchdog()
                return True
            if self._state == CameraState.STOPPING:
                return False
            logger.info("Transitioning TRAP → LIVE")
            # Capture current AE state to pre-seed the live camera.
            # This prevents AEC hunting from scratch (visible as brightness flicker)
            # during the first few seconds of the live stream.
            ae_seed = self._capture_ae_seed()
            self._teardown_camera()
            ok = self._setup_live_camera(ae_seed=ae_seed)
            if ok:
                self._state = CameraState.LIVE
                self._live_clients = 1
                self._reset_watchdog()
                logger.info("LIVE mode active.")
            return ok

    def _capture_ae_seed(self) -> Optional[dict]:
        """Read ExposureTime+AnalogueGain from the running camera for AE pre-seeding."""
        if not self._cam:
            return None
        try:
            meta = self._cam.capture_metadata()
            et  = meta.get("ExposureTime")
            ag  = meta.get("AnalogueGain")
            if et and ag:
                return {"ExposureTime": int(et), "AnalogueGain": float(ag)}
        except Exception:
            pass
        return None

    def trigger_af(self) -> bool:
        """Trigger a one-shot autofocus cycle (AfMode=1 + AfTrigger=0).
        After focus is acquired, the lens holds position.
        Returns True if the command was accepted.
        """
        with self._lock:
            if not self._cam:
                return False
            try:
                self._cam.set_controls({"AfMode": 1, "AfTrigger": 0})
                logger.info("One-shot AF triggered")
                return True
            except Exception:
                logger.exception("trigger_af failed")
                return False

    def release_live_mode(self) -> None:
        """
        Called when a streaming client disconnects.
        Reverts to Trap Mode when the last client leaves.
        """
        with self._lock:
            self._live_clients = max(0, self._live_clients - 1)
            if self._live_clients == 0:
                logger.info("Last LIVE client disconnected → reverting to TRAP")
                self._transition_to_trap()

    def trigger_recording(self) -> None:
        """
        Called by GPIOManager when PIR fires.
        Saves the pre-event buffer and records post_event_seconds more.
        Re-triggering while already RECORDING extends the recording timer.
        """
        with self._lock:
            if self._state == CameraState.RECORDING:
                # Extend the recording: reset the inactivity timer
                self._last_pir_time = time.monotonic()
                logger.debug("PIR re-trigger: extending recording timer")
                return
            if self._state != CameraState.TRAP:
                logger.debug("PIR trigger ignored (state=%s)", self._state)
                return
            self._state = CameraState.RECORDING
            self._last_pir_time = time.monotonic()
            self._recording_trigger_time = self._last_pir_time  # for PIR graph
            if self._relay_callback:
                try:
                    self._relay_callback(True)
                except Exception:
                    logger.exception("Failed to turn relay ON at recording start")

        threading.Thread(target=self._recording_worker, daemon=True).start()

    def apply_controls(self, controls: dict) -> None:
        """Apply a dict of libcamera controls to the running camera."""
        with self._lock:
            if self._cam:
                self._cam.set_controls(controls)
                logger.debug("Applied controls: %s", controls)

    def restart_camera(self) -> None:
        """Cycle the camera to apply config-time settings (e.g. Transform/flip)."""
        logger.info("Restarting camera to apply config-time settings")
        with self._lock:
            self._teardown_camera()
            self._setup_trap_camera()
            self._state = CameraState.TRAP
            self._mjpeg_output = None
        logger.info("Camera restarted in TRAP mode.")

    # ------------------------------------------------------------------ #
    # Internal — Trap Mode setup                                           #
    # ------------------------------------------------------------------ #

    def _enter_trap_mode(self) -> None:
        with self._lock:
            self._teardown_camera()
            self._setup_trap_camera()
            self._state = CameraState.TRAP
        logger.info("TRAP mode active.")

    def _load_tuning(self) -> Optional[dict]:
        """Try to load the configured NoIR tuning file; return None on failure."""
        if not self._tuning_file:
            return None
        try:
            tuning = Picamera2.load_tuning_file(self._tuning_file)
            logger.debug("Loaded tuning file: %s", self._tuning_file)
            return tuning
        except Exception:
            logger.warning("Could not load tuning file '%s' — using default",
                           self._tuning_file)
            return None

    def _setup_trap_camera(self) -> None:
        """Configure and start Picamera2 in CircularOutput (ring buffer) mode.

        FrameDurationLimits are set as (min_frame_us, max_exposure_us) so the
        AE algorithm can extend exposures in low light without flickering from
        a rigidly locked framerate.
        """
        # Must be called before Picamera2() for IMX708 sensor-level HDR
        self._set_sensor_hdr()

        tuning = self._load_tuning()
        cam = Picamera2(0, tuning=tuning) if tuning is not None else Picamera2(0)

        min_frame_us = max(33_333, int(1_000_000 / self.TRAP_FPS))
        max_frame_us = max(min_frame_us, self._max_exposure_us)

        video_cfg = cam.create_video_configuration(
            main={"size": self.TRAP_RESOLUTION, "format": "BGR888"},
            controls={
                "FrameDurationLimits": (min_frame_us, max_frame_us),
                **self._cam_controls,
            },
            transform=self._transform,
        )
        cam.configure(video_cfg)
        # Apply again post-configure so runtime controls take immediate effect
        try:
            cam.set_controls(self._cam_controls)
        except Exception:
            logger.debug("set_controls after configure raised (non-fatal)")

        encoder = H264Encoder(bitrate=4_000_000)
        buffer_secs = self.PRE_EVENT_SECONDS + 2  # small headroom
        circ = CircularOutput(buffersize=buffer_secs * self.TRAP_FPS)

        cam.start_recording(encoder, circ)
        self._cam = cam
        self._circ_output = circ
        self._encoder = encoder
        logger.debug("Trap camera started: %s @%dfps (max_exp=%dµs)",
                     self.TRAP_RESOLUTION, self.TRAP_FPS, max_frame_us)

    # ------------------------------------------------------------------ #
    # Internal — Live Mode setup                                           #
    # ------------------------------------------------------------------ #

    def _setup_live_camera(self, ae_seed: Optional[dict] = None) -> bool:
        """Configure and start Picamera2 as a MJPEG stream.

        Live mode uses a *fixed* FrameDurationLimits so the preview framerate
        is stable — no variable-exposure jitter in the browser feed.

        ae_seed: optional {ExposureTime, AnalogueGain} from the preceding trap
        camera, used to pre-seed AE so it converges instantly rather than
        hunting for several seconds (visible as brightness flickering).
        """
        try:
            self._set_sensor_hdr()

            tuning = self._load_tuning()
            cam = Picamera2(0, tuning=tuning) if tuning is not None else Picamera2(0)

            frame_us = max(33_333, int(1_000_000 / self.LIVE_FPS))

            video_cfg = cam.create_video_configuration(
                main={"size": self.LIVE_RESOLUTION, "format": "BGR888"},
                controls={
                    "FrameDurationLimits": (frame_us, frame_us),
                    **self._cam_controls,
                },
                transform=self._transform,
            )
            cam.configure(video_cfg)
            try:
                cam.set_controls(self._cam_controls)
            except Exception:
                logger.debug("set_controls after configure raised (non-fatal)")

            self._mjpeg_output = MjpegOutput()
            encoder = JpegEncoder(q=self._jpeg_quality)
            cam.start_recording(encoder, FileOutput(self._mjpeg_output))

            # Pre-seed AE from trap-mode state: apply the previous exposure
            # values momentarily so the AEC converges in 1-2 frames instead
            # of hunting from defaults (which can look like flickering).
            if ae_seed and self._cam_controls.get("AeEnable", True):
                try:
                    cam.set_controls({
                        "AeEnable":     True,
                        "ExposureTime": ae_seed["ExposureTime"],
                        "AnalogueGain": ae_seed["AnalogueGain"],
                    })
                    logger.debug("AE pre-seeded: ET=%d AG=%.2f",
                                 ae_seed["ExposureTime"], ae_seed["AnalogueGain"])
                except Exception:
                    logger.debug("AE pre-seed failed (non-fatal)")

            self._cam = cam
            self._encoder = encoder
            return True
        except Exception:
            logger.exception("Failed to start live camera")
            self._mjpeg_output = None
            return False

    # ------------------------------------------------------------------ #
    # Internal — Recording worker                                          #
    # ------------------------------------------------------------------ #

    def _recording_worker(self) -> None:
        import subprocess as _sp
        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            h264_path = self.RECORDINGS_DIR / f"{timestamp}_pir.h264"
            mp4_path  = self.RECORDINGS_DIR / f"{timestamp}_pir.mp4"
            logger.info("Recording → %s", h264_path)

            with self._lock:
                if self._state != CameraState.RECORDING or not self._cam:
                    return
                # fileoutput setter accepts str/Path, opens file in 'wb' mode
                self._circ_output.fileoutput = str(h264_path)
                self._circ_output.start()

            # Wait until POST_EVENT_SECONDS have passed with no PIR activity.
            # Each PIR re-trigger resets _last_pir_time, extending the window.
            _poll = 0.5
            while True:
                time.sleep(_poll)
                with self._lock:
                    if self._state != CameraState.RECORDING:
                        return  # Cancelled externally (e.g. LIVE request)
                    elapsed = time.monotonic() - self._last_pir_time
                if elapsed >= self.POST_EVENT_SECONDS:
                    break
                logger.debug("Recording active — %.1fs since last PIR (limit %ds)",
                             elapsed, self.POST_EVENT_SECONDS)

            with self._lock:
                if self._cam:
                    self._circ_output.stop()
                self._state = CameraState.TRAP
            if self._relay_callback:
                try:
                    self._relay_callback(False)
                except Exception:
                    logger.exception("Failed to turn relay OFF at recording end")

            size_kb = h264_path.stat().st_size / 1024
            logger.info("H264 flushed: %s (%.0f KB)", h264_path.name, size_kb)

            # Wrap raw H264 bitstream into a proper MP4 container so
            # browsers can play it natively.
            result = _sp.run(
                ["ffmpeg", "-y", "-framerate", str(self.TRAP_FPS),
                 "-i", str(h264_path), "-c:v", "copy", str(mp4_path)],
                capture_output=True, timeout=120,
                stdin=_sp.DEVNULL,  # prevent ffmpeg from reading terminal → SIGTTIN
            )
            if result.returncode == 0:
                h264_path.unlink(missing_ok=True)
                out_path = mp4_path
            else:
                # ffmpeg failed: keep raw H264 as fallback
                out_path = h264_path
                logger.warning("ffmpeg wrap failed: %s",
                               result.stderr.decode(errors="replace")[:300])

            logger.info("Recording saved: %s", out_path)

            # Generate PIR graph if enabled
            if self._pir_graph_enabled and self._pir_history_cb:
                try:
                    trigger_time = getattr(self, "_recording_trigger_time", None)
                    if trigger_time is not None:
                        history_since = trigger_time - self.PRE_EVENT_SECONDS - 1.0
                        pir_log = self._pir_history_cb(history_since)
                        _save_pir_graph(
                            out_path, pir_log, trigger_time, self.PRE_EVENT_SECONDS
                        )
                except Exception:
                    logger.exception("PIR graph generation failed (non-fatal)")

            if self._on_pir_trigger:
                self._on_pir_trigger(str(out_path))

        except Exception:
            logger.exception("Error in recording worker")
            with self._lock:
                if self._state == CameraState.RECORDING:
                    self._state = CameraState.TRAP

    # ------------------------------------------------------------------ #
    # Internal — Watchdog                                                  #
    # ------------------------------------------------------------------ #

    def _reset_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog_timer = threading.Timer(
            self.WATCHDOG_SECONDS, self._watchdog_fire
        )
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _cancel_watchdog(self) -> None:
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None

    def _watchdog_fire(self) -> None:
        logger.warning("Live Mode watchdog fired after %ds → reverting to TRAP",
                       self.WATCHDOG_SECONDS)
        with self._lock:
            self._live_clients = 0
            self._transition_to_trap()

    # ------------------------------------------------------------------ #
    # Internal — Transition helpers (must be called under _lock)           #
    # ------------------------------------------------------------------ #

    def _transition_to_trap(self) -> None:
        """Tear down whatever is running and restart in Trap Mode."""
        self._cancel_watchdog()
        self._teardown_camera()
        self._setup_trap_camera()
        self._state = CameraState.TRAP
        self._mjpeg_output = None
        logger.info("Reverted to TRAP mode.")

    def _teardown_camera(self) -> None:
        """Stop and close the Picamera2 instance."""
        if self._cam:
            try:
                self._cam.stop_recording()
            except Exception:
                pass
            try:
                self._cam.close()
            except Exception:
                pass
            self._cam = None
        self._state = CameraState.STOPPING
