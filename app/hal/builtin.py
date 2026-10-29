"""Built-in copies of the production profiles (rpi4 + lcdwiki-10.1-h).

Used only when PyYAML is missing or the YAML file is broken, so a bad deploy
degrades to the known-good hardware instead of a crash loop. They must stay
identical to profiles/sbc/rpi4.yaml and profiles/display/lcdwiki-10.1-h.yaml
(tests/test_profiles.py checks it).
"""

RPI4 = {
    "id": "rpi4",
    "name": "Raspberry Pi 4 Model B",
    "status": "verified",
    "implementation": "rpi4",
    "device_tree_models": ["Raspberry Pi 4"],
    "display": {"kmsdrm_device_index": 1, "hdmi_connector": "HDMI-A-1"},
    "buttons": {
        "backend": "gpiozero",
        "gpio_pin_factory": "lgpio",
        "numbering": "BCM",
        "pull_up": True,
        "bounce_time": 0.02,
        "pins": {"previous": 9, "play": 17, "record": 27, "next": 22},
    },
    "video": {"decoder": "h264_v4l2m2m", "threads": 4},
    "audio": {
        "mixer_frequency": 48000,
        "mixer_size": -16,
        "mixer_channels": 2,
        "mixer_buffer": 4096,
        "record_samplerate": 48000,
        "record_channels": 1,
        "hdmi_sink_pattern": r"^alsa_output\.platform-fef00700\.hdmi\.",
        "hdmi_pipewire_device": "alsa_card.platform-fef00700.hdmi",
        "hdmi_alsa_card": "vc4hdmi0",
        "mic_source_pattern": r"^alsa_input\.usb-",
    },
    "thermal": {"path": "/sys/class/thermal/thermal_zone0/temp", "throttling": "unavailable"},
    "fan": {"pwm_chip": "/sys/class/pwm/pwmchip0", "pwm_channel": 0, "period_ns": 40000},
    "watchdog": {"device": "/dev/watchdog", "runtime_sec": 15, "reboot_sec": 120},
    "panel_power": {"uhubctl_locations": ["1-1", "2"]},
    "boot": {"config_txt": "/boot/firmware/config.txt", "cmdline_txt": "/boot/firmware/cmdline.txt"},
    "todo": [],
}

LCDWIKI_10_1_H = {
    "id": "lcdwiki-10.1-h",
    "name": "LCDwiki 10.1inch HDMI Display-H",
    "status": "verified",
    "panel_reset": True,
    "native_size": [1024, 600],
    "connector": "hdmi",
    "kernel_mode": "1024x600M@60D",
    "edid_file": "config/edid/amona.bin",
    "usb_hid_ignore": ["0x0484:0x5750:0x0004"],
    "panel_width_mm": 230,
    "audio_out": "hdmi",
    "media_canvas": {"image": [1920, 1080], "video": [1280, 720]},
    "todo": [],
}

PROFILES = {
    "sbc": {"rpi4": RPI4},
    "display": {"lcdwiki-10.1-h": LCDWIKI_10_1_H},
}
