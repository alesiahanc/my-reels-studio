"""Reels Studio local server. Run: python3 server.py  (then open http://127.0.0.1:8765)

Only listens on 127.0.0.1, so nothing is reachable from other computers.
Uses only the Python standard library + FFmpeg.
"""

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import render

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser(os.environ.get("REELS_STUDIO_HOME", "~/ReelsStudio"))
LIBRARY = os.path.join(HOME, "Library")
REELS = os.path.join(HOME, "Reels")
CACHE = os.path.join(HOME, ".cache")
PORT = int(os.environ.get("REELS_STUDIO_PORT", "8765"))

for d in (LIBRARY, REELS, CACHE):
    os.makedirs(d, exist_ok=True)

_probe_cache = {}
_job = {"state": "idle", "message": "", "progress": 0, "result": None, "error": None, "started": 0}
_job_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers

def safe_lib_path(rel):
    # abspath (not realpath) so symlinks placed inside the library are allowed
    root = os.path.abspath(LIBRARY)
    p = os.path.abspath(os.path.join(root, rel))
    if not p.startswith(root + os.sep):
        raise ValueError("bad path")
    return p


def list_library():
    root = os.path.abspath(LIBRARY)
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if dirpath[len(root):].count(os.sep) > 4:
            continue
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            kind = render.media_type(full)
            if not kind:
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root)
            entry = {"file": rel, "type": kind, "size": st.st_size, "mtime": int(st.st_mtime)}
            if kind == "video" and render.capabilities()["ffmpeg"]:
                key = (full, st.st_mtime, st.st_size)
                if key not in _probe_cache:
                    try:
                        _probe_cache[key] = render.probe(full)
                    except Exception:
                        _probe_cache[key] = {"duration": 0, "error": True}
                info = _probe_cache[key]
                entry.update(duration=round(info.get("duration", 0), 2), hdr=info.get("hdr", False),
                             has_audio=info.get("has_audio", False), unreadable=info.get("error", False))
            out.append(entry)
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


def removed_stats():
    folder = os.path.join(HOME, "Removed from library")
    n, size = 0, 0
    if os.path.isdir(folder):
        for dirpath, _dirs, files in os.walk(folder):
            for fn in files:
                if fn.startswith("."):
                    continue
                n += 1
                try:
                    size += os.lstat(os.path.join(dirpath, fn)).st_size
                except OSError:
                    pass
    return {"count": n, "bytes": size}


def thumb_path(rel):
    full = safe_lib_path(rel)
    st = os.stat(full)
    h = hashlib.sha1(("%s|%s|%s" % (full, st.st_mtime, st.st_size)).encode()).hexdigest()[:16]
    p = os.path.join(CACHE, "thumbs", h + ".jpg")
    if not os.path.exists(p):
        render.make_thumbnail(full, p, CACHE)
    return p


def unique_name(folder, name):
    name = re.sub(r"[^\w .()\-]", "_", os.path.basename(name)).strip() or "upload"
    base, ext = os.path.splitext(name)
    cand, i = name, 1
    while os.path.exists(os.path.join(folder, cand)):
        cand = "%s (%d)%s" % (base, i, ext)
        i += 1
    return cand


def decode_png(data_url):
    if not data_url:
        return None
    m = re.match(r"data:image/png;base64,(.*)", data_url, re.S)
    return base64.b64decode(m.group(1)) if m else None


def list_reels():
    out = []
    for d in sorted(os.listdir(REELS), reverse=True):
        p = os.path.join(REELS, d, "reel.mp4")
        if os.path.exists(p):
            meta = {}
            mp = os.path.join(REELS, d, "project.json")
            if os.path.exists(mp):
                try:
                    meta = json.load(open(mp)).get("_result", {})
                except Exception:
                    pass
            out.append({"id": d, "video": "/reels/%s/reel.mp4" % d, "cover": "/reels/%s/cover.jpg" % d,
                        "duration": meta.get("duration")})
    return out[:30]


def run_job(project):
    def progress(msg, frac):
        with _job_lock:
            _job.update(message=msg, progress=round(frac, 3))

    try:
        stamp = time.strftime("%Y-%m-%d_%H%M%S")
        name = "%s_%s" % (stamp, project.get("template", "reel"))
        out_dir = os.path.join(REELS, name)
        clean = {k: v for k, v in project.items() if not k.endswith("_png")}
        for key in ("hook_png", "end_png", "watermark_png"):
            project[key] = decode_png(project.get(key))
        project["captions_png"] = [decode_png(c) if c else None for c in (project.get("captions_png") or [])]
        result = render.render_project(project, LIBRARY, out_dir, CACHE, progress)
        clean["_result"] = result
        with open(os.path.join(out_dir, "project.json"), "w") as f:
            json.dump(clean, f, indent=2)
        result.update(id=name, video="/reels/%s/reel.mp4" % name, cover="/reels/%s/cover.jpg" % name)
        with _job_lock:
            _job.update(state="done", result=result, message="Done", progress=1)
    except render.RenderError as e:
        with _job_lock:
            _job.update(state="error", error=str(e))
    except Exception as e:
        traceback.print_exc()
        with _job_lock:
            _job.update(state="error", error="Unexpected error: %s" % e)



# --------------------------------------------------------------------------- browser-rendered reels
# Templates drawn frame by frame in the browser (e.g. Spot the details) send JPEG frames here;
# they are piped straight into FFmpeg.

_frame_sessions = {}


def frames_start(meta):
    if not render.capabilities()["ffmpeg"]:
        raise render.RenderError("FFmpeg is not installed. In Terminal run: brew install ffmpeg")
    fps = int(meta.get("fps", 30))
    name = "%s_%s" % (time.strftime("%Y-%m-%d_%H%M%S"), re.sub(r"[^\w]", "", meta.get("template", "reel")) or "reel")
    out_dir = os.path.join(REELS, name)
    os.makedirs(out_dir, exist_ok=True)
    log = open(os.path.join(out_dir, ".ffmpeg.log"), "w")
    cmd = [render.FFMPEG, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-"]
    cmd += render.video_codec_args(final=True) + ["-r", str(fps), "-an", "-movflags", "+faststart",
                                                 os.path.join(out_dir, "reel.mp4")]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
    _frame_sessions[name] = {"proc": proc, "dir": out_dir, "frames": 0, "fps": fps, "log": log, "meta": meta}
    return name


def frames_append(sid, data, count):
    s = _frame_sessions[sid]
    try:
        s["proc"].stdin.write(data)
    except BrokenPipeError:
        raise render.RenderError("The video encoder stopped unexpectedly.")
    s["frames"] += count


def frames_finish(sid, cover_bytes):
    s = _frame_sessions.pop(sid)
    s["proc"].stdin.close()
    code = s["proc"].wait()
    s["log"].close()
    logp = os.path.join(s["dir"], ".ffmpeg.log")
    if code != 0:
        err = open(logp).read()[-1500:]
        raise render.RenderError("FFmpeg failed:\n" + err)
    os.remove(logp)
    if cover_bytes:
        with open(os.path.join(s["dir"], "cover.jpg"), "wb") as f:
            f.write(cover_bytes)
    name = os.path.basename(s["dir"])
    result = {"duration": round(s["frames"] / float(s["fps"]), 2), "parts": 1, "hdr_warning": False}
    meta = dict(s["meta"]); meta["_result"] = result
    with open(os.path.join(s["dir"], "project.json"), "w") as f:
        json.dump(meta, f, indent=2)
    result.update(id=name, video="/reels/%s/reel.mp4" % name, cover="/reels/%s/cover.jpg" % name)
    return result


def frames_abort(sid):
    s = _frame_sessions.pop(sid, None)
    if s:
        try:
            s["proc"].kill()
            s["log"].close()
        except Exception:
            pass

# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ReelsStudio/1.0"

    def log_message(self, fmt, *args):
        if "/api/job" not in (args[0] if args else ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None, download_name=None):
        if not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        length = max(0, end - start + 1)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if download_name:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % download_name)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(HERE, "static", "index.html"), "text/html; charset=utf-8")
            if path == "/api/status":
                return self.send_json({"caps": render.capabilities(), "library": LIBRARY, "reels": REELS,
                                       "templates": render.TEMPLATES})
            if path == "/api/library":
                return self.send_json({"items": list_library(), "removed": removed_stats()})
            if path == "/api/thumb":
                return self.send_file(thumb_path(q["file"][0]), "image/jpeg")
            if path == "/api/video":
                full = safe_lib_path(q["file"][0])
                if render.media_type(full) != "video":
                    return self.send_json({"error": "videos only"}, 400)
                ext = os.path.splitext(full)[1].lower()
                ctype = {".mov": "video/mp4", ".m4v": "video/mp4", ".mp4": "video/mp4", ".webm": "video/webm"}.get(ext)
                return self.send_file(full, ctype)
            if path == "/api/media":
                full = safe_lib_path(q["file"][0])
                if render.media_type(full) != "photo":
                    return self.send_json({"error": "photos only"}, 400)
                return self.send_file(render.prepare_photo(full, CACHE))
            if path == "/api/job":
                with _job_lock:
                    return self.send_json(dict(_job))
            if path == "/api/reels":
                return self.send_json({"items": list_reels()})
            m = re.match(r"^/fonts/([\w\-]+\.(woff2|ttf))$", path)
            if m:
                ctype = "font/woff2" if m.group(2) == "woff2" else "font/ttf"
                return self.send_file(os.path.join(HERE, "static", "fonts", m.group(1)), ctype)
            m = re.match(r"^/reels/([\w\-]+)/(reel\.mp4|cover\.jpg)$", path)
            if m:
                dl = None
                if "download" in q:
                    dl = "%s_%s" % (m.group(1), m.group(2))
                return self.send_file(os.path.join(REELS, m.group(1), m.group(2)), download_name=dl)
            return self.send_json({"error": "not found"}, 404)
        except render.RenderError as e:
            return self.send_json({"error": str(e)}, 500)
        except (ValueError, KeyError, FileNotFoundError) as e:
            return self.send_json({"error": str(e)}, 400)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        if url.path.startswith("/api/frames/"):
            data = self.rfile.read(length) if length else b""
            try:
                if url.path == "/api/frames/start":
                    return self.send_json({"id": frames_start(json.loads(data or b"{}"))})
                sid = q.get("id", [""])[0]
                if sid not in _frame_sessions:
                    return self.send_json({"error": "Export session expired. Please try again."}, 400)
                if url.path == "/api/frames/append":
                    frames_append(sid, data, int(q.get("n", ["1"])[0]))
                    return self.send_json({"ok": True})
                if url.path == "/api/frames/finish":
                    return self.send_json(frames_finish(sid, data))
                if url.path == "/api/frames/abort":
                    frames_abort(sid)
                    return self.send_json({"ok": True})
            except render.RenderError as e:
                frames_abort(q.get("id", [""])[0])
                return self.send_json({"error": str(e)}, 500)
            return self.send_json({"error": "not found"}, 404)
        if url.path == "/api/upload":
            name = unique_name(LIBRARY, q.get("name", ["upload"])[0])
            if not render.media_type(name):
                self.rfile.read(length)
                return self.send_json({"error": "Unsupported file type: %s" % name}, 400)
            tmp = os.path.join(LIBRARY, "." + name + ".part")
            with open(tmp, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                os.remove(tmp)
                return self.send_json({"error": "Upload interrupted"}, 400)
            os.rename(tmp, os.path.join(LIBRARY, name))
            return self.send_json({"file": name})

        body = json.loads(self.rfile.read(length) or b"{}")
        if url.path == "/api/render":
            with _job_lock:
                if _job["state"] == "running":
                    return self.send_json({"error": "A reel is already rendering."}, 409)
                _job.update(state="running", message="Starting", progress=0, result=None, error=None,
                            started=time.time())
            threading.Thread(target=run_job, args=(body,), daemon=True).start()
            return self.send_json({"ok": True})
        if url.path == "/api/shutdown":
            if self.headers.get("X-Reels-Studio") != "1":
                return self.send_json({"error": "forbidden"}, 403)
            self.send_json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if url.path in ("/api/remove_all", "/api/empty_removed"):
            if self.headers.get("X-Reels-Studio") != "1":
                return self.send_json({"error": "forbidden"}, 403)
            trash = os.path.join(HOME, "Removed from library")
            os.makedirs(trash, exist_ok=True)
            if url.path == "/api/remove_all":
                # Not a delete: every photo/video moves to ~/ReelsStudio/Removed from library
                moved = 0
                for e in list_library():
                    p = safe_lib_path(e["file"])
                    if os.path.lexists(p):
                        shutil.move(p, os.path.join(trash, unique_name(trash, os.path.basename(p))))
                        moved += 1
                return self.send_json({"ok": True, "moved": moved})
            # Permanent: only ever touches the Removed from library folder
            deleted = 0
            for name in os.listdir(trash):
                p = os.path.join(trash, name)
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                deleted += 1
            return self.send_json({"ok": True, "deleted": deleted})
        if url.path == "/api/remove":
            # Not a delete: the file is moved to ~/ReelsStudio/Removed from library (put it back in Library to restore)
            p = safe_lib_path(body.get("file", ""))
            if not os.path.lexists(p):
                return self.send_json({"error": "File not found"}, 404)
            trash = os.path.join(HOME, "Removed from library")
            os.makedirs(trash, exist_ok=True)
            dest = os.path.join(trash, unique_name(trash, os.path.basename(p)))
            shutil.move(p, dest)
            return self.send_json({"ok": True, "moved_to": dest})
        if url.path == "/api/open":
            target = {"library": LIBRARY, "reels": REELS,
                      "removed": os.path.join(HOME, "Removed from library")}.get(body.get("what"))
            if target and not os.path.isdir(target):
                os.makedirs(target, exist_ok=True)
            if body.get("reel"):
                target = os.path.join(REELS, re.sub(r"[^\w\-]", "", body["reel"]))
            if target and sys.platform == "darwin":
                subprocess.Popen(["open", target])
            return self.send_json({"ok": True, "path": target})
        return self.send_json({"error": "not found"}, 404)


def _port_free(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(port):
    """Use `port`; if an older Reels Studio is still running there, ask it to stop; else use the next free port."""
    if _port_free(port):
        return port
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/shutdown" % port, data=b"{}", method="POST",
                                     headers={"X-Reels-Studio": "1"})
        urllib.request.urlopen(req, timeout=2).read()
        for _ in range(30):
            time.sleep(0.1)
            if _port_free(port):
                print("  (Stopped the Reels Studio that was already running.)")
                return port
    except Exception:
        pass
    for p in range(port + 1, port + 30):
        if _port_free(p):
            print("  Note: port %d is busy (probably an older Reels Studio window that is still open)." % port)
            print("  Using port %d instead. You can close the old Terminal window.\n" % p)
            return p
    raise SystemExit("No free port found. Close other Reels Studio windows and try again.")


def main():
    caps = render.capabilities()
    print("\n  Reels Studio")
    print("  ------------")
    print("  Library folder : %s" % LIBRARY)
    print("  Finished reels : %s" % REELS)
    if not caps["ffmpeg"]:
        print("\n  !! FFmpeg not found. Install it with:  brew install ffmpeg\n")
    port = pick_port(PORT)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d" % port
    print("\n  Open %s  (keep this window open; press Ctrl+C to quit)\n" % url)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye!")


if __name__ == "__main__":
    main()
