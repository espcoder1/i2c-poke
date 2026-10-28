"""
SSD1306 / SH1106 I2C OLED driver for the CP2112 (used by I2C Poke).

Works in page-addressing mode, which both controllers support.
SH1106 (usually 1.3" displays) has 132 columns of RAM, so its image sits 2 columns in.
"""
from PIL import Image, ImageDraw, ImageFont, ImageOps

CTRL_CMD, CTRL_DATA = 0x00, 0x40
CHUNK = 60   # CP2112 writes are max 61 bytes incl. the control byte


class OLED:
    def __init__(self, dev, addr=0x3C, controller="SSD1306", width=128, height=64):
        self.dev, self.addr = dev, addr
        self.controller, self.w, self.h = controller, width, height
        self.col_offset = 2 if controller == "SH1106" else 0

    def cmd(self, *b):
        self.dev.write(self.addr, [CTRL_CMD, *b])

    def init(self, contrast=0xCF):
        tall = self.h == 64
        seq = [0xAE,                    # display off
               0xD5, 0x80,              # clock
               0xA8, self.h - 1,        # multiplex
               0xD3, 0x00,              # display offset
               0x40,                    # start line 0
               0x20, 0x02,              # page addressing mode (SSD1306)
               0xA1, 0xC8,              # flip so (0,0) is top-left
               0xDA, 0x12 if tall else 0x02,   # COM pins
               0x81, max(0, min(255, int(contrast))),
               0xD9, 0xF1,              # pre-charge
               0xDB, 0x40,              # VCOM detect
               0xA4, 0xA6]              # show RAM, normal (not inverted)
        if self.controller == "SH1106":
            seq += [0xAD, 0x8B]         # SH1106 DC-DC on
        else:
            seq += [0x8D, 0x14]         # SSD1306 charge pump on
        self.cmd(*seq)
        self.clear()
        self.cmd(0xAF)                  # display on

    def contrast(self, v):
        self.cmd(0x81, max(0, min(255, int(v))))

    def invert(self, on):
        self.cmd(0xA7 if on else 0xA6)

    def power(self, on):
        self.cmd(0xAF if on else 0xAE)

    def clear(self):
        self.show(Image.new("1", (self.w, self.h), 0))

    def show(self, img):
        """Send a 1-bit PIL image (w x h) to the display."""
        for page, data in enumerate(image_to_pages(img, self.w, self.h)):
            col = self.col_offset
            self.cmd(0xB0 | page, 0x00 | (col & 0x0F), 0x10 | (col >> 4))
            for i in range(0, len(data), CHUNK):
                self.dev.write(self.addr, [CTRL_DATA] + data[i:i + CHUNK])


def image_to_pages(img, w, h):
    """1-bit image -> list of pages, each a list of w column bytes (LSB = top pixel)."""
    img = img.convert("1")
    if img.size != (w, h):
        img = img.resize((w, h))
    px = img.load()
    pages = []
    for p in range(h // 8):
        row = []
        for x in range(w):
            b = 0
            for bit in range(8):
                if px[x, p * 8 + bit]:
                    b |= 1 << bit
            row.append(b)
        pages.append(row)
    return pages


def load_font(size):
    for name in ("consola.ttf", "arial.ttf", "DejaVuSansMono.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_text(text, w, h, size=12):
    img = Image.new("1", (w, h), 0)
    ImageDraw.Draw(img).multiline_text((0, 0), text, fill=1, font=load_font(size), spacing=1)
    return img


def render_image(path, w, h):
    """Fit any picture onto the display, dithered to black/white."""
    src = Image.open(path).convert("L")
    src = ImageOps.contain(src, (w, h))
    canvas = Image.new("L", (w, h), 0)
    canvas.paste(src, ((w - src.width) // 2, (h - src.height) // 2))
    return canvas.convert("1")   # Floyd-Steinberg dither


def test_pattern(w, h):
    img = Image.new("1", (w, h), 0)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w - 1, h - 1), outline=1)
    d.line((0, 0, w - 1, h - 1), fill=1)
    d.line((0, h - 1, w - 1, 0), fill=1)
    d.text((4, 3), f"{w}x{h} OK", fill=1, font=load_font(10))
    return img
