"""One-time Twitch authorization via device code flow.
Run this once. No secret needed (public client). No VNC needed:
authorize from any browser where the Twitch channel account is logged in.
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

TWITCH_CLIENT_ID = (os.environ.get("TWITCH_CLIENT_ID", "") or "").strip()
TOKEN_CACHE_FILE = Path.home() / ".config" / "utstream" / "twitch_token.json"
SCOPES = "channel:manage:broadcast"


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, json.loads(e.read().decode("utf-8", "replace") or "{}")


def main():
    if not TWITCH_CLIENT_ID:
        print("TWITCH_CLIENT_ID not set in environment.")
        return

    dev, err = post_form("https://id.twitch.tv/oauth2/device", {
        "client_id": TWITCH_CLIENT_ID,
        "scopes": SCOPES,
    })
    if err:
        print("Device code request failed:", err)
        return

    print()
    print("1. Open this URL in a browser logged in as the Twitch CHANNEL account:")
    print("   " + dev["verification_uri"])
    print("2. Enter this code: " + dev["user_code"])
    print()
    print("Waiting for authorization...")

    interval = dev.get("interval", 5)
    deadline = time.time() + dev.get("expires_in", 1800)

    while time.time() < deadline:
        time.sleep(interval)
        tok, err = post_form("https://id.twitch.tv/oauth2/token", {
            "client_id": TWITCH_CLIENT_ID,
            "device_code": dev["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        if tok:
            tok["_expires_at"] = time.time() + tok.get("expires_in", 14400)
            TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE_FILE.write_text(json.dumps(tok))
            TOKEN_CACHE_FILE.chmod(0o600)
            print("Authorized. Token saved to " + str(TOKEN_CACHE_FILE))
            print("This never needs doing again unless the authorization is revoked.")
            return
        msg = (err or {}).get("message", "")
        if msg == "authorization_pending":
            continue
        if msg == "slow_down":
            interval += 2
            continue
        print("Authorization failed:", err)
        return

    print("Timed out waiting for authorization. Run again.")


if __name__ == "__main__":
    main()
