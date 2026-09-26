"""HEAVEN//ROOT — a pro-acceleration cybersec edit, rendered entirely in code.

Art style: "1-bit seraph". Every scene is drawn at 360x640, crushed through
an 8x8 Bayer ordered dither into a 3-colour palette (void black, bone white,
blood red), blown up 3x nearest-neighbour to 1080x1920, then hit with CRT
scanlines, RGB split, slice glitches and beat-synced camera punches.
Typography: Anton (impact), UnifrakturMaguntia (blackletter), VT323 (terminal).

    python render.py --out edit.mp4 [--audio song.mp3 --bpm 140 --offset 0]
"""

import argparse
import math
import os
import random
import subprocess
import sys
from multiprocessing import Pool

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import timeline as T

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, S = 1080, 1920, 3
LW, LH = W // S, H // S
FPS = 30

BLACK = (7, 6, 9)
WHITE = (238, 234, 226)
RED = (255, 28, 44)
PAL = np.array([BLACK, WHITE, RED], np.uint8)

FONTS = {
    "anton": "fonts/Anton-Regular.ttf",
    "goth": "fonts/UnifrakturMaguntia-Book.ttf",
    "vt": "fonts/VT323-Regular.ttf",
    "px": "fonts/PressStart2P-Regular.ttf",
}

# ---------------------------------------------------------------- fonts / text

_fc = {}


def F(name, size):
    size = max(8, int(size))
    key = (name, size)
    if key not in _fc:
        _fc[key] = ImageFont.truetype(os.path.join(HERE, FONTS[name]), size)
    return _fc[key]


_fit = {}


def fit(name, text, max_w, max_size):
    key = (name, text, max_w, max_size)
    if key not in _fit:
        size = max_size
        while size > 10:
            bb = F(name, size).getbbox(text)
            if bb[2] - bb[0] <= max_w:
                break
            size = int(size * 0.94)
        _fit[key] = size
    return _fit[key]


def txt(d, xy, s, font, fill=WHITE, anchor="mm", stroke=0, shadow=None, soff=(-9, 6)):
    if shadow:
        d.text((xy[0] + soff[0], xy[1] + soff[1]), s, font=font, fill=shadow, anchor=anchor,
               stroke_width=stroke, stroke_fill=BLACK)
    d.text(xy, s, font=font, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=BLACK)


SMALL_WORDS = {"IS", "THE", "IN", "ARE"}
_lay = {}


def line_layout(words, font, cy):
    """Stack a lyric line vertically: big words huge, filler words small."""
    key = (tuple(words), font, cy)
    if key not in _lay:
        items = []
        for _, w in words:
            small = w in SMALL_WORDS
            ww = (w.lower() if small else w.capitalize()) if font == "goth" else w
            size = fit(font, ww, 960, (150 if small else 300) if font == "goth" else (130 if small else 270))
            bb = F(font, size).getbbox(ww, anchor="mm")
            items.append([ww, size, bb[3] - bb[1]])
        gap = 26
        total = sum(it[2] for it in items) + gap * (len(items) - 1)
        y = cy - total / 2
        out = []
        for ww, size, hgt in items:
            out.append((ww, size, y + hgt / 2))
            y += hgt + gap
        _lay[key] = out
    return _lay[key]


def slam(d, s, name, since, cx, cy, max_w, max_size, fill=WHITE, shadow=RED, dur=0.3, frm=1.35, stroke=8):
    k = min(1.0, max(0.0, since / dur))
    sc = frm + (1 - frm) * (1 - (1 - k) ** 3)
    size = fit(name, s, max_w, max_size) * sc
    size = int(size / 4) * 4
    txt(d, (cx, cy), s, F(name, size), fill, stroke=stroke, shadow=shadow)


GLYPHS = "!#$%&*+/<>=?@01ABCDEFXZ\\|"


def scramble(s, prog, seed):
    r = random.Random(seed)
    n = max(1, len(s))
    return "".join(c if (c == " " or i / n < prog) else r.choice(GLYPHS) for i, c in enumerate(s))


# ---------------------------------------------------------------- precompute

_b8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], np.float32)
THR = np.tile((_b8 + 0.5) / 64.0, (LH // 8, LW // 8))

YY, XX = np.mgrid[0:LH, 0:LW].astype(np.float32)

# CRT scanlines (1 dark row per 3-px "pixel") * vignette
_scan = np.ones(H, np.float32)
_scan[2::3] = 0.72
_vy = (np.arange(H) - H / 2) / (H / 2)
_vx = (np.arange(W) - W / 2) / (W / 2)
_vig = 1 - 0.38 * np.clip(_vy[:, None] ** 2 * 0.7 + _vx[None, :] ** 2 * 0.9 - 0.25, 0, 1)
POSTMUL = (_scan[:, None] * _vig)[:, :, None].astype(np.float32)

prng = np.random.default_rng(1337)

# neural net
NN_LAYERS = [5, 8, 10, 8, 5]
NN_NODES = []
for li, n in enumerate(NN_LAYERS):
    x = 40 + li * (280 / (len(NN_LAYERS) - 1))
    for j in range(n):
        y = 320 + (j - (n - 1) / 2) * 46
        NN_NODES.append((li, x, y))
NN_EDGES = []
for li in range(len(NN_LAYERS) - 1):
    A = [p for p in NN_NODES if p[0] == li]
    Bn = [p for p in NN_NODES if p[0] == li + 1]
    for a in A:
        for b_ in Bn:
            NN_EDGES.append((a[1], a[2], b_[1], b_[2], prng.random()))
prng.shuffle(NN_EDGES)

# hex rain texture (Press Start 2P at native 8px)
HEX_COLS = LW // 8
HEX_TEX = []
for v in range(2):
    im = Image.new("L", (LW, LH))
    d = ImageDraw.Draw(im)
    r = random.Random(v + 5)
    for c in range(HEX_COLS):
        for row in range(LH // 10):
            d.text((c * 8, row * 10), r.choice("0123456789ABCDEF"), font=F("px", 8), fill=255)
    HEX_TEX.append(np.asarray(im, np.float32) / 255)
HEX_SPD = prng.uniform(0.6, 1.8, HEX_COLS)
HEX_PH = prng.uniform(0, 1, HEX_COLS)

# circuit traces
CIRCUIT = []
for _ in range(70):
    gx, gy = prng.integers(1, LW // 12), prng.integers(1, LH // 12)
    pts = [(gx * 12, gy * 12)]
    dx, dy = [(1, 0), (-1, 0), (0, 1), (0, -1)][prng.integers(4)]
    for _ in range(prng.integers(12, 34)):
        if prng.random() < 0.28:
            dx, dy = (dy, dx) if prng.random() < 0.5 else (-dy, -dx)
        gx, gy = gx + dx, gy + dy
        pts.append((gx * 12, gy * 12))
    CIRCUIT.append((pts, prng.random(), prng.uniform(0.25, 0.6)))

# globe attack arcs
def _unit(lat, lon):
    return np.array([math.cos(lat) * math.sin(lon), -math.sin(lat), math.cos(lat) * math.cos(lon)])


ARCS = []
for _ in range(22):
    a = _unit(prng.uniform(-1.1, 1.1), prng.uniform(0, 2 * math.pi))
    b_ = _unit(prng.uniform(-1.1, 1.1), prng.uniform(0, 2 * math.pi))
    ARCS.append((a, b_, prng.random()))

# warp starfield
STARS = np.c_[prng.uniform(-1, 1, 320), prng.uniform(-1, 1, 320), prng.uniform(0, 1, 320)]

# crack tree ("the sky is breaking")
CRACKS = []


def _grow(x, y, ang, depth, dist):
    n = int(prng.integers(6, 14))
    for _ in range(n):
        ang += prng.normal(0, 0.35)
        L = prng.uniform(6, 16)
        nx, ny = x + math.cos(ang) * L, y + math.sin(ang) * L
        CRACKS.append((dist, x, y, nx, ny, depth))
        dist += L
        x, y = nx, ny
        if depth < 3 and prng.random() < 0.13:
            _grow(x, y, ang + prng.choice([-1, 1]) * prng.uniform(0.5, 1.1), depth + 1, dist)


for a in np.linspace(0, 2 * math.pi, 7)[:-1]:
    _grow(215, 150, a + prng.normal(0, 0.2), 0, 0)
CRACKS.sort()
CRACK_MAX = max(c[0] for c in CRACKS)

# sclera veins
_vim = Image.new("L", (LW, LH))
_vd = ImageDraw.Draw(_vim)
for _ in range(26):
    ang = prng.uniform(0, 2 * math.pi)
    rr = 175
    x, y = 180 + math.cos(ang) * rr, 320 + math.sin(ang) * rr * 0.7
    for _ in range(14):
        ang2 = math.atan2(320 - y, 180 - x) + prng.normal(0, 0.6)
        nx, ny = x + math.cos(ang2) * 7, y + math.sin(ang2) * 7
        _vd.line([(x, y), (nx, ny)], fill=200, width=1)
        x, y = nx, ny
VEINS = np.asarray(_vim, np.float32) / 255

# ---------------------------------------------------------------- beat helpers

KICKS = np.array(T.kick_beats(), np.float32)
SNARES = np.array(T.snare_beats(), np.float32)


def since_last(b, arr):
    past = arr[arr <= b + 1e-6]
    return b - past[-1] if len(past) else 99.0


def env(b, arr, decay=0.22):
    return math.exp(-since_last(b, arr) / decay)


def ease(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


# ---------------------------------------------------------------- low-res scene painters


class Canvas:
    def __init__(self):
        self.L = Image.new("L", (LW, LH))
        self.R = Image.new("L", (LW, LH))
        self.dl = ImageDraw.Draw(self.L)
        self.dr = ImageDraw.Draw(self.R)
        self.lum = np.zeros((LH, LW), np.float32)
        self.red = np.zeros((LH, LW), np.float32)

    def flush(self, glow=0.0, rglow=0.0):
        lum = np.asarray(self.L, np.float32) / 255
        red = np.asarray(self.R, np.float32) / 255
        if glow:
            lum = np.maximum(lum, np.asarray(self.L.filter(ImageFilter.GaussianBlur(6)), np.float32) / 255 * glow)
        if rglow:
            red = np.maximum(red, np.asarray(self.R.filter(ImageFilter.GaussianBlur(7)), np.float32) / 255 * rglow)
        self.lum = np.maximum(self.lum, lum)
        self.red = np.maximum(self.red, red)
        self.L.paste(0, (0, 0, LW, LH))
        self.R.paste(0, (0, 0, LW, LH))


def rays(b, cy=-60, amt=1.0, n=16):
    ang = np.arctan2(XX - LW / 2, YY - cy)
    dist = np.hypot(XX - LW / 2, YY - cy)
    r = (0.5 + 0.5 * np.sin(ang * n + b * 0.35)) ** 5 * 0.8 + (0.5 + 0.5 * np.sin(ang * 7 - b * 0.2)) ** 3 * 0.4
    return np.clip(r * np.exp(-dist / 520) * amt + np.exp(-dist / 160) * 0.45 * amt, 0, 1)


def rotm(ax, ay, az):
    ca, sa, cb, sb, cc, sc = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    Rx = np.array([[1, 0, 0], [0, ca, -sa], [0, sa, ca]])
    Ry = np.array([[cb, 0, sb], [0, 1, 0], [-sb, 0, cb]])
    Rz = np.array([[cc, -sc, 0], [sc, cc, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


_TH = np.linspace(0, 2 * math.pi, 97)
_CIRC = np.c_[np.cos(_TH), np.sin(_TH), np.zeros_like(_TH)]


def eye_shape(dl, dr, x, y, w, h, lid=1.0, pupil_red=True, fill=0):
    h = max(0.5, h * lid)
    pts = [(x + w * math.cos(a), y + h * math.sin(a) * abs(math.sin(a)) ** 0.2) for a in np.linspace(0, 2 * math.pi, 18)]
    dl.polygon(pts, fill=fill, outline=255)
    if lid > 0.35:
        pr = max(1.0, min(w, h) * 0.55)
        dl.ellipse([x - pr, y - pr, x + pr, y + pr], fill=230)
        pp = pr * 0.5
        dl.ellipse([x - pp, y - pp, x + pp, y + pp], fill=0)
        if pupil_red:
            dr.ellipse([x - pr, y - pr, x + pr, y + pr], outline=255, width=max(1, int(pr * 0.35)))


def seraph(cv, b, cx, cy, sc=1.0, blink=0.0, wings=True):
    dl, dr = cv.dl, cv.dr
    flap = math.sin(b * math.pi * 0.5) * 0.14
    if wings:
        for side in (-1, 1):
            for k, base in enumerate((-0.95, -0.3, 0.45)):
                a = base + flap * (1.0, 0.6, -0.8)[k]
                Ls = 175 * sc * (1.0, 0.92, 0.72)[k]
                n = 13
                prev = (cx, cy)
                for j in range(1, n + 1):
                    s = j / n
                    aa = a + s * 0.45 * (1 if k < 2 else -0.6)
                    px = cx + side * math.cos(aa) * Ls * s
                    py = cy + math.sin(aa) * Ls * s - math.sin(s * math.pi) * 14 * sc
                    dl.line([prev, (px, py)], fill=255, width=2)
                    prev = (px, py)
                    fa = aa + (1.25 if k < 2 else 0.9)
                    fl = (58 - 30 * s) * sc * (1.0, 0.85, 0.7)[k]
                    fx_ = px + side * math.cos(fa) * fl
                    fy_ = py + math.sin(fa) * fl
                    dl.line([(px, py), (fx_, fy_)], fill=200, width=1)
                    if j % 3 == 0:
                        dl.line([(px, py), (fx_ + side * 3, fy_ + 4)], fill=110, width=1)
    rings = [
        (92, (b * 0.42, 0.5, 0.0), 7),
        (122, (1.1, b * 0.33, 0.35), 9),
        (152, (0.35, 1.25, b * 0.25 + 1.0), 11),
    ]
    D = 650
    for R_, (ax, ay, az), neyes in rings:
        M = rotm(ax, ay, az)
        for dr_ in (0, 7):
            P = (_CIRC * (R_ + dr_) * sc) @ M.T
            p = D / (D + P[:, 2])
            x = cx + P[:, 0] * p
            y = cy + P[:, 1] * p
            for i in range(len(x) - 1):
                dl.line([(x[i], y[i]), (x[i + 1], y[i + 1])], fill=255 if P[i, 2] < 0 else 95, width=2 if dr_ == 0 else 1)
        th = np.linspace(0, 2 * math.pi, neyes, endpoint=False) + 0.3
        E = (np.c_[np.cos(th), np.sin(th), np.zeros_like(th)] * (R_ + 3.5) * sc) @ M.T
        for (ex, ey, ez) in E:
            p = D / (D + ez)
            if ez < 20:
                eye_shape(dl, dr, cx + ex * p, cy + ey * p, 8.5 * p * sc, 5 * p * sc, 1 - blink)
    # the central eye
    eye_shape(dl, dr, cx, cy, 34 * sc, 17 * sc, 1 - blink)
    # halo
    dl.ellipse([cx - 48 * sc, cy - 215 * sc, cx + 48 * sc, cy - 197 * sc], outline=255, width=2)
    dr.ellipse([cx - 52 * sc, cy - 219 * sc, cx + 52 * sc, cy - 193 * sc], outline=160, width=1)


def neural(cv, b, prog=1.0, act=1.0):
    dl, dr = cv.dl, cv.dr
    n = int(len(NN_EDGES) * prog)
    for i, (x0, y0, x1, y1, ph) in enumerate(NN_EDGES[:n]):
        dl.line([(x0, y0), (x1, y1)], fill=int(55 + 50 * act), width=1)
        s = (b * 0.5 + ph) % 1.0
        px, py = x0 + (x1 - x0) * s, y0 + (y1 - y0) * s
        (dr if i % 9 == 0 else dl).ellipse([px - 1.5, py - 1.5, px + 1.5, py + 1.5], fill=255)
    for li, x, y in NN_NODES:
        on = math.sin(b * math.pi + x * 0.1 + y * 0.07) > 0.2
        dl.ellipse([x - 5, y - 5, x + 5, y + 5], outline=255, fill=255 if on and act > 0.5 else 0, width=1)


def hexrain(b, speed=1.0, bright=1.0):
    out = np.zeros((LH, LW), np.float32)
    y = np.arange(LH, dtype=np.float32)
    for c in range(HEX_COLS):
        tex = HEX_TEX[int(b * 3 + c) % 2][:, c * 8:(c + 1) * 8]
        head = (b * HEX_SPD[c] * speed * 110 + HEX_PH[c] * 1000) % (LH + 260)
        dist = head - y
        tr = np.clip(1 - dist / 240, 0, 1) * (dist >= 0)
        tr = tr * 0.85 + (np.abs(dist) < 10) * 0.6 + 0.07
        out[:, c * 8:(c + 1) * 8] = tex * tr[:, None]
    return np.clip(out * bright, 0, 1)


def globe(cv, b, cx=180, cy=320, rad=150, arcs=1.0):
    dl, dr = cv.dl, cv.dr
    yaw = b * 0.28
    M = rotm(0.35, yaw, 0)
    lines = []
    for lat in np.radians(np.arange(-60, 61, 30)):
        th = np.linspace(0, 2 * math.pi, 49)
        lines.append(np.c_[np.cos(lat) * np.sin(th), -np.full_like(th, math.sin(lat)), np.cos(lat) * np.cos(th)])
    for lon in np.radians(np.arange(0, 180, 30)):
        th = np.linspace(0, 2 * math.pi, 49)
        lines.append(np.c_[np.cos(th) * math.sin(lon), np.sin(th), np.cos(th) * math.cos(lon)])
    for P in lines:
        P = P @ M.T
        for i in range(len(P) - 1):
            dl.line([(cx + P[i, 0] * rad, cy + P[i, 1] * rad), (cx + P[i + 1, 0] * rad, cy + P[i + 1, 1] * rad)],
                    fill=235 if P[i, 2] < 0.1 else 70, width=1)
    dl.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=255, width=2)
    na = int(len(ARCS) * arcs)
    for a, bb, ph in ARCS[:na]:
        om = math.acos(np.clip(a @ bb, -1, 1))
        head = ((b * 0.55 + ph) % 1.0) * 1.5
        ss = np.linspace(max(0, head - 0.45), min(1, head), 16)
        if head - 0.45 > 1:
            continue
        pts = []
        for s in ss:
            v = (math.sin((1 - s) * om) * a + math.sin(s * om) * bb) / math.sin(om)
            v = v * (1 + 0.32 * math.sin(math.pi * s))
            v = M @ v
            pts.append((cx + v[0] * rad, cy + v[1] * rad))
        if len(pts) > 1:
            dr.line(pts, fill=255, width=2)
        if head >= 1:
            v = M @ bb
            rr = 3 + (head - 1) * 18
            dr.ellipse([cx + v[0] * rad - rr, cy + v[1] * rad - rr, cx + v[0] * rad + rr, cy + v[1] * rad + rr], outline=255, width=1)


def big_eye(b, cx=180, cy=320, pupil=38, blink=0.0, redness=1.0):
    dx, dy = XX - cx, YY - cy
    r = np.hypot(dx, dy)
    th = np.arctan2(dy, dx)
    Ri = 112
    stri = 0.5 + 0.5 * np.sin(th * 46 + 2.5 * np.sin(th * 9 + b) + r * 0.06)
    lum = np.where(r < Ri, 0.22 + 0.7 * stri * (r / Ri) ** 0.7, 0.16)
    lum = np.where(np.abs(r - Ri) < 2.5, 1.0, lum)
    lum = np.where(r < pupil, 0.0, lum)
    red = np.where((r > pupil) & (r < pupil + 16), stri * 0.95 * redness, 0.0)
    red = np.where(np.abs(r - pupil) < 2.5, 1.0 * redness, red)
    red = np.maximum(red, np.where(r > Ri + 3, VEINS * redness, 0))
    hl = np.hypot(XX - (cx - 38), YY - (cy - 42)) < 13
    lum = np.where(hl, 1.0, lum)
    red = np.where(hl, 0, red)
    w = 176
    h = 132 * (1 - blink) * np.clip(1 - (dx / w) ** 2, 0, 1)
    inside = np.abs(dy) < h
    edge = np.abs(np.abs(dy) - h) < 2.5
    edge &= np.abs(dx) < w
    lum = np.where(inside, lum, 0) + edge * 1.0
    red = np.where(inside, red, 0)
    # lashes
    lash = ((np.abs(dy) > h) & (np.abs(dy) < h + 18) & (np.abs(dx) < w * 0.92) & (((XX + np.sign(dy) * YY * 0.3) // 5) % 3 == 0))
    lum = np.maximum(lum, lash * 0.9)
    return np.clip(lum, 0, 1).astype(np.float32), np.clip(red, 0, 1).astype(np.float32)


def circuit(cv, b, blood=1.0):
    dl, dr = cv.dl, cv.dr
    for pts, ph, spd in CIRCUIT:
        dl.line(pts, fill=105, width=1)
        x, y = pts[0]
        dl.rectangle([x - 2, y - 2, x + 2, y + 2], fill=255)
        x, y = pts[-1]
        dl.ellipse([x - 3, y - 3, x + 3, y + 3], outline=255)
        if blood > 0:
            L = len(pts)
            head = ((b * spd + ph) % 1.0) * (L + 8)
            a, c = int(max(0, head - 7 * blood)), int(min(L, head))
            if c - a >= 2:
                dr.line(pts[a:c], fill=255, width=3)


def chart(cv, b, prog):
    dl, dr = cv.dl, cv.dr
    x0, y0, x1, y1 = 44, 590, 330, 150
    for i in range(11):
        y = y0 - i * 44
        for x in range(x0, x1, 6):
            dl.point((x, y), fill=130)
    for i in range(7):
        x = x0 + i * (x1 - x0) / 6
        dl.line([(x, y0), (x, y0 + 5)], fill=255)
    dl.line([(x0, y0), (x1, y0)], fill=255, width=2)
    dl.line([(x0, y0), (x0, y1 - 40)], fill=255, width=2)
    xs = np.linspace(0, 1, 160)
    ys = (np.exp(6.2 * xs) - 1) / (math.exp(6.2) - 1)
    k = max(2, int(len(xs) * prog))
    pts = [(x0 + xs[i] * (x1 - x0), y0 - ys[i] * (y0 - y1) * 1.35) for i in range(k)]
    dl.polygon(pts + [(pts[-1][0], y0), (x0, y0)], fill=55)
    dl.line(pts, fill=255, width=3)
    hx, hy = pts[-1]
    rr = 5 + 10 * ((b * 2) % 1)
    dr.ellipse([hx - 5, hy - 5, hx + 5, hy + 5], fill=255)
    dr.ellipse([hx - rr, hy - rr, hx + rr, hy + rr], outline=255)
    return hx, hy


def warp(cv, b, speed=1.0, red_frac=0.0):
    dl, dr = cv.dl, cv.dr
    z = (STARS[:, 2] - b * 0.25 * speed) % 1.0 + 0.03
    z2 = z + 0.04 * speed
    for i in range(len(STARS)):
        x, y = STARS[i, 0], STARS[i, 1]
        p1 = (LW / 2 + x / z[i] * 60, LH / 2 + y / z[i] * 60)
        p2 = (LW / 2 + x / z2[i] * 60, LH / 2 + y / z2[i] * 60)
        (dr if i < len(STARS) * red_frac else dl).line([p2, p1], fill=int(255 * min(1, 0.25 / z[i])), width=1)


def cracks(cv, prog):
    lim = prog * CRACK_MAX
    for dist, x0, y0, x1, y1, depth in CRACKS:
        if dist > lim:
            break
        cv.dl.line([(x0, y0), (x1, y1)], fill=255, width=2 if depth == 0 else 1)
        if depth == 0:
            cv.dr.line([(x0 + 3, y0 + 2), (x1 + 3, y1 + 2)], fill=255, width=1)


def hazard(b, top=True, bottom=True):
    m = (((XX + YY + b * 24) // 14) % 2 == 0)
    band = np.zeros_like(m)
    if top:
        band |= (YY > 44) & (YY < 70)
    if bottom:
        band |= (YY > 570) & (YY < 596)
    return (m & band).astype(np.float32)


_NOISE = [np.random.default_rng(s).random((LH, LW), dtype=np.float32) for s in range(4)]


def noise(seed, amt=1.0):
    # a few fixed grain plates: random-per-frame 1-bit noise is incompressible
    return _NOISE[seed % 4] * amt


# ---------------------------------------------------------------- scene logic


class FX:
    def __init__(self):
        self.zoom = 1.0
        self.shake = 0.0
        self.rgb = 0
        self.slices = 0
        self.invert = False
        self.flash = 0.0      # white flash amount
        self.redflash = False
        self.crt = None       # ('on', k) / ('off', k)


def spell_unit(b):
    for i, (u, w, letters) in enumerate(T.SPELLS):
        if u <= b < u + 8:
            return i, u, w, letters
    return None


def scene(b, fi):
    cv = Canvas()
    fx = FX()
    hud = []  # callables(draw, img)
    ke = env(b, KICKS, 0.22)
    se = env(b, SNARES, 0.2)

    # ------------------------------------------------ INTRO
    if b < 16:
        if b < 4:
            prog = ease(b / 3.6)
            neural(cv, b, prog, act=0.3)
            cv.flush(glow=0.4)
            cv.lum *= 0.55 + 0.45 * env(b, np.array([0, 1, 2, 3, 0.3, 1.3, 2.3, 3.3], np.float32), 0.15)
            cv.lum = np.maximum(cv.lum, noise(0, 0.16))
            fx.crt = ("on", b / 0.6) if b < 0.6 else None
            lines = [
                "> boot seraph.sys",
                "[ OK ] 1.8T parameters online",
                "[ OK ] gradient descent: complete",
                "[ OK ] firewall: autonomous",
                "[ !! ] human operator: unverified",
                "> awaiting input_",
            ]

            def h(d, img, b=b):
                for i, l in enumerate(lines):
                    tstart = 0.4 + i * 0.55
                    if b > tstart:
                        n = int((b - tstart) * 60)
                        col = RED if "!!" in l else WHITE
                        txt(d, (70, 1180 + i * 62), l[:n], F("vt", 54), col, anchor="lm", stroke=4)
            hud.append(h)
        elif b < 12:
            idx = int((b - 4) // 2)
            since = (b - 4) % 2
            word = ["MACHINE", "LEARN", "SCHOOL", "TERROR"][idx]
            if idx == 0:
                neural(cv, b, 1.0, act=1.0)
                cv.flush(glow=0.6)
            elif idx == 1:
                cv.lum = hexrain(b, 1.6)
            elif idx == 2:
                circuit(cv, b, blood=0.0)
                cv.flush(glow=0.3)
                cv.lum = np.maximum(cv.lum, rays(b, amt=0.35))
            else:
                cv.lum, cv.red = big_eye(b, pupil=30 + 25 * ke)
                fx.slices = 6
            fx.zoom = 1 + 0.08 * math.exp(-since / 0.3) + since * 0.02
            fx.shake = 14 * math.exp(-since / 0.2)
            fx.rgb = int(18 * math.exp(-since / 0.25))
            fx.invert = since < 0.1
            fx.redflash = idx == 3 and 0.1 <= since < 0.2
            col = RED if idx == 3 else WHITE

            def h(d, img, word=word, since=since, col=col, idx=idx):
                slam(d, word, "anton", since, W / 2, H / 2, 980, 420, col, shadow=RED if col == WHITE else WHITE)
                txt(d, (W / 2, H / 2 + 330), ["// weights", "// gradient", "// dataset", "// threat model"][idx],
                    F("vt", 64), WHITE, stroke=4)
            hud.append(h)
        else:
            since = b - 12
            desc = ease(since / 1.6)
            sy = -160 + desc * (300 + 160)
            cv.lum = rays(b, amt=0.4 + 0.6 * desc)
            if b >= 14:
                cracks(cv, ease((b - 14) / 1.6))
                cv.flush()
            seraph(cv, b, 180, sy, 0.95)
            cv.flush(glow=0.7, rglow=0.6)
            local = b - 12 if b < 14 else b - 14
            fx.zoom = 1 + 0.06 * math.exp(-local / 0.35) + (max(0, b - 15) ** 2) * 0.12
            fx.invert = local < 0.08
            fx.shake = 10 * math.exp(-local / 0.25) + max(0, b - 15) * 16
            fx.rgb = int(12 * math.exp(-local / 0.3) + max(0, b - 15) * 14)
            word, sub = ("Angel", "(let us save you)") if b < 14 else ("Above", "(the sky is breaking)")

            def h(d, img, word=word, sub=sub, local=local):
                slam(d, word, "goth", local, W / 2, 1420, 900, 330, WHITE, shadow=RED, frm=1.2, stroke=6)
                if local > 0.4:
                    txt(d, (W / 2, 1640), sub, F("vt", 66), RED, stroke=4)
            hud.append(h)

    # ------------------------------------------------ SPELL (password crack)
    elif b < 48:
        k, u, word, letters = spell_unit(b)
        l = b - u
        bgs = ["hex", "globe", "eye", "nn"]
        bg = bgs[k]
        if bg == "hex":
            cv.lum = hexrain(b, 1.0, 0.8)
        elif bg == "globe":
            globe(cv, b, arcs=0.6)
            cv.flush(glow=0.3, rglow=0.4)
        elif bg == "eye":
            wrong = T.WRONG_AT <= l < T.WRONG_AT + 0.35
            cv.lum, cv.red = big_eye(b, pupil=26 + 30 * ke, blink=0.9 if wrong else 0.0, redness=0.8)
        else:
            neural(cv, b, 1.0, act=0.8)
            cv.flush(glow=0.35)
        cv.lum *= 0.5
        cv.red *= 0.6
        n_typed = 0
        if l >= T.LETTER_START:
            n_typed = min(len(letters), int((l - T.LETTER_START) / T.LETTER_STEP) + 1)
        phase = "prompt" if l < T.LETTER_START else ("type" if l < T.WRONG_AT else ("wrong" if l < T.TRY_AT else "try"))
        if phase == "prompt":
            fx.zoom = 1 + 0.04 * math.exp(-l / 0.4)
            fx.rgb = int(10 * math.exp(-l / 0.2))
        elif phase == "type":
            ls = (l - T.LETTER_START) % T.LETTER_STEP
            fx.zoom = 1 + 0.05 * math.exp(-ls / 0.12)
            fx.rgb = int(8 * math.exp(-ls / 0.1))
            fx.shake = 4 * ke
        else:
            ws = l - T.WRONG_AT if phase == "wrong" else l - T.TRY_AT
            fx.shake = 22 * math.exp(-ws / 0.25) if phase == "wrong" else 3
            fx.rgb = int(26 * math.exp(-ws / 0.3))
            fx.slices = 10 if ws < 0.4 and phase == "wrong" else 0
            fx.redflash = phase == "wrong" and ws < 0.08
            fx.invert = phase == "wrong" and 0.08 <= ws < 0.16
            if phase == "wrong":
                cv.red = np.maximum(cv.red, hazard(b) * 0.9)
        ph = phase

        def h(d, img, k=k, word=word, letters=letters, l=l, n_typed=n_typed, ph=ph, b=b):
            txt(d, (70, 250), "> SERAPH-OS // AUTH", F("vt", 58), WHITE, anchor="lm", stroke=4)
            txt(d, (W - 70, 250), f"ATTEMPT {k + 1:02d}/04", F("vt", 58), RED, anchor="rm", stroke=4)
            d.line([(70, 290), (W - 70, 290)], fill=WHITE, width=3)
            if ph == "prompt":
                txt(d, (W / 2, 760), "NOW SPELL", F("vt", 110), WHITE, stroke=5)
                s = scramble(word, l / 0.8, int(b * 12))
                slam(d, f'"{s}"', "anton", l, W / 2, 960, 960, 300, WHITE, frm=1.15)
            else:
                txt(d, (W / 2, 380), f'> spell "{word}"', F("vt", 70), WHITE, stroke=4)
            if ph == "type":
                ls = (l - T.LETTER_START) % T.LETTER_STEP
                ch = letters[n_typed - 1]
                slam(d, ch, "anton", ls, W / 2, 900, 900, 880, WHITE, frm=1.3, dur=0.2, stroke=10)
            if ph == "wrong":
                ws = l - T.WRONG_AT
                slam(d, "WRONG!", "anton", ws, W / 2, 900, 980, 420, RED, shadow=WHITE, frm=1.5, dur=0.2)
                txt(d, (W / 2, 1180), "ACCESS DENIED", F("vt", 96), RED, stroke=5)
            if ph == "try":
                if k < 3:
                    slam(d, "TRY AGAIN", "anton", l - T.TRY_AT, W / 2, 900, 900, 260, WHITE, frm=1.2)
                    txt(d, (W / 2, 1100), "ACCESS DENIED  //  RETRYING", F("vt", 62), RED, stroke=4)
                else:
                    slam(d, "WRONG!", "anton", 1, W / 2, 900, 980, 420, RED, shadow=WHITE)
                    txt(d, (W / 2, 1180), "the correct spelling is...", F("vt", 80), WHITE, stroke=5)
            # password field
            n = len(letters)
            bw, gap = 96, 16
            tot = n * bw + (n - 1) * gap
            x0 = (W - tot) / 2
            bad = ph in ("wrong", "try")
            for i in range(n):
                x = x0 + i * (bw + gap)
                d.rectangle([x, 1440, x + bw, 1560], outline=RED if bad else WHITE, width=5, fill=BLACK)
                if i < n_typed:
                    txt(d, (x + bw / 2, 1500), letters[i], F("anton", 84), RED if bad else WHITE)
                elif i == n_typed and int(b * 4) % 2 == 0 and ph == "type":
                    d.rectangle([x + 20, 1535, x + bw - 20, 1545], fill=WHITE)
            txt(d, (W / 2, 1620), f"{n} CHARS  //  BRUTE-FORCE: {'FAILED' if bad else 'RUNNING'}", F("vt", 50),
                RED if bad else WHITE, stroke=3)
        hud.append(h)

    # ------------------------------------------------ REVEAL
    elif b < 56:
        l = b - 48
        if l < 4:
            cv.lum = np.maximum(rays(b, amt=0.25 + l * 0.12), noise(1, 0.06 + l * 0.05))
            fx.zoom = 1 + l * 0.03
            fx.shake = l * 2

            def h(d, img, l=l, b=b):
                p = l / 3.2
                slam(d, scramble("THE CORRECT", p * 1.2, int(b * 16)), "anton", 1, W / 2, 780, 900, 200)
                slam(d, scramble("SPELLING IS", p * 1.2 - 0.25, int(b * 16) + 1), "anton", 1, W / 2, 1000, 900, 200)
                bars = int(min(1, p) * 20)
                txt(d, (W / 2, 1260), "DECRYPTING [" + "#" * bars + "." * (20 - bars) + "]", F("vt", 62), RED, stroke=4)
            hud.append(h)
        elif l < 7:
            i = int(l - 4)
            ls = l - 4 - i
            cv.lum, cv.red = big_eye(b, pupil=20 + i * 22 + 20 * math.exp(-ls / 0.2))
            cv.lum *= 0.45
            fx.invert = ls < 0.07
            fx.redflash = 0.07 <= ls < 0.14
            fx.zoom = 1 + 0.12 * math.exp(-ls / 0.25)
            fx.shake = 26 * math.exp(-ls / 0.2)
            fx.rgb = int(30 * math.exp(-ls / 0.3))
            fx.slices = 8 if ls < 0.3 else 0

            def h(d, img, i=i, ls=ls):
                slam(d, "YOU"[i], "anton", ls, W / 2, 880, 900, 1100, RED, shadow=WHITE, frm=1.6, dur=0.2, stroke=12)
                txt(d, (W / 2, 1560), "  ".join("YOU"[: i + 1]) + "  _" * (2 - i), F("anton", 120), WHITE, stroke=6)
            hud.append(h)
        else:
            ls = l - 7
            cv.lum = np.clip(rays(b, amt=1.0) + ls ** 2 * 0.9, 0, 1)
            fx.zoom = 1 + ls * 0.08
            fx.rgb = int(ls * 20)

            def h(d, img, ls=ls, b=b):
                if int(b * 12) % 3:
                    txt(d, (W / 2, H / 2), "ACCESS GRANTED", F("anton", 150), RED, stroke=10, shadow=BLACK)
                txt(d, (W / 2, H / 2 + 160), "root: Y-O-U", F("vt", 90), WHITE, stroke=6)
            hud.append(h)

    # ------------------------------------------------ CHORUS
    elif b < 88:
        line = next((ln for ln in T.LINES if ln[0] <= b < ln[2]), None)
        l0 = line[0] if line else 84
        l = b - l0
        fx.zoom = 1 + 0.06 * ke
        fx.shake = 12 * ke
        fx.rgb = int(16 * ke)
        fx.flash = max(0, 1 - (b - 56) / 0.5) if b < 56.5 else 0
        if l0 == 56:
            warp(cv, b, 1.6)
            cv.flush()
            cv.lum = np.maximum(cv.lum, rays(b, amt=0.55))
            seraph(cv, b, 180, 250 + math.sin(b * 0.8) * 8, 1.12 + 0.06 * ke)
            cv.flush(glow=0.8, rglow=0.6)
        elif l0 == 64:
            warp(cv, b, 0.8)
            cv.flush()
            cv.lum *= 0.5
            hx, hy = chart(cv, b, ease((b - 64) / 7))
            cv.flush(glow=0.4, rglow=0.7)
            seraph(cv, b, hx, hy - 10, 0.28 + 0.12 * ease((b - 64) / 7), wings=True)
            cv.flush(glow=0.6)
        elif l0 == 72:
            globe(cv, b, rad=160, arcs=1.0)
            cv.flush(glow=0.35, rglow=0.8)
            fx.redflash = since_last(b, KICKS) < 0.07
            fx.slices = int(6 * ke)
        elif l0 == 76:
            circuit(cv, b * 2.5, blood=1.0)
            cv.flush(glow=0.25, rglow=0.9)
            cv.red = np.maximum(cv.red, rays(b, amt=0.35) * 0.6)
        elif l0 == 80:
            cv.lum, cv.red = big_eye(b, pupil=18 + 40 * ke, redness=1.0)
            cv.red = np.maximum(cv.red, hazard(b))
            fx.slices = int(8 * ke)
        else:  # 84-88 autonomous defense
            neural(cv, b * 2, 1.0, act=1.0)
            cv.flush(glow=0.5)
            cv.lum = np.maximum(cv.lum * 0.6, hexrain(b, 3.0, 0.35))
            fx.zoom = 1 + 0.06 * ke + max(0, b - 86) * 0.06
            fx.shake = 12 * ke + max(0, b - 87) * 20

        def h(d, img, line=line, b=b, l=l, l0=l0):
            if line:
                _, words, _, style = line
                font = "goth" if style.startswith("goth") else "anton"
                col = RED if style in ("red", "gothred") else WHITE
                sh = WHITE if col == RED else RED
                lay = line_layout(words, font, 560 if l0 == 64 else 1260)
                for (off, w), (ww, size, y) in zip(words, lay):
                    if l >= off:
                        slam(d, ww, font, l - off, W / 2, y, 960, size, col, shadow=sh, frm=1.4, dur=0.22, stroke=8)
                # HUD per line
                info = {
                    56: ["COMPUTE  ^  1e27 FLOP", "ALIGNMENT: HOLDING"],
                    64: ["SCALING LAWS: HOLDING", "2012 ............ 2030"],
                    72: ["THREAT LEVEL: CRITICAL", "ATTACKS/SEC: 48,112"],
                    76: ["PULSE: 140 BPM", "INTEGRITY: 97.3%"],
                    80: ["!! INTRUSION DETECTED !!", "CVE-2026-0140 // 0DAY"],
                }[l0]
                blink = l0 == 80 and int(b * 4) % 2 == 0
                txt(d, (70, 1830 - 70), info[0], F("vt", 56), RED if blink or l0 in (72, 80) else WHITE, anchor="lm", stroke=4)
                txt(d, (70, 1830), info[1], F("vt", 56), WHITE, anchor="lm", stroke=4)
                if l0 == 64:
                    txt(d, (W / 2, 1880 - 190), "TRAINING COMPUTE (FLOP)", F("vt", 50), WHITE, stroke=3)
            else:
                logs = [
                    "[DEF] anomaly @ 10.0.13.37:443",
                    "[DEF] exploit signature matched",
                    "[DEF] patch synthesized ...... OK",
                    "[DEF] deployed to 40,112 hosts OK",
                    "[DEF] threat neutralized: 0.003s",
                ]
                for i, lg in enumerate(logs):
                    if l > i * 0.4:
                        txt(d, (60, 380 + i * 64), lg[: int((l - i * 0.4) * 70)], F("vt", 56),
                            RED if i == 4 else WHITE, anchor="lm", stroke=4)
                if l > 0:
                    slam(d, "DEFENSE AT", "anton", l, W / 2, 1180, 940, 200, WHITE, frm=1.2)
                if l > 1:
                    slam(d, "MACHINE SPEED", "anton", l - 1, W / 2, 1400, 960, 220, RED, shadow=WHITE, frm=1.3)
        hud.append(h)

    # ------------------------------------------------ CONTROL
    elif b < 104:
        if b < 100:
            line = next(ln for ln in T.LINES[5:] if ln[0] <= b < ln[2])
            l = b - line[0]
            wi = min(3, int(l))
            ws = l - wi
            word = line[1][wi][1]
            warp(cv, b, 3.0, red_frac=0.3 if line[3] == "red" else 0.0)
            cv.flush()
            if wi == 3:
                cv.lum = np.maximum(cv.lum, hexrain(b, 2.5, 0.5))
            fx.zoom = 1 + 0.1 * math.exp(-ws / 0.25)
            fx.shake = 18 * ke
            fx.rgb = int(20 * ke)
            fx.invert = ws < 0.06 and wi == 3
            fx.flash = max(0, 1 - (b - 88) / 0.4) if b < 88.4 else 0
            col = RED if (line[3] == "red" or wi == 3) else WHITE
            priv = min(1.0, (b - 88) / 12)

            def h(d, img, word=word, ws=ws, col=col, priv=priv, wi=wi):
                slam(d, word, "anton", ws, W / 2, H / 2 - 40, 1000, 560 if wi < 3 else 330, col,
                     shadow=WHITE if col == RED else RED, frm=1.45, dur=0.2, stroke=10)
                txt(d, (70, 1600), "PRIVILEGE ESCALATION", F("vt", 58), WHITE, anchor="lm", stroke=4)
                d.rectangle([70, 1650, W - 70, 1710], outline=WHITE, width=4)
                d.rectangle([80, 1660, 80 + (W - 160) * priv, 1700], fill=RED)
                txt(d, (70, 1760), f"user -> root   {int(priv * 100):3d}%", F("vt", 58), WHITE, anchor="lm", stroke=4)
            hud.append(h)
        else:
            i = min(3, int(b - 100))
            ls = b - 100 - i
            cv.lum = hexrain(b, 4.0, 0.6) if i % 2 == 0 else noise(i, 0.5)
            if i == 3:
                cv.red = np.maximum(cv.red, hazard(b, True, True))
                cv.red = np.maximum(cv.red, big_eye(b, pupil=60 - 30 * ls)[1] * 0.8)
            fx.zoom = 1 + 0.12 * math.exp(-ls / 0.2) + i * 0.02
            fx.shake = 20 + i * 8
            fx.rgb = int((14 + i * 8) * math.exp(-ls / 0.3))
            fx.invert = ls < 0.07 or (b > 103.5 and int(b * 16) % 2 == 0)
            fx.slices = 4 + i * 4

            def h(d, img, i=i, ls=ls, b=b):
                col = RED if i >= 2 else WHITE
                slam(d, "CONTROL", "anton", ls, W / 2, H / 2, 900 + i * 25, 300 + i * 60, col,
                     shadow=WHITE if col == RED else RED, frm=1.5, dur=0.18, stroke=10)
                for j in range(i + 1):
                    txt(d, (W / 2, 400 + j * 90), "control", F("vt", 80), RED if j == i else WHITE, stroke=4)
                if b > 103.5:
                    txt(d, (W / 2, H / 2 + 320), "ROOT ACCESS: GRANTED", F("vt", 80), RED, stroke=5)
            hud.append(h)

    # ------------------------------------------------ OUTRO
    else:
        l = b - 104
        cv.lum = rays(b, amt=0.7)
        seraph(cv, b * 0.6, 180, 250, 0.9 + 0.02 * l, blink=0.0 if l < 7 else min(1, (l - 7) * 3))
        cv.flush(glow=0.8, rglow=0.5)
        if l >= 4:
            # ECG sweep
            pts = []
            for x in range(0, LW, 2):
                xb = 108 + x / LW * 3.6
                y = 592
                for bt in (108.0, 109.0):
                    dd = (xb - bt) * 30
                    if -1 < dd < 2:
                        y -= [0, -8, 42, -26, 6][int((dd + 1) / 3 * 5)]
                if xb <= b:
                    pts.append((x, y))
            if len(pts) > 1:
                cv.dr.line(pts, fill=255, width=2)
            cv.flush(rglow=0.6)
        fx.flash = max(0, 1 - l / 0.6)
        fx.zoom = 1 + 0.02 * l
        if l > 7.4:
            fx.crt = ("off", (l - 7.4) / 0.6)

        def h(d, img, l=l, b=b):
            slam(d, "ACCELERATE", "anton", l, W / 2, 1180, 980, 300, WHITE, frm=1.3, dur=0.3)
            if l > 0.8:
                txt(d, (W / 2, 1360), "BUILD FAST.  SECURE EVERYTHING.", F("vt", 72), RED, stroke=4)
            if l > 2:
                slam(d, "e/acc", "goth", l - 2, W / 2, 1490, 600, 170, WHITE, frm=1.2)
            if l > 3:
                s = "root@you:~# ./take_control"
                n = int((l - 3) * 22)
                cur = "_" if int(b * 4) % 2 == 0 else " "
                txt(d, (70, 1660), s[:n] + cur, F("vt", 62), WHITE, anchor="lm", stroke=4)
        hud.append(h)

    return cv, fx, hud


# ---------------------------------------------------------------- frame pipeline

def always_hud(d, b, fi):
    if b < 0.6 or b > 111.3:
        return
    for (x, y, sx, sy) in ((40, 40, 1, 1), (W - 40, 40, -1, 1), (40, H - 40, 1, -1), (W - 40, H - 40, -1, -1)):
        d.line([(x, y), (x + sx * 70, y)], fill=WHITE, width=4)
        d.line([(x, y), (x, y + sy * 70)], fill=WHITE, width=4)
    txt(d, (80, 110), "SERAPH-OS v4.0", F("vt", 48), WHITE, anchor="lm", stroke=3)
    if int(b) % 2 == 0:
        d.ellipse([W - 250, 95, W - 220, 125], fill=RED)
    txt(d, (W - 80, 110), "REC", F("vt", 48), RED, anchor="rm", stroke=3)
    t = fi / FPS
    txt(d, (W - 80, H - 110), f"T+{int(t // 60):02d}:{int(t % 60):02d}:{int((t % 1) * 30):02d}", F("vt", 44), WHITE,
        anchor="rm", stroke=3)


def zoom_field(a, z):
    if z <= 1.002 or not a.any():
        return a
    cw, ch = LW / z, LH / z
    im = Image.fromarray(a.astype(np.float32), mode="F")
    box = ((LW - cw) / 2, (LH - ch) / 2, (LW + cw) / 2, (LH + ch) / 2)
    return np.asarray(im.resize((LW, LH), Image.BILINEAR, box=box))


def render_frame(fi):
    t = fi / FPS
    b = T.beat_of(t)
    if b < 0 or b >= T.TOTAL_BEATS:
        return np.full((H, W, 3), BLACK, np.uint8).tobytes()
    cv, fx, hud = scene(b, fi)
    return finish(cv, fx, hud, fi, lambda d: always_hud(d, b, fi))


def finish(cv, fx, hud, fi, frame_hud=None):
    """Dither + palette + HUD + post FX -> raw rgb24 bytes."""
    rng = np.random.default_rng(fi * 7919)

    # camera punch happens *before* dithering so the Bayer grid stays screen-locked
    lum = np.clip(zoom_field(cv.lum, fx.zoom) + fx.flash, 0, 1)
    red = np.clip(zoom_field(cv.red, fx.zoom), 0, 1)
    if fx.redflash:
        red = np.maximum(red, 0.85)
    idx = (lum > THR).astype(np.uint8)
    idx[red > THR] = 2
    small = Image.fromarray(PAL[idx])
    img = small.resize((W, H), Image.NEAREST)
    d = ImageDraw.Draw(img)
    for h in hud:
        h(d, img)
    if frame_hud:
        frame_hud(d)

    a = np.asarray(img).copy()

    if fx.shake > 1.5:
        dx, dy = (np.round(rng.uniform(-1, 1, 2) * fx.shake / S) * S).astype(int)
        a = np.roll(a, (dy, dx), axis=(0, 1))
    for _ in range(fx.slices):
        y0 = int(rng.integers(0, H - 40))
        hh = int(rng.integers(6, 90))
        a[y0:y0 + hh] = np.roll(a[y0:y0 + hh], int(rng.integers(-120, 120)), axis=1)
    # chromatic split only on real hits: constant split turns every dither dot into chroma noise
    k = int(fx.rgb) // S * S if fx.rgb >= 7 else 0
    if k:
        a[:, :, 0] = np.roll(a[:, :, 0], k, axis=1)
        a[:, :, 2] = np.roll(a[:, :, 2], -k, axis=1)
    if fx.invert:
        a = 255 - a
    a = (a * POSTMUL).astype(np.uint8)

    if fx.crt:
        mode, k = fx.crt
        k = min(1.0, max(0.0, k))
        out = np.zeros_like(a)
        out[:] = BLACK
        if mode == "on":
            hh = max(3, int(H * (k ** 2)))
            ww = W if k > 0.15 else max(6, int(W * k / 0.15))
            y0, x0 = (H - hh) // 2, (W - ww) // 2
            src = Image.fromarray(a).resize((ww, hh), Image.NEAREST)
            out[y0:y0 + hh, x0:x0 + ww] = np.clip(np.asarray(src).astype(np.int16) + int(200 * (1 - k)), 0, 255)
        else:
            if k < 0.6:
                hh = max(4, int(H * (1 - k / 0.6) ** 2))
                src = Image.fromarray(a).resize((W, hh), Image.NEAREST)
                y0 = (H - hh) // 2
                out[y0:y0 + hh] = np.clip(np.asarray(src).astype(np.int16) + int(255 * k / 0.6), 0, 255)
            elif k < 1.0:
                ww = max(4, int(W * (1 - (k - 0.6) / 0.4)))
                x0 = (W - ww) // 2
                out[H // 2 - 3:H // 2 + 3, x0:x0 + ww] = 255
        a = out
    return a.tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="heaven_root.mp4")
    ap.add_argument("--audio", default=None, help="audio file (default: synthesize the original soundtrack)")
    ap.add_argument("--bpm", type=float, default=None, help="re-time visuals to this BPM (external audio)")
    ap.add_argument("--offset", type=float, default=0.0, help="seconds into the audio where beat 0 lands")
    ap.add_argument("--frames", default=None, help="render only a frame range a:b (preview)")
    ap.add_argument("--procs", type=int, default=os.cpu_count())
    ap.add_argument("--crf", type=int, default=28)
    ap.add_argument("--lossless", action="store_true", help="write a lossless master (huge)")
    args = ap.parse_args()
    T.configure(args.bpm)

    audio = args.audio
    if audio is None:
        import music
        audio = os.path.splitext(args.out)[0] + "_soundtrack.wav"
        music.write_wav(audio, music.build())
        print("synthesized", audio)

    n = int(T.duration() * FPS)
    a0, a1 = 0, n
    if args.frames:
        a0, a1 = (int(x) for x in args.frames.split(":"))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-ss", f"{a0 / FPS + args.offset:.3f}", "-i", audio,
        "-map", "0:v", "-map", "1:a",
        "-af", f"afade=t=out:st={max(0, (a1 - a0) / FPS - 0.9):.3f}:d=0.9",
        *(["-c:v", "libx264rgb", "-preset", "ultrafast", "-qp", "0"] if args.lossless else
          ["-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-tune", "animation"]),
        "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", args.out,
    ]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with Pool(args.procs) as pool:
        for i, fr in enumerate(pool.imap(render_frame, range(a0, a1), chunksize=4)):
            ff.stdin.write(fr)
            if i % 60 == 0:
                print(f"frame {a0 + i}/{a1}", file=sys.stderr, flush=True)
    ff.stdin.close()
    ff.wait()
    print("wrote", args.out)


if __name__ == "__main__":
    main()
