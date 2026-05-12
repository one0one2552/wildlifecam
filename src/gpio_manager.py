"""
gpio_manager.py — PIR sensor (GPIO17) and Relay (GPIO18) management.

Uses lgpio directly (the Bookworm-native library) rather than RPi.GPIO.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

import lgpio

logger = logging.getLogger(__name__)

_GPIOCHIP = 0
_PIR_DEBOUNCE_MS = 300       # ignore re-triggers within this window
_POLL_INTERVAL = 0.05        # seconds between PIR polls
_PIR_LOG_WINDOW_S = 360      # seconds of PIR history to retain (covers max graph_pre_s=300 + margin)


class GPIOManager:
    """
    Manages:
      - PIR motion sensor on GPIO 17 (input, pull-down)
      - Relay / floodlight on GPIO 18 (output, active-high)

    PIR polling runs in a background daemon thread.

    Trigger logic (OR)
    ------------------
    A motion event is forwarded to *on_motion* when the trap is enabled
    AND **either** condition below is met:

    OR leg 1 — single long pulse:
      The PIR signal stays HIGH for at least *min_pulse_ms* milliseconds.
      This fires immediately on the falling edge without waiting for a
      pulse window (ideal for a slowly-passing animal).

    OR leg 2 — burst of short pulses:
      Within the last *pulse_window_s* seconds at least *pulse_count*
      pulses each lasting at least *pulse_window_min_ms* milliseconds have
      been detected.  Individual pulses may have different durations
      (ideal for a fast-moving animal that triggers the PIR multiple times).
    """

    def __init__(self, config: dict, on_motion: Optional[Callable] = None) -> None:
        pir_cfg = config.get("pir", {})
        relay_cfg = config.get("relay", {})

        self._pir_pin: int = pir_cfg.get("gpio_pin", 17)
        self._pir_pull_down: bool = pir_cfg.get("pull_down", True)
        self._relay_pin: int = relay_cfg.get("gpio_pin", 18)
        self._relay_active_high: bool = relay_cfg.get("active_high", True)

        # Pulse / trigger settings (hot-reloadable via update_config)
        self._min_pulse_ms: float = float(pir_cfg.get("min_pulse_ms", 100))
        # Minimum pulse duration for the multi-pulse window path (OR leg 2).
        # Pulses shorter than this are ignored entirely.
        self._pulse_window_min_ms: float = float(pir_cfg.get("pulse_window_min_ms", 50.0))
        self._pulse_count: int = max(1, int(pir_cfg.get("pulse_count", 1)))
        self._pulse_window_s: float = float(pir_cfg.get("pulse_window_s", 5.0))
        # Poll interval (hot-reloadable); clamped 10–500 ms
        self._poll_interval: float = max(0.01, min(0.5, float(pir_cfg.get("poll_interval_ms", 50)) / 1000.0))
        # IR LED: turn on when a pulse >= this threshold is seen (20-1000ms)
        self._ir_on_pulse_ms: float = max(20.0, min(1000.0, float(relay_cfg.get("ir_on_pulse_ms", 50.0))))
        # Timestamp of last pulse that switched the IR LED on (for auto-off logic)
        self._ir_last_on_ts: float = 0.0

        self._on_motion = on_motion
        self._relay_state = False
        self._trap_enabled: bool = bool(config.get("trap", {}).get("enabled", True))
        self._stop_event = threading.Event()
        self._pir_thread: Optional[threading.Thread] = None
        self._handle: Optional[int] = None
        self._last_trigger_ts: float = 0.0
        self._lock = threading.Lock()
        # Timestamps of qualifying pulses within the current window
        self._pulse_times: list = []
        # Rolling PIR state log: deque of (monotonic_time, value) tuples.
        # Keeps the last _PIR_LOG_WINDOW_S seconds for graph generation.
        self._pir_log: deque = deque()
        self._pir_log_lock = threading.Lock()
        # Rolling relay (IR LED) state log: same format as _pir_log.
        self._relay_log: deque = deque()
        self._relay_log_lock = threading.Lock()
        # Trigger event log: monotonic timestamps when _on_motion() fires.
        self._trigger_event_log: deque = deque()
        self._trigger_event_log_lock = threading.Lock()
        # Pulse width log: (fall_time, duration_ms) for each completed PIR pulse.
        self._pulse_width_log: deque = deque()
        self._pulse_width_log_lock = threading.Lock()
        # IR LED grace-period: monotonic time when conditions first cleared (0 = still active).
        self._ir_conditions_cleared_ts: float = 0.0
        # Direct bool set by camera_manager — avoids lock contention from is_recording() callback.
        self._recording_active: bool = False

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

    def get_pir_history(self, since: float) -> list:
        """Return [(monotonic_time, value), ...] samples recorded since *since*.

        Used by the graph generator after a recording to plot the PIR signal
        over the pre-event + event + post-event window.
        """
        with self._pir_log_lock:
            return [(t, v) for t, v in self._pir_log if t >= since]

    def get_relay_history(self, since: float) -> list:
        """Return [(monotonic_time, value), ...] relay state changes since *since*."""
        with self._relay_log_lock:
            return [(t, v) for t, v in self._relay_log if t >= since]

    def get_trigger_history(self, since: float) -> list:
        """Return [monotonic_time, ...] of each _on_motion() fire since *since*."""
        with self._trigger_event_log_lock:
            return [t for t in self._trigger_event_log if t >= since]

    def get_pulse_width_history(self, since: float) -> list:
        """Return [(fall_time, duration_ms), ...] for each completed PIR pulse since *since*."""
        with self._pulse_width_log_lock:
            return [(t, d) for t, d in self._pulse_width_log if t >= since]

    def recording_started(self) -> None:
        """Called by camera_manager when a PIR-triggered recording begins."""
        self._recording_active = True

    def recording_stopped(self) -> None:
        """Called by camera_manager when a PIR-triggered recording ends."""
        self._recording_active = False

    def set_trap_enabled(self, enabled: bool) -> None:
        """Enable or disable the trap (PIR → recording trigger)."""
        with self._lock:
            self._trap_enabled = bool(enabled)
        logger.info("Trap %s", "enabled" if enabled else "disabled")

    def get_trap_enabled(self) -> bool:
        return self._trap_enabled

    def update_config(self, config: dict) -> None:
        """Hot-reload PIR trigger settings from the current config."""
        pir_cfg = config.get("pir", {})
        relay_cfg = config.get("relay", {})
        with self._lock:
            self._min_pulse_ms = float(pir_cfg.get("min_pulse_ms", 100))
            self._pulse_window_min_ms = float(pir_cfg.get("pulse_window_min_ms", 50.0))
            self._pulse_count = max(1, int(pir_cfg.get("pulse_count", 1)))
            self._pulse_window_s = float(pir_cfg.get("pulse_window_s", 5.0))
            self._poll_interval = max(0.01, min(0.5, float(pir_cfg.get("poll_interval_ms", 50)) / 1000.0))
            self._ir_on_pulse_ms = max(20.0, min(1000.0, float(relay_cfg.get("ir_on_pulse_ms", 50.0))))
            self._trap_enabled = bool(config.get("trap", {}).get("enabled", True))

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _write_relay(self, state: bool) -> None:
        if self._handle is None:
            return
        level = (1 if state else 0) if self._relay_active_high else (0 if state else 1)
        lgpio.gpio_write(self._handle, self._relay_pin, level)
        self._relay_state = state
        now = time.monotonic()
        with self._relay_log_lock:
            self._relay_log.append((now, 1 if state else 0))
            # Trim log to _PIR_LOG_WINDOW_S
            cutoff = now - _PIR_LOG_WINDOW_S
            while self._relay_log and self._relay_log[0][0] < cutoff:
                self._relay_log.popleft()
        logger.debug("Relay → %s", "ON" if state else "OFF")

    def _pir_poll_loop(self) -> None:
        """Poll PIR pin in a tight loop; invoke callback on motion.

        OR leg 1: pulse >= _min_pulse_ms  → trigger immediately.
        OR leg 2: pulse >= _pulse_window_min_ms → count toward window;
                  fire when window reaches _pulse_count pulses.
        All paths respect _PIR_DEBOUNCE_MS and _trap_enabled.
        """
        last_val = 0
        pulse_start: float = 0.0
        in_pulse: bool = False

        while not self._stop_event.is_set():
            try:
                if self._handle is None:
                    break
                val = lgpio.gpio_read(self._handle, self._pir_pin)
                now = time.monotonic()

                # Append to rolling PIR log, pruning old samples
                with self._pir_log_lock:
                    self._pir_log.append((now, val))
                    cutoff = now - _PIR_LOG_WINDOW_S
                    while self._pir_log and self._pir_log[0][0] < cutoff:
                        self._pir_log.popleft()

                if val == 1 and last_val == 0:
                    # Rising edge — start measuring pulse duration
                    in_pulse = True
                    pulse_start = now

                elif val == 0 and last_val == 1 and in_pulse:
                    # Falling edge — evaluate the completed pulse
                    in_pulse = False
                    pulse_duration_ms = (now - pulse_start) * 1000.0

                    with self._lock:
                        min_ms = self._min_pulse_ms
                        window_min_ms = self._pulse_window_min_ms
                        debounce_ms = _PIR_DEBOUNCE_MS
                        pulse_count_needed = self._pulse_count
                        pulse_window = self._pulse_window_s
                        ir_on_ms = self._ir_on_pulse_ms
                        trap_on = self._trap_enabled

                    # Log pulse width for graph annotation
                    with self._pulse_width_log_lock:
                        self._pulse_width_log.append((now, pulse_duration_ms))
                        _pw_cutoff = now - _PIR_LOG_WINDOW_S
                        while self._pulse_width_log and self._pulse_width_log[0][0] < _pw_cutoff:
                            self._pulse_width_log.popleft()

                    # IR LED: mark qualifying pulses; per-cycle logic manages actual relay.
                    if pulse_duration_ms >= ir_on_ms:
                        self._ir_last_on_ts = now
                        logger.debug("IR LED qualifying pulse %.0f ms", pulse_duration_ms)

                    elapsed_since_last = (now - self._last_trigger_ts) * 1000

                    if pulse_duration_ms >= min_ms:
                        # ── OR leg 1: single long pulse → trigger immediately ──
                        if elapsed_since_last < debounce_ms:
                            logger.debug("PIR debounced long pulse (%.0f ms)", elapsed_since_last)
                        else:
                            self._last_trigger_ts = now
                            self._pulse_times.clear()
                            logger.info(
                                "PIR trigger: long pulse (%.0f ms >= %.0f ms) on GPIO%d",
                                pulse_duration_ms, min_ms, self._pir_pin,
                            )
                            if trap_on and self._on_motion:
                                with self._trigger_event_log_lock:
                                    self._trigger_event_log.append(now)
                                    _te_cutoff = now - _PIR_LOG_WINDOW_S
                                    while self._trigger_event_log and self._trigger_event_log[0] < _te_cutoff:
                                        self._trigger_event_log.popleft()
                                try:
                                    self._on_motion()
                                except Exception:
                                    logger.exception("on_motion callback raised")
                            elif not trap_on:
                                logger.debug("PIR trigger suppressed — trap disabled")

                    elif pulse_duration_ms >= window_min_ms:
                        # ── OR leg 2: short pulse → count toward window ──
                        if elapsed_since_last < debounce_ms:
                            logger.debug("PIR debounced short pulse (%.0f ms)", elapsed_since_last)
                        else:
                            self._last_trigger_ts = now
                            cutoff = now - pulse_window
                            self._pulse_times = [
                                t for t in self._pulse_times if t > cutoff
                            ]
                            self._pulse_times.append(now)
                            count = len(self._pulse_times)

                            logger.debug(
                                "PIR short pulse (%.0f ms), %d/%d in %.1fs window",
                                pulse_duration_ms, count, pulse_count_needed, pulse_window,
                            )

                            if count >= pulse_count_needed:
                                self._pulse_times.clear()
                                logger.info(
                                    "PIR trigger: %d short pulse(s) within %.1fs on GPIO%d",
                                    count, pulse_window, self._pir_pin,
                                )
                                if trap_on and self._on_motion:
                                    with self._trigger_event_log_lock:
                                        self._trigger_event_log.append(now)
                                        _te_cutoff = now - _PIR_LOG_WINDOW_S
                                        while self._trigger_event_log and self._trigger_event_log[0] < _te_cutoff:
                                            self._trigger_event_log.popleft()
                                    try:
                                        self._on_motion()
                                    except Exception:
                                        logger.exception("on_motion callback raised")
                                elif not trap_on:
                                    logger.debug("PIR trigger suppressed — trap disabled")

                    else:
                        logger.debug(
                            "PIR pulse too short (%.0f ms < %.0f ms), ignored",
                            pulse_duration_ms, window_min_ms,
                        )

                last_val = val

                # Per-cycle IR LED management:
                # ON while pulse_window active OR recording is running.
                # OFF after 1s grace period once both conditions clear.
                with self._lock:
                    _pw_s = self._pulse_window_s
                _window_active = (self._ir_last_on_ts > 0) and (now - self._ir_last_on_ts < _pw_s)
                _rec_active = self._recording_active  # set directly by camera_manager, no lock needed
                _want_on = _window_active or _rec_active
                if _want_on:
                    self._ir_conditions_cleared_ts = 0.0
                    if not self._relay_state:
                        self._write_relay(True)
                        logger.info("IR LED ON (window=%s, rec=%s)", _window_active, _rec_active)
                elif self._relay_state:
                    if self._ir_conditions_cleared_ts == 0.0:
                        self._ir_conditions_cleared_ts = now
                    elif now - self._ir_conditions_cleared_ts >= 1.0:
                        self._write_relay(False)
                        self._ir_conditions_cleared_ts = 0.0
                        logger.info("IR LED OFF — 1s grace expired")

            except Exception:
                logger.exception("PIR poll error")
            time.sleep(self._poll_interval)
