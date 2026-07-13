import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

TWITCH_CLIENT_ID = (os.environ.get("TWITCH_CLIENT_ID", "") or "").strip()
TOKEN_CACHE_FILE = Path.home() / ".config" / "utstream" / "twitch_token.json"

HTTP_TIMEOUT_SECONDS = 10
TITLE_MAX_LEN = 140


class TwitchAPIError(Exception):
    pass


def _http_json(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        raise TwitchAPIError("HTTP " + str(e.code) + ": " + e.read().decode("utf-8", "replace"))
    except Exception as e:
        raise TwitchAPIError("Request failed: " + repr(e))


def _load_cache():
    if not TOKEN_CACHE_FILE.exists():
        raise TwitchAPIError(
            "No Twitch token cache at " + str(TOKEN_CACHE_FILE) + ". Run twitch_setup.py once first."
        )
    return json.loads(TOKEN_CACHE_FILE.read_text())


def _save_cache(data):
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_FILE.write_text(json.dumps(data))
    TOKEN_CACHE_FILE.chmod(0o600)


def _refresh(cache):
    body = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": cache["refresh_token"],
    }).encode("utf-8")
    fresh = _http_json(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    fresh["_expires_at"] = time.time() + fresh.get("expires_in", 14400)
    # keep broadcaster id across refreshes
    if "_broadcaster_id" in cache:
        fresh["_broadcaster_id"] = cache["_broadcaster_id"]
    _save_cache(fresh)
    return fresh


def _get_valid_cache():
    if not TWITCH_CLIENT_ID:
        raise TwitchAPIError("TWITCH_CLIENT_ID not set in environment.")
    cache = _load_cache()
    if time.time() > cache.get("_expires_at", 0) - 300:
        cache = _refresh(cache)
    return cache


def _get_broadcaster_id(cache):
    if "_broadcaster_id" in cache:
        return cache["_broadcaster_id"]
    data = _http_json(
        "https://api.twitch.tv/helix/users",
        headers={
            "Authorization": "Bearer " + cache["access_token"],
            "Client-Id": TWITCH_CLIENT_ID,
        },
    )
    users = data.get("data", [])
    if not users:
        raise TwitchAPIError("Could not resolve broadcaster id from token.")
    cache["_broadcaster_id"] = users[0]["id"]
    _save_cache(cache)
    return cache["_broadcaster_id"]


def set_stream_title(title):
    title = (title or "").strip()
    if not title:
        raise TwitchAPIError("Empty title.")
    if len(title) > TITLE_MAX_LEN:
        title = title[: TITLE_MAX_LEN - 1] + "…"

    cache = _get_valid_cache()
    broadcaster_id = _get_broadcaster_id(cache)

    body = json.dumps({"title": title}).encode("utf-8")
    _http_json(
        "https://api.twitch.tv/helix/channels?broadcaster_id=" + broadcaster_id,
        data=body,
        headers={
            "Authorization": "Bearer " + cache["access_token"],
            "Client-Id": TWITCH_CLIENT_ID,
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    return title
