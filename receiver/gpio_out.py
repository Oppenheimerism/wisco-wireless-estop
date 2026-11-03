"""The one output the receiver controls: the E-stop GPIO line.

HIGH = permission to run (MOSFET on, contactor coil energized, 48 V connected)
LOW  = power cut

Everything else in receiver.py talks to this interface only, so swapping the
laptop fake for the real RK3588 pin is a one-line change (--gpio gpiod).
"""

from common import log


class FakeGPIO:
    """Laptop stand-in: prints instead of touching hardware."""

    def __init__(self):
        self.value = None  # unknown until first set()

    def set(self, high):
        if high == self.value:
            return
        self.value = high
        if high:
            log(">>> GPIO HIGH  ->  POWER ON  (contactor closed)", "green")
        else:
            log(">>> GPIO LOW   ->  POWER CUT (contactor open)", "red")

    def close(self):
        self.set(False)


class GpiodOutput:
    """Real pin on the RK3588 via libgpiod v2 Python bindings.

    UNTESTED - written for the v2 API (`gpiod.request_lines`). Older Armbian
    images ship v1 (`chip.get_line(...)`, as in the original doc), which has a
    different API. Check with: python3 -c "import gpiod; print(gpiod.__version__)"

    RK3588 pin names like GPIO1_B2 map to chip 1, line = bank*8 + n
    (A=0, B=1, C=2, D=3), so GPIO1_B2 -> /dev/gpiochip1 line 10.
    """

    def __init__(self, chip, line):
        import gpiod
        from gpiod.line import Direction, Value

        self._Value = Value
        self._line = line
        self.value = False
        # Request the line already driven LOW, so there is no glitch to HIGH.
        self._req = gpiod.request_lines(
            chip,
            consumer="estop_receiver",
            config={line: gpiod.LineSettings(direction=Direction.OUTPUT,
                                             output_value=Value.INACTIVE)},
        )
        log(f"gpiod: {chip} line {line} requested, driven LOW")

    def set(self, high):
        if high == self.value:
            return
        self.value = high
        self._req.set_value(self._line, self._Value.ACTIVE if high else self._Value.INACTIVE)
        log(f">>> GPIO {'HIGH -> POWER ON' if high else 'LOW -> POWER CUT'}",
            "green" if high else "red")

    def close(self):
        self.set(False)
        # After release the kernel no longer drives the pin. The 10k gate
        # pull-down on the PDB is what keeps power off from here on.
        self._req.release()
