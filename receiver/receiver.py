#!/usr/bin/env python3
"""RK3588 E-stop receiver.

Listens for "ALIVE <seq>" / "STOP <seq>" UDP packets from the ESP32 and drives
the E-stop GPIO. On a laptop the GPIO is printed (--gpio fake, the default);
on the RK3588 use --gpio gpiod --pin GPIO1_B2 (or --chip/--line).

Arm / reset, three ways (all go through the same checks):
  * press r in this terminal (when run interactively)
  * `python3 estopctl.py reset` on the same machine (e.g. over SSH), which
    sends "RESET" to 127.0.0.1:<control port>
  * `python3 estopctl.py status` prints the current state

Safety rules (fixes over the original doc code, see README):
  * Power starts OFF. It only goes on after N heartbeats in a row AND the
    operator presses r. No packets -> no power.
  * STOP packet, or no ALIVE for > 150 ms while running -> power cut and
    LATCHED. The process keeps running and keeps holding the pin LOW.
  * Leaving LATCHED needs a healthy link again (button released, N fresh
    heartbeats in a row) AND a deliberate r. It never re-powers on its own.

States:
  BOOT     power off, link not healthy yet
  READY    power off, link healthy, waiting for r (arm)
  RUNNING  power on
  LATCHED  power off after a stop; needs healthy link + r (reset)

Keys:  r = arm / reset    i = status    q = quit (power is cut first)
"""

import argparse
import select
import signal
import socket
import time

from common import KeyReader, log
from gpio_out import FakeGPIO

BOOT, READY, RUNNING, LATCHED = "BOOT", "READY", "RUNNING", "LATCHED"


class Receiver:
    def __init__(self, gpio, timeout_s, needed):
        self.gpio = gpio
        self.timeout = timeout_s
        self.needed = needed          # heartbeats in a row before arm/reset is allowed
        self.state = BOOT
        self.last_alive = None        # monotonic time of last ALIVE
        self.last_stop = None         # monotonic time of last STOP
        self.streak = 0               # consecutive on-time ALIVE packets
        self.was_healthy = False
        self.last_seq = None
        self.rx_alive = self.rx_stop = self.seq_gaps = 0
        self.gpio.set(False)          # safe state before anything else

    # ---- link health -------------------------------------------------
    def healthy(self, now):
        return (self.streak >= self.needed
                and self.last_alive is not None
                and now - self.last_alive <= self.timeout
                and (self.last_stop is None or now - self.last_stop > self.timeout))

    def why_not_healthy(self, now):
        if self.last_stop is not None and now - self.last_stop <= self.timeout:
            return "STOP is still being received - release the E-stop button first"
        if self.last_alive is None:
            return "no heartbeat received yet"
        if now - self.last_alive > self.timeout:
            return f"no heartbeat for {(now - self.last_alive) * 1000:.0f} ms"
        return f"need {self.needed} heartbeats in a row, have {self.streak}"

    # ---- events ------------------------------------------------------
    def on_packet(self, data, now):
        parts = data.decode(errors="ignore").split()
        if not parts:
            return
        kind = parts[0]
        seq = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if seq is not None:
            if self.last_seq is not None and seq > self.last_seq + 1:
                self.seq_gaps += seq - self.last_seq - 1
            self.last_seq = seq

        if kind == "ALIVE":
            self.rx_alive += 1
            on_time = self.last_alive is not None and now - self.last_alive <= self.timeout
            self.streak = self.streak + 1 if on_time else 1
            self.last_alive = now
        elif kind == "STOP":
            self.rx_stop += 1
            first = self.last_stop is None or now - self.last_stop > self.timeout
            self.last_stop = now
            self.streak = 0
            if self.state == RUNNING:
                self.latch("STOP packet received (E-stop button pressed)")
            elif first:
                log("STOP received (power already off)", "yellow")
        # anything else is ignored on purpose

    def tick(self, now):
        # Heartbeat timeout: the only check that catches a dead/out-of-range ESP.
        if self.last_alive is not None and now - self.last_alive > self.timeout:
            if self.state == RUNNING:
                self.latch(f"heartbeat lost: no ALIVE for {(now - self.last_alive) * 1000:.0f} ms")
            self.streak = 0

        h = self.healthy(now)
        if h != self.was_healthy:
            self.was_healthy = h
            if h:
                log(f"link HEALTHY ({self.streak} heartbeats in a row)", "cyan")
                if self.state == BOOT:
                    self.state = READY
                    log("state BOOT -> READY: press r to arm")
                elif self.state == LATCHED:
                    log("reset now allowed: press r to re-arm")
            else:
                log(f"link NOT healthy: {self.why_not_healthy(now)}", "yellow")
                if self.state == READY:
                    self.state = BOOT
                    log("state READY -> BOOT")

    def latch(self, reason):
        self.gpio.set(False)
        prev, self.state = self.state, LATCHED
        log(f"!!! E-STOP LATCHED ({prev} -> LATCHED): {reason}", "red")
        log("    power stays off until link is healthy AND you press r")

    def press_r(self, now):
        if self.state == RUNNING:
            log("r ignored: already RUNNING")
            return
        if not self.healthy(now):
            log(f"r REFUSED in {self.state}: {self.why_not_healthy(now)}", "yellow")
            return
        prev, self.state = self.state, RUNNING
        self.gpio.set(True)
        log(f"state {prev} -> RUNNING ({'armed' if prev == READY else 'reset by operator'})", "green")

    def status_line(self, now):
        age = "never" if self.last_alive is None else f"{(now - self.last_alive) * 1000:.0f} ms ago"
        why = "" if self.healthy(now) else f" (why: {self.why_not_healthy(now)})"
        return (f"state={self.state} power={'ON' if self.gpio.value else 'OFF'} "
                f"healthy={self.healthy(now)}{why} streak={self.streak} last_alive={age} "
                f"rx_alive={self.rx_alive} rx_stop={self.rx_stop} lost_by_seq={self.seq_gaps}")

    def status(self, now):
        log("status: " + self.status_line(now))


def rockchip_pin(name):
    """'GPIO1_B2' -> ('/dev/gpiochip1', 10). Rockchip: line = bank*8 + n, A=0..D=3.

    Same rule on RK3588 and RK3588S, so this works for both candidate boards
    (RP1's RBE-MAIN-RK3588S and ArmSoM Sige7) - only the chosen pin differs.
    """
    n = name.upper().replace("GPIO", "")
    try:
        chip, rest = n.split("_")
        bank = "ABCD".index(rest[0])
        return f"/dev/gpiochip{int(chip)}", bank * 8 + int(rest[1:])
    except (ValueError, IndexError):
        raise SystemExit(f"bad --pin {name!r}; expected like GPIO1_B2")


def main():
    p = argparse.ArgumentParser(description="RK3588 E-stop receiver")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=4242)
    p.add_argument("--control-port", type=int, default=4250,
                   help="localhost-only UDP port for estopctl.py (reset/status)")
    p.add_argument("--timeout-ms", type=float, default=150, help="heartbeat timeout")
    p.add_argument("--needed", type=int, default=5, help="heartbeats in a row before arm/reset")
    p.add_argument("--gpio", choices=["fake", "gpiod"], default="fake")
    p.add_argument("--pin", help="Rockchip pin name, e.g. GPIO1_B2 (sets --chip/--line)")
    p.add_argument("--chip", default="/dev/gpiochip0", help="for --gpio gpiod")
    p.add_argument("--line", type=int, default=17, help="for --gpio gpiod")
    args = p.parse_args()

    if args.gpio == "gpiod":
        from gpio_out import GpiodOutput
        if args.pin:
            args.chip, args.line = rockchip_pin(args.pin)
        gpio = GpiodOutput(args.chip, args.line)
    else:
        gpio = FakeGPIO()

    rx = Receiver(gpio, args.timeout_ms / 1000, args.needed)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))
    sock.setblocking(False)

    # Control socket: only 127.0.0.1, so reset needs a shell on the robot
    # (local or SSH). Nothing on the Wi-Fi network can reset it.
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.bind(("127.0.0.1", args.control_port))
    ctl.setblocking(False)

    # SIGTERM (e.g. systemd stop) should cut power the same way Ctrl+C does.
    def on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)

    log(f"E-stop receiver listening on UDP {args.bind}:{args.port} (gpio={args.gpio}), "
        f"timeout {args.timeout_ms:.0f} ms, need {args.needed} heartbeats to arm")
    log(f"Keys: r = arm/reset   i = status   q = quit   |   "
        f"or: estopctl.py reset/status (control port {args.control_port})")
    log("state BOOT: power OFF, waiting for heartbeats")

    try:
        with KeyReader() as keys:
            while True:
                ready, _, _ = select.select([sock, ctl] + keys.select_list(), [], [], 0.005)
                now = time.monotonic()
                if sock in ready:
                    while True:
                        try:
                            data, _addr = sock.recvfrom(1024)
                        except BlockingIOError:
                            break
                        rx.on_packet(data, now)
                if ctl in ready:
                    try:
                        data, addr = ctl.recvfrom(1024)
                        cmd = data.decode(errors="ignore").strip().upper()
                        if cmd == "RESET":
                            log("reset requested via estopctl")
                            rx.press_r(time.monotonic())
                        if cmd in ("RESET", "STATUS"):
                            ctl.sendto(rx.status_line(time.monotonic()).encode(), addr)
                    except BlockingIOError:
                        pass
                if keys in ready:
                    key = keys.read_key()
                    if key == "r":
                        rx.press_r(now)
                    elif key == "i":
                        rx.status(now)
                    elif key == "q":
                        break
                rx.tick(time.monotonic())
    except KeyboardInterrupt:
        pass
    finally:
        log("receiver shutting down: cutting power first")
        gpio.close()
        sock.close()
        ctl.close()
        rx.status(time.monotonic())


if __name__ == "__main__":
    main()
