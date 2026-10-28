"""
HD44780 character LCD (16x2 / 20x4) on a PCF8574 I2C backpack, for the CP2112.

Standard backpack wiring: P0=RS  P1=RW  P2=E  P3=backlight  P4..P7=D4..D7
The LCD runs in 4-bit mode; each byte is sent as two nibbles, each nibble
latched by pulsing E. The PCF8574 just copies every I2C byte to its pins,
so a whole run of nibbles goes out in one I2C write.
"""
import time

RS, E, BL = 0x01, 0x04, 0x08
ROW_OFFSETS = (0x00, 0x40, 0x14, 0x54)
CHUNK = 60      # CP2112 max write is 61 bytes; keep whole characters (4 bytes each)


class LCD:
    def __init__(self, dev, addr=0x27, cols=16, rows=2):
        self.dev, self.addr, self.cols, self.rows = dev, addr, cols, rows
        self.bl = BL

    # --- low level -----------------------------------------------------------
    def _pulse(self, nibble_bits, rs):
        b = (nibble_bits & 0xF0) | self.bl | rs
        return [b | E, b]                       # E high, then low = latch

    def _byte(self, val, rs):
        return self._pulse(val & 0xF0, rs) + self._pulse((val << 4) & 0xF0, rs)

    def _send(self, seq):
        for i in range(0, len(seq), CHUNK):
            self.dev.write(self.addr, seq[i:i + CHUNK])

    def command(self, c):
        self._send(self._byte(c, 0))
        if c in (0x01, 0x02):                   # clear / home are slow
            time.sleep(0.003)

    # --- public --------------------------------------------------------------
    def init(self):
        self.dev.write(self.addr, [self.bl])
        time.sleep(0.05)
        for nib, wait in ((0x30, 0.005), (0x30, 0.001), (0x30, 0.001), (0x20, 0.001)):
            self._send(self._pulse(nib, 0))     # wake-up: force 8-bit, then 4-bit
            time.sleep(wait)
        for c in (0x20 | 0x08,                  # 4-bit, 2+ lines, 5x8 font
                  0x08,                         # display off
                  0x01,                         # clear
                  0x06,                         # entry mode: move right
                  0x0C):                        # display on, no cursor
            self.command(c)

    def backlight(self, on):
        self.bl = BL if on else 0
        self.dev.write(self.addr, [self.bl])

    def clear(self):
        self.command(0x01)

    def write_line(self, row, text):
        self.command(0x80 | (ROW_OFFSETS[row] + 0))
        text = text[:self.cols].ljust(self.cols)
        seq = []
        for ch in text:
            code = ord(ch)
            seq += self._byte(code if 32 <= code < 127 else ord("?"), RS)
        self._send(seq)

    def show(self, lines):
        for r in range(self.rows):
            self.write_line(r, lines[r] if r < len(lines) else "")
