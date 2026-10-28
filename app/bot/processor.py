import shutil
import cv2
from pathlib import Path
import logging

from app import hal

logger = logging.getLogger(__name__)

# Canvas every photo is pre-rendered to, from the display profile
# (lcdwiki-10.1-h: 1920x1080). Any aspect ratio works: the blurred background
# is cropped to fill it and the photo is letterboxed inside it.
TARGET_W, TARGET_H = hal.get_selection().display.image_canvas

def process_image_sync(src: Path, dest: Path):
    """Make an image exactly TARGET_W x TARGET_H, padded with a blurred background."""
    try:
        img = cv2.imread(str(src))
        if img is None:
            logger.error(f"Could not read image: {src}")
            shutil.copy2(src, dest)
            return

        h, w = img.shape[:2]
        if w == TARGET_W and h == TARGET_H:
            shutil.copy2(src, dest)
            return

        aspect = w / h
        target_aspect = TARGET_W / TARGET_H

        # Background (crop to fill)
        if aspect > target_aspect:
            new_h = TARGET_H
            new_w = int(TARGET_H * aspect)
        else:
            new_w = TARGET_W
            new_h = int(TARGET_W / aspect)

        bg = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

        start_x = (new_w - TARGET_W) // 2
        start_y = (new_h - TARGET_H) // 2
        bg = bg[start_y:start_y+TARGET_H, start_x:start_x+TARGET_W]

        # Heavy blur
        bg = cv2.GaussianBlur(bg, (99, 99), 30)

        # Foreground (letterbox)
        if aspect > target_aspect:
            fg_w = TARGET_W
            fg_h = int(TARGET_W / aspect)
        else:
            fg_h = TARGET_H
            fg_w = int(TARGET_H * aspect)

        fg = cv2.resize(img, (fg_w, fg_h), interpolation=cv2.INTER_AREA)

        x_offset = (TARGET_W - fg_w) // 2
        y_offset = (TARGET_H - fg_h) // 2
        bg[y_offset:y_offset+fg_h, x_offset:x_offset+fg_w] = fg

        cv2.imwrite(str(dest), bg)
    except Exception as e:
        logger.exception(f"Error processing image {src}: {e}")
        shutil.copy2(src, dest)
