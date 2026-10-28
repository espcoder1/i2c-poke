#!/usr/bin/env python3
"""
i2cpoke - bench I2C tool for the CP2112 USB-to-I2C/SMBus bridge.

Talks to the CP2112 directly over USB HID (Silicon Labs AN495 protocol),
so it works the same on Windows, macOS and Linux with no driver install.

  pip install hidapi

Usage examples
  python i2cpoke.py scan                    # find devices, guess what they are
  python i2cpoke.py read 0x76 0xD0          # read 1 byte from register 0xD0
  python i2cpoke.py read 0x76 0xF7 8        # read 8 bytes starting at 0xF7
  python i2cpoke.py write 0x40 0x00 0x41 0x27   # write bytes to register 0x00
  python i2cpoke.py dump 0x50               # hex dump registers 0x00-0xFF
  python i2cpoke.py rawread 0x23 2          # plain read, no register pointer
  python i2cpoke.py watch 0x48 0x00 2       # re-read a register every 0.5 s
  python i2cpoke.py gpio 5 on               # drive IO5 high (IO5/IO6/IO7)
  python i2cpoke.py --speed 400000 scan     # 400 kHz bus

Linux: add a udev rule so you don't need sudo:
  SUBSYSTEM=="hidraw", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea90", MODE="0666"
"""
import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("Missing library: run  pip install hidapi")

VID, PID = 0x10C4, 0xEA90

# HID report IDs (AN495)
RPT_GPIO_CONFIG = 0x02
RPT_GPIO_GET = 0x03
RPT_GPIO_SET = 0x04
RPT_SMBUS_CONFIG = 0x06
RPT_READ_REQ = 0x10
RPT_WRITE_READ_REQ = 0x11
RPT_READ_FORCE_SEND = 0x12
RPT_READ_RESPONSE = 0x13
RPT_WRITE = 0x14
RPT_STATUS_REQ = 0x15
RPT_STATUS_RESP = 0x16
RPT_CANCEL = 0x17

ST0_BUSY, ST0_COMPLETE, ST0_ERROR = 0x01, 0x02, 0x03
ERRORS = {
    0x00: "no ACK from device (nothing at that address?)",
    0x01: "bus not free (SDA/SCL held low? check wiring/pull-ups)",
    0x02: "arbitration lost",
    0x03: "read incomplete",
    0x04: "write incomplete",
}

# Common chips by address - a guide, not gospel.
KNOWN = {
    0x0B: "Smart battery (SBS fuel gauge)",
    0x0C: "AK8963 magnetometer",
    0x0D: "QMC5883L compass",
    0x10: "VEML7700 light",
    0x18: "LIS3DH / MCP9808",
    0x19: "LIS3DH / LSM303 accel",
    0x1D: "ADXL345 / LSM9DS",
    0x1E: "HMC5883L / LSM303 mag",
    0x20: "PCF8574 / MCP23017 I/O expander",
    0x21: "PCF8574 / MCP23017",
    0x22: "PCF8574 / MCP23017",
    0x23: "BH1750 light / PCF8574",
    0x24: "PCF8574 / MCP23017",
    0x25: "PCF8574 / MCP23017",
    0x26: "PCF8574 / MCP23017",
    0x27: "PCF8574 (LCD backpack) / MCP23017",
    0x29: "VL53L0X / TSL2591 / TCS34725",
    0x36: "MAX17048 fuel gauge / AS5600",
    0x38: "AHT10/AHT20 / PCF8574A / FT6x06 touch",
    0x39: "APDS9960 / TSL2561 / PCF8574A",
    0x3C: "SSD1306 / SH1106 OLED",
    0x3D: "SSD1306 / SH1106 OLED (alt)",
    0x3F: "PCF8574A (LCD backpack)",
    0x40: "INA219 / INA226 / PCA9685 / HTU21D / Si7021",
    0x41: "INA219 / INA226",
    0x44: "SHT3x / SHT4x / INA219",
    0x45: "SHT3x (alt) / INA219",
    0x48: "ADS1115 / TMP102 / PCF8591 / LM75",
    0x49: "ADS1115 / TMP102 / LM75",
    0x4A: "ADS1115 / LM75",
    0x4B: "ADS1115 / LM75",
    0x50: "24Cxx EEPROM",
    0x51: "24Cxx EEPROM / PCF8563 RTC",
    0x52: "24Cxx EEPROM / Wii Nunchuk",
    0x53: "24Cxx EEPROM / ADXL345 (alt)",
    0x54: "24Cxx EEPROM",
    0x55: "24Cxx EEPROM",
    0x56: "24Cxx EEPROM",
    0x57: "24Cxx EEPROM (often on DS3231 module)",
    0x58: "SGP30 air quality",
    0x5A: "MLX90614 IR temp / CCS811 / MPR121",
    0x5B: "CCS811 (alt) / MPR121",
    0x5C: "AM2320 / BH1750 (alt)",
    0x60: "MCP4725 DAC / Si5351 / ATECC608",
    0x62: "SCD40/41 CO2 / MCP4725",
    0x68: "DS3231 / DS1307 RTC / MPU6050",
    0x69: "MPU6050 (alt) / ICM-20948",
    0x6A: "LSM6DS3 / LSM6DSOX",
    0x6B: "LSM6DS3 / LSM6DSOX (alt)",
    0x70: "TCA9548A mux / HT16K33",
    0x76: "BME280 / BMP280 / BME680 / MS5611",
    0x77: "BME280 / BMP280 / BMP180 / BME680 (alt)",
}


# Chips that refuse a read until they've been given a command (Sensirion etc).
# If the read probe gets no answer here, try a harmless 1-byte write instead.
QUIET_PROBE = {0x40: 0xE7, 0x44: 0x89, 0x45: 0x89, 0x46: 0x89, 0x58: 0x36, 0x59: 0x36,
               0x61: 0x36, 0x62: 0x36, 0x69: 0x36, 0x70: 0x00}


class CP2112Error(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code   # 0x00 = plain NACK (nothing at that address)


class CP2112:
    def __init__(self, speed_hz=100_000):
        self.h = hid.device()
        try:
            self.h.open(VID, PID)
        except (IOError, OSError):
            raise CP2112Error(
                "CP2112 not found. Is it plugged in? On Linux, check the udev rule "
                "(see top of this file) or try sudo.")
        self.configure(speed_hz)

    def close(self):
        self.h.close()

    # --- low level -------------------------------------------------------
    def _out(self, data):
        data = list(data)
        self.h.write(data + [0] * (64 - len(data)))

    def configure(self, speed_hz, timeout_ms=100, retries=1):
        rpt = ([RPT_SMBUS_CONFIG]
               + list(int(speed_hz).to_bytes(4, "big"))
               + [0x02,      # CP2112's own slave address (unused here)
                  0x00]      # auto-send read: off, we request data explicitly
               + list(timeout_ms.to_bytes(2, "big"))   # write timeout
               + list(timeout_ms.to_bytes(2, "big"))   # read timeout
               + [0x01]                                # SCL-low timeout on
               + list(retries.to_bytes(2, "big")))     # retries (0 = forever!)
        self.h.send_feature_report(rpt)

    def _wait(self, timeout=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._out([RPT_STATUS_REQ, 0x01])
            r = self.h.read(64, 100)
            if not r or r[0] != RPT_STATUS_RESP:
                continue
            s0, s1 = r[1], r[2]
            if s0 == ST0_COMPLETE:
                return
            if s0 == ST0_ERROR:
                raise CP2112Error(ERRORS.get(s1, f"error code 0x{s1:02X}"), s1)
            # busy or idle: keep polling
        self._out([RPT_CANCEL, 0x01])
        raise CP2112Error("timed out - check SDA/SCL wiring, GND and pull-ups", "timeout")

    def _collect(self, n):
        data = []
        tries = 0
        while len(data) < n:
            self._out([RPT_READ_FORCE_SEND] + list((n - len(data)).to_bytes(2, "big")))
            r = self.h.read(64, 200)
            if r and r[0] == RPT_READ_RESPONSE:
                if r[1] == ST0_ERROR:
                    raise CP2112Error(ERRORS.get(r[2], "read error"))
                data += r[3:3 + r[2]]
            tries += 1
            if tries > 20:
                raise CP2112Error(f"only got {len(data)} of {n} bytes")
        return data[:n]

    # --- I2C operations ----------------------------------------------------
    def read(self, addr, n):
        """Plain read of n bytes (no register pointer)."""
        self._out([RPT_READ_REQ, addr << 1] + list(n.to_bytes(2, "big")))
        self._wait()
        return self._collect(n)

    def write(self, addr, data):
        data = list(data)
        if not 1 <= len(data) <= 61:
            raise CP2112Error("write must be 1-61 bytes")
        self._out([RPT_WRITE, addr << 1, len(data)] + data)
        self._wait()

    def read_reg(self, addr, reg, n=1, reg_bytes=1):
        """Write register pointer then repeated-start read."""
        regb = list(reg.to_bytes(reg_bytes, "big"))
        self._out([RPT_WRITE_READ_REQ, addr << 1] + list(n.to_bytes(2, "big"))
                  + [len(regb)] + regb)
        self._wait()
        return self._collect(n)

    def probe(self, addr):
        """True if a device ACKs. Raises on bus faults (anything but a plain NACK)."""
        try:
            self.read(addr, 1)
            return True
        except CP2112Error as e:
            if e.code == 0x00:
                return False
            raise

    def present(self, addr):
        """Like probe(), but also finds chips that only answer writes."""
        if self.probe(addr):
            return True
        b = QUIET_PROBE.get(addr)
        if b is None:
            return False
        try:
            self.write(addr, [b])
            return True
        except CP2112Error as e:
            if e.code == 0x00:
                return False
            if e.code == 0x04:        # address acknowledged, data byte refused
                return True
            raise

    def version(self):
        r = self.h.get_feature_report(0x05, 3)
        return r[1], r[2]

    # --- GPIO (IO5-IO7 on this board) ----------------------------------------
    def gpio_set(self, pin, value):
        cfg = self.h.get_feature_report(RPT_GPIO_CONFIG, 5)
        direction, pushpull, special = cfg[1], cfg[2], cfg[3]
        bit = 1 << pin
        if pin == 7:
            special &= ~0x01          # turn off clock-out on GPIO7
        self.h.send_feature_report([RPT_GPIO_CONFIG, direction | bit,
                                    pushpull | bit, special, cfg[4] if len(cfg) > 4 else 0])
        self.h.send_feature_report([RPT_GPIO_SET, bit if value else 0, bit])

    def gpio_get(self):
        r = self.h.get_feature_report(RPT_GPIO_GET, 2)
        return r[1]


# --- output helpers ------------------------------------------------------------
def hexs(data):
    return " ".join(f"{b:02X}" for b in data)


def probe_or_stop(dev, a):
    try:
        return dev.present(a)
    except CP2112Error as e:
        print()
        raise CP2112Error(f"scan stopped at 0x{a:02X}: {e}")


def cmd_scan(dev, args):
    found = []
    print("    " + " ".join(f"{c:2X}" for c in range(16)))
    for row in range(0, 0x80, 0x10):
        line = f"{row:02X}: "
        for col in range(16):
            a = row + col
            if a < 0x08 or a > 0x77:
                line += "   "
            elif probe_or_stop(dev, a):
                line += f"{a:02X} "
                found.append(a)
            else:
                line += "-- "
        print(line)
    print()
    if not found:
        print("Nothing found. Check: VCC/GND to the target, SDA/SCL not swapped, "
              "target powered at the right voltage.")
        return
    for a in found:
        print(f"  0x{a:02X}  {KNOWN.get(a, 'unknown - look it up')}")


def cmd_read(dev, args):
    data = dev.read_reg(args.addr, args.reg, args.n, args.regwidth)
    print(f"0x{args.addr:02X} reg 0x{args.reg:02X}: {hexs(data)}")
    if args.n == 1:
        b = data[0]
        print(f"  dec {b}   bin {b:08b}")
    elif args.n == 2:
        be, le = (data[0] << 8) | data[1], (data[1] << 8) | data[0]
        print(f"  16-bit big-endian {be} (0x{be:04X})   little-endian {le} (0x{le:04X})")


def cmd_rawread(dev, args):
    print(f"0x{args.addr:02X}: {hexs(dev.read(args.addr, args.n))}")


def cmd_write(dev, args):
    dev.write(args.addr, [args.reg] + args.data)
    print(f"Wrote 0x{args.addr:02X} reg 0x{args.reg:02X} <- {hexs(args.data)}")


def cmd_dump(dev, args):
    print("     " + " ".join(f"{c:02X}" for c in range(16)))
    for row in range(0, 0x100, 0x10):
        data = dev.read_reg(args.addr, row, 16)
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        print(f"{row:02X}:  {hexs(data)}  {txt}")


def cmd_watch(dev, args):
    print("Ctrl+C to stop")
    try:
        while True:
            data = dev.read_reg(args.addr, args.reg, args.n)
            print(f"{time.strftime('%H:%M:%S')}  {hexs(data)}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def cmd_gpio(dev, args):
    if args.state is None:
        v = dev.gpio_get()
        print(f"GPIO latch: {v:08b}  (IO{args.pin} = {(v >> args.pin) & 1})")
    else:
        dev.gpio_set(args.pin, args.state == "on")
        print(f"IO{args.pin} -> {args.state.upper()}")


def main():
    num = lambda s: int(s, 0)
    p = argparse.ArgumentParser(description="CP2112 bench I2C tool")
    p.add_argument("--speed", type=num, default=100_000, help="bus speed Hz (default 100000)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="find devices on the bus")

    s = sub.add_parser("read", help="read register(s)")
    s.add_argument("addr", type=num); s.add_argument("reg", type=num)
    s.add_argument("n", type=num, nargs="?", default=1)
    s.add_argument("--regwidth", type=int, default=1, help="register address bytes (2 for big EEPROMs)")

    s = sub.add_parser("rawread", help="read without a register pointer")
    s.add_argument("addr", type=num); s.add_argument("n", type=num, nargs="?", default=1)

    s = sub.add_parser("write", help="write byte(s) to a register")
    s.add_argument("addr", type=num); s.add_argument("reg", type=num)
    s.add_argument("data", type=num, nargs="+")

    s = sub.add_parser("dump", help="hex dump registers 0x00-0xFF")
    s.add_argument("addr", type=num)

    s = sub.add_parser("watch", help="poll a register repeatedly")
    s.add_argument("addr", type=num); s.add_argument("reg", type=num)
    s.add_argument("n", type=num, nargs="?", default=1)
    s.add_argument("--interval", type=float, default=0.5)

    s = sub.add_parser("gpio", help="read or set IO5/IO6/IO7")
    s.add_argument("pin", type=int, choices=[5, 6, 7])
    s.add_argument("state", nargs="?", choices=["on", "off"])

    args = p.parse_args()
    try:
        dev = CP2112(args.speed)
    except CP2112Error as e:
        sys.exit(str(e))
    try:
        {"scan": cmd_scan, "read": cmd_read, "rawread": cmd_rawread, "write": cmd_write,
         "dump": cmd_dump, "watch": cmd_watch, "gpio": cmd_gpio}[args.cmd](dev, args)
    except CP2112Error as e:
        sys.exit(f"I2C error: {e}")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
