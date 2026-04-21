"""
gpio_manager.py — PIR sensor (GPIO17) and Relay (GPIO18) management.

Uses lgpio directly (the Bookworm-native library) rather than RPi.GPIO.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import lgpio

logger = logging.getLogger(__name__)

_GPIOCHIP = 0
_PIR_DEBOUNCE_MS = 300       # ignore re-triggers within this window
_POLL_INTERVAL = 0.05        # seconds between PIR polls


class GPIOManager:
    """
    Manages:
      - PIR motion sensor on GPIO 17 (input, pull-down)
      - Relay / floodlight on GPIO 18 (output, active-high)

    PIR polling runs in a background daemon thread.
    """

    def __init__(self, config: dict, on_motion: Optional[Callable] = None) -> None:
        pir_cfg = config.get("pir", {})
        relay_cfg = config.get("relay", {})

        self._pir_pin: int = pir_cfg.get("gpio_pin", 17)
        self._pir_pull_down: bool = pir_cfg.get("pull_down", True)
        self._relay_pin: int = relay_cfg.get("gpio_pin", 18)
        self._relay_active_high: bool = relay_cfg.get("active_high", True)

        self._on_motion = on_motion
        self._relay_state = False
        self._stop_event = threading.Event()
        self._pir_thread: Optional[threading.Thread] = None
        self._handle: Optional[int] = None
        self._last_trigger_ts: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._handle = lgpio.gpiochip_open(_GPIOCHIP)

        # PIR — input with optional pull-down
        pull_flag = lgpio.SET_PULL_DOWN if self._pir_pull_down else lgpio.SET_PULL_NONE
        lgpio.gpio_claim_input(self._handle, self._pir_pin, pull_flag)
        logger.info("PIR  GPIO%d: claimed as input", self._pir_pin)

        # Relay — output, default OFF
        off_level = 0 if self._relay_active_high else 1
        lgpio.gpio_claim_output(self._handle, self._relay_pin, off_level)
        logger.info("Relay GPIO%d: claimed as output (OFF)", self._relay_pin)

        self._pir_thread = threading.Thread(
            target=self._pir_poll_loop, name="pir-poll", daemon=True
        )
        self._pir_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._pir_thread:
            self._pir_thread.join(timeout=2.0)
        if self._handle is not None:
            # Ensure relay is OFF before releasing
            self._write_relay(False)
            lgpio.gpio_free(self._handle, self._pir_pin)
            lgpio.gpio_free(self._handle, self._relay_pin)
            lgpio.gpiochip_close(self._handle)
            self._handle = None
        logger.info("GPIOManager stopped.")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def set_relay(self, state: bool) -> None:
        """Turn relay ON (True) or OFF (False)."""
        with self._lock:
            self._write_relay(state)

    def get_relay_state(self) -> bool:
        return self._relay_state

    def get_pir_state(self) -> bool:
        """Read the current (raw) PIR pin state."""
        if self._handle is None:
            return False
        return bool(lgpio.gpio_read(self._handle, self._pir_pin))

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _write_relay(self, state: bool) -> None:
        if self._handle is None:
            return
        level = (1 if state else 0) if self._relay_active_high else (0 if state else 1)
        lgpio.gpio_write(self._handle, self._relay_pin, level)
        self._relay_state = state
        logger.debug("Relay → %s", "ON" if state else "OFF")

    def _pir_poll_loop(self) -> None:
        """Poll PIR pin in a tight loop; invoke callback on rising edge."""
        last_val = 0
        while not self._stop_event.is_set():
            try:
                if self._handle is None:
                    break
                val = lgpio.gpio_read(self._handle, self._pir_pin)
                if val == 1 and last_val == 0:
                    # Rising edge — apply debounce
                    now = time.monotonic()
                    elapsed = now - self._last_trigger_ts
                    if elapsed * 1000 >= _PIR_DEBOUNCE_MS:
                        self._last_trigger_ts = now
                        logger.info("PIR motion detected on GPIO%d", self._pir_pin)
                        if self._on_motion:
                            try:
                                self._on_motion()
                            except Exception:
                                logger.exception("on_motion callback raised")
                last_val = val
            except Exception:
                logger.exception("PIR poll error")
            time.sleep(_POLL_INTERVAL)
