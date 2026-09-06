"""The server icon declared on `Implementation`.

The icon is a base64 data URI baked into the source, so nothing at runtime
fetches it and nothing at build time regenerates it. That makes it exactly the
kind of asset that rots silently: a truncated paste or a stale re-encode still
imports fine and only shows up as a broken image in a client, if the client
renders server icons at all.

These checks decode the payload and read the PNG header, so a corrupt or
truncated URI fails here rather than in someone's connector list.

Runs standalone (`python tests/test_icon.py`) or under pytest.
"""

from __future__ import annotations

import base64
import binascii
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from garmlink.icon import ICON_DATA_URI, ICON_SIZES  # noqa: E402

PREFIX = "data:image/png;base64,"


def _png() -> bytes:
    assert ICON_DATA_URI.startswith(PREFIX), "icon must be a base64 PNG data URI"
    try:
        return base64.b64decode(ICON_DATA_URI[len(PREFIX):], validate=True)
    except binascii.Error as e:  # pragma: no cover - only on a corrupt paste
        raise AssertionError(f"icon payload is not valid base64: {e}") from e


def test_icon_is_a_valid_png() -> None:
    data = _png()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "icon payload is not a PNG"
    # IHDR is always the first chunk: length(4) + type(4) + w(4) + h(4) + ...
    assert data[12:16] == b"IHDR", "PNG is missing its IHDR chunk"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height, f"icon should be square, got {width}x{height}"
    # IEND is 12 bytes: length(4) + type(4) + CRC(4).
    assert data[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82", "PNG is truncated"


def test_declared_sizes_match_the_payload() -> None:
    width, height = struct.unpack(">II", _png()[16:24])
    assert ICON_SIZES == [f"{width}x{height}"], (
        f"ICON_SIZES {ICON_SIZES} disagrees with the actual {width}x{height} PNG"
    )


def test_server_declares_the_icon() -> None:
    from garmlink.server import mcp

    icons = mcp.icons
    assert icons, "server declares no icons"
    assert any(i.src == ICON_DATA_URI for i in icons), (
        "server's declared icon is not the one in garmlink.icon"
    )


if __name__ == "__main__":
    test_icon_is_a_valid_png()
    test_declared_sizes_match_the_payload()
    print("icon: valid PNG, sizes agree")
    print("(server declaration check needs the fastmcp env; run under pytest)")
