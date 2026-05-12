"""
network_manager.py — WiFi monitoring and automatic hotspot fallback.

Monitors WiFi connectivity in a background thread.  When the Pi cannot
reach a WiFi network for more than *timeout_s* seconds it activates a
WPA2 hotspot via nmcli.  Once a WiFi connection is re-established the
hotspot is torn down automatically.

Hotspot defaults (all configurable via config.yaml → network.hotspot):
  SSID     : owl_wildcam
  Password : heisenberg
  IP       : 192.168.4.4
  Timeout  : 120 s

The web interface remains reachable on port 8080 both while on WiFi and
while the hotspot is active (accessible at http://<ip>:8080).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30        # seconds between WiFi connectivity checks
_CON_NAME       = "OWL-Hotspot"


class NetworkManager:
    """Monitors WiFi and manages the hotspot fallback."""

    def __init__(self, config: dict) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hotspot_active = False
        self._disconnected_since: Optional[float] = None
        self._apply_config(config)

    # ------------------------------------------------------------------ #
    # Config                                                               #
    # ------------------------------------------------------------------ #

    def _apply_config(self, config: dict) -> None:
        hs = config.get("network", {}).get("hotspot", {})
        self._hotspot_enabled: bool = bool(hs.get("enabled", True))
        self._ssid:            str  = str(hs.get("ssid",     "owl_wildcam"))
        self._password:        str  = str(hs.get("password", "heisenberg"))
        self._timeout_s:       float = float(hs.get("timeout_s", 120))
        self._ip:              str  = str(hs.get("ip", "192.168.4.4"))

    def update_config(self, config: dict) -> None:
        """Hot-reload hotspot settings."""
        with self._lock:
            self._apply_config(config)
        logger.info("NetworkManager config updated")

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._monitor_loop, name="net-monitor", daemon=True
        )
        self._thread.start()
        logger.info("NetworkManager started (hotspot timeout %.0fs)", self._timeout_s)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._hotspot_active:
            self._stop_hotspot()
        logger.info("NetworkManager stopped")

    # ------------------------------------------------------------------ #
    # Public status                                                        #
    # ------------------------------------------------------------------ #

    def get_status(self) -> dict:
        """Return the current network state as a plain dict for the web API."""
        current_ssid = self._get_current_ssid()
        with self._lock:
            hotspot_active = self._hotspot_active
            ssid = self._ssid
            ip   = self._ip
        return {
            "mode":           "hotspot" if hotspot_active else ("wifi" if current_ssid else "disconnected"),
            "wifi_ssid":      current_ssid,
            "hotspot_active": hotspot_active,
            "hotspot_ssid":   ssid if hotspot_active else None,
            "hotspot_ip":     ip   if hotspot_active else None,
        }

    # ------------------------------------------------------------------ #
    # Internal monitoring loop                                             #
    # ------------------------------------------------------------------ #

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    enabled = self._hotspot_enabled
                    timeout = self._timeout_s

                connected = self._is_wifi_connected()

                if connected:
                    # WiFi is up — tear down hotspot if it was running
                    with self._lock:
                        self._disconnected_since = None
                        hotspot_was_active = self._hotspot_active
                    if hotspot_was_active:
                        logger.info("WiFi reconnected — stopping hotspot")
                        self._stop_hotspot()
                else:
                    # WiFi is down
                    with self._lock:
                        hotspot_active = self._hotspot_active
                        if self._disconnected_since is None:
                            self._disconnected_since = time.monotonic()
                        offline_for = time.monotonic() - self._disconnected_since

                    if not hotspot_active and enabled and offline_for >= timeout:
                        logger.info(
                            "WiFi offline for %.0fs (>= %.0fs) — starting hotspot",
                            offline_for, timeout,
                        )
                        self._start_hotspot()

            except Exception:
                logger.exception("NetworkManager monitor error")

            self._stop_event.wait(_CHECK_INTERVAL)

    # ------------------------------------------------------------------ #
    # WiFi detection                                                       #
    # ------------------------------------------------------------------ #

    def _is_wifi_connected(self) -> bool:
        """Return True when NetworkManager reports full WiFi connectivity."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "STATE", "general"],
                capture_output=True, text=True, timeout=10,
            )
            return "connected" in result.stdout.lower()
        except Exception:
            logger.debug("nmcli connectivity check failed", exc_info=True)
            return False

    def _get_current_ssid(self) -> Optional[str]:
        """Return the currently active WiFi SSID, or None."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[0] == "yes":
                    ssid = parts[1].strip()
                    if ssid and ssid != _CON_NAME:
                        return ssid
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # Hotspot management                                                   #
    # ------------------------------------------------------------------ #

    def _start_hotspot(self) -> None:
        with self._lock:
            ssid     = self._ssid
            password = self._password
            ip       = self._ip

        try:
            # Remove any stale connection with the same name first
            subprocess.run(
                ["nmcli", "con", "delete", _CON_NAME],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

        try:
            # Create the access-point connection
            result = subprocess.run(
                [
                    "nmcli", "con", "add",
                    "type", "wifi",
                    "ifname", "wlan0",
                    "con-name", _CON_NAME,
                    "autoconnect", "no",
                    "ssid", ssid,
                    "mode", "ap",
                    "ipv4.method", "shared",
                    "ipv4.addresses", f"{ip}/24",
                    "wifi-sec.key-mgmt", "wpa-psk",
                    "wifi-sec.psk", password,
                ],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                logger.error("Failed to create hotspot connection: %s", result.stderr.strip())
                return

            # Bring it up
            result = subprocess.run(
                ["nmcli", "con", "up", _CON_NAME],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0:
                logger.error("Failed to start hotspot: %s", result.stderr.strip())
                return

            with self._lock:
                self._hotspot_active = True
            logger.info("Hotspot '%s' active on %s", ssid, ip)

        except Exception:
            logger.exception("Error starting hotspot")

    def _stop_hotspot(self) -> None:
        try:
            subprocess.run(
                ["nmcli", "con", "down", _CON_NAME],
                capture_output=True, timeout=15,
            )
            subprocess.run(
                ["nmcli", "con", "delete", _CON_NAME],
                capture_output=True, timeout=10,
            )
            with self._lock:
                self._hotspot_active = False
            logger.info("Hotspot stopped")
        except Exception:
            logger.exception("Error stopping hotspot")
