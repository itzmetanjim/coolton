"""Original soundtrack for the HEAVEN//ROOT edit, synthesized from scratch.

Dark drift-phonk / industrial at 140 BPM in F minor: distorted 808s,
pitched cowbell lead, angelic formant choir, glitch SFX laid on the
same beat grid the video uses (see timeline.py).

    python music.py out.wav
"""

import sys
import wave

import numpy as np
from scipy import signal

import timeline as T

SR = 44100
rng = np.random.default_rng(7)


def nf(name):
    """Note name -> Hz (e.g. 'F1', 'Db2', 'C#5')."""
    names = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    n = names[name[0]]
    i = 1
    if name[i] in "#b":
        n += 1 if name[i] == "#" else -1
        i += 1
    octv = int(name[i:])
    midi = 12 * (octv + 1) + n
    return 440.0 * 2 ** ((midi - 69) / 12)


def tt(dur):
    return np.arange(int(dur * SR)) / SR


def bp(x, lo, hi, order=2):
    sos = signal.butter(order, [lo, hi], "bandpass", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def hp(x, f, order=2):
    sos = signal.butter(order, f, "highpass", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def lp(x, f, order=2):
    sos = signal.butter(order, f, "lowpass", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def saw(ph):
    return 2.0 * (ph - np.floor(ph + 0.5))


def sq(ph):
    return np.sign(np.sin(2 * np.pi * ph))


# ---------------------------------------------------------------- instruments

def kick(hard=1.0, low=45.0):
    t = tt(0.55)
    f = low + 140 * np.exp(-t * 28)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t * 5.5)
    click = hp(rng.standard_normal(len(t)), 2000) * np.exp(-t * 350) * 0.35
    return np.tanh((body + click) * (2.2 * hard)) * 0.9


def heartbeat():
    t = tt(0.35)
    f = 38 + 50 * np.exp(-t * 20)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.tanh(np.sin(ph) * np.exp(-t * 9) * 1.6)


def snare():
    t = tt(0.45)
    n = bp(rng.standard_normal(len(t)), 900, 9000) * np.exp(-t * 16)
    tone = np.sin(2 * np.pi * 185 * t) * np.exp(-t * 28) * 0.7
    # clap: three quick bursts
    clap = np.zeros_like(t)
    for d in (0.0, 0.011, 0.023):
        k = int(d * SR)
        clap[k:] += bp(rng.standard_normal(len(t) - k), 1000, 3500) * np.exp(-t[: len(t) - k] * 60)
    return np.tanh((n + tone + clap * 0.8) * 1.8) * 0.75


def hat(opn=False):
    t = tt(0.35 if opn else 0.07)
    n = hp(rng.standard_normal(len(t)), 7500, 4)
    return n * np.exp(-t * (11 if opn else 90)) * 0.5


def bass808(freq, dur, slide_from=None):
    t = tt(dur)
    f = np.full_like(t, freq)
    if slide_from:
        f = freq + (slide_from - freq) * np.exp(-t * 18)
    ph = 2 * np.pi * np.cumsum(f) / SR
    env = np.minimum(1, t / 0.004) * np.exp(-t * 0.9)
    rel = np.clip((dur - t) / 0.03, 0, 1)
    x = np.sin(ph) * env * rel
    x = np.tanh(x * 4.5) * 0.8 + np.sin(2 * ph) * env * rel * 0.12
    return x


def cowbell(freq, dur=0.32):
    t = tt(dur)
    x = sq(freq * t) * 0.6 + sq(freq * 1.4836 * t) * 0.4
    x = bp(x, freq * 0.8, min(freq * 6, 16000), 2)
    env = np.exp(-t * 11) * 0.8 + np.exp(-t * 70) * 0.6
    return np.tanh(x * env * 2.5) * 0.55


FORMANTS_AH = [(800, 80), (1150, 90), (2900, 120), (3900, 130)]


def choir(freqs, dur, att=0.6, rel=1.2, bright=1.0):
    t = tt(dur + rel)
    x = np.zeros_like(t)
    for f0 in freqs:
        for d in (-0.14, -0.06, 0.0, 0.07, 0.13):
            vib = 1 + 0.004 * np.sin(2 * np.pi * (5.2 + d) * t + rng.random() * 6)
            ph = np.cumsum(f0 * (1 + d / 100) * vib) / SR + rng.random()
            x += saw(ph)
    y = np.zeros_like(x)
    for fc, bw in FORMANTS_AH:
        b, a = signal.iirpeak(fc, fc / bw, fs=SR)
        y += signal.lfilter(b, a, x) * (1.0 if fc < 3000 else 0.5 * bright)
    y += lp(x, 600) * 0.25
    env = np.minimum(1, t / att) * np.clip((dur + rel - t) / rel, 0, 1)
    y = y * env
    return y / (np.abs(y).max() + 1e-9) * 0.5


def riser(dur):
    t = tt(dur)
    n = rng.standard_normal(len(t))
    out = np.zeros_like(n)
    chunks = 40
    edges = np.linspace(0, len(t), chunks + 1).astype(int)
    for i in range(chunks):
        a, b = edges[i], edges[i + 1]
        fc = 300 * (40 ** (i / chunks))
        out[a:b] = bp(n[a:b], fc * 0.6, min(fc * 1.8, 19000))
    sweep = np.sin(2 * np.pi * np.cumsum(200 * 8 ** (t / dur)) / SR) * 0.25
    env = (t / dur) ** 2.2
    return (out * 0.7 + sweep) * env


def impact(size=1.0):
    t = tt(3.0)
    f = 30 + 70 * np.exp(-t * 6)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.6)
    crash = lp(rng.standard_normal(len(t)), 6000) * np.exp(-t * 2.5) * 0.5
    crack = hp(rng.standard_normal(len(t)), 3000) * np.exp(-t * 40) * 0.7
    return np.tanh((boom * 1.4 + crash + crack) * 1.5 * size) * 0.9


def glass(dur=1.2):
    t = tt(dur)
    x = np.zeros_like(t)
    for _ in range(40):
        f = rng.uniform(2500, 9000)
        st = rng.uniform(0, 0.25)
        k = int(st * SR)
        tt_ = t[: len(t) - k]
        x[k:] += np.sin(2 * np.pi * f * tt_) * np.exp(-tt_ * rng.uniform(8, 30)) * rng.uniform(0.2, 1)
    x += hp(rng.standard_normal(len(t)), 4000) * np.exp(-t * 12)
    return x / np.abs(x).max() * 0.6


def blip(freq, dur=0.07):
    t = tt(dur)
    x = sq(freq * t) * np.exp(-t * 35)
    x += sq(freq * 2.01 * t) * np.exp(-t * 60) * 0.4
    return bitcrush(x, 6) * 0.35


def buzzer(dur=0.42):
    t = tt(dur)
    x = sq(98 * t) + sq(103.5 * t) + sq(147 * t) * 0.5
    x = lp(x, 3000) * np.clip((dur - t) / 0.02, 0, 1)
    return bitcrush(np.tanh(x * 1.5), 5) * 0.45


def chime(freqs, step=0.07, dur=1.5):
    t = tt(dur + step * len(freqs))
    x = np.zeros_like(t)
    for i, f in enumerate(freqs):
        k = int(i * step * SR)
        tl = t[: len(t) - k]
        x[k:] += (np.sin(2 * np.pi * f * tl) + 0.3 * np.sin(2 * np.pi * f * 3.01 * tl)) * np.exp(-tl * 3)
    return x / np.abs(x).max() * 0.35


def beep(freq=1000, dur=0.12):
    t = tt(dur)
    return np.sin(2 * np.pi * freq * t) * np.clip((dur - t) / 0.01, 0, 1) * np.minimum(1, t / 0.005) * 0.3


def subdrop(dur=2.0):
    t = tt(dur)
    f = 25 + 60 * np.exp(-t * 2.2)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.1) * 0.9


def bitcrush(x, bits=6, down=1):
    q = 2 ** bits
    y = np.round(x * q) / q
    if down > 1:
        y = np.repeat(y[::down], down)[: len(x)]
    return y


# ---------------------------------------------------------------- mixing helpers

class Bus:
    def __init__(self, n):
        self.buf = np.zeros((n, 2))

    def add(self, sig, t, gain=1.0, pan=0.0):
        k = int(round(t * SR))
        if k >= len(self.buf) or k + len(sig) <= 0:
            return
        if k < 0:
            sig = sig[-k:]
            k = 0
        sig = sig[: len(self.buf) - k]
        l = np.cos((pan + 1) * np.pi / 4) * 1.4142
        r = np.sin((pan + 1) * np.pi / 4) * 1.4142
        self.buf[k : k + len(sig), 0] += sig * gain * l
        self.buf[k : k + len(sig), 1] += sig * gain * r


def reverb(x, decay=1.8, pre=0.02, damp=5000):
    n = int(decay * 1.5 * SR)
    t = np.arange(n) / SR
    out = np.zeros_like(x)
    for ch in range(2):
        ir = rng.standard_normal(n) * np.exp(-t * 6.9 / decay)
        ir = lp(ir, damp)
        ir[: int(pre * SR)] = 0
        ir /= np.sqrt((ir ** 2).sum())
        out[:, ch] = signal.fftconvolve(x[:, ch], ir)[: len(x)]
    return out


def B(beat):
    return T.t_of(beat)


# ---------------------------------------------------------------- arrangement

CHORDS = {  # F minor: i  VI  iv  V
    "Fm": ["F3", "Ab3", "C4", "F4"],
    "Db": ["Db3", "F3", "Ab3", "Db4"],
    "Bbm": ["Bb2", "Db3", "F3", "Bb3"],
    "C": ["C3", "E3", "G3", "C4"],
}
PROG = ["Fm", "Db", "Bbm", "C"]
ROOTS808 = {"Fm": "F1", "Db": "Db2", "Bbm": "Bb1", "C": "C2"}

# 2-bar cowbell lead (16th steps, note)
COWBELL = [
    (0, "F5"), (3, "F5"), (6, "Ab5"), (8, "G5"), (10, "F5"), (12, "Eb5"), (14, "F5"),
    (16, "C6"), (19, "Ab5"), (22, "G5"), (24, "F5"), (27, "Eb5"), (28, "C5"), (30, "Eb5"),
]
MINOR = ["F", "G", "Ab", "Bb", "C", "Db", "Eb"]


def build():
    n = int(T.duration() * SR) + SR
    drums, bass, lead, pad, fx, send = (Bus(n) for _ in range(6))
    kicks = T.kick_beats()

    # ---- intro
    for b in (0, 1, 2, 3):
        fx.add(heartbeat(), B(b * 1.0 * 1), 0.9)
        fx.add(heartbeat() * 0.7, B(b + 0.3), 0.6)
    pad.add(choir([nf(n_) for n_ in CHORDS["Fm"]], 2 * T.BAR, att=2.0), B(0), 0.7)
    pad.add(choir([nf(n_) for n_ in CHORDS["Db"]], T.BAR, att=0.3), B(8), 0.7)
    pad.add(choir([nf(n_) for n_ in CHORDS["C"] + ["E5", "G5"]], T.BAR, att=0.05, bright=1.6), B(12), 0.9)
    for b in (4, 6, 8, 10):
        drums.add(kick(1.3, 40), B(b), 0.9)
        fx.add(impact(0.6), B(b), 0.35)
        send.add(hp(impact(0.5), 400), B(b), 0.25)
    for b in (4, 6, 8):
        fx.add(blip(nf("C6")), B(b), 0.6)
    fx.add(buzzer(0.3), B(10), 0.6)
    fx.add(impact(1.2), B(12), 0.8)
    drums.add(kick(1.5, 38), B(12), 1.0)
    fx.add(glass(1.6), B(14), 0.7, 0.2)
    send.add(glass(1.6), B(14), 0.4)
    drums.add(kick(1.2, 42), B(14), 0.9)
    fx.add(riser(2 * T.BEAT), B(14), 0.5)
    for i, b in enumerate(np.arange(15, 16, 0.25)):
        drums.add(snare(), B(b), 0.25 + i * 0.12)

    # ---- spelling (half-time)
    for bar in range(4, 12):
        s = bar * 4
        ch = PROG[(bar - 4) % 4]
        bass.add(bass808(nf(ROOTS808[ch]), 2.4 * T.BEAT), B(s), 0.9)
        bass.add(bass808(nf(ROOTS808[ch]), 1.4 * T.BEAT, slide_from=nf(ROOTS808[ch]) * 2), B(s + 2.5), 0.8)
        pad.add(choir([nf(n_) for n_ in CHORDS[ch]], T.BAR * 0.95, att=0.15, rel=0.6), B(s), 0.28)
        for e in range(8):
            drums.add(hat(), B(s + e * 0.5), 0.35 if e % 2 else 0.5, 0.3)
        # sparse cowbell answer
        for st, note in COWBELL[:7] if bar % 2 == 0 else []:
            if st >= 8:
                lead.add(cowbell(nf(note)), B(s + st / 4), 0.35, -0.2)
    for k, (u, word, letters) in enumerate(T.SPELLS):
        fx.add(chime([nf("C6"), nf("G6")], 0.05, 0.4), B(u), 0.5)
        for i, ch in enumerate(letters):
            f = nf(MINOR[(ord(ch) * 3) % 7] + "6")
            fx.add(blip(f), B(u + T.LETTER_START + i * T.LETTER_STEP), 0.8, (-0.3, 0.3)[i % 2])
        fx.add(buzzer(), B(u + T.WRONG_AT), 0.9)
        fx.add(impact(0.5), B(u + T.WRONG_AT), 0.3)
        fx.add(blip(nf("F4"), 0.12), B(u + T.TRY_AT), 0.6)
    for b in [x for x in kicks if 16 <= x < 48]:
        drums.add(kick(1.1), B(b), 0.95)
    for b in [x for x in T.snare_beats() if 16 <= x < 48]:
        drums.add(snare(), B(b), 0.8)
        send.add(snare(), B(b), 0.35)

    # ---- reveal: drop out, decrypt, Y O U
    pad.add(choir([nf(n_) for n_ in CHORDS["Bbm"]], T.BAR, att=0.4), B(48), 0.4)
    fx.add(riser(4 * T.BEAT), B(48), 0.7)
    for i in range(16):
        fx.add(blip(nf(MINOR[i % 7] + "7"), 0.03), B(48 + i * 0.25), 0.3, (-0.6, 0.6)[i % 2])
    for b in (52, 53, 54):
        drums.add(kick(1.6, 36), B(b), 1.0)
        bass.add(bass808(nf("C2"), 0.9 * T.BEAT, slide_from=nf("C3")), B(b), 0.9)
        fx.add(impact(0.9), B(b), 0.6)
        send.add(snare(), B(b), 0.4)
    rev = choir([nf("C4"), nf("E4"), nf("G4")], T.BEAT, att=0.9, rel=0.02, bright=2)[::-1]
    fx.add(rev, B(55) - 0.0, 0.8)
    fx.add(chime([nf(x) for x in ("C5", "E5", "G5", "C6", "E6")], 0.04, 0.8), B(55), 0.5)

    # ---- chorus: full phonk
    fx.add(impact(1.4), B(56), 1.0)
    fx.add(subdrop(), B(56), 0.8)
    for bar in range(14, 22):
        s = bar * 4
        ch = PROG[(bar - 14) % 4]
        pad.add(choir([nf(n_) for n_ in CHORDS[ch]] + [nf(CHORDS[ch][1][:-1] + "5")], T.BAR * 0.98, att=0.05, rel=0.5, bright=1.3), B(s), 0.42)
        root = nf(ROOTS808[ch])
        bass.add(bass808(root, 1.4 * T.BEAT), B(s), 1.0)
        bass.add(bass808(root, 0.9 * T.BEAT, slide_from=root * 1.5), B(s + 1.5), 0.9)
        bass.add(bass808(root * (2 if bar % 2 else 1), 1.4 * T.BEAT, slide_from=root * 0.75), B(s + 2.5), 0.95)
        half = (bar - 14) % 2
        for st, note in COWBELL:
            if (st >= 16) == bool(half):
                lead.add(cowbell(nf(note)), B(s + (st % 16) / 4), 0.6, -0.15)
                send.add(cowbell(nf(note)), B(s + (st % 16) / 4), 0.18)
        for e in range(8):
            drums.add(hat(), B(s + e * 0.5), 0.45 if e % 2 else 0.6, 0.35)
        if bar % 2 == 1:  # trap triplet roll
            for j in range(6):
                drums.add(hat(), B(s + 3 + j / 6), 0.3 + j * 0.05, 0.35)
        else:
            drums.add(hat(True), B(s + 3.5), 0.35, -0.3)
    for b in [x for x in kicks if 56 <= x < 88]:
        drums.add(kick(1.2), B(b), 1.0)
    for b in [x for x in T.snare_beats() if 56 <= x < 88]:
        drums.add(snare(), B(b), 0.9)
        send.add(snare(), B(b), 0.3)
    for b in (72, 74, 80, 82):
        fx.add(buzzer(0.25), B(b), 0.35)
    fx.add(glass(1.0), B(76), 0.4)
    fx.add(riser(2 * T.BEAT), B(86), 0.6)
    for i, b in enumerate(np.arange(87, 88, 0.125)):
        drums.add(snare(), B(b), 0.2 + i * 0.07)

    # ---- control: industrial four-on-floor
    fx.add(impact(1.2), B(88), 0.9)
    for bar in range(22, 26):
        s = bar * 4
        ch = PROG[(bar - 22) % 4] if bar < 25 else "C"
        pad.add(choir([nf(n_) for n_ in CHORDS[ch]], T.BAR * 0.98, att=0.05, rel=0.4, bright=1.6), B(s), 0.38)
        for e in range(8):
            drums.add(hat(e % 2 == 1), B(s + e * 0.5), 0.3 if e % 2 else 0.5, 0.3)
        if bar < 25:
            for st, note in COWBELL[:7] if bar % 2 == 0 else COWBELL[7:]:
                lead.add(cowbell(nf(note)), B(s + (st % 16) / 4), 0.55, -0.15)
    for line in T.LINES[5:]:
        root = nf("F1") if line[0] != 96 else nf("Bb1")
        for off, _ in line[1]:
            bass.add(bass808(root * (1.5 if off == 3 else 1), 0.9 * T.BEAT, slide_from=root * 2), B(line[0] + off), 0.95)
    for b in range(88, 100):
        drums.add(kick(1.5), B(b), 1.0)
    for b in [x for x in T.snare_beats() if 88 <= x < 100]:
        drums.add(snare(), B(b), 1.0)
    for i, b in enumerate(T.CONTROL_HITS):
        drums.add(kick(1.8, 38), B(b), 1.0)
        fx.add(impact(0.8 + i * 0.2), B(b), 0.5 + i * 0.1)
        bass.add(bass808(nf("C2") * (1, 1.189, 1.335, 1.498)[i], 0.9 * T.BEAT, slide_from=nf("C3")), B(b), 1.0)
        fx.add(buzzer(0.2), B(b), 0.25 + i * 0.1)
    for i, b in enumerate(np.arange(103.5, 104, 1 / 16)):
        drums.add(snare(), B(b), 0.4 + i * 0.07)

    # ---- outro
    fx.add(impact(1.6), B(104), 1.1)
    fx.add(subdrop(3.0), B(104), 1.0)
    drums.add(kick(2.0, 34), B(104), 1.0)
    pad.add(choir([nf(n_) for n_ in CHORDS["Fm"]] + [nf("C5"), nf("F5")], 1.8 * T.BAR, att=0.02, rel=2.0, bright=1.5), B(104), 0.7)
    fx.add(chime([nf(x) for x in ("F5", "Ab5", "C6", "F6")], 0.09, 1.2), B(106), 0.35)
    for b in (108, 109):
        fx.add(beep(988, 0.1), B(b), 0.7)
    fx.add(beep(988, 1.6 * T.BAR / 4 * 3), B(110), 0.6)

    # ---- mix
    kick_t = np.array([B(b) for b in kicks])
    t = np.arange(n) / SR
    duck = np.ones(n)
    for kt in kick_t:
        k = int(kt * SR)
        m = min(n - k, int(0.35 * SR))
        if m > 0:
            duck[k : k + m] = np.minimum(duck[k : k + m], 1 - 0.65 * np.exp(-t[:m] / 0.09))
    duck = duck[:, None]
    wet = reverb(send.buf + pad.buf * 0.35 + lead.buf * 0.25, decay=2.2)
    mix = (
        drums.buf * 0.9
        + bass.buf * 0.45
        + lead.buf * 1.4 * duck
        + pad.buf * 1.2 * duck
        + fx.buf * 0.7
        + wet * 0.6 * duck
    )
    # glitch stutters on WRONG / CONTROL moments (repeat a 1/16 slice)
    for u, _, _ in T.SPELLS:
        stutter(mix, B(u + T.WRONG_AT), T.BEAT / 4, 3)
    stutter(mix, B(103.5), T.BEAT / 8, 3)
    # tape-stop feel: fade to silence at the end
    end = int(B(T.TOTAL_BEATS) * SR)
    mix[end:] = 0
    fade = int(0.15 * SR)
    mix[end - fade : end] *= np.linspace(1, 0, fade)[:, None]
    mix = hp(mix.T, 25).T
    mix /= np.percentile(np.abs(mix), 99.95) + 1e-9
    mix = np.tanh(mix * 1.25)
    mix /= np.abs(mix).max() + 1e-9
    mix *= 0.93
    return mix[: int(T.duration() * SR)]


def stutter(mix, t0, slice_len, reps):
    a = int(t0 * SR)
    L = int(slice_len * SR)
    seg = mix[a : a + L].copy()
    win = np.ones(L)
    f = int(0.003 * SR)
    win[:f] = np.linspace(0, 1, f)
    win[-f:] = np.linspace(1, 0, f)
    for r in range(1, reps + 1):
        mix[a + r * L : a + (r + 1) * L] = seg * win[:, None]


def write_wav(path, x):
    y = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(y.tobytes())


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "soundtrack.wav"
    write_wav(out, build())
    print("wrote", out, f"{T.duration():.2f}s")
