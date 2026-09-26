"""Board-geometry conformance for the Last.fm plugin.

Renders the plugin across every board shape FiestaBoard supports -- Flagship,
Note, and Note arrays from 15x3 up to 120x24 (which is also what a
FiestaPanel is) -- and asserts it never overflows a row or column, that its
declared template-variable lengths are honest, and that a taller board shows
more (the album) rather than staying letterboxed at a Flagship's 6 rows. See
``src/plugins/geometry_conformance.py`` in FiestaBoard core for what is
checked.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

from src.plugins.geometry_conformance import assert_board_conformance

from plugins.last_fm import LastFmPlugin

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"


def _manifest() -> dict:
    """The real manifest, with per-variable ``max_length`` hoisted to the
    top-level ``max_lengths`` map the conformance suite reads.

    ``PluginManifest.from_dict`` does exactly this merge in core (see
    ``src/plugins/manifest.py``); doing it here too means the suite checks
    the same bounds the page editor actually uses, including
    title/artist/album/formatted/status, which this manifest declares only
    per-variable.
    """
    manifest = json.loads(_MANIFEST_PATH.read_text())
    max_lengths = dict(manifest.get("max_lengths") or {})
    for name, meta in (manifest.get("variables", {}).get("simple") or {}).items():
        if isinstance(meta, dict) and isinstance(meta.get("max_length"), int):
            max_lengths.setdefault(name, meta["max_length"])
    manifest["max_lengths"] = max_lengths
    return manifest


MANIFEST = _manifest()

_NOWPLAYING_RESPONSE = {
    "recenttracks": {
        "track": [
            {
                "name": "Bohemian Rhapsody",
                "artist": {"#text": "Queen"},
                "album": {"#text": "A Night at the Opera"},
                "image": [
                    {"#text": "https://example.com/art.jpg", "size": "extralarge"}
                ],
                "url": "https://www.last.fm/music/Queen/_/Bohemian+Rhapsody",
                "@attr": {"nowplaying": "true"},
            }
        ]
    }
}


def make_plugin() -> LastFmPlugin:
    """A fresh, configured LastFmPlugin. Call only while `requests.get` is patched."""
    plugin = LastFmPlugin(MANIFEST)
    plugin.config = {
        "username": "testuser",
        "api_key": "test_api_key",
        "refresh_seconds": 30,
        "show_album": False,
    }
    return plugin


def test_renders_on_every_board_shape():
    """The plugin must fit, and grow into, every board shape the platform supports.

    ``strict_growth=True``: track/artist/status are fixed content (three
    fields), but the album is real extra detail this plugin has and
    currently doesn't show unless ``show_album`` is on -- a taller board must
    reveal it rather than rendering the same three lines letterboxed in more
    blank rows.

    The suite renders the plugin many times, forwards and backwards, across
    every geometry, and must never touch the network -- ``requests.get`` is
    patched for the whole call so every render (including each call to
    `make_plugin`, which triggers a fetch on first use) stays stubbed.
    """
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = _NOWPLAYING_RESPONSE

    with patch("plugins.last_fm.requests.get", return_value=mock_response):
        assert_board_conformance(
            make_plugin,
            manifest=MANIFEST,
            strict_growth=True,
            require_note_array_preview=True,
        )
