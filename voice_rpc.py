import json
import os
import socket
import struct
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

CLIENT_ID = (os.environ.get("VOICEBOT_CLIENT_ID", "") or "").strip() or "1524129725843247287"
CLIENT_SECRET = os.environ.get("VOICEBOT_CLIENT_SECRET", "").strip()
TOKEN_CACHE_FILE = Path.home() / ".config" / "utstream" / "voicebot_rpc_token.json"

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

SOCKET_TIMEOUT_SECONDS = 10
HTTP_TIMEOUT_SECONDS = 10

OP_HANDSHAKE = 0
OP_FRAME = 1


class VoiceRPCError(Exception):
    pass


def _find_ipc_socket():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    for i in range(10):
        path = runtime_dir + "/discord-ipc-" + str(i)
        if os.path.exists(path):
            return path
    raise VoiceRPCError("No discord-ipc-* socket found. Is the voicebot Discord client running?")


def _send(sock, opcode, payload):
    data = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack("<II", opcode, len(data)) + data)


def _recv(sock):
    header = b""
    while len(header) < 8:
        chunk = sock.recv(8 - len(header))
        if not chunk:
            raise VoiceRPCError("IPC socket closed unexpectedly")
        header += chunk
    opcode, length = struct.unpack("<II", header)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise VoiceRPCError("IPC socket closed unexpectedly")
        data += chunk
    return opcode, json.loads(data.decode("utf-8"))


def _load_token_cache():
    if not TOKEN_CACHE_FILE.exists():
        raise VoiceRPCError(
            "No token cache at " + str(TOKEN_CACHE_FILE) + ". Run voice_rpc_setup.py once via VNC first."
        )
    return json.loads(TOKEN_CACHE_FILE.read_text())


def _save_token_cache(data):
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_FILE.write_text(json.dumps(data))
    TOKEN_CACHE_FILE.chmod(0o600)


def _refresh_access_token(refresh_token):
    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://discord.com/api/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": BROWSER_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise VoiceRPCError("Token refresh failed: HTTP " + str(e.code) + " " + e.read().decode("utf-8", "replace"))
    except Exception as e:
        raise VoiceRPCError("Token refresh failed: " + repr(e))


def get_valid_access_token():
    cache = _load_token_cache()
    expires_at = cache.get("_expires_at", 0)

    if time.time() > expires_at - 3600:
        fresh = _refresh_access_token(cache["refresh_token"])
        fresh["_expires_at"] = time.time() + fresh.get("expires_in", 604800)
        _save_token_cache(fresh)
        return fresh["access_token"]

    return cache["access_token"]


def _authenticated_socket():
    if not CLIENT_SECRET:
        raise VoiceRPCError("VOICEBOT_CLIENT_SECRET not set in environment.")

    access_token = get_valid_access_token()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT_SECONDS)
    try:
        sock.connect(_find_ipc_socket())

        _send(sock, OP_HANDSHAKE, {"v": 1, "client_id": CLIENT_ID})
        _recv(sock)  # READY dispatch

        _send(sock, OP_FRAME, {
            "cmd": "AUTHENTICATE",
            "args": {"access_token": access_token},
            "nonce": "auth",
        })
        opcode, payload = _recv(sock)
        if payload.get("evt") == "ERROR":
            raise VoiceRPCError("Authenticate failed: " + repr(payload))
    except socket.timeout:
        sock.close()
        raise VoiceRPCError("IPC socket timed out (is the Discord client responsive?)")
    except VoiceRPCError:
        sock.close()
        raise
    except Exception as e:
        sock.close()
        raise VoiceRPCError("IPC connection failed: " + repr(e))

    return sock


def select_voice_channel(channel_id):
    sock = _authenticated_socket()
    try:
        _send(sock, OP_FRAME, {
            "cmd": "SELECT_VOICE_CHANNEL",
            "args": {"channel_id": channel_id, "force": True},
            "nonce": "select",
        })
        opcode, payload = _recv(sock)
        if payload.get("evt") == "ERROR":
            raise VoiceRPCError("Select voice channel failed: " + repr(payload))
        return payload
    except socket.timeout:
        raise VoiceRPCError("IPC socket timed out during channel select")
    finally:
        sock.close()


def leave_voice_channel():
    sock = _authenticated_socket()
    try:
        _send(sock, OP_FRAME, {
            "cmd": "SELECT_VOICE_CHANNEL",
            "args": {"channel_id": None},
            "nonce": "leave",
        })
        opcode, payload = _recv(sock)
        if payload.get("evt") == "ERROR":
            raise VoiceRPCError("Leave voice channel failed: " + repr(payload))
        return payload
    except socket.timeout:
        raise VoiceRPCError("IPC socket timed out during leave")
    finally:
        sock.close()
