"""ComfyUI nodes: DLSS SR + Neural Rendering upscaling (Windows + NVIDIA RTX).

video2dlssnr.exe by DaniilSokolyuk does all GPU work (DLSS SR -> optical flow ->
Neural Rendering -> composite); this pack streams data to it and integrates it
into ComfyUI.
"""

import glob
import hashlib
import os
import subprocess
import tempfile
import time
from datetime import datetime

import folder_paths
from comfy_api.latest import io, ui

from . import engine

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNTIMES = os.path.join(ROOT, "runtimes")


# ----------------------------------------------------------------- i18n ----

def _detect_lang():
    env = os.environ.get("DLSSNR_LANG", "").strip().lower()
    if env in ("zh", "en"):
        return env
    try:
        import ctypes
        if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF) == 0x04:
            return "zh"
    except Exception:
        pass
    try:
        import locale
        if (locale.getdefaultlocale()[0] or "").lower().startswith("zh"):
            return "zh"
    except Exception:
        pass
    return "en"


_LANG = _detect_lang()


def t(key):
    return T[key][_LANG]


def sec(key):
    """Section separator label, rendered as a single-choice combo bar."""
    return "━━  " + T[key][_LANG] + "  ━━"


T = {
    # section headers
    "sec_preset": {"zh": "画质预设", "en": "PRESET"},
    "sec_size": {"zh": "输出尺寸", "en": "OUTPUT SIZE"},
    "sec_nr": {"zh": "NR 增强 - 调这些改变细节/色调/皮肤", "en": "NR ENHANCE - detail / tone / skin"},
    "sec_enc": {"zh": "运动与编码", "en": "MOTION & ENCODING"},
    # node display names
    "video_node": {"zh": "DLSS NR 视频超分 (SR+NR)", "en": "DLSS NR Video Upscale (SR+NR)"},
    "image_node": {"zh": "DLSS NR 图片超分 (SR+NR)", "en": "DLSS NR Image Upscale (SR+NR)"},
    # short widget labels
    "lbl_video_path": {"zh": "视频路径", "en": "Video path"},
    "lbl_preset": {"zh": "画质预设", "en": "Quality preset"},
    "lbl_images": {"zh": "输入图片", "en": "Images"},
    "lbl_batch": {"zh": "批处理模式", "en": "Batch mode"},
    "lbl_selfcheck": {"zh": "运行前自检", "en": "Self-check first"},
    # combos
    "preset_custom": {"zh": "自定义 (下方手动参数生效)", "en": "Custom (sliders below apply)"},
    "preset_lite": {"zh": "轻度增强 (接近原图)", "en": "Light (close to source)"},
    "preset_standard": {"zh": "标准增强 (推荐日常)", "en": "Standard (recommended)"},
    "preset_max": {"zh": "细节拉满 (最强效果)", "en": "Max detail (strongest)"},
    "preset_portrait": {"zh": "人像保护 (皮肤柔和)", "en": "Portrait (soft skin)"},
    "preset_night": {"zh": "夜景电影 (暗场增质)", "en": "Night cinematic"},
    "batch_independent": {"zh": "独立图片 (每张互不影响)", "en": "Still images (independent)"},
    "batch_sequence": {"zh": "帧序列 (同一视频的帧, 时域连续)", "en": "Frame sequence (temporal)"},
    # tooltips
    "video_path_tt": {
        "zh": "输入视频完整路径。留空时使用下方 video 输入的磁盘文件。",
        "en": "Full path of the input video. Leave empty to use the file behind the video input."},
    "preset_tt": {
        "zh": "一键画质方案。选择非自定义项时, NR 增强组的手动参数全部忽略。",
        "en": "One-click quality recipe. When not Custom, all manual NR parameters are ignored."},
    "upscale_tt": {
        "zh": "放大倍率。1 = 不放大, 纯 NR 原生细节增强。",
        "en": "Upscale factor. 1 = no upscale, native-resolution NR enhancement only."},
    "out_width_tt": {
        "zh": "直接指定输出宽度 (0 = 按倍率)。高度自动按比例计算并取偶。",
        "en": "Output width (0 = use factor). Height follows aspect, rounded to even."},
    "style_tt": {
        "zh": "NR 风格。0=标准(效果最明显) 1=自然(最收敛) 2=电影感(加对比)。",
        "en": "NR style. 0=Default (strongest) 1=Natural (mildest) 2=Cinematic."},
    "intensity_tt": {
        "zh": "NR 强度, 细节脑补力度。0=关, 1=标准, 2=最强。人像建议 ≤1.5。",
        "en": "NR intensity (detail hallucination strength). 0=off, 1=standard, 2=max. Keep ≤1.5 for faces."},
    "detail_tt": {
        "zh": "合成强度。0=完全原图, 1=完全采用 NR 结果。",
        "en": "Composite strength. 0=original, 1=full NR result."},
    "color_tt": {
        "zh": "NR 色彩占比。发现偏色时降低此值。",
        "en": "How much NR recolours the result. Lower it if colours drift."},
    "skin_tt": {
        "zh": "皮肤结构强度。-1=模型默认。人像皮肤蜡感时调低, 想要皮肤纹理时调高。",
        "en": "Skin structure strength. -1=model default. Lower for waxy skin, raise for skin texture."},
    "structure_tt": {
        "zh": "局部结构强度 (微细节/纹理锐度)。",
        "en": "Local structure strength (micro-detail sharpness)."},
    "tone_tt": {
        "zh": "局部色调强度 (局部光影对比)。",
        "en": "Local tone strength (local light/shadow contrast)."},
    "global_tone_tt": {
        "zh": "全局色调。-1=模型默认, 可整体提亮或压暗。",
        "en": "Global tone. -1=model default; brightens or darkens overall."},
    "auto_mask_tt": {
        "zh": "自动遮罩 (UI/文字/字幕区域保护)。",
        "en": "Auto mask (protects UI/text/subtitle regions)."},
    "motion_tt": {
        "zh": "光流引擎。auto=优先 NVOFA 硬件; 画面异常时试 lk。",
        "en": "Optical flow engine. auto=NVOFA hardware first; try lk if artifacts appear."},
    "adapter_tt": {
        "zh": "选择 NVIDIA 显卡。-1=自动选最快的一张。双卡时可把超分丢给副卡。",
        "en": "NVIDIA adapter index. -1=fastest. Use a secondary GPU for upscaling."},
    "codec_tt": {
        "zh": "编码器。hevc=体积小(默认) h264=兼容性最好 av1=最新(体积最小)。",
        "en": "Encoder. hevc=small (default) h264=best compatibility av1=newest/smallest."},
    "cq_tt": {
        "zh": "画质值, 越小质量越高。18=高 22=均衡 26=省体积。0=编码器默认。",
        "en": "Constant quality. Lower=better. 18=high 22=balanced 26=small. 0=encoder default."},
    "batch_tt": {
        "zh": "独立图片: 每张单独处理, 互不影响, 适合无关图。帧序列: 整批一次会话流式处理, 快且带时域连续, 仅适合同一视频拆出的帧。",
        "en": "Still: each image processed separately. Sequence: whole batch in one session - faster with temporal continuity, only for frames of the same clip."},
    "selfcheck_tt": {
        "zh": "处理前先跑一次 2 帧功能自检 (约 10 秒)。首次使用或换环境时建议打开。",
        "en": "Run a 2-frame functional self-test before processing (~10s). Useful on first run."},
    # runtime messages
    "err_no_runtime": {
        "zh": "未找到运行时。请把 video2dlssnr.exe、nvngx_dlss.dll、nvngx_dlssnr.dll、nvngx.dll_dlssnr.dll 放入插件 runtimes/default/ 目录。",
        "en": "Runtime not found. Put video2dlssnr.exe, nvngx_dlss.dll, nvngx_dlssnr.dll and nvngx.dll_dlssnr.dll into the plugin's runtimes/default/ folder."},
    "err_need_path": {
        "zh": "请填写 video_path, 或连接一个 VIDEO 输入。",
        "en": "Fill in video_path, or connect a VIDEO input."},
    "err_no_file": {
        "zh": "找不到输入视频",
        "en": "Input video not found"},
    "err_diag": {
        "zh": "环境诊断",
        "en": "environment diagnostics"},
    "log_start_video": {
        "zh": "开始处理视频: {src}\n           输出 -> {out}",
        "en": "Processing video: {src}\n           output -> {out}"},
    "log_start_image": {
        "zh": "开始处理图片: {n} 张, 模式={mode}",
        "en": "Processing images: n={n}, mode={mode}"},
    "log_params": {
        "zh": "参数: 预设={preset} | 倍率={scale} | style={style} intensity={intensity} detail={detail} skin={skin}",
        "en": "params: preset={preset} | scale={scale} | style={style} intensity={intensity} detail={detail} skin={skin}"},
    "log_done": {
        "zh": "完成: {frames} 帧 | {mb:.1f} MB | {fps:.1f} fps GPU | {sec:.1f}s | 输出 {w}x{h}",
        "en": "done: {frames} frames | {mb:.1f} MB | {fps:.1f} fps GPU | {sec:.1f}s | output {w}x{h}"},
    "log_video": {"zh": "视频", "en": "video"},
    "log_image": {"zh": "图片", "en": "image"},
    "selftest_run": {
        "zh": "功能自检中 (2 帧, 960x544 -> 1920x1088)…",
        "en": "Functional self-test (2 frames, 960x544 -> 1920x1088)…"},
    "selftest_ok": {"zh": "自检通过", "en": "self-test passed"},
    "selftest_fail": {"zh": "自检失败", "en": "self-test failed"},
}


def _log(msg):
    print(f"[DLSS-NR {datetime.now():%H:%M:%S}] {msg}", flush=True)


def list_runtimes():
    found = []
    if os.path.isdir(RUNTIMES):
        for name in sorted(os.listdir(RUNTIMES)):
            if os.path.isfile(os.path.join(RUNTIMES, name, "video2dlssnr.exe")):
                found.append(name)
    return found


def _runtime_or_raise(runtime):
    found = list_runtimes()
    if not found:
        raise RuntimeError(t("err_no_runtime"))
    if runtime in found:
        return runtime
    return found[0]


def _sha256(path, limit_mb=400):
    try:
        if os.path.getsize(path) > limit_mb * 2 ** 20:
            return "too large to hash"
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8 * 2 ** 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16] + "…"
    except OSError:
        return "missing"


def _quick_report(runtime):
    exe = os.path.join(RUNTIMES, runtime, "video2dlssnr.exe")
    ok = lambda p: os.path.isfile(p)
    lines = [f"exe: {'OK' if ok(exe) else 'MISSING'} ({exe})"]
    for dll in ("nvngx_dlss.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll"):
        p = os.path.join(RUNTIMES, runtime, dll)
        lines.append(f"{dll}: {'OK sha256=' + _sha256(p) if ok(p) else 'MISSING (' + p + ')'}")
    lines.append(f"ffmpeg: {engine.find_tool('ffmpeg') or 'MISSING'}")
    lines.append(f"ffprobe: {engine.find_tool('ffprobe') or 'MISSING'}")
    try:
        import torch
        lines.append("cuda: " + (torch.cuda.get_device_name(0)
                                if torch.cuda.is_available() else "unavailable"))
    except Exception as e:
        lines.append(f"cuda: error ({e})")
    return lines


def _functional_self_test(runtime):
    exe = os.path.join(RUNTIMES, runtime, "video2dlssnr.exe")
    lines = [t("selftest_run")]
    ffmpeg = engine.find_tool("ffmpeg")
    if not ffmpeg:
        return False, lines + ["ffmpeg missing"]
    try:
        with tempfile.TemporaryDirectory() as td:
            test_in = os.path.join(td, "in.mp4")
            test_out = os.path.join(td, "out.mp4")
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=960x544:rate=24:duration=1",
                 "-pix_fmt", "yuv420p", test_in],
                capture_output=True, timeout=60)
            engine.run_pipeline(test_in, test_out,
                                {"exe": exe, "scale": 2.0, "frames": 2, "cq": 0})
            lines.append(t("selftest_ok"))
            return True, lines
    except Exception as e:
        lines.append(f"{t('selftest_fail')}: {e}")
        return False, lines


_PRESET_KEYS = ["custom", "lite", "standard", "max", "portrait", "night"]


def _preset_choices():
    return [t("preset_" + k) for k in _PRESET_KEYS]


def _preset_key_of(value):
    for k in _PRESET_KEYS:
        for lang in ("zh", "en"):
            if value == T["preset_" + k][lang]:
                return k
    return "custom"


def _effective(preset_key, vals):
    PRESETS = {
        "lite": {"style": 0, "intensity": 1.0, "detail": 0.8, "color": 1.0},
        "standard": {"style": 0, "intensity": 1.5, "detail": 1.0, "color": 1.0},
        "max": {"style": 0, "intensity": 2.0, "detail": 1.0, "color": 1.0},
        "portrait": {"style": 1, "intensity": 1.2, "detail": 0.9, "color": 0.8,
                     "skin": 1.0},
        "night": {"style": 2, "intensity": 1.8, "detail": 1.0, "color": 1.0},
    }
    if preset_key == "custom":
        return vals
    eff = dict(vals)
    eff.update(PRESETS[preset_key])
    return eff


def _make_progress(log, label=None):
    from comfy.utils import ProgressBar
    holder = {"pbar": None}
    last = {"t": 0.0}

    def cb(frames, total, fps):
        if holder["pbar"] is None:
            holder["pbar"] = ProgressBar(total)
        holder["pbar"].update_absolute(frames, total)
        now = time.time()
        if frames >= total or now - last["t"] >= 2.0:
            suffix = f" ({label})" if label else ""
            _log(f"{frames}/{total} 帧 | {fps:.1f} fps GPU{suffix}")
            last["t"] = now

    return cb


def _interrupt():
    try:
        from comfy.model_management import (
            throw_exception_if_processing_interrupted as _c)
        _c()
    except ImportError:
        pass


# ------------------------------------------------------------------ nodes --

class DLSSNRVideoUpscale(io.ComfyNode):
    """DLSS SR + Neural Rendering upscale for whole video files."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DLSSNRVideoUpscale",
            display_name=t("video_node"),
            category="video/upscaling",
            description=t("preset_tt"),
            is_output_node=True,
            inputs=[
                io.Video.Input("video", optional=True, tooltip=t("video_path_tt")),
                io.String.Input("video_path", default="",
                                display_name=t("lbl_video_path"),
                                tooltip=t("video_path_tt")),
                io.Combo.Input("sec_preset", options=[sec("sec_preset")],
                               default=sec("sec_preset"), optional=True),
                io.Combo.Input("quality_preset", options=_preset_choices(),
                               default=_preset_choices()[2],
                               display_name=t("lbl_preset"),
                               tooltip=t("preset_tt")),

                io.Combo.Input("sec_size", options=[sec("sec_size")],
                               default=sec("sec_size"), optional=True),
                io.Combo.Input("upscale_factor",
                               options=["1", "1.5", "2", "3", "4"], default="2",
                               display_name=t("upscale_tt").split("。")[0],
                               tooltip=t("upscale_tt")),
                io.Int.Input("output_width", default=0, min=0, max=7680, step=2,
                             display_name=t("out_width_tt").split(" (")[0],
                             tooltip=t("out_width_tt")),

                io.Combo.Input("sec_nr", options=[sec("sec_nr")],
                               default=sec("sec_nr"), optional=True),
                io.Combo.Input("nr_style", options=["0 Default", "1 Natural", "2 Cinematic"],
                               default="0 Default", display_name=t("style_tt").split("。")[0],
                               tooltip=t("style_tt")),
                io.Float.Input("nr_intensity", default=1.5, min=0.0, max=2.0, step=0.05,
                               display_name=t("intensity_tt").split(",")[0],
                               tooltip=t("intensity_tt")),
                io.Float.Input("nr_detail", default=1.0, min=0.0, max=1.0, step=0.05,
                               display_name=t("detail_tt").split("。")[0],
                               tooltip=t("detail_tt")),
                io.Float.Input("nr_color", default=1.0, min=0.0, max=1.0, step=0.05,
                               display_name=t("color_tt").split("。")[0],
                               tooltip=t("color_tt")),
                io.Float.Input("nr_skin", default=-1.0, min=-1.0, max=2.0, step=0.05,
                               display_name=t("skin_tt").split("。")[0],
                               tooltip=t("skin_tt")),
                io.Float.Input("nr_structure", default=1.0, min=0.0, max=2.0, step=0.05,
                               display_name=t("structure_tt").split(" (")[0],
                               tooltip=t("structure_tt")),
                io.Float.Input("nr_tone", default=1.0, min=0.0, max=2.0, step=0.05,
                               display_name=t("tone_tt").split(" (")[0],
                               tooltip=t("tone_tt")),
                io.Float.Input("nr_global_tone", default=-1.0, min=-1.0, max=2.0, step=0.05,
                               display_name=t("global_tone_tt").split("。")[0],
                               tooltip=t("global_tone_tt")),
                io.Boolean.Input("auto_mask", default=False,
                                 display_name=t("auto_mask_tt").split(" (")[0],
                                 tooltip=t("auto_mask_tt")),

                io.Combo.Input("sec_enc", options=[sec("sec_enc")],
                               default=sec("sec_enc"), optional=True),
                io.Combo.Input("motion_engine", options=["auto", "nvof", "lk"],
                               default="auto", display_name=t("motion_tt").split("。")[0],
                               tooltip=t("motion_tt")),
                io.Int.Input("gpu_adapter", default=-1, min=-1, max=8, step=1,
                             display_name=t("adapter_tt").split("。")[0],
                             tooltip=t("adapter_tt")),
                io.Combo.Input("codec", options=["hevc_nvenc", "h264_nvenc", "av1_nvenc"],
                               default="hevc_nvenc", display_name=t("codec_tt").split("。")[0],
                               tooltip=t("codec_tt")),
                io.Int.Input("cq", default=22, min=0, max=34, step=1,
                             display_name=t("cq_tt").split(",")[0],
                             tooltip=t("cq_tt")),
            ],
            hidden=[io.Hidden.unique_id],
            outputs=[
                io.Video.Output("video", display_name=t("log_video")),
                io.String.Output("file_path"),
            ],
        )

    @classmethod
    def execute(cls, video_path, quality_preset, upscale_factor, output_width,
                nr_style, nr_intensity, nr_detail, nr_color, nr_skin,
                nr_structure, nr_tone, nr_global_tone, auto_mask,
                motion_engine, gpu_adapter, codec, cq,
                runtime=None, video=None, **kwargs):
        src = (video_path or "").strip().strip('"')
        if not src and video is not None:
            try:
                s = video.get_stream_source()
                if isinstance(s, str) and os.path.isfile(s):
                    src = s
            except Exception:
                pass
        if not src:
            raise ValueError(t("err_need_path"))
        if not os.path.isfile(src):
            raise FileNotFoundError(f"{t('err_no_file')}: {src}")
        runtime = _runtime_or_raise(runtime)

        pkey = _preset_key_of(quality_preset)
        eff = _effective(pkey, {
            "style": int(nr_style.split(" ")[0]),
            "intensity": float(nr_intensity), "detail": float(nr_detail),
            "color": float(nr_color), "skin": float(nr_skin),
            "local_structure": float(nr_structure), "local_tone": float(nr_tone),
            "global_tone": float(nr_global_tone), "auto_mask": bool(auto_mask)})

        opts = dict(eff)
        opts.update({
            "exe": os.path.join(RUNTIMES, runtime, "video2dlssnr.exe"),
            "scale": float(upscale_factor),
            "out_size": (int(output_width), 0) if output_width else None,
            "motion": True, "motion_engine": motion_engine,
            "adapter": int(gpu_adapter),
            "codec": codec, "cq": int(cq), "audio": True,
        })

        out_dir = os.path.join(folder_paths.get_output_directory(), "dlssnr")
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(out_dir, f"{stem}_x{upscale_factor}.mp4")
        n = 1
        while os.path.isfile(out_path):
            out_path = os.path.join(out_dir, f"{stem}_x{upscale_factor}_{n}.mp4")
            n += 1

        _log(t("log_start_video").format(src=src, out=out_path))
        _log(t("log_params").format(
            preset=quality_preset, scale=upscale_factor,
            intensity=opts["intensity"], detail=opts["detail"],
            style=opts["style"], skin=opts.get("skin", -1)))

        try:
            frames, gpu_fps, elapsed, size = engine.run_pipeline(
                src, out_path, opts,
                progress_cb=_make_progress(_log, t("log_video")),
                log_cb=lambda line: _log(f"[exe] {line}"),
                interrupt_check=_interrupt)
        except engine.PipelineError as e:
            report = "\n".join(_quick_report(runtime))
            raise RuntimeError(f"{e}\n---- {t('err_diag')} ----\n{report}") from e

        _log(t("log_done").format(frames=frames, mb=os.path.getsize(out_path) / 2 ** 20,
                                  fps=gpu_fps, sec=elapsed, w=size[0], h=size[1]))
        try:
            from comfy_api.latest import InputImpl
        except ImportError:
            from comfy_api.v0_0_2 import InputImpl
        out_video = InputImpl.VideoFromFile(out_path)
        return io.NodeOutput(
            out_video, out_path,
            ui=ui.PreviewVideo([ui.SavedResult(os.path.basename(out_path),
                                               "dlssnr", io.FolderType.output)]))


class DLSSNRImageUpscale(io.ComfyNode):
    """DLSS SR + Neural Rendering for IMAGE batches (self-check included)."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="DLSSNRImageUpscale",
            display_name=t("image_node"),
            category="image/upscaling",
            description=t("batch_tt"),
            inputs=[
                io.Image.Input("images", display_name=t("lbl_images"),
                               tooltip=t("images_tt")),

                io.Combo.Input("sec_preset", options=[sec("sec_preset")],
                               default=sec("sec_preset"), optional=True),
                io.Combo.Input("quality_preset", options=_preset_choices(),
                               default=_preset_choices()[2], tooltip=t("preset_tt")),

                io.Combo.Input("sec_size", options=[sec("sec_size")],
                               default=sec("sec_size"), optional=True),
                io.Combo.Input("upscale_factor",
                               options=["1", "1.5", "2", "3", "4"], default="2",
                               display_name=t("upscale_tt").split("。")[0],
                               tooltip=t("upscale_tt")),
                io.Int.Input("output_width", default=0, min=0, max=7680, step=2,
                             display_name=t("out_width_tt").split(" (")[0],
                             tooltip=t("out_width_tt")),

                io.Combo.Input("sec_nr", options=[sec("sec_nr")],
                               default=sec("sec_nr"), optional=True),
                io.Combo.Input("batch_mode",
                               options=[t("batch_independent"), t("batch_sequence")],
                               default=t("batch_independent"),
                               display_name=t("lbl_batch"),
                               tooltip=t("batch_tt")),
                io.Boolean.Input("self_check", default=False,
                                 display_name=t("lbl_selfcheck"),
                                 tooltip=t("selfcheck_tt")),
                io.Combo.Input("nr_style", options=["0 Default", "1 Natural", "2 Cinematic"],
                               default="0 Default", display_name=t("style_tt").split("。")[0],
                               tooltip=t("style_tt")),
                io.Float.Input("nr_intensity", default=1.5, min=0.0, max=2.0, step=0.05,
                               display_name=t("intensity_tt").split(",")[0],
                               tooltip=t("intensity_tt")),
                io.Float.Input("nr_detail", default=1.0, min=0.0, max=1.0, step=0.05,
                               display_name=t("detail_tt").split("。")[0],
                               tooltip=t("detail_tt")),
                io.Float.Input("nr_color", default=1.0, min=0.0, max=1.0, step=0.05,
                               display_name=t("color_tt").split("。")[0],
                               tooltip=t("color_tt")),
                io.Float.Input("nr_skin", default=-1.0, min=-1.0, max=2.0, step=0.05,
                               display_name=t("skin_tt").split("。")[0],
                               tooltip=t("skin_tt")),
                io.Float.Input("nr_structure", default=1.0, min=0.0, max=2.0, step=0.05,
                               display_name=t("structure_tt").split(" (")[0],
                               tooltip=t("structure_tt")),
                io.Float.Input("nr_tone", default=1.0, min=0.0, max=2.0, step=0.05,
                               display_name=t("tone_tt").split(" (")[0],
                               tooltip=t("tone_tt")),
                io.Float.Input("nr_global_tone", default=-1.0, min=-1.0, max=2.0, step=0.05,
                               display_name=t("global_tone_tt").split("。")[0],
                               tooltip=t("global_tone_tt")),
                io.Boolean.Input("auto_mask", default=False,
                                 display_name=t("auto_mask_tt").split(" (")[0],
                                 tooltip=t("auto_mask_tt")),
            ],
            outputs=[
                io.Image.Output("images", display_name=t("images_tt")),
            ],
        )

    @classmethod
    def execute(cls, images, quality_preset, upscale_factor, output_width,
                batch_mode, self_check, nr_style, nr_intensity, nr_detail,
                nr_color, nr_skin, nr_structure, nr_tone, nr_global_tone,
                auto_mask, runtime=None, **kwargs):
        runtime = _runtime_or_raise(runtime)
        exe = os.path.join(RUNTIMES, runtime, "video2dlssnr.exe")

        pkey = _preset_key_of(quality_preset)
        eff = _effective(pkey, {
            "style": int(nr_style.split(" ")[0]),
            "intensity": float(nr_intensity), "detail": float(nr_detail),
            "color": float(nr_color), "skin": float(nr_skin),
            "local_structure": float(nr_structure), "local_tone": float(nr_tone),
            "global_tone": float(nr_global_tone), "auto_mask": bool(auto_mask)})

        opts = dict(eff)
        opts.update({"exe": exe, "scale": float(upscale_factor),
                     "width": int(output_width)})

        _log(t("log_start_image").format(n=images.shape[0], mode=batch_mode))
        _log(t("log_params").format(
            preset=quality_preset, scale=upscale_factor,
            intensity=opts["intensity"], detail=opts["detail"],
            style=opts["style"], skin=opts.get("skin", -1)))

        if self_check:
            ok, lines = _functional_self_test(runtime)
            for line in lines:
                _log(line)
            if not ok:
                raise RuntimeError("\n".join(lines))

        import numpy as np
        import torch
        from PIL import Image

        b, inH, inW = images.shape[0], images.shape[1], images.shape[2]
        outW, outH = engine._output_size(opts, inW, inH)
        sequence = batch_mode in (t("batch_sequence"),
                                  T["batch_sequence"]["en"])
        pbar_progress = _make_progress(_log, t("log_image"))

        try:
            if sequence and b > 1:
                frames = []
                for i in range(b):
                    _interrupt()
                    frames.append((images[i].detach().cpu().numpy() * 255.0)
                                  .round().clip(0, 255).astype("uint8"))
                t0 = time.perf_counter()
                outs, gpu_fps = engine.run_tensor_batch(
                    frames, inW, inH, outW, outH, opts,
                    progress_cb=pbar_progress,
                    log_cb=lambda line: _log(f"[exe] {line}"),
                    interrupt_check=_interrupt)
                wall = (time.perf_counter() - t0) or 1e-6
                _log(t("log_done").format(frames=b, mb=0,
                                          fps=max(gpu_fps, b / wall),
                                          sec=wall, w=outW, h=outH))
                return io.NodeOutput(torch.from_numpy(
                    np.stack(outs).astype("float32") / 255.0))

            outs = []
            with tempfile.TemporaryDirectory() as work:
                for i in range(b):
                    _interrupt()
                    arr = (images[i].detach().cpu().numpy() * 255.0
                           ).round().clip(0, 255).astype("uint8")
                    in_png = os.path.join(work, f"in_{i:05d}.png")
                    Image.fromarray(arr, "RGB").save(in_png)
                    out_png = engine.run_image(in_png, work, opts)
                    out = np.asarray(Image.open(out_png).convert("RGB"))
                    outs.append(torch.from_numpy(out.astype("float32") / 255.0))
                    pbar_progress(i + 1, b, 0.0)
            return io.NodeOutput(torch.stack(outs, dim=0))
        except engine.PipelineError as e:
            report = "\n".join(_quick_report(runtime))
            raise RuntimeError(f"{e}\n---- {t('err_diag')} ----\n{report}") from e


# image tooltip lives with the others
T["images_tt"] = {"zh": "输入图片 batch。", "en": "Input IMAGE batch."}
