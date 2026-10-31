"""Audio fan-out engine.

Listens in on whichever output device Windows is already playing to, using
WASAPI loopback, and renders that same audio to any number of other output
devices at once. Nothing about the system's audio setup is changed: the source
device keeps playing normally, with its own volume control, and each extra
listener gets an independent copy.

Three things make it hold together with several wireless devices at once:

* Every listener runs off its own sound-card clock, so its buffer slowly drifts.
  Drift is taken out by resampling a block by a few samples rather than by
  duplicating or dropping raw ones -- a duplicated sample is a step change, and
  at fifty blocks a second that is audible as a buzz.
* Devices report very different output latencies (Bluetooth is far slower than
  USB), so each listener is delayed to match the slowest one. Without that, two
  people sitting together hear the same song a beat apart.
* When a device cannot keep up, its cushion grows automatically and every other
  listener grows with it, so they stay aligned.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pyaudiowpatch as pa

from .endpoints import match_index

BLOCK = 960                 # frames per callback (20 ms at 48 kHz)
TARGET_BLOCKS = 5           # starting cushion per listener (~100 ms)
MAX_TARGET_BLOCKS = 18      # ceiling when a device keeps stumbling (~360 ms)
GROW_COOLDOWN = 1.5         # seconds between cushion increases
MAX_CORRECTION = 4          # frames per block of drift correction (~0.4 %)
RING_SECONDS = 3.0
FADE_SECONDS = 0.05         # ramp in, so starting a stream never pops


def _stretch(x: np.ndarray, frames: int) -> np.ndarray:
    """Resample x to exactly `frames` rows, keeping both end points.

    Used for drift correction and for riding out a short starve. Because the
    first and last input samples are preserved, consecutive blocks still join
    up seamlessly, so there is no click where a dropped sample would leave one.
    """
    n = x.shape[0]
    if n == frames:
        return x
    if n < 2:
        return np.repeat(x[:1] if n else np.zeros((1, x.shape[1]), np.float32),
                         frames, axis=0)
    t = np.linspace(0.0, n - 1.0, frames)
    i0 = np.floor(t).astype(np.int64)
    i1 = np.minimum(i0 + 1, n - 1)
    frac = (t - i0).astype(np.float32)[:, None]
    return (x[i0] * (1.0 - frac) + x[i1] * frac).astype(np.float32)


class _Ring:
    """Single-producer / single-consumer circular buffer of float32 frames."""

    def __init__(self, frames: int, channels: int):
        self.buf = np.zeros((frames, channels), dtype=np.float32)
        self.cap = frames
        self.channels = channels
        self._w = self._r = self.count = 0
        self._lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        n = x.shape[0]
        if n == 0:
            return
        with self._lock:
            if n > self.cap:
                x, n = x[-self.cap:], self.cap
            overflow = self.count + n - self.cap
            if overflow > 0:
                self._r = (self._r + overflow) % self.cap
                self.count -= overflow
            first = min(n, self.cap - self._w)
            self.buf[self._w:self._w + first] = x[:first]
            if n > first:
                self.buf[:n - first] = x[first:]
            self._w = (self._w + n) % self.cap
            self.count += n

    def read(self, n: int):
        out = np.zeros((n, self.channels), dtype=np.float32)
        with self._lock:
            got = min(n, self.count)
            if got:
                first = min(got, self.cap - self._r)
                out[:first] = self.buf[self._r:self._r + first]
                if got > first:
                    out[first:got] = self.buf[:got - first]
                self._r = (self._r + got) % self.cap
                self.count -= got
        return out, got

    def discard(self, n: int) -> None:
        with self._lock:
            n = min(n, self.count)
            self._r = (self._r + n) % self.cap
            self.count -= n


class _Resampler:
    """Linear resampler that carries its position across blocks.

    Only used when a listener refuses the source's sample rate, which is rare --
    nearly every endpoint on Windows runs at 48 kHz.
    """

    def __init__(self, ratio: float):
        self.step = 1.0 / ratio
        self.pos = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        n = x.shape[0]
        if n == 0:
            return x
        count = int(np.ceil((n - 1 - self.pos) / self.step)) + 1
        t = self.pos + np.arange(max(count, 0)) * self.step
        t = t[t <= n - 1]
        if t.size == 0:
            self.pos -= n
            return np.zeros((0, x.shape[1]), dtype=np.float32)
        i0 = np.floor(t).astype(np.int64)
        i1 = np.minimum(i0 + 1, n - 1)
        frac = (t - i0).astype(np.float32)[:, None]
        self.pos = float(t[-1] + self.step - n)
        return (x[i0] * (1.0 - frac) + x[i1] * frac).astype(np.float32)


def _remap_channels(x: np.ndarray, want: int) -> np.ndarray:
    have = x.shape[1]
    if have == want:
        return x
    if want == 1:
        return x.mean(axis=1, keepdims=True).astype(np.float32)
    if have == 1:
        return np.repeat(x, want, axis=1)
    out = np.zeros((x.shape[0], want), dtype=np.float32)
    keep = min(have, want)
    out[:, :keep] = x[:, :keep]
    return out


@dataclass
class Listener:
    """One output device the mix is being copied to."""

    key: str
    name: str
    pa_index: int
    stream: object = None
    ring: _Ring = None
    resampler: _Resampler = None
    channels: int = 2
    rate: int = 48000
    base_target: int = BLOCK * TARGET_BLOCKS
    align: int = 0              # measured delay so slower devices stay in step
    user_delay: int = 0         # hand-tuned nudge for codec delay we cannot see
    latency: float = 0.0        # what PortAudio reports for this stream
    gain: float = 1.0
    fade: float = 0.0
    fade_step: float = 1.0
    underruns: int = 0
    wants_growth: bool = False
    error: str = ""
    _last: np.ndarray = field(default=None, repr=False)
    _played_gain: float = field(default=None, repr=False)

    @property
    def target(self) -> int:
        return self.base_target + self.align + self.user_delay

    def push(self, block: np.ndarray) -> None:
        if self.ring is None:
            return
        if self.resampler is not None:
            block = self.resampler.process(block)
        self.ring.write(_remap_channels(block, self.channels))

    def pull(self, frames: int) -> np.ndarray:
        ring = self.ring
        if ring is None:
            return np.zeros((frames, self.channels), dtype=np.float32)

        target = self.target
        fill = ring.count
        if fill > target + max(target, BLOCK * 8):
            ring.discard(fill - target)          # device stalled, then flooded
            fill = ring.count

        # Nudge playback speed by a few frames per block until the cushion sits
        # back on target. The deadband stops it hunting around the set point.
        correction = 0
        error = fill - target
        if abs(error) > BLOCK // 2:
            correction = int(np.clip(error // BLOCK, -MAX_CORRECTION, MAX_CORRECTION))
        want = max(8, frames + correction)

        data, got = ring.read(want)
        if got < want:
            self.underruns += 1
            self.wants_growth = True
            if got >= 8:
                data = _stretch(data[:got], frames)   # stretch rather than click
            else:
                data = self._silence(frames)
        elif want != frames:
            data = _stretch(data, frames)

        if self.fade < 1.0:
            ramp = np.minimum(self.fade + self.fade_step * np.arange(1, frames + 1), 1.0)
            self.fade = float(ramp[-1])
            data = data * ramp[:, None].astype(np.float32)
        gain = self.gain
        was = gain if self._played_gain is None else self._played_gain
        if gain != was:                 # glide across the block, so a drag never zippers
            data = data * np.linspace(was, gain, frames, dtype=np.float32)[:, None]
        elif gain != 1.0:
            data = data * np.float32(gain)
        self._played_gain = gain

        self._last = data[-1:].copy()
        return data

    def _silence(self, frames: int) -> np.ndarray:
        """Nothing to play: glide down from the last sample instead of cutting."""
        out = np.zeros((frames, self.channels), dtype=np.float32)
        if self._last is not None and self._last.any():
            ramp = np.linspace(1.0, 0.0, min(frames, 256), dtype=np.float32)
            out[:len(ramp)] = self._last * ramp[:, None]
        return out


class Engine:
    """Owns the PortAudio session, the loopback capture and every listener."""

    def __init__(self):
        self._pa = None
        self._capture = None
        self._keepalive = None
        self._listeners: dict = {}
        self._lock = threading.RLock()
        self._last_growth = 0.0
        self.delays: dict = {}          # device key -> seconds of extra delay
        self.volumes: dict = {}         # device key -> slider level, 0 to 1
        self.src_channels = 2
        self.src_rate = 48000
        self.source_name = ""

    # -- PortAudio session -------------------------------------------------
    # PortAudio snapshots the device list when it initialises, so a Bluetooth
    # device that connects later stays invisible until the session is rebuilt.

    def _open_pa(self):
        if self._pa is None:
            self._pa = pa.PyAudio()
        return self._pa

    def _close_pa(self) -> None:
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def _render_map(self, p) -> dict:
        """Map of PortAudio index -> name for WASAPI render devices."""
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
        except OSError:
            return {}
        found = {}
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if (d["hostApi"] == wasapi and d["maxOutputChannels"] > 0
                    and not d.get("isLoopbackDevice")):
                found[i] = d["name"]
        return found

    def output_devices(self) -> dict:
        """Fresh map of PortAudio index -> name, rebuilding the session if idle."""
        with self._lock:
            if self._capture is None:
                self._close_pa()
            return self._render_map(self._open_pa())

    def _loopback_index_for(self, render_index: int):
        p = self._open_pa()
        want = p.get_device_info_by_index(render_index)["name"].strip().lower()
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if not d.get("isLoopbackDevice"):
                continue
            if d["name"].replace("[Loopback]", "").strip().lower() == want:
                return i
        return None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._capture is not None

    def start(self, source_name: str, listeners: list) -> dict:
        """Copy the source device's audio to every listener.

        Devices are named rather than indexed because PortAudio indices are only
        valid for one session, and the session is rebuilt here so that Bluetooth
        devices connected since the last start are visible.
        """
        self.stop()
        report = {"source": "", "ok": [], "failed": {}, "missing": []}
        with self._lock:
            self._close_pa()          # rebuild so freshly connected devices exist
            p = self._open_pa()
            devices = self._render_map(p)

            source_index = match_index(source_name, devices)
            if source_index is None:
                report["failed"]["*"] = "%s is no longer available." % source_name
                self._close_pa()
                return report

            loopback = self._loopback_index_for(source_index)
            if loopback is None:
                report["failed"]["*"] = "Windows will not let us listen in on %s." % source_name
                self._close_pa()
                return report

            info = p.get_device_info_by_index(loopback)
            self.src_channels = int(info["maxInputChannels"]) or 2
            self.src_rate = int(info["defaultSampleRate"])
            self.source_name = source_name
            report["source"] = source_name

            for key, name in listeners:
                index = match_index(name, devices)
                if index is None or index == source_index:
                    report["missing"].append(key)
                    continue
                listener = Listener(key=key, name=name, pa_index=index)
                try:
                    self._open_listener(p, listener)
                    self._listeners[key] = listener
                    report["ok"].append(key)
                except Exception as exc:               # device busy or vanished
                    listener.error = str(exc)
                    report["failed"][key] = str(exc)

            if not self._listeners:
                self._close_pa()
                return report

            self._align_listeners()

            # An idle endpoint delivers nothing to its loopback client, which
            # stalls capture until something plays. Holding a silent stream open
            # on the source keeps its audio engine running, so the first sound
            # after a quiet stretch arrives immediately instead of glitching.
            self._keepalive = self._open_silence(p, source_index)

            try:
                self._capture = p.open(
                    format=pa.paFloat32,
                    channels=self.src_channels,
                    rate=self.src_rate,
                    input=True,
                    input_device_index=loopback,
                    frames_per_buffer=BLOCK,
                    stream_callback=self._capture_callback,
                )
                self._capture.start_stream()
                for listener in self._listeners.values():
                    listener.stream.start_stream()
            except Exception as exc:
                report["failed"]["*"] = "Could not listen in on %s: %s" % (source_name, exc)
                self.stop()
        report["latency_ms"] = {k: round(1000 * l.latency)
                                for k, l in self._listeners.items()}
        return report

    def _align_listeners(self) -> None:
        """Delay the quicker devices so every listener plays the same instant.

        PortAudio reports each stream's output latency; Bluetooth endpoints sit
        far behind wired ones, and without this two people hear the same song
        noticeably out of step.
        """
        if not self._listeners:
            return
        for listener in self._listeners.values():
            try:
                listener.latency = float(listener.stream.get_output_latency())
            except Exception:
                listener.latency = 0.0
        slowest = max(l.latency for l in self._listeners.values())
        for key, listener in self._listeners.items():
            listener.align = max(0, int((slowest - listener.latency) * listener.rate))
            listener.user_delay = max(0, int(self.delays.get(key, 0.0) * listener.rate))
            listener.gain = self.volumes.get(key, 1.0) ** 2
            listener.ring.write(
                np.zeros((listener.target, listener.channels), np.float32))

    def _open_listener(self, p, listener: Listener) -> None:
        info = p.get_device_info_by_index(listener.pa_index)
        max_ch = int(info["maxOutputChannels"])
        dev_rate = int(info["defaultSampleRate"])

        last = None
        for channels in dict.fromkeys((min(2, max_ch), max_ch)):
            for rate in dict.fromkeys((self.src_rate, dev_rate)):
                try:
                    stream = p.open(
                        format=pa.paFloat32,
                        channels=channels,
                        rate=rate,
                        output=True,
                        output_device_index=listener.pa_index,
                        frames_per_buffer=BLOCK,
                        stream_callback=(
                            lambda *a, _l=listener: self._output_callback(_l, *a)
                        ),
                        start=False,
                    )
                except Exception as exc:
                    last = exc
                    continue
                listener.channels = channels
                listener.rate = rate
                listener.ring = _Ring(int(RING_SECONDS * rate), channels)
                listener.base_target = int(BLOCK * TARGET_BLOCKS * rate / self.src_rate)
                listener.resampler = (
                    None if rate == self.src_rate else _Resampler(rate / self.src_rate)
                )
                listener.fade = 0.0
                listener.fade_step = 1.0 / max(1, int(FADE_SECONDS * rate))
                listener.stream = stream
                return          # started once capture is live, so it never starts starved
        raise RuntimeError(str(last) if last else "device refused every format")

    def _open_silence(self, p, device_index: int):
        """Open a stream that renders silence, used to keep the source awake."""
        info = p.get_device_info_by_index(device_index)
        channels = min(2, int(info["maxOutputChannels"])) or 1
        for rate in dict.fromkeys((self.src_rate, int(info["defaultSampleRate"]))):
            quiet = np.zeros((BLOCK, channels), dtype=np.float32).tobytes()
            try:
                return p.open(
                    format=pa.paFloat32,
                    channels=channels,
                    rate=rate,
                    output=True,
                    output_device_index=device_index,
                    frames_per_buffer=BLOCK,
                    stream_callback=(
                        lambda in_data, frames, t, s, _q=quiet, _c=channels: (
                            _q if frames == BLOCK
                            else np.zeros((frames, _c), np.float32).tobytes(),
                            pa.paContinue,
                        )
                    ),
                )
            except Exception:
                continue
        return None

    def set_delay(self, key: str, seconds: float) -> None:
        """Shift one listener in time while it plays.

        PortAudio only reports a device's host buffer, not the delay a Bluetooth
        codec adds after that, so the remaining offset has to be dialled in by
        ear. Padding the ring pushes the device later; discarding pulls it
        earlier, and both take effect on the next block.
        """
        seconds = max(0.0, min(0.5, seconds))
        self.delays[key] = seconds
        listener = self._listeners.get(key)
        if listener is None or listener.ring is None:
            return
        wanted = int(seconds * listener.rate)
        change = wanted - listener.user_delay
        listener.user_delay = wanted
        if change > 0:
            listener.ring.write(np.zeros((change, listener.channels), np.float32))
        elif change < 0:
            listener.ring.discard(-change)

    def set_volume(self, key: str, level: float) -> None:
        """Set one listener's level, 0 to 1, while it plays.

        This scales the listener's own copy of the audio. The Windows device
        volume is no use for this: a device that claims hardware volume is left to
        apply it itself, and virtual cables and many Bluetooth sets simply do not.
        Hearing is logarithmic, so the level is squared: halfway is -12 dB, which
        sounds about half as loud.
        """
        level = max(0.0, min(1.0, level))
        self.volumes[key] = level
        listener = self._listeners.get(key)
        if listener is not None:
            listener.gain = level ** 2

    def stop(self) -> None:
        with self._lock:
            for attr in ("_capture", "_keepalive"):
                stream = getattr(self, attr)
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
            for listener in self._listeners.values():
                try:
                    listener.stream.stop_stream()
                    listener.stream.close()
                except Exception:
                    pass
            self._listeners.clear()
            self._close_pa()

    # -- callbacks ---------------------------------------------------------
    # Both are wrapped: an exception escaping a PortAudio callback aborts that
    # stream, which would take a listener (or the whole capture) down for good.

    def _capture_callback(self, in_data, frame_count, time_info, status):
        try:
            block = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self.src_channels)
            listeners = tuple(self._listeners.values())
            if any(l.wants_growth for l in listeners):
                self._grow_cushion(listeners)
            for listener in listeners:
                listener.push(block)
        except Exception:
            pass
        return None, pa.paContinue

    def _grow_cushion(self, listeners) -> None:
        """A device could not keep up: give everyone more slack, together.

        Growing only the struggling listener would leave it playing behind the
        others, so the cushion is raised across the board to preserve alignment.
        """
        now = time.monotonic()
        for listener in listeners:
            listener.wants_growth = False
        if now - self._last_growth < GROW_COOLDOWN:
            return
        self._last_growth = now
        for listener in listeners:
            ceiling = int(BLOCK * MAX_TARGET_BLOCKS * listener.rate / self.src_rate)
            if listener.base_target >= ceiling:
                continue
            step = int(BLOCK * listener.rate / self.src_rate)
            listener.base_target = min(ceiling, listener.base_target + step)
            if listener.ring is not None and listener.ring.count < listener.target:
                listener.ring.write(
                    np.zeros((step, listener.channels), dtype=np.float32))

    def _output_callback(self, listener, in_data, frame_count, time_info, status):
        try:
            return listener.pull(frame_count).tobytes(), pa.paContinue
        except Exception:
            return (np.zeros((frame_count, listener.channels), np.float32).tobytes(),
                    pa.paContinue)

    # -- status ------------------------------------------------------------

    def stats(self) -> dict:
        return {
            key: {
                "underruns": l.underruns,
                "buffer_ms": int(1000 * l.ring.count / l.rate) if l.ring else 0,
                "target_ms": int(1000 * l.target / l.rate),
                "delay_ms": int(1000 * l.user_delay / l.rate),
                "latency_ms": int(1000 * l.latency),
                "rate": l.rate,
                "channels": l.channels,
                "error": l.error,
            }
            for key, l in self._listeners.items()
        }
