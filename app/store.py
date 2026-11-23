"""Persistent state: users, stations, playback positions (data/state.json)."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

USERNAME_RE = re.compile(r"^[a-z0-9_]{1,20}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RESERVED_SLUGS = {"api", "static", "login", "logout", "index", "favicon"}
PERSONAL_PREFIX = "streaming-"
SESSION_DAYS = 30


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(test.hex(), digest)


class StoreError(Exception):
    pass


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "state.json")
        self.cookies_path = os.path.join(data_dir, "cookies.txt")
        self._lock = threading.RLock()
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {}
        self.data.setdefault("users", {})
        self.data.setdefault("stations", {})
        self.data.setdefault("secret", secrets.token_hex(32))
        self.save()

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    # ---------- users ----------
    @property
    def users(self):
        return self.data["users"]

    def add_user(self, username: str, password: str, admin: bool = False):
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise StoreError("사용자 이름은 영문 소문자, 숫자, _ 로 20자 이내여야 합니다.")
        if username in self.users:
            raise StoreError("이미 있는 사용자입니다.")
        if len(password) < 4:
            raise StoreError("비밀번호는 4자 이상이어야 합니다.")
        with self._lock:
            self.users[username] = {
                "pw": hash_password(password),
                "admin": admin,
                "token": secrets.token_urlsafe(24),
                "share_url": "",
                "shuffle": False,
                "last_id": None,
            }
            self.save()

    def delete_user(self, username: str):
        with self._lock:
            if username not in self.users:
                raise StoreError("없는 사용자입니다.")
            admins = [u for u, v in self.users.items() if v["admin"]]
            if self.users[username]["admin"] and len(admins) <= 1:
                raise StoreError("마지막 관리자는 삭제할 수 없습니다.")
            del self.users[username]
            self.save()

    def set_password(self, username: str, password: str):
        if len(password) < 4:
            raise StoreError("비밀번호는 4자 이상이어야 합니다.")
        with self._lock:
            self.users[username]["pw"] = hash_password(password)
            self.save()

    def regenerate_token(self, username: str) -> str:
        with self._lock:
            token = secrets.token_urlsafe(24)
            self.users[username]["token"] = token
            self.save()
            return token

    def user_by_token(self, token: str):
        if not token:
            return None
        for name, u in self.users.items():
            if hmac.compare_digest(u["token"], token):
                return name
        return None

    def authenticate(self, username: str, password: str):
        username = (username or "").strip().lower()
        u = self.users.get(username)
        if u and check_password(password or "", u["pw"]):
            return username
        return None

    # ---------- sessions (signed cookie) ----------
    def make_session(self, username: str) -> str:
        exp = int(time.time()) + SESSION_DAYS * 86400
        msg = f"{username}:{exp}"
        sig = hmac.new(self.data["secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
        return f"{msg}:{sig}"

    def check_session(self, value: str):
        try:
            username, exp, sig = (value or "").rsplit(":", 2)
            exp_i = int(exp)
        except ValueError:
            return None
        msg = f"{username}:{exp}"
        good = hmac.new(self.data["secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig) or exp_i < time.time():
            return None
        return username if username in self.users else None

    # ---------- stations ----------
    @property
    def stations(self):
        return self.data["stations"]

    def station_config(self, slug: str):
        """Unified view of a shared station or a personal streaming channel."""
        if slug.startswith(PERSONAL_PREFIX):
            owner = slug[len(PERSONAL_PREFIX):]
            u = self.users.get(owner)
            if not u:
                return None
            return {
                "slug": slug,
                "name": f"{owner}의 스트리밍",
                "url": u.get("share_url", ""),
                "shuffle": u.get("shuffle", False),
                "last_id": u.get("last_id"),
                "personal": True,
                "owner": owner,
            }
        s = self.stations.get(slug)
        if not s:
            return None
        return {
            "slug": slug,
            "name": s["name"],
            "url": s["url"],
            "shuffle": s.get("shuffle", False),
            "last_id": s.get("last_id"),
            "personal": False,
            "owner": None,
        }

    def validate_slug(self, slug: str):
        if not SLUG_RE.match(slug):
            raise StoreError("주소 이름은 영문 소문자, 숫자, -, _ 로 40자 이내여야 합니다.")
        if slug in RESERVED_SLUGS or slug.startswith(PERSONAL_PREFIX):
            raise StoreError("사용할 수 없는 주소 이름입니다.")

    def add_station(self, slug: str, name: str, url: str, shuffle: bool):
        slug = slug.strip().lower()
        self.validate_slug(slug)
        if slug in self.stations:
            raise StoreError("이미 있는 주소 이름입니다.")
        if not url.strip():
            raise StoreError("플레이리스트 주소를 입력하세요.")
        with self._lock:
            self.stations[slug] = {
                "name": name.strip() or slug,
                "url": url.strip(),
                "shuffle": bool(shuffle),
                "last_id": None,
            }
            self.save()
        return slug

    def update_station(self, slug: str, name=None, url=None, shuffle=None):
        """Returns True if the playlist URL changed."""
        with self._lock:
            if slug.startswith(PERSONAL_PREFIX):
                target = self.users[slug[len(PERSONAL_PREFIX):]]
                url_key = "share_url"
            else:
                target = self.stations[slug]
                url_key = "url"
                if name is not None:
                    target["name"] = name.strip() or slug
            changed = url is not None and url.strip() != target.get(url_key, "")
            if url is not None:
                if not url.strip() and not slug.startswith(PERSONAL_PREFIX):
                    raise StoreError("플레이리스트 주소를 입력하세요.")
                target[url_key] = url.strip()
            if shuffle is not None:
                target["shuffle"] = bool(shuffle)
            if changed:
                target["last_id"] = None
            self.save()
            return changed

    def delete_station(self, slug: str):
        with self._lock:
            del self.stations[slug]
            self.save()

    def set_last_id(self, slug: str, video_id: str):
        with self._lock:
            if slug.startswith(PERSONAL_PREFIX):
                target = self.users.get(slug[len(PERSONAL_PREFIX):])
            else:
                target = self.stations.get(slug)
            if target is not None:
                target["last_id"] = video_id
                self.save()

    def all_slugs(self):
        return list(self.stations) + [PERSONAL_PREFIX + u for u in self.users]
