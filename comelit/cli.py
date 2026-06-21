"""Command-line interface for the Comelit ViP intercom.

    comelit config                      # print cached LAN connection config
    comelit open-door [--relay N]       # buzz the entrance relay
    comelit rings                       # stream ring/call events
    comelit video OUT.h264 [--hd]       # record live video (or --udp HOST:PORT)
    comelit record OUT.h264 [--seconds] # fixed clip + measured framerate
    comelit listen OUT.wav [--seconds]  # record panel audio (no noise at door)
    comelit talk FILE --yes             # play audio AT THE DOOR (needs the 'talk' extra)

All commands accept ``--secrets PATH`` (default: $COMELIT_SECRETS, ./secrets.json,
or ~/.config/comelit/secrets.json).
"""
from __future__ import annotations

import argparse
import getpass
import socket
import statistics
import sys
import time
import wave
from pathlib import Path

from . import g711
from ._paths import default_secrets_path
from .credentials import ViperCredentials
from .intercom import Intercom
from .viper import H264Depacketizer

RTP_CLOCK_HZ = 90000


def _intercom(args) -> Intercom:
    timeout = None if getattr(args, "wait", False) else 15.0
    return Intercom.from_secrets(args.secrets, timeout=timeout)


# --- commands ------------------------------------------------------------
def cmd_config(args) -> int:
    credentials = ViperCredentials(args.secrets)
    cfg = credentials.viper
    if not cfg.get("panel_host"):
        raise SystemExit(
            f"no cached LAN configuration in {credentials.path}; "
            "run `comelit bootstrap-local PANEL_IP`"
        )
    print(f"secrets       : {credentials.path}")
    print(f"panel         : {cfg['panel_host']}:{cfg.get('panel_port', 64100)}")
    print(f"source        : {cfg.get('source_address', '(default)')}")
    print(f"entrance      : {cfg.get('entrance_address', '(default)')}")
    print(f"description   : {cfg.get('description', '(none)')}")
    print(f"LAN token     : {'cached' if cfg.get('user_token') else 'missing'}")
    return 0


def cmd_bootstrap_local(args) -> int:
    credentials = ViperCredentials(args.secrets)
    password = args.password or getpass.getpass("Installer password: ")
    user = credentials.bootstrap_local(
        args.host,
        password,
        web_port=args.web_port,
        panel_port=args.panel_port,
        user_slot=args.user_slot,
        description=args.description,
    )
    label = user.description or f"slot {user.slot}"
    print(f"saved LAN credentials for {label} to {credentials.path}")
    return 0


def cmd_open_door(args) -> int:
    with _intercom(args) as panel:
        result = panel.open_door(relay=args.relay, target=args.target)
    print("door opened:", result)
    return 0


def cmd_rings(args) -> int:
    args.wait = True
    with _intercom(args) as panel:
        print(f"listening for rings as {panel.source} ...", flush=True)
        for event in panel.rings():
            door = event.body[2:12].rstrip(b"\x00").decode("ascii", "replace")
            print(
                f"RING {event.received_at.isoformat()} "
                f"door={door} route={event.source}->{event.destination}",
                flush=True,
            )
    return 0


def cmd_video(args) -> int:
    args.wait = True
    quality = "HD" if args.hd else "SD"
    with _intercom(args) as panel:
        print(f"starting local video {panel.source} -> {panel.entrance} [{quality}]", flush=True)
        with panel.video(hd=args.hd, bitrate=args.bitrate) as stream:
            if args.udp:
                host, port = args.udp.rsplit(":", 1)
                destination = (host, int(port))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    for packet in stream.packets():
                        udp.sendto(packet.raw, destination)
            else:
                with args.output.open("wb") as file:
                    for unit in stream.h264():
                        file.write(unit)
                        file.flush()
    return 0


def cmd_record(args) -> int:
    args.wait = True
    quality = "HD" if args.hd else "SD"
    timestamps: list[int] = []
    frames = bytes_written = 0
    started = None
    with _intercom(args) as panel:
        print(f"recording {quality} for {args.seconds}s -> {args.output}", flush=True)
        depacketizer = H264Depacketizer()
        with panel.video(hd=args.hd, bitrate=args.bitrate) as stream, args.output.open("wb") as file:
            for packet in stream.packets():
                if started is None:
                    started = time.monotonic()
                if not timestamps or timestamps[-1] != packet.timestamp:
                    timestamps.append(packet.timestamp)
                for unit in depacketizer.feed(packet):
                    file.write(unit)
                    bytes_written += len(unit)
                    if (unit[4] & 0x1F) in (1, 5):
                        frames += 1
                if time.monotonic() - started >= args.seconds:
                    break

    elapsed = (time.monotonic() - started) if started else 0.0
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    rtp_fps = RTP_CLOCK_HZ / statistics.median(deltas) if deltas else 0.0
    print(f"done: {frames} frames, {bytes_written} bytes in {elapsed:.1f}s", flush=True)
    if elapsed > 0:
        print(f"  fps (RTP timestamps): {rtp_fps:.3f}")
        print(f"  ~bitrate            : {bytes_written * 8 / elapsed / 1000:.0f} kbps")
    print(f"MEASURED_FPS={rtp_fps:.3f}")
    return 0


def cmd_listen(args) -> int:
    samples = 0
    with _intercom(args) as panel:
        print(f"listening to {panel.entrance} for {args.seconds}s -> {args.output}", flush=True)
        with panel.video() as stream:
            stream.enable_audio()
            with wave.open(str(args.output), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(g711.SAMPLE_RATE)
                deadline = time.monotonic() + args.seconds
                for pcm in stream.audio():
                    wav.writeframes(pcm)
                    samples += len(pcm) // 2
                    if time.monotonic() >= deadline:
                        break
    print(f"done: {samples} samples ({samples / g711.SAMPLE_RATE:.1f}s of audio)", flush=True)
    return 0


def _load_pcm_8k_mono(path: Path) -> bytes:
    """Decode any audio file to signed 16-bit LE 8 kHz mono PCM (needs PyAV)."""
    try:
        import av
    except ImportError:
        raise SystemExit(
            "the 'talk' command needs PyAV. Install it with: "
            "uv tool install 'comelit-vip[talk]'"
        )
    container = av.open(str(path))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=g711.SAMPLE_RATE)
    pcm = bytearray()
    for frame in container.decode(audio=0):
        for out in resampler.resample(frame):
            pcm.extend(bytes(out.planes[0])[: out.samples * 2])
    for out in resampler.resample(None):
        pcm.extend(bytes(out.planes[0])[: out.samples * 2])
    container.close()
    return bytes(pcm)


def cmd_talk(args) -> int:
    if not args.yes:
        print("Refusing without --yes: this plays sound at the door.", file=sys.stderr)
        return 2
    pcm = _load_pcm_8k_mono(args.audio)
    print(f"loaded {len(pcm)//2} samples ({len(pcm)/2/g711.SAMPLE_RATE:.1f}s)", flush=True)
    with _intercom(args) as panel:
        with panel.video() as stream:
            stream.enable_audio()
            # The panel takes ~4.4 s after VOICESTATUS to open its voice path;
            # lead with silence so we don't clip the start of the audio.
            silence = b"\x00\x00" * int(g711.SAMPLE_RATE * args.warmup)
            print(f"warm-up {args.warmup}s, then talking...", flush=True)
            stream.send_audio_pcm(silence + pcm)
            print("done talking", flush=True)
            time.sleep(0.3)
    return 0


# --- parser --------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="comelit", description="Comelit ViP intercom client.")
    parser.add_argument(
        "--secrets",
        type=Path,
        default=None,
        help="path to secrets.json (default: $COMELIT_SECRETS, ./secrets.json, ~/.config/comelit)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config", help="print cached LAN connection config").set_defaults(
        func=cmd_config
    )

    p = sub.add_parser(
        "bootstrap-local",
        help="bootstrap LAN credentials from the panel installer web UI",
    )
    p.add_argument("host", help="panel IP address or web UI URL")
    p.add_argument(
        "--password",
        default=None,
        help="local installer web UI password (default: prompt securely)",
    )
    p.add_argument("--web-port", type=int, default=8080)
    p.add_argument("--panel-port", type=int, default=64100)
    select = p.add_mutually_exclusive_group()
    select.add_argument("--user-slot", type=int, default=None)
    select.add_argument("--description", default=None, help="exact panel user description")
    p.set_defaults(func=cmd_bootstrap_local)

    p = sub.add_parser("open-door", help="open an entrance relay")
    p.add_argument("--relay", type=int, default=1)
    p.add_argument("--target", default=None, help="entrance ViP address (default: configured)")
    p.set_defaults(func=cmd_open_door)

    sub.add_parser("rings", help="stream ring/call events").set_defaults(func=cmd_rings)

    p = sub.add_parser("video", help="record live video to a file or forward RTP over UDP")
    out = p.add_mutually_exclusive_group(required=True)
    out.add_argument("output", nargs="?", type=Path, help="write Annex-B H.264")
    out.add_argument("--udp", metavar="HOST:PORT", help="forward raw RTP packets")
    p.add_argument("--hd", action="store_true", help="request HD (640x240) instead of SD (320x240)")
    p.add_argument("--bitrate", type=int, default=None, help="encoder bitrate kbps (0=panel default)")
    p.set_defaults(func=cmd_video)

    p = sub.add_parser("record", help="record a fixed clip and report the measured framerate")
    p.add_argument("output", type=Path, help="raw .h264 output path")
    p.add_argument("--hd", action="store_true")
    p.add_argument("--bitrate", type=int, default=None)
    p.add_argument("--seconds", type=float, default=15.0)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("listen", help="record panel audio to a WAV (no noise at the door)")
    p.add_argument("output", type=Path, help="output .wav path")
    p.add_argument("--seconds", type=float, default=10.0)
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("talk", help="play an audio file AT THE DOOR (makes noise outside)")
    p.add_argument("audio", type=Path, help="audio file to play")
    p.add_argument("--yes", action="store_true", help="confirm: this makes noise at the door")
    p.add_argument("--warmup", type=float, default=4.5, help="leading silence (s) for voice warm-up")
    p.set_defaults(func=cmd_talk)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.secrets is None:
        args.secrets = default_secrets_path()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
