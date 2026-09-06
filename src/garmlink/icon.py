"""The garmlink server icon.

The same mark the training-plan PWA uses (its `icons/icon.svg`), inlined here as
a data URI rather than served from a URL. The MCP spec tells clients to fetch
icons *without credentials* and to prefer an icon origin matching the server's;
garmlink sits behind OAuth, so a URL would have to be an unauthenticated route
punched through the auth layer. A data URI sidesteps that entirely.

Regenerate by base64-encoding `icons/icon-192.png` from the training-plan repo.

No `mime_type` is declared. The spec treats it as an override for when the
source type is missing or generic, and a `data:image/png;base64,` URI is
neither. Omitting it also dodges a spelling trap: mcp 1.24 declares the field
as `mimeType` with `extra="allow"` and no `populate_by_name`, so a snake_case
kwarg there is silently swallowed as an extra field rather than rejected,
while newer mcp declares `mime_type` with a camelCase alias. `src` and
`sizes` are spelled the same in both.
"""

from __future__ import annotations

_B64 = (
    "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"
)

ICON_DATA_URI = "data:image/png;base64," + _B64
ICON_SIZES = ["192x192"]
