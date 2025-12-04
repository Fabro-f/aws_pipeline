#!/usr/bin/env python3
"""SessionManager - Cross-platform file-based session storage with proper locking"""

import os
import sys
import json
import time
import uuid
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger("session-manager")

# Cross-platform file locking
if sys.platform == 'win32':
    import msvcrt

    def lock_file(f):
        """Lock file for Windows"""
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def unlock_file(f):
        """Unlock file for Windows"""
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def lock_file(f):
        """Lock file for Unix"""
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def unlock_file(f):
        """Unlock file for Unix"""
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class SessionManager:
    """Thread-safe session manager with file-based persistence

    Each session stored in separate file: ./sessions/{uuid}.json
    """

    def __init__(self, sessions_dir: str = "./sessions"):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(exist_ok=True)
        logger.info(f"SessionManager initialized: {self.sessions_dir.absolute()}")

    def _get_session_path(self, session_id: str) -> Path:
        """Get path to session file with validation"""
        if not self._is_valid_session_id(session_id):
            raise ValueError(f"Invalid session ID format: {session_id}")
        return self.sessions_dir / f"{session_id}.json"

    @staticmethod
    def _is_valid_session_id(session_id: str) -> bool:
        """Validate session ID format (UUID or custom alphanumeric)"""
        if not session_id:
            return False

        # Try UUID format first
        try:
            uuid.UUID(session_id)
            return True
        except (ValueError, AttributeError):
            pass

        # Allow custom alphanumeric session IDs (letters, numbers, hyphens, underscores)
        # Length: 3-64 characters
        if not (3 <= len(session_id) <= 64):
            return False

        # Check if contains only valid characters
        allowed_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_')
        return all(c in allowed_chars for c in session_id)

    def create_session(self, token: str, api_url: str, portal_domain: str, user_email: str,
                      app_user_id: str, company_id: str, profile_id: str, exp: int,
                      user_data: Dict[str, Any] = None, session_id: Optional[str] = None) -> str:
        """Create a new session and return session ID

        Args:
            token: Authentication token
            api_url: API URL
            portal_domain: Portal domain
            user_email: User email
            app_user_id: App user ID
            company_id: Company ID
            profile_id: Profile ID (used as AreaId)
            exp: Expiration timestamp
            user_data: Additional user data (optional)
            session_id: Custom session ID (optional). If not provided, generates UUID.
                       Must be alphanumeric (letters, numbers, hyphens, underscores), 3-64 chars.

        Returns:
            session_id: The session ID (custom or auto-generated UUID)
        """
        # Use provided session_id or generate new UUID
        if session_id:
            if not self._is_valid_session_id(session_id):
                raise ValueError(f"Invalid session_id format: '{session_id}'. Must be alphanumeric (letters, numbers, hyphens, underscores), 3-64 characters.")
            final_session_id = session_id
            logger.info(f"Using custom session_id: {session_id}")
        else:
            final_session_id = str(uuid.uuid4())
            logger.info(f"Generated UUID session_id: {final_session_id}")

        session_data = {
            "session_uuid": final_session_id,  # Keep this key for backward compatibility
            "session_id": final_session_id,
            "token": token,
            "api_url": api_url,
            "portal_domain": portal_domain,
            "user_email": user_email,
            "app_user_id": app_user_id,
            "company_id": company_id,
            "profile_id": profile_id,
            "exp": exp,
            "created_at": int(time.time()),
            "last_accessed": int(time.time()),
            "user_data": user_data or {}
        }

        path = self._get_session_path(final_session_id)

        # Check if session already exists
        if path.exists():
            logger.warning(f"Session {final_session_id} already exists, overwriting...")

        try:
            with open(path, 'w') as f:
                lock_file(f)
                json.dump(session_data, f, indent=2)
                unlock_file(f)

            logger.info(f"Session created: {final_session_id} (User: {app_user_id}, Profile: {profile_id})")
            return final_session_id

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load session from file with automatic expiration check"""
        if not session_id:
            return None

        try:
            path = self._get_session_path(session_id)

            if not path.exists():
                return None

            with open(path, 'r') as f:
                lock_file(f)
                session_data = json.load(f)
                unlock_file(f)

            if self._is_expired(session_data):
                self.delete_session(session_id)
                return None

            self._update_access_time(session_id)
            return session_data

        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete a session file"""
        if not session_id:
            return False

        try:
            path = self._get_session_path(session_id)
            if path.exists():
                path.unlink()
                logger.info(f"Session deleted: {session_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            return False

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all active sessions"""
        sessions = []
        try:
            for filepath in self.sessions_dir.glob("*.json"):
                try:
                    session_id = filepath.stem
                    session = self.load_session(session_id)
                    if session:
                        sessions.append({
                            "session_uuid": session_id,  # Keep for backward compatibility
                            "session_id": session_id,
                            "user_email": session.get("user_email"),
                            "app_user_id": session.get("app_user_id"),
                            "company_id": session.get("company_id"),
                            "profile_id": session.get("profile_id"),
                            "created_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.get("created_at", 0))),
                            "expires_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.get("exp", 0)))
                        })
                except Exception as e:
                    logger.error(f"Error reading session {filepath}: {e}")
        except Exception as e:
            logger.error(f"Error listing sessions: {e}")
        return sessions

    def cleanup_expired_sessions(self) -> int:
        """Remove all expired session files"""
        count = 0
        try:
            for filepath in self.sessions_dir.glob("*.json"):
                try:
                    with open(filepath, 'r') as f:
                        lock_file(f)
                        session_data = json.load(f)
                        unlock_file(f)
                    if self._is_expired(session_data):
                        self.delete_session(filepath.stem)
                        count += 1
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
        if count > 0:
            logger.info(f"Cleaned up {count} expired sessions")
        return count

    def _is_expired(self, session: Dict[str, Any]) -> bool:
        """Check if session token is expired"""
        exp = session.get("exp")
        if not exp:
            return False
        return time.time() >= (int(exp) - 60)

    def _update_access_time(self, session_id: str):
        """Update last_accessed timestamp"""
        try:
            path = self._get_session_path(session_id)
            with open(path, 'r+') as f:
                lock_file(f)
                session = json.load(f)
                session["last_accessed"] = int(time.time())
                f.seek(0)
                json.dump(session, f, indent=2)
                f.truncate()
                unlock_file(f)
        except Exception:
            pass

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Alias for load_session for backward compatibility"""
        return self.load_session(session_id)
