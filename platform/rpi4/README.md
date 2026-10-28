# platform/rpi4 — Raspberry Pi 4 system files

Everything here is what `setup.sh` already applies on a Raspberry Pi 4 frame.
The files are **reference copies**: `setup.sh`, `test_setup.sh` and the
systemd units were deliberately left unchanged while the HAL was introduced,
so the provisioning of the deployed frames is byte-for-byte what it was.
`tests/test_platform_files.py` fails if a fragment and `setup.sh` drift apart.

| File | Applied by | Where on the device |
|---|---|---|
| `config.txt.fragment` | setup.sh Steps 3 and 4 | `/boot/firmware/config.txt` |
| `cmdline.txt.fragment` | setup.sh Step 3b | `/boot/firmware/cmdline.txt` |
| `system.conf.fragment` | setup.sh Step 4 (watchdog) | `/etc/systemd/system.conf` |

Other rpi4-only pieces that stay where they are:

- `amonavision.service` — `SDL_VIDEODRIVER=kmsdrm`, `SDL_KMSDRM_DEVICE_INDEX=1`
  (card1 = vc4), `GPIOZERO_PIN_FACTORY=lgpio`.
- `fan_control.py` + `fan-control.service` — PWM fan on `/sys/class/pwm/pwmchip0/pwm0`.
- `tools/panel-power-cycle.sh` — `uhubctl -l 1-1` / `-l 2` (Pi 4 USB topology).
- `config/edid/amona.bin`, `config/initramfs-tools/hooks/amona-edid` — the
  panel EDID baked into the initramfs (`drm.edid_firmware=`).
- `config/wireplumber/50-hdmi-default.conf` — HDMI as the default sink (generic).
- `test_setup.sh` — the health checks are written for this board.

Runtime values (pins, audio names, decoder, sysfs paths) live in
`profiles/sbc/rpi4.yaml` and are read through `app/hal/sbc_rpi4.py`.
