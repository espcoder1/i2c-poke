"""
Environmental sensor drivers for the CP2112 (used by I2C Poke).

  AHT10 / AHT20 (0x38)          temperature + humidity
  BMP280 (0x76/0x77, id 0x58)   temperature + pressure
  BME280 (0x76/0x77, id 0x60)   temperature + pressure + humidity

Compensation maths follows the Bosch BMP280/BME280 datasheets (float versions).
"""
import math
import time

from i2cpoke import CP2112Error


def s16(lo, hi):
    v = lo | (hi << 8)
    return v - 0x10000 if v & 0x8000 else v


def u16(lo, hi):
    return lo | (hi << 8)


# ---------------------------------------------------------------- AHT10/20 --
class AHT20:
    name = "AHT20"

    def __init__(self, dev, addr=0x38):
        self.dev, self.addr = dev, addr
        st = self.dev.read(addr, 1)[0]
        if not st & 0x08:                       # not calibrated -> load calibration
            self.dev.write(addr, [0xBE, 0x08, 0x00])
            time.sleep(0.02)

    def read(self):
        self.dev.write(self.addr, [0xAC, 0x33, 0x00])
        time.sleep(0.085)
        for _ in range(10):
            d = self.dev.read(self.addr, 6)
            if not d[0] & 0x80:                 # busy bit clear
                break
            time.sleep(0.02)
        else:
            raise CP2112Error("AHT20 stayed busy")
        raw_h = (d[1] << 12) | (d[2] << 4) | (d[3] >> 4)
        raw_t = ((d[3] & 0x0F) << 16) | (d[4] << 8) | d[5]
        return {"temp_c": raw_t * 200.0 / (1 << 20) - 50.0,
                "humidity": raw_h * 100.0 / (1 << 20)}


# ------------------------------------------------------------ BMP/BME280 --
class BMx280:
    def __init__(self, dev, addr):
        self.dev, self.addr = dev, addr
        cid = dev.read_reg(addr, 0xD0, 1)[0]
        if cid not in (0x56, 0x57, 0x58, 0x60):
            raise CP2112Error(f"0x{addr:02X} chip id 0x{cid:02X} is not a BMP280/BME280")
        self.has_h = cid == 0x60
        self.name = "BME280" if self.has_h else "BMP280"
        c = dev.read_reg(addr, 0x88, 24)
        self.T1 = u16(c[0], c[1]); self.T2 = s16(c[2], c[3]); self.T3 = s16(c[4], c[5])
        self.P1 = u16(c[6], c[7])
        self.P2, self.P3, self.P4, self.P5, self.P6, self.P7, self.P8, self.P9 = (
            s16(c[i], c[i + 1]) for i in range(8, 24, 2))
        if self.has_h:
            self.H1 = dev.read_reg(addr, 0xA1, 1)[0]
            h = dev.read_reg(addr, 0xE1, 7)
            self.H2 = s16(h[0], h[1]); self.H3 = h[2]
            h4 = (h[3] << 4) | (h[4] & 0x0F); self.H4 = h4 - 4096 if h4 & 0x800 else h4
            h5 = (h[5] << 4) | (h[4] >> 4); self.H5 = h5 - 4096 if h5 & 0x800 else h5
            self.H6 = h[6] - 256 if h[6] & 0x80 else h[6]
            dev.write(addr, [0xF2, 0x01])       # humidity x1 (must precede 0xF4)
        dev.write(addr, [0xF5, 0x00])           # no filter

    def read(self):
        self.dev.write(self.addr, [0xF4, 0x25])  # temp x1, press x1, forced mode
        time.sleep(0.012)
        for _ in range(10):
            if not self.dev.read_reg(self.addr, 0xF3, 1)[0] & 0x08:
                break
            time.sleep(0.005)
        d = self.dev.read_reg(self.addr, 0xF7, 8 if self.has_h else 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        out = self.compensate(adc_t, adc_p, (d[6] << 8) | d[7] if self.has_h else None)
        return out

    def compensate(self, adc_t, adc_p, adc_h=None):
        v1 = (adc_t / 16384.0 - self.T1 / 1024.0) * self.T2
        v2 = (adc_t / 131072.0 - self.T1 / 8192.0) ** 2 * self.T3
        t_fine = v1 + v2
        out = {"temp_c": t_fine / 5120.0}
        v1 = t_fine / 2.0 - 64000.0
        v2 = v1 * v1 * self.P6 / 32768.0
        v2 = v2 + v1 * self.P5 * 2.0
        v2 = v2 / 4.0 + self.P4 * 65536.0
        v1 = (self.P3 * v1 * v1 / 524288.0 + self.P2 * v1) / 524288.0
        v1 = (1.0 + v1 / 32768.0) * self.P1
        if v1:
            p = 1048576.0 - adc_p
            p = (p - v2 / 4096.0) * 6250.0 / v1
            v1 = self.P9 * p * p / 2147483648.0
            v2 = p * self.P8 / 32768.0
            out["pressure_hpa"] = (p + (v1 + v2 + self.P7) / 16.0) / 100.0
        if adc_h is not None:
            h = t_fine - 76800.0
            h = (adc_h - (self.H4 * 64.0 + self.H5 / 16384.0 * h)) * (
                self.H2 / 65536.0 * (1.0 + self.H6 / 67108864.0 * h * (1.0 + self.H3 / 67108864.0 * h)))
            h = h * (1.0 - self.H1 * h / 524288.0)
            out["humidity"] = max(0.0, min(100.0, h))
        return out


# --------------------------------------------------------------- helpers --
def detect(dev):
    """Return a list of driver objects for every supported sensor found."""
    found = []
    for addr in (0x38,):
        if dev.probe(addr):
            found.append(AHT20(dev, addr))
    for addr in (0x76, 0x77):
        if dev.probe(addr):
            try:
                found.append(BMx280(dev, addr))
            except CP2112Error:
                pass
    return found


def dew_point(t, rh):
    if rh <= 0:
        return None
    a, b = 17.62, 243.12
    g = math.log(rh / 100.0) + a * t / (b + t)
    return b * g / (a - g)


def altitude_m(hpa, sea_level=1013.25):
    return 44330.0 * (1.0 - (hpa / sea_level) ** 0.1903)
