# UT Streambot

Discord-controlled Unreal Tournament spectator + OBS streaming automation. I wrote most of the script stuff. ChatGPT helped me with the discord implementation which is nice. Claude fucked up everything when it wanted to add voice communications to it. After fixing it myself, Claude added fancy buttons to push for discord and it made this repo. This vibe coding does make stuff easy, when it works and doesnt break your Linux system.
Full write-up: https://www.zkyp.nl/UT/streambot.html

## Components

| File | Job |
|---|---|
| `bot.py` | Discord bot: `!stream` commands, auto Stream/Watch buttons + comms dropdown when a pug fills, voicebot control, Twitch title updates, pending-target queueing |
| `voice_rpc.py` | Controls a real Discord client ("voicebot") over the local RPC socket to join/leave team voice channels. Bots can't decrypt Discord voice anymore (DAVE protocol), so a real client does the listening |
| `voice_rpc_setup.py` | One-time RPC authorization (run via VNC with the voicebot client open) |
| `twitch_api.py` | Stream title updates via Helix; device-code tokens, auto-refresh |
| `twitch_setup.py` | One-time Twitch authorization (device code, any browser) |
| `scripts/stream_session.sh` | Session controller: starts UT + OBS, watches the UT log for game end / password fail / fallback maps (Coret, Terra*), waiting scene with resume window, pending-target pickup |
| `scripts/obs_scene.py` | OBS websocket scene switching + idle countdown text |
| `scripts/stream_stop.sh` | Stops controller + UT + OBS, clears state. **Reconstructed** - see below |
| `scripts/utstream_resetall.sh` | Nuke-and-restart everything |
| `systemd/` | Service units: bot (system), OBS + UT (user session) |

## Deploy layout on the box

```
/home/zkyp/utbot/          bot.py, voice_rpc*.py, twitch*.py, venv/
/home/zkyp/bin/            scripts/*
/etc/systemd/system/       utbot.service
~/.config/systemd/user/    obs-stream.service, ut99.service
~/.config/utstream/        bot.env, obs.env, tokens, runtime state (all gitignored)
```

## Setup (short version)

1. `python3 -m venv venv && venv/bin/pip install "discord.py[voice]" obsws-python`
2. `cp bot.env.example ~/.config/utstream/bot.env` and fill in; same for `obs.env.example`
3. One-time auths: `venv/bin/python twitch_setup.py` (any browser, logged in as the
   channel) and `venv/bin/python voice_rpc_setup.py` (VNC, voicebot client open,
   click Authorize)
4. Voicebot client: Speaker output -> the sink OBS captures; mic muted +
   push-to-talk with no keybind
5. Install units, `systemctl enable --now utbot.service`

## Secrets removed for this repo

The following were scrubbed or excluded compared to what runs on the box.
**If you clone this fresh, these must be re-provided via the env files:**

- `systemd/obs-stream.service`: the hardcoded `OBS_WS_PASSWORD` (and unused
  `OBS_WS_HOST`/`OBS_WS_PORT`/scene env lines) were **removed**. The websocket
  credentials live in `~/.config/utstream/obs.env` (gitignored), which is what
  `obs_scene.py` actually reads. Rotate the OBS websocket password if reusing
  an old deployment - the old one existed in plaintext in multiple places.
- Legacy `start_stream.sh` / `stop_stream.sh` (pre-systemd, with another
  hardcoded OBS password): **excluded entirely**, they were dead code.
- Discord bot token, voicebot OAuth client secret, Twitch client ID:
  never hardcoded, provided via `bot.env` (see `bot.env.example`).
- Token caches (`voicebot_rpc_token.json`, `twitch_token.json`) and all
  runtime state files: gitignored.
- The voicebot Discord application ID is env-configurable
  (`VOICEBOT_CLIENT_ID`); the default in code is a public identifier, not
  a secret.

## Known caveats

- `/usr/local/bin/xenv.sh` (X display env for the user session) is referenced
  by the services but not included - it's machine-specific.
- Known host quirk: PipeWire/PulseAudio can come up stuck after boot;
  `systemctl --user restart pipewire wireplumber` clears it. Do NOT install
  wireplumber as the session manager on this Ubuntu 22.04 box - it fights
  PulseAudio for the sound card. Ask us how we know (hint: fuck you Claude).
