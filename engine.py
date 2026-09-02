"""Streaming engine: drives video2dlssnr.exe for video files, image files and
in-memory tensor batches.

Video file pipeline (adapted from video2dlssnr's nr_video.py):
    ffmpeg decode -> raw RGBA -> exe (GPU) -> raw RGBA -> ffmpeg NVENC

Tensor batch pipeline (no disk round-trip for the frames themselves):
    numpy RGBA -> exe stdin (GPU) -> exe stdout -> numpy
"""

import glob
import os
import re
import shutil
import subprocess
import threading
import time


def find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    try:
        import imageio_ffmpeg
        if name == "ffmpeg":
            return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    for hit in glob.glob(os.path.join(root, "Gyan.FFmpeg*", "**", name + ".exe"),
                         recursive=True):
        return hit
    return None


def probe(ffprobe, path):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,nb_frames", "-show_entries",
         "format=duration", "-of", "default=noprint_wrappers=1", path],
        capture_output=True, text=True).stdout
    d = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    num, den = (d["r_frame_rate"].split("/") + ["1"])[:2]
    fps = float(num) / float(den or 1)
    frames = int(d.get("nb_frames") or 0)
    if frames <= 0:
        try:
            frames = round(float(d.get("duration") or 0) * fps)
        except ValueError:
            frames = 0
    return int(d["width"]), int(d["height"]), fps, frames


def _nr_args(opts, motion=True):
    """Shared --nr-* argument builder for all three pipeline flavours."""
    args = ["--nr-style", str(int(opts.get("style", 0))),
            "--nr-preset", str(int(opts.get("preset", 0))),
            "--nr-intensity", str(float(opts.get("intensity", 1.0))),
            "--nr-local-structure", str(float(opts.get("local_structure", 1.0))),
            "--nr-local-tone", str(float(opts.get("local_tone", 1.0))),
            "--nr-skin", str(float(opts.get("skin", -1.0))),
            "--nr-global-tone", str(float(opts.get("global_tone", -1.0))),
            "--nr-detail", str(float(opts.get("detail", 1.0))),
            "--nr-color", str(float(opts.get("color", 1.0))),
            "--nr-ui-correction", "1" if opts.get("ui_correction") else "0"]
    if motion:
        args += ["--nr-motion", "1" if opts.get("motion", True) else "0",
                 "--nr-motion-engine", opts.get("motion_engine", "auto")]
    if opts.get("auto_mask"):
        args += ["--nr-auto-mask"]
    a = int(opts.get("adapter", -1))
    if a >= 0:
        args += ["--adapter", str(a)]
    return args


def _output_size(opts, inW, inH):
    outW, outH = opts.get("out_size") or (0, 0)
    if outW and not outH:
        outH = round(inH * outW / inW)
    elif outH and not outW:
        outW = round(inW * outH / inH)
    elif not outW and not outH:
        scale = float(opts.get("scale", 2.0))
        outW, outH = round(inW * scale), round(inH * scale)
    return outW - outW % 2, outH - outH % 2


class PipelineError(RuntimeError):
    pass


class PipelineInterrupted(Exception):
    pass


def kill(*procs):
    for p in procs:
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass


def _make_pump(stderr, state, log_cb):
    prog = re.compile(r"^NRPROG (\d+) ([\d.]+)")

    def pump():
        for raw in iter(stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            m = prog.match(line)
            if m:
                state["frames"] = int(m.group(1))
                state["fps"] = float(m.group(2))
            elif line.startswith("done:"):
                state["done"] = line
            elif line:
                state["log"].append(line)
                if log_cb:
                    try:
                        log_cb(line)
                    except Exception:
                        pass
        state["log_done"] = True

    return pump


# ---------------------------------------------------------------------------
# 1) whole video file: decode | GPU | encode, audio remuxed
# ---------------------------------------------------------------------------

def run_pipeline(video_in, video_out, opts, progress_cb=None, log_cb=None,
                 interrupt_check=None):
    """Returns (frames, gpu_fps, elapsed_seconds, (outW, outH))."""
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise PipelineError("ffmpeg/ffprobe not found. "
                            "Install ffmpeg or add it to PATH.")

    inW, inH, fps, total = probe(ffprobe, video_in)
    if total <= 0:
        raise PipelineError(f"could not read any frames from {video_in}")

    outW, outH = _output_size(opts, inW, inH)

    vf = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709,format=rgba"
    dec = [ffmpeg, "-v", "error", "-i", video_in]
    if int(opts.get("frames", 0)) > 0:
        dec += ["-frames:v", str(int(opts["frames"]))]
    dec += ["-vf", vf, "-f", "rawvideo", "-"]

    tool = [opts["exe"], "--nr-video", "--nr-in", f"{inW}x{inH}"]
    tool += _nr_args(opts)
    if (outW, outH) != (inW, inH):
        tool += ["--nr-width", str(outW), "--nr-height", str(outH)]

    cq = int(opts.get("cq", 0))
    audio = bool(opts.get("audio", True))
    enc = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{outW}x{outH}", "-r", f"{fps}", "-i", "-",
           "-i", video_in, "-map", "0:v:0"]
    if audio:
        enc += ["-map", "1:a:0?", "-c:a", "aac", "-b:a", "192k"]
    else:
        enc += ["-an"]
    enc += ["-c:v", opts.get("codec", "hevc_nvenc"), "-preset", "p5",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest"]
    if cq > 0:
        enc[enc.index("-preset") + 2:enc.index("-preset") + 2] = ["-cq", str(cq)]
    enc.append(video_out)

    started = time.perf_counter()
    p1 = p2 = p3 = None
    state = {"frames": 0, "fps": 0.0, "done": None, "log": []}

    try:
        p1 = subprocess.Popen(dec, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(tool, stdin=p1.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        p1.stdout.close()
        p3 = subprocess.Popen(enc, stdin=p2.stdout, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        p2.stdout.close()

        t = threading.Thread(target=_make_pump(p2.stderr, state, log_cb), daemon=True)
        t.start()

        def interrupted():
            if interrupt_check is None:
                return False
            try:
                return bool(interrupt_check())
            except Exception:
                return True

        while True:
            try:
                rc3 = p3.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if progress_cb:
                    try:
                        progress_cb(state["frames"], total, state["fps"])
                    except Exception:
                        pass
                if interrupted():
                    raise PipelineInterrupted("cancelled")
        rc2 = p2.wait(timeout=30)
        rc1 = p1.wait(timeout=30)
        t.join(timeout=2)

        elapsed = time.perf_counter() - started
        rc = rc3 or rc2 or rc1
        if rc != 0:
            tail = "\n".join(state["log"][-8:])
            enc_err = ""
            try:
                enc_err = p3.stderr.read().decode("utf-8", "replace")[-500:]
            except Exception:
                pass
            raise PipelineError(
                f"pipeline failed (decode={rc1} tool={rc2} encode={rc3})\n"
                f"{tail}\n{enc_err}".strip())
        if progress_cb:
            progress_cb(total, total, state["fps"])
        frames, steady = state["frames"], state["fps"]
        m = re.search(r"done: (\d+) frames in [\d.]+ s \(([\d.]+) fps\)",
                      state["done"] or "")
        if m:
            frames, steady = int(m.group(1)), float(m.group(2))
        return frames, steady, elapsed, (outW, outH)
    except PipelineInterrupted:
        kill(p1, p2, p3)
        raise
    except Exception as e:
        kill(p1, p2, p3)
        if isinstance(e, PipelineError):
            raise
        raise PipelineError(f"pipeline error: {e}") from e


# ---------------------------------------------------------------------------
# 2) in-memory tensor batch: one exe session streams every frame
#    (temporal continuity across the batch, single NGX init)
# ---------------------------------------------------------------------------

def run_tensor_batch(frames, inW, inH, outW, outH, opts,
                     progress_cb=None, log_cb=None, interrupt_check=None):
    """frames: list of HxWx3 uint8 numpy arrays, all inW x inH.

    Returns (list of HxWx3 uint8 outputs, gpu_fps). One exe process total.
    Treats the batch as a temporal sequence (motion history carried across).
    """
    import numpy as np

    tool = [opts["exe"], "--nr-video", "--nr-in", f"{inW}x{inH}"]
    tool += _nr_args(opts)
    if (outW, outH) != (inW, inH):
        tool += ["--nr-width", str(outW), "--nr-height", str(outH)]

    in_bytes = inW * inH * 4
    out_bytes = outW * outH * 4
    n = len(frames)
    state = {"frames": 0, "fps": 0.0, "done": None, "log": []}

    def interrupted():
        if interrupt_check is None:
            return False
        try:
            return bool(interrupt_check())
        except Exception:
            return True

    p2 = None
    try:
        p2 = subprocess.Popen(tool, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rgba = np.empty((inH, inW, 4), dtype=np.uint8)
        rgba[..., 3] = 255

        def writer():
            try:
                for f in frames:
                    rgba[..., :3] = f
                    p2.stdin.write(rgba.tobytes())
            except Exception:
                pass
            finally:
                try:
                    p2.stdin.close()
                except Exception:
                    pass

        wt = threading.Thread(target=writer, daemon=True)
        wt.start()
        st = threading.Thread(target=_make_pump(p2.stderr, state, log_cb), daemon=True)
        st.start()

        outputs = []
        for i in range(n):
            buf = bytearray()
            while len(buf) < out_bytes:
                chunk = p2.stdout.read(out_bytes - len(buf))
                if not chunk:
                    raise PipelineError(
                        "exe closed output early (frame %d/%d). Likely GPU or "
                        "NGX failure.\n%s" % (i + 1, n, "\n".join(state["log"][-6:])))
                buf.extend(chunk)
            arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(outH, outW, 4)
            outputs.append(np.ascontiguousarray(arr[..., :3]))
            if progress_cb:
                try:
                    progress_cb(i + 1, n, state["fps"])
                except Exception:
                    pass
            if interrupted():
                raise PipelineInterrupted("cancelled")
        p2.stdin.close() if p2.stdin else None
        rc = p2.wait(timeout=60)
        wt.join(timeout=10)
        st.join(timeout=2)
        if rc != 0:
            raise PipelineError("exe failed rc=%d\n%s" % (rc, "\n".join(state["log"][-6:])))
        steady = state["fps"]
        return outputs, steady
    except PipelineInterrupted:
        kill(p2)
        raise
    except Exception as e:
        kill(p2)
        if isinstance(e, PipelineError):
            raise
        raise PipelineError(f"tensor batch error: {e}") from e


# ---------------------------------------------------------------------------
# 3) single image file: exe --nr-run (independent, no temporal history)
# ---------------------------------------------------------------------------

def run_image(in_path, out_dir, opts, timeout=600):
    """Returns the path of the written "<stem>_nr.png"."""
    tool = [opts["exe"], "--nr-run", "--in", in_path, "--out", out_dir]
    w = int(opts.get("width", 0) or 0)
    if w > 0:
        tool += ["--nr-width", str(w)]
    else:
        tool += ["--nr-scale", str(float(opts.get("scale", 2.0)))]
    tool += _nr_args(opts, motion=False)

    r = subprocess.run(tool, capture_output=True, text=True, timeout=timeout,
                       cwd=os.path.dirname(opts["exe"]))
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
        raise PipelineError(f"image pipeline failed (rc={r.returncode})\n"
                            + "\n".join(tail))
    hits = sorted(glob.glob(os.path.join(out_dir, "*_nr.png")))
    if not hits:
        raise PipelineError(f"exe reported success but wrote no *_nr.png into {out_dir}")
    return hits[0]
