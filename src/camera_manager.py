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

        night_vision = cam_cfg.get("night_vision", False)
        self._cam_controls = {
            "AfMode": cam_cfg.get("af_mode", 1),
            "AfRange": cam_cfg.get("af_range", 0),
            "Contrast": cam_cfg.get("contrast", 1.0),
            "Saturation": cam_cfg.get("saturation", 1.0),
        }
        if night_vision:
            # Disable colour correction for pure IR imaging (NoIR module)
            self._cam_controls["AwbEnable"] = False
            self._cam_controls["ColourGains"] = (1.0, 1.0)
        else:
            self._cam_controls["AwbMode"] = cam_cfg.get("awb_mode", 0)
            # AwbEnable must be True for AwbMode to take effect
            self._cam_controls["AwbEnable"] = True
        exposure = cam_cfg.get("exposure_time", 0)
        if exposure > 0:
            self._cam_controls["ExposureTime"] = exposure
        else:
            # exposure_time == 0 means auto — explicitly re-enable AEC/AGC
            self._cam_controls["AeEnable"] = True
        gain = cam_cfg.get("analogue_gain", 0.0)
        if gain > 0:
            self._cam_controls["AnalogueGain"] = gain
        # gain == 0 means auto — AeEnable above covers it
        lens = cam_cfg.get("lens_position", None)
        if lens is not None and cam_cfg.get("af_mode", 1) == 0:
            self._cam_controls["LensPosition"] = lens

        self._transform = Transform(
            hflip=bool(cam_cfg.get("hflip", False)),
            vflip=bool(cam_cfg.get("vflip", False)),
        )

    def update_config(self, config: dict) -> None:
        """Hot-reload settings; applies run-time controls to the running camera.

        Note: Transform (hflip/vflip) changes are NOT applied here — those
        require a full restart via restart_camera().
        """
        with self._lock:
            self._config = config
            self._apply_config(config)
            if self._cam and self._state in (CameraState.TRAP, CameraState.LIVE,
                                             CameraState.RECORDING):
                try:
                    self._cam.set_controls(self._cam_controls)
                except Exception:
                    logger.exception("Failed to apply hot-reload controls")

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
            self._teardown_camera()
            ok = self._setup_live_camera()
            if ok:
                self._state = CameraState.LIVE
                self._live_clients = 1
                self._reset_watchdog()
                logger.info("LIVE mode active.")
            return ok

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
        """Configure and start Picamera2 in CircularOutput (ring buffer) mode."""
        tuning = self._load_tuning()
        cam = Picamera2(0, tuning=tuning) if tuning is not None else Picamera2(0)
        video_cfg = cam.create_video_configuration(
            main={"size": self.TRAP_RESOLUTION, "format": "BGR888"},
            controls={"FrameRate": self.TRAP_FPS},
            transform=self._transform,
        )
        cam.configure(video_cfg)
        cam.set_controls(self._cam_controls)

        encoder = H264Encoder(bitrate=4_000_000)
        buffer_secs = self.PRE_EVENT_SECONDS + 2  # small headroom
        circ = CircularOutput(buffersize=buffer_secs * self.TRAP_FPS)

        cam.start_recording(encoder, circ)
        self._cam = cam
        self._circ_output = circ
        self._encoder = encoder
        logger.debug("Trap camera started: %s @%dfps", self.TRAP_RESOLUTION, self.TRAP_FPS)

    # ------------------------------------------------------------------ #
    # Internal — Live Mode setup                                           #
    # ------------------------------------------------------------------ #

    def _setup_live_camera(self) -> bool:
        try:
            tuning = self._load_tuning()
            cam = Picamera2(0, tuning=tuning) if tuning is not None else Picamera2(0)
            video_cfg = cam.create_video_configuration(
                main={"size": self.LIVE_RESOLUTION, "format": "BGR888"},
                controls={"FrameRate": self.LIVE_FPS},
                transform=self._transform,
            )
            cam.configure(video_cfg)
            cam.set_controls(self._cam_controls)

            self._mjpeg_output = MjpegOutput()
            encoder = JpegEncoder(q=self._jpeg_quality)
            cam.start_recording(encoder, FileOutput(self._mjpeg_output))
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
