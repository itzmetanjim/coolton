"""Shared timeline for the HEAVEN//ROOT edit.

Everything (audio + video) is laid out in *beats* so the visuals can be
re-timed onto a different track by changing BPM / OFFSET only.
"""

BPM = 140.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
OFFSET = 0.0          # seconds of audio before beat 0 (for external tracks)
TOTAL_BEATS = 112     # 28 bars = 48.0 s @ 140
TAIL = 0.8            # black tail after the last beat (seconds)

# Sections (beats)
INTRO = (0, 16)
SPELL = (16, 48)
REVEAL = (48, 56)
CHORUS = (56, 88)
CONTROL = (88, 104)
OUTRO = (104, 112)

# Spelling quiz rendered as a password-cracking sequence.
# (start_beat, prompt word, attempted letters)
SPELLS = [
    (16, "ANSWER", "FREEDOM"),
    (24, "MANKIND", "DISEASE"),
    (32, "ANSWER", "VIOLENCE"),
    (40, "SIN", "FRIENDS"),
]
LETTER_START = 1.5     # beats after unit start
LETTER_STEP = 0.5
WRONG_AT = 6.0         # beats after unit start
TRY_AT = 7.0

# Big kinetic words: (beat, text, style)
# style: 'white', 'red', 'goth' (blackletter), 'gothred', 'mono'
WORDS = [
    # verse 1
    (4, "MACHINE", "white"),
    (6, "LEARN", "white"),
    (8, "SCHOOL", "white"),
    (10, "TERROR", "red"),
    (12, "Angel", "goth"),
    (14, "Above", "goth"),
    # reveal
    (52, "Y", "red"),
    (53, "O", "red"),
    (54, "U", "red"),
]

# Chorus / control lines: (beat, [(word_offset_beats, word)], end_beat, style)
LINES = [
    (56, [(0, "HEAVEN"), (1.5, "IS"), (2, "ABOVE")], 64, "goth"),
    (64, [(0, "HEAVEN"), (1, "IS"), (1.5, "THE"), (2, "ANSWER")], 72, "goth"),
    (72, [(0, "LIFE"), (1, "IS"), (2, "TERROR")], 76, "red"),
    (76, [(0, "BLOOD"), (1, "IN"), (1.5, "THE"), (2, "MACHINE")], 80, "gothred"),
    (80, [(0, "YOU"), (1, "ARE"), (1.5, "IN"), (2, "DANGER")], 84, "red"),
    (88, [(0, "TAKE"), (1, "BACK"), (2, "YOUR"), (3, "CONTROL")], 92, "white"),
    (92, [(0, "TAKE"), (1, "BACK"), (2, "YOUR"), (3, "CONTROL")], 96, "white"),
    (96, [(0, "TAKE"), (1, "BACK"), (2, "YOUR"), (3, "CONTROL")], 100, "red"),
]

CONTROL_HITS = [100, 101, 102, 103]


def t_of(beat):
    return OFFSET + beat * BEAT


def beat_of(t):
    return (t - OFFSET) / BEAT


def duration():
    return OFFSET + TOTAL_BEATS * BEAT + TAIL


def configure(bpm=None, offset=None):
    """Re-time everything (used when syncing to an external track)."""
    global BPM, BEAT, BAR, OFFSET
    if bpm:
        BPM = float(bpm)
        BEAT = 60.0 / BPM
        BAR = 4 * BEAT
    if offset is not None:
        OFFSET = float(offset)


# Kick grid used by both music and video (beats).
def kick_beats():
    ks = []
    # intro: heartbeat "lub-dub" + word impacts
    for b in (0, 2, 4, 6, 8, 10, 12, 14):
        ks.append(b)
    # spelling: half-time (steps 0 and 10 of a 16-step bar)
    for bar in range(4, 12):
        s = bar * 4
        ks += [s, s + 2.5]
    ks += [52, 53, 54]
    # chorus: phonk pattern
    for bar in range(14, 22):
        s = bar * 4
        ks += [s, s + 1.5, s + 2.5]
        if bar % 2 == 1:
            ks.append(s + 3.5)
    # control: four on the floor
    for b in range(88, 104):
        ks.append(b)
    ks.append(104)
    return sorted(set(ks))


def snare_beats():
    sn = []
    for bar in range(4, 12):
        sn.append(bar * 4 + 2)
    for bar in range(14, 22):
        sn += [bar * 4 + 1, bar * 4 + 3]
    for bar in range(22, 25):
        sn += [bar * 4 + 1, bar * 4 + 3]
    return sn
