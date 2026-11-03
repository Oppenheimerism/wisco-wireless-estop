#!/usr/bin/env python3
"""Talk to a running receiver.py on this machine.

  python3 estopctl.py status   # print state
  python3 estopctl.py reset    # arm / reset (same checks as pressing r)

Works over SSH: the control port only listens on 127.0.0.1.
"""

import argparse
import socket
import sys


def main():
    p = argparse.ArgumentParser(description="E-stop receiver control")
    p.add_argument("command", choices=["status", "reset"])
    p.add_argument("--control-port", type=int, default=4250)
    args = p.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    s.sendto(args.command.upper().encode(), ("127.0.0.1", args.control_port))
    try:
        reply, _ = s.recvfrom(4096)
    except (socket.timeout, ConnectionRefusedError):
        sys.exit("no answer - is receiver.py running?")
    print(reply.decode())
    if args.command == "reset" and "state=RUNNING" not in reply.decode():
        sys.exit(1)  # reset refused; reason is in the line above


if __name__ == "__main__":
    main()
