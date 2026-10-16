"""
AmonaVision – Message Grouper (Reply-Based Pairing)
=====================================================
Collects incoming media (photos, videos, video notes, voice notes) from
Telegram and turns each into a displayable "session" for the frame.

Each new, non-reply message starts its own session immediately — there is
no debounce timer or waiting window. Pairing across separate Telegram
messages (e.g. a photo and a voice note sent moments apart) is done by the
user explicitly **replying** to the earlier message:

- Reply to a photo/video with a voice note → paired into the same session.
- Reply to a video with a photo → that photo becomes the video's thumbnail.
- Reply to a photo with a video → the video replaces the photo, which
  becomes the video's thumbnail instead.

Background processing (image/video optimisation, thumbnail extraction) is
handed off to a single worker thread (``_processing_worker``) so uploads
are acknowledged immediately without blocking the bot's event loop.
"""

from __future__ import annotations

import logging
import shutil
import queue
import threading
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Any

import imageio_ffmpeg
from app.bot.processor import process_image_sync
from app.bot.video_processor import optimize_video

logger = logging.getLogger("AmonaVision.grouper")

# ---------------------------------------------------------------------------
# Background Processing Queue
# ---------------------------------------------------------------------------
_processing_queue = queue.Queue()

def _processing_worker():
    while True:
        task = _processing_queue.get()
        if task is None:
            break
        try:
            task_type = task.get('type')
            if task_type == 'thumbnail':
                temp_path = task.get('temp_path')
                thumb_dest = task.get('thumb_dest')
                try:
                    process_image_sync(temp_path, thumb_dest)
                    logger.info(f"Processed and saved custom thumbnail to {thumb_dest.name}")
                except Exception as e:
                    logger.error(f"Error processing custom thumbnail: {e}")
                finally:
                    if temp_path and temp_path.exists():
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
            else:
                db_manager = task.get('db')
                session_db_id = task.get('session_db_id')
                items_to_process = task.get('items_to_process', [])
                db_files_to_add = task.get('db_files_to_add', [])

                for media_type, dest, processed_dest in items_to_process:
                    try:
                        if media_type == MEDIA_PHOTO:
                            process_image_sync(dest, processed_dest)
                        elif media_type in VIDEO_TYPES:
                            success = optimize_video(dest, processed_dest)
                            if not success:
                                shutil.copy2(str(dest), str(processed_dest))

                            # Extract a thumbnail with ffmpeg (synchronously) unless a custom one exists
                            try:
                                PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
                                thumbnails_dir = PROJECT_ROOT / "app" / "media" / "thumbnails"
                                thumbnails_dir.mkdir(parents=True, exist_ok=True)

                                thumb_dest = thumbnails_dir / f"thumb_{processed_dest.name}.jpg"
                                if not thumb_dest.exists():
                                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                                    cmd = [
                                        ffmpeg_exe, "-y",
                                        "-i", str(processed_dest),
                                        "-ss", "00:00:00",
                                        "-vframes", "1",
                                        str(thumb_dest)
                                    ]
                                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    logger.info(f"Generated persistent thumbnail for video: {thumb_dest.name}")
                            except Exception as e:
                                logger.error(f"Failed to generate thumbnail for {processed_dest.name}: {e}")
                        else:
                            shutil.copy2(str(dest), str(processed_dest))
                    except Exception:
                        logger.exception(f"Background processing failed for {dest}")
                        shutil.copy2(str(dest), str(processed_dest))

                if db_manager is not None and session_db_id > 0 and db_files_to_add:
                    try:
                        db_manager.add_files_to_session(session_db_id, db_files_to_add)
                        db_manager.mark_session_files_optimized(session_db_id)
                        db_manager.mark_as_unread(session_db_id)
                        logger.info(f"Session {session_db_id} is now UNREAD (ready) and optimized.")
                    except Exception:
                        logger.exception(f"Failed to update DB for session {session_db_id}")
        except Exception:
            logger.exception("Error in background processing worker")
        finally:
            _processing_queue.task_done()

threading.Thread(target=_processing_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_FILE_SIZE_LIMIT: int = 20_971_520    # 20 MB – Telegram Bot API limit

# Media-type constants
MEDIA_PHOTO = "photo"
MEDIA_VIDEO = "video"
MEDIA_VIDEO_NOTE = "video_note"
MEDIA_VOICE = "voice"

# Types that qualify as video for post-processing
VIDEO_TYPES = {MEDIA_VIDEO, MEDIA_VIDEO_NOTE}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FlushedSession:
    """What the bot needs to confirm a received file to its sender."""

    media_types: list[str]    # ["thumbnail"] for a custom video thumbnail
    db_session_id: int = 0    # ID in the sessions table
    telegram_message_id: int = 0   # message_id of the anchor visual for linked replies
    telegram_chat_id: int = 0      # chat_id where the anchor message lives


# Callback: async def callback(user_id, session: FlushedSession) -> None
FlushCallback = Callable[[int, "FlushedSession"], Awaitable[None]]


# ---------------------------------------------------------------------------
# Size validation
# ---------------------------------------------------------------------------
class FileTooLargeError(Exception):
    """Raised when a file exceeds the Telegram download limit."""

    def __init__(self, file_size: int, limit: int = TELEGRAM_FILE_SIZE_LIMIT) -> None:
        self.file_size = file_size
        self.limit = limit
        size_mb = file_size / (1024 * 1024)
        limit_mb = limit / (1024 * 1024)
        super().__init__(
            f"File size ({size_mb:.1f} MB) exceeds the Telegram limit ({limit_mb:.0f} MB)."
        )


def validate_file_size(file_size: int | None) -> None:
    """Raise ``FileTooLargeError`` if *file_size* exceeds the Telegram limit."""
    if file_size is not None and file_size > TELEGRAM_FILE_SIZE_LIMIT:
        raise FileTooLargeError(file_size)


# ---------------------------------------------------------------------------
# MessageGrouper
# ---------------------------------------------------------------------------
class MessageGrouper:
    """
    MessageGrouper – Reply-based message collector.
    Processes messages immediately and associates them using Telegram replies.
    """

    def __init__(
        self,
        received_dir: Path,
        processed_dir: Path,
        *,
        on_flush: FlushCallback | None = None,
        db: Any = None,
    ) -> None:
        self._received_dir = received_dir
        self._processed_dir = processed_dir
        self._on_flush = on_flush
        self._db = db

        # Ensure directories exist
        for d in (self._received_dir, self._processed_dir):
            d.mkdir(parents=True, exist_ok=True)

    async def add(
        self,
        user_id: int,
        temp_path: Path,
        extension: str,
        media_type: str,
        user_name: str,
        message_id: int = 0,
        chat_id: int = 0,
        reply_to_message_id: int = 0,
    ) -> None:
        """
        Process a single received media file.
        If reply_to_message_id is provided, associate it with the parent session.
        Otherwise, create a brand new session.
        """
        logger.info(
            "Received media add request: type=%s, user=%s, msg_id=%d, reply_to=%d",
            media_type, user_name, message_id, reply_to_message_id
        )

        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', user_name).strip('_')

        # 1. If this is a reply, look up the parent session
        parent_session = None
        if reply_to_message_id > 0 and self._db is not None:
            parent_session = self._db.get_session_by_telegram_message_id(reply_to_message_id)
            if parent_session:
                parent_session = self._db.get_session_by_id(parent_session['id'])
                logger.info("Found parent session ID=%d for reply", parent_session['id'])

        # 2. Reply to an existing session
        if parent_session:
            session_db_id = parent_session['id']

            # Build the file-name timestamp from the parent session's created_at
            try:
                # SQLite CURRENT_TIMESTAMP is YYYY-MM-DD HH:MM:SS
                dt = datetime.strptime(parent_session['created_at'], "%Y-%m-%d %H:%M:%S")
                session_timestamp = dt.strftime("%Y%m%d_%H%M%S")
            except Exception:
                session_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            # Case A: thumbnail for a video
            # The parent is a VIDEO and this file is a PHOTO: store it as the thumbnail
            if parent_session['type'] in ("VIDEO", "VIDEO_AUDIO") and media_type == MEDIA_PHOTO:
                video_file = next((f for f in parent_session['files'] if f['file_type'] == 'VIDEO'), None)
                if video_file:
                    video_file_name = Path(video_file['file_path']).name
                    thumbnails_dir = self._processed_dir.parent / "thumbnails"
                    thumbnails_dir.mkdir(parents=True, exist_ok=True)
                    thumb_dest = thumbnails_dir / f"thumb_{video_file_name}.jpg"

                    # Queue the thumbnail processing in the background so nothing blocks
                    _processing_queue.put({
                        'type': 'thumbnail',
                        'temp_path': temp_path,
                        'thumb_dest': thumb_dest
                    })

                    # Thumbnail-specific confirmation callback
                    if self._on_flush:
                        session_obj = FlushedSession(
                            media_types=["thumbnail"],
                            db_session_id=session_db_id,
                            telegram_message_id=message_id,
                            telegram_chat_id=chat_id,
                        )
                        await self._on_flush(user_id, session_obj)
                    return

            # Case B: a video replies to a photo (the photo becomes its thumbnail, the video replaces it)
            if parent_session['type'] == "PHOTO" and media_type in VIDEO_TYPES:
                photo_file = next((f for f in parent_session['files'] if f['file_type'] == 'IMAGE'), None)
                if photo_file:
                    photo_src = self._processed_dir.parent / photo_file['file_path']
                    new_video_name = f"{session_timestamp}_{safe_name}_{session_db_id}.{extension}"

                    # Copy the photo to the new video's persistent thumbnail
                    thumbnails_dir = self._processed_dir.parent / "thumbnails"
                    thumbnails_dir.mkdir(parents=True, exist_ok=True)
                    thumb_dest = thumbnails_dir / f"thumb_{new_video_name}.jpg"

                    try:
                        if photo_src.exists():
                            shutil.copy2(str(photo_src), str(thumb_dest))
                            logger.info(f"Saved parent photo {photo_src.name} as thumbnail for video {new_video_name}")
                            photo_src.unlink(missing_ok=True)
                    except Exception as e:
                        logger.error(f"Error copying parent photo as thumbnail: {e}")

                    # Drop the session's previous files
                    self._db.clear_session_files(session_db_id)

                    # Move the new video to received/
                    dest = self._received_dir / new_video_name
                    processed_dest = self._processed_dir / new_video_name
                    shutil.move(str(temp_path), str(dest))

                    # Session type becomes VIDEO, telegram_message_id now points at the video's message
                    self._db.update_session_type(session_db_id, "VIDEO")
                    self._db.set_telegram_message_id(session_db_id, message_id, chat_id)

                    # Queue the video optimization
                    _processing_queue.put({
                        'db': self._db,
                        'session_db_id': session_db_id,
                        'items_to_process': [(media_type, dest, processed_dest)],
                        'db_files_to_add': [(f"processed/{processed_dest.name}", "VIDEO")]
                    })

                    if self._on_flush:
                        session_obj = FlushedSession(
                            media_types=[media_type],
                            db_session_id=session_db_id,
                            telegram_message_id=message_id,
                            telegram_chat_id=chat_id,
                        )
                        await self._on_flush(user_id, session_obj)
                    return

            # Case C: add a regular file to the existing session (e.g. a voice note to a photo/video)
            dup_suffix = f"_{len(parent_session['files'])}"
            new_name = f"{session_timestamp}_{safe_name}_{session_db_id}{dup_suffix}.{extension}"

            dest = self._received_dir / new_name
            processed_dest = self._processed_dir / new_name
            shutil.move(str(temp_path), str(dest))

            # Work out the new session type
            db_type = parent_session['type']
            if media_type == MEDIA_VOICE:
                if db_type == "PHOTO": db_type = "PHOTO_AUDIO"
                elif db_type == "VIDEO": db_type = "VIDEO_AUDIO"
            elif media_type == MEDIA_PHOTO:
                if db_type == "AUDIO": db_type = "PHOTO_AUDIO"
            elif media_type in VIDEO_TYPES:
                if db_type == "AUDIO": db_type = "VIDEO_AUDIO"

            self._db.update_session_type(session_db_id, db_type)

            # File record type
            f_type = "IMAGE" if media_type == MEDIA_PHOTO else ("VIDEO" if media_type in VIDEO_TYPES else ("AUDIO" if media_type == MEDIA_VOICE else "OTHER"))

            # Queue background processing
            _processing_queue.put({
                'db': self._db,
                'session_db_id': session_db_id,
                'items_to_process': [(media_type, dest, processed_dest)],
                'db_files_to_add': [(f"processed/{processed_dest.name}", f_type)]
            })

            if self._on_flush:
                session_obj = FlushedSession(
                    media_types=[media_type],
                    db_session_id=session_db_id,
                    telegram_message_id=message_id,
                    telegram_chat_id=chat_id,
                )
                await self._on_flush(user_id, session_obj)
            return

        # 3. New standalone message (or the parent session wasn't found)
        # Initial session type
        db_type = "OTHER"
        if media_type == MEDIA_PHOTO: db_type = "PHOTO"
        elif media_type in VIDEO_TYPES: db_type = "VIDEO"
        elif media_type == MEDIA_VOICE: db_type = "AUDIO"

        session_db_id = 0
        if self._db is not None:
            session_db_id = self._db.create_empty_session(db_type, safe_name)

        now = datetime.now()
        session_id = now.strftime("%Y%m%d_%H%M%S")
        new_name = f"{session_id}_{safe_name}_{session_db_id}.{extension}"

        dest = self._received_dir / new_name
        processed_dest = self._processed_dir / new_name

        shutil.move(str(temp_path), str(dest))

        # Store the telegram_message_id so later replies can find this session
        if self._db is not None:
            self._db.set_telegram_message_id(session_db_id, message_id, chat_id)

        f_type = "IMAGE" if media_type == MEDIA_PHOTO else ("VIDEO" if media_type in VIDEO_TYPES else ("AUDIO" if media_type == MEDIA_VOICE else "OTHER"))

        # Queue background processing
        _processing_queue.put({
            'db': self._db,
            'session_db_id': session_db_id,
            'items_to_process': [(media_type, dest, processed_dest)],
            'db_files_to_add': [(f"processed/{processed_dest.name}", f_type)]
        })

        if self._on_flush:
            session_obj = FlushedSession(
                media_types=[media_type],
                db_session_id=session_db_id,
                telegram_message_id=message_id,
                telegram_chat_id=chat_id,
            )
            await self._on_flush(user_id, session_obj)
