"""Clip app: editor + library + live watcher. Run with run.bat."""
import json, re, shutil, subprocess, sys, threading, time, uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import library, media, watcher
from media import MediaError, WORK, OUT, probe, run

WEB = Path(__file__).parent / "web"
app = FastAPI()
app.include_router(library.router)
JOBS = {}


@app.exception_handler(MediaError)
async def media_error(_, e):
    return JSONResponse({"detail": str(e)}, status_code=500)


def source():
    return next((f for f in WORK.glob("source*") if f.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov", ".avi")), None)


def clear_source():
    for f in WORK.glob("source*"):
        f.unlink(missing_ok=True)


def normalize():
    """Browsers only play mp4/webm reliably, so convert anything else and use one fixed name."""
    f = source()
    if not f:
        raise MediaError("The download finished but no video file was found.")
    if f.suffix.lower() not in (".mp4", ".webm"):
        run(["ffmpeg", "-y", "-i", f.name, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
             "converted.mp4"], cwd=WORK)
        f.unlink()
        (WORK / "converted.mp4").rename(WORK / "source.mp4")
    elif f.name != "source.mp4" and f.suffix.lower() == ".mp4":
        f.rename(WORK / "source.mp4")


def info():
    f = source()
    if not f:
        return {"exists": False}
    w, h, d, a = probe(f)
    return {"exists": True, "url": f"/media/{f.name}?t={int(time.time())}", "w": w, "h": h, "duration": d,
            "has_audio": a}


@app.get("/api/health")
def health():
    try:
        import faster_whisper  # noqa: F401
        fw = True
    except ImportError:
        fw = False
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")), "whisper": fw,
            "version": 3}


@app.get("/api/source")
def get_source():
    return info()


# ---------------------------------------------------------------- link: load the whole video, with progress + time left
class UrlReq(BaseModel):
    url: str


def size_estimates(d):
    """Rough download size in bytes for each quality, from the formats yt-dlp reports."""
    fm, dur = d.get("formats") or [], d.get("duration") or 0
    size = lambda f: f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * dur
    audio = [f for f in fm if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    a = size(max(audio, key=lambda f: f.get("abr") or 0)) if audio else 0
    out = {}
    for q in (480, 720, 1080):
        vids = [f for f in fm if f.get("vcodec") not in (None, "none") and 0 < (f.get("height") or 0) <= q]
        avc = [f for f in vids if str(f.get("vcodec")).startswith("avc1")] or vids
        out[str(q)] = int(size(max(avc, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))) + a) if avc else 0
    return out


@app.post("/api/url/info")
def url_info(req: UrlReq):
    d = json.loads(run([sys.executable, "-m", "yt_dlp", "-J", "--no-playlist", "--skip-download", req.url]).stdout)
    return {"title": d.get("title", ""), "duration": d.get("duration"), "live": bool(d.get("is_live")),
            "sizes": size_estimates(d)}


class DlReq(BaseModel):
    url: str
    quality: int = 1080


def formats_for(q):
    return [f"bv*[height<={q}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={q}]+ba/b[height<={q}]/b",
            f"b[height<={q}]/b"]  # 2nd try: one combined stream, in case the first came without sound


def _download(jid, url, q):
    j = JOBS[jid]
    try:
        for fmt in formats_for(q):
            clear_source()
            j.update(pct=0, speed="", eta="", stage=0, msg="Starting the download")
            p = subprocess.Popen(
                [sys.executable, "-m", "yt_dlp", "-f", fmt, "--merge-output-format", "mp4", "--no-playlist",
                 "--no-colors", "--newline", "-N", "4", "-o", "source.%(ext)s", "--progress-template",
                 "download:PROG|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s", url],
                cwd=WORK, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1)
            tail = []
            for line in p.stdout:
                line = line.strip()
                tail = (tail + [line])[-10:]
                if line.startswith("PROG|"):
                    _, pct, speed, eta = (line.split("|") + ["", "", ""])[:4]
                    m = re.search(r"[\d.]+", pct)
                    j.update(pct=float(m.group()) if m else 0, speed=speed.strip(), eta=eta.strip())
                elif "Destination:" in line:
                    j["stage"] += 1
                elif "[Merger]" in line:
                    j.update(msg="Putting video and sound together", pct=100, eta="")
            if p.wait():
                raise MediaError("\n".join(tail)[-800:])
            j.update(msg="Preparing the video", eta="")
            normalize()
            if info()["has_audio"]:
                break
            j["msg"] = "No sound in that download, trying another format"
        j.update(state="done", info=info())
    except Exception as ex:
        j.update(state="error", msg=str(ex))


@app.post("/api/url/download")
def url_download(req: DlReq):
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"state": "running", "t0": time.time(), "msg": "Starting the download", "pct": 0,
                 "speed": "", "eta": "", "stage": 0}
    threading.Thread(target=_download, args=(jid, req.url, req.quality), daemon=True).start()
    return {"job": jid}


@app.get("/api/job/{jid}")
def job(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "Unknown job.")
    return {**j, "elapsed": int(time.time() - j["t0"])}


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    clear_source()
    with open(WORK / f"source{Path(file.filename).suffix.lower() or '.mp4'}", "wb") as out:
        shutil.copyfileobj(file.file, out)
    normalize()
    return info()


# ---------------------------------------------------------------- captions + render
class TranscribeReq(BaseModel):
    start: float
    end: float
    language: str = ""


@app.post("/api/transcribe")
def transcribe(req: TranscribeReq):
    f = source()
    if not f:
        raise HTTPException(400, "Load a video first.")
    return {"words": media.transcribe(f, req.start, req.end, req.language)}


class RenderReq(BaseModel):
    start: float
    end: float
    cam: list[float]
    game: list[float]
    words: list[dict] = []
    style: dict = {}
    hook: dict = {}
    layout: dict = {}
    auto_captions: bool = True
    label: str = ""


@app.post("/api/render")
def render(req: RenderReq):
    f = source()
    if not f:
        raise HTTPException(400, "Load a video first.")
    r = media.render_clip(f, req.start, req.end, req.cam, req.game, req.words, req.style, req.hook, req.layout,
                          req.auto_captions)
    cid = library.add_clip(r["file"], req.label or " ".join(w.get("t", "") for w in req.hook.get("words", [])))
    return {"file": f"/outputs/{r['file']}", "id": cid, "has_audio": r["has_audio"], "words": len(r["words"])}


# ---------------------------------------------------------------- defaults used by live clips
@app.get("/api/defaults")
def get_defaults():
    return library.get_json("defaults", {})


@app.post("/api/defaults")
def set_defaults(d: dict):
    library.set_json("defaults", d)
    return {"ok": True}


# ---------------------------------------------------------------- live watching
class WatchReq(BaseModel):
    url: str
    before: float = 30
    after: float = 5
    sensitivity: float = 3.0
    cooldown: float = 60


@app.post("/api/watch/start")
def watch_start(req: WatchReq):
    return {"id": watcher.start(req.url, req.before, req.after, req.sensitivity, req.cooldown)}


@app.get("/api/watch")
def watch_list():
    return {"sessions": [s.status() for s in watcher.SESSIONS.values()]}


@app.post("/api/watch/{sid}/stop")
def watch_stop(sid: str):
    watcher.get(sid).stop()
    return {"ok": True}


@app.post("/api/watch/{sid}/clipnow")
def watch_clipnow(sid: str):
    watcher.get(sid).clip_now()
    return {"ok": True}


app.mount("/media", StaticFiles(directory=WORK), name="media")
app.mount("/outputs", StaticFiles(directory=OUT), name="outputs")
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
