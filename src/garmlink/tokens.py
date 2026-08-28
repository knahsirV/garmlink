"""Durable storage for the Garmin token blob.

Garmin rotates the DI refresh token on every refresh: `_refresh_di_token`
replaces `di_refresh_token` with whatever the response carries, which
invalidates the value we presented. garminconnect then writes the new blob back
to whatever tokenstore path it was loaded from.

On Cloud Run that path is `/tmp`, which dies with the instance, and the seed in
`GARMIN_TOKENS_JSON` is an immutable env var. So the rotated token was written
and immediately thrown away, and the next cold start presented a refresh token
Garmin had already invalidated — every login after the first rotation failed
with "Failed to retrieve social profile".

This is the same problem, and the same fix, as the OAuth state in
`auth_provider.build_oauth_store`: a credential that changes at runtime cannot
live on an ephemeral filesystem behind a scale-to-zero service.

The env var stays as the *seed*: it is what bootstraps a brand new deployment,
or recovers one whose stored blob has gone bad. Once a rotation has been
persisted here, the stored copy wins.
"""

from __future__ import annotations

from key_value.aio.protocols import AsyncKeyValue

__all__ = ["TOKEN_COLLECTION", "TOKEN_KEY", "GarminTokenStore"]

# A separate collection from the OAuth state. These are Garmin credentials, not
# claude.ai's OAuth records, and nothing should be able to iterate one and reach
# the other.
TOKEN_COLLECTION = "garmin-tokens"

# One Garmin account per deployment, so a single fixed key. Slash-free, so the
# Firestore key sanitizer leaves it alone.
TOKEN_KEY = "tokenstore"


class GarminTokenStore:
    """Reads and writes the current Garmin token blob.

    The blob is exactly what `Garmin.client.dumps()` produces — `di_token`,
    `di_refresh_token`, `di_client_id` — and is fed back to `Garmin.login()`,
    which accepts inline JSON as well as a path.
    """

    def __init__(self, kv: AsyncKeyValue) -> None:
        self._kv = kv

    async def load(self) -> str | None:
        """Return the stored blob, or None if nothing has been persisted yet."""
        record = await self._kv.get(key=TOKEN_KEY, collection=TOKEN_COLLECTION)
        if not record:
            return None
        blob = record.get("tokens")
        # A malformed record must read as "nothing stored" so the caller falls
        # back to the seed rather than handing garbage to login().
        if not isinstance(blob, str) or not blob.strip():
            return None
        return blob

    async def save(self, blob: str) -> None:
        """Persist the current blob, replacing any previous one."""
        await self._kv.put(
            key=TOKEN_KEY, value={"tokens": blob}, collection=TOKEN_COLLECTION
        )
