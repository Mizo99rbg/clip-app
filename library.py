"""Clip library: review (keep/delete) -> queue with a daily cap that overflows to the next day."""
import json, sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

BASE = Path(__file__).parent
OUT = BASE / "outputs"
DB = BASE / "clip.db"
FMT = "%Y-%m-%d %H:%M"
DEFAULTS = {"per_day": 9, "first": "08:00", "last": "23:00"}
router = APIRouter(prefix="/api")


@contextmanager
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT, label TEXT,"
                  " hook TEXT, created TEXT, status TEXT, sched TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")


init()


def get_settings(c):
    s = dict(DEFAULTS)
    for r in c.execute("SELECT k, v FROM settings"):
        if r["k"] in DEFAULTS:
            s[r["k"]] = int(r["v"]) if r["k"] == "per_day" else r["v"]
    return s


def slot_times(s):
    """Evenly spaced posting times between 'first' and 'last' (per_day of them)."""
    a, b = [int(x) * 60 + int(y) for x, y in (s["first"].split(":"), s["last"].split(":"))]
    n = s["per_day"]
    mins = [a] if n == 1 else [round(a + (b - a) * i / (n - 1)) for i in range(n)]
    return [f"{m // 60:02d}:{m % 60:02d}" for m in mins]


def next_slot(c, day=None):
    """Earliest free future slot. If a day is full the search moves on to the next day."""
    times = slot_times(get_settings(c))
    cutoff = datetime.now() + timedelta(minutes=5)
    taken = {r[0] for r in c.execute("SELECT sched FROM clips WHERE status='queued'")}
    days = [day] if day else [date.today() + timedelta(d) for d in range(400)]
    for d in days:
        for t in times:
            dt = datetime.strptime(f"{d.isoformat()} {t}", FMT)
            if dt >= cutoff and dt.strftime(FMT) not in taken:
                return dt.strftime(FMT)
    return None


def add_clip(file, label="", hook=""):
    with db() as c:
        cur = c.execute("INSERT INTO clips(file,label,hook,created,status) VALUES(?,?,?,?,'review')",
                        (Path(file).name, label, hook, datetime.now().strftime(FMT)))
        return cur.lastrowid


def row(r):
    d = dict(r)
    d["url"] = f"/outputs/{d['file']}"
    return d


def get_json(key, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return json.loads(r["v"]) if r else default


def set_json(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (key, json.dumps(value)))


def sync_outputs():
    """Any finished clip file that is not in the library yet is added to Review, so nothing gets lost."""
    with db() as c:
        known = {r["file"] for r in c.execute("SELECT file FROM clips")}
    for f in sorted(OUT.glob("*.mp4")):
        if f.name not in known:
            add_clip(f.name, f.stem)


@router.get("/library")
def library(status: str = "review"):
    sync_outputs()
    with db() as c:
        return {"clips": [row(r) for r in c.execute("SELECT * FROM clips WHERE status=? ORDER BY id", (status,))]}


@router.post("/library/{cid}/keep")
def keep(cid: int):
    with db() as c:
        slot = next_slot(c)
        if not slot:
            raise HTTPException(409, "No free slots in the next 400 days.")
        c.execute("UPDATE clips SET status='queued', sched=? WHERE id=?", (slot, cid))
    return {"sched": slot}


@router.post("/library/{cid}/delete")
def delete(cid: int):
    with db() as c:
        r = c.execute("SELECT file FROM clips WHERE id=?", (cid,)).fetchone()
        if r:
            (OUT / r["file"]).unlink(missing_ok=True)
            c.execute("DELETE FROM clips WHERE id=?", (cid,))
    return {"ok": True}


@router.post("/queue/{cid}/unqueue")
def unqueue(cid: int):
    with db() as c:
        c.execute("UPDATE clips SET status='review', sched=NULL WHERE id=?", (cid,))
    return {"ok": True}


class MoveReq(BaseModel):
    date: str


@router.post("/queue/{cid}/move")
def move(cid: int, req: MoveReq):
    try:
        day = date.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(400, "Pick a valid date.")
    if day < date.today():
        raise HTTPException(400, "That day is in the past.")
    with db() as c:
        c.execute("UPDATE clips SET sched=NULL WHERE id=?", (cid,))
        slot = next_slot(c, day)
        if not slot:
            raise HTTPException(409, "That day is full or its slots have passed. Pick another day.")
        c.execute("UPDATE clips SET sched=? WHERE id=?", (slot, cid))
    return {"sched": slot}


@router.post("/queue/repack")
def repack():
    """Refill the days in order, with no gaps (useful after changing the daily cap)."""
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM clips WHERE status='queued' ORDER BY sched, id")]
        c.execute("UPDATE clips SET sched=NULL WHERE status='queued'")
        for i in ids:
            c.execute("UPDATE clips SET sched=? WHERE id=?", (next_slot(c), i))
    return {"ok": True}


@router.get("/queue")
def queue():
    with db() as c:
        s = get_settings(c)
        days = {}
        for r in c.execute("SELECT * FROM clips WHERE status='queued' ORDER BY sched, id"):
            days.setdefault(r["sched"][:10], []).append(row(r))
    return {"settings": s, "days": [{"date": d, "cap": s["per_day"], "clips": v} for d, v in sorted(days.items())]}


class SettingsReq(BaseModel):
    per_day: int
    first: str
    last: str


@router.post("/settings")
def save_settings(req: SettingsReq):
    try:
        for t in (req.first, req.last):
            datetime.strptime(t, "%H:%M")
    except ValueError:
        raise HTTPException(400, "Times must look like 08:00.")
    if not 1 <= req.per_day <= 30:
        raise HTTPException(400, "Clips per day must be between 1 and 30.")
    with db() as c:
        for k, v in req.dict().items():
            c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    return {"ok": True}
