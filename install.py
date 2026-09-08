"""One-shot installer for ComfyUI-DLSS-NR runtime files.

Downloads everything the plugin needs from official sources and installs it
into runtimes/default/:

  - video2dlssnr.exe + forwarder + both NGX DLLs + bundled ffmpeg
    from the upstream project's GitHub release (v1.2 bundles all of them)
  - ffmpeg/ffprobe are optional: the plugin finds a system install first

Usage:
    python install.py            # check + install what is missing
    python install.py --force    # re-download even if files exist

Nothing NVIDIA-proprietary is distributed by this repository: all files are
fetched at install time from their own official release pages.
"""

import argparse
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(HERE, "runtimes", "default")

RELEASE_URL = ("https://github.com/DaniilSokolyuk/video2dlssnr/releases/"
               "download/v1.3/video2dlssnr_release.zip")
WANTED = [
    "video2dlssnr/out/video2dlssnr.exe",
    "video2dlssnr/out/nvngx.dll_dlssnr.dll",
    "video2dlssnr/out/nvngx_dlss.dll",
    "video2dlssnr/out/nvngx_dlssnr.dll",
]

# exe versions below this lack --nr-sr-preset and friends (upstream v1.3+)
MIN_EXE_BYTES = 441856

# ---- optional DLSSG frame generation runtime (DLSSNRFrameInterp node) ----
# The NGX dlssg runtime ships in NVIDIA's own DLSS SDK repo; the small D3D12
# worker binary comes from the upstream ComfyUI pack at a pinned commit.
# Both are verified against sha256 whitelists below before they land in
# runtimes/default/.
FG_COMMIT = "c755e274a405a7a47667bd567d489b6845066bcf"
FG_WORKER_URL = ("https://media.githubusercontent.com/media/Konohamaru04/"
                 "ComfyUI-NVIDIA-DLSS-Frame-Interpolation/" + FG_COMMIT +
                 "/bin/runtime/dlssg/dlssg-worker.exe")
FG_WORKER_SHA256 = ("8a747f9ed613842d5b8b34a811ad43bc1a9466540e2e5a0c8ef"
                    "4005f0db9e384")
DLSSG_SDK_TAG = "v310.7.0"
DLSSG_DLL_URL = ("https://raw.githubusercontent.com/NVIDIA/DLSS/refs/tags/" +
                 DLSSG_SDK_TAG + "/lib/Windows_x86_64/rel/nvngx_dlssg.dll")
DLSSG_DLL_SHA256 = ("135eaf0733c1e37381a8c28abcf7a862404a54132b81787c04e35d"
                    "09efc5e36f")


def log(msg):
    print(f"[install] {msg}", flush=True)


def have(name):
    return os.path.isfile(os.path.join(RUNTIME, name))


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, desc):
    log(f"downloading {desc} ...")
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "ComfyUI-DLSS-NR-installer"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = min(100, done * 100 // total)
                sys.stdout.write(f"\r  {pct:3d}%  ({done // 2**20} / {total // 2**20} MB)")
                sys.stdout.flush()
    sys.stdout.write("\n")
    os.replace(tmp, dest)
    return dest


def check_ffmpeg():
    from shutil import which
    if which("ffmpeg") and which("ffprobe"):
        return True
    try:
        import imageio_ffmpeg  # noqa
        return True
    except ImportError:
        return False


def install_fg():
    """Fetch the optional DLSSG frame-generation pair with hash pinning."""
    targets = [
        ("dlssg-worker.exe", FG_WORKER_URL, FG_WORKER_SHA256,
         f"dlssg-worker @ Konohamaru04 commit {FG_COMMIT[:12]}"),
        ("nvngx_dlssg.dll", DLSSG_DLL_URL, DLSSG_DLL_SHA256,
         f"nvngx_dlssg.dll @ NVIDIA/DLSS SDK {DLSSG_SDK_TAG} (official)"),
    ]
    ok = True
    for name, url, want, desc in targets:
        dest = os.path.join(RUNTIME, name)
        if have(name) and sha256_file(dest) == want:
            log(f"{name}: already installed (sha256 OK)")
            continue
        try:
            download(url, dest, desc)
        except Exception as e:
            log(f"  ! {name} download failed: {e}")
            ok = False
            continue
        got = sha256_file(dest)
        if got != want:
            os.remove(dest)
            log(f"  ! {name} sha256 mismatch (got {got[:16]}…), removed. "
                f"Upstream may have moved; do NOT install unverified binaries.")
            ok = False
        else:
            log(f"  installed {name} (sha256 OK)")
    if ok:
        log("DLSSG frame generation ready - the 'DLSS 视频插帧 (DLSSG)' node "
            "needs an RTX GPU, driver 610+ and HAGS enabled.")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download even if files already exist")
    ap.add_argument("--fg", action="store_true",
                    help="install the optional DLSSG frame-generation runtime "
                         "(dlssg-worker.exe + nvngx_dlssg.dll, sha256-pinned)")
    args = ap.parse_args()

    os.makedirs(RUNTIME, exist_ok=True)

    if args.fg:
        sys.exit(0 if install_fg() else 1)

    needed = [w for w in WANTED if args.force or not have(os.path.basename(w))]

    if not needed:
        log("all runtime files already present - nothing to do")
    else:
        missing_names = ", ".join(os.path.basename(n) for n in needed)
        log(f"missing: {missing_names}")
        # the release zip carries every component; one download covers all
        zpath = download(RELEASE_URL, os.path.join(HERE, "runtimes",
                                                   "upstream.zip"),
                         "video2dlssnr v1.3 release (all components)")
        log("extracting ...")
        with zipfile.ZipFile(zpath) as z:
            for member in WANTED:
                if member not in z.namelist():
                    log(f"  ! upstream zip has no {member}, skipping")
                    continue
                base = os.path.basename(member)
                if not args.force and have(base):
                    continue
                with z.open(member) as src, \
                        open(os.path.join(RUNTIME, base), "wb") as dst:
                    dst.write(src.read())
                log(f"  installed {base}")
        os.remove(zpath)

    # summary
    log("--- status ---")
    ok = True
    for w in WANTED:
        name = os.path.basename(w)
        present = have(name)
        ok &= present
        log(f"  {name}: {'OK' if present else 'MISSING'}")
    exe_path = os.path.join(RUNTIME, "video2dlssnr.exe")
    if have("video2dlssnr.exe") and \
            os.path.getsize(exe_path) < MIN_EXE_BYTES:
        log("  ! video2dlssnr.exe is an old upstream release; the SR preset")
        log("    and 10-bit options need v1.3. Re-run:  python install.py --force")
    for fg_name in ("dlssg-worker.exe", "nvngx_dlssg.dll"):
        if have(fg_name):
            log(f"  {fg_name}: OK (frame generation ready)")
        else:
            log(f"  {fg_name}: not installed (optional - the DLSSG frame")
            log("    interpolation node needs it:  python install.py --fg)")
    if not check_ffmpeg():
        log("  ffmpeg: not found - the VIDEO node needs it.")
        log("  install with:  winget install Gyan.FFmpeg")
    else:
        log("  ffmpeg: OK")

    if not have("nvngx_dlssnr.dll"):
        log("")
        log("NOTE: if nvngx_dlssnr.dll failed to install, get it from a game")
        log("that ships DLSS 5 Neural Rendering, or a community DLL pack.")
    if not ok:
        sys.exit(1)
    log("done - restart ComfyUI and enjoy")


if __name__ == "__main__":
    main()
