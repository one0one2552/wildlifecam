#!/usr/bin/env python3
"""
Self-Test: Camera Module 3 Wide NoIR (IMX708)
Validates PDAF controls, AWB modes, and a 1080p still capture.
Run standalone: python3 tests/test_camera.py
"""
import sys
import time
from picamera2 import Picamera2

REQUIRED_CONTROLS = ["AfMode", "LensPosition", "AfRange", "AwbMode",
                     "ExposureTime", "AnalogueGain", "Contrast", "Saturation"]

def test_camera():
    print("=== Camera Self-Test ===")
    failures = []

    cam = Picamera2(0)
    props = cam.camera_properties
    controls = cam.camera_controls

    model = props.get("Model", "?")
    print(f"  Model          : {model}")
    if "imx708" not in model:
        failures.append(f"Unexpected sensor model: {model}")

    pixel_size = props.get("PixelArraySize", (0, 0))
    print(f"  PixelArraySize : {pixel_size}")

    for ctrl in REQUIRED_CONTROLS:
        present = ctrl in controls
        status = "OK" if present else "MISSING"
        print(f"  {ctrl:<18}: {status}  {controls.get(ctrl,'')}")
        if not present:
            failures.append(f"Control missing: {ctrl}")

    # Quick still capture
    cfg = cam.create_still_configuration(main={"size": (1920, 1080)})
    cam.configure(cfg)
    cam.start()
    time.sleep(1.5)
    meta = cam.capture_metadata()
    cam.stop()
    cam.close()

    if "ExposureTime" not in meta:
        failures.append("capture_metadata missing ExposureTime")

    if failures:
        print("\n[FAIL] Failures detected:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("=== Camera Self-Test PASSED ===")


if __name__ == "__main__":
    test_camera()
