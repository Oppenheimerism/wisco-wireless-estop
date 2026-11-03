# Wireless E-Stop (ESP32-S3 → RK3588)

A handheld ESP32-S3 sends a UDP heartbeat over Wi-Fi to the RK3588. The RK3588
holds the E-stop GPIO **HIGH only while heartbeats keep arriving and the button
is not pressed**. A STOP packet, or **150 ms of silence**, drops the GPIO LOW →
MOSFET off → 48 V contactor opens. Power then stays off (latched) until someone
deliberately resets it.

```
[NC button] → ESP32-S3 ──Wi-Fi / UDP──→ RK3588 receiver.py → GPIO → MOSFET gate → contactor coil → 48 V
              "ALIVE n" every 50 ms      (RK3588 runs the hotspot)
              "STOP n"  every 20 ms while pressed
```

The point of the heartbeat: a dead battery, a crashed ESP, walking out of range,
or a Wi-Fi drop all produce *silence*, and silence stops the robot. Nothing has
to work correctly for the robot to stop.

## Status

| Part | State |
|---|---|
| `esp32_transmitter/` (C, ESP-IDF) | **Builds** for ESP32-S3 with ESP-IDF v5.5.4. **Not flashed / not run on hardware yet.** |
| `receiver/receiver.py` (Python) | Logic tested on macOS against `fake_sender.py`: arm, STOP latch, reset refused while pressed, heartbeat timeout, lossy link, remote reset. |
| `receiver/gpio_out.py` → `GpiodOutput` | **Untested** (no RK3588 board yet). Written for the libgpiod **v2** Python API. |
| End to end (real ESP → real RK3588 → real pin) | Not done. |

## Layout

```
esp32_transmitter/          ESP-IDF project (C) — the handheld
  main/main.c               Wi-Fi + button + UDP sender
  main/Kconfig.projbuild    settings shown in `idf.py menuconfig`
  sdkconfig.defaults        1 kHz FreeRTOS tick
receiver/                   runs on the RK3588 (any Linux; macOS for testing)
  receiver.py               state machine + UDP listener + GPIO
  gpio_out.py               FakeGPIO (prints) / GpiodOutput (real pin)
  estopctl.py               `reset` / `status` from a shell (e.g. over SSH)
  estop-receiver.service    systemd unit
  fake_sender.py            laptop stand-in for the ESP32 (keyboard controlled)
  common.py                 logging + single-key input helpers
```

## Protocol and timing

| Packet | When | Receiver does |
|---|---|---|
| `ALIVE <seq>` | every 50 ms, button released | counts toward a healthy link |
| `STOP <seq>` | every 20 ms while pressed (first one immediately) | cut power + latch |
| *(nothing for > 150 ms)* | ESP dead / out of range / Wi-Fi down | cut power + latch |

- 150 ms timeout / 50 ms period = trips after **2 lost packets in a row**. Real
  Wi-Fi loss should be measured on the robot before finalizing these numbers
  (`estopctl.py status` shows `lost_by_seq`, counted from sequence numbers).
- STOP is repeated, not sent once, because UDP can drop a single packet, and
  because a continuing STOP tells the receiver "button still pressed", which
  blocks reset. If every STOP were lost, the missing ALIVE still trips in 150 ms.

## Receiver states

```
BOOT ──5 heartbeats in a row──► READY ──reset──► RUNNING ──STOP or 150 ms timeout──► LATCHED
  ▲                               │                ▲                                   │
  └────────link unhealthy─────────┘                └──healthy link + reset─────────────┘
```

Only RUNNING has GPIO HIGH. Every way **into** RUNNING needs a human reset;
no way **out** of RUNNING needs anyone.

Reset = `r` in the receiver's terminal, or `python3 estopctl.py reset` on the
RK3588 (the control port listens on 127.0.0.1 only, so you need a shell on the
robot; nothing on the Wi-Fi can reset it). A reset is refused unless the link
is healthy (5 fresh ALIVEs, no STOP in the last 150 ms) — it prints why.

## ESP32-S3 (handheld)

**Wiring:** normally-closed E-stop contact between **GPIO 4** and **GND**.
Internal pull-up is enabled (add an external ~10 kΩ to 3.3 V for long wires).
Not pressed → contact closed → pin 0. Pressed **or wire cut** → pin 1 → STOP.

```bash
cd esp32_transmitter
idf.py set-target esp32s3
idf.py menuconfig      # "Wireless E-Stop Transmitter": SSID, password, receiver IP, GPIO, periods
idf.py build flash monitor
```

Defaults: SSID `EstopNet`, password `SecureEstopPass`, receiver `10.42.0.1:4242`.
Change the password in menuconfig; `sdkconfig` is git-ignored so it stays local.

Firmware behavior:
- Press is acted on immediately; release must be stable for 50 ms before ALIVE resumes.
- Boots as "pressed" until the button reads released.
- Reconnects to Wi-Fi forever; while disconnected nothing is sent (so the robot is stopped).
- Wi-Fi power save is turned off (it can add ~100 ms latency).

## RK3588 (receiver)

Works the same on both candidate boards — only the pin and the Wi-Fi interface name differ:
- **RP1's RBE-MAIN-RK3588S**: its BOM lists an AP6256 Wi-Fi/BT module.
- **ArmSoM Sige7 (RK3588)**: check its onboard Wi-Fi supports AP (hotspot) mode.

Rockchip pin names map to libgpiod as `GPIO<chip>_<bank><n>` → `/dev/gpiochip<chip>`,
line `bank*8 + n` (A=0 … D=3), e.g. `GPIO1_B2` → chip 1, line 10. `--pin` does this for you.

```bash
# 1. Hotspot (NetworkManager). Default IP is 10.42.0.1.
sudo nmcli device wifi hotspot ifname wlan0 ssid EstopNet password "<password>"
sudo nmcli connection modify Hotspot connection.autoconnect yes 802-11-wireless.powersave 2
sudo iw dev wlan0 set power_save off

# 2. libgpiod v2 Python bindings (check: python3 -c "import gpiod; print(gpiod.__version__)")
pip install "gpiod>=2"

# 3. Run it by hand first
cd receiver
sudo python3 receiver.py --gpio gpiod --pin GPIO1_B2     # replace with the real pin
python3 estopctl.py status
python3 estopctl.py reset

# 4. Then as a service (edit WorkingDirectory and --pin in the file first)
sudo cp estop-receiver.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now estop-receiver
journalctl -u estop-receiver -f
```

If the receiver crashes or the board reboots, the pin is no longer driven and the
**10 kΩ gate pull-down** keeps the contactor open; systemd restarts the receiver
in BOOT (power off). Check on the real board that the chosen pin does not float
HIGH or get pulled up during boot before the service starts.

## Testing without hardware

```bash
cd receiver
python3 receiver.py          # terminal 1 (prints instead of driving a pin)
python3 fake_sender.py       # terminal 2: s = press/release, k = kill/revive, l = 50% loss, q = quit
```

With a real ESP32 but no RK3588: put the laptop's IP in menuconfig as the
receiver IP and the laptop's Wi-Fi as the SSID, then run `receiver.py` on the laptop.

## Changes vs. the original design doc's code

- Starts with GPIO **LOW**; the doc set it HIGH at startup (≈150 ms powered before any heartbeat).
- Latches by staying alive and holding LOW; the doc called `sys.exit()`, leaving the released pin to whatever the kernel does.
- Adds an explicit reset path with checks; adds sequence numbers to measure loss.
- STOP resend every 20 ms instead of 5 ms (see "Protocol and timing").
- libgpiod v2 API (the doc used v1 `chip.get_line`, which newer Armbian may not ship).
- Firmware in ESP-IDF C instead of Arduino.

## Open questions

1. **Which pin** on the final board, and confirm it is 3.3 V.
2. **Schematic:** the on-robot button and the Rockchip GPIO both feed the MOSFET gate through 100 Ω.
   The requirement is "either one alone cuts power". From the low-res image it looks like a
   parallel connection, where GPIO HIGH might keep the gate on even with the button pressed.
   Worth checking with whoever owns the PDB schematic.
3. Handheld power (battery) and enclosure.
