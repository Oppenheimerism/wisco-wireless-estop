#!/usr/bin/env python3
"""Fake ESP32-S3 wireless E-stop transmitter.

Sends "ALIVE <seq>" every 50 ms over UDP. While the E-stop button is pressed
it sends "STOP <seq>" every 20 ms instead (same as the ESP32 firmware).
The sequence number lets the receiver count lost packets.

Keys:
  s  press / release the E-stop button (real E-stops latch mechanically;
     you twist to release, so this is a toggle)
  k  kill the transmitter (battery dead / ESP crashed); press again to revive
  l  toggle lossy-radio mode (drops --loss fraction of packets, default 50%)
  q  quit
"""

import argparse
import random
import select
import socket
import time

from common import KeyReader, log


class FakeESP:
    """What the ESP32 firmware does, minus the hardware.

    Call step(now) often; it sends a packet when one is due and returns how
    many seconds until the next one. The toggle_* methods are the three things
    that can happen to a real transmitter.
    """

    def __init__(self, sock, dest, period_ms=50, stop_period_ms=20, loss=0.5):
        self.sock = sock
        self.dest = dest
        self.period = period_ms / 1000
        self.stop_period = stop_period_ms / 1000
        self.loss = loss
        self.button_pressed = False
        self.powered = True
        self.lossy = False
        self.seq = 0
        self.sent_alive = self.sent_stop = self.dropped = 0
        self.next_send = time.monotonic()
        self._send_error_logged = False

    def step(self, now):
        if not self.powered:
            return 0.1
        if now >= self.next_send:
            msg = "STOP" if self.button_pressed else "ALIVE"
            self.seq += 1
            if self.lossy and random.random() < self.loss:
                self.dropped += 1
            else:
                try:
                    self.sock.sendto(f"{msg} {self.seq}".encode(), self.dest)
                    if msg == "STOP":
                        self.sent_stop += 1
                    else:
                        self.sent_alive += 1
                except OSError as e:
                    if not self._send_error_logged:
                        log(f"send failed ({e}); continuing", "yellow")
                        self._send_error_logged = True
            self.next_send = now + (self.stop_period if self.button_pressed else self.period)
        return max(0.0, self.next_send - time.monotonic())

    def toggle_button(self):
        self.button_pressed = not self.button_pressed
        if self.button_pressed:
            log("E-STOP BUTTON PRESSED -> sending STOP", "red")
        else:
            log("E-stop button released (twisted out) -> back to ALIVE", "green")
        self.next_send = time.monotonic()  # react immediately

    def toggle_power(self):
        self.powered = not self.powered
        if self.powered:
            log("ESP revived -> heartbeat resumes", "green")
            self.next_send = time.monotonic()
        else:
            log("ESP KILLED -> no more packets of any kind", "red")

    def toggle_lossy(self):
        self.lossy = not self.lossy
        if self.lossy:
            log(f"Lossy radio ON: dropping ~{self.loss:.0%} of packets", "yellow")
        else:
            log("Lossy radio OFF", "green")


def main():
    p = argparse.ArgumentParser(description="Fake ESP32 E-stop transmitter")
    p.add_argument("--host", default="127.0.0.1", help="receiver IP (RK3588 would be 192.168.49.1)")
    p.add_argument("--port", type=int, default=4242)
    p.add_argument("--period-ms", type=float, default=50, help="heartbeat period")
    p.add_argument("--stop-period-ms", type=float, default=20, help="STOP resend period while pressed")
    p.add_argument("--loss", type=float, default=0.5, help="drop probability in lossy mode")
    args = p.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    esp = FakeESP(sock, (args.host, args.port), args.period_ms, args.stop_period_ms, args.loss)

    log(f"Fake ESP32 sending to {args.host}:{args.port}, heartbeat every {args.period_ms:.0f} ms")
    log("Keys: s = E-stop button   k = kill/revive ESP   l = lossy radio   q = quit")

    with KeyReader() as keys:
        while True:
            wait = esp.step(time.monotonic())
            ready, _, _ = select.select(keys.select_list(), [], [], wait)
            if not ready:
                continue
            key = keys.read_key()
            if key == "s":
                esp.toggle_button()
            elif key == "k":
                esp.toggle_power()
            elif key == "l":
                esp.toggle_lossy()
            elif key == "q":
                break

    log(f"Sender exiting. sent={esp.sent_alive + esp.sent_stop} dropped_on_purpose={esp.dropped}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
