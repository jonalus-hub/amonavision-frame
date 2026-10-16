import subprocess
import logging
import threading
from pathlib import Path
import imageio_ffmpeg

logger = logging.getLogger("AmonaVision.video_processor")

# Only one FFmpeg transcode runs at a time so CPU is not stolen from playback.
_transcode_lock = threading.Semaphore(1)

def optimize_video(input_path: str | Path, output_path: str | Path) -> bool:
    input_str = str(input_path)
    output_str = str(output_path)

    TARGET_W = 1280
    TARGET_H = 720
    BG_W = TARGET_W // 4   # 320 — blur at 1/4 res, then upscale (16× cheaper)
    BG_H = TARGET_H // 4   # 180
    filter_complex = (
        f"[0:v]scale={BG_W}:{BG_H}:force_original_aspect_ratio=increase,"
        f"crop={BG_W}:{BG_H},boxblur=luma_radius=6,"
        f"scale={TARGET_W}:{TARGET_H}[bg];"
        f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )

    cmd = [
        "nice", "-n", "10",
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", input_str,
        "-filter_complex", filter_complex,
        "-vcodec", "libx264", "-profile:v", "main", "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-acodec", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
        "-threads", "4",
        output_str
    ]

    logger.info(f"Starting video optimization: {input_str} -> {output_str}")
    try:
        with _transcode_lock:
            # Hard timeout: there is only one background worker thread processing
            # this queue (see grouper.py). A single malformed/corrupt video that
            # makes ffmpeg hang would otherwise freeze ALL future photo/video
            # processing forever, since nothing else can run until this returns.
            process = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300
            )

        if process.returncode != 0:
            logger.error(f"FFmpeg failed to process {input_str}: {process.stderr.decode('utf-8', errors='replace')}")
            if Path(output_str).exists():
                Path(output_str).unlink()
            return False

        logger.info(f"Optimization completed successfully: {output_str}")
        return True

    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg exceeded the 300s timeout processing {input_str} — aborted.")
        if Path(output_str).exists():
            try:
                Path(output_str).unlink()
            except Exception:
                pass
        return False

    except Exception as e:
        logger.exception(f"Exception running FFmpeg on {input_str}: {e}")
        if Path(output_str).exists():
            try:
                Path(output_str).unlink()
            except Exception:
                pass
        return False
