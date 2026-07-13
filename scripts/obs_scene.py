#!/usr/bin/env python3
import os
import sys
from obsws_python import ReqClient


def fmt_mmss(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: obs_scene.py <SceneName> [--countdown SECONDS]", file=sys.stderr)
        return 2
    scene = sys.argv[1]
    countdown_seconds = None
    if len(sys.argv) == 4 and sys.argv[2] == "--countdown":
        try:
            countdown_seconds = int(sys.argv[3])
        except ValueError:
            print("countdown must be integer seconds", file=sys.stderr)
            return 2
    host = os.environ.get("OBS_WS_HOST", "127.0.0.1")
    port = int(os.environ.get("OBS_WS_PORT", "4455"))
    password = os.environ.get("OBS_WS_PASSWORD", "")
    cl = ReqClient(host=host, port=port, password=password, timeout=2)
    cl.set_current_program_scene(scene)
    if countdown_seconds is not None:
        text = f"Stream stops in: {fmt_mmss(countdown_seconds)}"
        cl.set_input_settings(
            name="IdleCountdown",
            settings={"text": text},
            overlay=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
