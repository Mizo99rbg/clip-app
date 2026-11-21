"""Live watching: record the stream in a rolling buffer, read chat, spot spikes, cut + render clips into Review."""
import csv, random, re, socket, sys, threading, time, uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import library, media
from media import WORK, MediaError, run

SESSIONS = {}
RENDER_LOCK = threading.Lock()
HYPE = re.compile(r"(lmao|lmfao|lol|omg|wtf|no way|nooo|clip|pog|kekw|lul|dead|insane|crazy|cracked|\bw\b|"
                  r"😂|🤣|💀|😭|🔥|😱)", re.I)


class SpikeDetector:
    """A spike = many more chat messages in the last few seconds than in the minutes before."""

    def __init__(self, sensitivity=3.0, window=8, baseline=120, min_score=10, cooldown=60):
        self.sens, self.win, self.base, self.min, self.cool = sensitivity, window, baseline, min_score, cooldown
        self.ev, self.last, self.first = deque(), -1e9, None

    def add(self, t, text):
        self.first = self.first or t
        self.ev.append((t, 2.0 if HYPE.search(text) else 1.0))   # hype words count double

    def score(self, now):
        while self.ev and now - self.ev[0][0] > self.base + self.win:
            self.ev.popleft()
        return sum(w for t, w in self.ev if now - t <= self.win)

    def check(self, now):
        if self.first is None or now - self.first < 60:           # learn what normal chat looks like first
            return None
        recent = self.score(now)
        older = sum(w for t, w in self.ev if self.win < now - t)
        typical = max(older * self.win / self.base, 2.0)          # what a normal window looks like
        if recent >= self.min and recent >= typical * self.sens and now - self.last >= self.cool:
            self.last = now
            return recent
        return None


# ------------------------------------------------------------------ chat readers
def twitch_chat(channel, on_msg, stop):
    while not stop.is_set():
        try:
            s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=20)
            s.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{channel}\r\n".encode())
            buf = ""
            while not stop.is_set():
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data.decode("utf-8", "ignore")
                *lines, buf = buf.split("\r\n")
                for ln in lines:
                    if ln.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG " in ln:
                        on_msg(ln.split(" PRIVMSG ", 1)[1].split(" :", 1)[-1])
        except OSError:
            time.sleep(5)


def youtube_chat(url, on_msg, stop, note):
    try:
        from chat_downloader import ChatDownloader
        for m in ChatDownloader().get_chat(url):
            if stop.is_set():
                break
            on_msg(m.get("message") or "")
    except Exception as e:
        note(f"Chat could not be read ({str(e)[:120]}). The video is still recording; use Clip now.")


# ------------------------------------------------------------------ one watched stream
class Session:
    def __init__(self, url, before, after, sens, cooldown):
        self.id, self.url = uuid.uuid4().hex[:6], url
        self.before, self.after = before, after
        self.det = SpikeDetector(sens, cooldown=cooldown)
        self.dir = WORK / "live" / self.id
        self.dir.mkdir(parents=True)
        self.stop_ev, self.proc = threading.Event(), None
        self.state, self.title, self.msgs, self.clips, self.events, self.rate = "starting", url, 0, 0, [], 0
        host = urlparse(url).netloc.lower()
        self.platform = "twitch" if "twitch.tv" in host else "youtube"
        threading.Thread(target=self._main, daemon=True).start()

    def log(self, text):
        self.events.append(f"{time.strftime('%H:%M:%S')}  {text}")
        self.events = self.events[-30:]

    def status(self):
        return {"id": self.id, "url": self.url, "title": self.title, "platform": self.platform, "state": self.state,
                "msgs": self.msgs, "rate": round(self.rate, 1), "clips": self.clips, "events": self.events[-8:],
                "ready": library.get_json("defaults", {}).get("cam") is not None}

    def on_msg(self, text):
        self.msgs += 1
        self.det.add(time.time(), text)

    def _main(self):
        try:
            cmd = [sys.executable, "-m", "yt_dlp", "-g", "-f", "best[height<=1080]/best", "--no-playlist", self.url]
            urls = [u for u in run(cmd).stdout.split() if u.startswith("http")]
            if not urls:
                raise MediaError("Could not find a live stream at that link.")
            try:
                self.title = run([sys.executable, "-m", "yt_dlp", "-e", "--no-playlist", self.url]).stdout.strip() or self.url
            except MediaError:
                pass
            args = ["ffmpeg", "-loglevel", "error"]
            for u in urls:
                args += ["-i", u]
            if len(urls) == 2:
                args += ["-map", "0:v", "-map", "1:a"]
            args += ["-c", "copy", "-f", "segment", "-segment_time", "10", "-segment_format", "mpegts",
                     "-reset_timestamps", "1", "-segment_list", "list.csv", "-segment_list_type", "csv",
                     "seg_%06d.ts"]
            self.proc = __import__("subprocess").Popen(args, cwd=self.dir, stderr=__import__("subprocess").DEVNULL)
            if self.platform == "twitch":
                chan = urlparse(self.url).path.strip("/").split("/")[0].lower()
                threading.Thread(target=twitch_chat, args=(chan, self.on_msg, self.stop_ev), daemon=True).start()
            else:
                threading.Thread(target=youtube_chat, args=(self.url, self.on_msg, self.stop_ev, self.log),
                                 daemon=True).start()
            self.state = "watching"
            self.log("Watching. Chat spikes will become clips in Review.")
            while not self.stop_ev.is_set():
                time.sleep(1)
                if self.proc.poll() is not None:
                    self.state = "ended"
                    self.log("The stream ended or the connection dropped.")
                    return
                now = time.time()
                self.rate = self.det.score(now) / self.det.win
                if self.det.check(now):
                    self.log(f"Chat spike ({self.rate:.1f} msgs/s). Making a clip...")
                    threading.Thread(target=self.make_clip, args=(now,), daemon=True).start()
                self.prune()
        except Exception as e:
            self.state = "error"
            self.log(str(e)[-300:])

    def stop(self):
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.state != "error":
            self.state = "stopped"

    def clip_now(self):
        threading.Thread(target=self.make_clip, args=(time.time(), "Manual clip"), daemon=True).start()
        self.log("Clip now: cutting the last moments...")

    # -------------------------------------------------------------- buffer + cutting
    def segments(self):
        """[(file, start, end, finished_wall_time)] for segments that exist on disk."""
        out = []
        try:
            for row in csv.reader(open(self.dir / "list.csv", encoding="utf-8")):
                f = self.dir / row[0]
                if len(row) >= 3 and f.exists():
                    out.append((f, float(row[1]), float(row[2]), f.stat().st_mtime))
        except OSError:
            pass
        return out

    def prune(self):
        segs = self.segments()
        if segs:
            for f, _, end, _ in segs:
                if end < segs[-1][2] - 900:                      # keep the last 15 minutes
                    f.unlink(missing_ok=True)

    def cut(self, T, out_file):
        """Cut [T-before, T+after] (wall-clock T) from the buffer into one mp4. Returns its length."""
        deadline = time.time() + self.after + 45
        while True:
            segs = self.segments()
            if segs:
                _, _, end_last, mt = segs[-1]
                rec_T = end_last + (T - mt)                          # wall time -> position in the recording
                if end_last >= rec_T + self.after or time.time() > deadline:
                    break
            elif time.time() > deadline:
                raise MediaError("Nothing recorded yet.")
            time.sleep(1)
        a, b = max(0, rec_T - self.before), min(end_last, rec_T + self.after)
        use = [s for s in segs if s[2] > a and s[1] < b]
        if not use or b - a < 2:
            raise MediaError("Not enough video in the buffer yet.")
        lst = self.dir / f"cut_{uuid.uuid4().hex[:5]}.txt"
        lst.write_text("".join(f"file '{s[0].as_posix()}'\n" for s in use), encoding="utf-8")
        try:
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-ss", str(max(0, a - use[0][1])),
                 "-t", str(b - a), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
                 "-movflags", "+faststart", str(out_file)])
        finally:
            lst.unlink(missing_ok=True)
        return b - a

    def make_clip(self, T, why="Chat spike"):
        try:
            raw = self.dir / f"raw_{uuid.uuid4().hex[:6]}.mp4"
            dur = self.cut(T, raw)
            d = library.get_json("defaults", {})
            label = f"{why} {time.strftime('%H:%M', time.localtime(T))} - {self.title[:40]}"
            if not d.get("cam"):
                name = f"clip_live_{raw.stem}.mp4"                  # no layout saved yet: keep the raw cut
                raw.replace(media.OUT / name)
                library.add_clip(name, label + " (raw, save a layout first)")
            else:
                with RENDER_LOCK:
                    r = media.render_clip(raw, 0, dur, d["cam"], d["game"], [], d.get("style"), d.get("hook"),
                                          d.get("layout"), True)
                library.add_clip(r["file"], label)
                raw.unlink(missing_ok=True)
            self.clips += 1
            self.log(f"Clip ready in Review: {label}")
        except Exception as e:
            self.log(f"Clip failed: {str(e)[-200:]}")


def start(url, before, after, sens, cooldown):
    s = Session(url.strip(), before, after, sens, cooldown)
    SESSIONS[s.id] = s
    return s.id


def get(sid):
    from fastapi import HTTPException
    if sid not in SESSIONS:
        raise HTTPException(404, "Unknown session.")
    return SESSIONS[sid]
