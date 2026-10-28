#!/usr/bin/env python3
"""
CP2112 board enclosure generator  (python3 cp2112_case.py)

Two parts: a base with standoffs/pegs, a micro-USB cut-out at one end and a
pin slot at the other; and a press-fit lid with an LED window and clamp tubes
that hold the board down.  Writes STL files plus a 3MF with both parts.

All dimensions are in mm.  Board numbers were measured from photos - check them
with calipers and edit the BOARD section if your board differs.

Model is built from overlapping closed solids (boxes, cylinders, extrusions);
slicers union these automatically (the 3MF keeps them as parts of one object).
"""
import math
import struct
import zipfile
import numpy as np

# ---------------- BOARD (CP2112 purple breakout) -----------------------------
BX, BY, PCB_T = 29.3, 20.9, 1.6          # board length (USB->pins), width, thickness
HOLES = [(2.4, 2.9), (2.4, 18.6)]        # mounting hole centres (USB end)
PEG_D = 2.0                              # peg through mounting hole
USB_Y = 10.7                             # micro-USB centre across the board
USB_W, USB_H = 11.5, 7.5                 # opening for the plug's overmould
USB_Z = 1.4                              # USB centre height above PCB top
LED_X, LED_Y0, LED_Y1 = 16.0, 14.9, 16.3 # the two activity LEDs
PIN_Y0, PIN_Y1 = 0.6, 20.3               # pin slot span across the board
PIN_Z0, PIN_Z1 = -0.8, 6.4               # pin slot heights relative to PCB top

# ---------------- CASE -------------------------------------------------------
T = 1.8            # wall thickness
FLOOR = 1.8
LID_T = 1.8
R = 2.5            # outer corner radius
CLR = 0.4          # side clearance around board
CLR_PINS = 1.0     # extra room at the header end
STANDOFF_H = 3.0   # board underside clearance (pin tails)
HEADROOM = 8.0     # space above PCB top (right-angle header fits)
STANDOFF_D = 4.5
TUBE_OD, TUBE_ID = 4.0, 2.6
LIP_H, LIP_W, LIP_GAP = 1.5, 1.2, 0.15
SEG = 64

# derived
Z_PCB_BOT = FLOOR + STANDOFF_H
Z_PCB_TOP = Z_PCB_BOT + PCB_T
Z_TOP = Z_PCB_TOP + HEADROOM            # top of walls / underside of lid
CX0, CX1 = -CLR, BX + CLR_PINS          # cavity
CY0, CY1 = -CLR, BY + CLR
X0, X1, Y0, Y1 = CX0 - T, CX1 + T, CY0 - T, CY1 + T


# ---------------- tiny mesh kernel ------------------------------------------
def ccw(pts):
    pts = [tuple(p) for p in pts]
    a = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
    return pts if a > 0 else pts[::-1]


def hull(pts):
    pts = sorted(set((round(x, 6), round(y, 6)) for x, y in pts))
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    return lo[:-1] + hi[:-1]


def circle(cx, cy, d, n=SEG):
    return [(cx + d / 2 * math.cos(2 * math.pi * i / n), cy + d / 2 * math.sin(2 * math.pi * i / n))
            for i in range(n)]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def rrect(x0, y0, x1, y1, r, n=12):
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def stadium(cx, y0, y1, d, n=16):
    pts = []
    r = d / 2
    for cy, a0 in ((y1, 0), (y0, 180)):
        for i in range(n + 1):
            a = math.radians(a0 + 180 * i / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def extrude(outer, z0, z1, hole=None):
    """Closed solid from a convex outline (optionally minus one convex hole)."""
    outer = ccw(outer)
    tris = []
    n = len(outer)
    for i in range(n):  # outer wall
        a, b = outer[i], outer[(i + 1) % n]
        A0, B0, B1, A1 = (*a, z0), (*b, z0), (*b, z1), (*a, z1)
        tris += [(A0, B0, B1), (A0, B1, A1)]
    if hole is None:
        c = (sum(p[0] for p in outer) / n, sum(p[1] for p in outer) / n)
        for i in range(n):
            a, b = outer[i], outer[(i + 1) % n]
            tris.append(((*c, z1), (*a, z1), (*b, z1)))
            tris.append(((*c, z0), (*b, z0), (*a, z0)))
        return tris
    hole = ccw(hole)
    m = len(hole)
    for i in range(m):  # hole wall, facing inwards
        a, b = hole[i], hole[(i + 1) % m]
        A0, B0, B1, A1 = (*a, z0), (*b, z0), (*b, z1), (*a, z1)
        tris += [(B0, A0, A1), (B0, A1, B1)]
    # caps: zip the two loops by angle around the hole centre
    hc = (sum(p[0] for p in hole) / m, sum(p[1] for p in hole) / m)
    ang = lambda p: math.atan2(p[1] - hc[1], p[0] - hc[0]) % (2 * math.pi)
    o = outer[min(range(n), key=lambda k: ang(outer[k])):] + outer[:min(range(n), key=lambda k: ang(outer[k]))]
    h = hole[min(range(m), key=lambda k: ang(hole[k])):] + hole[:min(range(m), key=lambda k: ang(hole[k]))]
    oa = [ang(p) for p in o] + [ang(o[0]) + 2 * math.pi]
    ha = [ang(p) for p in h] + [ang(h[0]) + 2 * math.pi]
    i = j = 0
    while i < n or j < m:
        if j == m or (i < n and oa[i + 1] <= ha[j + 1]):
            p, q, r_ = o[i], o[(i + 1) % n], h[j % m]
            i += 1
        else:
            p, q, r_ = o[i % n], h[(j + 1) % m], h[j]
            j += 1
        tris.append(((*p, z1), (*q, z1), (*r_, z1)))
        tris.append(((*p, z0), (*r_, z0), (*q, z0)))
    return tris


def box(x0, y0, z0, x1, y1, z1):
    return extrude(rect(x0, y0, x1, y1), z0, z1)


def volume(tris):
    v = 0.0
    for a, b, c in tris:
        v += np.dot(a, np.cross(b, c)) / 6
    return v


# ---------------- parts ------------------------------------------------------
def wall_end(x0, x1, yo0, yo1, zo0, zo1):
    """End wall from Y0+R..Y1-R, floor..top, with a rectangular opening."""
    ya, yb, za, zb = Y0 + R, Y1 - R, FLOOR, Z_TOP
    parts = []
    zo0, zo1 = max(zo0, za), min(zo1, zb)
    yo0, yo1 = max(yo0, ya), min(yo1, yb)
    if zo0 > za:
        parts.append(box(x0, ya, za, x1, yb, zo0))
    if zb > zo1:
        parts.append(box(x0, ya, zo1, x1, yb, zb))
    if yo0 > ya:
        parts.append(box(x0, ya, zo0, x1, yo0, zo1))
    if yb > yo1:
        parts.append(box(x0, yo1, zo0, x1, yb, zo1))
    return parts


def corner(cx, cy, sx, sy):
    """Rounded outer corner piece; (cx,cy) = outer corner, (sx,sy) = inward signs."""
    arc = []
    ox, oy = cx + sx * R, cy + sy * R
    for i in range(17):
        a = math.pi / 2 * i / 16
        arc.append((ox - sx * R * math.cos(a), oy - sy * R * math.sin(a)))
    pts = arc + [(cx + sx * R, cy + sy * T), (cx + sx * T, cy + sy * R), (cx + sx * T, cy + sy * T)]
    return extrude(hull(pts), FLOOR, Z_TOP)


def base():
    p = [extrude(rrect(X0, Y0, X1, Y1, R), 0, FLOOR)]
    p.append(box(X0 + R, Y0, FLOOR, X1 - R, Y0 + T, Z_TOP))          # long walls
    p.append(box(X0 + R, Y1 - T, FLOOR, X1 - R, Y1, Z_TOP))
    for cx, cy, sx, sy in ((X0, Y0, 1, 1), (X1, Y0, -1, 1), (X1, Y1, -1, -1), (X0, Y1, 1, -1)):
        p.append(corner(cx, cy, sx, sy))
    uz = Z_PCB_TOP + USB_Z                                           # USB end
    p += wall_end(X0, X0 + T, USB_Y - USB_W / 2, USB_Y + USB_W / 2, uz - USB_H / 2, uz + USB_H / 2)
    p += wall_end(X1 - T, X1, PIN_Y0, PIN_Y1, Z_PCB_TOP + PIN_Z0, Z_PCB_TOP + PIN_Z1)  # pin end
    for hx, hy in HOLES:
        p.append(extrude(circle(hx, hy, STANDOFF_D), FLOOR, Z_PCB_BOT))
        p.append(extrude(circle(hx, hy, PEG_D), Z_PCB_BOT - 0.2, Z_PCB_TOP + 1.2))
    return p


def lid():
    p = [extrude(rrect(X0, Y0, X1, Y1, R), Z_TOP, Z_TOP + LID_T,
                 hole=stadium(LED_X, LED_Y0, LED_Y1, 1.8))]
    g = LIP_GAP
    p.append(extrude(rrect(CX0 + g, CY0 + g, CX1 - g, CY1 - g, 1.0), Z_TOP - LIP_H, Z_TOP + 0.2,
                     hole=rrect(CX0 + g + LIP_W, CY0 + g + LIP_W, CX1 - g - LIP_W, CY1 - g - LIP_W, 0.5)))
    for hx, hy in HOLES:
        p.append(extrude(circle(hx, hy, TUBE_OD), Z_PCB_TOP + 0.05, Z_TOP + 0.2,
                         hole=circle(hx, hy, TUBE_ID)))
    return p


def flip_for_print(parts):
    """Rotate lid 180 deg about X so its top face sits on the bed."""
    zt = Z_TOP + LID_T
    return [[tuple((x, -y, zt - z) for x, y, z in t) for t in prt] for prt in parts]


def shift(parts, dx, dy):
    return [[tuple((x + dx, y + dy, z) for x, y, z in t) for t in prt] for prt in parts]


# ---------------- export -----------------------------------------------------
def write_stl(path, parts):
    tris = [t for prt in parts for t in prt]
    with open(path, "wb") as f:
        f.write(b"CP2112 case".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            nrm = np.cross(np.subtract(b, a), np.subtract(c, a))
            ln = np.linalg.norm(nrm) or 1.0
            f.write(struct.pack("<12fH", *(nrm / ln), *a, *b, *c, 0))
    return len(tris)


def write_3mf(path, objects):
    """objects: list of (name, parts). Each part = mesh; parent object groups parts."""
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
           '<resources>']
    oid = 1
    build = []
    for name, parts in objects:
        child_ids = []
        for prt in parts:
            verts, idx, faces = [], {}, []
            for tri in prt:
                f = []
                for v in tri:
                    k = (round(v[0], 5), round(v[1], 5), round(v[2], 5))
                    if k not in idx:
                        idx[k] = len(verts)
                        verts.append(k)
                    f.append(idx[k])
                faces.append(f)
            xml.append(f'<object id="{oid}" type="model"><mesh><vertices>')
            xml += [f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts]
            xml.append('</vertices><triangles>')
            xml += [f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces]
            xml.append('</triangles></mesh></object>')
            child_ids.append(oid)
            oid += 1
        xml.append(f'<object id="{oid}" name="{name}" type="model"><components>')
        xml += [f'<component objectid="{c}"/>' for c in child_ids]
        xml.append('</components></object>')
        build.append(oid)
        oid += 1
    xml.append('</resources><build>')
    xml += [f'<item objectid="{b}"/>' for b in build]
    xml.append('</build></model>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", "\n".join(xml))


if __name__ == "__main__":
    b = base()
    l_print = shift(flip_for_print(lid()), 0, (Y1 - Y0) * 2 + 8)
    for name, parts in (("base", b), ("lid", lid())):
        bad = [i for i, prt in enumerate(parts) if volume(prt) <= 0]
        assert not bad, f"{name}: inside-out parts {bad}"
    print(f"Outer size: {X1 - X0:.1f} x {Y1 - Y0:.1f} x {Z_TOP + LID_T:.1f} mm")
    print("base tris:", write_stl("cp2112_case_base.stl", b))
    print("lid  tris:", write_stl("cp2112_case_lid.stl", flip_for_print(lid())))
    write_3mf("cp2112_case.3mf", [("CP2112 case base", b), ("CP2112 case lid", l_print)])
    print("wrote cp2112_case_base.stl, cp2112_case_lid.stl, cp2112_case.3mf")
