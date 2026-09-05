"""Offline checks: fan-out, formats, drift correction, and listener alignment."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np
from multiblu.engine import (BLOCK, MAX_CORRECTION, Engine, Listener, _Ring,
                             _Resampler, _remap_channels, _stretch)

fails = []


def check(name, ok, detail=""):
    print("  %-48s %s %s" % (name, "PASS" if ok else "FAIL", detail), flush=True)
    if not ok:
        fails.append(name)


def make(key, channels=2, rate=48000, target=BLOCK):
    l = Listener(key=key, name=key, pa_index=0)
    l.channels, l.rate = channels, rate
    l.ring = _Ring(rate * 3, channels)
    # The engine primes each ring to `target` on open; here `target` is chosen so
    # a single written block sits at the steady-state level and drift correction
    # stays out of the way, letting samples be compared exactly.
    l.base_target, l.align = target, 0
    l.fade, l.fade_step = 1.0, 1.0          # skip the ramp so samples compare exactly
    return l


class FakeStream:
    def __init__(self, latency):
        self._latency = latency

    def get_output_latency(self):
        return self._latency


src = (np.arange(BLOCK * 2, dtype=np.float32).reshape(BLOCK, 2) / (BLOCK * 2)) - 0.5

print("\n--- fan-out to many listeners ---", flush=True)
eng = Engine()
eng.src_channels = 2
names = ["earbuds-a", "earbuds-b", "earbuds-c", "speakers-d", "headset-e"]
for n in names:
    eng._listeners[n] = make(n)
eng._capture_callback(src.tobytes(), BLOCK, None, 0)

for n in names:
    out = eng._listeners[n].pull(BLOCK)
    check("%s receives the exact source block" % n,
          np.allclose(out, src, atol=1e-6), "max diff %.2e" % np.abs(out - src).max())
check("all five listeners stay in lockstep",
      len({eng._listeners[n].ring.count for n in names}) == 1)

print("\n--- independent volume per listener ---", flush=True)
eng2 = Engine(); eng2.src_channels = 2
a, b = make("loud"), make("quiet")
b.gain = 0.25
eng2._listeners = {"loud": a, "quiet": b}
eng2._capture_callback(src.tobytes(), BLOCK, None, 0)
check("gain applies per listener only",
      np.allclose(a.pull(BLOCK), src, atol=1e-6)
      and np.allclose(b.pull(BLOCK), src * 0.25, atol=1e-6))

print("\n--- format adaptation ---", flush=True)
mono = _remap_channels(src, 1)
check("stereo folds down to a mono device",
      mono.shape == (BLOCK, 1) and np.allclose(mono[:, 0], src.mean(axis=1), atol=1e-6))
surround = _remap_channels(src, 6)
check("stereo maps onto a 6-channel device",
      surround.shape == (BLOCK, 6) and np.allclose(surround[:, :2], src, atol=1e-6))

rs = _Resampler(44100 / 48000)
total = sum(rs.process(src).shape[0] for _ in range(50))
check("48k -> 44.1k keeps the right sample count",
      abs(total - 50 * BLOCK * 44100 / 48000) < 3, "%d frames" % total)

tone = np.sin(2 * np.pi * 440 * np.arange(BLOCK * 20) / 48000).astype(np.float32)
tone = np.column_stack([tone, tone])
rs2 = _Resampler(44100 / 48000)
res = np.vstack([rs2.process(tone[i:i + BLOCK]) for i in range(0, len(tone), BLOCK)])
peak = np.fft.rfftfreq(len(res), 1 / 44100)[
    int(np.argmax(np.abs(np.fft.rfft(res[:, 0] * np.hanning(len(res))))))]
check("resampled 440 Hz tone stays at 440 Hz", abs(peak - 440) < 6, "%.1f Hz" % peak)

print("\n--- drift correction is smooth, not clicky ---", flush=True)
smooth = np.column_stack([np.sin(2 * np.pi * 200 * np.arange(BLOCK * 6) / 48000)] * 2
                         ).astype(np.float32)
step_limit = np.abs(np.diff(smooth[:, 0])).max()

drift = make("drift", target=BLOCK)
drift.ring.write(smooth[:BLOCK * 4])                  # well over target -> correct down
out = drift.pull(BLOCK)
inner = np.abs(np.diff(out[:, 0])).max()
check("speeding up introduces no step discontinuity",
      inner < step_limit * 3, "max step %.5f vs %.5f" % (inner, step_limit))

slow = make("slow", target=BLOCK * 8)
slow.ring.write(smooth[:BLOCK])                       # under target -> correct up
out = slow.pull(BLOCK)
check("slowing down introduces no step discontinuity",
      np.abs(np.diff(out[:, 0])).max() < step_limit * 3)

check("correction is bounded so pitch never audibly shifts",
      MAX_CORRECTION / BLOCK < 0.006, "%.3f %%" % (100 * MAX_CORRECTION / BLOCK))

big = make("big", target=BLOCK)
big.ring.write(np.zeros((BLOCK * 20, 2), np.float32))
before = big.ring.count
big.pull(BLOCK)
check("a flooded buffer is resynced rather than left to grow",
      big.ring.count < before - BLOCK, "%d -> %d frames" % (before, big.ring.count))

print("\n--- starving sounds like a stumble, not a crack ---", flush=True)
starved = make("starved", target=BLOCK * 3)
starved.ring.write(smooth[:BLOCK // 2])
out = starved.pull(BLOCK)
check("a short starve is stretched to fill the block",
      out.shape == (BLOCK, 2) and np.abs(np.diff(out[:, 0])).max() < step_limit * 3)
check("starving asks for a bigger cushion", starved.wants_growth)

empty = make("empty", target=BLOCK * 3)
empty._last = np.array([[0.8, 0.8]], dtype=np.float32)
out = empty.pull(BLOCK)
check("a total dropout glides to silence instead of cutting",
      abs(float(out[0, 0]) - 0.8) < 0.05 and abs(float(out[-1, 0])) < 1e-6)

print("\n--- listeners are aligned to the slowest device ---", flush=True)
eng3 = Engine(); eng3.src_channels = 2
fast, slow2 = make("wired"), make("bluetooth")
fast.stream, slow2.stream = FakeStream(0.040), FakeStream(0.220)
eng3._listeners = {"wired": fast, "bluetooth": slow2}
eng3._align_listeners()
gap = (fast.align / fast.rate) - (slow2.align / slow2.rate)
check("the quicker device is delayed by the latency gap",
      abs(gap - 0.180) < 0.002, "%.0f ms of delay added" % (1000 * gap))
check("the slowest device gets no extra delay", slow2.align == 0)
check("both end up emitting the same instant",
      abs((fast.latency + fast.align / fast.rate)
          - (slow2.latency + slow2.align / slow2.rate)) < 0.002)

print("\n--- cushion growth keeps everyone together ---", flush=True)
eng4 = Engine(); eng4.src_channels = 2
group = {k: make(k, target=BLOCK) for k in ("one", "two", "three")}
eng4._listeners = group
group["two"].wants_growth = True
eng4._last_growth = 0.0
eng4._grow_cushion(tuple(group.values()))
targets = {l.base_target for l in group.values()}
check("one struggling device raises the cushion for all",
      len(targets) == 1 and targets.pop() == BLOCK * 2)
check("the growth request is cleared", not any(l.wants_growth for l in group.values()))

print("\n--- a dead listener cannot break the others ---", flush=True)
eng5 = Engine(); eng5.src_channels = 2
good, broken = make("good"), make("broken")
broken.ring = None                                     # as if its stream vanished
eng5._listeners = {"good": good, "broken": broken}
try:
    eng5._capture_callback(src.tobytes(), BLOCK, None, 0)
    check("capture survives a listener with no buffer", True)
except Exception as exc:
    check("capture survives a listener with no buffer", False, str(exc))
check("healthy listener still got its audio",
      np.allclose(good.pull(BLOCK), src, atol=1e-6))
check("a broken listener returns silence of the right shape",
      broken.pull(BLOCK).shape == (BLOCK, 2))

print("\n--- stretch keeps its end points ---", flush=True)
ramp = np.column_stack([np.linspace(0, 1, 500)] * 2).astype(np.float32)
for target_len in (480, 500, 520):
    out = _stretch(ramp, target_len)
    check("stretch %d -> %d preserves both ends" % (len(ramp), target_len),
          out.shape[0] == target_len and abs(out[0, 0]) < 1e-6
          and abs(out[-1, 0] - 1.0) < 1e-6)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails), flush=True)
sys.exit(1 if fails else 0)
