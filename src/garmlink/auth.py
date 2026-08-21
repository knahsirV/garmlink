"""CLI script to authenticate with Garmin Connect and save tokens."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path

from .client import GarminClient


def main() -> None:
    """Authenticate with Garmin Connect and save tokens to ~/.garminconnect/."""
    email = input("Garmin email: ")
    password = getpass("Garmin password: ")
    tokenstore = Path.home() / ".garminconnect"
    tokenstore.mkdir(mode=0o700, exist_ok=True)

    client = GarminClient(email, password, str(tokenstore))
    print("Authenticating with Garmin Connect...")
    client.authenticate()

    print("\n✓ Authentication successful. Tokens saved to ~/.garminconnect/\n")
    print("To deploy, run:  ./scripts/setup-cloudrun.sh")
    print("It reads the token file directly and stores it in Secret Manager as")
    print("GARMIN_TOKENS_JSON — no need to copy anything by hand.")


if __name__ == "__main__":
    main()
