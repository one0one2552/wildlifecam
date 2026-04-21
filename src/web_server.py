"""
web_server.py — Flask web server with HTTP Basic Auth

Endpoints:
  GET  /                    Dashboard (settings form)
  GET  /stream              MJPEG live stream
  POST /api/stream/stop     Release Live Mode
  POST /api/settings        Update camera/relay settings (persists to config.yaml)
  POST /api/relay           Toggle relay
  GET  /api/status          JSON status
  GET  /api/recordings      JSON list of local recordings
  GET  /video_feed          Raw MJPEG multipart stream
"""

from __future__ import annotations

import logging
import re
import time
from functools import wraps
from typing import TYPE_CHECKING

import bcrypt
import yaml
from flask import (Flask, Response, jsonify, render_template,
                   request, send_from_directory, stream_with_context)

if TYPE_CHECKING:
    from camera_manager import CameraManager
    from gpio_manager import GPIOManager
    from storage_manager import StorageManager

logger = logging.getLogger(__name__)


def _check_password(stored_hash: str, provided: str) -> bool:
    return bcrypt.checkpw(provided.encode(), stored_hash.encode())


def create_app(
    camera_manager: "CameraManager",
    gpio_manager: "GPIOManager",
    storage_manager: "StorageManager",
    config: dict,
    config_path: str = "config.yaml",
) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["SECRET_KEY"] = "wc-pi4-secret"

    _cfg = config
    _auth_user: str = _cfg.get("server", {}).get("username", "admin")
    _auth_hash: str = _cfg.get("server", {}).get("password_hash", "")

    # ------------------------------------------------------------------ #
    # Basic Auth decorator                                                 #
    # ------------------------------------------------------------------ #

    def _auth_response():
        return Response(
            "Invalid credentials. Please try again.",
            401,
            {"WWW-Authenticate": 'Basic realm="Wildlife Cam"'},
        )

    def require_auth(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            auth = request.authorization
            if not auth:
                return _auth_response()
            if auth.username != _auth_user:
                return _auth_response()
            if _auth_hash and not _check_password(_auth_hash, auth.password):
                return _auth_response()
            return f(*args, **kwargs)
        return decorated

    # ------------------------------------------------------------------ #
    # Pages                                                                #
    # ------------------------------------------------------------------ #

    @app.get("/")
    @require_auth
    def index():
        cam_cfg = _cfg.get("camera", {})
        trap_cfg = _cfg.get("trap", {})
        nas = _cfg.get("storage", {}).get("nas", {})
        nas_cfg = {
            "enabled": nas.get("enabled", False),
            "server": nas.get("server", ""),
            "share": nas.get("share", ""),
            "remote_path": nas.get("remote_path", "/"),
            "username": nas.get("username", ""),
        }
        relay_state = gpio_manager.get_relay_state()
        free_mb = storage_manager.free_mb()
        return render_template(
            "index.html",
            state=camera_manager.state.name,
            cam_cfg=cam_cfg,
            trap_cfg=trap_cfg,
            nas_cfg=nas_cfg,
            relay_state=relay_state,
            free_mb=round(free_mb, 0),
            storage_low=storage_manager.is_storage_low(),
        )

    @app.get("/stream")
    @require_auth
    def stream_page():
        return render_template("stream.html")

    # ------------------------------------------------------------------ #
    # MJPEG stream                                                         #
    # ------------------------------------------------------------------ #

    @app.get("/video_feed")
    @require_auth
    def video_feed():
        ok = camera_manager.request_live_mode()
        if not ok:
            return Response("Camera unavailable", 503)

        def generate():
            output = camera_manager.mjpeg_output
            try:
                while True:
                    frame = output.get_frame(timeout=3.0)
                    if not frame:
                        continue
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                    )
            except GeneratorExit:
                pass
            finally:
                camera_manager.release_live_mode()

        return Response(
            stream_with_context(generate()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    # ------------------------------------------------------------------ #
    # REST API                                                             #
    # ------------------------------------------------------------------ #

    @app.post("/api/stream/stop")
    @require_auth
    def api_stream_stop():
        camera_manager.release_live_mode()
        return jsonify({"ok": True})

    @app.get("/api/status")
    @require_auth
    def api_status():
        return jsonify({
            "state": camera_manager.state.name,
            "relay": gpio_manager.get_relay_state(),
            "pir": gpio_manager.get_pir_state(),
            "free_mb": round(storage_manager.free_mb(), 1),
            "storage_low": storage_manager.is_storage_low(),
            "ts": time.time(),
        })

    @app.get("/api/settings")
    @require_auth
    def api_settings_get():
        nas = _cfg.get("storage", {}).get("nas", {})
        return jsonify({
            "camera": _cfg.get("camera", {}),
            "trap": _cfg.get("trap", {}),
            "nas": {
                "enabled": nas.get("enabled", False),
                "server": nas.get("server", ""),
                "share": nas.get("share", ""),
                "remote_path": nas.get("remote_path", "/"),
                "username": nas.get("username", ""),
            },
        })

    @app.get("/api/recordings")
    @require_auth
    def api_recordings():
        return jsonify(storage_manager.list_recordings())

    @app.get("/recordings/<path:filename>")
    @require_auth
    def serve_recording(filename: str):
        recordings_path = str(storage_manager._recordings_path)
        mime = "video/mp4" if filename.endswith(".mp4") else "video/h264"
        return send_from_directory(recordings_path, filename, mimetype=mime)

    @app.delete("/api/recordings/<filename>")
    @require_auth
    def api_delete_recording(filename: str):
        if not re.match(r'^[\w\-\.]+$', filename):
            return jsonify({"ok": False, "reason": "Invalid filename"}), 400
        path = storage_manager._recordings_path / filename
        if not path.resolve().is_relative_to(storage_manager._recordings_path.resolve()):
            return jsonify({"ok": False, "reason": "Forbidden"}), 403
        if not path.exists():
            return jsonify({"ok": False, "reason": "Not found"}), 404
        path.unlink()
        logger.info("Recording deleted: %s", filename)
        return jsonify({"ok": True})

    @app.post("/api/recordings/<filename>/upload")
    @require_auth
    def api_upload_recording(filename: str):
        if not re.match(r'^[\w\-\.]+$', filename):
            return jsonify({"ok": False, "reason": "Invalid filename"}), 400
        if not storage_manager._nas_enabled:
            return jsonify({"ok": False, "reason": "NAS nicht aktiviert"}), 409
        if not storage_manager.request_upload(filename):
            return jsonify({"ok": False, "reason": "Datei nicht gefunden"}), 404
        return jsonify({"ok": True, "queued": True})

    @app.post("/api/trigger")
    @require_auth
    def api_trigger():
        from camera_manager import CameraState
        if camera_manager.state not in (CameraState.TRAP, CameraState.RECORDING):
            return jsonify({"ok": False, "reason": "Not in TRAP mode"}), 409
        camera_manager.trigger_recording()
        return jsonify({"ok": True})

    @app.post("/api/relay")
    @require_auth
    def api_relay():
        data = request.get_json(force=True, silent=True) or {}
        state = bool(data.get("state", False))
        gpio_manager.set_relay(state)
        return jsonify({"relay": state})

    @app.post("/api/settings")
    @require_auth
    def api_settings():
        data = request.get_json(force=True, silent=True) or {}
        _update_camera_settings(data)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Settings persistence                                                 #
    # ------------------------------------------------------------------ #

    def _update_camera_settings(data: dict) -> None:
        nonlocal _cfg
        cam = _cfg.setdefault("camera", {})

        # Trap timing — update config dict and notify camera_manager
        trap = _cfg.setdefault("trap", {})
        changed_trap = False
        if "pre_event_seconds" in data:
            val = max(1, min(10, int(data["pre_event_seconds"])))
            trap["pre_event_seconds"] = val
            changed_trap = True
        if "post_event_seconds" in data:
            val = max(1, min(120, int(data["post_event_seconds"])))
            trap["post_event_seconds"] = val
            changed_trap = True
        if changed_trap:
            camera_manager.update_config(_cfg)

        # NAS settings
        nas = _cfg.setdefault("storage", {}).setdefault("nas", {})
        changed_nas = False
        if "nas_enabled" in data:
            nas["enabled"] = bool(data["nas_enabled"])
            changed_nas = True
        if "nas_server" in data:
            nas["server"] = str(data["nas_server"])[:256]
            changed_nas = True
        if "nas_share" in data:
            nas["share"] = str(data["nas_share"])[:256]
            changed_nas = True
        if "nas_remote_path" in data:
            nas["remote_path"] = str(data["nas_remote_path"])[:512]
            changed_nas = True
        if "nas_username" in data:
            nas["username"] = str(data["nas_username"])[:256]
            changed_nas = True
        if "nas_password" in data and data["nas_password"]:
            nas["password"] = str(data["nas_password"])[:256]
            changed_nas = True
        if changed_nas:
            storage_manager.update_config(_cfg)

        int_fields = {
            "af_mode": (0, 2),
            "af_range": (0, 2),
            "awb_mode": (0, 7),
        }
        float_fields = {
            "contrast": (0.0, 32.0),
            "saturation": (0.0, 32.0),
            "analogue_gain": (1.0, 16.0),
            "lens_position": (0.0, 32.0),
        }

        controls_update: dict = {}

        for field, (lo, hi) in int_fields.items():
            if field in data:
                val = max(lo, min(hi, int(data[field])))
                cam[field] = val
                ctrl_name = {
                    "af_mode": "AfMode",
                    "af_range": "AfRange",
                    "awb_mode": "AwbMode",
                }.get(field)
                if ctrl_name:
                    controls_update[ctrl_name] = val

        for field, (lo, hi) in float_fields.items():
            if field in data:
                val = max(lo, min(hi, float(data[field])))
                cam[field] = val
                ctrl_name = {
                    "contrast": "Contrast",
                    "saturation": "Saturation",
                    "analogue_gain": "AnalogueGain",
                    "lens_position": "LensPosition",
                }.get(field)
                if ctrl_name:
                    # Only apply LensPosition when in manual AF mode
                    if field == "lens_position" and cam.get("af_mode", 1) != 0:
                        pass
                    else:
                        controls_update[ctrl_name] = val

        if "exposure_time" in data:
            val = max(0, min(1_000_000, int(data["exposure_time"])))
            cam["exposure_time"] = val
            if val > 0:
                controls_update["ExposureTime"] = val

        if "hdr" in data:
            cam["hdr"] = bool(data["hdr"])

        if controls_update:
            camera_manager.apply_controls(controls_update)

        _save_config(_cfg, config_path)

    def _save_config(cfg: dict, path: str) -> None:
        try:
            with open(path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
            logger.info("Config saved to %s", path)
        except OSError:
            logger.exception("Failed to save config")

    return app
