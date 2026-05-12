"""
web_server.py — Flask web server with HTTP Basic Auth

All routes require Basic Auth.  The /logout endpoint accepts any
credentials and always returns 401, which causes browsers to discard
their cached credentials (the only way to "log out" of HTTP Basic Auth).

Pages
-----
  GET  /                         Dashboard (recording library, status)
  GET  /stream                   Live MJPEG stream + camera settings
  GET  /settings                 Settings (trap timing, NAS, WiFi, health, logs)
  GET  /logout                   Force-clears Basic Auth session

Camera / stream API
-------------------
  GET  /video_feed               Raw MJPEG multipart stream
  POST /api/stream/stop          Release Live Mode, revert to Trap

Settings API
------------
  GET  /api/settings             Current camera + trap + NAS config (JSON)
  POST /api/settings             Update any subset of settings; persists to config.yaml
  POST /api/relay                Toggle IR relay  { state: true|false }
  POST /api/trigger              Manually trigger a PIR recording

Status / health API
-------------------
  GET  /api/status               Live status: state, PIR, disk, temp, voltage
  GET  /api/health               Detailed system health: CPU/RAM/temp peaks since reboot

Recordings API
--------------
  GET    /api/recordings              List local MP4s (newest first)
  DELETE /api/recordings/<file>       Delete one recording
  DELETE /api/recordings              Delete multiple  { filenames: [...] }
  POST   /api/recordings/<f>/upload   Queue single NAS upload
  POST   /api/recordings/upload       Queue multiple NAS uploads  { filenames: [...] }
  POST   /api/recordings/zip          Stream selected files as ZIP  { filenames: [...] }
  GET    /api/recordings/nas          List MP4s on the NAS share
  DELETE /api/recordings/nas/<file>   Delete one NAS recording
  GET    /nas-recordings/<file>       Proxy-stream a NAS file to the browser

WiFi API
--------
  GET  /api/wifi        Scan + current SSID
  POST /api/wifi        Connect  { ssid, password }

Log API
-------
  GET  /api/logs        Last N lines of wildlife_cam.log  ?lines=200

User management API (admin only)
---------------------------------
  GET    /api/me                    Current user info
  GET    /api/users                 List all users
  POST   /api/users                 Create user  { username, password, role }
  DELETE /api/users/<username>      Delete user
  PUT    /api/users/<username>      Update password  { password }
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import time
import zipfile
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

import bcrypt
import psutil
import yaml
from flask import (Flask, Response, jsonify, render_template,
                   request, send_from_directory, stream_with_context)

if TYPE_CHECKING:
    from camera_manager import CameraManager
    from gpio_manager import GPIOManager
    from network_manager import NetworkManager
    from storage_manager import StorageManager

logger = logging.getLogger(__name__)

# ── In-memory health tracker (reset on restart) ──────────────────────────────
_health: dict = {
    "temp_max": None,
    "voltage_min": None,
    "cpu_max": 0.0,
    "ram_max": 0.0,
}


def _check_password(stored_hash: str, provided: str) -> bool:
    return bcrypt.checkpw(provided.encode(), stored_hash.encode())


def _save_config(cfg: dict, path: str) -> None:
    try:
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
        logger.info("Config saved to %s", path)
    except OSError:
        logger.exception("Failed to save config")


def _read_cpu_temp() -> float | None:
    """Read CPU temperature from sysfs (works on Raspberry Pi and most Linux boards)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def _read_voltage() -> float | None:
    """Read core voltage via vcgencmd (Raspberry Pi only)."""
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_volts", "core"],
            capture_output=True, text=True, timeout=3,
        )
        match = re.search(r"volt=([0-9.]+)V", result.stdout)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


def _read_throttled() -> bool | None:
    """Return True if under-voltage has been detected (bit 0 or bit 16 of get_throttled)."""
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=3,
        )
        match = re.search(r"throttled=0x([0-9a-fA-F]+)", result.stdout)
        if match:
            val = int(match.group(1), 16)
            # bit 0 = under-voltage now, bit 16 = under-voltage since reboot
            return bool(val & 0x10001)
    except Exception:
        pass
    return None


# ── Power-saving helpers ──────────────────────────────────────────────────────

def _set_pi_leds(off: bool) -> None:
    """Turn the Pi ACT and PWR LEDs on or off."""
    try:
        if off:
            for led_path, trigger_path in [
                ("/sys/class/leds/ACT/trigger",    "/sys/class/leds/ACT/trigger"),
                ("/sys/class/leds/PWR/trigger",    "/sys/class/leds/PWR/trigger"),
            ]:
                try:
                    with open(led_path, "w") as f:
                        f.write("none")
                except OSError:
                    pass
            for brightness_path in [
                "/sys/class/leds/ACT/brightness",
                "/sys/class/leds/PWR/brightness",
            ]:
                try:
                    with open(brightness_path, "w") as f:
                        f.write("0")
                except OSError:
                    pass
        else:
            # Restore default triggers
            try:
                with open("/sys/class/leds/ACT/trigger", "w") as f:
                    f.write("mmc0")
            except OSError:
                pass
            try:
                with open("/sys/class/leds/PWR/brightness", "w") as f:
                    f.write("1")
            except OSError:
                pass
    except Exception:
        logger.debug("LED control not available", exc_info=True)


def _set_bluetooth(off: bool) -> None:
    """Block or unblock Bluetooth via rfkill."""
    try:
        action = "block" if off else "unblock"
        subprocess.run(["rfkill", action, "bluetooth"], capture_output=True, timeout=5)
    except Exception:
        logger.debug("rfkill not available", exc_info=True)


def _set_hdmi(off: bool) -> None:
    """Enable or disable HDMI output via vcgencmd."""
    try:
        value = "0" if off else "1"
        subprocess.run(
            ["vcgencmd", "display_power", value],
            capture_output=True, timeout=5,
        )
    except Exception:
        logger.debug("vcgencmd display_power not available", exc_info=True)


def create_app(
    camera_manager: "CameraManager",
    gpio_manager: "GPIOManager",
    storage_manager: "StorageManager",
    network_manager: "NetworkManager",
    config: dict,
    config_path: str = "config.yaml",
) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.config["SECRET_KEY"] = "wc-pi4-secret"

    _cfg = config

    # Apply persisted power-saving settings on startup
    _ps = _cfg.get("power_saving", {})
    if _ps.get("leds_off"):
        _set_pi_leds(True)
    if _ps.get("bluetooth_off"):
        _set_bluetooth(True)
    if _ps.get("hdmi_off"):
        _set_hdmi(True)

    # Migrate old single-user config to new multi-user list format
    _srv = _cfg.setdefault("server", {})
    if "users" not in _srv:
        old_user = _srv.get("username", "admin")
        old_hash = _srv.get("password_hash", "")
        _srv["users"] = [{"username": old_user, "password_hash": old_hash, "role": "admin"}]
        _save_config(_cfg, config_path)  # forward-defined — see below
    _users: list = _srv["users"]

    # ------------------------------------------------------------------ #
    # Auth helpers                                                         #
    # ------------------------------------------------------------------ #

    def _get_current_user() -> dict | None:
        auth = request.authorization
        if not auth:
            return None
        for u in _users:
            if u.get("username") == auth.username:
                if _check_password(u.get("password_hash", ""), auth.password):
                    return u
        return None

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
            user = _get_current_user()
            if user is None:
                return _auth_response()
            return f(*args, **kwargs)
        return decorated

    def require_admin(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = _get_current_user()
            if user is None:
                return _auth_response()
            if user.get("role") != "admin":
                return jsonify({"ok": False, "reason": "Admin-Rechte erforderlich"}), 403
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

    @app.get("/settings")
    @require_auth
    def settings_page():
        log_path = Path(config_path).parent / "logs" / "wildlife_cam.log"
        current_user = _get_current_user()
        is_admin = current_user is not None and current_user.get("role") == "admin"
        return render_template("settings.html", log_path=str(log_path), is_admin=is_admin,
                               current_username=current_user["username"] if current_user else "")

    @app.get("/logout")
    def logout():
        """Force Basic Auth logout by returning 401 so the browser discards credentials."""
        return Response(
            "Abgemeldet.",
            401,
            {"WWW-Authenticate": 'Basic realm="Wildlife Cam"'},
        )

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
        recs = storage_manager.list_recordings()
        rec_latest_mtime = recs[0]["mtime"] if recs else 0
        return jsonify({
            "state": camera_manager.state.name,
            "relay": gpio_manager.get_relay_state(),
            "pir": gpio_manager.get_pir_state(),
            "free_mb": round(storage_manager.free_mb(), 1),
            "storage_low": storage_manager.is_storage_low(),
            "cpu_temp": _read_cpu_temp(),
            "under_voltage": _read_throttled(),
            "rec_latest_mtime": rec_latest_mtime,
            "ts": time.time(),
        })

    @app.get("/api/health")
    @require_auth
    def api_health():
        temp = _read_cpu_temp()
        voltage = _read_voltage()
        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        ram_pct = round(mem.percent, 1)
        uptime_secs = int(time.time() - psutil.boot_time())
        # Track peak/trough values since last restart
        if temp is not None:
            if _health["temp_max"] is None or temp > _health["temp_max"]:
                _health["temp_max"] = temp
        if voltage is not None:
            if _health["voltage_min"] is None or voltage < _health["voltage_min"]:
                _health["voltage_min"] = voltage
        if cpu > _health["cpu_max"]:
            _health["cpu_max"] = round(cpu, 1)
        if ram_pct > _health["ram_max"]:
            _health["ram_max"] = ram_pct
        return jsonify({
            "temp_now": temp,
            "temp_max": _health["temp_max"],
            "voltage_now": voltage,
            "voltage_min": _health["voltage_min"],
            "cpu_now": round(cpu, 1),
            "cpu_max": _health["cpu_max"],
            "ram_now": ram_pct,
            "ram_max": _health["ram_max"],
            "ram_total_mb": round(mem.total / 1024 / 1024),
            "ram_used_mb": round(mem.used / 1024 / 1024),
            "uptime_secs": uptime_secs,
            "throttled": _read_throttled(),
        })

    @app.get("/api/settings")
    @require_auth
    def api_settings_get():
        nas = _cfg.get("storage", {}).get("nas", {})
        pir_cfg = _cfg.get("pir", {})
        relay_cfg = _cfg.get("relay", {})
        return jsonify({
            "camera": _cfg.get("camera", {}),
            "trap": _cfg.get("trap", {}),
            "pir": {
                "min_pulse_ms":        pir_cfg.get("min_pulse_ms", 100),
                "pulse_window_min_ms": pir_cfg.get("pulse_window_min_ms", 50.0),
                "pulse_count":         pir_cfg.get("pulse_count", 1),
                "pulse_window_s":      pir_cfg.get("pulse_window_s", 5.0),
                "poll_interval_ms":    pir_cfg.get("poll_interval_ms", 50),
                "save_graph":          bool(pir_cfg.get("save_graph", False)),
                "graph_pre_s":         pir_cfg.get("graph_pre_s", 30),
                "graph_post_s":        pir_cfg.get("graph_post_s", 30),
            },
            "relay": {
                "ir_on_pulse_ms": relay_cfg.get("ir_on_pulse_ms", 50),
            },
            "trap_enabled": gpio_manager.get_trap_enabled(),
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

    @app.get("/api/recordings/nas")
    @require_auth
    def api_nas_recordings():
        try:
            return jsonify(storage_manager.list_nas_recordings())
        except Exception as exc:
            return jsonify({"error": str(exc)[:300]}), 500

    @app.get("/nas-recordings/<filename>")
    @require_auth
    def serve_nas_recording(filename: str):
        """Stream a file from the NAS share through the Pi as a proxy."""
        if not re.match(r'^[\w\-\.]+$', filename):
            return jsonify({"ok": False, "reason": "Invalid filename"}), 400
        nas = storage_manager
        try:
            import smbclient  # type: ignore
            smbclient.register_session(
                nas._nas_server, username=nas._nas_user, password=nas._nas_password,
            )
            nas_remote = nas._nas_remote_path.replace("/", "\\")
            remote_file = f"\\\\{nas._nas_server}\\{nas._nas_share}{nas_remote}\\{filename}"

            def generate():
                with smbclient.open_file(remote_file, mode="rb") as f:
                    while True:
                        chunk = f.read(1 << 16)
                        if not chunk:
                            break
                        yield chunk

            return Response(
                stream_with_context(generate()),
                mimetype="video/mp4",
                headers={"Content-Disposition": f'inline; filename="{filename}"'},
            )
        except Exception as exc:
            return jsonify({"ok": False, "reason": str(exc)[:300]}), 500

    @app.delete("/api/recordings/nas/<filename>")
    @require_auth
    def api_delete_nas_recording(filename: str):
        if not re.match(r'^[\w\-\.]+$', filename):
            return jsonify({"ok": False, "reason": "Invalid filename"}), 400
        nas = storage_manager
        try:
            import smbclient  # type: ignore
            smbclient.register_session(
                nas._nas_server, username=nas._nas_user, password=nas._nas_password,
            )
            nas_remote = nas._nas_remote_path.replace("/", "\\")
            remote_file = f"\\\\{nas._nas_server}\\{nas._nas_share}{nas_remote}\\{filename}"
            smbclient.remove(remote_file)
            logger.info("NAS recording deleted: %s", filename)
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "reason": str(exc)[:300]}), 500

    @app.delete("/api/recordings/nas")
    @require_auth
    def api_multi_delete_nas_recordings():
        data = request.get_json(force=True, silent=True) or {}
        filenames = data.get("filenames", [])
        nas = storage_manager
        deleted, errors = [], []
        try:
            import smbclient  # type: ignore
            smbclient.register_session(
                nas._nas_server, username=nas._nas_user, password=nas._nas_password,
            )
            nas_remote = nas._nas_remote_path.replace("/", "\\")
            for fname in filenames:
                if not re.match(r'^[\w\-\.]+$', fname):
                    errors.append(fname)
                    continue
                try:
                    remote_file = f"\\\\{nas._nas_server}\\{nas._nas_share}{nas_remote}\\{fname}"
                    smbclient.remove(remote_file)
                    deleted.append(fname)
                    logger.info("NAS recording deleted: %s", fname)
                except Exception as exc:
                    logger.warning("Failed to delete NAS file %s: %s", fname, exc)
                    errors.append(fname)
        except Exception as exc:
            return jsonify({"deleted": deleted, "errors": errors, "reason": str(exc)[:300]}), 500
        return jsonify({"deleted": deleted, "errors": errors})

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

    @app.delete("/api/recordings")
    @require_auth
    def api_multi_delete_recordings():
        data = request.get_json(force=True, silent=True) or {}
        filenames = data.get("filenames", [])
        deleted, errors = [], []
        for fname in filenames:
            if not re.match(r'^[\w\-\.]+$', fname):
                errors.append(fname)
                continue
            path = storage_manager._recordings_path / fname
            if not path.resolve().is_relative_to(storage_manager._recordings_path.resolve()):
                errors.append(fname)
                continue
            if path.exists():
                path.unlink()
                deleted.append(fname)
                logger.info("Recording deleted: %s", fname)
            else:
                errors.append(fname)
        return jsonify({"deleted": deleted, "errors": errors})

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

    @app.post("/api/recordings/upload")
    @require_auth
    def api_multi_upload_recordings():
        data = request.get_json(force=True, silent=True) or {}
        filenames = data.get("filenames", [])
        if not storage_manager._nas_enabled:
            return jsonify({"ok": False, "reason": "NAS nicht aktiviert"}), 409
        queued, errors = [], []
        for fname in filenames:
            if not re.match(r'^[\w\-\.]+$', fname):
                errors.append(fname)
                continue
            if storage_manager.request_upload(fname):
                queued.append(fname)
            else:
                errors.append(fname)
        return jsonify({"queued": queued, "errors": errors})

    @app.post("/api/recordings/zip")
    @require_auth
    def api_recordings_zip():
        data = request.get_json(force=True, silent=True) or {}
        filenames = data.get("filenames", [])
        if not filenames:
            return jsonify({"ok": False, "reason": "No filenames"}), 400
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not re.match(r'^[\w\-\.]+$', fname):
                    continue
                path = storage_manager._recordings_path / fname
                if not path.resolve().is_relative_to(storage_manager._recordings_path.resolve()):
                    continue
                if path.exists():
                    zf.write(path, fname)
        buf.seek(0)
        return Response(
            buf.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": "attachment; filename=recordings.zip"},
        )

    @app.get("/api/logs")
    @require_auth
    def api_logs():
        lines = int(request.args.get("lines", 200))
        lines = max(10, min(lines, 2000))
        log_path = Path(config_path).parent / "logs" / "wildlife_cam.log"
        try:
            with open(log_path) as f:
                all_lines = f.readlines()
            return jsonify({"lines": all_lines[-lines:]})
        except FileNotFoundError:
            return jsonify({"lines": []})
        except Exception as e:
            return jsonify({"lines": [], "error": str(e)}), 500

    @app.get("/api/wifi")
    @require_auth
    def api_wifi():
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "active,ssid,signal,security", "dev", "wifi"],
                capture_output=True, text=True, timeout=10,
            )
            networks, current = [], None
            seen: set = set()
            for line in result.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) < 3:
                    continue
                active, ssid, signal = parts[0], parts[1], parts[2]
                security = parts[3] if len(parts) > 3 else ""
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                entry = {
                    "ssid": ssid,
                    "signal": int(signal) if signal.lstrip("-").isdigit() else 0,
                    "security": security,
                    "active": active == "yes",
                }
                networks.append(entry)
                if active == "yes":
                    current = ssid
            networks.sort(key=lambda n: n["signal"], reverse=True)
            return jsonify({"current": current, "networks": networks})
        except FileNotFoundError:
            return jsonify({"current": None, "networks": [], "error": "nmcli not available"})
        except Exception as e:
            return jsonify({"current": None, "networks": [], "error": str(e)})

    @app.post("/api/wifi")
    @require_auth
    def api_wifi_connect():
        data = request.get_json(force=True, silent=True) or {}
        ssid = str(data.get("ssid", ""))[:64]
        password = str(data.get("password", ""))[:128]
        if not ssid:
            return jsonify({"ok": False, "reason": "SSID required"}), 400
        try:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return jsonify({"ok": True, "msg": result.stdout.strip()[:200]})
            return jsonify({"ok": False, "reason": result.stderr.strip()[:200]}), 500
        except FileNotFoundError:
            return jsonify({"ok": False, "reason": "nmcli not available"}), 500
        except Exception as e:
            return jsonify({"ok": False, "reason": str(e)}), 500

    @app.post("/api/trigger")
    @require_auth
    def api_trigger():
        from camera_manager import CameraState
        if camera_manager.state not in (CameraState.TRAP, CameraState.RECORDING):
            return jsonify({"ok": False, "reason": "Not in TRAP mode"}), 409
        camera_manager.trigger_recording()
        return jsonify({"ok": True})

    @app.get("/api/trap/enabled")
    @require_auth
    def api_trap_enabled_get():
        return jsonify({"enabled": gpio_manager.get_trap_enabled()})

    @app.post("/api/trap/enabled")
    @require_auth
    def api_trap_enabled_set():
        data = request.get_json(force=True, silent=True) or {}
        enabled = bool(data.get("enabled", True))
        gpio_manager.set_trap_enabled(enabled)
        # Persist to config
        _cfg.setdefault("trap", {})["enabled"] = enabled
        _save_config(_cfg, config_path)
        return jsonify({"enabled": enabled})

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
    # Power saving API                                                     #
    # ------------------------------------------------------------------ #

    @app.get("/api/power_saving")
    @require_auth
    def api_power_saving_get():
        ps = _cfg.get("power_saving", {})
        return jsonify({
            "leds_off":      bool(ps.get("leds_off", False)),
            "bluetooth_off": bool(ps.get("bluetooth_off", False)),
            "hdmi_off":      bool(ps.get("hdmi_off", False)),
        })

    @app.post("/api/power_saving")
    @require_auth
    def api_power_saving_set():
        data = request.get_json(force=True, silent=True) or {}
        ps = _cfg.setdefault("power_saving", {})
        changed = False
        if "leds_off" in data:
            val = bool(data["leds_off"])
            ps["leds_off"] = val
            _set_pi_leds(val)
            changed = True
        if "bluetooth_off" in data:
            val = bool(data["bluetooth_off"])
            ps["bluetooth_off"] = val
            _set_bluetooth(val)
            changed = True
        if "hdmi_off" in data:
            val = bool(data["hdmi_off"])
            ps["hdmi_off"] = val
            _set_hdmi(val)
            changed = True
        if changed:
            _save_config(_cfg, config_path)
        return jsonify({"ok": True, "power_saving": ps})

    # ------------------------------------------------------------------ #
    # Network / hotspot API                                                #
    # ------------------------------------------------------------------ #

    @app.get("/api/network/status")
    @require_auth
    def api_network_status():
        return jsonify(network_manager.get_status())

    @app.get("/api/network/hotspot")
    @require_auth
    def api_hotspot_get():
        hs = _cfg.get("network", {}).get("hotspot", {})
        return jsonify({
            "enabled":    bool(hs.get("enabled", True)),
            "ssid":       hs.get("ssid", "owl_wildcam"),
            "timeout_s":  float(hs.get("timeout_s", 120)),
            "ip":         hs.get("ip", "192.168.4.4"),
        })

    @app.post("/api/network/hotspot")
    @require_auth
    def api_hotspot_set():
        data = request.get_json(force=True, silent=True) or {}
        hs = _cfg.setdefault("network", {}).setdefault("hotspot", {})
        if "enabled" in data:
            hs["enabled"] = bool(data["enabled"])
        if "ssid" in data:
            hs["ssid"] = str(data["ssid"])[:64]
        if "password" in data and data["password"]:
            hs["password"] = str(data["password"])[:128]
        if "timeout_s" in data:
            hs["timeout_s"] = max(30.0, min(600.0, float(data["timeout_s"])))
        if "ip" in data:
            hs["ip"] = str(data["ip"])[:39]
        network_manager.update_config(_cfg)
        _save_config(_cfg, config_path)
        return jsonify({"ok": True})

    @app.post("/api/nas/test")
    @require_auth
    def api_nas_test():
        nas = _cfg.get("storage", {}).get("nas", {})
        server = nas.get("server", "").strip()
        share = nas.get("share", "").strip()
        username = nas.get("username", "")
        password = nas.get("password", "")
        if not server or not share:
            return jsonify({"ok": False, "reason": "NAS nicht konfiguriert (Server und Share erforderlich)"}), 400
        try:
            import smbclient  # type: ignore
            smbclient.register_session(
                server, username=username, password=password, connection_timeout=5,
            )
            unc_path = f"\\\\{server}\\{share}"
            entries = list(smbclient.scandir(unc_path))
            return jsonify({"ok": True, "msg": f"Verbunden \u2014 {len(entries)} Eintr\u00e4ge in '{share}'"})
        except Exception as exc:
            return jsonify({"ok": False, "reason": str(exc)[:300]}), 500

    # ------------------------------------------------------------------ #
    # User management                                                      #
    # ------------------------------------------------------------------ #

    @app.get("/api/me")
    @require_auth
    def api_me():
        user = _get_current_user()
        return jsonify({"username": user["username"], "role": user.get("role", "user")})

    @app.get("/api/users")
    @require_admin
    def api_users_list():
        return jsonify([{"username": u["username"], "role": u.get("role", "user")} for u in _users])

    @app.post("/api/users")
    @require_admin
    def api_users_create():
        data = request.get_json(force=True, silent=True) or {}
        username = str(data.get("username", "")).strip()[:64]
        password = str(data.get("password", ""))
        role = str(data.get("role", "user"))
        if role not in ("admin", "user"):
            return jsonify({"ok": False, "reason": "Ungültige Rolle"}), 400
        if not username or not password:
            return jsonify({"ok": False, "reason": "Benutzername und Passwort erforderlich"}), 400
        if any(u["username"] == username for u in _users):
            return jsonify({"ok": False, "reason": "Benutzername bereits vorhanden"}), 409
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        _users.append({"username": username, "password_hash": pw_hash, "role": role})
        _cfg.setdefault("server", {})["users"] = _users
        _save_config(_cfg, config_path)
        return jsonify({"ok": True})

    @app.delete("/api/users/<username>")
    @require_admin
    def api_users_delete(username: str):
        current = _get_current_user()
        if current and current["username"] == username:
            return jsonify({"ok": False, "reason": "Eigenes Konto kann nicht gelöscht werden"}), 400
        target = next((u for u in _users if u["username"] == username), None)
        if not target:
            return jsonify({"ok": False, "reason": "Benutzer nicht gefunden"}), 404
        remaining_admins = [u for u in _users if u.get("role") == "admin" and u["username"] != username]
        if target.get("role") == "admin" and not remaining_admins:
            return jsonify({"ok": False, "reason": "Letzten Admin kann nicht gelöscht werden"}), 400
        _users[:] = [u for u in _users if u["username"] != username]
        _cfg.setdefault("server", {})["users"] = _users
        _save_config(_cfg, config_path)
        return jsonify({"ok": True})

    @app.put("/api/users/<username>")
    @require_auth
    def api_users_update(username: str):
        current = _get_current_user()
        if current["username"] != username and current.get("role") != "admin":
            return jsonify({"ok": False, "reason": "Keine Berechtigung"}), 403
        data = request.get_json(force=True, silent=True) or {}
        target = next((u for u in _users if u["username"] == username), None)
        if not target:
            return jsonify({"ok": False, "reason": "Benutzer nicht gefunden"}), 404
        if "password" in data and data["password"]:
            target["password_hash"] = bcrypt.hashpw(
                str(data["password"]).encode(), bcrypt.gensalt()
            ).decode()
        if "role" in data and current.get("role") == "admin":
            if str(data["role"]) in ("admin", "user"):
                target["role"] = str(data["role"])
        _cfg.setdefault("server", {})["users"] = _users
        _save_config(_cfg, config_path)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Settings persistence                                                 #
    # ------------------------------------------------------------------ #

    def _update_camera_settings(data: dict) -> None:
        """
        Parse an arbitrary settings payload and persist any recognised fields
        to config.yaml.  Unknown keys are silently ignored.

        Categories handled:
          - Trap timing  (pre/post_event_seconds)
          - NAS config   (nas_enabled, nas_server, …)
          - Camera controls applied at run-time via set_controls()
              af_mode, af_range, awb_mode, contrast, saturation,
              analogue_gain, lens_position, exposure_time, hdr, night_vision
          - Image transform  (hflip, vflip) — requires camera restart
        """
        nonlocal _cfg
        cam = _cfg.setdefault("camera", {})

        # ── Trap timing ──────────────────────────────────────────────────
        # pre_event_seconds: length of the ring buffer kept before a trigger.
        # post_event_seconds: idle time (no PIR) before recording stops.
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

        # ── PIR trigger settings ──────────────────────────────────────────
        pir = _cfg.setdefault("pir", {})
        changed_pir = False
        if "min_pulse_ms" in data:
            pir["min_pulse_ms"] = max(0.0, min(5000.0, float(data["min_pulse_ms"])))
            changed_pir = True
        if "pulse_window_min_ms" in data:
            pir["pulse_window_min_ms"] = max(0.0, min(5000.0, float(data["pulse_window_min_ms"])))
            changed_pir = True
        if "pulse_count" in data:
            pir["pulse_count"] = max(1, min(20, int(data["pulse_count"])))
            changed_pir = True
        if "pulse_window_s" in data:
            pir["pulse_window_s"] = max(1.0, min(15.0, float(data["pulse_window_s"])))
            changed_pir = True
        if "poll_interval_ms" in data:
            pir["poll_interval_ms"] = max(10, min(500, int(data["poll_interval_ms"])))
            changed_pir = True
        if "save_graph" in data:
            pir["save_graph"] = bool(data["save_graph"])
            changed_pir = True
        if "graph_pre_s" in data:
            pir["graph_pre_s"] = max(5, min(300, int(data["graph_pre_s"])))
            changed_pir = True
        if "graph_post_s" in data:
            pir["graph_post_s"] = max(5, min(300, int(data["graph_post_s"])))
            changed_pir = True
        if changed_pir:
            gpio_manager.update_config(_cfg)

        # ── relay settings ───────────────────────────────────────────────────
        relay = _cfg.setdefault("relay", {})
        if "ir_on_pulse_ms" in data:
            relay["ir_on_pulse_ms"] = max(20, min(1000, int(data["ir_on_pulse_ms"])))
            gpio_manager.update_config(_cfg)

        # ── NAS settings ─────────────────────────────────────────────────
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

        # ── Run-time camera controls ─────────────────────────────────────
        # These are passed to Picamera2.set_controls() and take effect on
        # the next frame without restarting the camera.
        int_fields = {
            "af_mode":              (0, 2),    # 0=Manual, 1=Auto, 2=Continuous
            "af_range":             (0, 2),    # 0=Normal, 1=Macro, 2=Full
            "af_speed":             (0, 1),    # 0=Normal, 1=Fast
            "awb_mode":             (0, 7),    # 0=Auto … 6=Cloudy, 7=Custom
            "noise_reduction_mode": (0, 2),    # 0=Off, 1=Fast, 2=HighQuality
            "ae_metering_mode":     (0, 3),    # 0=CentreWeighted…3=Custom
            "ae_exposure_mode":     (0, 3),    # 0=Normal…3=Custom
            "ae_constraint_mode":   (0, 3),    # 0=Normal…3=Custom
            "flicker_avoidance_hz": (0, 120),  # 0=Off,50,60,100,120 Hz
        }
        float_fields = {
            "contrast":         (0.0, 32.0),
            "saturation":       (0.0, 32.0),
            "sharpness":        (0.0, 16.0),
            "brightness":       (-1.0, 1.0),
            "analogue_gain":    (0.0, 16.0),   # 0 = auto
            "lens_position":    (0.0, 32.0),   # diopters — manual AF only
            "exposure_value":   (-8.0, 8.0),   # EV compensation
            "colour_gain_red":  (0.0, 32.0),   # manual ColourGains[0]
            "colour_gain_blue": (0.0, 32.0),   # manual ColourGains[1]
        }

        for field, (lo, hi) in int_fields.items():
            if field in data:
                cam[field] = max(lo, min(hi, int(data[field])))

        for field, (lo, hi) in float_fields.items():
            if field in data:
                cam[field] = max(lo, min(hi, float(data[field])))

        if "exposure_time" in data:
            cam["exposure_time"] = max(0, min(1_000_000, int(data["exposure_time"])))

        if "max_exposure_us" in data:
            # max exposure for trap-mode FrameDurationLimits (AE latitude)
            cam["max_exposure_us"] = max(33_333, min(2_000_000, int(data["max_exposure_us"])))

        if "hdr" in data:
            new_hdr = bool(data["hdr"])
            hdr_changed = cam.get("hdr", False) != new_hdr
            cam["hdr"] = new_hdr
        else:
            hdr_changed = False

        if "night_vision" in data:
            cam["night_vision"] = bool(data["night_vision"])

        # ── Image transform (hflip / vflip) ──────────────────────────────
        # Transform is a libcamera config-time parameter; a camera restart is
        # required.  Only restart when the value actually changes to avoid
        # disrupting live recordings on every settings save.
        flip_changed = False
        if "hflip" in data:
            new_val = bool(data["hflip"])
            if cam.get("hflip", False) != new_val:
                cam["hflip"] = new_val
                flip_changed = True
        if "vflip" in data:
            new_val = bool(data["vflip"])
            if cam.get("vflip", False) != new_val:
                cam["vflip"] = new_val
                flip_changed = True

        # Push the full updated config to CameraManager so that _cam_controls
        # is rebuilt and applied to the running camera.
        camera_manager.update_config(_cfg)

        if flip_changed or hdr_changed:
            # Restart in a background thread so the HTTP response returns
            # immediately and the browser doesn't see a timeout.
            def _restart_camera():
                import time as _time
                _time.sleep(0.3)
                camera_manager.restart_camera()
            import threading as _threading
            _threading.Thread(target=_restart_camera, daemon=True).start()

        _save_config(_cfg, config_path)

    @app.post("/api/camera/af_trigger")
    @require_auth
    def api_af_trigger():
        """Trigger a one-shot autofocus cycle then hold the focused position."""
        ok = camera_manager.trigger_af()
        return jsonify({"ok": ok})

    # Default camera settings — only camera-section fields are reset
    _CAMERA_DEFAULTS: dict = {
        "ae_constraint_mode": 0,
        "ae_exposure_mode": 0,
        "ae_metering_mode": 0,
        "af_mode": 0,
        "af_range": 0,
        "af_speed": 0,
        "analogue_gain": 0.0,
        "awb_mode": 0,
        "brightness": 0.0,
        "colour_gain_blue": 1.0,
        "colour_gain_red": 1.0,
        "contrast": 1.6,
        "exposure_time": 0,
        "exposure_value": 0.0,
        "flicker_avoidance_hz": 0,
        "hdr": False,
        "hflip": False,
        "lens_position": 0.0,
        "max_exposure_us": 66666,
        "night_vision": False,
        "noise_reduction_mode": 1,
        "saturation": 1.2,
        "sharpness": 1.0,
        "vflip": False,
    }

    @app.post("/api/camera/reset")
    @require_auth
    def api_camera_reset():
        """Reset camera settings to factory defaults and apply immediately."""
        nonlocal _cfg
        _cfg.setdefault("camera", {}).update(_CAMERA_DEFAULTS)
        _save_config(_cfg, config_path)
        camera_manager.update_config(_cfg)
        return jsonify({"ok": True, "camera": _cfg["camera"]})

    def _save_config(cfg: dict, path: str) -> None:
        try:
            with open(path, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
            logger.info("Config saved to %s", path)
        except OSError:
            logger.exception("Failed to save config")

    return app
