"""ComfyUI nodes: DLSS SR + Neural Rendering video upscaling (Windows + NVIDIA RTX)."""

import os
import subprocess

import folder_paths

from . import engine

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNTIMES = os.path.join(ROOT, "runtimes")


def list_runtimes():
    found = []
    if os.path.isdir(RUNTIMES):
        for name in sorted(os.listdir(RUNTIMES)):
            exe = os.path.join(RUNTIMES, name, "video2dlssnr.exe")
            if os.path.isfile(exe):
                found.append(name)
    return found or ["(missing - put video2dlssnr.exe in runtimes/<name>/)"]


def resolve_video_file(video):
    """Best-effort: extract a disk path from a ComfyUI VIDEO object."""
    if video is None:
        return None
    for attr in ("get_stream_source",):
        try:
            src = getattr(video, attr)()
            if isinstance(src, str) and os.path.isfile(src):
                return src
        except Exception:
            pass
    for attr in ("_VideoFromFile__file", "source_file", "file", "path"):
        v = getattr(video, attr, None)
        if isinstance(v, str) and os.path.isfile(v):
            return v
    return None


class DLSSNRVideoUpscale:
    """DLSS Super Resolution + Neural Rendering upscale for whole video files.

    Frames stay on the GPU end to end: ffmpeg decodes to raw RGBA, the NGX
    pipeline (DLSS SR -> optical flow -> Neural Rendering -> composite) runs in
    one video2dlssnr.exe process, and NVENC encodes the result. Windows only;
    needs an NVIDIA RTX GPU and driver 616.56+.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_path": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "输入视频完整路径。留空则使用右侧 video 输入的磁盘文件。",
                }),
                "runtime": (list_runtimes(),),
                "upscale_factor": (["1", "1.5", "2", "3", "4"], {
                    "default": "2",
                    "tooltip": "放大倍率。1 = 原生分辨率纯 NR 细节增强。",
                }),
                "output_width": ("INT", {
                    "default": 0, "min": 0, "max": 7680, "step": 2,
                    "tooltip": "输出宽度（0 = 按倍率）。高度自动按比例取偶。",
                }),
                "nr_style": (["0 Default", "1 Natural", "2 Cinematic"], {
                    "default": "0 Default",
                }),
                "nr_preset": (["0", "1", "2", "3"], {"default": "0"}),
                "nr_intensity": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                    "tooltip": "NR 强度。2 = 最强细节增强。",
                }),
                "nr_detail": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "合成强度。0 = 原图，1 = 完整 NR。",
                }),
                "nr_color": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "NR 色彩占比。0 = 保留原色彩。",
                }),
                "motion_engine": (["auto", "nvof", "lk"], {
                    "default": "auto",
                    "tooltip": "auto = NVOFA 硬件光流优先，否则 Lucas-Kanade。",
                }),
                "codec": (["hevc_nvenc", "h264_nvenc", "av1_nvenc"], {
                    "default": "hevc_nvenc",
                }),
                "cq": ("INT", {
                    "default": 22, "min": 0, "max": 34, "step": 1,
                    "tooltip": "NVENC 质量值，越小质量越高。0 = 用编码器默认。",
                }),
                "preserve_audio": ("BOOLEAN", {"default": True}),
                "filename_prefix": ("STRING", {"default": "dlssnr"}),
            },
            "optional": {
                "video": ("VIDEO",),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "file_path")
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "video/upscaling"

    def run(self, video_path, runtime, upscale_factor, output_width,
            nr_style, nr_preset, nr_intensity, nr_detail, nr_color,
            motion_engine, codec, cq, preserve_audio, filename_prefix,
            video=None, unique_id=None):
        src = video_path.strip().strip('"')
        if not src and video is not None:
            src = resolve_video_file(video) or ""
        if not src:
            raise ValueError("请填写 video_path，或连接一个 VIDEO 输入。")
        if not os.path.isfile(src):
            raise FileNotFoundError(f"找不到输入视频: {src}")
        if runtime not in list_runtimes():
            raise ValueError(f"runtime '{runtime}' 不可用。请检查 runtimes 目录。")

        exe = os.path.join(RUNTIMES, runtime, "video2dlssnr.exe")
        out_dir = os.path.join(folder_paths.get_output_directory(), "dlssnr")
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(
            out_dir, f"{filename_prefix}_{stem}_x{upscale_factor}.mp4")
        n = 1
        while os.path.isfile(out_path):
            out_path = os.path.join(
                out_dir, f"{filename_prefix}_{stem}_x{upscale_factor}_{n}.mp4")
            n += 1

        opts = {
            "exe": exe,
            "scale": float(upscale_factor),
            "out_size": (output_width, 0) if output_width else None,
            "style": int(nr_style.split(" ")[0]),
            "preset": int(nr_preset),
            "intensity": float(nr_intensity),
            "detail": float(nr_detail),
            "color": float(nr_color),
            "motion": True,
            "motion_engine": motion_engine,
            "codec": codec,
            "cq": int(cq),
            "audio": bool(preserve_audio),
        }

        from comfy.utils import ProgressBar
        try:
            from comfy.model_management import (
                throw_exception_if_processing_interrupted as _cancelled)
        except ImportError:
            def _cancelled():
                return False

        pbar_holder = {}

        def progress_cb(frames, total, fps):
            pbar = pbar_holder.get("pbar")
            if pbar is None:
                pbar = pbar_holder["pbar"] = ProgressBar(total)
            pbar.update_absolute(frames, total)

        frames, gpu_fps, elapsed, (outW, outH) = engine.run_pipeline(
            src, out_path, opts, progress_cb=progress_cb,
            interrupt_check=_cancelled)

        try:
            from comfy_api.latest import InputImpl
        except ImportError:
            from comfy_api.v0_0_2 import InputImpl
        out_video = InputImpl.VideoFromFile(out_path)
        info = (f"{frames} frames | {in_size(out_path)} | "
                f"{gpu_fps:.1f} fps GPU | {elapsed:.1f}s | -> {outW}x{outH}")
        print(f"[DLSS-NR] {info}")
        ui = {"images": [{"filename": os.path.basename(out_path),
                          "subfolder": "dlssnr", "type": "output"}],
              "animated": (True,)}
        return {"ui": ui, "result": (out_video, out_path)}


def in_size(path):
    try:
        return f"{os.path.getsize(path) / 2**20:.1f} MB"
    except OSError:
        return "?"


class DLSSNRCheck:
    """Diagnostic: verifies exe / DLLs / ffmpeg / driver and probes NGX feature 18."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"runtime": (list_runtimes(),)}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "video/upscaling"

    def run(self, runtime):
        lines = []
        exe = os.path.join(RUNTIMES, runtime, "video2dlssnr.exe")
        lines.append(f"exe: {exe} [{'OK' if os.path.isfile(exe) else 'MISSING'}]")
        for dll in ("nvngx_dlss.dll", "nvngx_dlssnr.dll"):
            p = os.path.join(RUNTIMES, runtime, dll)
            sz = os.path.getsize(p) / 2**20 if os.path.isfile(p) else -1
            lines.append(f"{dll}: {'OK (%.0f MB)' % sz if sz >= 0 else 'MISSING'}")
        ffmpeg = engine.find_tool("ffmpeg")
        ffprobe = engine.find_tool("ffprobe")
        lines.append(f"ffmpeg: {ffmpeg or 'MISSING'}")
        lines.append(f"ffprobe: {ffprobe or 'MISSING'}")
        try:
            import torch
            lines.append(f"cuda: {torch.cuda.get_device_name(0)}"
                         if torch.cuda.is_available() else "cuda: unavailable")
        except Exception as e:
            lines.append(f"cuda: error ({e})")
        if os.path.isfile(exe) and ffmpeg and ffprobe:
            lines.append("functional self-test (2 frames, 960x544 -> 1920x1088):")
            try:
                import tempfile
                with tempfile.TemporaryDirectory() as td:
                    test_in = os.path.join(td, "in.mp4")
                    test_out = os.path.join(td, "out.mp4")
                    subprocess.run(
                        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                         "-i", "testsrc2=size=960x544:rate=24:duration=1",
                         "-pix_fmt", "yuv420p", test_in],
                        capture_output=True, timeout=60)
                    result = engine.run_pipeline(
                        test_in, test_out,
                        {"exe": exe, "scale": 2.0, "frames": 2, "cq": 0},
                        progress_cb=None, interrupt_check=None)
                    lines.append(f"  OK - processed {result[0]} frames "
                                 f"({result[1]:.1f} fps GPU)")
            except Exception as e:
                lines.append(f"  FAILED: {e}")
        report = "\n".join(lines)
        print(f"[DLSS-NR check]\n{report}")
        return {"ui": {"text": [report]}, "result": ((report,),)}


class DLSSNRImageUpscale:
    """DLSS SR + Neural Rendering for IMAGE batches (exe --nr-run mode).

    Each frame is written to a temp PNG, processed on the GPU by the NGX
    pipeline, and read back as an IMAGE tensor. Supports upscaling (SR) plus
    Neural Rendering detail enhancement. Windows + NVIDIA RTX, driver 616.56+.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "runtime": (list_runtimes(),),
                "upscale_factor": (["1", "1.5", "2", "3", "4"], {
                    "default": "2",
                    "tooltip": "放大倍率。1 = 原生分辨率纯 NR 细节增强。",
                }),
                "output_width": ("INT", {
                    "default": 0, "min": 0, "max": 7680, "step": 2,
                    "tooltip": "输出宽度（0 = 按倍率）。",
                }),
                "nr_style": (["0 Default", "1 Natural", "2 Cinematic"], {
                    "default": "0 Default",
                }),
                "nr_preset": (["0", "1", "2", "3"], {"default": "0"}),
                "nr_intensity": ("FLOAT", {
                    "default": 1.5, "min": 0.0, "max": 2.0, "step": 0.05,
                    "tooltip": "NR 强度。2 = 最强细节增强。",
                }),
                "nr_detail": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "合成强度。0 = 原图，1 = 完整 NR。",
                }),
                "nr_color": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "NR 色彩占比。0 = 保留原色彩。",
                }),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "run"
    CATEGORY = "image/upscaling"

    def run(self, images, runtime, upscale_factor, output_width, nr_style,
            nr_preset, nr_intensity, nr_detail, nr_color):
        if runtime not in list_runtimes():
            raise ValueError(f"runtime '{runtime}' 不可用。请检查 runtimes 目录。")
        opts = {
            "exe": os.path.join(RUNTIMES, runtime, "video2dlssnr.exe"),
            "scale": float(upscale_factor),
            "width": int(output_width),
            "style": int(nr_style.split(" ")[0]),
            "preset": int(nr_preset),
            "intensity": float(nr_intensity),
            "detail": float(nr_detail),
            "color": float(nr_color),
        }

        import numpy as np
        import torch
        from PIL import Image
        from comfy.utils import ProgressBar

        batch = images.shape[0]
        pbar = ProgressBar(batch)
        outs = []
        try:
            import tempfile
            td = tempfile.TemporaryDirectory()
        except Exception:
            raise
        with td as work:
            for i in range(batch):
                try:
                    from comfy.model_management import (
                        throw_exception_if_processing_interrupted as _c)
                    _c()
                except ImportError:
                    pass
                arr = (images[i].detach().cpu().numpy() * 255.0
                       ).round().clip(0, 255).astype("uint8")
                in_png = os.path.join(work, f"in_{i:05d}.png")
                Image.fromarray(arr, "RGB").save(in_png)
                out_png = engine.run_image(in_png, work, opts)
                out = np.asarray(Image.open(out_png).convert("RGB"))
                outs.append(torch.from_numpy(out.astype("float32") / 255.0))
                pbar.update_absolute(i + 1, batch)
        result = torch.stack(outs, dim=0)
        return (result,)


NODE_CLASS_MAPPINGS = {
    "DLSSNRVideoUpscale": DLSSNRVideoUpscale,
    "DLSSNRImageUpscale": DLSSNRImageUpscale,
    "DLSSNRCheck": DLSSNRCheck,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DLSSNRVideoUpscale": "DLSS NR 视频超分 (SR+NR)",
    "DLSSNRImageUpscale": "DLSS NR 图片超分 (SR+NR)",
    "DLSSNRCheck": "DLSS NR 环境自检",
}
