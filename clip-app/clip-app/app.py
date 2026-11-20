"""Clip app - step 1: source -> face cam / gameplay boxes -> 9:16 layout + word-by-word captions."""
import shutil, subprocess, sys, time
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
WORK, OUT, WEB = BASE / "workspace", BASE / "outputs", BASE / "web"
WORK.mkdir(exist_ok=True); OUT.mkdir(exist_ok=True)

W, H = 1080, 1920
CAM_H, GAME_Y = 720, 1100          # cam on top, captions in the middle band, gameplay at the bottom
GAME_H = H - GAME_Y
CAP_Y = (CAM_H + GAME_Y) // 2      # vertical centre of the caption band

app = FastAPI()


def run(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise HTTPException(500, f"'{cmd[0]}' was not found. Run install_tools.bat, then reopen the terminal.")
    if p.returncode:
        raise HTTPException(500, (p.stderr or p.stdout)[-1500:])
    return p


def source():
    return next(iter(WORK.glob("source.*")), None)


def clear_source():
    for f in WORK.glob("source.*"):
        f.unlink()


def normalize():
    """Browsers only play mp4/webm reliably, so convert anything else."""
    f = source()
    if f and f.suffix.lower() not in (".mp4", ".webm"):
        run(["ffmpeg", "-y", "-i", f.name, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "converted.mp4"], cwd=WORK)
        f.unlink()
        (WORK / "converted.mp4").rename(WORK / "source.mp4")


def probe(f):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "csv=p=0", str(f)]).stdout.split()
    w, h = r[0].strip(",").split(",")[:2]
    return int(w), int(h), float(r[-1].split(",")[-1])


def info():
    f = source()
    if not f:
        return {"exists": False}
    w, h, d = probe(f)
    return {"exists": True, "url": f"/media/{f.name}?t={int(time.time())}", "w": w, "h": h, "duration": d}


@app.get("/api/health")
def health():
    try:
        import faster_whisper  # noqa: F401
        fw = True
    except ImportError:
        fw = False
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")), "whisper": fw}


@app.get("/api/source")
def get_source():
    return info()


class UrlReq(BaseModel):
    url: str


@app.post("/api/url")
def load_url(req: UrlReq):
    clear_source()
    run([sys.executable, "-m", "yt_dlp", "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
         "--merge-output-format", "mp4", "--no-playlist", "-o", "source.%(ext)s", req.url], cwd=WORK)
    normalize()
    return info()


@app.post("/api/upload")
def upload(file: UploadFile = File(...)):
    clear_source()
    ext = Path(file.filename).suffix.lower() or ".mp4"
    with open(WORK / f"source{ext}", "wb") as out:
        shutil.copyfileobj(file.file, out)
    normalize()
    return info()


# ---------------------------------------------------------------- transcription
_models = {}


class TranscribeReq(BaseModel):
    start: float
    end: float
    language: str = ""


@app.post("/api/transcribe")
def transcribe(req: TranscribeReq):
    f = source()
    if not f:
        raise HTTPException(400, "Load a video first.")
    run(["ffmpeg", "-y", "-ss", str(req.start), "-t", str(req.end - req.start), "-i", f.name,
         "-vn", "-ac", "1", "-ar", "16000", "clip_audio.wav"], cwd=WORK)
    from faster_whisper import WhisperModel
    err = None
    for dev, ct in (("cuda", "float16"), ("cpu", "int8")):
        try:
            if dev not in _models:
                _models[dev] = WhisperModel("small", device=dev, compute_type=ct)
            segs, _ = _models[dev].transcribe(str(WORK / "clip_audio.wav"), word_timestamps=True,
                                              language=req.language or None, vad_filter=True)
            words = [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                     for s in segs for w in (s.words or [])]
            return {"words": words, "device": dev}
        except Exception as e:  # CUDA libs missing -> fall back to CPU
            _models.pop(dev, None)
            err = e
    raise HTTPException(500, f"Transcription failed: {err}")


# ---------------------------------------------------------------- captions (ASS) + render
def ass_color(hex_):
    h = hex_.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()  # ASS is BGR


def ts(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "")


def build_ass(words, style, hook, hook_secs, dur):
    font = style.get("font", "Impact")
    size = int(style.get("size", 96))
    main, hl = ass_color(style.get("color", "#ffffff")), ass_color(style.get("hl", "#ffe600"))
    chunk = max(1, int(style.get("chunk", 3)))
    upper = style.get("upper", True)
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Cap,{font},{size},{main},{main},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,9,2,5,60,60,0,1",
        f"Style: Hook,{font},64,&H00000000,&H00000000,&H00FFFFFF,&H00000000,-1,0,0,0,100,100,0,0,3,16,0,8,60,60,0,1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    words = [w for w in words if w.get("w", "").strip()]
    for i in range(0, len(words), chunk):
        grp = words[i:i + chunk]
        for j, w in enumerate(grp):
            end = grp[j + 1]["s"] if j + 1 < len(grp) else min(w["e"] + 0.2, dur)
            parts = []
            for k, x in enumerate(grp):
                t = clean(x["w"].upper() if upper else x["w"])
                if k == j:
                    parts.append(f"{{\\c{hl}\\fscx115\\fscy115}}{t}{{\\c{main}\\fscx100\\fscy100}}")
                else:
                    parts.append(t)
            out.append(f"Dialogue: 0,{ts(w['s'])},{ts(end)},Cap,,0,0,0,,{{\\an5\\pos({W // 2},{CAP_Y})}}{' '.join(parts)}")
    if hook.strip():
        end = dur if hook_secs <= 0 else min(hook_secs, dur)
        out.append(f"Dialogue: 1,{ts(0)},{ts(end)},Hook,,0,0,0,,{{\\an8\\pos({W // 2},70)}}{clean(hook.strip())}")
    (WORK / "captions.ass").write_text("\n".join(out), encoding="utf-8")


class RenderReq(BaseModel):
    start: float
    end: float
    cam: list[float]    # x, y, w, h  (0-1 of the video frame)
    game: list[float]
    words: list[dict] = []
    style: dict = {}
    hook: str = ""
    hook_secs: float = 4


def crop_expr(box, sw, sh):
    x, y, w, h = box
    cw, ch = max(2, int(w * sw) // 2 * 2), max(2, int(h * sh) // 2 * 2)
    cx, cy = min(int(x * sw), sw - cw), min(int(y * sh), sh - ch)
    return f"crop={cw}:{ch}:{max(cx, 0)}:{max(cy, 0)}"


@app.post("/api/render")
def render(req: RenderReq):
    f = source()
    if not f:
        raise HTTPException(400, "Load a video first.")
    dur = req.end - req.start
    if dur <= 0:
        raise HTTPException(400, "End must be after start.")
    sw, sh, _ = probe(f)
    build_ass(req.words, req.style, req.hook, req.hook_secs, dur)
    fit = lambda box, h: (f"{crop_expr(box, sw, sh)},scale={W}:{h}:force_original_aspect_ratio=increase,crop={W}:{h}")
    graph = (f"[0:v]{fit(req.cam, CAM_H)}[cam];[0:v]{fit(req.game, GAME_H)}[game];"
             f"color=c=black:s={W}x{H}:r=30[bg];[bg][cam]overlay=0:0:shortest=1[a];"
             f"[a][game]overlay=0:{GAME_Y}:shortest=1[b];[b]ass=captions.ass[v]")
    name = f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    run(["ffmpeg", "-y", "-ss", str(req.start), "-t", str(dur), "-i", f.name, "-filter_complex", graph,
         "-map", "[v]", "-map", "0:a?", "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(OUT / name)], cwd=WORK)
    return {"file": f"/outputs/{name}"}


app.mount("/media", StaticFiles(directory=WORK), name="media")
app.mount("/outputs", StaticFiles(directory=OUT), name="outputs")
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
