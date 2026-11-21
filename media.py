"""Core video work: ffmpeg helpers, transcription, captions, hook image and the 9:16 render."""
import json, shutil, subprocess, threading, time, uuid
from pathlib import Path

BASE = Path(__file__).parent
WORK, OUT = BASE / "workspace", BASE / "outputs"
WORK.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)
W, H = 1080, 1920
FONT_FILES = {"Impact": "impact.ttf", "Arial Black": "ariblk.ttf", "Segoe UI Black": "seguibl.ttf",
              "Verdana": "verdanab.ttf", "Arial": "arialbd.ttf"}


class MediaError(RuntimeError):
    pass


def run(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise MediaError(f"'{cmd[0]}' was not found. Run install_tools.bat, then reopen the terminal.")
    if p.returncode:
        raise MediaError((p.stderr or p.stdout)[-1500:])
    return p


def probe(f):
    """-> width, height, duration, has_audio"""
    d = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                        "-of", "json", str(f)]).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return int(v["width"]), int(v["height"]), float(d["format"].get("duration", 0)), \
        any(s["codec_type"] == "audio" for s in d["streams"])


# ------------------------------------------------------------------ transcription
_models, _lock = {}, threading.Lock()


def transcribe(src, start, end, lang=""):
    tmp = WORK / f"a_{uuid.uuid4().hex[:8]}.wav"
    try:
        run(["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start), "-i", str(src), "-vn", "-ac", "1",
             "-ar", "16000", str(tmp)])
        from faster_whisper import WhisperModel
        import wave
        import numpy as np
        with wave.open(str(tmp)) as wf:   # hand Whisper the sound directly (skips the 'av' library that broke)
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        err = None
        with _lock:
            for dev, ct in (("cuda", "float16"), ("cpu", "int8")):
                try:
                    if dev not in _models:
                        _models[dev] = WhisperModel("small", device=dev, compute_type=ct)
                    segs, _ = _models[dev].transcribe(audio, word_timestamps=True, language=lang or None,
                                                      vad_filter=True)
                    return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)}
                            for s in segs for w in (s.words or [])]
                except Exception as e:  # CUDA libraries missing -> CPU
                    _models.pop(dev, None)
                    err = e
        raise MediaError(f"Transcription failed: {err}")
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ captions (ASS)
def bgr(hex_):
    h = hex_.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ts(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(s):
    return s.replace("{", "(").replace("}", ")").replace("\\", "")


def build_ass(path, words, st, dur, cap_y):
    font, size = st.get("font", "Impact"), int(st.get("size", 96))
    main, hl, box = bgr(st.get("color", "#ffffff")), bgr(st.get("hl", "#ffe600")), bgr(st.get("box", "#8a3cff"))
    chunk, upper, boxmode = max(1, int(st.get("chunk", 3))), st.get("upper", True), st.get("mode") == "box"
    fmt = ("Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
           "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
           "MarginV,Encoding")
    out = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
           "[V4+ Styles]", fmt,
           f"Style: Cap,{font},{size},{main},{main},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,9,2,5,60,60,0,1",
           f"Style: Box,{font},{size},{main},{main},{box},&H00000000,-1,0,0,0,100,100,0,0,3,16,0,5,60,60,0,1",
           "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    pos = f"{{\\an5\\pos({W // 2},{cap_y})}}"
    words = [w for w in words if w.get("w", "").strip()]
    for i in range(0, len(words), chunk):
        grp = words[i:i + chunk]
        for j, w in enumerate(grp):
            end = grp[j + 1]["s"] if j + 1 < len(grp) else min(w["e"] + 0.2, dur)
            txt = [clean(x["w"].upper() if upper else x["w"]) for x in grp]
            show = " ".join(
                f"{{\\c{hl if (k == j and not boxmode) else main}\\fscx{115 if k == j else 100}"
                f"\\fscy{115 if k == j else 100}}}{t}" for k, t in enumerate(txt))
            out.append(f"Dialogue: 1,{ts(w['s'])},{ts(end)},Cap,,0,0,0,,{pos}{show}")
            if boxmode:  # layer 0: only the active word gets a box (other words invisible)
                hid = " ".join(
                    (f"{{\\alpha&H00&\\1a&HFF&\\fscx115\\fscy115}}{t}" if k == j else f"{{\\alpha&HFF&\\fscx100\\fscy100}}{t}")
                    for k, t in enumerate(txt))
                out.append(f"Dialogue: 0,{ts(w['s'])},{ts(end)},Box,,0,0,0,,{pos}{hid}")
    Path(path).write_text("\n".join(out), encoding="utf-8")


# ------------------------------------------------------------------ hook box (PNG)
def load_font(name, size):
    from PIL import ImageFont
    for f in (FONT_FILES.get(name, "arialbd.ttf"), "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def hook_png(path, hook):
    """White rounded box with per-word colours. hook = {words:[{t,c}], size, font, color, box, ...}"""
    from PIL import Image, ImageDraw
    words = [w for w in hook.get("words", []) if w.get("t", "").strip()]
    if not words:
        return None
    size, base = int(hook.get("size", 64)), hook.get("color", "#111111")
    f, pad = load_font(hook.get("font", "Arial"), size), int(size * 0.65)
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    sp, maxw = d.textlength(" ", font=f), int(W * 0.9) - 2 * pad
    lines, cur, curw = [], [], 0
    for w in words:
        ww = d.textlength(w["t"], font=f)
        if cur and curw + sp + ww > maxw:
            lines.append((cur, curw))
            cur, curw = [], 0
        curw += (sp if cur else 0) + ww
        cur.append((w, ww))
    lines.append((cur, curw))
    lh = int(size * 1.28)
    bw, bh = int(max(l[1] for l in lines)) + 2 * pad, lh * len(lines) + int(pad * 0.9)
    im = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=int(hook.get("radius", 44)), fill=hook.get("box", "#ffffff"))
    y = int(pad * 0.45)
    for items, lw in lines:
        x = (bw - lw) / 2
        for w, ww in items:
            dr.text((x, y), w["t"], font=f, fill=w.get("c") or base)
            x += ww + sp
        y += lh
    im.save(path)
    return bw, bh


# ------------------------------------------------------------------ the render
def crop_expr(box, sw, sh):
    x, y, w, h = box
    cw, ch = max(2, int(w * sw) // 2 * 2), max(2, int(h * sh) // 2 * 2)
    return f"crop={cw}:{ch}:{max(min(int(x * sw), sw - cw), 0)}:{max(min(int(y * sh), sh - ch), 0)}"


def render_clip(src, start, end, cam, game, words=(), style=None, hook=None, layout=None, auto_captions=True,
                lang=""):
    """Cut [start,end] of src into a 1080x1920 clip: cam on top, gameplay below, captions on the seam, hook box."""
    style, hook, layout = style or {}, hook or {}, layout or {}
    dur = end - start
    if dur <= 0:
        raise MediaError("End must be after start.")
    sw, sh, _, _ = probe(src)
    words = list(words)
    if auto_captions and not words:
        words = transcribe(src, start, end, lang)
    cam_h = max(400, min(1100, int(layout.get("cam_h", 700)))) // 2 * 2
    cap_y, game_h = int(layout.get("cap_y", cam_h)), H - cam_h
    tmp = WORK / f"r_{uuid.uuid4().hex[:8]}"
    tmp.mkdir()
    try:
        build_ass(tmp / "c.ass", words, style, dur, cap_y)
        hk = hook_png(tmp / "hook.png", hook)
        fit = lambda b, h: f"{crop_expr(b, sw, sh)},scale={W}:{h}:force_original_aspect_ratio=increase,crop={W}:{h}"
        g = (f"[0:v]{fit(cam, cam_h)}[cam];[0:v]{fit(game, game_h)}[game];color=c=black:s={W}x{H}:r=30[bg];"
             f"[bg][cam]overlay=0:0:shortest=1[a];[a][game]overlay=0:{cam_h}:shortest=1[b]")
        inputs = ["-ss", str(start), "-t", str(dur), "-i", str(src)]
        last = "b"
        if hk:
            secs = float(hook.get("secs", 3))
            hs = dur if secs <= 0 else min(secs, dur)
            fade = "fade=t=in:st=0:d=0.25:alpha=1" + (f",fade=t=out:st={max(hs - 0.25, 0)}:d=0.25:alpha=1" if secs > 0 else "")
            hx = (W - hk[0]) // 2
            hy = max(10, min(H - hk[1] - 10, int(float(hook.get("y", 34)) / 100 * H - hk[1] / 2)))
            inputs += ["-loop", "1", "-framerate", "30", "-t", str(dur), "-i", "hook.png"]
            g += (f";[1:v]format=rgba,{fade}[hk];[b][hk]overlay={hx}:{hy}:enable='between(t,0,{hs})'[b2]")
            last = "b2"
        g += f";[{last}]ass=c.ass[v]"
        name = f"clip_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.mp4"
        run(["ffmpeg", "-y", *inputs, "-filter_complex", g, "-map", "[v]", "-map", "0:a:0?", "-t", str(dur),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-b:a", "160k", "-ac", "2", "-ar", "48000", "-movflags", "+faststart", str(OUT / name)], cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"file": name, "has_audio": probe(OUT / name)[3], "words": words}
