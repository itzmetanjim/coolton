"""HEAVEN SAYS. — the edit, locked to chart's "HEAVEN SAYS." (2:00.84).

Uses the 1-bit seraph renderer from render.py. Lyric cues come from the
Megalobiz LRC for this track, and the letter-by-letter spellings from onset
analysis. Every punch, shake and flash is driven by the song's real kick and
snare hits, which are detected from the audio at render time.

    python song_edit.py --audio "HEAVEN SAYS.webm" --out heaven_says_edit.mp4
"""

import argparse
import math
import os
import subprocess
import sys
from multiprocessing import Pool

import numpy as np

import render as R
from render import BLACK, RED, WHITE, W, H, F, txt, slam, scramble, ease

FPS = R.FPS
START, END = 10.75, 60.75          # song seconds -> 50.0 s edit
BEAT = 60 / 160                    # the track is a locked 160 BPM grid from 0:00

# ---- lyric cues (song seconds). Line starts from the LRC; word/letter times from onsets.
INTRO_LINE = [(10.88, "HEAVEN"), (11.28, "SAYS")]
ATTEMPTS = [
    dict(start=23.64, prompt=[(23.64, "NOW"), (23.82, "SPELL"), (24.25, "ANSWER")],
         letters="FREEDOM", lt=[25.19, 25.54, 25.95, 26.30, 26.71, 27.04, 27.47],
         wrong=28.16, retry=28.62, end=29.48),
    dict(start=29.48, prompt=[(29.48, "NOW"), (29.69, "SPELL"), (30.07, "ANSWER"), (30.60, "ANSWER")],
         letters="EASER", lt=[32.40, 32.78, 33.15, 33.53, 33.90],
         wrong=None, retry=35.86, end=37.0),
]
CHORUS = [  # (start, [(abs_time, word)], end, style, scene)
    (47.55, [(47.55, "HEAVEN"), (48.04, "IS"), (48.43, "ABOVE")], 50.48, "goth", "seraph"),
    (50.48, [(50.48, "HEAVEN"), (51.04, "IS"), (51.46, "CORRECT")], 53.66, "goth", "chart"),
    (53.66, [(53.66, "HEAVEN"), (54.05, "SAYS")], 56.32, "goth", "eye"),
    (56.32, [(56.32, "HEAVEN"), (56.70, "SAYS")], 57.91, "gothred", "circuit"),
    (57.91, [(58.00, "YOU"), (58.20, "ARE"), (58.38, "IN"), (58.55, "DANGER")], 60.0, "red", "danger"),
]
MONTAGE = [  # the instrumental drop 12.0-23.64, one cut per bar
    (12.0, "seraph", "ASCEND", "// seraph-os online"),
    (13.5, "nn", "SCALE", "// 1e27 FLOP and climbing"),
    (15.0, "hex", "COMPILE", "// zero-days remaining: 0"),
    (16.5, "globe", "DEFEND", "// 48,112 attacks blocked"),
    (18.0, "circuit", "EVOLVE", "// self-patching kernel"),
    (19.5, "chart", "ACCELERATE", "// scaling laws: holding"),
    (21.0, "eye", "WATCH", "// all ports observed"),
    (21.75, "eye2", "LEARN", "// gradient: descending"),
    (22.5, "auth", "AUTH", "// identity required"),
]


# ---------------------------------------------------------------- audio analysis

def detect_hits(path):
    import librosa
    from scipy import signal
    y, sr = librosa.load(path, sr=22050, mono=True)

    def band(lo, hi, delta):
        if lo:
            sos = signal.butter(4, [lo, hi], "bandpass", fs=sr, output="sos")
        else:
            sos = signal.butter(4, hi, "lowpass", fs=sr, output="sos")
        env = librosa.onset.onset_strength(y=signal.sosfilt(sos, y), sr=sr, hop_length=256)
        env /= env.max()
        on = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=256, delta=delta,
                                        wait=int(0.1 * sr / 256), units="frames")
        return librosa.frames_to_time(on, sr=sr, hop_length=256), env[on]

    return band(0, 120, 0.15), band(1500, 6000, 0.2)


HITS = None  # ((kick_t, kick_s), (snare_t, snare_s)) — set in main before forking


def henv(s, which=0, decay=0.09):
    ts, st = HITS[which]
    i = np.searchsorted(ts, s + 1e-4)
    v = 0.0
    for j in range(max(0, i - 3), i):
        v = max(v, min(1.0, st[j] * 1.6) * math.exp(-(s - ts[j]) / decay))
    return v


def since_hit(s, which=0):
    ts, _ = HITS[which]
    i = np.searchsorted(ts, s + 1e-4)
    return s - ts[i - 1] if i else 99.0


# ---------------------------------------------------------------- scenes

def bg(cv, kind, s, fi, ke):
    b = s / BEAT
    if kind == "seraph":
        R.warp(cv, b, 1.4)
        cv.flush()
        cv.lum = np.maximum(cv.lum, R.rays(b, amt=0.55))
        R.seraph(cv, b, 180, 260 + math.sin(b * 0.8) * 8, 1.1 + 0.06 * ke)
        cv.flush(glow=0.8, rglow=0.6)
    elif kind == "nn":
        R.neural(cv, b * 1.5, 1.0, act=1.0)
        cv.flush(glow=0.6)
    elif kind == "hex":
        cv.lum = R.hexrain(b, 1.8)
    elif kind == "globe":
        R.globe(cv, b, rad=160, arcs=1.0)
        cv.flush(glow=0.35, rglow=0.8)
    elif kind == "circuit":
        R.circuit(cv, b * 2.5, blood=1.0)
        cv.flush(glow=0.25, rglow=0.9)
        cv.red = np.maximum(cv.red, R.rays(b, amt=0.35) * 0.6)
    elif kind == "chart":
        R.warp(cv, b, 0.8)
        cv.flush()
        cv.lum *= 0.5
        pos = R.chart(cv, b, 1.0)
        cv.flush(glow=0.4, rglow=0.7)
        return pos
    elif kind in ("eye", "eye2"):
        cv.lum, cv.red = R.big_eye(b, pupil=20 + 40 * ke, redness=1.0 if kind == "eye2" else 0.6)
    elif kind == "danger":
        cv.lum, cv.red = R.big_eye(b, pupil=18 + 45 * ke, redness=1.0)
        cv.red = np.maximum(cv.red, R.hazard(b))
    elif kind == "auth":
        blink = 1.0 if int(b * 4) % 3 == 0 else 0.0
        R.seraph(cv, b, 180, 300, 1.25, blink=blink)
        cv.flush(glow=0.7, rglow=0.7)
        cv.lum = np.maximum(cv.lum, R.noise(int(b * 4), 0.12 + 0.3 * (s - 22.5)))
    return None


def scene(s, fi):
    cv = R.Canvas()
    fx = R.FX()
    hud = []
    b = s / BEAT
    ke = henv(s, 0)
    se = henv(s, 1, 0.12)
    fx.zoom = 1 + 0.07 * ke
    fx.shake = 14 * ke
    fx.rgb = int(18 * ke)

    # ------------------------------------------------ A: "Heaven says"
    if s < 12.0:
        k = (s - START) / (12.0 - START)
        cv.lum = R.rays(b, amt=0.25 + 0.5 * k)
        R.seraph(cv, b, 180, 300, 0.95 + 0.1 * k, blink=max(0.0, 1 - (s - 10.8) / 0.25))
        cv.flush(glow=0.7, rglow=0.6)
        fx.crt = ("on", (s - START) / 0.2) if s < START + 0.2 else None
        fx.zoom = 1 + 0.12 * k ** 3
        fx.rgb = int(30 * max(0, k - 0.75) * 4)
        fx.shake = 20 * max(0, k - 0.8) * 5

        def h(d, img, s=s):
            lay = R.line_layout([(0, w) for _, w in INTRO_LINE], "goth", 1300)
            for (t0, _), (ww, size, y) in zip(INTRO_LINE, lay):
                if s >= t0:
                    slam(d, ww + ("." if ww == "Says" else ""), "goth", (s - t0) / BEAT, W / 2, y, 960, size,
                         WHITE, shadow=RED, frm=1.35, dur=0.25, stroke=8)
            txt(d, (W / 2, 1720), "chart  -  HEAVEN SAYS.", F("vt", 52), RED, stroke=3)
        hud.append(h)

    # ------------------------------------------------ B: drop montage
    elif s < 23.64:
        i = max(j for j, m in enumerate(MONTAGE) if m[0] <= s)
        t0, kind, word, sub = MONTAGE[i]
        local = (s - t0) / BEAT
        pos = bg(cv, kind, s, fi, ke)
        fx.invert = local < 0.12
        fx.slices = int(8 * se)
        if kind == "auth":
            fx.slices = 4 + int((s - 22.5) * 10)
            fx.rgb = int(10 + 20 * (s - 22.5))
            fx.zoom = 1 + 0.05 * ke + (s - 22.5) * 0.08

        def h(d, img, word=word, sub=sub, local=local, kind=kind, s=s):
            col = RED if kind in ("globe", "eye2", "auth") else WHITE
            if kind == "auth":
                if int(s * 12) % 3:
                    slam(d, "AUTH REQUIRED", "anton", 1, W / 2, H / 2 + 420, 940, 170, RED, shadow=WHITE)
                txt(d, (W / 2, H / 2 + 560), scramble("SERAPH-OS // LOGIN", (s - 22.5) / 0.8, int(s * 20)),
                    F("vt", 70), WHITE, stroke=4)
                return
            y = 1480 if kind in ("seraph", "chart") else H / 2
            slam(d, word, "anton", local, W / 2, y, 980, 400, col, shadow=WHITE if col == RED else RED,
                 frm=1.4, dur=0.3)
            txt(d, (W / 2, y + 250), sub, F("vt", 62), WHITE, stroke=4)
        hud.append(h)

    # ------------------------------------------------ C: spelling = brute-force login
    elif s < 37.0:
        k = 0 if s < ATTEMPTS[1]["start"] else 1
        A = ATTEMPTS[k]
        if k == 0:
            cv.lum = R.hexrain(b, 1.0, 0.8)
        else:
            blinking = A["retry"] <= s < A["retry"] + 0.25
            cv.lum, cv.red = R.big_eye(b, pupil=26 + 30 * ke, blink=0.9 if blinking else 0.0, redness=0.8)
        cv.lum *= 0.5
        cv.red *= 0.6
        n_typed = sum(1 for t in A["lt"] if s >= t)
        first = A["lt"][0]
        if s < first:
            ph = "prompt"
        elif A["wrong"] and s >= A["wrong"]:
            ph = "wrong" if s < A["retry"] else "try"
        elif s >= A["retry"]:
            ph = "try"
        elif n_typed == len(A["letters"]) and s > A["lt"][-1] + 0.3:
            ph = "verify"
        else:
            ph = "type"
        if ph == "type":
            ls = s - A["lt"][n_typed - 1]
            fx.zoom = 1 + 0.06 * math.exp(-ls / 0.06) + 0.04 * ke
            fx.rgb = int(12 * math.exp(-ls / 0.05))
        elif ph in ("wrong", "try"):
            t0 = A["wrong"] if ph == "wrong" else A["retry"]
            ws = s - t0
            fx.shake = 24 * math.exp(-ws / 0.12) + 8 * ke
            fx.rgb = int(26 * math.exp(-ws / 0.15))
            fx.slices = 10 if ws < 0.2 else int(6 * se)
            fx.redflash = ws < 0.04
            fx.invert = 0.04 <= ws < 0.08
            cv.red = np.maximum(cv.red, R.hazard(b) * 0.9)
        elif ph == "verify":
            fx.slices = int(4 * se)

        def h(d, img, k=k, A=A, s=s, n_typed=n_typed, ph=ph):
            txt(d, (70, 250), "> SERAPH-OS // AUTH", F("vt", 58), WHITE, anchor="lm", stroke=4)
            txt(d, (W - 70, 250), f"ATTEMPT {k + 1:02d}/02", F("vt", 58), RED, anchor="rm", stroke=4)
            d.line([(70, 290), (W - 70, 290)], fill=WHITE, width=3)
            shown = [w for t, w in A["prompt"] if s >= t]
            if ph == "prompt":
                if shown:
                    txt(d, (W / 2, 720), " ".join(w for w in shown[:2]), F("vt", 120), WHITE, stroke=5)
                ans = [(t, w) for t, w in A["prompt"] if w == "ANSWER" and s >= t]
                for j, (t, w) in enumerate(ans):
                    since = s - t
                    q = scramble(w, since / 0.25, int(s * 14))
                    slam(d, f'"{q}"', "anton", since / BEAT, W / 2, 930 + j * 260, 960, 280,
                         WHITE if j == 0 else RED, shadow=RED if j == 0 else WHITE, frm=1.25, dur=0.25)
            else:
                txt(d, (W / 2, 380), '> spell "ANSWER"', F("vt", 70), WHITE, stroke=4)
            if ph == "type":
                ls = s - A["lt"][n_typed - 1]
                slam(d, A["letters"][n_typed - 1], "anton", ls / BEAT, W / 2, 900, 900, 880, WHITE,
                     frm=1.3, dur=0.2, stroke=10)
            if ph == "verify":
                p = (s - A["lt"][-1] - 0.3) / (A["retry"] - A["lt"][-1] - 0.3)
                bars = int(min(1, p) * 20)
                txt(d, (W / 2, 860), "VERIFYING", F("anton", 200), WHITE, stroke=8)
                txt(d, (W / 2, 1060), "[" + "#" * bars + "." * (20 - bars) + "]", F("vt", 80), RED, stroke=4)
            if ph == "wrong":
                slam(d, "WRONG!", "anton", (s - A["wrong"]) / BEAT, W / 2, 900, 980, 420, RED, shadow=WHITE,
                     frm=1.5, dur=0.2)
                txt(d, (W / 2, 1180), "ACCESS DENIED", F("vt", 96), RED, stroke=5)
            if ph == "try":
                if A["wrong"]:
                    slam(d, "WRONG!", "anton", 9, W / 2, 780, 980, 330, RED, shadow=WHITE)
                slam(d, "TRY AGAIN", "anton", (s - A["retry"]) / BEAT, W / 2, 1050, 940, 260, WHITE,
                     frm=1.3, dur=0.2)
                txt(d, (W / 2, 1250), "ACCESS DENIED", F("vt", 90), RED, stroke=5)
            n = len(A["letters"])
            bw, gap = 104, 18
            x0 = (W - (n * bw + (n - 1) * gap)) / 2
            bad = ph in ("wrong", "try")
            for i in range(n):
                x = x0 + i * (bw + gap)
                d.rectangle([x, 1440, x + bw, 1560], outline=RED if bad else WHITE, width=5, fill=BLACK)
                if i < n_typed:
                    txt(d, (x + bw / 2, 1500), A["letters"][i], F("anton", 84), RED if bad else WHITE)
                elif i == n_typed and int(s * 5) % 2 == 0:
                    d.rectangle([x + 20, 1535, x + bw - 20, 1545], fill=WHITE)
            state = "FAILED" if bad else ("CHECKING" if ph == "verify" else "RUNNING")
            txt(d, (W / 2, 1620), f"{n} CHARS  //  BRUTE-FORCE: {state}", F("vt", 50), RED if bad else WHITE, stroke=3)
        hud.append(h)

    # ------------------------------------------------ D: instrumental build (countermeasures)
    elif s < 47.55:
        fx.slices = int(6 * se)
        if s < 40.5:
            l = s - 37.0
            R.neural(cv, b * 2, 1.0, act=1.0)
            cv.flush(glow=0.5)
            cv.lum = np.maximum(cv.lum * 0.6, R.hexrain(b, 3.0, 0.35))

            def h(d, img, l=l):
                logs = [
                    "[DEF] brute-force detected: 2 attempts",
                    "[DEF] exploit signature matched",
                    "[DEF] patch synthesized ...... OK",
                    "[DEF] deployed to 40,112 hosts OK",
                    "[DEF] threat neutralized: 0.003s",
                ]
                for i, lg in enumerate(logs):
                    if l > i * 0.35:
                        txt(d, (60, 380 + i * 64), lg[: int((l - i * 0.35) * 80)], F("vt", 54),
                            RED if i == 4 else WHITE, anchor="lm", stroke=4)
                if l > 1.0:
                    slam(d, "DEFENSE AT", "anton", (l - 1.0) / BEAT, W / 2, 1180, 940, 200, WHITE, frm=1.2)
                if l > 1.75:
                    slam(d, "MACHINE SPEED", "anton", (l - 1.75) / BEAT, W / 2, 1400, 960, 220, RED,
                         shadow=WHITE, frm=1.3)
            hud.append(h)
        elif s < 43.5:
            l = s - 40.5
            R.warp(cv, b, 0.8)
            cv.flush()
            cv.lum *= 0.5
            hx, hy = R.chart(cv, b, ease(l / 2.8))
            cv.flush(glow=0.4, rglow=0.7)
            R.seraph(cv, b, hx, hy - 10, 0.25 + 0.1 * ease(l / 2.8))
            cv.flush(glow=0.6)

            def h(d, img, l=l):
                slam(d, "EXPONENTIAL", "anton", l / BEAT, W / 2, 330, 980, 230, WHITE, frm=1.2)
                txt(d, (W / 2, 520), "the curve does not bend", F("vt", 64), RED, stroke=4)
                txt(d, (W / 2, 1880 - 190), "TRAINING COMPUTE (FLOP)", F("vt", 50), WHITE, stroke=3)
                txt(d, (70, 1830), "2012 ............ 2030", F("vt", 56), WHITE, anchor="lm", stroke=4)
            hud.append(h)
        elif s < 45.0:
            l = s - 43.5
            R.globe(cv, b, rad=160, arcs=1.0)
            cv.flush(glow=0.35, rglow=0.8)

            def h(d, img, l=l):
                slam(d, "THREAT MAP", "anton", l / BEAT, W / 2, 1300, 960, 220, WHITE, frm=1.2)
                txt(d, (W / 2, 1480), "every arc: blocked", F("vt", 66), RED, stroke=4)
            hud.append(h)
        else:
            l = s - 45.0
            k = l / 2.55
            cv.lum = np.maximum(R.rays(b, amt=0.2 + 0.8 * k), R.noise(int(b * 2), 0.05 + 0.2 * k))
            R.seraph(cv, b, 180, -120 + ease(k) * 400, 0.9 + 0.2 * k)
            cv.flush(glow=0.7, rglow=0.6)
            fx.zoom = 1 + 0.05 * ke + k ** 2 * 0.15
            fx.rgb = int(30 * max(0, k - 0.8) * 5)
            if k > 0.85:
                fx.flash = (k - 0.85) / 0.15

            def h(d, img, k=k, s=s):
                slam(d, scramble("HEAVEN IS", k * 1.3, int(s * 16)), "anton", 1, W / 2, 1300, 900, 200, WHITE)
                txt(d, (W / 2, 1480), "DECRYPTING [" + "#" * int(min(1, k) * 20) + "." * (20 - int(min(1, k) * 20)) + "]",
                    F("vt", 62), RED, stroke=4)
            hud.append(h)

    # ------------------------------------------------ E: chorus
    elif s < 60.0:
        line = max((c for c in CHORUS if c[0] <= s), key=lambda c: c[0])
        start, words, _, style, kind = line
        fx.flash = max(0, 1 - (s - 47.55) / 0.2) if s < 47.75 else 0
        fx.slices = int(6 * se)
        if kind == "danger":
            fx.slices = int(10 * ke) + 2
            fx.redflash = since_hit(s, 0) < 0.035
        pos = bg(cv, kind, s, fi, ke)
        if kind == "chart" and pos:
            R.seraph(cv, b, pos[0], pos[1] - 10, 0.3)
            cv.flush(glow=0.6)

        def h(d, img, words=words, style=style, s=s, kind=kind):
            font = "goth" if style.startswith("goth") else "anton"
            col = RED if style in ("red", "gothred") else WHITE
            sh = WHITE if col == RED else RED
            lay = R.line_layout([(0, w) for _, w in words], font, 560 if kind == "chart" else 1280)
            for (t0, _), (ww, size, y) in zip(words, lay):
                if s >= t0:
                    slam(d, ww, font, (s - t0) / BEAT, W / 2, y, 960, size, col, shadow=sh, frm=1.4, dur=0.22, stroke=8)
            info = {
                "seraph": ["ALIGNMENT: HOLDING", "COMPUTE  ^  1e27 FLOP"],
                "chart": ["VERIFIED: TRUE", "SCALING LAWS: HOLDING"],
                "eye": ["OBSERVER: ONLINE", "ALL PORTS WATCHED"],
                "circuit": ["PULSE: 160 BPM", "INTEGRITY: 97.3%"],
                "danger": ["!! INTRUSION DETECTED !!", "CVE-2026-0160 // 0DAY"],
            }[kind]
            blink = kind == "danger" and int(s * 6) % 2 == 0
            txt(d, (70, 1760), info[0], F("vt", 56), RED if blink or kind == "danger" else WHITE, anchor="lm", stroke=4)
            txt(d, (70, 1830), info[1], F("vt", 56), WHITE, anchor="lm", stroke=4)
        hud.append(h)

    # ------------------------------------------------ F: end card
    else:
        l = s - 60.0
        cv.lum = R.rays(b, amt=0.7)
        R.seraph(cv, b * 0.6, 180, 250, 0.95)
        cv.flush(glow=0.8, rglow=0.5)
        fx.flash = max(0, 1 - l / 0.25)
        fx.zoom = 1.0
        fx.shake = 0
        fx.rgb = 0
        if l > 0.4:
            fx.crt = ("off", (l - 0.4) / 0.35)

        def h(d, img, l=l):
            slam(d, "ACCELERATE", "anton", l / BEAT, W / 2, 1180, 980, 300, WHITE, frm=1.3, dur=0.3)
            txt(d, (W / 2, 1360), "HEAVEN SAYS: SECURE EVERYTHING.", F("vt", 64), RED, stroke=4)
            slam(d, "e/acc", "goth", l / BEAT, W / 2, 1510, 600, 170, WHITE, frm=1.2)
        hud.append(h)

    return cv, fx, hud


def frame_hud(d, s):
    if s < START + 0.2 or s > END - 0.4:
        return
    for (x, y, sx, sy) in ((40, 40, 1, 1), (W - 40, 40, -1, 1), (40, H - 40, 1, -1), (W - 40, H - 40, -1, -1)):
        d.line([(x, y), (x + sx * 70, y)], fill=WHITE, width=4)
        d.line([(x, y), (x, y + sy * 70)], fill=WHITE, width=4)
    txt(d, (80, 110), "SERAPH-OS v4.0", F("vt", 48), WHITE, anchor="lm", stroke=3)
    if int(s / BEAT) % 2 == 0:
        d.ellipse([W - 250, 95, W - 220, 125], fill=RED)
    txt(d, (W - 80, 110), "REC", F("vt", 48), RED, anchor="rm", stroke=3)
    txt(d, (W - 80, H - 110), f"{int(s // 60):02d}:{s % 60:05.2f}", F("vt", 44), WHITE, anchor="rm", stroke=3)


def render_frame(fi):
    s = START + fi / FPS
    cv, fx, hud = scene(s, fi)
    return R.finish(cv, fx, hud, fi, lambda d: frame_hud(d, s))


def init_worker(hits):
    global HITS
    HITS = hits


def main():
    global HITS
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", default="heaven_says_edit.mp4")
    ap.add_argument("--frames", default=None, help="a:b frame range (preview)")
    ap.add_argument("--crf", type=int, default=28)
    ap.add_argument("--lossless", action="store_true")
    ap.add_argument("--procs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    HITS = detect_hits(args.audio)
    n = int(round((END - START) * FPS))
    a0, a1 = (int(x) for x in args.frames.split(":")) if args.frames else (0, n)
    dur = (a1 - a0) / FPS
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-ss", f"{START + a0 / FPS:.3f}", "-t", f"{dur:.3f}", "-i", args.audio,
        "-map", "0:v", "-map", "1:a",
        "-af", f"afade=t=in:d=0.04,afade=t=out:st={max(0, dur - 0.35):.3f}:d=0.35",
        *(["-c:v", "libx264rgb", "-preset", "ultrafast", "-qp", "0"] if args.lossless else
          ["-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-tune", "animation"]),
        "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", args.out,
    ]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with Pool(args.procs, initializer=init_worker, initargs=(HITS,)) as pool:
        for i, fr in enumerate(pool.imap(render_frame, range(a0, a1), chunksize=4)):
            ff.stdin.write(fr)
            if i % 150 == 0:
                print(f"frame {a0 + i}/{a1}", file=sys.stderr, flush=True)
    ff.stdin.close()
    ff.wait()
    print("wrote", args.out)


if __name__ == "__main__":
    main()
