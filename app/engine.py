"""Streaming engine.

One Channel per station. A channel runs only while someone is listening:
  producer: yt-dlp (audio to stdout, no files) -> ffmpeg (MP3 CBR) -> in-memory buffer
  pacer:    sends buffered MP3 at real-time speed to every listener (broadcast)
When the buffer is empty (start-up, slow network) the pacer sends silent MP3
frames so players never time out.
"""
import asyncio
import collections
import json
import logging
import os
import random
import subprocess
import sys

log = logging.getLogger("relayradio")

BITRATE = int(os.environ.get("BITRATE", "192"))  # kbps
BYTES_PER_SEC = BITRATE * 1000 // 8
LOOKAHEAD_SEC = 60          # how far the producer may run ahead of playback
HISTORY_SEC = 3             # burst sent to a new listener so playback starts fast
GRACE_SEC = int(os.environ.get("GRACE_SEC", "30"))  # keep running after last listener leaves
TICK = 0.1
ICY_METAINT = 16000
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
YTDLP = [sys.executable, "-m", "yt_dlp"]
READ_CHUNK = 16384

MP3_ARGS = [
    "-vn", "-map_metadata", "-1",
    "-c:a", "libmp3lame", "-b:a", f"{BITRATE}k", "-ar", "44100", "-ac", "2",
    "-id3v2_version", "0", "-write_xing", "0",
    "-f", "mp3", "pipe:1",
]


def make_silence() -> bytes:
    """About 0.5 s of silent MP3 frames in exactly the stream's format."""
    out = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.5", *MP3_ARGS],
        capture_output=True, check=True,
    ).stdout
    if not out:
        raise RuntimeError("ffmpeg could not create silence")
    return out


def ytdlp_base_args(cookies_path: str):
    args = ["--no-warnings", "--ignore-config"]
    if cookies_path and os.path.exists(cookies_path):
        args += ["--cookies", cookies_path]
    return args


async def _drain(stream, keep: collections.deque):
    while True:
        line = await stream.readline()
        if not line:
            return
        keep.append(line.decode(errors="replace").rstrip())


async def load_playlist(url: str, cookies_path: str):
    """Return [{'id', 'title'}] for a playlist, a video, or a video-in-playlist URL."""
    proc = await asyncio.create_subprocess_exec(
        *YTDLP, *ytdlp_base_args(cookies_path), "--flat-playlist", "-J", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("플레이리스트 불러오기 시간 초과")
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip()[-500:] or "yt-dlp 오류")
    data = json.loads(out)
    items = data.get("entries") if data.get("_type") == "playlist" else [data]
    result = []
    for e in items or []:
        if not e or not e.get("id"):
            continue
        title = e.get("title") or e["id"]
        if title in ("[Private video]", "[Deleted video]"):
            continue
        result.append({"id": e["id"], "title": title})
    return result


class Listener:
    def __init__(self, icy: bool):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=int(LOOKAHEAD_SEC / TICK))
        self.icy = icy
        self._count = 0
        self._last_title = None

    def encode(self, data: bytes, title: str) -> bytes:
        if not self.icy:
            return data
        out = bytearray()
        while data:
            n = min(len(data), ICY_METAINT - self._count)
            out += data[:n]
            data = data[n:]
            self._count += n
            if self._count == ICY_METAINT:
                out += self._meta(title)
                self._count = 0
        return bytes(out)

    def _meta(self, title: str) -> bytes:
        if title == self._last_title:
            return b"\x00"
        self._last_title = title
        text = "StreamTitle='{}';".format((title or "").replace("'", "’"))
        raw = text.encode("utf-8")[: 255 * 16]
        blocks = (len(raw) + 15) // 16
        return bytes([blocks]) + raw.ljust(blocks * 16, b"\x00")


class Channel:
    def __init__(self, manager: "Manager", slug: str):
        self.m = manager
        self.slug = slug
        self.listeners: set[Listener] = set()
        self.running = False
        self.buffer: collections.deque = collections.deque()  # (track, bytes)
        self.buffered = 0
        self.space = asyncio.Event()
        self.history: collections.deque = collections.deque()
        self.history_bytes = 0
        self.current = None        # track now being heard
        self.playlist_len = 0
        self.error = ""
        self._producer = None
        self._pacer = None
        self._stop_timer = None

    # ---------- status ----------
    @property
    def config(self):
        return self.m.store.station_config(self.slug)

    @property
    def title(self):
        return self.current["title"] if self.current else ""

    def status(self):
        return {
            "listeners": len(self.listeners),
            "running": self.running,
            "now": self.title,
            "tracks": self.playlist_len,
            "error": self.error,
        }

    # ---------- listeners ----------
    def add_listener(self, listener: Listener):
        self.listeners.add(listener)
        if self._stop_timer:
            self._stop_timer.cancel()
            self._stop_timer = None
        for chunk in self.history:
            listener.queue.put_nowait(chunk)
        if not self.running:
            self.start()

    def remove_listener(self, listener: Listener):
        self.listeners.discard(listener)
        if not self.listeners and self.running and not self._stop_timer:
            self._stop_timer = asyncio.get_running_loop().call_later(GRACE_SEC, self.stop)

    def disconnect_all(self):
        for l in list(self.listeners):
            _hang_up(l)
        self.listeners.clear()
        self.stop()

    # ---------- lifecycle ----------
    def start(self):
        log.info("[%s] start", self.slug)
        self.running = True
        self.error = ""
        self._pacer = asyncio.create_task(self._pace())
        self._start_producer(resume=True)

    def stop(self):
        self._stop_timer = None
        if not self.running:
            return
        log.info("[%s] stop", self.slug)
        self.running = False
        for t in (self._producer, self._pacer):
            if t:
                t.cancel()
        self._producer = self._pacer = None
        self._clear_buffer()
        self.history.clear()
        self.history_bytes = 0
        self.current = None

    def restart(self, skip_current=False):
        """Playlist changed (reload) or skip requested. Only matters while running."""
        if not self.running:
            return
        if skip_current and self.current:
            self.m.store.set_last_id(self.slug, self.current["id"])
        if self._producer:
            self._producer.cancel()
        self._clear_buffer()
        self.current = None
        self._start_producer(resume=True)

    def _start_producer(self, resume: bool):
        self._producer = asyncio.create_task(self._produce(resume))

    def _clear_buffer(self):
        self.buffer.clear()
        self.buffered = 0
        self.space.set()

    # ---------- producer ----------
    async def _produce(self, resume: bool):
        fails = 0
        first = resume
        while True:
            cfg = self.config
            if not cfg:
                return
            if not cfg["url"]:
                self.error = "재생할 플레이리스트가 없습니다. 공유하거나 주소를 입력하세요."
                await asyncio.sleep(5)
                continue
            try:
                entries = await load_playlist(cfg["url"], self.m.store.cookies_path)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error = f"플레이리스트 오류: {e}"
                log.warning("[%s] playlist error: %s", self.slug, e)
                await asyncio.sleep(30)
                continue
            if not entries:
                self.error = "플레이리스트에 재생할 곡이 없습니다."
                await asyncio.sleep(30)
                continue
            self.playlist_len = len(entries)
            self.error = ""
            order = list(range(len(entries)))
            if cfg["shuffle"]:
                random.shuffle(order)
            elif first and cfg["last_id"]:
                ids = [e["id"] for e in entries]
                if cfg["last_id"] in ids:
                    start = (ids.index(cfg["last_id"]) + 1) % len(ids)
                    order = order[start:] + order[:start]
            first = False
            for i in order:
                ok = await self._play_track(entries[i])
                if not ok:  # YouTube sometimes answers 403 once; retry before skipping
                    await asyncio.sleep(2)
                    ok = await self._play_track(entries[i])
                fails = 0 if ok else fails + 1
                if fails >= 5:
                    self.error = "연속으로 5곡 재생에 실패했습니다. 잠시 후 다시 시도합니다."
                    await asyncio.sleep(60)
                    fails = 0
            # end of playlist: loop (re-fetch so new songs show up)

    async def _push(self, track, data: bytes):
        while self.buffered > LOOKAHEAD_SEC * BYTES_PER_SEC:
            self.space.clear()
            await self.space.wait()
        self.buffer.append((track, data))
        self.buffered += len(data)

    async def _play_track(self, track) -> bool:
        url = f"https://www.youtube.com/watch?v={track['id']}"
        r, w = os.pipe()
        ytdlp = ffmpeg = None
        yt_err, ff_err = collections.deque(maxlen=20), collections.deque(maxlen=20)
        try:
            ytdlp = await asyncio.create_subprocess_exec(
                *YTDLP, *ytdlp_base_args(self.m.store.cookies_path),
                "-f", "bestaudio/best", "--no-playlist", "--quiet", "--no-part",
                "-o", "-", url,
                stdout=w, stderr=asyncio.subprocess.PIPE,
            )
            ffmpeg = await asyncio.create_subprocess_exec(
                FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *MP3_ARGS,
                stdin=r, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            os.close(w)
            os.close(r)
            w = r = None
            drains = [asyncio.create_task(_drain(ytdlp.stderr, yt_err)),
                      asyncio.create_task(_drain(ffmpeg.stderr, ff_err))]
            total = 0
            while True:
                data = await ffmpeg.stdout.read(READ_CHUNK)
                if not data:
                    break
                total += len(data)
                await self._push(track, data)
            await ffmpeg.wait()
            await ytdlp.wait()
            await asyncio.gather(*drains, return_exceptions=True)
            if total < BYTES_PER_SEC:  # less than ~1 s of audio: treat as failure
                msg = " / ".join(list(yt_err)[-3:] + list(ff_err)[-3:])
                log.warning("[%s] track failed %s: %s", self.slug, track["id"], msg)
                self.error = f"곡 재생 실패({track['title']}): {msg[-300:]}"
                return False
            return True
        finally:
            for fd in (r, w):
                if fd is not None:
                    os.close(fd)
            for p in (ytdlp, ffmpeg):
                if p and p.returncode is None:
                    try:
                        p.kill()
                    except ProcessLookupError:
                        pass

    # ---------- pacer ----------
    async def _pace(self):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        sent = 0
        while True:
            await asyncio.sleep(TICK)
            now = loop.time()
            target = (now - t0) * BYTES_PER_SEC
            if target - sent > 3 * BYTES_PER_SEC:  # event loop stalled: don't burst
                t0 = now - sent / BYTES_PER_SEC
                target = sent
            while sent < target:
                if self.buffer:
                    track, data = self.buffer[0]
                    need = int(target - sent) + 1
                    if len(data) > need:
                        self.buffer[0] = (track, data[need:])
                        data = data[:need]
                    else:
                        self.buffer.popleft()
                    self.buffered -= len(data)
                    self.space.set()
                    if self.current is not track:
                        self.current = track
                        self.m.store.set_last_id(self.slug, track["id"])
                        log.info("[%s] now playing: %s", self.slug, track["title"])
                else:
                    data = self.m.silence
                sent += len(data)
                self._broadcast(data)

    def _broadcast(self, data: bytes):
        self.history.append(data)
        self.history_bytes += len(data)
        while self.history_bytes > HISTORY_SEC * BYTES_PER_SEC:
            self.history_bytes -= len(self.history.popleft())
        for l in list(self.listeners):
            try:
                l.queue.put_nowait(data)
            except asyncio.QueueFull:  # client stopped reading
                self.listeners.discard(l)
                _hang_up(l)


def _hang_up(listener: Listener):
    """Tell a listener's connection handler to close (None = end of stream)."""
    try:
        listener.queue.put_nowait(None)
    except asyncio.QueueFull:
        try:
            listener.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        listener.queue.put_nowait(None)


class Manager:
    def __init__(self, store):
        self.store = store
        self.channels: dict[str, Channel] = {}
        self.silence = make_silence()

    def get(self, slug: str, create=True):
        if slug not in self.channels:
            if not create or not self.store.station_config(slug):
                return None
            self.channels[slug] = Channel(self, slug)
        return self.channels[slug]

    def remove(self, slug: str):
        ch = self.channels.pop(slug, None)
        if ch:
            ch.disconnect_all()

    def status(self, slug: str):
        ch = self.channels.get(slug)
        if ch:
            return ch.status()
        return {"listeners": 0, "running": False, "now": "", "tracks": 0, "error": ""}
