"""
main.py — Wildlife Cam Pi 4 entry point.

Starts:
  1. CameraManager  (Trap Mode ring buffer)
  2. GPIOManager    (PIR + Relay)
  3. StorageManager (disk guard + NAS)
  4. Flask web server

Handles SIGTERM / SIGINT for clean GPIO / camera release.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

import yaml

# Resolve paths relative to the project root (one level up from src/)
_SRC_DIR = Path(__file__).parent
_PROJECT_ROOT = _SRC_DIR.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "wildlife_cam.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        logging.warning("config.yaml not found — using defaults.")
        return {}
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def main() -> None:
    _setup_logging()
    logger = logging.getLogger("main")
    logger.info("=== Wildlife Cam Pi 4 starting ===")

    config = _load_config()

    # Managers
    from storage_manager import StorageManager
    storage_mgr = StorageManager(config)

    from camera_manager import CameraManager
    cam_mgr = CameraManager(config, on_pir_trigger=storage_mgr.handle_new_recording)

    from gpio_manager import GPIOManager
    gpio_mgr = GPIOManager(config, on_motion=cam_mgr.trigger_recording)

    # Auto-control IR LED relay during PIR-triggered recordings
    cam_mgr._relay_callback = gpio_mgr.set_relay
    # Notify gpio_manager of recording state changes (direct bool, avoids lock contention)
    cam_mgr._recording_notify_start = gpio_mgr.recording_started
    cam_mgr._recording_notify_stop  = gpio_mgr.recording_stopped
    # Every valid PIR pulse (>= T_VALID) extends the recording end time (Spec §3.2)
    gpio_mgr._on_valid_pulse = cam_mgr.extend_recording
    # Provide PIR and relay history to camera_manager for graph generation
    cam_mgr._pir_history_cb          = gpio_mgr.get_pir_history
    cam_mgr._relay_history_cb        = gpio_mgr.get_relay_history
    cam_mgr._trigger_history_cb      = gpio_mgr.get_trigger_history
    cam_mgr._pulse_width_history_cb  = gpio_mgr.get_pulse_width_history

    from network_manager import NetworkManager
    net_mgr = NetworkManager(config)

    # Flask app
    from web_server import create_app
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = int(server_cfg.get("port", 8080))
    app = create_app(cam_mgr, gpio_mgr, storage_mgr, net_mgr, config, str(_CONFIG_PATH))

    # Graceful shutdown
    def _shutdown(signum, frame):
        logger.info("Signal %d received — shutting down.", signum)
        gpio_mgr.stop()
        cam_mgr.stop()
        storage_mgr.shutdown()
        net_mgr.stop()
        logger.info("Shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Start hardware subsystems
    cam_mgr.start()
    gpio_mgr.start()
    net_mgr.start()

    logger.info("Web server starting on http://%s:%d", host, port)
    # Use single-threaded server so Flask doesn't spawn threads that
    # race on the camera resource.  The MJPEG generator uses its own
    # thread-safe MjpegOutput; all API calls are short and non-blocking.
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
