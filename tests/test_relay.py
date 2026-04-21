#!/usr/bin/env python3
"""
Self-Test: Relay on GPIO 18 (LED floodlight)
Toggles the relay output and verifies readback.
Run standalone: python3 tests/test_relay.py
"""
import sys
import time
import lgpio


def test_relay():
    print("=== Relay GPIO18 Self-Test ===")
    failures = []

    h = lgpio.gpiochip_open(0)
    try:
        ret = lgpio.gpio_claim_output(h, 18, 0)
        if ret < 0:
            failures.append(f"gpio_claim_output GPIO18 failed: {ret}")
        else:
            for state, label in [(1, "ON"), (0, "OFF")]:
                lgpio.gpio_write(h, 18, state)
                time.sleep(0.05)
                readback = lgpio.gpio_read(h, 18)
                status = "OK" if readback == state else "MISMATCH"
                print(f"  Relay SET={label:3s} readback={readback} : {status}")
                if readback != state:
                    failures.append(f"Relay readback mismatch: set={state} got={readback}")
        lgpio.gpio_free(h, 18)
    finally:
        lgpio.gpiochip_close(h)

    if failures:
        print("\n[FAIL]")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("=== Relay Self-Test PASSED ===")


if __name__ == "__main__":
    test_relay()
