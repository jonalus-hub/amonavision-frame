import sqlite3
import datetime
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

class DatabaseManager:
    """
    SQLite database manager for AmonaVision.
    Holds the media sessions and their files.
    """

    # Table definitions
    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        running_number INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        type TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        status TEXT DEFAULT 'UNREAD',
        first_viewed_at TIMESTAMP,
        telegram_message_id INTEGER,
        telegram_chat_id INTEGER,
        -- scoring signals (see app/scoring.py)
        last_shown_at INTEGER,
        show_count INTEGER DEFAULT 0,
        total_dwell_seconds REAL DEFAULT 0.0,
        revisit_count INTEGER DEFAULT 0,
        voice_reply_count INTEGER DEFAULT 0,
        last_voice_reply_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        is_optimized BOOLEAN DEFAULT 0,
        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
    );
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """
        Yields a configured connection scoped to a single 'with' block.
        This is a real context manager (unlike sqlite3.Connection's own
        __enter__/__exit__, which only commits/rolls back the transaction
        and never closes the connection). Every call site already does
        `with self._get_connection() as conn:`, so this guarantees the
        connection — and its OS-level file handle — is released
        deterministically instead of waiting on garbage collection, which
        matters for a process that stays up for months at a time.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA cache_size = 2000;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create the tables if they don't exist yet."""
        with self._get_connection() as conn:
            conn.executescript(self.SCHEMA_SQL)

    def create_empty_session(self, session_type: str, sender_id: str) -> int:
        """
        Create an empty session and return its autoincrement ID.
        That ID is then used as the running_number in the file name.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sessions (running_number, type, sender_id, status)
                VALUES (0, ?, ?, 'PROCESSING')
                """,
                (session_type, sender_id)
            )
            session_id = cursor.lastrowid

            # Update running_number to be equal to the session_id
            cursor.execute(
                "UPDATE sessions SET running_number = ? WHERE id = ?",
                (session_id, session_id)
            )

            return session_id

    def add_files_to_session(self, session_id: int, files: List[Tuple[str, str]]) -> None:
        """
        Attach a list of files to an existing session.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            file_records = [
                (session_id, file_path, file_type)
                for file_path, file_type in files
            ]
            cursor.executemany(
                """
                INSERT INTO files (session_id, file_path, file_type)
                VALUES (?, ?, ?)
                """,
                file_records
            )

    def mark_as_unread(self, session_id: int) -> None:
        """
        Move a session from 'PROCESSING' to 'UNREAD'.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = 'UNREAD'
                WHERE id = ? AND status = 'PROCESSING'
                """,
                (session_id,)
            )

    def mark_session_files_optimized(self, session_id: int) -> None:
        """
        Mark every file of a session as optimized.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE files
                SET is_optimized = 1
                WHERE session_id = ?
                """,
                (session_id,)
            )

    def mark_as_read(self, session_id: int) -> None:
        """
        Mark a session as 'READ' and set when it was first viewed.
        """
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = 'READ', first_viewed_at = ?
                WHERE id = ?
                """,
                (now, session_id)
            )

    def set_telegram_message_id(self, session_id: int, message_id: int, chat_id: int | None = None) -> None:
        """
        Store a session's Telegram message ID and, optionally, its chat_id.
        This lets the viewer send voice replies linked to the original message.
        """
        with self._get_connection() as conn:
            if chat_id is not None:
                conn.execute(
                    """
                    UPDATE sessions
                    SET telegram_message_id = ?, telegram_chat_id = ?
                    WHERE id = ?
                    """,
                    (message_id, chat_id, session_id)
                )
            else:
                conn.execute(
                    """
                    UPDATE sessions
                    SET telegram_message_id = ?
                    WHERE id = ?
                    """,
                    (message_id, session_id)
                )

    def get_session_by_telegram_message_id(self, message_id: int) -> Optional[Dict[str, Any]]:
        """
        Return the session that matches a telegram_message_id.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, running_number, created_at, type, sender_id, status, telegram_message_id, telegram_chat_id FROM sessions WHERE telegram_message_id = ?",
                (message_id,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def get_session_by_id(self, session_id: int) -> Optional[Dict[str, Any]]:
        """
        Return a session (with its files) by ID.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id as s_id, s.running_number, s.created_at, s.type, s.sender_id, s.status, s.first_viewed_at, s.telegram_message_id, s.telegram_chat_id,
                       f.id as f_id, f.file_path, f.file_type, f.is_optimized
                FROM sessions s
                LEFT JOIN files f ON s.id = f.session_id
                WHERE s.id = ?
                ORDER BY f.id ASC
                """,
                (session_id,)
            )
            rows = cursor.fetchall()
            if not rows:
                return None

            # Atomic single-read reconstruction
            session_data = {
                'id': rows[0]['s_id'],
                'running_number': rows[0]['running_number'],
                'created_at': rows[0]['created_at'],
                'type': rows[0]['type'],
                'sender_id': rows[0]['sender_id'],
                'status': rows[0]['status'],
                'first_viewed_at': rows[0]['first_viewed_at'],
                'telegram_message_id': rows[0]['telegram_message_id'],
                'telegram_chat_id': rows[0]['telegram_chat_id'],
                'files': []
            }

            for row in rows:
                if row['f_id'] is not None:
                    session_data['files'].append({
                        'id': row['f_id'],
                        'session_id': rows[0]['s_id'],
                        'file_path': row['file_path'],
                        'file_type': row['file_type'],
                        'is_optimized': bool(row['is_optimized'])
                    })

            return session_data

    def get_previous_session(self, current_id: Optional[int] = None, only_optimized: bool = False) -> Optional[Dict[str, Any]]:
        """
        Return the session before the current one (lower ID), skipping PROCESSING and HIDDEN.
        If current_id is None, return the most recent session.
        """
        opt_clause = ""
        if only_optimized:
            opt_clause = "AND id NOT IN (SELECT session_id FROM files WHERE is_optimized = 0)"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            if current_id is None:
                cursor.execute(
                    f"SELECT * FROM sessions WHERE status NOT IN ('PROCESSING', 'HIDDEN') {opt_clause} ORDER BY id DESC LIMIT 1"
                )
            else:
                cursor.execute(
                    f"SELECT * FROM sessions WHERE id < ? AND status NOT IN ('PROCESSING', 'HIDDEN') {opt_clause} ORDER BY id DESC LIMIT 1",
                    (current_id,)
                )
            row = cursor.fetchone()
            if not row:
                return None
            return self.get_session_by_id(row['id'])

    def get_next_session(self, current_id: int, only_optimized: bool = False) -> Optional[Dict[str, Any]]:
        """
        Return the session after the current one (higher ID), skipping PROCESSING and HIDDEN.
        """
        opt_clause = ""
        if only_optimized:
            opt_clause = "AND id NOT IN (SELECT session_id FROM files WHERE is_optimized = 0)"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM sessions WHERE id > ? AND status NOT IN ('PROCESSING', 'HIDDEN') {opt_clause} ORDER BY id ASC LIMIT 1",
                (current_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self.get_session_by_id(row['id'])

    def get_first_session(self, only_optimized: bool = False) -> Optional[Dict[str, Any]]:
        """
        Return the oldest session, skipping PROCESSING and HIDDEN.
        """
        opt_clause = ""
        if only_optimized:
            opt_clause = "AND id NOT IN (SELECT session_id FROM files WHERE is_optimized = 0)"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM sessions WHERE status NOT IN ('PROCESSING', 'HIDDEN') {opt_clause} ORDER BY id ASC LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self.get_session_by_id(row['id'])

    def mark_as_hidden(self, session_id: int) -> None:
        """Hide a session from the frame without deleting it (status = 'HIDDEN')."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'HIDDEN' WHERE id = ?",
                (session_id,)
            )

    def mark_as_visible(self, session_id: int) -> None:
        """Restore a hidden session so it shows up on the frame again (status = 'READ')."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'READ' WHERE id = ? AND status = 'HIDDEN'",
                (session_id,)
            )

    def clear_session_files(self, session_id: int) -> None:
        """
        Delete every file record of a session from the files table.
        """
        with self._get_connection() as conn:
            conn.execute("DELETE FROM files WHERE session_id = ?", (session_id,))

    def delete_session(self, session_id: int) -> None:
        """Permanently remove a session and all its file records from the database."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM files WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def update_session_type(self, session_id: int, session_type: str) -> None:
        """
        Change a session's type.
        """
        with self._get_connection() as conn:
            conn.execute("UPDATE sessions SET type = ? WHERE id = ?", (session_type, session_id))

    # ------------------------------------------------------------------
    # Scoring signals
    # ------------------------------------------------------------------

    def record_shown(self, session_id: int, dwell_seconds: float) -> None:
        """Called when the viewer leaves a session. Records dwell time and advances show count."""
        now = int(_time.time())
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET last_shown_at       = ?,
                    show_count          = COALESCE(show_count, 0) + 1,
                    total_dwell_seconds = COALESCE(total_dwell_seconds, 0.0) + ?
                WHERE id = ?
                """,
                (now, max(0.0, dwell_seconds), session_id)
            )

    def record_revisit(self, session_id: int) -> None:
        """Called when the user manually navigates to a session."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET revisit_count = COALESCE(revisit_count, 0) + 1 WHERE id = ?",
                (session_id,)
            )

    def record_voice_reply(self, session_id: int) -> None:
        """Called when the frame user records a voice reply to a session."""
        now = int(_time.time())
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET voice_reply_count   = COALESCE(voice_reply_count, 0) + 1,
                    last_voice_reply_at = ?
                WHERE id = ?
                """,
                (now, session_id)
            )

    def get_all_sessions_for_scoring(self) -> List[Dict[str, Any]]:
        """Return all displayable sessions with scoring columns. No file list."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.sender_id, s.created_at,
                       s.last_shown_at, s.show_count,
                       s.total_dwell_seconds, s.revisit_count,
                       s.voice_reply_count, s.last_voice_reply_at
                FROM sessions s
                WHERE s.status NOT IN ('PROCESSING', 'HIDDEN')
                  AND s.id NOT IN (
                      SELECT session_id FROM files WHERE is_optimized = 0
                  )
                """
            )
            result = []
            for row in cursor.fetchall():
                d = dict(row)
                try:
                    created_dt = datetime.datetime.fromisoformat(d['created_at'])
                    d['created_ts'] = created_dt.timestamp()
                except Exception:
                    d['created_ts'] = 0.0
                result.append(d)
            return result

    def get_recent_senders(self, n: int) -> List[str]:
        """Return sender_ids of the n most recently shown sessions (for sender balance)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT sender_id FROM sessions
                WHERE last_shown_at IS NOT NULL
                  AND status NOT IN ('PROCESSING', 'HIDDEN')
                ORDER BY last_shown_at DESC
                LIMIT ?
                """,
                (n,)
            )
            return [row['sender_id'] for row in cursor.fetchall()]

    def get_stats(self) -> Dict[str, int]:
        """
        Basic session and file counts.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            total_sessions = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM sessions WHERE status = 'UNREAD'")
            unread_sessions = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM sessions WHERE status = 'READ'")
            read_sessions = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM files")
            total_files = cursor.fetchone()[0]

            return {
                'total_sessions': total_sessions,
                'unread_sessions': unread_sessions,
                'read_sessions': read_sessions,
                'total_files': total_files
            }
