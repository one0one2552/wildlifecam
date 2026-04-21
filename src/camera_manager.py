"""
camera_manager.py — Exclusive Camera Access State Machine

States:
  TRAP     : Picamera2 running CircularOutput ring buffer. PIR active.
  LIVE     : Picamera2 streaming MJPEG. Watchdog armed.
  RECORDING: Transient sub-state of TRAP; camera continues, saving to disk.
  STOPPING : Transition state while camera is being released.

The CameraManager is the single arbiter of the IMX708.  All access goes
through request_live_mode() / release_live_mode().
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
        self._lock = threading.Lock()
        self._state = CameraState.STOPPING
        self._cam: Optional[Picamera2] = None
        self._mjpeg_output: Optional[MjpegOutput] = None
        self._watchdog_timer: Optional[threading.Timer] = None
        self._live_clients: int = 0
        self._stop_event = threading.Event()

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

        self._cam_controls = {
            "AfMode": cam_cfg.get("af_mode", 1),
            "AfRange": cam_cfg.get("af_range", 0),
            "AwbMode": cam_cfg.get("awb_mode", 0),
            "Contrast": cam_cfg.get("contrast", 1.0),
            "Saturation": cam_cfg.get("saturation", 1.0),
        }
        exposure = cam_cfg.get("exposure_time", 0)
        if exposure > 0:
            self._cam_controls["ExposureTime"] = exposure
        gain = cam_cfg.get("analogue_gain", 0.0)
        if gain > 0:
            self._cam_controls["AnalogueGain"] = gain
        lens = cam_cfg.get("lens_position", None)
        if lens is not None and cam_cfg.get("af_mode", 1) == 0:
            self._cam_controls["LensPosition"] = lens

    def update_config(self, config: dict) -> None:
        """Hot-reload settings; applies controls to a running camera if possible."""
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
        Must only be called in TRAP state.
        """
        with self._lock:
            if self._state != CameraState.TRAP:
                logger.debug("PIR trigger ignored (not in TRAP mode)")
                return
            self._state = CameraState.RECORDING

        threading.Thread(target=self._recording_worker, daemon=True).start()

    def apply_controls(self, controls: dict) -> None:
        """Apply a dict of libcamera controls to the running camera."""
        with self._lock:
            if self._cam:
                self._cam.set_controls(controls)
                logger.debug("Applied controls: %s", controls)

    # ------------------------------------------------------------------ #
    # Internal — Trap Mode setup                                           #
    # ------------------------------------------------------------------ #

    def _enter_trap_mode(self) -> None:
        with self._lock:
            self._teardown_camera()
            self._setup_trap_camera()
            self._state = CameraState.TRAP
        logger.info("TRAP mode active.")

    def _setup_trap_camera(self) -> None:
        """Configure and start Picamera2 in CircularOutput (ring buffer) mode."""
        cam = Picamera2(0)
        video_cfg = cam.create_video_configuration(
            main={"size": self.TRAP_RESOLUTION, "format": "BGR888"},
            controls={"FrameRate": self.TRAP_FPS},
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
            cam = Picamera2(0)
            video_cfg = cam.create_video_configuration(
                main={"size": self.LIVE_RESOLUTION, "format": "BGR888"},
                controls={"FrameRate": self.LIVE_FPS},
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

            time.sleep(self.POST_EVENT_SECONDS)

            with self._lock:
                if self._cam:
                    self._circ_output.stop()
                self._state = CameraState.TRAP

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
