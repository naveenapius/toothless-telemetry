#!/usr/bin/env python3
"""Probe A7670 modem: try common baud rates x flow control combos, print any response."""

import serial
import time

PORT = "/dev/cu.usbserial-120"
BAUDS = [115200, 9600, 57600, 38400, 19200, 4800]

print("=== passive listen (2s, 115200, no flow control) ===")
try:
    s = serial.Serial(PORT, 115200, rtscts=False, dsrdtr=False, timeout=2)
    s.reset_input_buffer()
    time.sleep(2)
    data = s.read(256)
    print(f"received: {repr(data)}")
    s.close()
except Exception as e:
    print(f"ERROR — {e}")

print()
print("=== AT probe: baud x flow control ===")
for baud in BAUDS:
    for rtscts, label in [(False, "no-flow"), (True, "rtscts")]:
        try:
            s = serial.Serial(PORT, baud, rtscts=rtscts, dsrdtr=rtscts, timeout=1)
            s.reset_input_buffer()
            s.write(b"AT\r\n")
            time.sleep(0.8)
            response = s.read(64)
            s.close()
            print(f"{baud:>7} {label:>8}: {repr(response)}")
        except Exception as e:
            print(f"{baud:>7} {label:>8}: ERROR — {e}")
