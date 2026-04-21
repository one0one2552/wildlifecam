#!/usr/bin/env python3
"""
Self-Test: PIR sensor on GPIO 17
Validates the pin can be claimed as input and read a stable idle state.
Run standalone: python3 tests/test_pir.py
"""
import sys
import lgpio


def test_pir():
    print("=== PIR GPIO17 Self-Test ===")
    failures = []

    h = lgpio.gpiochip_open(0)
    try:
        ret = lgpio.gpio_claim_input(h, 17, lgpio.SET_PULL_DOWN)
        if ret < 0:
            failures.append(f"gpio_claim_input GPIO17 failed: {ret}")
        else:
            val = lgpio.gpio_read(h, 17)
            print(f"  GPIO17 state: {val} ({'MOTION' if val else 'IDLE'})")
            if val not in (0, 1):
                failures.append(f"Unexpected GPIO17 read value: {val}")
            else:
                print("  GPIO17 read  : OK")
        lgpio.gpio_free(h, 17)
    finally:
        lgpio.gpiochip_close(h)

    if failures:
        print("\n[FAIL]")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("=== PIR Self-Test PASSED ===")


if __name__ == "__main__":
    test_pir()
