# comelit-vip

A Python client for **Comelit ViP** door intercoms, talking the native ViP
protocol **directly over your LAN** (TCP `:64100`). No cloud round-trip for the
live features. Reverse-engineered from the official app (`com.comelit.bigapp`
7.4.0-4) and verified live.

It can:

- **Open the door** / trigger entrance relays
- **Receive ring/call events** as they happen
- **Stream live video** (H.264, SD or HD)
- **Listen** to the entrance panel microphone (G.711 audio)
- **Talk back** through the panel loudspeaker (two-way audio)

> ⚠️ **Use it only on intercoms you own/administer.** Talk-back plays sound out
> of the entrance panel — it makes audible noise at the door. Opening the door is
> a physical action. Don't point this at hardware that isn't yours.

## Install

Install the `comelit` command directly from Git with
[uv](https://docs.astral.sh/uv/):

```bash
uv tool install "comelit-vip @ git+https://github.com/ttmx/comelit-vip"
# with talk-back (file → door audio) support, which pulls in PyAV:
uv tool install "comelit-vip[talk] @ git+https://github.com/ttmx/comelit-vip"
```

To use the library from another uv-managed project instead:

```bash
uv add "comelit-vip @ git+https://github.com/ttmx/comelit-vip"
```

Python 3.10+. The core (LAN protocol, video, receive/send audio) needs only
`requests`; the optional `talk` extra adds [PyAV](https://pyav.org) just for
decoding arbitrary audio files for talk-back.

## Local bootstrap (recommended)

If the panel's installer web UI is enabled, bootstrap directly from its local
configuration backup:

```bash
comelit bootstrap-local 192.168.55.4
```

This logs into port 8080, creates and downloads a configuration backup, extracts
an active persistent ViP LAN token, and writes it with the panel address to
`secrets.json`. If the panel contains several active users, select one with
`--user-slot N` or `--description NAME`. No app traffic interception, account
login, or cloud service is involved.

The command prompts for the installer password without echoing it. You can also
pass `--password`, though that may expose it in shell history and process lists.
The password is used for this command only and is not saved. Configuration
backups contain sensitive network and account data; the downloaded bytes are
parsed in memory and are not retained.

The secrets file is read from, in order: the path you pass explicitly, then
`$COMELIT_SECRETS`, then `./secrets.json`, then `~/.config/comelit/secrets.json`.
If the cached token is revoked, rerun `bootstrap-local`.

## Quick start (library)

```python
from comelit import Intercom

with Intercom.from_secrets() as panel:        # connects, authenticates, inits
    panel.open_door()                          # buzz the entrance relay

    # live video → raw Annex-B H.264
    with panel.video(hd=True) as stream:
        with open("door.h264", "wb") as f:
            for nal in stream.h264():
                f.write(nal)

    # two-way audio
    with panel.video() as stream:
        stream.enable_audio()
        for pcm in stream.audio():             # 16-bit LE 8 kHz mono from the door
            ...
        stream.send_audio_pcm(my_pcm)          # ⚠️ plays at the door

    # ring/call events
    for ring in panel.rings():
        print("ring from", ring.source)
```

Mux recorded video into a playable file with the measured framerate, e.g.:

```bash
ffmpeg -r 16 -i door.h264 -c copy door.mp4
```

## Quick start (CLI)

The package installs a `comelit` command:

```bash
comelit config                       # print cached LAN connection config
comelit open-door --relay 1          # buzz the entrance relay
comelit rings                        # stream ring/call events
comelit video door.h264 --hd         # record live video (or: --udp 127.0.0.1:5000)
comelit record clip.h264 --seconds 5 # fixed clip + measured framerate
comelit listen door.wav --seconds 10 # record panel audio (no noise at the door)
comelit talk hello.wav --yes         # ⚠️ play audio AT THE DOOR (needs [talk] extra)

comelit --secrets /path/to/secrets.json open-door   # explicit secrets path
```

## API overview

High-level:

- **`Intercom`** — the facade. `Intercom.from_secrets(path=None)`, context
  manager; `.open_door()`, `.rings()`, `.video(hd=...)`, `.source`, `.entrance`,
  `.client` (the underlying `ViperClient`).

Low-level (for advanced use):

- **`ViperClient`** — the raw LAN ViP protocol: `authenticate`,
  `get_configuration`, `listen_rings`, `open_door`, `open_video_stream`.
- **`VideoStream`** — a live call: `h264()`, `packets()`, `audio()`,
  `audio_packets()`, `enable_audio()`, `disable_audio()`, `send_audio_pcm()`.
- **`PanelWebClient`** — retrieve LAN credentials from the installer UI.
- **`ViperCredentials`** — bootstrap and persist the LAN-only `secrets.json`.
- **`g711`** — the A-law (PCMA) codec used on the wire.

See [`examples/`](examples) for runnable scripts of every feature.

## Development

`uv` manages the project environment and lockfile:

```bash
uv sync --extra talk
uv run python -m unittest discover -s tests -v
uv run comelit --help
uv build
```

Use `uv lock --check` and `uv sync --locked` in CI to ensure the
committed lockfile is current and installs reproducibly.

## Scope & limitations

- **LAN-direct only.** Cloud/P2P remote operation is intentionally out of scope.
- Verified against an MSVF entrance panel; other ViP models may differ
  (e.g. supported video resolutions).

## License

Released to everyone under **GNU GPL v3 or later**. In addition, **ttmx
(git@tteles.dev), as the sole copyright holder, can do whatever he wants with
the work**, including relicensing it under other terms. See [LICENSE](LICENSE).
