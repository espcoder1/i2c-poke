"""
I2C Poke - Windows GUI for the CP2112 USB-to-I2C bridge.

Needs i2cpoke.py in the same folder (it holds the CP2112 driver).
Run directly:   py i2cpoke_gui.pyw
Build an .exe:  run build.bat
"""
import csv
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import hid  # noqa: F401  (checked early so we can show a friendly message)
    import i2cpoke
    import oled
    import lcd
    import sensors
    import devices
    from i2cpoke import CP2112, CP2112Error, KNOWN
except (ImportError, SystemExit):
    tk.Tk().withdraw()
    messagebox.showerror("I2C Poke", "The hidapi library is missing.\n\nRun:  py -m pip install hidapi")
    raise

APP = "I2C Poke"
FOUND_BG, EMPTY_BG, SEL_BG = "#2e7d32", "#e0e0e0", "#1565c0"


def parse_num(s):
    return int(str(s).strip(), 0)


def parse_bytes(s):
    """'0x41 27 0xFF' or '41,27' -> [0x41, 0x27, 0xFF]. Bare tokens are hex."""
    out = []
    for tok in s.replace(",", " ").split():
        out.append(int(tok, 16) if not tok.lower().startswith("0x") else int(tok, 0))
    if any(not 0 <= b <= 255 for b in out):
        raise ValueError("bytes must be 0-255")
    return out


def hexs(data):
    return " ".join(f"{b:02X}" for b in data)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("820x640")
        self.minsize(720, 560)
        self.dev = None
        self.lock = threading.Lock()
        self.ui_q = queue.Queue()
        self.watching = False
        self.watch_rows = []
        self.addr_var = tk.StringVar(value="0x76")

        self._build_top()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._build_scan(nb)
        self._build_regs(nb)
        self._build_watch(nb)
        self._build_gpio(nb)
        self._build_oled(nb)
        self._build_lcd(nb)
        self._build_sensors(nb)
        self._build_devices(nb)
        self._build_log()
        self.after(50, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(200, self.connect)

    # ---------- plumbing --------------------------------------------------
    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                fn = self.ui_q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def ui(self, fn):
        """Schedule fn on the Tk thread."""
        self.ui_q.put(fn)

    def bg(self, work, done=None, label=None, always=None):
        """Run work(dev) on a worker thread; done(result) back on the UI."""
        if not self.dev:
            self.log("Not connected.")
            return

        def run():
            try:
                with self.lock:
                    res = work(self.dev)
                if done:
                    self.ui(lambda: done(res))
            except CP2112Error as e:
                self.ui(lambda e=e: self.log(f"{label or 'I2C'} error: {e}"))
            except Exception as e:  # USB unplugged etc.
                self.ui(lambda e=e: self._lost(e))
            finally:
                if always:
                    self.ui(always)
        threading.Thread(target=run, daemon=True).start()

    def _lost(self, e):
        self.log(f"Device error: {e}")
        self.disconnect()

    # ---------- top bar ----------------------------------------------------
    def _build_top(self):
        f = ttk.Frame(self, padding=8)
        f.pack(fill="x")
        self.conn_btn = ttk.Button(f, text="Connect", command=self.toggle_conn, width=12)
        self.conn_btn.pack(side="left")
        self.status = ttk.Label(f, text="Disconnected", foreground="#b71c1c")
        self.status.pack(side="left", padx=10)
        ttk.Label(f, text="Bus speed").pack(side="left", padx=(20, 4))
        self.speed = ttk.Combobox(f, values=["10000", "100000", "400000"], width=8, state="readonly")
        self.speed.set("100000")
        self.speed.pack(side="left")
        self.speed.bind("<<ComboboxSelected>>", lambda e: self.apply_speed())
        ttk.Label(f, text="Hz").pack(side="left", padx=4)
        ttk.Label(f, text="Device address").pack(side="left", padx=(20, 4))
        ttk.Entry(f, textvariable=self.addr_var, width=6).pack(side="left")

    def toggle_conn(self):
        self.disconnect() if self.dev else self.connect()

    def connect(self):
        try:
            self.dev = CP2112(int(self.speed.get()))
        except CP2112Error as e:
            self.dev = None
            self.status.config(text="Not found - plug in the CP2112", foreground="#b71c1c")
            self.log(str(e))
            return
        self.status.config(text="Connected", foreground=FOUND_BG)
        self.conn_btn.config(text="Disconnect")
        try:
            part, ver = self.dev.version()
            self.log(f"CP2112 connected at {self.speed.get()} Hz (part 0x{part:02X}, firmware v{ver})")
        except Exception:
            self.log(f"CP2112 connected at {self.speed.get()} Hz")

    def disconnect(self):
        self.watching = False
        self.sn_live = False
        self.dv_live = False
        if self.dev:
            try:
                with self.lock:
                    self.dev.close()
            except Exception:
                pass
        self.dev = None
        self.status.config(text="Disconnected", foreground="#b71c1c")
        self.conn_btn.config(text="Connect")

    def apply_speed(self):
        hz = int(self.speed.get())
        self.bg(lambda d: d.configure(hz), lambda _: self.log(f"Bus speed set to {hz} Hz"))

    def addr(self):
        a = parse_num(self.addr_var.get())
        if not 0x03 <= a <= 0x77:
            raise ValueError("address must be 0x03-0x77")
        return a

    # ---------- scan tab ---------------------------------------------------
    def _build_scan(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  Scan  ")
        top = ttk.Frame(t)
        top.pack(fill="x")
        self.scan_btn = ttk.Button(top, text="Scan bus", command=self.scan)
        self.scan_btn.pack(side="left")
        ttk.Label(top, text="Click a found address to select it.").pack(side="left", padx=10)

        grid = ttk.Frame(t)
        grid.pack(pady=10)
        for c in range(16):
            ttk.Label(grid, text=f"{c:X}", width=4, anchor="center").grid(row=0, column=c + 1)
        self.cells = {}
        for r in range(8):
            ttk.Label(grid, text=f"{r:X}0", width=4).grid(row=r + 1, column=0)
            for c in range(16):
                a = r * 16 + c
                if a < 0x08 or a > 0x77:
                    continue
                b = tk.Label(grid, text="--", width=4, bg=EMPTY_BG, relief="groove", font=("Consolas", 10))
                b.grid(row=r + 1, column=c + 1, padx=1, pady=1)
                b.bind("<Button-1>", lambda e, a=a: self.pick(a))
                self.cells[a] = b

        self.found_list = tk.Listbox(t, height=8, font=("Consolas", 10))
        self.found_list.pack(fill="both", expand=True)
        self.found_list.bind("<<ListboxSelect>>", self._list_pick)
        self.found = []

    def scan(self):
        for a, b in self.cells.items():
            b.config(text="--", bg=EMPTY_BG, fg="black")
        self.found_list.delete(0, "end")
        self.scan_btn.config(state="disabled")

        def work(d):
            hits = []
            for a in range(0x08, 0x78):
                if a % 8 == 0:
                    self.ui(lambda a=a: self.scan_btn.config(text=f"Scanning 0x{a:02X}..."))
                try:
                    found = d.present(a)
                except CP2112Error as e:
                    raise CP2112Error(f"stopped at 0x{a:02X}: {e}")
                if found:
                    hits.append(a)
                    self.ui(lambda a=a: self.cells[a].config(text=f"{a:02X}", bg=FOUND_BG, fg="white"))
            return hits

        def done(hits):
            self.found = hits
            for a in hits:
                self.found_list.insert("end", f"0x{a:02X}   {KNOWN.get(a, 'unknown - look it up')}")
            self.log(f"Scan: {len(hits)} device(s) " + " ".join(f"0x{a:02X}" for a in hits) if hits
                     else "Scan: nothing found - check GND/VCC, SDA/SCL swap")
        def reset():
            self.scan_btn.config(state="normal", text="Scan bus")
        self.bg(work, done, "Scan", always=reset)
        if not self.dev:
            reset()

    def pick(self, a):
        if self.cells[a].cget("text") == "--":
            return
        self.addr_var.set(f"0x{a:02X}")
        for x, b in self.cells.items():
            if b.cget("text") != "--":
                b.config(bg=SEL_BG if x == a else FOUND_BG)
        self.log(f"Selected 0x{a:02X}")

    def _list_pick(self, _):
        sel = self.found_list.curselection()
        if sel:
            self.pick(self.found[sel[0]])

    # ---------- registers tab ----------------------------------------------
    def _build_regs(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  Registers  ")

        rd = ttk.LabelFrame(t, text="Read", padding=8)
        rd.pack(fill="x")
        ttk.Label(rd, text="Register").grid(row=0, column=0, sticky="w")
        self.r_reg = ttk.Entry(rd, width=8); self.r_reg.insert(0, "0xD0"); self.r_reg.grid(row=0, column=1, padx=4)
        ttk.Label(rd, text="Bytes").grid(row=0, column=2)
        self.r_n = ttk.Spinbox(rd, from_=1, to=256, width=5); self.r_n.set(1); self.r_n.grid(row=0, column=3, padx=4)
        self.r_wide = tk.BooleanVar()
        ttk.Checkbutton(rd, text="16-bit register address (big EEPROMs)", variable=self.r_wide).grid(row=0, column=4, padx=8)
        ttk.Button(rd, text="Read", command=self.do_read).grid(row=0, column=5, padx=4)
        ttk.Button(rd, text="Plain read (no register)", command=self.do_rawread).grid(row=0, column=6)
        self.r_out = ttk.Label(rd, text="", font=("Consolas", 11))
        self.r_out.grid(row=1, column=0, columnspan=7, sticky="w", pady=(8, 0))

        wr = ttk.LabelFrame(t, text="Write", padding=8)
        wr.pack(fill="x", pady=8)
        ttk.Label(wr, text="Register").grid(row=0, column=0)
        self.w_reg = ttk.Entry(wr, width=8); self.w_reg.insert(0, "0x00"); self.w_reg.grid(row=0, column=1, padx=4)
        ttk.Label(wr, text="Bytes (hex)").grid(row=0, column=2)
        self.w_data = ttk.Entry(wr, width=30); self.w_data.grid(row=0, column=3, padx=4)
        ttk.Button(wr, text="Write", command=self.do_write).grid(row=0, column=4)

        dp = ttk.LabelFrame(t, text="Dump 0x00-0xFF", padding=8)
        dp.pack(fill="both", expand=True)
        bar = ttk.Frame(dp); bar.pack(fill="x")
        ttk.Button(bar, text="Dump", command=self.do_dump).pack(side="left")
        ttk.Button(bar, text="Save dump...", command=self.save_dump).pack(side="left", padx=6)
        self.dump_box = tk.Text(dp, height=18, font=("Consolas", 10), state="disabled")
        self.dump_box.pack(fill="both", expand=True, pady=(6, 0))

    def _show_read(self, data):
        txt = hexs(data)
        if len(data) == 1:
            txt += f"     dec {data[0]}   bin {data[0]:08b}"
        elif len(data) == 2:
            be, le = (data[0] << 8) | data[1], (data[1] << 8) | data[0]
            txt += f"     BE {be}   LE {le}"
        self.r_out.config(text=txt)

    def _safe(self, fn):
        try:
            fn()
        except ValueError as e:
            messagebox.showwarning(APP, f"Check your input: {e}")

    def do_read(self):
        def go():
            a, reg, n = self.addr(), parse_num(self.r_reg.get()), int(self.r_n.get())
            w = 2 if self.r_wide.get() else 1

            def done(d):
                self._show_read(d)
                self.log(f"Read 0x{a:02X} reg 0x{reg:02X}: {hexs(d)}")
            self.bg(lambda dev: dev.read_reg(a, reg, n, w), done, "Read")
        self._safe(go)

    def do_rawread(self):
        def go():
            a, n = self.addr(), int(self.r_n.get())

            def done(d):
                self._show_read(d)
                self.log(f"Plain read 0x{a:02X}: {hexs(d)}")
            self.bg(lambda dev: dev.read(a, n), done, "Read")
        self._safe(go)

    def do_write(self):
        def go():
            a, reg, data = self.addr(), parse_num(self.w_reg.get()), parse_bytes(self.w_data.get())
            if not data:
                raise ValueError("enter at least one byte")
            if not messagebox.askyesno(APP, f"Write {hexs(data)} to 0x{a:02X} register 0x{reg:02X}?"):
                return
            self.bg(lambda dev: dev.write(a, [reg] + data),
                    lambda _: self.log(f"Wrote 0x{a:02X} reg 0x{reg:02X} <- {hexs(data)}"), "Write")
        self._safe(go)

    def do_dump(self):
        def go():
            a = self.addr()

            def work(dev):
                return [dev.read_reg(a, row, 16) for row in range(0, 0x100, 0x10)]

            def done(rows):
                lines = ["     " + " ".join(f"{c:02X}" for c in range(16))]
                for i, d in enumerate(rows):
                    asc = "".join(chr(b) if 32 <= b < 127 else "." for b in d)
                    lines.append(f"{i * 16:02X}:  {hexs(d)}  {asc}")
                self.dump_box.configure(state="normal")
                self.dump_box.delete("1.0", "end")
                self.dump_box.insert("1.0", "\n".join(lines))
                self.dump_box.configure(state="disabled")
                self.log(f"Dumped 0x{a:02X}")
            self.bg(work, done, "Dump")
        self._safe(go)

    def save_dump(self):
        txt = self.dump_box.get("1.0", "end").strip()
        if not txt:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt",
                                         initialfile=f"dump_{self.addr_var.get()}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        if p:
            with open(p, "w") as f:
                f.write(txt + "\n")
            self.log(f"Saved {p}")

    # ---------- watch tab --------------------------------------------------
    def _build_watch(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  Watch  ")
        bar = ttk.Frame(t); bar.pack(fill="x")
        ttk.Label(bar, text="Register").pack(side="left")
        self.wt_reg = ttk.Entry(bar, width=8); self.wt_reg.insert(0, "0x00"); self.wt_reg.pack(side="left", padx=4)
        ttk.Label(bar, text="Bytes").pack(side="left")
        self.wt_n = ttk.Spinbox(bar, from_=1, to=32, width=4); self.wt_n.set(2); self.wt_n.pack(side="left", padx=4)
        ttk.Label(bar, text="Every (s)").pack(side="left")
        self.wt_int = ttk.Spinbox(bar, from_=0.05, to=60, increment=0.05, width=6); self.wt_int.set(0.5)
        self.wt_int.pack(side="left", padx=4)
        self.wt_btn = ttk.Button(bar, text="Start", command=self.toggle_watch); self.wt_btn.pack(side="left", padx=8)
        ttk.Button(bar, text="Save CSV...", command=self.save_csv).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_watch).pack(side="left", padx=4)

        cols = ("time", "hex", "be", "le")
        self.wt_tree = ttk.Treeview(t, columns=cols, show="headings", height=16)
        for c, w, h in zip(cols, (100, 260, 120, 120), ("Time", "Raw bytes", "16-bit BE", "16-bit LE")):
            self.wt_tree.heading(c, text=h)
            self.wt_tree.column(c, width=w, anchor="w")
        self.wt_tree.pack(fill="both", expand=True, pady=(8, 0))

    def toggle_watch(self):
        if self.watching:
            self.watching = False
            self.wt_btn.config(text="Start")
            return
        try:
            a, reg, n, iv = (self.addr(), parse_num(self.wt_reg.get()),
                             int(self.wt_n.get()), float(self.wt_int.get()))
        except ValueError as e:
            messagebox.showwarning(APP, f"Check your input: {e}")
            return
        if not self.dev:
            self.log("Not connected.")
            return
        self.watching = True
        self.wt_btn.config(text="Stop")
        self.log(f"Watching 0x{a:02X} reg 0x{reg:02X} x{n} every {iv}s")

        def loop():
            while self.watching and self.dev:
                try:
                    with self.lock:
                        d = self.dev.read_reg(a, reg, n)
                    self.ui(lambda d=d: self._add_watch(d))
                except CP2112Error as e:
                    self.ui(lambda e=e: self.log(f"Watch error: {e}"))
                except Exception as e:
                    self.ui(lambda e=e: self._lost(e))
                    break
                time.sleep(iv)
            self.ui(lambda: self.wt_btn.config(text="Start"))
        threading.Thread(target=loop, daemon=True).start()

    def _add_watch(self, d):
        ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        be = le = ""
        if len(d) >= 2:
            be, le = (d[0] << 8) | d[1], (d[1] << 8) | d[0]
        row = (ts, hexs(d), be, le)
        self.watch_rows.append(row)
        self.wt_tree.insert("", "end", values=row)
        kids = self.wt_tree.get_children()
        if len(kids) > 2000:
            self.wt_tree.delete(kids[0])
        self.wt_tree.see(self.wt_tree.get_children()[-1])

    def clear_watch(self):
        self.watch_rows.clear()
        self.wt_tree.delete(*self.wt_tree.get_children())

    def save_csv(self):
        if not self.watch_rows:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         initialfile=f"watch_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if p:
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "raw_hex", "u16_be", "u16_le"])
                w.writerows(self.watch_rows)
            self.log(f"Saved {len(self.watch_rows)} rows to {p}")

    # ---------- gpio tab ---------------------------------------------------
    def _build_gpio(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  GPIO  ")
        ttk.Label(t, text="Pins IO5, IO6 and IO7 on the back of the board (push-pull outputs).").pack(anchor="w")
        row = ttk.Frame(t); row.pack(anchor="w", pady=12)
        self.gpio_btns = {}
        for pin in (5, 6, 7):
            b = tk.Button(row, text=f"IO{pin}\nOFF", width=10, height=3, bg=EMPTY_BG,
                          command=lambda p=pin: self.toggle_gpio(p))
            b.pack(side="left", padx=6)
            self.gpio_btns[pin] = [b, False]

    def toggle_gpio(self, pin):
        b, state = self.gpio_btns[pin]
        new = not state

        def done(_):
            self.gpio_btns[pin][1] = new
            b.config(text=f"IO{pin}\n{'ON' if new else 'OFF'}", bg=FOUND_BG if new else EMPTY_BG,
                     fg="white" if new else "black")
            self.log(f"IO{pin} -> {'ON' if new else 'OFF'}")
        self.bg(lambda d: d.gpio_set(pin, new), done, "GPIO")

    # ---------- OLED tab ---------------------------------------------------
    def _build_oled(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  OLED  ")
        cfg = ttk.Frame(t); cfg.pack(fill="x")
        ttk.Label(cfg, text="Controller").pack(side="left")
        self.ol_ctrl = ttk.Combobox(cfg, values=["SSD1306", "SH1106"], width=9, state="readonly")
        self.ol_ctrl.set("SSD1306"); self.ol_ctrl.pack(side="left", padx=4)
        ttk.Label(cfg, text="Size").pack(side="left", padx=(10, 0))
        self.ol_size = ttk.Combobox(cfg, values=["128x64", "128x32"], width=7, state="readonly")
        self.ol_size.set("128x64"); self.ol_size.pack(side="left", padx=4)
        ttk.Button(cfg, text="Init display", command=self.ol_init).pack(side="left", padx=(12, 4))
        ttk.Button(cfg, text="Clear", command=lambda: self.ol_send(None)).pack(side="left")
        ttk.Button(cfg, text="Test pattern", command=lambda: self.ol_send("test")).pack(side="left", padx=4)
        ttk.Label(t, text="0.96\" boards are usually SSD1306, 1.3\" usually SH1106. "
                          "If the picture is shifted 2 pixels or garbled, switch controller.",
                  foreground="#666").pack(anchor="w", pady=(4, 0))

        mid = ttk.Frame(t); mid.pack(fill="both", expand=True, pady=8)
        left = ttk.Frame(mid); left.pack(side="left", fill="both", expand=True)
        self.ol_text = tk.Text(left, height=6, width=30, font=("Consolas", 11))
        self.ol_text.insert("1.0", "Hello\nI2C Poke\nCP2112 OK")
        self.ol_text.pack(fill="both", expand=True)
        row = ttk.Frame(left); row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Font size").pack(side="left")
        self.ol_font = ttk.Spinbox(row, from_=8, to=48, width=4); self.ol_font.set(12)
        self.ol_font.pack(side="left", padx=4)
        ttk.Button(row, text="Send text", command=lambda: self.ol_send("text")).pack(side="left", padx=6)
        ttk.Button(row, text="Load image...", command=self.ol_image).pack(side="left")

        right = ttk.Frame(mid); right.pack(side="left", padx=(12, 0))
        ttk.Label(right, text="Preview").pack(anchor="w")
        self.ol_canvas = tk.Canvas(right, width=128 * 3 + 4, height=64 * 3 + 4, bg="#111", highlightthickness=0)
        self.ol_canvas.pack()
        self.ol_photo = None

        ctl = ttk.Frame(t); ctl.pack(fill="x")
        ttk.Label(ctl, text="Contrast").pack(side="left")
        self.ol_con = tk.Scale(ctl, from_=0, to=255, orient="horizontal", length=200, showvalue=True,
                               command=lambda v: self._ol_contrast_later())
        self.ol_con.set(207); self.ol_con.pack(side="left", padx=4)
        self.ol_inv = tk.BooleanVar()
        ttk.Checkbutton(ctl, text="Invert", variable=self.ol_inv,
                        command=lambda: self._ol_do(lambda o: o.invert(self.ol_inv.get()), "Invert")).pack(side="left", padx=10)
        self.ol_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(ctl, text="Display on", variable=self.ol_on,
                        command=lambda: self._ol_do(lambda o: o.power(self.ol_on.get()), "Power")).pack(side="left")
        self._con_job = None

    def _ol(self, dev):
        w, h = (int(v) for v in self.ol_size.get().split("x"))
        return oled.OLED(dev, self.addr(), self.ol_ctrl.get(), w, h)

    def _ol_do(self, fn, label):
        try:
            self.addr()
        except ValueError as e:
            messagebox.showwarning(APP, f"Check the device address: {e}")
            return
        self.bg(lambda d: fn(self._ol(d)), lambda _: None, f"OLED {label}")

    def _ol_contrast_later(self):
        if self._con_job:
            self.after_cancel(self._con_job)
        self._con_job = self.after(150, lambda: self._ol_do(lambda o: o.contrast(self.ol_con.get()), "contrast"))

    def ol_init(self):
        def done(_):
            self.log(f"OLED {self.ol_ctrl.get()} {self.ol_size.get()} initialised at {self.addr_var.get()}")
            self._preview(None)
        try:
            self.addr()
        except ValueError as e:
            messagebox.showwarning(APP, f"Check the device address: {e}")
            return
        self.bg(lambda d: self._ol(d).init(self.ol_con.get()), done, "OLED init")

    def ol_image(self):
        p = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")])
        if p:
            self.ol_send(("image", p))

    def ol_send(self, what):
        w, h = (int(v) for v in self.ol_size.get().split("x"))
        try:
            if what is None:
                img = oled.Image.new("1", (w, h), 0)
            elif what == "test":
                img = oled.test_pattern(w, h)
            elif what == "text":
                img = oled.render_text(self.ol_text.get("1.0", "end").rstrip("\n"), w, h, int(self.ol_font.get()))
            else:
                img = oled.render_image(what[1], w, h)
        except Exception as e:
            messagebox.showwarning(APP, f"Couldn't prepare that: {e}")
            return
        self._preview(img)
        t0 = time.time()
        self.bg(lambda d: self._ol(d).show(img),
                lambda _: self.log(f"OLED updated in {time.time() - t0:.2f}s"), "OLED")

    def _preview(self, img):
        c = self.ol_canvas
        c.delete("all")
        if img is None:
            return
        w, h = img.size
        px = img.load()
        rows = []
        for y in range(h):
            rows.append("{" + " ".join("#7fd4ff" if px[x, y] else "#111111" for x in range(w)) + "}")
        base = tk.PhotoImage(width=w, height=h)
        base.put(" ".join(rows))
        self.ol_photo = base.zoom(3)
        c.create_image(2, 2, image=self.ol_photo, anchor="nw")

    # ---------- LCD tab ----------------------------------------------------
    def _build_lcd(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  LCD  ")
        cfg = ttk.Frame(t); cfg.pack(fill="x")
        ttk.Label(cfg, text="Size").pack(side="left")
        self.lc_size = ttk.Combobox(cfg, values=["16x2", "20x4"], width=6, state="readonly")
        self.lc_size.set("16x2"); self.lc_size.pack(side="left", padx=4)
        self.lc_size.bind("<<ComboboxSelected>>", lambda e: self._lc_rows())
        ttk.Button(cfg, text="Init LCD", command=self.lc_init).pack(side="left", padx=(12, 4))
        ttk.Button(cfg, text="Clear", command=lambda: self._lc_do(lambda l: l.clear(), "clear")).pack(side="left")
        self.lc_bl = tk.BooleanVar(value=True)
        ttk.Checkbutton(cfg, text="Backlight", variable=self.lc_bl,
                        command=lambda: self._lc_do(lambda l: l.backlight(self.lc_bl.get()), "backlight")).pack(side="left", padx=12)
        ttk.Label(t, text="Character LCD on a PCF8574 backpack (usually 0x27 or 0x3F - pick it on the Scan tab). "
                          "Needs 5 V on VCC. Blank or solid blocks = turn the blue contrast pot.",
                  foreground="#666", wraplength=760, justify="left").pack(anchor="w", pady=(4, 8))
        lines = ttk.Frame(t); lines.pack(anchor="w")
        self.lc_entries = []
        defaults = ["Hello", "I2C Poke LCD OK", "CP2112 bench", "tester"]
        for i in range(4):
            ttk.Label(lines, text=f"Line {i + 1}").grid(row=i, column=0, sticky="w", pady=2)
            e = ttk.Entry(lines, width=24, font=("Consolas", 12))
            e.insert(0, defaults[i])
            e.grid(row=i, column=1, padx=6, pady=2)
            self.lc_entries.append(e)
        ttk.Button(t, text="Send text", command=self.lc_send).pack(anchor="w", pady=8)
        self._lc_rows()

    def _lc_dims(self):
        c, r = (int(v) for v in self.lc_size.get().split("x"))
        return c, r

    def _lc_rows(self):
        cols, rows = self._lc_dims()
        for i, e in enumerate(self.lc_entries):
            e.configure(state="normal" if i < rows else "disabled", width=cols + 4)

    def _lc(self, dev):
        cols, rows = self._lc_dims()
        l = lcd.LCD(dev, self.addr(), cols, rows)
        l.bl = lcd.BL if self.lc_bl.get() else 0
        return l

    def _lc_do(self, fn, label, done=None):
        try:
            self.addr()
        except ValueError as e:
            messagebox.showwarning(APP, f"Check the device address: {e}")
            return
        self.bg(lambda d: fn(self._lc(d)), done or (lambda _: None), f"LCD {label}")

    def lc_init(self):
        self._lc_do(lambda l: l.init(), "init",
                    lambda _: self.log(f"LCD {self.lc_size.get()} initialised at {self.addr_var.get()}"))

    def lc_send(self):
        cols, rows = self._lc_dims()
        text = [e.get() for e in self.lc_entries[:rows]]
        self._lc_do(lambda l: l.show(text), "text",
                    lambda _: self.log("LCD: " + " | ".join(x[:cols] for x in text)))

    # ---------- Sensors tab ------------------------------------------------
    def _build_sensors(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  Sensors  ")
        bar = ttk.Frame(t); bar.pack(fill="x")
        ttk.Button(bar, text="Detect sensors", command=self.sn_detect).pack(side="left")
        ttk.Button(bar, text="Read now", command=self.sn_read).pack(side="left", padx=6)
        self.sn_live_btn = ttk.Button(bar, text="Start live", command=self.sn_toggle_live)
        self.sn_live_btn.pack(side="left")
        ttk.Label(bar, text="every").pack(side="left", padx=(8, 2))
        self.sn_int = ttk.Spinbox(bar, from_=0.5, to=600, increment=0.5, width=5); self.sn_int.set(2)
        self.sn_int.pack(side="left")
        ttk.Label(bar, text="s").pack(side="left", padx=(2, 12))
        ttk.Button(bar, text="Save CSV...", command=self.sn_save).pack(side="left")
        ttk.Label(t, text="Supports AHT10/AHT20 (0x38), BMP280 and BME280 (0x76/0x77) - e.g. the AHT20+BMP280 combo board.",
                  foreground="#666").pack(anchor="w", pady=(4, 10))
        self.sn_big = ttk.Label(t, text="--", font=("Segoe UI", 26))
        self.sn_big.pack(anchor="w")
        self.sn_extra = ttk.Label(t, text="", font=("Segoe UI", 12), foreground="#555")
        self.sn_extra.pack(anchor="w", pady=(2, 10))
        self.sn_detail = ttk.Label(t, text="Press Detect sensors.", font=("Consolas", 11), justify="left")
        self.sn_detail.pack(anchor="w")
        self.sn_drivers, self.sn_rows, self.sn_live = [], [], False

    def sn_detect(self):
        def done(found):
            self.sn_drivers = found
            if found:
                self.sn_detail.config(text="\n".join(f"{d.name:7} at 0x{d.addr:02X}" for d in found))
                self.log("Sensors: " + ", ".join(f"{d.name} @0x{d.addr:02X}" for d in found))
                self.sn_read()
            else:
                self.sn_detail.config(text="No supported sensors found.")
                self.log("Sensors: none found")
        self.bg(sensors.detect, done, "Detect")

    def _sn_read_all(self, dev):
        return [(d, d.read()) for d in self.sn_drivers]

    def _sn_show(self, results):
        temps = [r["temp_c"] for _, r in results]
        hums = [r["humidity"] for _, r in results if "humidity" in r]
        press = [r["pressure_hpa"] for _, r in results if "pressure_hpa" in r]
        parts = []
        if temps:
            parts.append(f"{sum(temps) / len(temps):.1f} \u00b0C")
        if hums:
            parts.append(f"{hums[0]:.0f} %RH")
        if press:
            parts.append(f"{press[0]:.1f} hPa")
        self.sn_big.config(text="    ".join(parts) or "--")
        extra = []
        if temps and hums:
            dp = sensors.dew_point(temps[0], hums[0])
            if dp is not None:
                extra.append(f"Dew point {dp:.1f} \u00b0C")
        if press:
            extra.append(f"Approx. altitude {sensors.altitude_m(press[0]):.0f} m (vs 1013.25 hPa)")
        self.sn_extra.config(text="     ".join(extra))
        lines = []
        for d, r in results:
            vals = [f"{r['temp_c']:6.2f} \u00b0C"]
            if "humidity" in r:
                vals.append(f"{r['humidity']:5.1f} %RH")
            if "pressure_hpa" in r:
                vals.append(f"{r['pressure_hpa']:7.2f} hPa")
            lines.append(f"{d.name:7} 0x{d.addr:02X}   " + "   ".join(vals))
        self.sn_detail.config(text="\n".join(lines))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for d, r in results:
            self.sn_rows.append((ts, d.name, f"0x{d.addr:02X}", f"{r['temp_c']:.2f}",
                                 f"{r['humidity']:.1f}" if "humidity" in r else "",
                                 f"{r['pressure_hpa']:.2f}" if "pressure_hpa" in r else ""))

    def sn_read(self):
        if not self.sn_drivers:
            self.log("Sensors: press Detect sensors first")
            return
        self.bg(self._sn_read_all, self._sn_show, "Sensor read")

    def sn_toggle_live(self):
        if self.sn_live:
            self.sn_live = False
            self.sn_live_btn.config(text="Start live")
            return
        if not self.sn_drivers or not self.dev:
            self.log("Sensors: connect and press Detect sensors first")
            return
        try:
            iv = max(0.5, float(self.sn_int.get()))
        except ValueError:
            iv = 2.0
        self.sn_live = True
        self.sn_live_btn.config(text="Stop live")

        def loop():
            while self.sn_live and self.dev:
                try:
                    with self.lock:
                        res = self._sn_read_all(self.dev)
                    self.ui(lambda res=res: self._sn_show(res))
                except CP2112Error as e:
                    self.ui(lambda e=e: self.log(f"Sensor error: {e}"))
                except Exception as e:
                    self.ui(lambda e=e: self._lost(e))
                    break
                time.sleep(iv)
            self.ui(lambda: self.sn_live_btn.config(text="Start live"))
        threading.Thread(target=loop, daemon=True).start()

    def sn_save(self):
        if not self.sn_rows:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         initialfile=f"sensors_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if p:
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "sensor", "address", "temp_c", "humidity_pct", "pressure_hpa"])
                w.writerows(self.sn_rows)
            self.log(f"Saved {len(self.sn_rows)} sensor rows to {p}")

    # ---------- Devices tab ------------------------------------------------
    DV_ERRORS = (CP2112Error, ValueError, ZeroDivisionError, IndexError, KeyError, TypeError, OverflowError)

    def _build_devices(self, nb):
        t = ttk.Frame(nb, padding=10)
        nb.add(t, text="  Devices  ")
        bar = ttk.Frame(t); bar.pack(fill="x")
        self.dv_id_btn = ttk.Button(bar, text="Identify devices", command=self.dv_identify)
        self.dv_id_btn.pack(side="left")
        ttk.Button(bar, text="Read all", command=self.dv_read_all).pack(side="left", padx=6)
        self.dv_live_btn = ttk.Button(bar, text="Start live", command=self.dv_toggle_live)
        self.dv_live_btn.pack(side="left")
        ttk.Label(bar, text="every").pack(side="left", padx=(8, 2))
        self.dv_int = ttk.Spinbox(bar, from_=0.5, to=600, increment=0.5, width=5); self.dv_int.set(2)
        self.dv_int.pack(side="left")
        ttk.Label(bar, text="s").pack(side="left", padx=(2, 12))
        ttk.Button(bar, text="Save CSV...", command=self.dv_save).pack(side="left")
        ttk.Button(bar, text="Supported parts", command=self.dv_supported).pack(side="right")

        cols = ("addr", "device", "values")
        self.dv_tree = ttk.Treeview(t, columns=cols, show="headings", height=7, selectmode="browse")
        for c, w, h in zip(cols, (70, 210, 520), ("Address", "Device", "Readings")):
            self.dv_tree.heading(c, text=h)
            self.dv_tree.column(c, width=w, anchor="w", stretch=(c == "values"))
        self.dv_tree.pack(fill="x", pady=(8, 6))
        self.dv_tree.bind("<<TreeviewSelect>>", lambda e: self.dv_show_detail())

        self.dv_detail = ttk.LabelFrame(t, text="Device", padding=8)
        self.dv_detail.pack(fill="both", expand=True)
        top = ttk.Frame(self.dv_detail); top.pack(fill="x")
        ttk.Label(top, text="Treat as").pack(side="left")
        self.dv_as = ttk.Combobox(top, width=34, state="readonly")
        self.dv_as.pack(side="left", padx=4)
        self.dv_as.bind("<<ComboboxSelected>>", lambda e: self.dv_change_driver())
        ttk.Button(top, text="Read", command=lambda: self.dv_read_all(only=self.dv_sel())).pack(side="left", padx=6)
        self.dv_note = ttk.Label(self.dv_detail, text="Press Identify devices.", foreground="#666",
                                 wraplength=780, justify="left")
        self.dv_note.pack(anchor="w", pady=(6, 4))
        self.dv_settings = ttk.Frame(self.dv_detail); self.dv_settings.pack(fill="x")
        self.dv_actions = ttk.Frame(self.dv_detail); self.dv_actions.pack(fill="x", pady=4)
        self.dv_values = ttk.Label(self.dv_detail, text="", font=("Consolas", 11), justify="left")
        self.dv_values.pack(anchor="w", pady=(4, 0))
        self.dv = {}            # addr -> {"drv": driver, "cands": [classes], "vals": [...], "err": str}
        self.dv_rows, self.dv_live = [], False

    def dv_sel(self):
        s = self.dv_tree.selection()
        return int(s[0], 16) if s else None

    def dv_identify(self):
        self.dv_id_btn.config(state="disabled", text="Identifying...")

        def work(d):
            found = []
            for a in range(0x08, 0x78):
                if d.present(a):
                    found.append(a)
            res = {}
            for a in found:
                cls, cands = devices.identify(d, a)
                res[a] = {"drv": cls(d, a), "cands": cands, "vals": [], "err": ""}
            return res

        def done(res):
            self.dv = res
            self.dv_tree.delete(*self.dv_tree.get_children())
            for a, e in sorted(res.items()):
                self.dv_tree.insert("", "end", iid=f"{a:02X}", values=(f"0x{a:02X}", e["drv"].name, ""))
            self.log("Devices: " + (", ".join(f"0x{a:02X} {e['drv'].name}" for a, e in sorted(res.items()))
                                    or "nothing found"))
            if res:
                first = f"{min(res):02X}"
                self.dv_tree.selection_set(first)
                self.dv_read_all()

        def reset():
            self.dv_id_btn.config(state="normal", text="Identify devices")
        self.bg(work, done, "Identify", always=reset)
        if not self.dev:
            reset()

    def _dv_read_one(self, a):
        e = self.dv[a]
        try:
            e["drv"].ensure()
            e["vals"], e["err"] = e["drv"].read(), ""
        except self.DV_ERRORS as ex:
            e["err"] = str(ex) or ex.__class__.__name__
            e["drv"].ready = False

    def dv_read_all(self, only=None):
        if not self.dv:
            self.log("Devices: press Identify devices first")
            return
        targets = [only] if only is not None else sorted(self.dv)

        def work(d):
            for a in targets:
                self._dv_read_one(a)
            return targets
        self.bg(work, self._dv_refresh, "Devices")

    @staticmethod
    def _fmt(v, unit):
        if isinstance(v, float):
            v = f"{v:.3f}" if abs(v) < 10 else (f"{v:.2f}" if abs(v) < 1000 else f"{v:.1f}")
        return f"{v} {unit}".strip()

    def _dv_refresh(self, addrs):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for a in addrs:
            e = self.dv.get(a)
            if not e:
                continue
            summary = e["err"] and f"error: {e['err']}" or ",  ".join(
                f"{lab} {self._fmt(v, u)}" for lab, v, u in e["vals"][:4])
            self.dv_tree.item(f"{a:02X}", values=(f"0x{a:02X}", e["drv"].name, summary))
            for lab, v, u in e["vals"]:
                self.dv_rows.append((ts, f"0x{a:02X}", e["drv"].name, lab, v, u))
        self.dv_show_detail(refresh_only=True)

    def dv_show_detail(self, refresh_only=False):
        a = self.dv_sel()
        if a is None or a not in self.dv:
            return
        e = self.dv[a]; drv = e["drv"]
        self.dv_detail.config(text=f"0x{a:02X}  {drv.name}  ({drv.category})")
        lines = [f"{lab:<18}{self._fmt(v, u)}" for lab, v, u in e["vals"]]
        if e["err"]:
            lines.append(f"Error: {e['err']}")
        self.dv_values.config(text="\n".join(lines) or "(not read yet)")
        if refresh_only and getattr(self, "_dv_shown", None) == (a, drv.__class__):
            return
        self._dv_shown = (a, drv.__class__)
        names = [c.name for c in e["cands"]]
        self.dv_as.configure(values=names)
        self.dv_as.set(drv.__class__.name)
        self.dv_note.config(text=drv.note or "")
        for w in self.dv_settings.winfo_children() + self.dv_actions.winfo_children():
            w.destroy()
        self.dv_set_vars = {}
        for i, (k, v) in enumerate(drv.opts.items()):
            ttk.Label(self.dv_settings, text=k).grid(row=i, column=0, sticky="w")
            ent = ttk.Entry(self.dv_settings, width=10); ent.insert(0, str(v))
            ent.grid(row=i, column=1, padx=6, pady=1, sticky="w")
            self.dv_set_vars[k] = ent
        if drv.opts:
            ttk.Button(self.dv_settings, text="Apply settings", command=self.dv_apply_settings).grid(
                row=0, column=2, rowspan=max(1, len(drv.opts)), padx=8)
        for i, (label, meth, params) in enumerate(drv.actions):
            row = ttk.Frame(self.dv_actions); row.pack(fill="x", pady=1)
            ents = {}
            for plabel, default in params:
                ttk.Label(row, text=plabel).pack(side="left")
                en = ttk.Entry(row, width=8); en.insert(0, default); en.pack(side="left", padx=(2, 8))
                ents[plabel] = en
            ttk.Button(row, text=label, command=lambda m=meth, en=ents: self.dv_action(m, en)).pack(side="left")

    def dv_change_driver(self):
        a = self.dv_sel()
        if a is None:
            return
        e = self.dv[a]
        cls = next(c for c in e["cands"] if c.name == self.dv_as.get())
        e["drv"] = cls(self.dev, a, e["drv"].opts)
        e["vals"], e["err"] = [], ""
        self._dv_shown = None
        self.dv_show_detail()
        self.log(f"0x{a:02X} now treated as {cls.name}")
        self.dv_read_all(only=a)

    def dv_apply_settings(self):
        a = self.dv_sel()
        drv = self.dv[a]["drv"]
        for k, ent in self.dv_set_vars.items():
            try:
                drv.opts[k] = float(ent.get())
            except ValueError:
                messagebox.showwarning(APP, f"'{k}' must be a number")
                return
        drv.ready = False
        self.log(f"0x{a:02X} settings updated")
        self.dv_read_all(only=a)

    def dv_action(self, meth, ents):
        a = self.dv_sel()
        drv = self.dv[a]["drv"]
        params = {k: en.get() for k, en in ents.items()}

        def work(d):
            drv.ensure()
            try:
                return getattr(drv, meth)(params)
            except (ValueError, KeyError) as ex:
                raise CP2112Error(f"bad value: {ex}")
        self.bg(work, lambda msg: (self.log(f"0x{a:02X}: {msg}"), self.dv_read_all(only=a)), drv.name)

    def dv_toggle_live(self):
        if self.dv_live:
            self.dv_live = False
            self.dv_live_btn.config(text="Start live")
            return
        if not self.dv or not self.dev:
            self.log("Devices: connect and press Identify devices first")
            return
        try:
            iv = max(0.5, float(self.dv_int.get()))
        except ValueError:
            iv = 2.0
        self.dv_live = True
        self.dv_live_btn.config(text="Stop live")

        def loop():
            while self.dv_live and self.dev:
                try:
                    with self.lock:
                        for a in sorted(self.dv):
                            self._dv_read_one(a)
                    self.ui(lambda: self._dv_refresh(sorted(self.dv)))
                except Exception as e:
                    self.ui(lambda e=e: self._lost(e))
                    break
                time.sleep(iv)
            self.ui(lambda: self.dv_live_btn.config(text="Start live"))
        threading.Thread(target=loop, daemon=True).start()

    def dv_save(self):
        if not self.dv_rows:
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         initialfile=f"devices_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if p:
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["time", "address", "device", "reading", "value", "unit"])
                w.writerows(self.dv_rows)
            self.log(f"Saved {len(self.dv_rows)} rows to {p}")

    def dv_supported(self):
        by = {}
        for c in devices.REGISTRY:
            by.setdefault(c.category, []).append(f"{c.name} ({', '.join(f'0x{a:02X}' for a in (c.addrs[:2]))}"
                                                 f"{'...' if len(c.addrs) > 2 else ''})")
        txt = "\n\n".join(f"{cat}:\n  " + "\n  ".join(v) for cat, v in sorted(by.items()))
        messagebox.showinfo(APP, txt)

    # ---------- log -------------------------------------------------------
    def _build_log(self):
        self.logbox = tk.Text(self, height=6, font=("Consolas", 9), state="disabled", bg="#fafafa")
        self.logbox.pack(fill="x", padx=8, pady=(0, 8))

    def _quit(self):
        self.disconnect()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
