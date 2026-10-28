"""
Driver tests against simulated chips - no hardware needed.

    python -m unittest discover -s tests -v

Expected values come from the chip datasheets' worked examples where they
exist (BMP280, BMP180), otherwise from values the fake chip was loaded with.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fake_cp2112 as fake            # noqa: E402
sys.modules["hid"] = fake             # the app imports 'hid'; give it the fake

from fake_cp2112 import Reg, sw, u16  # noqa: E402
import i2cpoke                        # noqa: E402
import devices                        # noqa: E402
import lcd                            # noqa: E402
import oled                           # noqa: E402


def bus(chips):
    fake.DEVICES.clear()
    fake.DEVICES.update(chips)
    return i2cpoke.CP2112()


def reading(drv, label):
    drv.ensure()
    for lab, v, _ in drv.read():
        if lab == label:
            return v
    raise KeyError(label)


class SGP30(Reg):
    def __init__(s):
        super().__init__({})
        s.cmd = None

    def write(s, d):
        s.cmd = tuple(d)
        return True

    def read(s, n):
        c, s.cmd = s.cmd, None
        if c == (0x20, 0x2F):
            return sw(0x0022)
        if c == (0x20, 0x08):
            return sw(415, 23)
        return None


def rig_a():
    return {
        0x0B: fake.SBS(),
        0x0D: Reg({0x0D: [0xFF], 0x00: [100, 0, 0, 0, 0x10, 0]}),
        0x18: Reg({0x06: u16(0x0054), 0x07: u16(0x0400), 0x05: [0xC1, 0x94]}),
        0x27: fake.PCF(),
        0x38: fake.AHT(),
        0x3C: Reg({}),
        0x40: Reg({0xFE: u16(0x5449), 0xFF: u16(0x2260), 0x02: u16(9600), 0x01: u16(4000)}),
        0x41: Reg({0x00: u16(0x399F), 0x02: u16(int(5.0 / 0.004) << 3), 0x01: u16(2500)}),
        0x44: fake.SHT3x(),
        0x48: fake.ADS(),
        0x49: Reg({0x00: [0x17, 0x80], 0x01: [0x60, 0xA0]}),
        0x50: fake.EEP(),
        0x62: fake.SCD(),
        0x68: Reg({0x75: [0x68], 0x3B: u16(16384) + [0] * 4 + u16(0xFFFF + 1 - 3400) + u16(131) + [0] * 4}),
        0x70: fake.TCA(),
        0x76: fake.BMP280(),
        0x77: fake.BMP180(),
    }


def rig_b():
    return {
        0x10: Reg({0x07: [0x81, 0xC4], 0x04: [0xE8, 0x03], 0x05: [0, 1]}),
        0x19: Reg({0x0F: [0x33], 0xA8: [0x00, 0x40, 0, 0, 0, 0]}),
        0x1E: Reg({0x0A: list(b"H43"), 0x03: u16(1090) + u16(0) + u16(1090)}),
        0x23: Reg({0x00: [0, 0], 0x20: [0x01, 0x2C], 0x01: [0x01, 0x2C]}),
        0x29: Reg({0xB2: [0x50], 0xB4: [0x10, 0x27], 0xB6: [0xE8, 0x03]}),
        0x36: Reg({0x08: u16(0x0012), 0x02: u16(int(3.9 / 78.125e-6)), 0x04: [76, 128], 0x16: u16(0xFFFF - 9)}),
        0x40: Reg({0x01: [0x04], 0xFE: [0x1E], 0x00: [0x11]}),
        0x53: Reg({0x00: [0xE5], 0x32: [0, 1, 0, 0, 0, 0]}),
        0x58: SGP30(),
        0x5A: Reg({0x20: [0x81], 0x00: [0x98], 0x02: [0x01, 0x90, 0x00, 0x0A]}),
        0x68: Reg({0x75: [0x00], 0x00: [0x30, 0x45, 0x12, 0x03, 0x28, 0x10, 0x26], 0x11: [0x16, 0x40]}),
        0x71: Reg({}),
    }


EXPECT_A = {
    0x0B: "Smart battery (SBS)", 0x0D: "QMC5883L compass", 0x18: "MCP9808",
    0x27: "PCF8574/A I/O expander", 0x38: "AHT10/AHT20", 0x3C: "SSD1306/SH1106 OLED",
    0x40: "INA226 power monitor", 0x41: "INA219 power monitor", 0x44: "SHT30/31/35",
    0x48: "ADS1115/ADS1015 ADC", 0x49: "TMP102/LM75", 0x50: "24Cxx EEPROM",
    0x62: "SCD40/SCD41 CO2", 0x68: "MPU6050/6500/9250", 0x70: "TCA9548A I2C multiplexer",
    0x76: "BMP280/BME280", 0x77: "BMP180/BMP085",
}
EXPECT_B = {
    0x10: "VEML7700 light", 0x19: "LIS3DH accelerometer", 0x1E: "HMC5883L compass",
    0x23: "BH1750 light", 0x29: "TSL2591 light", 0x36: "MAX17048/43 battery gauge",
    0x40: "PCA9685 16-ch PWM/servo", 0x53: "ADXL345 accelerometer", 0x58: "SGP30 air quality",
    0x5A: "CCS811 air quality", 0x68: "DS3231/DS1307 RTC", 0x71: "HT16K33 LED matrix/7-seg",
}


class TestScan(unittest.TestCase):
    def test_finds_every_chip(self):
        dev = bus(rig_a())
        found = [a for a in range(0x08, 0x78) if dev.present(a)]
        self.assertEqual(found, sorted(EXPECT_A))

    def test_nack_is_not_an_error(self):
        dev = bus({})
        self.assertFalse(dev.probe(0x50))


class TestIdentify(unittest.TestCase):
    def _check(self, chips, expect):
        dev = bus(chips)
        for addr, name in expect.items():
            with self.subTest(addr=hex(addr)):
                cls, _ = devices.identify(dev, addr)
                self.assertEqual(cls.name, name)

    def test_rig_a(self):
        self._check(rig_a(), EXPECT_A)

    def test_rig_b(self):
        self._check(rig_b(), EXPECT_B)


class TestReadings(unittest.TestCase):
    def setUp(self):
        self.dev = bus({**rig_a(), **{k: v for k, v in rig_b().items() if k not in rig_a()}})

    def drv(self, cls, addr):
        return cls(self.dev, addr)

    def test_bmp280_datasheet_example(self):
        d = self.drv(devices.BMx280, 0x76)
        self.assertAlmostEqual(reading(d, "Temperature"), 25.08, places=2)
        self.assertAlmostEqual(reading(d, "Pressure"), 1006.53, places=1)

    def test_bmp180_datasheet_example(self):
        d = self.drv(devices.BMP180, 0x77)
        d.ensure()
        t, hpa = d.compute(27898, 23843)
        self.assertAlmostEqual(t, 15.0, places=1)
        self.assertAlmostEqual(hpa, 699.64, places=2)

    def test_aht20(self):
        d = self.drv(devices.AHTx0, 0x38)
        self.assertAlmostEqual(reading(d, "Temperature"), 25.0, places=1)
        self.assertAlmostEqual(reading(d, "Humidity"), 50.0, places=1)

    def test_sht3x(self):
        d = self.drv(devices.SHT3x, 0x44)
        self.assertAlmostEqual(reading(d, "Temperature"), 22.5, places=1)
        self.assertAlmostEqual(reading(d, "Humidity"), 55.0, places=1)

    def test_ina226(self):
        d = self.drv(devices.INA226, 0x40)
        self.assertAlmostEqual(reading(d, "Bus voltage"), 12.0, places=3)
        self.assertAlmostEqual(reading(d, "Current"), 100.0, places=2)

    def test_ina219(self):
        d = self.drv(devices.INA219, 0x41)
        self.assertAlmostEqual(reading(d, "Bus voltage"), 5.0, places=3)
        self.assertAlmostEqual(reading(d, "Current"), 250.0, places=2)

    def test_ads1115_and_divider(self):
        d = self.drv(devices.ADS1x15, 0x48)
        self.assertAlmostEqual(reading(d, "A1"), 2.5, places=3)
        d.opts["Divider x (5 for 0-25V module)"] = 5
        self.assertAlmostEqual(reading(d, "A1"), 12.5, places=2)

    def test_smart_battery(self):
        d = self.drv(devices.SmartBattery, 0x0B)
        self.assertAlmostEqual(reading(d, "Voltage"), 16.2)
        self.assertEqual(reading(d, "Current"), -500)
        self.assertAlmostEqual(reading(d, "Health"), 80.0)
        self.assertEqual(reading(d, "Model"), "DELL 4P894")

    def test_mcp9808(self):
        self.assertAlmostEqual(reading(self.drv(devices.MCP9808, 0x18), "Temperature"), 25.25)

    def test_scd4x(self):
        d = self.drv(devices.SCD4x, 0x62)
        self.assertEqual(reading(d, "CO2"), 650)

    def test_rtc_set_time_writes_bcd(self):
        chips = rig_b()
        self.dev = bus(chips)
        d = self.drv(devices.DS3231, 0x68)
        d.set_time({})
        w = chips[0x68].log[-1]
        self.assertEqual(w[0], 0x00)
        self.assertEqual(len(w), 8)
        for b in w[1:]:
            self.assertLess(b & 0x0F, 10, "every field must be BCD")

    def test_pca9685_servo_centre(self):
        chips = rig_b()
        self.dev = bus(chips)
        d = self.drv(devices.PCA9685, 0x40)
        d.ensure()
        d.servo({"Channel 0-15": "0", "Angle 0-180": "90"})
        w = chips[0x40].log[-1]
        self.assertEqual(w[0], 0x06)
        ticks = w[3] | (w[4] << 8)
        self.assertAlmostEqual(ticks, 1500e-6 * 50 * 4096, delta=2)   # 1.5 ms at 50 Hz


class TestDisplays(unittest.TestCase):
    def test_oled_bytes_rebuild_the_image(self):
        sent = []

        class Dev:
            def write(self, addr, data):
                sent.append(list(data))
        img = oled.render_text("Test 123", 128, 64, 12)
        oled.OLED(Dev(), 0x3C).show(img)
        ram, page, col = [[0] * 128 for _ in range(8)], 0, 0
        for d in sent:
            if d[0] == 0x00:
                page, col = d[1] & 7, (d[2] & 0x0F) | ((d[3] & 0x0F) << 4)
            else:
                for b in d[1:]:
                    ram[page][col] = b
                    col += 1
        px = img.load()
        for x in range(128):
            for y in range(64):
                self.assertEqual(bool(ram[y // 8][x] >> (y % 8) & 1), bool(px[x, y]))
        self.assertLessEqual(max(len(d) for d in sent), 61)

    def test_lcd_text_decodes(self):
        sent = []

        class Dev:
            def write(self, addr, data):
                sent.extend(data)
        l = lcd.LCD(Dev(), 0x27, 16, 2)
        l.show(["Hello", "World"])
        prev, nibs, chars = 0, [], []
        for b in sent:
            if prev & lcd.E and not b & lcd.E:
                nibs.append((b >> 4, b & lcd.RS))
                if len(nibs) == 2:
                    v, rs = (nibs[0][0] << 4) | nibs[1][0], nibs[0][1]
                    if rs:
                        chars.append(chr(v))
                    nibs = []
            prev = b
        text = "".join(chars)
        self.assertEqual(text, "Hello".ljust(16) + "World".ljust(16))


if __name__ == "__main__":
    unittest.main()
