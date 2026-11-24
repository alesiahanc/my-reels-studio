"""Reels Studio render engine.

Turns an ordered list of photos/videos into a 1080x1920 Instagram reel using FFmpeg.
Designed for very long source videos: long clips become "jump-cut timelapses" built
from many short, fast-seeked chunks, so a 3-hour recording renders as quickly as a
3-minute one.
"""

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

W, H, FPS = 1080, 1920, 30
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".3gp"}

TEMPLATES = {
    "reveal": "Reveal: finished piece as the hook, fast process, finished piece again",
    "timelapse": "Timelapse: your process compressed, ends on the last item",
    "progression": "Progression: quick cuts through photos/clips with slow zooms",
    "details": "Details: close-up clips at real speed, then the full piece",
    "custom": "Custom: items play in order, each with its own length, speed and motion",
}

# Per-item motion for photos. "" means the template decides.
PAN_MOTIONS = {"pan_lr", "pan_rl", "pan_tb", "pan_bt"}
SPEEDS = {0.25, 0.5, 1.0, 2.0, 4.0, 8.0}

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}

# Simple effects, applied to the whole reel (work with every template).
TRANSITIONS = {
    "cut": "Cut (no effect)",
    "fade": "Soft crossfade",
    "flash": "White flash",
}
LOOKS = {
    "none": "None",
    "warm": "Warm (cosy, slightly richer colour)",
    "film": "Soft film (warm, light grain, vignette)",
}
LOOK_FILTERS = {
    "warm": "colorbalance=rs=0.035:bs=-0.035:rm=0.025:bm=-0.025,eq=saturation=1.06:contrast=1.03",
    "film": "colorbalance=rs=0.04:bs=-0.04:rm=0.03:bm=-0.03,eq=brightness=0.03:saturation=0.97:contrast=1.03,"
            "vignette=angle=PI/8,noise=alls=5:allf=t",
}
FADE_SEC = 0.35   # crossfade length
# Transitions that blend two items (FFmpeg xfade name, seconds). "cut" and "flash" don't blend.
XFADES = {
    "fade": ("fade", 0.35),
    "fade_long": ("fade", 1.0),
    "fadewhite": ("fadewhite", 0.8),
    "zoomin": ("zoomin", 0.8),
    "circleopen": ("circleopen", 0.9),
}
FLASH_SEC = 0.12  # half-width of the white flash around each cut


class RenderError(Exception):
    pass


# --------------------------------------------------------------------------- tools

def find_tool(name):
    for p in (shutil.which(name), "/opt/homebrew/bin/" + name, "/usr/local/bin/" + name):
        if p and os.path.exists(p):
            return p
    return None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
_CAPS = None


def capabilities():
    """What the installed FFmpeg can do (encoder, HDR tone mapping)."""
    global _CAPS
    if _CAPS is not None:
        return _CAPS
    caps = {"ffmpeg": bool(FFMPEG and FFPROBE), "encoder": None, "hdr_tonemap": False, "stabilise": None}
    if caps["ffmpeg"]:
        enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        filt = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if " libx264 " in enc:
            caps["encoder"] = "libx264"
        elif "h264_videotoolbox" in enc:
            caps["encoder"] = "h264_videotoolbox"
        caps["hdr_tonemap"] = (" zscale " in filt) and (" tonemap " in filt)
        # vidstab (two passes, best) ships with Homebrew's FFmpeg; deshake is the built-in fallback.
        caps["stabilise"] = "vidstab" if " vidstabdetect " in filt else ("deshake" if " deshake " in filt else None)
    _CAPS = caps
    return caps


def video_codec_args(final=False):
    if capabilities()["encoder"] == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", "16M" if final else "24M", "-pix_fmt", "yuv420p"]
    cap = ["-maxrate", "16M", "-bufsize", "32M"] if final else []  # grain can otherwise bloat the file
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18" if final else "15",
            "-pix_fmt", "yuv420p", "-profile:v", "high"] + cap


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-15:])
        raise RenderError("FFmpeg failed:\n" + tail)
    return p


# --------------------------------------------------------------------------- media info

def media_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    return None


def probe(path):
    p = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,color_transfer:stream_side_data=rotation",
                        "-of", "json", path], capture_output=True, text=True)
    if p.returncode != 0:
        raise RenderError("Could not read %s" % os.path.basename(path))
    data = json.loads(p.stdout or "{}")
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False, "hdr": False}
    try:
        info["duration"] = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        pass
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not info["width"]:
            info["width"], info["height"] = s.get("width", 0), s.get("height", 0)
            info["hdr"] = s.get("color_transfer") in HDR_TRANSFERS
            for sd in s.get("side_data_list", []) or []:
                if abs(int(sd.get("rotation", 0) or 0)) in (90, 270):
                    info["width"], info["height"] = info["height"], info["width"]
        if s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def exif_orientation(path):
    """Read the EXIF orientation tag from a JPEG.

    Newer FFmpeg versions rotate JPEGs by themselves and older ones don't, so every
    photo command passes -noautorotate and applies this rotation explicitly.
    """
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return 1
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return 1
                size = struct.unpack(">H", f.read(2))[0]
                if marker[1] == 0xE1:
                    data = f.read(size - 2)
                    if data[:6] != b"Exif\x00\x00":
                        return 1
                    tiff = data[6:]
                    end = "<" if tiff[:2] == b"II" else ">"
                    ifd = struct.unpack(end + "I", tiff[4:8])[0]
                    count = struct.unpack(end + "H", tiff[ifd:ifd + 2])[0]
                    for i in range(count):
                        e = tiff[ifd + 2 + i * 12: ifd + 14 + i * 12]
                        if struct.unpack(end + "H", e[:2])[0] == 0x0112:
                            return struct.unpack(end + "H", e[8:10])[0]
                    return 1
                if marker[1] in (0xDA, 0xD9):
                    return 1
                f.seek(size - 2, 1)
    except Exception:
        return 1


ORIENT_FILTER = {2: "hflip", 3: "hflip,vflip", 4: "vflip", 5: "transpose=0",
                 6: "transpose=1", 7: "transpose=3", 8: "transpose=2"}


def prepare_photo(path, cache_dir):
    """HEIC -> JPEG via macOS sips (cached). Returns a path FFmpeg can read."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".heic", ".heif"):
        return path
    os.makedirs(cache_dir, exist_ok=True)
    st = os.stat(path)
    out = os.path.join(cache_dir, "%s_%d.jpg" % (os.path.basename(path), int(st.st_mtime)))
    if not os.path.exists(out):
        sips = find_tool("sips")
        if not sips:
            raise RenderError("HEIC photos need macOS (sips). Convert %s to JPEG." % os.path.basename(path))
        subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", "95", path, "--out", out],
                       capture_output=True)
        if not os.path.exists(out):
            raise RenderError("Could not convert %s" % os.path.basename(path))
    return out


def make_thumbnail(path, out_path, cache_dir):
    kind = media_type(path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if kind == "photo":
        src = prepare_photo(path, cache_dir)
        vf = ORIENT_FILTER.get(exif_orientation(src), "")
        vf = (vf + "," if vf else "") + "scale=480:-2"
        run([FFMPEG, "-y", "-v", "error", "-noautorotate", "-i", src, "-vf", vf, "-frames:v", "1", "-q:v", "4", out_path])
    else:
        info = probe(path)
        t = min(info["duration"] * 0.3, 30) if info["duration"] else 0
        vf = "scale=480:-2"
        if info["hdr"] and capabilities()["hdr_tonemap"]:
            vf = tonemap_chain() + "," + vf
        run([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % t, "-i", path, "-vf", vf,
             "-frames:v", "1", "-q:v", "4", out_path])


# --------------------------------------------------------------------------- filters

def tonemap_chain():
    return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def frame_chain(fit, fx, fy, w=W, h=H, tag=""):
    """Filters that turn any frame into exactly w x h (crop to focus point, or blurred-background fit)."""
    fx, fy = max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy))
    if fit == "blur":
        return ("split[fa{t}][fb{t}];"
                "[fa{t}]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
                "boxblur=12:2,eq=brightness=-0.06,scale={w}:{h}[bg{t}];"
                "[fb{t}]scale={w}:{h}:force_original_aspect_ratio=decrease[fg{t}];"
                "[bg{t}][fg{t}]overlay=(W-w)/2:(H-h)/2").format(sw=w // 4, sh=h // 4, w=w, h=h, t=tag)
    return ("scale={w}:{h}:force_original_aspect_ratio=increase,"
            "crop={w}:{h}:(iw-ow)*{fx:.4f}:(ih-oh)*{fy:.4f}").format(w=w, h=h, fx=fx, fy=fy)


def audio_chain(label, dur, volume):
    return ("[{l}]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,volume={v},"
            "afade=t=in:d=0.04,afade=t=out:st={fo:.3f}:d=0.04,apad,atrim=0:{d:.3f}").format(
        l=label, d=dur, v=volume, fo=max(0, dur - 0.04))


def silence(dur):
    return "anullsrc=r=48000:cl=stereo,atrim=0:%.3f" % dur


# --------------------------------------------------------------------------- segments

def photo_size(src):
    """Width/height of a photo as displayed (after EXIF rotation)."""
    p = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", src], capture_output=True, text=True)
    try:
        w, h = [int(x) for x in p.stdout.strip().split(",")[:2]]
    except ValueError:
        raise RenderError("Could not read %s" % os.path.basename(src))
    if exif_orientation(src) in (5, 6, 7, 8):
        w, h = h, w
    return w, h


def render_photo_pan(seg, out_path, src, frames):
    """Slide a 9:16 window across the photo (for wide or tall artwork).

    `zoom` makes the window smaller than the photo's full height/width, i.e. closer;
    `fy` (or `fx` for vertical pans) chooses which band of the photo the pan follows.
    """
    iw, ih = photo_size(src)
    z = max(1.0, min(4.0, float(seg.get("zoom") or 1.0)))
    if iw / ih > W / H:
        bh, bw = ih, ih * W / H
    else:
        bw, bh = iw, iw * H / W
    ww, wh = int(bw / z) // 2 * 2, int(bh / z) // 2 * 2
    ww, wh = max(2, min(ww, iw)), max(2, min(wh, ih))
    fx, fy = max(0.0, min(1.0, seg.get("fx", 0.5))), max(0.0, min(1.0, seg.get("fy", 0.5)))
    last = max(1, frames - 1)
    # Gentle ease in/out so the pan starts and stops softly.
    p = "(0.5*(n/{l})+0.5*(n/{l})*(n/{l})*(3-2*(n/{l})))".format(l=last)
    # Optional pan range (0..1 of the photo's free travel), e.g. to stop before the desk shows.
    a = max(0.0, min(1.0, float(seg.get("pan_from") if seg.get("pan_from") is not None else 0)))
    b = max(0.0, min(1.0, float(seg.get("pan_to") if seg.get("pan_to") is not None else 1)))
    p = "(%.4f+%.4f*%s)" % (a, b - a, p)
    m = seg["motion"]
    if m in ("pan_lr", "pan_rl"):
        p = p if m == "pan_lr" else "(1-%s)" % p
        x, y = "(iw-ow)*%s" % p, "(ih-oh)*%.4f" % fy
    else:
        p = p if m == "pan_tb" else "(1-%s)" % p
        x, y = "(iw-ow)*%.4f" % fx, "(ih-oh)*%s" % p
    orient = ORIENT_FILTER.get(exif_orientation(src), "")
    chain = ("[0:v]" + (orient + "," if orient else "") +
             "format=yuv420p,loop=loop={l}:size=1:start=0,setpts=N/{fps}/TB,"
             "crop={ww}:{wh}:x='{x}':y='{y}':exact=1,scale={w}:{h}:flags=lanczos,"
             "fps={fps},setsar=1,format=yuv420p[v];{sil}[a]").format(
        l=frames, fps=FPS, ww=ww, wh=wh, x=x, y=y, w=W, h=H, sil=silence(seg["dur"]))
    run([FFMPEG, "-y", "-v", "error", "-noautorotate", "-i", src, "-filter_complex", chain, "-map", "[v]", "-map", "[a]",
         "-frames:v", str(frames), "-t", "%.3f" % seg["dur"]] + video_codec_args() +
        ["-c:a", "aac", "-b:a", "192k", out_path])


def render_photo(seg, out_path, cache_dir):
    src = prepare_photo(seg["path"], cache_dir)
    frames = max(1, int(round(seg["dur"] * FPS)))
    motion = seg.get("motion", "in")
    if motion in PAN_MOTIONS and seg.get("fit", "fill") == "fill":
        return render_photo_pan(seg, out_path, src, frames)
    if motion in PAN_MOTIONS:
        motion = "in"  # "Whole + blur" shows everything already; a pan has nothing to reveal.
    orient = ORIENT_FILTER.get(exif_orientation(src), "")
    zmax = max(1.02, min(3.0, float(seg.get("zoom") or 1.10)))
    if motion == "in":
        z = "min(1+%.4f*on/%d,%.3f)" % (zmax - 1, frames, zmax)
    elif motion == "out":
        z = "max(%.3f-%.4f*on/%d,1)" % (zmax, zmax - 1, frames)
    else:
        z = "1"
    fx, fy = seg.get("fx", 0.5), seg.get("fy", 0.5)
    # Supersample 2x so the slow zoom does not wobble.
    chain = ("[0:v]" + (orient + "," if orient else "") + "format=yuv420p," +
             frame_chain(seg.get("fit", "fill"), fx, fy, W * 2, H * 2) +
             ",zoompan=z='{z}':x='(iw-iw/zoom)*{fx:.4f}':y='(ih-ih/zoom)*{fy:.4f}':d={n}:s={w}x{h}:fps={fps},"
             "setsar=1,format=yuv420p[v];{sil}[a]").format(
        z=z, fx=fx, fy=fy, n=frames, w=W, h=H, fps=FPS, sil=silence(seg["dur"]))
    run([FFMPEG, "-y", "-v", "error", "-noautorotate", "-i", src, "-filter_complex", chain, "-map", "[v]", "-map", "[a]",
         "-frames:v", str(frames), "-t", "%.3f" % seg["dur"]] + video_codec_args() +
        ["-c:a", "aac", "-b:a", "192k", out_path])


def plan_video_chunks(src_start, span, out_dur, max_speed=8.0):
    """How to squeeze `span` source seconds into `out_dur` output seconds.

    Returns a list of (source_start, source_len, output_len). Up to max_speed the
    footage is simply sped up (one chunk). Beyond that it becomes a jump-cut
    timelapse: evenly spaced chunks, each played at max_speed.
    """
    if span <= out_dur * 1.02:
        return [(src_start, span, span)]
    if span <= out_dur * max_speed:
        return [(src_start, span, out_dur)]
    chunk_out = 0.5
    k = max(2, int(round(out_dur / chunk_out)))
    k = min(k, 90)
    chunk_out = out_dur / k
    chunk_src = chunk_out * max_speed
    step = (span - chunk_src) / (k - 1)
    return [(src_start + i * step, chunk_src, chunk_out) for i in range(k)]


STAB_MAX_SPAN = 180.0  # longer ranges become jump-cut timelapses, where stabilising doesn't help


def stabilised_copy(path, start, span, info, cache_dir, progress=lambda m: None):
    """Stabilised copy of path[start:start+span], cached. Returns (path, info) or None if not possible.

    Only the used range is processed, once; later renders reuse the cached file.
    """
    mode = capabilities()["stabilise"]
    if not mode or span > STAB_MAX_SPAN:
        return None
    st = os.stat(path)
    key = "%s_%d_%d_%.2f_%.2f" % (os.path.basename(path), int(st.st_mtime), st.st_size, start, span)
    folder = os.path.join(cache_dir, "stabilised")
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "".join(c if c.isalnum() or c in "._-" else "_" for c in key) + ".mp4")
    if os.path.exists(out):
        return out, probe(out)
    progress("Stabilising %s (first time only)" % os.path.basename(path))
    hdr = info["hdr"] and capabilities()["hdr_tonemap"]
    pre = (tonemap_chain() + ",") if hdr else ""
    cut = ["-ss", "%.3f" % start, "-t", "%.3f" % span, "-i", path]
    tmp = out + ".part.mp4"
    if mode == "vidstab":
        trf = out + ".trf"
        run([FFMPEG, "-y", "-v", "error"] + cut +
            ["-vf", pre + "vidstabdetect=shakiness=6:accuracy=12:result=" + trf.replace(":", "\\:"), "-f", "null", "-"])
        vf = pre + ("vidstabtransform=input=%s:smoothing=25:optzoom=1:interpol=bicubic,unsharp=5:5:0.6"
                    % trf.replace(":", "\\:"))
    else:
        vf = pre + ("deshake=rx=32:ry=32,crop=trunc(iw*0.94/2)*2:trunc(ih*0.94/2)*2,"
                    "scale=trunc(iw/0.94/2)*2:trunc(ih/0.94/2)*2")
    try:
        run([FFMPEG, "-y", "-v", "error"] + cut + ["-vf", vf, "-pix_fmt", "yuv420p"] + video_codec_args() +
            ["-c:a", "aac", "-b:a", "192k", tmp])
        os.replace(tmp, out)
    finally:
        for f in (tmp, out + ".trf"):
            if os.path.exists(f):
                os.remove(f)
    return out, probe(out)


def render_video(seg, out_path, audio_mode, cache_dir=None, progress=lambda m: None):
    if seg.get("stabilise") and cache_dir:
        # Stabilise just the range this segment uses, then read from that copy.
        got = stabilised_copy(seg["path"], seg["src_start"], seg["src_span"], seg["info"], cache_dir, progress)
        if got:
            seg = dict(seg, path=got[0], info=got[1], src_start=0.0,
                       src_span=min(seg["src_span"], got[1]["duration"] or seg["src_span"]))
    info = seg["info"]
    chunks = plan_video_chunks(seg["src_start"], seg["src_span"], seg["out_dur"])
    hdr = info["hdr"] and capabilities()["hdr_tonemap"]
    use_audio = audio_mode != "silent" and info["has_audio"]
    volume = 0.35 if audio_mode == "quiet" else 1.0
    args, parts = [FFMPEG, "-y", "-v", "error"], []
    for i, (s, ln, out) in enumerate(chunks):
        args += ["-ss", "%.3f" % s, "-t", "%.3f" % (max(ln, out) + 0.1), "-i", seg["path"]]
        speed = ln / out
        v = "[{i}:v]{tm}{cr}setpts=(PTS-STARTPTS)/{sp:.5f},{fc},fps={fps},setsar=1,format=yuv420p," \
            "tpad=stop_mode=clone:stop_duration={o:.3f},trim=duration={o:.3f},setpts=PTS-STARTPTS[v{i}]".format(
                i=i, tm=(tonemap_chain() + ",") if hdr else "", cr=crop_filter(seg.get("crop")), sp=speed,
                fc=frame_chain(seg.get("fit", "fill"), seg.get("fx", 0.5), seg.get("fy", 0.5), tag=str(i)),
                fps=FPS, o=out)
        a = (audio_chain("%d:a" % i, out, volume) if use_audio else silence(out)) + "[a%d]" % i
        parts += [v, a]
    n = len(chunks)
    concat = "".join("[v%d][a%d]" % (i, i) for i in range(n)) + "concat=n=%d:v=1:a=1[v][a]" % n
    graph = ";".join(parts + [concat])
    total = sum(c[2] for c in chunks)
    run(args + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", "%.3f" % total] +
        video_codec_args() + ["-c:a", "aac", "-b:a", "192k", out_path])
    return total


# --------------------------------------------------------------------------- templates

def _item_span(item):
    info = item["info"]
    start = max(0.0, float(item.get("start") or 0))
    end = item.get("end")
    end = float(end) if end not in (None, "", 0) else info["duration"]
    end = min(end, info["duration"]) if info["duration"] else end
    return start, max(0.1, end - start)


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _pct(v):
    """Percent (0-100) from the UI -> fraction, or None."""
    try:
        return max(0.0, min(100.0, float(v))) / 100.0
    except (TypeError, ValueError):
        return None


def _speed(item):
    s = _num(item.get("speed"))
    return s if s in SPEEDS else None


def _fixed(item):
    """Output length the user chose for this item, or None to let the template decide."""
    length = _num(item.get("length"))
    if length:
        return max(0.3, min(60.0, length))
    if item["type"] == "video" and _speed(item):
        return _item_span(item)[1] / _speed(item)
    return None


def _crop(c):
    """Crop box from the clip viewer: fractions {x, y, w, h} of the frame, or None."""
    try:
        x, y, w, h = (float(c[k]) for k in ("x", "y", "w", "h"))
    except Exception:
        return None
    if w >= 0.995 and h >= 0.995:
        return None
    x, y = max(0.0, min(0.95, x)), max(0.0, min(0.95, y))
    w, h = max(0.05, min(1 - x, w)), max(0.05, min(1 - y, h))
    return {"x": x, "y": y, "w": w, "h": h}


def crop_filter(c):
    if not c:
        return ""
    return ("crop=w='trunc(iw*{w:.5f}/2)*2':h='trunc(ih*{h:.5f}/2)*2':x='iw*{x:.5f}':y='ih*{y:.5f}':exact=1,"
            .format(**c))


def _clip(item, dur, motion="in", mode="fit", use_item=True):
    """Segment showing `item` for about `dur` seconds.

    mode='fit': squeeze the whole trimmed range into dur (timelapse);
    mode='real': play dur seconds at real speed from the trim start;
    mode='middle': real speed, centred in the trimmed range.
    With use_item, the item's own length/speed/motion settings win over the template's.
    """
    base = {"path": item["path"], "fit": item.get("fit", "fill"),
            "fx": float(item.get("fx", 0.5)), "fy": float(item.get("fy", 0.5)),
            "zoom": _num(item.get("zoom")), "idx": item.get("_idx"), "captioned": use_item,
            "stabilise": bool(item.get("stabilise")), "crop": _crop(item.get("crop")),
            "trans_in": item.get("transition_in") or "",
            "pan_from": _pct(item.get("pan_from")), "pan_to": _pct(item.get("pan_to"))}
    if use_item:
        dur = _fixed(item) or dur
        motion = item.get("motion") or motion
    if item["type"] == "photo":
        return dict(base, kind="photo", dur=dur, motion=motion)
    start, span = _item_span(item)
    speed = _speed(item) if use_item else None
    if speed:
        sp = min(span, dur * speed)
        return dict(base, kind="video", info=item["info"], src_start=start, src_span=sp,
                    out_dur=sp / speed)
    if mode == "fit":
        s, sp = start, span
    elif mode == "middle":
        sp = min(span, dur)
        s = start + (span - sp) / 2
    else:
        s, sp = start, min(span, dur)
    return dict(base, kind="video", info=item["info"], src_start=s, src_span=sp,
                out_dur=min(dur, sp) if mode != "fit" else dur)


def _allocate(items, total):
    """Share `total` seconds between items; long videos get more time.

    Items with their own length (or speed) keep it; the rest share what is left.
    """
    fixed = [_fixed(it) for it in items]
    left = max(0.5 * fixed.count(None), total - sum(f for f in fixed if f))
    weights = []
    for it, f in zip(items, fixed):
        if f:
            weights.append(0.0)
        elif it["type"] == "photo":
            weights.append(1.0)
        else:
            weights.append(2.0 + min(3.0, _item_span(it)[1] / 600.0))
    s = sum(weights) or 1
    return [f if f else max(0.5, left * w / s) for f, w in zip(fixed, weights)]


def plan(project):
    items = project["items"]
    if not items:
        raise RenderError("Add at least one photo or video.")
    tpl = project.get("template", "reveal")
    T = max(5.0, min(180.0, float(project.get("duration", 20))))
    segs = []
    motions = ["in", "out"]
    if tpl == "reveal" and len(items) >= 2:
        final, process = items[-1], items[:-1]
        hook, end = 1.5, 2.5
        end = _fixed(final) or end
        segs.append(_clip(final, hook, "out", "real", use_item=False))
        for i, (it, d) in enumerate(zip(process, _allocate(process, T - hook - end))):
            segs.append(_clip(it, d, motions[i % 2], "fit"))
        segs.append(_clip(final, end, "in", "real"))
    elif tpl in ("reveal", "timelapse"):
        if len(items) == 1:
            segs.append(_clip(items[0], T, "in", "fit"))
        else:
            hold = 2.0
            for i, (it, d) in enumerate(zip(items[:-1], _allocate(items[:-1], T - hold))):
                segs.append(_clip(it, d, motions[i % 2], "fit"))
            segs.append(_clip(items[-1], hold, "in", "real"))
    elif tpl == "progression":
        n = len(items)
        fixed = [_fixed(it) for it in items]
        # Free items share what is left; the last one counts double.
        shares = sum((2 if i == n - 1 else 1) for i, f in enumerate(fixed) if not f)
        slot = max(0.4, (T - sum(f for f in fixed if f)) / shares) if shares else 0
        for i, it in enumerate(items):
            d = slot * 2 if i == n - 1 else slot
            segs.append(_clip(it, max(0.4, d), motions[i % 2], "fit"))
    elif tpl == "details":
        n = len(items)
        fixed = [_fixed(it) for it in items]
        shares = sum((2 if i == n - 1 else 1) for i, f in enumerate(fixed) if not f)
        free = T - sum(f for f in fixed if f)
        slot = max(1.0, min(4.0, free / shares)) if shares else 0
        for i, it in enumerate(items):
            last = i == n - 1
            segs.append(_clip(it, slot * (2 if last else 1), "out" if last else "in", "middle"))
    elif tpl == "custom":
        # Every item as set: photos 2.5 s by default, videos at real speed over their trim.
        for i, it in enumerate(items):
            if it["type"] == "photo":
                d = 2.5
            else:
                d = min(60.0, _item_span(it)[1])
            segs.append(_clip(it, d, motions[i % 2], "real"))
    else:
        raise RenderError("Unknown template: %s" % tpl)
    return segs


# --------------------------------------------------------------------------- main entry

def render_project(project, library_dir, out_dir, cache_dir, progress=lambda msg, frac: None):
    """Render a project dict to out_dir/reel.mp4 and out_dir/cover.jpg. Returns result info."""
    if not capabilities()["ffmpeg"]:
        raise RenderError("FFmpeg is not installed. In Terminal run: brew install ffmpeg")
    lib = os.path.abspath(library_dir)
    items = []
    for idx, it in enumerate(project.get("items", [])):
        it = dict(it, _idx=idx)
        path = os.path.abspath(os.path.join(lib, it["file"]))
        if not path.startswith(lib + os.sep) or not os.path.exists(path):
            raise RenderError("File not found in library: %s" % it.get("file"))
        kind = media_type(path)
        item = dict(it, path=path, type=kind)
        if kind == "video":
            item["info"] = probe(path)
        items.append(item)
    project = dict(project, items=items)
    segs = plan(project)

    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="work_", dir=out_dir)
    hdr_warn = any(s.get("info", {}).get("hdr") for s in segs) and not capabilities()["hdr_tonemap"]
    try:
        seg_files, total, seg_times = [], 0.0, []
        for i, seg in enumerate(segs):
            progress("Rendering part %d of %d" % (i + 1, len(segs)), i / (len(segs) + 1))
            out = os.path.join(work, "seg%03d.mp4" % i)
            seg_start = total
            if seg["kind"] == "photo":
                render_photo(seg, out, cache_dir)
                total += seg["dur"]
            else:
                total += render_video(seg, out, project.get("audio", "original"), cache_dir,
                                      lambda m, i=i: progress(m, i / (len(segs) + 1)))
            seg_files.append(out)
            seg_times.append((seg, seg_start, total))

        progress("Adding effects, text and finishing", len(segs) / (len(segs) + 1))
        transition = project.get("transition") or "cut"
        look = project.get("look") or "none"
        durs = [b - a for _, a, b in seg_times]
        joined = os.path.join(work, "joined.mp4")
        # Each cut uses the next item's own "transition in" if set, otherwise the reel setting.
        specs = [(seg.get("trans_in") or transition) for seg, _, _ in seg_times[1:]]
        cut_times = []
        if any(sp in XFADES for sp in specs):
            jargs, vparts, aparts = [FFMPEG, "-y", "-v", "error"], [], []
            for i, p in enumerate(seg_files):
                jargs += ["-i", p]
                vparts.append("[%d:v]settb=AVTB,fps=%d,format=yuv420p[sv%d]" % (i, FPS, i))
                aparts.append("[%d:a]aresample=48000[sa%d]" % (i, i))
            chain, lv, la, t = [], "sv0", "sa0", durs[0]
            new_times = [(seg_times[0][0], 0.0, durs[0])]
            for i in range(1, len(seg_files)):
                name, d = XFADES.get(specs[i - 1], ("fade", 1.0 / FPS))  # plain cut = one-frame blend
                d = max(1.0 / FPS, min(d, durs[i - 1] / 2, durs[i] / 2))
                off = t - d
                chain.append("[%s][sv%d]xfade=transition=%s:duration=%.3f:offset=%.3f[xv%d]" % (lv, i, name, d, off, i))
                chain.append("[%s][sa%d]acrossfade=d=%.3f[xa%d]" % (la, i, d, i))
                lv, la = "xv%d" % i, "xa%d" % i
                new_times.append((seg_times[i][0], off, off + durs[i]))
                if specs[i - 1] == "flash":
                    cut_times.append(off + d / 2)
                t = off + durs[i]
            total = t
            seg_times = new_times
            run(jargs + ["-filter_complex", ";".join(vparts + aparts + chain), "-map", "[%s]" % lv,
                         "-map", "[%s]" % la] + video_codec_args() + ["-c:a", "aac", "-b:a", "192k", joined])
        else:
            lst = os.path.join(work, "list.txt")
            with open(lst, "w") as f:
                for p in seg_files:
                    f.write("file '%s'\n" % p.replace("'", "'\\''"))
            run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
            cut_times = [a for (_, a, _), sp in zip(seg_times[1:], specs) if sp == "flash"]

        args = [FFMPEG, "-y", "-v", "error", "-i", joined]
        overlays = []  # (png_path, start, end)
        for key, fname in (("hook_png", "hook.png"), ("end_png", "end.png"), ("watermark_png", "wm.png")):
            data = project.get(key)
            if data:
                p = os.path.join(work, fname)
                with open(p, "wb") as f:
                    f.write(data)
                if key == "hook_png":
                    overlays.append((p, 0, float(project.get("hook_seconds", 2.5))))
                elif key == "end_png":
                    e = float(project.get("end_seconds", 2.5))
                    overlays.append((p, max(0, total - e), total + 1))
                else:
                    overlays.append((p, 0, total + 1))
        # Per-item text, shown while that item is on screen.
        captions = project.get("captions_png") or []
        for seg, a, b in seg_times:
            idx = seg.get("idx")
            if not seg.get("captioned") or idx is None or idx >= len(captions) or not captions[idx]:
                continue
            p = os.path.join(work, "cap%03d_%d.png" % (idx, int(a * 1000)))
            with open(p, "wb") as f:
                f.write(captions[idx])
            overlays.append((p, a, b))

        # Effects on the picture (before text, so text stays crisp and unflashed).
        fx = []
        if look in LOOK_FILTERS:
            fx.append(LOOK_FILTERS[look])
        if cut_times:
            pulse = "+".join("max(0,1-abs(t-%.3f)/%.3f)" % (c, FLASH_SEC) for c in cut_times)
            fx.append("eq=brightness='0.55*min(1,%s)':contrast='1-0.3*min(1,%s)':eval=frame" % (pulse, pulse))
        chain, last = [], "0:v"
        if fx:
            chain.append("[0:v]%s[fx]" % ",".join(fx))
            last = "fx"
        for i, (p, a, b) in enumerate(overlays):
            args += ["-loop", "1", "-i", p]
            chain.append("[%s][%d:v]overlay=0:0:shortest=1:enable='between(t,%.3f,%.3f)'[o%d]" % (last, i + 1, a, b, i))
            last = "o%d" % i
        reel = os.path.join(out_dir, "reel.mp4")
        if chain:
            args += ["-filter_complex", ";".join(chain) + ";[%s]format=yuv420p[vout]" % last,
                     "-map", "[vout]", "-map", "0:a"] + video_codec_args(final=True)
        else:
            args += ["-map", "0:v", "-map", "0:a", "-c:v", "copy"]
        if project.get("audio") == "silent":
            args += ["-an"]
        else:
            args += ["-c:a", "copy"]
        args += ["-t", "%.3f" % total, "-movflags", "+faststart", reel]
        run(args)

        cover_t = 0.7 if project.get("hook_png") else max(0, total - 1.0)
        cover = os.path.join(out_dir, "cover.jpg")
        run([FFMPEG, "-y", "-v", "error", "-ss", "%.3f" % cover_t, "-i", reel, "-frames:v", "1", "-q:v", "2", cover])
        progress("Done", 1.0)
        timeline = [{"item": seg.get("idx"), "file": os.path.basename(seg["path"]),
                     "start": round(a, 2), "end": round(b, 2)} for seg, a, b in seg_times]
        return {"duration": round(total, 2), "parts": len(segs), "hdr_warning": hdr_warn,
                "timeline": timeline}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    # CLI: python3 render.py project.json library_dir out_dir
    proj = json.load(open(sys.argv[1]))
    print(render_project(proj, sys.argv[2], sys.argv[3], os.path.join(sys.argv[3], ".cache"),
                         lambda m, f: print("%3d%% %s" % (f * 100, m))))
