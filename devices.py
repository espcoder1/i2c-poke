"""
I2C device library for I2C Poke (CP2112).

Every driver knows which addresses it can live at, how to tell whether the chip
at an address really is that part (ID registers where the chip has them), how to
read it, and optional settings / actions.

To add a chip: subclass Driver, fill in name/addrs/identify/read, and add the
class to REGISTRY. Order in REGISTRY matters - drivers that confirm a chip by an
ID register come before ones that can only guess.

identify() returns 2 = confirmed by ID, 1 = plausible (no ID to check), 0 = no.
"""
import math
import time
import datetime

from i2cpoke import CP2112Error
import sensors as env

# ------------------------------------------------------------------ helpers --
def s16be(a, b):
    v = (a << 8) | b
    return v - 0x10000 if v & 0x8000 else v


def u16be(a, b):
    return (a << 8) | b


def s16le(lo, hi):
    return s16be(hi, lo)


def u16le(lo, hi):
    return (hi << 8) | lo


def s8(v):
    return v - 256 if v & 0x80 else v


def bcd(b):
    return (b >> 4) * 10 + (b & 0x0F)


def to_bcd(v):
    return ((v // 10) << 4) | (v % 10)


def tdiv(a, b):
    """C-style integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def crc8(data, init=0xFF, poly=0x31):
    c = init
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def sensirion_words(d):
    """Split [hi, lo, crc] groups into words, checking each CRC."""
    out = []
    for i in range(0, len(d), 3):
        if crc8(d[i:i + 2]) != d[i + 2]:
            raise CP2112Error("CRC mismatch - wrong chip or noisy bus")
        out.append((d[i] << 8) | d[i + 1])
    return out


def heading(x, y):
    return math.degrees(math.atan2(y, x)) % 360


# --------------------------------------------------------------- base class --
class Driver:
    name = "?"
    addrs = ()
    category = "Other"
    settings = {}          # label -> default value
    actions = []           # (button label, method name, [(param label, default), ...])
    note = ""
    home = None            # usual default addresses; wins ties against other guesses
    destructive_identify = False   # identify() writes something that could upset other chips

    def __init__(self, dev, addr, opts=None):
        self.dev, self.addr = dev, addr
        self.opts = dict(self.settings)
        if opts:
            self.opts.update({k: v for k, v in opts.items() if k in self.opts})
        self.ready = False

    @classmethod
    def identify(cls, dev, addr):
        return 1

    def setup(self):
        pass

    def ensure(self):
        if not self.ready:
            self.setup()
            self.ready = True

    def read(self):
        return []

    # shorthand
    def rr(self, reg, n=1, reg_bytes=1):
        return self.dev.read_reg(self.addr, reg, n, reg_bytes)

    def wr(self, *b):
        self.dev.write(self.addr, list(b))

    def rd(self, n):
        return self.dev.read(self.addr, n)

    def opt(self, k):
        return float(self.opts[k])


def _rr(dev, addr, reg, n=1):
    return dev.read_reg(addr, reg, n)


# =========================================================== ENVIRONMENT ====
class AHTx0(Driver):
    name, addrs, category = "AHT10/AHT20", (0x38,), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        st = dev.read(addr, 1)[0]
        if st == 0xFF:
            return 0
        return 2 if (st & 0x88) == 0x08 else 1

    def setup(self):
        self.s = env.AHT20(self.dev, self.addr)

    def read(self):
        r = self.s.read()
        out = [("Temperature", r["temp_c"], "°C"), ("Humidity", r["humidity"], "%RH")]
        dp = env.dew_point(r["temp_c"], r["humidity"])
        if dp is not None:
            out.append(("Dew point", dp, "°C"))
        return out


class BMx280(Driver):
    name, addrs, category = "BMP280/BME280", (0x76, 0x77), "Environment"
    settings = {"Sea level hPa": 1013.25}

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0xD0)[0] in (0x56, 0x57, 0x58, 0x60) else 0

    def setup(self):
        self.s = env.BMx280(self.dev, self.addr)
        self.name = self.s.name

    def read(self):
        r = self.s.read()
        out = [("Temperature", r["temp_c"], "°C")]
        if "humidity" in r:
            out.append(("Humidity", r["humidity"], "%RH"))
        if "pressure_hpa" in r:
            out += [("Pressure", r["pressure_hpa"], "hPa"),
                    ("Altitude", env.altitude_m(r["pressure_hpa"], self.opt("Sea level hPa")), "m")]
        return out


class BMP180(Driver):
    name, addrs, category = "BMP180/BMP085", (0x77,), "Environment"
    settings = {"Sea level hPa": 1013.25}

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0xD0)[0] == 0x55 else 0

    def setup(self):
        c = self.rr(0xAA, 22)
        w = [u16be(c[i], c[i + 1]) for i in range(0, 22, 2)]
        s = lambda v: v - 0x10000 if v & 0x8000 else v
        (self.AC1, self.AC2, self.AC3) = (s(w[0]), s(w[1]), s(w[2]))
        (self.AC4, self.AC5, self.AC6) = (w[3], w[4], w[5])
        (self.B1, self.B2, self.MB, self.MC, self.MD) = (s(w[6]), s(w[7]), s(w[8]), s(w[9]), s(w[10]))

    def raw(self):
        self.wr(0xF4, 0x2E); time.sleep(0.005)
        d = self.rr(0xF6, 2); ut = u16be(d[0], d[1])
        self.wr(0xF4, 0x34); time.sleep(0.008)
        d = self.rr(0xF6, 3); up = ((d[0] << 16) | (d[1] << 8) | d[2]) >> 8
        return ut, up

    def compute(self, ut, up, oss=0):
        x1 = tdiv((ut - self.AC6) * self.AC5, 1 << 15)
        x2 = tdiv(self.MC * (1 << 11), x1 + self.MD)
        b5 = x1 + x2
        t = tdiv(b5 + 8, 16) / 10.0
        b6 = b5 - 4000
        x1 = tdiv(self.B2 * tdiv(b6 * b6, 1 << 12), 1 << 11)
        x2 = tdiv(self.AC2 * b6, 1 << 11)
        x3 = x1 + x2
        b3 = tdiv(((self.AC1 * 4 + x3) << oss) + 2, 4)
        x1 = tdiv(self.AC3 * b6, 1 << 13)
        x2 = tdiv(self.B1 * tdiv(b6 * b6, 1 << 12), 1 << 16)
        x3 = tdiv(x1 + x2 + 2, 4)
        b4 = (self.AC4 * ((x3 + 32768) & 0xFFFFFFFF)) >> 15
        b7 = ((up - b3) & 0xFFFFFFFF) * (50000 >> oss)
        p = (b7 * 2) // b4 if b7 < 0x80000000 else (b7 // b4) * 2
        x1 = (p >> 8) * (p >> 8)
        x1 = (x1 * 3038) >> 16
        x2 = tdiv(-7357 * p, 1 << 16)
        p = p + ((x1 + x2 + 3791) >> 4)
        return t, p / 100.0

    def read(self):
        t, hpa = self.compute(*self.raw())
        return [("Temperature", t, "°C"), ("Pressure", hpa, "hPa"),
                ("Altitude", env.altitude_m(hpa, self.opt("Sea level hPa")), "m")]


class BME680(Driver):
    name, addrs, category = "BME680/BME688", (0x76, 0x77), "Environment"
    note = "Identified by chip ID. Gas/temperature readout not supported yet."

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0xD0)[0] == 0x61 else 0

    def read(self):
        return [("Chip ID", "0x61", "")]


class SHT4x(Driver):
    name, addrs, category = "SHT40/41/45", (0x44, 0x45, 0x46), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        dev.write(addr, [0x89]); time.sleep(0.002)
        sensirion_words(dev.read(addr, 6))
        return 2

    def read(self):
        self.wr(0xFD); time.sleep(0.01)
        t, h = sensirion_words(self.rd(6))
        return [("Temperature", -45 + 175 * t / 65535, "°C"),
                ("Humidity", max(0.0, min(100.0, -6 + 125 * h / 65535)), "%RH")]


class SHT3x(Driver):
    name, addrs, category = "SHT30/31/35", (0x44, 0x45), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        dev.write(addr, [0x24, 0x00]); time.sleep(0.016)
        sensirion_words(dev.read(addr, 6))
        return 2

    def read(self):
        self.wr(0x24, 0x00); time.sleep(0.016)
        t, h = sensirion_words(self.rd(6))
        return [("Temperature", -45 + 175 * t / 65535, "°C"), ("Humidity", 100 * h / 65535, "%RH")]


class HTU21D(Driver):
    name, addrs, category = "HTU21D/Si7021/SHT21", (0x40,), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        dev.write(addr, [0xF3]); time.sleep(0.06)
        d = dev.read(addr, 3)
        return 2 if crc8(d[:2], init=0x00) == d[2] else 0

    def _meas(self, cmd, wait):
        self.wr(cmd); time.sleep(wait)
        d = self.rd(3)
        if crc8(d[:2], init=0x00) != d[2]:
            raise CP2112Error("CRC mismatch")
        return u16be(d[0], d[1]) & 0xFFFC

    def read(self):
        t = -46.85 + 175.72 * self._meas(0xF3, 0.06) / 65536
        h = -6 + 125 * self._meas(0xF5, 0.03) / 65536
        return [("Temperature", t, "°C"), ("Humidity", max(0.0, min(100.0, h)), "%RH")]


class MCP9808(Driver):
    name, addrs, category = "MCP9808", tuple(range(0x18, 0x20)), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        m = _rr(dev, addr, 0x06, 2); d = _rr(dev, addr, 0x07, 2)
        return 2 if u16be(*m) == 0x0054 and d[0] == 0x04 else 0

    def read(self):
        d = self.rr(0x05, 2)
        up = d[0] & 0x1F
        t = (up & 0x0F) * 16 + d[1] / 16.0
        if up & 0x10:
            t -= 256
        return [("Temperature", t, "°C")]


class TMP102(Driver):
    name, addrs, category = "TMP102/LM75", tuple(range(0x48, 0x50)), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        d = _rr(dev, addr, 0x00, 2)
        return 1 if -40 < s16be(*d) / 256.0 < 125 else 0

    def read(self):
        return [("Temperature", s16be(*self.rr(0x00, 2)) / 256.0, "°C")]


class MLX90614(Driver):
    name, addrs, category = "MLX90614 IR thermometer", (0x5A,), "Environment"

    @classmethod
    def identify(cls, dev, addr):
        d = _rr(dev, addr, 0x06, 3)
        return 1 if -40 < u16le(d[0], d[1]) * 0.02 - 273.15 < 125 else 0

    def _t(self, reg):
        d = self.rr(reg, 3)
        return u16le(d[0], d[1]) * 0.02 - 273.15

    def read(self):
        return [("Object", self._t(0x07), "°C"), ("Ambient", self._t(0x06), "°C")]


# ================================================================ AIR ======
class SCD4x(Driver):
    name, addrs, category = "SCD40/SCD41 CO2", (0x62,), "Air quality"
    note = "First reading appears after about 5 seconds, then every 5 s."
    destructive_identify = True

    @classmethod
    def identify(cls, dev, addr):
        dev.write(addr, [0x3F, 0x86]); time.sleep(0.5)          # stop periodic
        dev.write(addr, [0x36, 0x82]); time.sleep(0.002)        # serial number
        sensirion_words(dev.read(addr, 9))
        return 2

    def setup(self):
        self.last = None
        self.wr(0x21, 0xB1)                                      # start periodic

    def read(self):
        self.wr(0xE4, 0xB8); time.sleep(0.002)
        if sensirion_words(self.rd(3))[0] & 0x07FF:
            self.wr(0xEC, 0x05); time.sleep(0.002)
            co2, t, h = sensirion_words(self.rd(9))
            self.last = [("CO2", co2, "ppm"), ("Temperature", -45 + 175 * t / 65536, "°C"),
                         ("Humidity", 100 * h / 65536, "%RH")]
        return self.last or [("Status", "warming up", "")]


class SGP30(Driver):
    name, addrs, category = "SGP30 air quality", (0x58,), "Air quality"
    note = "Reads 400 ppm / 0 ppb for the first ~15 s. Best read every 1 s (live mode)."

    @classmethod
    def identify(cls, dev, addr):
        dev.write(addr, [0x20, 0x2F]); time.sleep(0.01)
        sensirion_words(dev.read(addr, 3))
        return 2

    def setup(self):
        self.wr(0x20, 0x03); time.sleep(0.01)

    def read(self):
        self.wr(0x20, 0x08); time.sleep(0.012)
        co2, tvoc = sensirion_words(self.rd(6))
        return [("eCO2", co2, "ppm"), ("TVOC", tvoc, "ppb")]


class CCS811(Driver):
    name, addrs, category = "CCS811 air quality", (0x5A, 0x5B), "Air quality"
    note = "WAKE pin must be tied to GND. Needs 20 min burn-in for accurate values."

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x20)[0] == 0x81 else 0

    def setup(self):
        self.last = None
        if not self.rr(0x00)[0] & 0x80:
            self.wr(0xF4); time.sleep(0.1)                      # app start
        self.wr(0x01, 0x10)                                     # 1 s measurements

    def read(self):
        st = self.rr(0x00)[0]
        if st & 0x01:
            raise CP2112Error(f"CCS811 error 0x{self.rr(0xE0)[0]:02X}")
        if st & 0x08:
            d = self.rr(0x02, 4)
            self.last = [("eCO2", u16be(d[0], d[1]), "ppm"), ("TVOC", u16be(d[2], d[3]), "ppb")]
        return self.last or [("Status", "waiting for data", "")]


# ============================================================== LIGHT =====
class BH1750(Driver):
    name, addrs, category = "BH1750 light", (0x23, 0x5C), "Light"

    @classmethod
    def identify(cls, dev, addr):
        dev.read(addr, 2)
        return 1

    def read(self):
        self.wr(0x01); self.wr(0x20); time.sleep(0.18)
        d = self.rd(2)
        return [("Light", u16be(*d) / 1.2, "lux")]


class VEML7700(Driver):
    name, addrs, category = "VEML7700 light", (0x10,), "Light"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x07, 2)[0] == 0x81 else 1

    def setup(self):
        self.wr(0x00, 0x00, 0x00); time.sleep(0.12)

    def read(self):
        a = self.rr(0x04, 2); w = self.rr(0x05, 2)
        return [("Light", u16le(*a) * 0.0576, "lux"), ("White", u16le(*w), "counts")]


class TSL2591(Driver):
    name, addrs, category = "TSL2591 light", (0x29,), "Light"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0xB2)[0] == 0x50 else 0

    def setup(self):
        self.wr(0xA1, 0x10)          # medium gain, 100 ms
        self.wr(0xA0, 0x03); time.sleep(0.12)

    def read(self):
        c0 = u16le(*self.rr(0xB4, 2)); c1 = u16le(*self.rr(0xB6, 2))
        cpl = (100 * 25) / 408.0
        lux = (c0 - c1) * (1.0 - c1 / c0) / cpl if c0 else 0.0
        return [("Light", max(0.0, lux), "lux"), ("Full", c0, "counts"), ("IR", c1, "counts")]


class TCS34725(Driver):
    name, addrs, category = "TCS34725 colour", (0x29,), "Light"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x92)[0] in (0x44, 0x4D, 0x10) else 0

    def setup(self):
        self.wr(0x81, 0xD5); self.wr(0x8F, 0x01)
        self.wr(0x80, 0x01); time.sleep(0.003); self.wr(0x80, 0x03); time.sleep(0.12)

    def read(self):
        d = self.rr(0xB4, 8)
        c, r, g, b = (u16le(d[i], d[i + 1]) for i in range(0, 8, 2))
        hexcol = "#%02X%02X%02X" % tuple(min(255, int(v * 255 / c)) if c else 0 for v in (r, g, b))
        return [("Clear", c, ""), ("Red", r, ""), ("Green", g, ""), ("Blue", b, ""), ("Colour", hexcol, "")]


class TSL2561(Driver):
    name, addrs, category = "TSL2561 light", (0x29, 0x39, 0x49), "Light"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x8A)[0] >> 4 in (0x1, 0x5) else 0

    def setup(self):
        self.wr(0x80, 0x03); self.wr(0x81, 0x02); time.sleep(0.42)

    def read(self):
        ch0 = u16le(*self.rr(0xAC, 2)); ch1 = u16le(*self.rr(0xAE, 2))
        a, b = ch0 * 16, ch1 * 16       # scale gain 1x to the datasheet's 16x
        r = b / a if a else 0
        if a == 0 or r > 1.30:
            lux = 0.0
        elif r <= 0.50:
            lux = 0.0304 * a - 0.062 * a * r ** 1.4
        elif r <= 0.61:
            lux = 0.0224 * a - 0.031 * b
        elif r <= 0.80:
            lux = 0.0128 * a - 0.0153 * b
        else:
            lux = 0.00146 * a - 0.00112 * b
        return [("Light", max(0.0, lux), "lux"), ("Broadband", ch0, "counts"), ("IR", ch1, "counts")]


class APDS9960(Driver):
    name, addrs, category = "APDS9960 gesture/colour", (0x39,), "Light"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x92)[0] in (0xAB, 0x9C, 0xA8) else 0

    def setup(self):
        self.wr(0x80, 0x00); self.wr(0x81, 0xDB); self.wr(0x8F, 0x01)
        self.wr(0x80, 0x07); time.sleep(0.12)

    def read(self):
        d = self.rr(0x94, 8)
        c, r, g, b = (u16le(d[i], d[i + 1]) for i in range(0, 8, 2))
        return [("Proximity", self.rr(0x9C)[0], "0-255"), ("Clear", c, ""), ("Red", r, ""),
                ("Green", g, ""), ("Blue", b, "")]


class VL53L0X(Driver):
    name, addrs, category = "VL53L0X distance", (0x29,), "Light"
    note = "Identified by ID register. Ranging needs ST's full init sequence - not supported here."

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0xC0)[0] == 0xEE else 0

    def read(self):
        return [("Model ID", f"0x{self.rr(0xC0)[0]:02X}", ""), ("Revision", f"0x{self.rr(0xC2)[0]:02X}", "")]


# ============================================================== POWER =====
class INA226(Driver):
    name, addrs, category = "INA226 power monitor", tuple(range(0x40, 0x50)), "Power"
    settings = {"Shunt ohms": 0.1}

    @classmethod
    def identify(cls, dev, addr):
        return 2 if u16be(*_rr(dev, addr, 0xFE, 2)) == 0x5449 and u16be(*_rr(dev, addr, 0xFF, 2)) == 0x2260 else 0

    def setup(self):
        self.wr(0x00, 0x41, 0x27)

    def read(self):
        v = u16be(*self.rr(0x02, 2)) * 1.25e-3
        vs = s16be(*self.rr(0x01, 2)) * 2.5e-6
        i = vs / self.opt("Shunt ohms")
        return [("Bus voltage", v, "V"), ("Shunt", vs * 1000, "mV"), ("Current", i * 1000, "mA"),
                ("Power", v * i * 1000, "mW")]


class INA3221(Driver):
    name, addrs, category = "INA3221 3-channel power", (0x40, 0x41, 0x42, 0x43), "Power"
    settings = {"Shunt ohms": 0.1}

    @classmethod
    def identify(cls, dev, addr):
        return 2 if u16be(*_rr(dev, addr, 0xFE, 2)) == 0x5449 and u16be(*_rr(dev, addr, 0xFF, 2)) == 0x3220 else 0

    def read(self):
        out = []
        for ch in range(3):
            vs = (s16be(*self.rr(0x01 + 2 * ch, 2)) >> 3) * 40e-6
            v = (s16be(*self.rr(0x02 + 2 * ch, 2)) >> 3) * 8e-3
            out += [(f"CH{ch + 1} voltage", v, "V"), (f"CH{ch + 1} current", vs / self.opt("Shunt ohms") * 1000, "mA")]
        return out


class INA219(Driver):
    name, addrs, category = "INA219 power monitor", tuple(range(0x40, 0x50)), "Power"
    settings = {"Shunt ohms": 0.1}
    home = (0x40, 0x41, 0x44, 0x45)

    @classmethod
    def identify(cls, dev, addr):
        cfg = u16be(*_rr(dev, addr, 0x00, 2))
        if cfg == 0x399F:
            return 2
        return 1 if not cfg & 0x8000 else 0

    def read(self):
        v = (u16be(*self.rr(0x02, 2)) >> 3) * 0.004
        vs = s16be(*self.rr(0x01, 2)) * 10e-6
        i = vs / self.opt("Shunt ohms")
        return [("Bus voltage", v, "V"), ("Shunt", vs * 1000, "mV"), ("Current", i * 1000, "mA"),
                ("Power", v * i * 1000, "mW")]


class MAX1704x(Driver):
    name, addrs, category = "MAX17048/43 battery gauge", (0x36,), "Power"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if 0x0001 <= u16be(*_rr(dev, addr, 0x08, 2)) <= 0x001F else 0

    def read(self):
        v = u16be(*self.rr(0x02, 2)) * 78.125e-6
        d = self.rr(0x04, 2)
        out = [("Cell voltage", v, "V"), ("Charge", d[0] + d[1] / 256.0, "%")]
        try:
            out.append(("Rate", s16be(*self.rr(0x16, 2)) * 0.208, "%/hr"))
        except CP2112Error:
            pass
        return out


class SmartBattery(Driver):
    name, addrs, category = "Smart battery (SBS)", (0x0B,), "Power"
    note = "Laptop / power-tool / e-bike packs. Capacities may be in 10 mWh units on some packs."

    @classmethod
    def identify(cls, dev, addr):
        v = u16le(*_rr(dev, addr, 0x09, 2))
        return 2 if 3000 <= v <= 60000 else 1

    def w(self, cmd):
        return u16le(*self.rr(cmd, 2))

    def s(self, cmd):
        d = self.rr(cmd, 33)
        n = min(d[0], 32)
        return bytes(d[1:1 + n]).decode("ascii", "replace").strip("\x00 ")

    def read(self):
        out = [("Voltage", self.w(0x09) / 1000.0, "V"),
               ("Current", s16le(*self.rr(0x0A, 2)), "mA"),
               ("Charge", self.w(0x0D), "%"),
               ("Temperature", self.w(0x08) / 10.0 - 273.15, "°C"),
               ("Remaining", self.w(0x0F), "mAh"),
               ("Full charge", self.w(0x10), "mAh"),
               ("Design capacity", self.w(0x18), "mAh"),
               ("Cycle count", self.w(0x17), "")]
        full, design = out[5][1], out[6][1]
        if design:
            out.append(("Health", 100.0 * full / design, "%"))
        for label, cmd in (("Maker", 0x20), ("Model", 0x21), ("Chemistry", 0x22)):
            try:
                out.append((label, self.s(cmd), ""))
            except CP2112Error:
                pass
        return out


# ============================================================ MOTION ======
class MPU6xxx(Driver):
    name, addrs, category = "MPU6050/6500/9250", (0x68, 0x69), "Motion"
    WHO = {0x68: "MPU6050", 0x70: "MPU6500", 0x71: "MPU9250", 0x73: "MPU9255", 0x19: "MPU6886", 0x12: "ICM20602"}

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x75)[0] in cls.WHO else 0

    def setup(self):
        self.who = self.rr(0x75)[0]
        self.name = self.WHO.get(self.who, self.name)
        self.wr(0x6B, 0x00); time.sleep(0.05)
        self.wr(0x1C, 0x00); self.wr(0x1B, 0x00)

    def read(self):
        d = self.rr(0x3B, 14)
        v = [s16be(d[i], d[i + 1]) for i in range(0, 14, 2)]
        temp = v[3] / 340.0 + 36.53 if self.who == 0x68 else v[3] / 333.87 + 21.0
        return [("Accel X", v[0] / 16384, "g"), ("Accel Y", v[1] / 16384, "g"), ("Accel Z", v[2] / 16384, "g"),
                ("Gyro X", v[4] / 131, "°/s"), ("Gyro Y", v[5] / 131, "°/s"), ("Gyro Z", v[6] / 131, "°/s"),
                ("Chip temp", temp, "°C")]


class ADXL345(Driver):
    name, addrs, category = "ADXL345 accelerometer", (0x53, 0x1D), "Motion"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x00)[0] == 0xE5 else 0

    def setup(self):
        self.wr(0x31, 0x08); self.wr(0x2D, 0x08)

    def read(self):
        d = self.rr(0x32, 6)
        x, y, z = (s16le(d[i], d[i + 1]) * 0.0039 for i in range(0, 6, 2))
        return [("Accel X", x, "g"), ("Accel Y", y, "g"), ("Accel Z", z, "g")]


class LIS3DH(Driver):
    name, addrs, category = "LIS3DH accelerometer", (0x18, 0x19), "Motion"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x0F)[0] == 0x33 else 0

    def setup(self):
        self.wr(0x20, 0x57); self.wr(0x23, 0x88)

    def read(self):
        d = self.rr(0xA8, 6)
        x, y, z = ((s16le(d[i], d[i + 1]) >> 4) / 1000.0 for i in range(0, 6, 2))
        return [("Accel X", x, "g"), ("Accel Y", y, "g"), ("Accel Z", z, "g")]


class LSM6DS(Driver):
    name, addrs, category = "LSM6DS3/DSL/DSOX", (0x6A, 0x6B), "Motion"
    WHO = {0x69: "LSM6DS3/DS33", 0x6A: "LSM6DSL/DS3TR-C", 0x6B: "LSM6DS3TR", 0x6C: "LSM6DSOX/DSO"}

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x0F)[0] in cls.WHO else 0

    def setup(self):
        self.name = self.WHO.get(self.rr(0x0F)[0], self.name)
        self.wr(0x10, 0x40); self.wr(0x11, 0x40); self.wr(0x12, 0x44); time.sleep(0.05)

    def read(self):
        d = self.rr(0x22, 12)
        v = [s16le(d[i], d[i + 1]) for i in range(0, 12, 2)]
        return [("Gyro X", v[0] * 8.75e-3, "°/s"), ("Gyro Y", v[1] * 8.75e-3, "°/s"), ("Gyro Z", v[2] * 8.75e-3, "°/s"),
                ("Accel X", v[3] * 0.061e-3, "g"), ("Accel Y", v[4] * 0.061e-3, "g"), ("Accel Z", v[5] * 0.061e-3, "g")]


class HMC5883L(Driver):
    name, addrs, category = "HMC5883L compass", (0x1E,), "Motion"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if bytes(_rr(dev, addr, 0x0A, 3)) == b"H43" else 0

    def setup(self):
        self.wr(0x00, 0x70); self.wr(0x01, 0x20); self.wr(0x02, 0x00); time.sleep(0.01)

    def read(self):
        d = self.rr(0x03, 6)
        x, z, y = (s16be(d[i], d[i + 1]) / 1090.0 for i in range(0, 6, 2))
        return [("X", x, "G"), ("Y", y, "G"), ("Z", z, "G"), ("Heading", heading(x, y), "°")]


class QMC5883L(Driver):
    name, addrs, category = "QMC5883L compass", (0x0D,), "Motion"

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x0D)[0] == 0xFF else 0

    def setup(self):
        self.wr(0x0B, 0x01); self.wr(0x09, 0x1D); time.sleep(0.01)

    def read(self):
        d = self.rr(0x00, 6)
        x, y, z = (s16le(d[i], d[i + 1]) / 3000.0 for i in range(0, 6, 2))
        return [("X", x, "G"), ("Y", y, "G"), ("Z", z, "G"), ("Heading", heading(x, y), "°")]


class AS5600(Driver):
    name, addrs, category = "AS5600 magnetic angle", (0x36,), "Motion"

    def read(self):
        raw = u16be(*self.rr(0x0C, 2)) & 0x0FFF
        st = self.rr(0x0B)[0]
        mag = "OK" if st & 0x20 and not st & 0x18 else ("too weak" if st & 0x10 else "too strong" if st & 0x08 else "no magnet")
        return [("Angle", raw * 360.0 / 4096, "°"), ("Magnet", mag, ""), ("AGC", self.rr(0x1A)[0], "")]


# ============================================================== TIME ======
class DS3231(Driver):
    name, addrs, category = "DS3231/DS1307 RTC", (0x68,), "Time"
    actions = [("Set to PC time", "set_time", [])]

    @classmethod
    def identify(cls, dev, addr):
        d = _rr(dev, addr, 0x00, 7)
        ok = (d[0] & 0x7F) < 0x60 and d[1] < 0x60 and 1 <= bcd(d[5] & 0x1F) <= 12 and 1 <= bcd(d[4]) <= 31
        return 2 if ok else 1

    def read(self):
        d = self.rr(0x00, 7)
        hr = d[2]
        h = bcd(hr & 0x1F) % 12 + (12 if hr & 0x20 else 0) if hr & 0x40 else bcd(hr & 0x3F)
        out = [("Time", f"20{bcd(d[6]):02d}-{bcd(d[5] & 0x1F):02d}-{bcd(d[4]):02d} "
                        f"{h:02d}:{bcd(d[1]):02d}:{bcd(d[0] & 0x7F):02d}", "")]
        if d[0] & 0x80:
            out.append(("Clock", "HALTED (DS1307) - use Set to PC time", ""))
        try:
            t = self.rr(0x11, 2)
            temp = s8(t[0]) + (t[1] >> 6) * 0.25
            if -40 <= temp <= 85 and not t[1] & 0x3F:
                out.append(("Chip temp", temp, "°C"))
        except CP2112Error:
            pass
        return out

    def set_time(self, p):
        n = datetime.datetime.now()
        self.wr(0x00, to_bcd(n.second), to_bcd(n.minute), to_bcd(n.hour), n.isoweekday(),
                to_bcd(n.day), to_bcd(n.month), to_bcd(n.year % 100))
        return f"RTC set to {n:%Y-%m-%d %H:%M:%S}"


class PCF8563(Driver):
    name, addrs, category = "PCF8563 RTC", (0x51,), "Time"
    actions = [("Set to PC time", "set_time", [])]

    @classmethod
    def identify(cls, dev, addr):
        return 0          # shares 0x51 with EEPROMs - choose manually

    def read(self):
        d = self.rr(0x02, 7)
        out = [("Time", f"20{bcd(d[6]):02d}-{bcd(d[5] & 0x1F):02d}-{bcd(d[3] & 0x3F):02d} "
                        f"{bcd(d[2] & 0x3F):02d}:{bcd(d[1] & 0x7F):02d}:{bcd(d[0] & 0x7F):02d}", "")]
        if d[0] & 0x80:
            out.append(("Warning", "clock lost power - set time", ""))
        return out

    def set_time(self, p):
        n = datetime.datetime.now()
        self.wr(0x02, to_bcd(n.second), to_bcd(n.minute), to_bcd(n.hour), to_bcd(n.day),
                n.weekday(), to_bcd(n.month), to_bcd(n.year % 100))
        return f"RTC set to {n:%Y-%m-%d %H:%M:%S}"


# ============================================================ ANALOG ======
class ADS1x15(Driver):
    name, addrs, category = "ADS1115/ADS1015 ADC", (0x48, 0x49, 0x4A, 0x4B), "Analog"
    settings = {"Range V (6.144/4.096/2.048/1.024)": 4.096, "Divider x (5 for 0-25V module)": 1.0}
    PGA = {6.144: 0, 4.096: 1, 2.048: 2, 1.024: 3}

    @classmethod
    def identify(cls, dev, addr):
        cfg = u16be(*_rr(dev, addr, 0x01, 2))
        return 2 if cfg == 0x8583 else (1 if cfg & 0x0003 == 0x0003 else 0)

    def read(self):
        rng = min(self.PGA, key=lambda k: abs(k - self.opt("Range V (6.144/4.096/2.048/1.024)")))
        div = self.opt("Divider x (5 for 0-25V module)")
        out = []
        for ch in range(4):
            cfg = 0x8000 | ((4 + ch) << 12) | (self.PGA[rng] << 9) | 0x0100 | (4 << 5) | 0x03
            self.wr(0x01, cfg >> 8, cfg & 0xFF)
            time.sleep(0.009)
            for _ in range(10):
                if self.rr(0x01, 2)[0] & 0x80:
                    break
                time.sleep(0.002)
            v = s16be(*self.rr(0x00, 2)) * rng / 32768.0
            out.append((f"A{ch}", v * div, "V"))
        return out


class PCF8591(Driver):
    name, addrs, category = "PCF8591 ADC/DAC", tuple(range(0x48, 0x50)), "Analog"
    settings = {"Vref V": 3.3}
    actions = [("Set DAC", "set_dac", [("Value 0-255", "128")])]

    @classmethod
    def identify(cls, dev, addr):
        return 0          # can't be told apart safely - choose manually

    def read(self):
        out = []
        for ch in range(4):
            self.wr(0x40 | ch)
            out.append((f"AIN{ch}", self.rd(2)[1] / 255.0 * self.opt("Vref V"), "V"))
        return out

    def set_dac(self, p):
        v = max(0, min(255, int(p["Value 0-255"], 0)))
        self.wr(0x40, v)
        return f"DAC = {v}"


class MCP4725(Driver):
    name, addrs, category = "MCP4725 DAC", tuple(range(0x60, 0x68)), "Analog"
    settings = {"VCC V": 3.3}
    actions = [("Set output", "set_out", [("Volts", "1.65")])]

    @classmethod
    def identify(cls, dev, addr):
        dev.read(addr, 5)
        return 1

    def read(self):
        d = self.rd(5)
        code = (d[1] << 4) | (d[2] >> 4)
        return [("Output", code / 4095.0 * self.opt("VCC V"), "V"), ("Code", code, "/4095"),
                ("EEPROM code", ((d[3] & 0x0F) << 8) | d[4], "")]

    def set_out(self, p):
        code = max(0, min(4095, round(float(p["Volts"]) / self.opt("VCC V") * 4095)))
        self.wr(code >> 8, code & 0xFF)
        return f"DAC code {code}"


# ======================================================== OUTPUTS / IO =====
class PCA9685(Driver):
    name, addrs, category = "PCA9685 16-ch PWM/servo", tuple(range(0x40, 0x70)), "Outputs"
    settings = {"Frequency Hz": 50}
    actions = [("Set duty", "set_duty", [("Channel 0-15", "0"), ("Duty %", "50")]),
               ("Servo angle", "servo", [("Channel 0-15", "0"), ("Angle 0-180", "90")]),
               ("All off", "all_off", [])]

    @classmethod
    def identify(cls, dev, addr):
        return 2 if _rr(dev, addr, 0x01)[0] == 0x04 and _rr(dev, addr, 0xFE)[0] >= 3 else 0

    def setup(self):
        pre = max(3, min(255, round(25e6 / (4096 * self.opt("Frequency Hz"))) - 1))
        self.wr(0x00, 0x10); self.wr(0xFE, pre); self.wr(0x01, 0x04)
        self.wr(0x00, 0x20); time.sleep(0.005); self.wr(0x00, 0xA0)

    def read(self):
        pre = self.rr(0xFE)[0]
        return [("Frequency", 25e6 / (4096 * (pre + 1)), "Hz"), ("MODE1", f"0x{self.rr(0x00)[0]:02X}", "")]

    def _set(self, ch, off_ticks, full_on=False, full_off=False):
        on_h = 0x10 if full_on else 0
        off_h = (off_ticks >> 8) | (0x10 if full_off else 0)
        self.wr(0x06 + 4 * ch, 0, on_h, off_ticks & 0xFF, off_h)

    def set_duty(self, p):
        ch = int(p["Channel 0-15"]); duty = float(p["Duty %"])
        if duty >= 100:
            self._set(ch, 0, full_on=True)
        elif duty <= 0:
            self._set(ch, 0, full_off=True)
        else:
            self._set(ch, int(duty * 4095 / 100))
        return f"CH{ch} = {duty:g}%"

    def servo(self, p):
        ch = int(p["Channel 0-15"]); ang = max(0.0, min(180.0, float(p["Angle 0-180"])))
        us = 500 + ang / 180.0 * 2000
        self._set(ch, int(us * 4096 * self.opt("Frequency Hz") / 1e6))
        return f"CH{ch} servo {ang:g}° ({us:.0f} µs)"

    def all_off(self, p):
        self.wr(0xFA, 0, 0, 0, 0x10)
        return "All channels off"


class TCA9548A(Driver):
    name, addrs, category = "TCA9548A I2C multiplexer", tuple(range(0x70, 0x78)), "IO"
    actions = [("Select channel", "select", [("Channel 0-7 (blank = none)", "0")])]
    note = "Select a channel, then go to the Scan tab and scan to see the devices behind it."

    @classmethod
    def identify(cls, dev, addr):
        dev.write(addr, [0x05]); a = dev.read(addr, 1)[0]
        dev.write(addr, [0x00]); b = dev.read(addr, 1)[0]
        return 2 if (a, b) == (0x05, 0x00) else 0

    def read(self):
        m = self.rd(1)[0]
        on = [str(i) for i in range(8) if m >> i & 1]
        return [("Enabled channels", ", ".join(on) or "none", "")]

    def select(self, p):
        s = p["Channel 0-7 (blank = none)"].strip()
        self.wr(0 if s == "" else 1 << int(s))
        return f"Mux channel {s or 'none'} selected"


class HT16K33(Driver):
    name, addrs, category = "HT16K33 LED matrix/7-seg", tuple(range(0x70, 0x78)), "Outputs"
    actions = [("All LEDs on", "all_on", []), ("Clear", "clear", []),
               ("Brightness", "bright", [("Level 0-15", "15")])]

    def setup(self):
        self.wr(0x21); self.wr(0x81); self.wr(0xEF)

    def read(self):
        return [("Display", "ready - use the buttons", "")]

    def all_on(self, p):
        self.wr(0x00, *[0xFF] * 16)
        return "All LEDs on"

    def clear(self, p):
        self.wr(0x00, *[0x00] * 16)
        return "Cleared"

    def bright(self, p):
        lv = max(0, min(15, int(p["Level 0-15"])))
        self.wr(0xE0 | lv)
        return f"Brightness {lv}"


class PCF8574(Driver):
    name, addrs, category = "PCF8574/A I/O expander", tuple(range(0x20, 0x28)) + tuple(range(0x38, 0x40)), "IO"
    actions = [("Write port", "write_port", [("Value (hex)", "FF")])]
    note = "If this is an LCD backpack (0x27/0x3F), use the LCD tab. Write FF to make all pins inputs."

    def read(self):
        v = self.rd(1)[0]
        return [("Port P7..P0", f"{v:08b}", ""), ("Hex", f"0x{v:02X}", "")]

    def write_port(self, p):
        v = int(p["Value (hex)"], 16) & 0xFF
        self.wr(v)
        return f"Port = 0x{v:02X}"


class MCP23017(Driver):
    name, addrs, category = "MCP23017 16-bit I/O", tuple(range(0x20, 0x28)), "IO"
    actions = [("Port A output", "out_a", [("Value (hex)", "00")]), ("Port B output", "out_b", [("Value (hex)", "00")])]

    @classmethod
    def identify(cls, dev, addr):
        return 0          # shares addresses with PCF8574 - choose manually

    def read(self):
        a, b = self.rr(0x12)[0], self.rr(0x13)[0]
        return [("GPIOA", f"{a:08b}", ""), ("GPIOB", f"{b:08b}", "")]

    def out_a(self, p):
        v = int(p["Value (hex)"], 16) & 0xFF
        self.wr(0x00, 0x00); self.wr(0x14, v)
        return f"Port A outputs = 0x{v:02X}"

    def out_b(self, p):
        v = int(p["Value (hex)"], 16) & 0xFF
        self.wr(0x01, 0x00); self.wr(0x15, v)
        return f"Port B outputs = 0x{v:02X}"


# ========================================================= DISPLAY / MEM ===
class OLEDdisp(Driver):
    name, addrs, category = "SSD1306/SH1106 OLED", (0x3C, 0x3D), "Display"
    note = "Use the OLED tab to draw text and images."

    def read(self):
        return [("Display", "use the OLED tab", "")]


class EEPROM24(Driver):
    name, addrs, category = "24Cxx EEPROM", tuple(range(0x50, 0x58)), "Memory"
    settings = {"Address bytes (1=24C01-16, 2=24C32+)": 2}
    note = "Read-only preview. Use Registers > Dump (tick 16-bit for 24C32+) for more."

    def read(self):
        rb = 2 if self.opt("Address bytes (1=24C01-16, 2=24C32+)") >= 2 else 1
        d = self.rr(0x00, 16, rb)
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in d)
        return [("First 16 bytes", " ".join(f"{b:02X}" for b in d), ""), ("ASCII", txt, "")]


class Unknown(Driver):
    name, category = "Unknown device", "Other"
    note = "Not recognised. Try 'Treat as' if you know what it is, or explore it on the Registers tab."

    def read(self):
        try:
            d = self.rd(4)
            return [("Raw read", " ".join(f"{b:02X}" for b in d), "")]
        except CP2112Error as e:
            return [("Raw read", str(e), "")]


# ------------------------------------------------------------------ registry --
REGISTRY = [
    INA226, INA3221, PCA9685, HTU21D, INA219,
    SHT4x, SHT3x,
    BMx280, BMP180, BME680,
    AHTx0,
    APDS9960, TSL2591, TCS34725, VL53L0X, TSL2561,
    MCP9808, LIS3DH,
    MPU6xxx, DS3231, PCF8563,
    ADXL345, LSM6DS, HMC5883L, QMC5883L,
    MAX1704x, AS5600,
    SmartBattery,
    CCS811, MLX90614, SGP30,
    MCP4725, SCD4x,
    TCA9548A, HT16K33,
    ADS1x15, TMP102, PCF8591,
    BH1750, VEML7700,
    OLEDdisp,
    EEPROM24,
    PCF8574, MCP23017,
]


def candidates(addr):
    return [c for c in REGISTRY if addr in c.addrs]


def identify(dev, addr):
    """Return (best driver class, [all candidate classes]) for one address."""
    cands = candidates(addr)
    best, best_score = None, 0
    for c in cands:
        if c.destructive_identify and best_score >= 1:
            continue
        try:
            sc = c.identify(dev, addr)
        except (CP2112Error, IndexError, ValueError):
            sc = 0
        if 0 < sc < 2 and addr in (c.home or c.addrs):
            sc += 0.5
        if sc > best_score:
            best, best_score = c, sc
        if sc >= 2:
            break
    return (best or Unknown), cands + [Unknown]


def supported_parts():
    return sorted({c.name for c in REGISTRY})
