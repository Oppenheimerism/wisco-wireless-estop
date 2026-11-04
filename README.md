# Wireless E-Stop

Handheld ESP32-S3 → Wi-Fi (UDP) → RK3588 → E-stop GPIO → MOSFET → 48 V contactor.

- ESP32 sends `ALIVE <seq>` every 50 ms, or `STOP <seq>` every 20 ms while the button is pressed.
- RK3588 keeps the GPIO **HIGH only while ALIVEs keep arriving**. STOP, or 150 ms of silence
  (dead battery, crash, out of range), drives it LOW and **latches** until someone runs `reset`.

**Status:** firmware builds (ESP-IDF v5.5.4, ESP32-S3) but has not run on hardware yet.
Receiver logic is tested on a laptop. `GpiodOutput` (the real pin) is untested.

## Layout

```
esp32_transmitter/   ESP-IDF project (C): main/main.c, settings in main/Kconfig.projbuild
receiver/            estop_receiver.py (runs on the RK3588), estop-receiver.service (systemd)
tools/               fake_sender.py: laptop stand-in for the ESP32
```

## ESP32-S3

Wire a **normally-closed** button between **GPIO 4** and **GND** (internal pull-up on).
Pressed or wire cut → pin reads HIGH → STOP.

```bash
cd esp32_transmitter
idf.py set-target esp32s3
idf.py menuconfig          # "Wireless E-Stop Transmitter": SSID, password, receiver IP, GPIO
idf.py -p <port> flash monitor
```

Defaults: SSID `EstopNet`, receiver `10.42.0.1:4242` (the nmcli hotspot address).

## RK3588

```bash
sudo nmcli device wifi hotspot ifname wlan0 ssid EstopNet password "<password>"
sudo nmcli connection modify Hotspot connection.autoconnect yes 802-11-wireless.powersave 2
pip install "gpiod>=2"

cd receiver
sudo python3 estop_receiver.py run --gpio gpiod --pin GPIO1_B2   # use the real pin
python3 estop_receiver.py status
python3 estop_receiver.py reset    # arm / reset; refused unless the link is healthy
```

`--pin` takes Rockchip names (`GPIO1_B2` → gpiochip1 line 10); same on RK3588 and RK3588S.
To start on boot: edit and install `receiver/estop-receiver.service`.

## Test on a laptop

```bash
python3 receiver/estop_receiver.py run        # terminal 1: GPIO is printed
python3 tools/fake_sender.py                  # terminal 2: s = button, k = kill, l = 50% loss
python3 receiver/estop_receiver.py reset      # terminal 3
```

With a real ESP32: put the laptop's IP and a shared 2.4 GHz network in menuconfig.

## Things to check

These came up while writing and reviewing the code. None has been confirmed on hardware,
so treat them as questions rather than findings.

1. **Pin state after a hard crash.** On Ctrl+C / SIGTERM the receiver drives the pin LOW first.
   After SIGKILL or a kernel hang, Linux may not guarantee that, so the pin could stay HIGH.
   A hardware watchdog (e.g. gate held on only by a toggling signal) would cover this, if needed.
2. **Schematic:** the on-robot button and the GPIO both seem to feed the MOSFET gate through
   100 Ω (from a low-res image). If that is a parallel (OR) connection, GPIO HIGH might keep
   the gate on with the button pressed. Intended behavior: either one alone cuts power.
3. A short in the button wiring would read as "not pressed" (NC catches a cut wire, not a short).
4. 50 / 150 ms timing vs. real Wi-Fi packet loss (`status` shows `lost`).
5. The chosen pin's state during boot, before the service starts.
