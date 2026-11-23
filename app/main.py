"""RelayRadio: YouTube playlists as on-demand MP3 radio stations (web UI + streams)."""
import asyncio
import logging
import os
import re
import secrets
import sys

from aiohttp import web

from engine import BITRATE, ICY_METAINT, YTDLP, Listener, Manager
from store import PERSONAL_PREFIX, Store, StoreError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relayradio")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
STREAM_KEY = os.environ.get("STREAM_KEY", "")
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "1") == "1"
STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "relayradio_session"
URL_RE = re.compile(r"https?://\S+")

routes = web.RouteTableDef()


# ---------- helpers ----------
def err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


def current_user(request):
    return request.app["store"].check_session(request.cookies.get(COOKIE, ""))


def require_user(handler):
    async def wrapped(request):
        user = current_user(request)
        if not user:
            return err("로그인이 필요합니다.", 401)
        request["user"] = user
        try:
            return await handler(request)
        except StoreError as e:
            return err(str(e))
    return wrapped


def require_admin(handler):
    @require_user
    async def wrapped(request):
        if not request.app["store"].users[request["user"]]["admin"]:
            return err("관리자만 할 수 있습니다.", 403)
        return await handler(request)
    return wrapped


def base_url(request):
    return PUBLIC_URL or f"{request.scheme}://{request.host}"


def stream_url(request, slug):
    url = f"{base_url(request)}/{slug}.mp3"
    return f"{url}?key={STREAM_KEY}" if STREAM_KEY else url


def can_edit(store, user, slug):
    if slug.startswith(PERSONAL_PREFIX):
        return slug == PERSONAL_PREFIX + user
    return slug in store.stations


async def body(request):
    if request.content_type == "application/json":
        try:
            return await request.json()
        except ValueError:
            return {}
    return dict(await request.post())


# ---------- pages ----------
@routes.get("/")
async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"))


# ---------- streams ----------
@routes.get(r"/{slug:[a-z0-9][a-z0-9_-]*}.mp3")
async def stream(request):
    if STREAM_KEY and request.query.get("key") != STREAM_KEY:
        return web.Response(status=403, text="forbidden")
    slug = request.match_info["slug"]
    manager: Manager = request.app["manager"]
    ch = manager.get(slug)
    if not ch:
        return web.Response(status=404, text="no such station")
    icy = request.headers.get("Icy-MetaData") == "1"
    headers = {
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache, no-store",
        "icy-name": ch.config["name"],
        "icy-br": str(BITRATE),
        "icy-pub": "0",
    }
    if icy:
        headers["icy-metaint"] = str(ICY_METAINT)
    resp = web.StreamResponse(headers=headers)
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp
    listener = Listener(icy)
    ch.add_listener(listener)
    log.info("[%s] listener joined (%d)", slug, len(ch.listeners))
    try:
        while True:
            data = await listener.queue.get()
            if data is None:
                break
            await resp.write(listener.encode(data, ch.title))
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        ch.remove_listener(listener)
        log.info("[%s] listener left (%d)", slug, len(ch.listeners))
    return resp


# ---------- auth ----------
@routes.post("/api/login")
async def login(request):
    data = await body(request)
    store: Store = request.app["store"]
    user = store.authenticate(data.get("username"), data.get("password"))
    if not user:
        await asyncio.sleep(1)
        return err("아이디 또는 비밀번호가 틀렸습니다.", 401)
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, store.make_session(user), max_age=30 * 86400,
                    httponly=True, samesite="Lax")
    return resp


@routes.post("/api/logout")
async def logout(request):
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE)
    return resp


# ---------- state ----------
@routes.get("/api/state")
@require_user
async def state(request):
    store: Store = request.app["store"]
    manager: Manager = request.app["manager"]
    user = request["user"]
    me = store.users[user]

    def station(slug):
        cfg = store.station_config(slug)
        return {**{k: cfg[k] for k in ("slug", "name", "url", "shuffle", "personal")},
                "stream_url": stream_url(request, slug), **manager.status(slug)}

    result = {
        "me": {
            "username": user,
            "admin": me["admin"],
            "token": me["token"],
            "share_endpoint": f"{base_url(request)}/api/share",
        },
        "personal": station(PERSONAL_PREFIX + user),
        "stations": [station(s) for s in sorted(store.stations)],
    }
    if me["admin"]:
        result["users"] = [{"username": u, "admin": v["admin"]} for u, v in sorted(store.users.items())]
        result["cookies"] = os.path.exists(store.cookies_path)
        result["ytdlp_version"] = request.app.get("ytdlp_version", "")
    return web.json_response(result)


# ---------- share (iOS shortcut) ----------
@routes.post("/api/share")
async def share(request):
    data = await body(request)
    store: Store = request.app["store"]
    token = data.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = store.user_by_token(token)
    if not user:
        return err("토큰이 올바르지 않습니다.", 401)
    text = str(data.get("url") or data.get("text") or "")
    m = URL_RE.search(text)
    if not m:
        return err("공유한 내용에 주소가 없습니다.")
    url = m.group(0)
    slug = PERSONAL_PREFIX + user
    store.update_station(slug, url=url)
    ch = request.app["manager"].get(slug)
    if ch:
        ch.restart()
    log.info("[%s] shared %s", slug, url)
    return web.json_response({"ok": True, "message": "라디오에서 재생합니다.", "url": url})


# ---------- stations ----------
@routes.post("/api/stations")
@require_user
async def add_station(request):
    d = await body(request)
    slug = request.app["store"].add_station(
        d.get("slug", ""), d.get("name", ""), d.get("url", ""), bool(d.get("shuffle")))
    return web.json_response({"ok": True, "slug": slug})


@routes.put("/api/stations/{slug}")
@require_user
async def edit_station(request):
    store: Store = request.app["store"]
    slug = request.match_info["slug"]
    if not can_edit(store, request["user"], slug):
        return err("수정할 수 없는 방송국입니다.", 403)
    d = await body(request)
    changed = store.update_station(slug, name=d.get("name"), url=d.get("url"),
                                   shuffle=d.get("shuffle"))
    ch = request.app["manager"].get(slug, create=False)
    if ch and (changed or "shuffle" in d):
        ch.restart()
    return web.json_response({"ok": True})


@routes.delete("/api/stations/{slug}")
@require_user
async def delete_station(request):
    store: Store = request.app["store"]
    slug = request.match_info["slug"]
    if slug not in store.stations:
        return err("없는 방송국입니다.", 404)
    request.app["manager"].remove(slug)
    store.delete_station(slug)
    return web.json_response({"ok": True})


@routes.post("/api/stations/{slug}/skip")
@require_user
async def skip(request):
    store: Store = request.app["store"]
    slug = request.match_info["slug"]
    if not can_edit(store, request["user"], slug):
        return err("조작할 수 없는 방송국입니다.", 403)
    ch = request.app["manager"].get(slug, create=False)
    if ch:
        ch.restart(skip_current=True)
    return web.json_response({"ok": True})


@routes.post("/api/me/save-share")
@require_user
async def save_share(request):
    store: Store = request.app["store"]
    d = await body(request)
    url = store.users[request["user"]].get("share_url", "")
    if not url:
        return err("아직 공유한 플레이리스트가 없습니다.")
    slug = store.add_station(d.get("slug", ""), d.get("name", ""), url, bool(d.get("shuffle")))
    return web.json_response({"ok": True, "slug": slug})


# ---------- my account ----------
@routes.post("/api/me/password")
@require_user
async def change_password(request):
    store: Store = request.app["store"]
    d = await body(request)
    if not store.authenticate(request["user"], d.get("old", "")):
        return err("현재 비밀번호가 틀렸습니다.")
    store.set_password(request["user"], d.get("new", ""))
    return web.json_response({"ok": True})


@routes.post("/api/me/token")
@require_user
async def new_token(request):
    token = request.app["store"].regenerate_token(request["user"])
    return web.json_response({"ok": True, "token": token})


# ---------- admin ----------
@routes.post("/api/users")
@require_admin
async def add_user(request):
    d = await body(request)
    request.app["store"].add_user(d.get("username", ""), d.get("password", ""), bool(d.get("admin")))
    return web.json_response({"ok": True})


@routes.delete("/api/users/{username}")
@require_admin
async def delete_user(request):
    username = request.match_info["username"]
    if username == request["user"]:
        return err("자기 자신은 삭제할 수 없습니다.")
    request.app["manager"].remove(PERSONAL_PREFIX + username)
    request.app["store"].delete_user(username)
    return web.json_response({"ok": True})


@routes.post("/api/users/{username}/password")
@require_admin
async def reset_password(request):
    username = request.match_info["username"]
    store: Store = request.app["store"]
    if username not in store.users:
        return err("없는 사용자입니다.", 404)
    d = await body(request)
    store.set_password(username, d.get("password", ""))
    return web.json_response({"ok": True})


@routes.post("/api/cookies")
@require_admin
async def upload_cookies(request):
    store: Store = request.app["store"]
    text = ""
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        part = await reader.next()
        if part is not None:
            text = (await part.read()).decode("utf-8", errors="replace")
    else:
        text = await request.text()
    if "youtube.com" not in text:
        return err("유튜브 쿠키가 들어 있는 cookies.txt 파일이 아닙니다.")
    with open(store.cookies_path, "w", encoding="utf-8") as f:
        f.write(text)
    return web.json_response({"ok": True})


@routes.delete("/api/cookies")
@require_admin
async def delete_cookies(request):
    path = request.app["store"].cookies_path
    if os.path.exists(path):
        os.remove(path)
    return web.json_response({"ok": True})


@routes.post("/api/ytdlp/update")
@require_admin
async def ytdlp_update(request):
    ok, output = await update_ytdlp(request.app)
    return web.json_response({"ok": ok, "version": request.app.get("ytdlp_version"), "output": output[-1500:]})


# ---------- yt-dlp maintenance ----------
async def read_ytdlp_version(app):
    proc = await asyncio.create_subprocess_exec(*YTDLP, "--version", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    app["ytdlp_version"] = out.decode().strip()


async def update_ytdlp(app):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "yt-dlp[default]",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    await read_ytdlp_version(app)
    log.info("yt-dlp update finished (%s): %s", proc.returncode, app["ytdlp_version"])
    return proc.returncode == 0, out.decode(errors="replace")


async def auto_update_loop(app):
    while True:
        try:
            await update_ytdlp(app)
        except Exception as e:  # noqa: BLE001
            log.warning("yt-dlp update failed: %s", e)
        await asyncio.sleep(24 * 3600)


# ---------- app ----------
async def on_startup(app):
    await read_ytdlp_version(app)
    log.info("yt-dlp %s", app["ytdlp_version"])
    if AUTO_UPDATE:
        app["updater"] = asyncio.create_task(auto_update_loop(app))


async def on_cleanup(app):
    for ch in list(app["manager"].channels.values()):
        ch.disconnect_all()
    if "updater" in app:
        app["updater"].cancel()


def create_app():
    store = Store(DATA_DIR)
    if not store.users:
        user = os.environ.get("ADMIN_USER", "admin").lower()
        password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        store.add_user(user, password, admin=True)
        if not os.environ.get("ADMIN_PASSWORD"):
            log.warning("관리자 계정을 만들었습니다: %s / %s  (로그인 후 비밀번호를 바꾸세요)", user, password)
    app = web.Application(client_max_size=5 * 1024 * 1024)
    app["store"] = store
    app["manager"] = Manager(store)
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=PORT, access_log=None)
