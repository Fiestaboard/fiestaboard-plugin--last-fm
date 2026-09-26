"""Last.fm Now Playing plugin for FiestaBoard.

Displays what's currently playing via Last.fm scrobbling.
Works with Apple Music, Spotify, and any music source that scrobbles to Last.fm.
"""

import logging
import os
import textwrap
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.plugins.base import PluginBase, PluginResult

logger = logging.getLogger(__name__)

# Last.fm API endpoint
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

# Fallback geometry when no board is bound (legacy callers, unit tests).
# Never used to size the board-shaped display when a real board is known --
# see `_board_dims`.
DEFAULT_COLS = 22
DEFAULT_ROWS = 6

# Matches the `max_length` declared in manifest.json for title/artist/album/
# formatted/status. Those are single-line template variables the user can
# drop anywhere, on any board, so -- unlike the board-shaped display below --
# there is no board width to reflow against; they get one honest bound
# instead, enforced here so the manifest's declaration is never a lie.
MAX_VAR_LENGTH = 22


def _cap(text: str, limit: int = MAX_VAR_LENGTH) -> str:
    """Truncate *text* to at most *limit* characters, ellipsizing on cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _wrap(text: str, cols: int) -> List[str]:
    """Word-wrap *text* to *cols* wide; empty text wraps to no lines at all."""
    if not text:
        return []
    return textwrap.wrap(text, width=max(cols, 1)) or [text[:cols]]


def _layout_lines(status: str, title: str, artist: str, album: str, cols: int, rows: int, show_album: bool) -> List[str]:
    """Lay out the now-playing card for a board *cols* tiles wide and *rows* tall.

    Names are wrapped to the board's actual width instead of being clipped at
    a fixed column count, so a wide board shows the whole title instead of 22
    characters of it padded with blank tiles. Content is built front-to-back
    (status, title, artist, then album) and only trimmed from the end if a
    very narrow/short board can't hold all of it -- the reverse of the old
    behavior, which always reserved rows for the shorter board's layout
    regardless of how many rows were actually available.

    The album is treated as core content when the user asked for it
    (``show_album``); otherwise it's exactly the kind of extra detail a
    taller board's spare rows should carry instead of sitting blank.
    """
    content: List[str] = [status or ""]
    content.extend(_wrap(title, cols))
    content.extend(_wrap(artist, cols))
    if album and (show_album or len(content) < rows):
        content.extend(_wrap(album, cols))

    content = content[:rows]

    if len(content) < rows:
        # Centre the card vertically in the remaining space rather than
        # always padding at the bottom.
        pad_top = (rows - len(content)) // 2
        content = [""] * pad_top + content
    while len(content) < rows:
        content.append("")

    return [line.center(cols) for line in content[:rows]]


class LastFmPlugin(PluginBase):
    """Last.fm Now Playing plugin.

    Fetches the currently playing or most recently played track
    from a user's Last.fm scrobbling history.
    """

    def __init__(self, manifest: Dict[str, Any]):
        """Initialize the Last.fm plugin."""
        super().__init__(manifest)
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: Optional[datetime] = None

    @property
    def plugin_id(self) -> str:
        return "last_fm"

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate Last.fm configuration."""
        errors = []

        username = config.get("username", "").strip()
        if not username:
            # Check environment variable
            username = os.getenv("LASTFM_USERNAME", "").strip()
            if not username:
                errors.append("Last.fm username is required")

        api_key = config.get("api_key", "").strip()
        if not api_key:
            # Check environment variable
            api_key = os.getenv("LASTFM_API_KEY", "").strip()
            if not api_key:
                errors.append("Last.fm API key is required")

        refresh_seconds = config.get("refresh_seconds", 30)
        if not isinstance(refresh_seconds, int) or refresh_seconds < 10:
            errors.append("Refresh interval must be at least 10 seconds")

        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Drop the cached track so a config change takes effect immediately.

        The cache is keyed only on age, so without this a change to
        `username` would keep serving the old user's track for up to
        refresh_seconds.
        """
        self._cache = None
        self._cache_time = None
        logger.debug("Cleared cached track after config change")

    def _get_username(self) -> str:
        """Get username from config or environment."""
        return (
            self.config.get("username", "").strip()
            or os.getenv("LASTFM_USERNAME", "").strip()
        )

    def _get_api_key(self) -> str:
        """Get API key from config or environment."""
        return (
            self.config.get("api_key", "").strip()
            or os.getenv("LASTFM_API_KEY", "").strip()
        )

    def _board_dims(self) -> Tuple[int, int]:
        """(cols, rows) for the board currently bound to `self.board`.

        `self.board` is `None` outside a board-scoped render (legacy callers,
        unit tests) -- treated as a Flagship, never as a reason to crash.
        """
        board = self.board
        if board is None:
            return DEFAULT_COLS, DEFAULT_ROWS
        return board.cols, board.rows

    def _build_result(self, raw: Dict[str, Any]) -> PluginResult:
        """Build a `PluginResult` from *raw* (uncapped) track data.

        `raw` is cached across calls so repeated fetches don't hit the
        network, but the presentation is rebuilt every time from
        `self.board`: the same cached track can be rendered onto any board
        size, and a Flagship-shaped cache must never be handed to a Note.
        """
        cols, rows = self._board_dims()
        show_album = self.config.get("show_album", False)

        status = raw.get("status", "") or ""
        title = raw.get("title", "") or ""
        artist = raw.get("artist", "") or ""
        album = raw.get("album", "") or ""

        public_data = dict(raw)
        public_data["status"] = _cap(status)
        public_data["title"] = _cap(title)
        public_data["artist"] = _cap(artist)
        public_data["album"] = _cap(album)
        public_data["formatted"] = _cap(raw.get("formatted", "") or "")

        formatted_lines = _layout_lines(status, title, artist, album, cols, rows, show_album)

        return PluginResult(available=True, data=public_data, formatted_lines=formatted_lines)

    def fetch_data(self) -> PluginResult:
        """Fetch currently playing or recent track from Last.fm."""
        username = self._get_username()
        api_key = self._get_api_key()

        if not username:
            return PluginResult(
                available=False,
                error="Last.fm username not configured"
            )

        if not api_key:
            return PluginResult(
                available=False,
                error="Last.fm API key not configured"
            )

        # Check cache
        refresh_seconds = self.config.get("refresh_seconds", 30)
        if self._cache and self._cache_time:
            cache_age = (datetime.now() - self._cache_time).total_seconds()
            if cache_age < refresh_seconds:
                logger.debug(f"Using cached data (age: {cache_age:.0f}s)")
                return self._build_result(self._cache)

        try:
            # Call Last.fm API
            params = {
                "method": "user.getRecentTracks",
                "user": username,
                "api_key": api_key,
                "format": "json",
                "limit": 1,
            }

            response = requests.get(LASTFM_API_URL, params=params, timeout=10)

            if response.status_code == 403:
                return PluginResult(
                    available=False,
                    error="Invalid API key"
                )

            if response.status_code == 404:
                return PluginResult(
                    available=False,
                    error=f"User '{username}' not found"
                )

            if response.status_code != 200:
                return PluginResult(
                    available=False,
                    error=f"Last.fm API error: {response.status_code}"
                )

            data = response.json()

            # Check for API error response
            if "error" in data:
                error_msg = data.get("message", "Unknown error")
                return PluginResult(
                    available=False,
                    error=f"Last.fm error: {error_msg}"
                )

            # Parse response
            tracks = data.get("recenttracks", {}).get("track", [])

            if not tracks:
                return self._build_result(self._empty_data("No recent tracks"))

            # Handle case where tracks is a single dict instead of list
            if isinstance(tracks, dict):
                tracks = [tracks]

            track = tracks[0]

            # Check if currently playing (nowplaying attribute)
            is_playing = track.get("@attr", {}).get("nowplaying") == "true"

            # Extract track info
            title = track.get("name", "Unknown")
            artist = track.get("artist", {})
            # Artist can be a dict or a string depending on API response
            if isinstance(artist, dict):
                artist_name = artist.get("#text", "") or artist.get("name", "Unknown")
            else:
                artist_name = str(artist) if artist else "Unknown"

            album = track.get("album", {})
            if isinstance(album, dict):
                album_name = album.get("#text", "") or album.get("name", "")
            else:
                album_name = str(album) if album else ""

            # Get artwork URL (largest available)
            images = track.get("image", [])
            artwork_url = ""
            if images:
                # Last image is usually the largest
                for img in reversed(images):
                    if isinstance(img, dict) and img.get("#text"):
                        artwork_url = img["#text"]
                        break

            # Track URL
            track_url = track.get("url", "")

            # Build formatted string
            show_album = self.config.get("show_album", False)
            if show_album and album_name:
                formatted = f"{title} - {artist_name}"
            else:
                formatted = f"{title} by {artist_name}"

            # Status text
            if is_playing:
                status = "NOW PLAYING"
            else:
                status = "LAST PLAYED"

            result_data = {
                "title": title,
                "artist": artist_name,
                "album": album_name,
                "is_playing": is_playing,
                "artwork_url": artwork_url,
                "track_url": track_url,
                "formatted": formatted,
                "status": status,
            }

            # Update cache. Kept uncapped and board-agnostic -- it's the raw
            # track, not a rendering of it -- so `_build_result` can lay it
            # out fresh for whatever board asks next.
            self._cache = result_data
            self._cache_time = datetime.now()

            return self._build_result(result_data)

        except requests.exceptions.Timeout:
            logger.warning("Last.fm API request timed out")
            if self._cache:
                return self._build_result(self._cache)
            return PluginResult(
                available=False,
                error="Request timed out"
            )
        except requests.exceptions.RequestException as e:
            logger.exception("Error fetching Last.fm data")
            if self._cache:
                return self._build_result(self._cache)
            return PluginResult(
                available=False,
                error=f"Network error: {str(e)}"
            )
        except Exception as e:
            logger.exception("Unexpected error fetching Last.fm data")
            if self._cache:
                return self._build_result(self._cache)
            return PluginResult(
                available=False,
                error=str(e)
            )

    def _empty_data(self, status: str = "Nothing playing") -> Dict[str, Any]:
        """Return empty data structure when no track is available."""
        return {
            "title": "",
            "artist": "",
            "album": "",
            "is_playing": False,
            "artwork_url": "",
            "track_url": "",
            "formatted": "",
            "status": status,
        }

    def get_formatted_display(self) -> Optional[List[str]]:
        """Return the now-playing card shaped for `self.board`.

        This hook has no caller in FiestaBoard core today, but it is the
        documented plugin contract (and the conformance suite checks it), so
        it gets the same board-aware layout as `fetch_data`'s
        `formatted_lines` rather than a second, independent 22x6 rendering.
        """
        if not self._cache:
            result = self.fetch_data()
            if not result.available:
                return None

        data = self._cache
        if not data or not data.get("title"):
            return None

        cols, rows = self._board_dims()
        show_album = self.config.get("show_album", False)
        return _layout_lines(
            data.get("status", ""),
            data.get("title", ""),
            data.get("artist", ""),
            data.get("album", ""),
            cols,
            rows,
            show_album,
        )

    def cleanup(self) -> None:
        """Cleanup when plugin is disabled."""
        self._cache = None
        self._cache_time = None
        logger.info(f"Plugin {self.plugin_id} cleanup")


# Export the plugin class
Plugin = LastFmPlugin
