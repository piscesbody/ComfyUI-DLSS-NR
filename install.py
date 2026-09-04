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


def log(msg):
    print(f"[install] {msg}", flush=True)


def have(name):
    return os.path.isfile(os.path.join(RUNTIME, name))


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download even if files already exist")
    args = ap.parse_args()

    os.makedirs(RUNTIME, exist_ok=True)

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
