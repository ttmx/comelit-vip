#!/usr/bin/env python3
"""Receive the entrance-panel video stream over the LAN.

Writes Annex-B H.264 to a file, or forwards raw RTP packets over UDP.
(`comelit video` is the installed CLI equivalent.)
"""
import argparse
import pathlib
import socket

from comelit import Intercom


def main():
    parser = argparse.ArgumentParser()
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=pathlib.Path, help="write Annex-B H.264")
    output.add_argument("--udp", metavar="HOST:PORT", help="forward raw RTP packets")
    parser.add_argument("--hd", action="store_true", help="request HD (640x240) instead of SD")
    parser.add_argument("--bitrate", type=int, default=None, help="encoder bitrate kbps (0=panel default)")
    args = parser.parse_args()

    with Intercom.from_secrets(timeout=None) as panel:
        quality = "HD" if args.hd else "SD"
        print(f"starting local video {panel.source} -> {panel.entrance} [{quality}]", flush=True)
        with panel.video(hd=args.hd, bitrate=args.bitrate) as stream:
            if args.output:
                with args.output.open("wb") as file:
                    for unit in stream.h264():
                        file.write(unit)
                        file.flush()
            else:
                host, port = args.udp.rsplit(":", 1)
                destination = (host, int(port))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    for packet in stream.packets():
                        udp.sendto(packet.raw, destination)


if __name__ == "__main__":
    main()
