#!/usr/bin/env python3
"""PWM fan controller for Raspberry Pi 4 — BCM 18 (physical pin 12), 25 kHz."""

import time
import os
import signal
import sys

PWM_CHIP    = "/sys/class/pwm/pwmchip0"
PWM_CHANNEL = f"{PWM_CHIP}/pwm0"
PERIOD_NS   = 40000          # 25 kHz — standard 4-pin PWM fan frequency
TEMP_PATH   = "/sys/class/thermal/thermal_zone0/temp"
POLL_SEC    = 5

# Temperature → duty cycle curve (°C : fraction of PERIOD_NS)
# Below MIN_TEMP the fan is off; at MAX_TEMP it runs at full speed.
CURVE = [
    (40,  0.00),
    (50,  0.30),
    (60,  0.60),
    (70,  0.80),
    (75,  1.00),
]

def read_temp() -> float:
    with open(TEMP_PATH) as f:
        return int(f.read().strip()) / 1000.0

def temp_to_duty(temp: float) -> int:
    if temp <= CURVE[0][0]:
        return 0
    if temp >= CURVE[-1][0]:
        return PERIOD_NS
    for (t0, d0), (t1, d1) in zip(CURVE, CURVE[1:]):
        if t0 <= temp <= t1:
            frac = (temp - t0) / (t1 - t0)
            duty = d0 + frac * (d1 - d0)
            return int(PERIOD_NS * duty)
    return PERIOD_NS

def write_sysfs(path: str, value) -> None:
    with open(path, 'w') as f:
        f.write(str(value))

def setup() -> None:
    if not os.path.exists(PWM_CHANNEL):
        write_sysfs(f"{PWM_CHIP}/export", 0)
        time.sleep(0.2)
    write_sysfs(f"{PWM_CHANNEL}/duty_cycle", 0)
    write_sysfs(f"{PWM_CHANNEL}/period", PERIOD_NS)
    write_sysfs(f"{PWM_CHANNEL}/enable", 1)
    print("Fan controller started.")

def cleanup(*_) -> None:
    try:
        # Full speed on exit so the Pi doesn't overheat if the service restarts
        write_sysfs(f"{PWM_CHANNEL}/duty_cycle", PERIOD_NS)
    except Exception:
        pass
    print("Fan controller stopped — fan set to full speed.")
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)
    setup()
    while True:
        try:
            temp = read_temp()
            duty = temp_to_duty(temp)
            write_sysfs(f"{PWM_CHANNEL}/duty_cycle", duty)
        except Exception as e:
            print(f"Warning: {e}", file=sys.stderr)
        time.sleep(POLL_SEC)
