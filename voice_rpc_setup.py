"""One-time Discord RPC authorization for the voicebot client.
Run once via VNC with the voicebot Discord client open; click Authorize
in the client when prompted. Tokens are cached and auto-refreshed after.
"""
import json
import os
import socket
import struct
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

CLIENT_ID = (os.environ.get("VOICEBOT_CLIENT_ID", "") or "").strip() or "1524129725843247287"
CLIENT_SECRET = os.environ.get("VOICEBOT_CLIENT_SECRET", "").strip()
TOKEN_CACHE_FILE = Path.home() / ".config" / "utstream" / "voicebot_rpc_token.json"

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

OP_HANDSHAKE = 0
OP_FRAME = 1


def find_ipc_socket():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    for i in range(10):
        path = runtime_dir + "/discord-ipc-" + str(i)
        if os.path.exists(path):
            return path
    raise RuntimeError("No discord-ipc-* socket found. Is the Discord client running?")


def send(sock, opcode, payload):
    data = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack("<II", opcode, len(data)) + data)


def recv(sock):
    header = b""
    while len(header) < 8:
        header += sock.recv(8 - len(header))
    opcode, length = struct.unpack("<II", header)
    data = b""
    while len(data) < length:
        data += sock.recv(length - len(data))
    return opcode, json.loads(data.decode("utf-8"))


def exchange_code_for_token(code):
    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://localhost",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://discord.com/api/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": BROWSER_UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("Token exchange failed:", e.code, e.read().decode("utf-8", "replace"))
        raise


def main():
    if not CLIENT_SECRET:
        print("VOICEBOT_CLIENT_SECRET not set.")
        return

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(find_ipc_socket())

    send(sock, OP_HANDSHAKE, {"v": 1, "client_id": CLIENT_ID})
    print("Handshake:", recv(sock))

    send(sock, OP_FRAME, {"cmd": "AUTHORIZE", "args": {"client_id": CLIENT_ID, "scopes": ["rpc"]}, "nonce": "setup-1"})
    print("Check the Discord client - click Authorize.")
    opcode, payload = recv(sock)
    print("Authorize response:", payload)

    if payload.get("evt") == "ERROR":
        print("Authorization failed.")
        return

    token_data = exchange_code_for_token(payload["data"]["code"])
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_FILE.write_text(json.dumps(token_data))
    TOKEN_CACHE_FILE.chmod(0o600)
    print("Done. Token saved to " + str(TOKEN_CACHE_FILE))
    sock.close()


if __name__ == "__main__":
    main()
