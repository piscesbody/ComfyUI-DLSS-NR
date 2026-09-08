"""DLSS Frame Generation engine: drives dlssg-worker.exe over its binary pipe
protocol (ffmpeg decode -> raw RGBA -> worker (D3D12 NGX DLSSG) -> RGBA -> ffmpeg).

The worker protocol (reverse-engineered from upstream project
Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation, MIT):

  setup  in : <5I  magic 0x31534746, width, height, frame_count, generated_per_gap
  setup  out : <4I  magic 0x31524746, status, multi_frame_count_max, reserved
  frame  in : <4I2q magic 0x31464746, index, reset, 0, ts_num, ts_den
              + RGBA bytes (W*H*4 uint8) + motion vectors W*H*2 float16
  frame  out : <4I  magic 0x314F4746, status, generated_count, disabled
              + generated_count * RGBA bytes

Measured on RTX 4090 (test_dlss/dlssfg/probe_motion.py): the worker estimates
motion on the GPU itself - feeding zero vectors changes its output by ~63 dB
(invisible) while skipping a whole CPU optical-flow stage, so we always feed
zeros. Same benchmark puts generated-frame quality at +0.75 dB PSNR over
ffmpeg minterpolate on AI video, at ~1/7 the wall time.

Ada GPUs cap at multi_frame_count_max=1, i.e. one generated frame per gap (2x).
Higher multipliers chain 2x stages: stage k consumes stage k-1's temporal output
(including its generated frames) and interpolates again.
"""

import json
import os
import struct
import subprocess
import threading
import time
from collections import deque
from fractions import Fraction

from .engine import (PipelineError, PipelineInterrupted, _audio_args,
                     _CONTAINERS, _encoder_args, _SWS, find_tool, kill, probe)

_SETUP, _SETUP_OK = 0x31534746, 0x31524746
_FRAME, _FRAME_OK = 0x31464746, 0x314F4746

CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def fg_files(runtime_dir):
    return (os.path.join(runtime_dir, "dlssg-worker.exe"),
            os.path.join(runtime_dir, "nvngx_dlssg.dll"))


_CAPS_CACHE = {}


def probe_caps(worker, runtime_dir, timeout=60):
    """Run dlssg-worker --probe; returns {available, multi_frame_count_max, ...}.

    Cached per runtime dir + worker mtime: NGX init costs ~1s per probe.
    """
    exe, dll = fg_files(runtime_dir)
    if not (os.path.isfile(exe) and os.path.isfile(dll)):
        raise PipelineError("DLSSG runtime missing: dlssg-worker.exe / "
                            "nvngx_dlssg.dll not found in " + runtime_dir +
                            "\nInstall with:  python install.py --fg")
    try:
        mtime = os.path.getmtime(exe)
    except OSError:
        mtime = 0
    cached = _CAPS_CACHE.get(runtime_dir)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        r = subprocess.run([worker, "--probe"], cwd=runtime_dir,
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATIONFLAGS)
    except subprocess.TimeoutExpired as e:
        raise PipelineError(
            "DLSSG capability probe timed out. Check the NVIDIA driver (610+) "
            "and that HAGS is enabled: Windows Settings > System > Display > "
            "Graphics > Change default graphics settings > Hardware-accelerated "
            "GPU scheduling, then reboot.") from e
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-4:]
        raise PipelineError("DLSSG probe failed (rc=%d):\n%s"
                            % (r.returncode, "\n".join(tail)))
    lines = [x for x in (r.stdout or "").splitlines() if x.strip()]
    info = {}
    if lines:
        try:
            info = json.loads(lines[-1])
        except json.JSONDecodeError:
            info = {}
    if not info:
        raise PipelineError("DLSSG probe returned no JSON:\n" + "\n".join(lines[-4:]))
    if not info.get("available") or int(info.get("multi_frame_count_max", 0)) < 1:
        raise PipelineError(
            "DLSSG Frame Generation is unavailable: %s\n"
            "Needs an RTX GPU, driver 610+, and HAGS enabled + reboot."
            % info.get("detail", info))
    _CAPS_CACHE[runtime_dir] = (mtime, info)
    return info


def hags_enabled():
    """Windows HAGS state; DLSSG refuses to run without it on most setups."""
    if os.name != "nt":
        return True
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers") as key:
            value, _kind = winreg.QueryValueEx(key, "HwSchMode")
        return int(value) == 2
    except OSError:
        return False


class _Session:
    """One worker process = one DLSSG history over a fixed-size frame stream."""

    def __init__(self, worker, runtime_dir, w, h, n_frames, per_gap):
        self.logs = deque(maxlen=300)
        self.fb = w * h * 4          # RGBA frame bytes == motion bytes (WHx2 f16)
        self._zeros = b"\x00" * self.fb   # zero motion vectors, preallocated
        self.p = subprocess.Popen([worker, "--serve"], cwd=runtime_dir,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  creationflags=CREATIONFLAGS)
        self._lt = threading.Thread(target=self._read_logs, daemon=True)
        self._lt.start()
        self.p.stdin.write(struct.pack("<5I", _SETUP, w, h,
                                       max(1, n_frames), per_gap))
        self.p.stdin.flush()
        magic, status, maximum, _r = struct.unpack("<4I", self._read(16))
        if magic != _SETUP_OK or status:
            text = self.log_text()
            self.close()
            raise PipelineError(
                "DLSSG session setup failed (status %d, runtime max %dx).\n%s"
                % (status, maximum + 1, text[-600:]))
        self.index = 0

    def _read_logs(self):
        for raw in iter(self.p.stderr.readline, b""):
            self.logs.append(raw.decode("utf-8", "replace").rstrip())

    def log_text(self):
        return "\n".join(self.logs)

    def _read(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.p.stdout.read(n - len(buf))
            if not chunk:
                raise PipelineError("DLSSG worker closed its output:\n"
                                    + self.log_text()[-800:])
            buf += chunk
        return bytes(buf)

    def push(self, rgba, ts, reset):
        """Feed one frame; returns the generated mid-frame bytes (0 or 1 items)."""
        self.p.stdin.write(struct.pack("<4I2q", _FRAME, self.index,
                                       1 if reset else 0, 0,
                                       ts.numerator, ts.denominator))
        self.p.stdin.write(rgba)
        self.p.stdin.write(self._zeros)   # zero motion vectors (WHx2 float16)
        self.p.stdin.flush()
        self.index += 1
        magic, status, generated, disabled = struct.unpack("<4I", self._read(16))
        if magic != _FRAME_OK or status:
            raise PipelineError("DLSSG failed at frame %d (status %d):\n%s"
                                % (self.index - 1, status, self.log_text()[-800:]))
        if disabled:
            return []
        return [self._read(self.fb) for _ in range(generated)]

    def close(self):
        try:
            if self.p.stdin and not self.p.stdin.closed:
                self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            kill(self.p)


class _Stage:
    """One 2x interpolation stage: wraps a session, tracks temporal order.

    Frames carry a segment id; a segment change means an upstream scene cut, so
    this stage resets its history too and refuses to interpolate across it.
    """

    def __init__(self, worker, runtime_dir, w, h, n_frames):
        self.sess = _Session(worker, runtime_dir, w, h, n_frames, 1)
        self.prev = None          # (rgba, ts, segment)
        self.prime = True

    def push(self, rgba, ts, segment, force_reset):
        """Feed one frame; returns this stage's ordered output (rgba, ts, segment).

        Every input slot maps to exactly two output slots (except the very
        first frame of the stream, which only seeds the NGX history). At a
        reset the source frame is repeated through the mid slot ("held") so
        the CFR slot count stays exact across scene cuts.
        """
        reset = force_reset or self.prime or (
            self.prev is not None and segment != self.prev[2])
        generated = self.sess.push(rgba, ts, reset)
        if self.prime:
            self.prime = False
            self.prev = (rgba, ts, segment)
            return [(rgba, ts, segment)]
        mid_ts = self.prev[1] + (ts - self.prev[1]) / 2
        if reset or not generated:
            out = [(rgba, mid_ts, segment)]
        else:
            out = [(generated[0], mid_ts, segment)]
        out.append((rgba, ts, segment))
        self.prev = (rgba, ts, segment)
        return out

    def close(self):
        self.sess.close()


def _cut_scorer(inW, inH):
    """Returns f(rgba_bytes) -> mean |luma delta| vs previous call, in 0..1.

    Luma is block-averaged 8x8 first: cheap, and immune to the fine-grain noise
    that trips pixel-level cut detection on AI video.
    """
    import numpy as np

    sh, sw = max(1, inH // 8), max(1, inW // 8)
    state = {"prev": None}

    def score(rgba):
        a = np.frombuffer(rgba, np.uint8).reshape(inH, inW, 4)
        g = a[:sh * 8, :sw * 8, :3].astype(np.uint16)
        g = g.reshape(sh, 8, sw, 8, 3).mean(axis=(1, 3, 4))
        cur = g.astype(np.float32)
        prev = state["prev"]
        state["prev"] = cur
        if prev is None or prev.shape != cur.shape:
            return 1.0
        return float(np.abs(cur - prev).mean()) / 255.0

    return score


def run_frame_interp(video_in, video_out, opts, progress_cb=None, log_cb=None,
                     interrupt_check=None):
    """ffmpeg decode | N x DLSSG 2x stages | ffmpeg encode, audio remuxed.

    opts: worker, runtime_dir, passes (1 or 2), cut_thresh, codec, cq, bitrate,
    enc_preset, bit_depth, audio_mode, audio_bitrate.
    Returns (in_frames, out_frames, in_fps, elapsed, (w, h), cuts).
    """
    worker = opts["worker"]
    runtime_dir = opts["runtime_dir"]
    passes = max(1, min(2, int(opts.get("passes", 1))))
    cut_thresh = float(opts.get("cut_thresh", 0.24))

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PipelineError("ffmpeg/ffprobe not found. "
                            "Install ffmpeg or add it to PATH.")

    caps = probe_caps(worker, runtime_dir)
    if int(caps.get("multi_frame_count_max", 1)) < 1:
        raise PipelineError("DLSSG runtime reports no frame generation capability")

    inW, inH, fps_f, total = probe(ffprobe, video_in)
    if total <= 0:
        raise PipelineError(f"could not read any frames from {video_in}")

    # exact CFR rate; DLSSG has no notion of variable frame timing
    def frac(entry):
        s = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=" + entry, "-of", "csv=p=0", video_in],
            capture_output=True, text=True).stdout.strip()
        num, den = (s.split("/") + ["1"])[:2]
        try:
            return Fraction(int(num), int(den or 1))
        except ValueError:
            return None

    rate = frac("r_frame_rate")
    if not rate or rate <= 0:
        raise PipelineError("could not read the frame rate of " + video_in)
    avg = frac("avg_frame_rate")
    if avg and avg != rate:
        raise PipelineError(
            f"input is variable frame rate ({float(rate):.3f} nominal vs "
            f"{float(avg):.3f} average). DLSSG needs constant frame rate - "
            "convert the clip first (ffmpeg -vsync cfr).")

    mult = 2 ** passes
    total_out = total * mult
    out_rate = rate * mult

    out_ext = os.path.splitext(video_out)[1].lower() or ".mp4"
    allowed = _CONTAINERS.get(out_ext, {"hevc_nvenc", "h264_nvenc", "av1_nvenc"})
    if opts.get("codec", "hevc_nvenc") not in allowed:
        raise PipelineError(f"codec {opts['codec']} cannot go into a {out_ext} "
                            f"container; allowed: {', '.join(sorted(allowed))}")

    v_args, v_pix = _encoder_args(inW, inH, opts, out_ext)
    audio_mode = str(opts.get("audio_mode", "auto"))
    if not opts.get("audio", True):
        audio_mode = "none"
    enc = [ffmpeg, "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{inW}x{inH}",
           "-r", f"{out_rate.numerator}/{out_rate.denominator}", "-i", "-",
           "-i", video_in, "-map", "0:v:0", "-frames:v", str(total_out)]
    enc += _audio_args(out_ext, audio_mode, int(opts.get("audio_bitrate", 192)))
    if v_pix != "bgr0":
        evf = [f"scale=out_color_matrix=bt709:out_range=tv:flags={_SWS}",
               f"format={v_pix}",
               "setparams=colorspace=bt709:color_primaries=bt709:"
               "color_trc=bt709:range=tv"]
    else:
        evf = [f"format={v_pix}",
               "setparams=color_primaries=bt709:color_trc=bt709:range=pc"]
    enc += ["-vf", ",".join(evf)] + v_args
    enc += ["-movflags", "+faststart"] if out_ext in (".mp4", ".mov") else []
    enc += ["-shortest", video_out]

    dec = [ffmpeg, "-v", "error", "-i", video_in, "-map", "0:v:0", "-an",
           "-vf", "setparams=colorspace=bt709:color_primaries=bt709:"
                  "color_trc=bt709,format=rgba",
           "-f", "rawvideo", "-"]

    started = time.perf_counter()
    p_dec = p_enc = None
    stages = []
    fb = inW * inH * 4

    def interrupted():
        if interrupt_check is None:
            return False
        try:
            return bool(interrupt_check())
        except Exception:
            return True

    def cleanup():
        for s in stages:
            try:
                s.close()
            except Exception:
                pass
        kill(p_dec, p_enc)

    try:
        p_dec = subprocess.Popen(dec, stdout=subprocess.PIPE)
        stages = [_Stage(worker, runtime_dir, inW, inH, total * (2 ** k))
                  for k in range(passes)]
        p_enc = subprocess.Popen(enc, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE)

        cut_score = _cut_scorer(inW, inH)
        n_in = n_out = cuts = 0
        last_tick = [0.0]
        tail_rgba = None

        while True:
            raw = p_dec.stdout.read(fb)
            if not raw:
                break
            n_in += 1
            tail_rgba = raw
            force_reset = cut_score(raw) > cut_thresh
            if force_reset and n_in > 1:
                cuts += 1
            segment = cuts

            # stage 0 sees source timing; later stages see the previous stage's
            # output, whose timestamps are already interleaved
            items = [(raw, Fraction(n_in - 1, 1) / rate, segment)]
            for st in stages:
                nxt = []
                for rgba, ts, seg in items:
                    nxt.extend(st.push(rgba, ts, seg, False))
                items = nxt

            for rgba, _ts, _seg in items:
                p_enc.stdin.write(rgba)
                n_out += 1

            if progress_cb and time.perf_counter() - last_tick[0] >= 0.8:
                try:
                    progress_cb(n_in, total,
                                n_in / (time.perf_counter() - started))
                except Exception:
                    pass
                last_tick[0] = time.perf_counter()
            if interrupted():
                raise PipelineInterrupted("cancelled")

        if n_in == 0:
            raise PipelineError("decoded nothing from " + video_in)
        # pad trailing CFR slots so output duration matches the source exactly
        while n_out < total_out and tail_rgba is not None:
            p_enc.stdin.write(tail_rgba)
            n_out += 1

        for s in stages:
            s.close()
        p_dec.stdout.close()
        rc_dec = p_dec.wait(timeout=60)
        p_enc.stdin.close()
        try:
            enc_err = p_enc.stderr.read().decode("utf-8", "replace")[-500:]
        except Exception:
            enc_err = ""
        rc_enc = p_enc.wait(timeout=60)
        if rc_enc or rc_dec:
            raise PipelineError(
                f"frame interpolation failed (decode={rc_dec} encode={rc_enc})"
                + (f"\n{enc_err}" if enc_err.strip() else ""))
        if n_out != total_out:
            raise PipelineError(f"wrote {n_out} frames, expected {total_out}")
        if progress_cb:
            try:
                progress_cb(total, total, total / (time.perf_counter() - started))
            except Exception:
                pass
        return n_in, n_out, fps_f, time.perf_counter() - started, (inW, inH), cuts
    except PipelineInterrupted:
        cleanup()
        raise
    except Exception as e:
        cleanup()
        if isinstance(e, PipelineError):
            raise
        raise PipelineError(f"frame interpolation error: {e}") from e


def self_test(runtime_dir, opts_extra=None):
    """Two synthetic frames through the real pipeline; returns (ok, log_lines)."""
    import tempfile

    worker, _dll = fg_files(runtime_dir)
    lines = []
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False, lines + ["ffmpeg missing"]
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.mp4")
            dst = os.path.join(td, "out.mp4")
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=640x384:rate=24:duration=1",
                 "-pix_fmt", "yuv420p", src],
                capture_output=True, timeout=60)
            o = {"worker": worker, "runtime_dir": runtime_dir, "passes": 1,
                 "codec": "h264_nvenc", "cq": 20}
            o.update(opts_extra or {})
            n_in, n_out, _fps, el, size, cuts = run_frame_interp(src, dst, o)
            lines.append(f"self-test: {n_in} -> {n_out} frames in {el:.1f}s, "
                         f"{size[0]}x{size[1]}")
            return os.path.isfile(dst) and n_out == 2 * n_in, lines
    except Exception as e:
        lines.append(f"self-test failed: {e}")
        return False, lines
