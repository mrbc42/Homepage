"""Home Base: a small start page server.

Serves the page, stores settings and apps in SQLite, fetches weather and
headlines on the server, and checks whether each app is reachable.
Standard library only.
"""

import hmac
import math
import json
import mimetypes
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path(os.environ.get("HOMEPAGE_DATA", "/data"))
DB_PATH = DATA_DIR / "homepage.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("HOMEPAGE_PORT", "8080"))
ADMIN_PASSWORD = os.environ.get("HOMEPAGE_ADMIN_PASSWORD", "")
USER_AGENT = "HomeBase/1.0 (+https://github.com/mrbc42/homepage)"

COLOURS = ["tangerine", "berry", "teal", "lime", "sky", "sun", "violet", "coral"]
TEMP_UNITS = ["celsius", "fahrenheit"]
WIND_UNITS = ["kmh", "mph", "ms", "kn"]

DEFAULT_SETTINGS = {
    "title": "Home Base",
    "location": None,
    "temperature_unit": "celsius",
    "wind_unit": "kmh",
    "clock_24h": True,
    "search_url": "https://duckduckgo.com/?q=",
    "headlines": 8,
    "feed_title": "ABC News",
    "feed_url": "https://www.abc.net.au/news/feed/51120/rss.xml",
    "open_in_new_tab": False,
    "status_checks": True,
    "status_interval": 60,
    "show_tides": True,
}


class BadRequest(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


# ——— database ———

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS apps (
    id        INTEGER PRIMARY KEY,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    url       TEXT NOT NULL,
    colour    TEXT NOT NULL,
    icon      TEXT NOT NULL DEFAULT '',
    check_enabled INTEGER NOT NULL DEFAULT 1,
    check_url TEXT NOT NULL DEFAULT '',
    position  INTEGER NOT NULL
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def get_settings(conn):
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in out:
            out[row["key"]] = json.loads(row["value"])
    return out


def app_dict(row):
    return {
        "id": row["id"],
        "group_id": row["group_id"],
        "name": row["name"],
        "url": row["url"],
        "colour": row["colour"],
        "icon": row["icon"],
        "check": bool(row["check_enabled"]),
        "check_url": row["check_url"],
    }


def get_groups(conn):
    groups = [
        {"id": g["id"], "name": g["name"], "apps": []}
        for g in conn.execute("SELECT id, name FROM groups ORDER BY position, id")
    ]
    by_id = {g["id"]: g for g in groups}
    for row in conn.execute("SELECT * FROM apps ORDER BY position, id"):
        by_id[row["group_id"]]["apps"].append(app_dict(row))
    return groups


# ——— validation ———

def clean_url(value, field="Address", required=True):
    value = str(value or "").strip()
    if not value:
        if required:
            raise BadRequest(f"{field} is required.")
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        value = "http://" + value
    parts = urllib.parse.urlsplit(value)
    try:
        parts.port  # raises ValueError for things like "javascript:alert(1)"
    except ValueError:
        raise BadRequest(f"{field} has an invalid port number.")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise BadRequest(f"{field} must start with http:// or https://.")
    if len(value) > 500:
        raise BadRequest(f"{field} is too long.")
    return value


def clean_text(value, field, max_len, required=True):
    value = str(value or "").strip()
    if required and not value:
        raise BadRequest(f"{field} is required.")
    if len(value) > max_len:
        raise BadRequest(f"{field} must be {max_len} characters or fewer.")
    return value


def clean_icon(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return clean_url(value, "Icon")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", value.lower()):
        raise BadRequest("Icon must be an icon name like 'jellyfin' or a full image URL.")
    return value.lower()


def clean_settings(patch):
    if not isinstance(patch, dict):
        raise BadRequest("Settings must be an object.")
    out = {}
    for key, value in patch.items():
        if key == "title":
            out[key] = clean_text(value, "Title", 40)
        elif key == "location":
            if value is None:
                out[key] = None
                continue
            try:
                lat, lon = float(value["latitude"]), float(value["longitude"])
            except (TypeError, KeyError, ValueError):
                raise BadRequest("Location needs a latitude and longitude.")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise BadRequest("Latitude must be between -90 and 90, longitude between -180 and 180.")
            out[key] = {
                "name": clean_text(value.get("name"), "Location name", 80),
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
            }
        elif key == "temperature_unit":
            if value not in TEMP_UNITS:
                raise BadRequest("Temperature unit must be celsius or fahrenheit.")
            out[key] = value
        elif key == "wind_unit":
            if value not in WIND_UNITS:
                raise BadRequest("Wind unit must be kmh, mph, ms or kn.")
            out[key] = value
        elif key in ("clock_24h", "open_in_new_tab", "status_checks", "show_tides"):
            out[key] = bool(value)
        elif key == "search_url":
            out[key] = clean_url(value, "Search address")
        elif key == "feed_title":
            out[key] = clean_text(value, "Feed name", 40, required=False) or "News"
        elif key == "feed_url":
            out[key] = clean_url(value, "Feed address", required=False)
        elif key == "headlines":
            n = int(value)
            if not 0 <= n <= 20:
                raise BadRequest("Headlines must be between 0 and 20.")
            out[key] = n
        elif key == "status_interval":
            n = int(value)
            if not 15 <= n <= 3600:
                raise BadRequest("Status check interval must be between 15 and 3600 seconds.")
            out[key] = n
        else:
            raise BadRequest(f"Unknown setting: {key}")
    return out


def clean_app(body, partial=False):
    out = {}
    if not partial or "name" in body:
        out["name"] = clean_text(body.get("name"), "Name", 60)
    if not partial or "url" in body:
        out["url"] = clean_url(body.get("url"))
    if not partial or "colour" in body:
        colour = body.get("colour") or "sky"
        if colour not in COLOURS:
            raise BadRequest("Colour must be one of: " + ", ".join(COLOURS) + ".")
        out["colour"] = colour
    if not partial or "icon" in body:
        out["icon"] = clean_icon(body.get("icon"))
    if not partial or "check" in body:
        out["check_enabled"] = 1 if body.get("check", True) else 0
    if not partial or "check_url" in body:
        out["check_url"] = clean_url(body.get("check_url"), "Check address", required=False)
    return out


def resolve_group(conn, body):
    """Find the target group from group_id, or by name (creating it if new)."""
    if body.get("group_id") is not None:
        row = conn.execute("SELECT id FROM groups WHERE id = ?", (int(body["group_id"]),)).fetchone()
        if not row:
            raise BadRequest("That group doesn't exist.", HTTPStatus.NOT_FOUND)
        return row["id"]
    name = clean_text(body.get("group") or "Apps", "Group", 40)
    row = conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM groups").fetchone()[0]
    return conn.execute("INSERT INTO groups (name, position) VALUES (?, ?)", (name, pos)).lastrowid


# ——— outbound fetches ———

_external = urllib.request.build_opener()
# Status checks go straight to the LAN: no proxy, and self-signed certificates are fine.
_insecure = ssl.create_default_context()
_insecure.check_hostname = False
_insecure.verify_mode = ssl.CERT_NONE
_lan = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_insecure)
)


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with _external.open(req, timeout=timeout) as r:
        return json.loads(r.read())


class TTLCache:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key, ttl, loader):
        with self._lock:
            hit = self._data.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = loader()
        except Exception:
            if hit:  # serve stale data rather than nothing
                return hit[1]
            raise
        with self._lock:
            self._data[key] = (time.time(), value)
        return value


cache = TTLCache()


def weather_for(settings):
    loc = settings["location"]
    if not loc:
        return {"error": "no_location"}
    q = urllib.parse.urlencode({"latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto"})
    units = f"&temperature_unit={settings['temperature_unit']}&wind_speed_unit={settings['wind_unit']}"

    def load():
        forecast = fetch_json(
            f"https://api.open-meteo.com/v1/forecast?{q}{units}&forecast_days=6"
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
            "&hourly=temperature_2m,weather_code,precipitation_probability"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_probability_max"
        )
        try:
            air = fetch_json(f"https://air-quality-api.open-meteo.com/v1/air-quality?{q}&current=european_aqi")
        except Exception:
            air = None
        tides = None
        if settings["show_tides"]:
            try:
                tides = tides_for(loc, feet=settings["temperature_unit"] == "fahrenheit")
            except Exception as e:
                print(f"tides unavailable: {e}", flush=True)
        return {"location": loc, "weather": forecast, "air": air, "tides": tides, "fetched_at": int(time.time())}

    return cache.get(("weather", q, units, settings["show_tides"]), 600, load)


COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def distance_and_bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371 * 2 * math.asin(math.sqrt(a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return round(km), COMPASS[round(deg / 45) % 8]


def tide_events(times, heights):
    """Find highs and lows in an hourly series, refined with a parabola through each peak."""
    events = []
    for i in range(1, len(heights) - 1):
        a, b, c = heights[i - 1], heights[i], heights[i + 1]
        if None in (a, b, c):
            continue
        if b > a and b >= c:
            kind = "high"
        elif b < a and b <= c:
            kind = "low"
        else:
            continue
        curve = a - 2 * b + c
        offset = (a - c) / (2 * curve) if curve else 0.0
        offset = max(-0.5, min(0.5, offset))
        when = datetime.fromisoformat(times[i]) + timedelta(hours=offset)
        events.append({
            "type": kind,
            "time": when.strftime("%Y-%m-%dT%H:%M"),
            "height": round(b - (a - c) * offset / 4, 2),
        })
    return events


def tides_for(loc, feet=False):
    """Modelled sea level at the nearest sea grid cell (Open-Meteo Marine). Not for navigation."""
    q = urllib.parse.urlencode({
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto",
        "hourly": "sea_level_height_msl", "past_days": 1, "forecast_days": 3, "cell_selection": "sea",
        "length_unit": "imperial" if feet else "metric",
    })
    data = fetch_json(f"https://marine-api.open-meteo.com/v1/marine?{q}")
    times, heights = data["hourly"]["time"], data["hourly"]["sea_level_height_msl"]
    if sum(h is not None for h in heights) < 24:
        return None
    km, direction = distance_and_bearing(loc["latitude"], loc["longitude"], data["latitude"], data["longitude"])
    return {
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "distance_km": km,
        "direction": direction,
        "unit": "ft" if feet else "m",
        "utc_offset_seconds": data.get("utc_offset_seconds", 0),
        "series": [[t, h] for t, h in zip(times, heights) if h is not None],
        "events": tide_events(times, heights),
    }


def headlines(count):
    if count <= 0:
        return {"items": []}

    def load():
        ids = fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:20]
        with ThreadPoolExecutor(8) as pool:
            items = list(pool.map(
                lambda i: fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json"), ids
            ))
        return [
            {
                "id": it["id"],
                "title": it.get("title", ""),
                "url": it.get("url") or f"https://news.ycombinator.com/item?id={it['id']}",
                "score": it.get("score", 0),
                "time": it.get("time", 0),
            }
            for it in items if it
        ]

    return {"items": cache.get("headlines", 900, load)[:count]}


def _feed_time(text):
    if not text:
        return 0
    text = text.strip()
    try:
        return int(parsedate_to_datetime(text).timestamp())  # RSS: "Thu, 23 Oct 2026 04:10:00 +1000"
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # Atom: ISO 8601
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    except ValueError:
        return 0


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw):
    """Read RSS 2.0, RSS 1.0 or Atom into a list of {title, url, time}."""
    root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields = {_local(c.tag): c for c in el}
        title = (fields.get("title").text or "").strip() if "title" in fields else ""
        link = ""
        for c in el:
            if _local(c.tag) == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if not href and (c.text or "").strip():
                    link = c.text.strip()
                    break
        if not link and "guid" in fields and (fields["guid"].text or "").startswith("http"):
            link = fields["guid"].text.strip()
        when = next((fields[k].text for k in ("pubDate", "published", "updated", "date") if k in fields), "")
        if title and link.startswith(("http://", "https://")):
            items.append({"title": title, "url": link, "time": _feed_time(when)})
    return items


def feed(url, count):
    if not url or count <= 0:
        return {"items": []}

    def load():
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        })
        with _external.open(req, timeout=10) as r:
            raw = r.read(3_000_000)
        return parse_feed(raw)

    return {"items": cache.get(("feed", url), 900, load)[:count]}


def geocode(query):
    query = query.strip()
    if len(query) < 2:
        return {"results": []}
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": query, "count": 8, "language": "en", "format": "json"}
    )
    data = fetch_json(url)
    return {
        "results": [
            {
                "name": r["name"],
                "detail": ", ".join(x for x in (r.get("admin1"), r.get("country")) if x),
                "latitude": r["latitude"],
                "longitude": r["longitude"],
            }
            for r in data.get("results", [])
        ]
    }


# ——— status checks ———

status = {}
status_lock = threading.Lock()
status_wake = threading.Event()


def check_one(app):
    target = app["check_url"] or app["url"]
    started = time.monotonic()
    result = {"checked_at": int(time.time())}
    try:
        req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        with _lan.open(req, timeout=5) as r:
            result.update(state="up", code=r.status)
    except urllib.error.HTTPError as e:
        # The server answered, so it's running; a 5xx means it's unhealthy.
        result.update(state="error" if e.code >= 500 else "up", code=e.code)
    except Exception as e:
        reason = getattr(e, "reason", e)
        result.update(state="down", detail=str(reason)[:120])
    result["ms"] = round((time.monotonic() - started) * 1000)
    return app["id"], result


def status_loop():
    while True:
        interval = 60
        try:
            with db() as conn:
                settings = get_settings(conn)
                apps = [app_dict(r) for r in conn.execute("SELECT * FROM apps WHERE check_enabled = 1")]
            interval = settings["status_interval"]
            if settings["status_checks"] and apps:
                with ThreadPoolExecutor(min(16, len(apps))) as pool:
                    results = dict(pool.map(check_one, apps))
            else:
                results = {}
            with status_lock:
                status.clear()
                status.update(results)
        except Exception as e:  # keep the loop alive whatever happens
            print(f"status check failed: {e}", flush=True)
        status_wake.wait(interval)
        status_wake.clear()


# ——— HTTP ———

ROUTES = []


def route(method, pattern, auth=False):
    def wrap(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), auth, fn))
        return fn
    return wrap


@route("GET", "/api/state")
def api_state(req):
    with db() as conn:
        return {
            "settings": get_settings(conn),
            "groups": get_groups(conn),
            "auth_required": bool(ADMIN_PASSWORD),
            "colours": COLOURS,
        }


@route("POST", "/api/login")
def api_login(req):
    if ADMIN_PASSWORD and not hmac.compare_digest(str(req.body().get("password", "")), ADMIN_PASSWORD):
        raise BadRequest("That password isn't right.", HTTPStatus.UNAUTHORIZED)
    return {"ok": True}


@route("PUT", "/api/settings", auth=True)
def api_settings(req):
    patch = clean_settings(req.body())
    with db() as conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in patch.items()],
        )
        settings = get_settings(conn)
    status_wake.set()
    return {"settings": settings}


@route("POST", "/api/groups", auth=True)
def api_group_create(req):
    name = clean_text(req.body().get("name"), "Group name", 40)
    with db() as conn:
        if conn.execute("SELECT 1 FROM groups WHERE name = ?", (name,)).fetchone():
            raise BadRequest(f"There's already a group called {name}.", HTTPStatus.CONFLICT)
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM groups").fetchone()[0]
        gid = conn.execute("INSERT INTO groups (name, position) VALUES (?, ?)", (name, pos)).lastrowid
    return {"id": gid, "name": name}


@route("PATCH", r"/api/groups/(\d+)", auth=True)
def api_group_rename(req, gid):
    name = clean_text(req.body().get("name"), "Group name", 40)
    with db() as conn:
        clash = conn.execute("SELECT id FROM groups WHERE name = ? AND id != ?", (name, gid)).fetchone()
        if clash:
            raise BadRequest(f"There's already a group called {name}.", HTTPStatus.CONFLICT)
        if not conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, gid)).rowcount:
            raise BadRequest("That group doesn't exist.", HTTPStatus.NOT_FOUND)
    return {"id": int(gid), "name": name}


@route("DELETE", r"/api/groups/(\d+)", auth=True)
def api_group_delete(req, gid):
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM apps WHERE group_id = ?", (gid,)).fetchone()[0]
        if n:
            raise BadRequest("Move or delete the apps in this group first.", HTTPStatus.CONFLICT)
        conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
    return None


@route("POST", "/api/groups/order", auth=True)
def api_group_order(req):
    ids = [int(i) for i in req.body().get("ids", [])]
    with db() as conn:
        conn.executemany("UPDATE groups SET position = ? WHERE id = ?", list(enumerate(ids)))
    return None


@route("POST", "/api/apps", auth=True)
def api_app_create(req):
    body = req.body()
    fields = clean_app(body)
    with db() as conn:
        gid = resolve_group(conn, body)
        pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM apps WHERE group_id = ?", (gid,)
        ).fetchone()[0]
        aid = conn.execute(
            "INSERT INTO apps (group_id, name, url, colour, icon, check_enabled, check_url, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, fields["name"], fields["url"], fields["colour"], fields["icon"],
             fields["check_enabled"], fields["check_url"], pos),
        ).lastrowid
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (aid,)).fetchone()
    status_wake.set()
    return app_dict(row)


@route("PATCH", r"/api/apps/(\d+)", auth=True)
def api_app_update(req, aid):
    body = req.body()
    fields = clean_app(body, partial=True)
    with db() as conn:
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (aid,)).fetchone()
        if not row:
            raise BadRequest("That app doesn't exist.", HTTPStatus.NOT_FOUND)
        if "group_id" in body or "group" in body:
            gid = resolve_group(conn, body)
            if gid != row["group_id"]:
                fields["group_id"] = gid
                fields["position"] = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM apps WHERE group_id = ?", (gid,)
                ).fetchone()[0]
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE apps SET {sets} WHERE id = ?", (*fields.values(), aid))
        row = conn.execute("SELECT * FROM apps WHERE id = ?", (aid,)).fetchone()
    status_wake.set()
    return app_dict(row)


@route("DELETE", r"/api/apps/(\d+)", auth=True)
def api_app_delete(req, aid):
    with db() as conn:
        conn.execute("DELETE FROM apps WHERE id = ?", (aid,))
    with status_lock:
        status.pop(int(aid), None)
    return None


@route("POST", "/api/apps/order", auth=True)
def api_app_order(req):
    ids = [int(i) for i in req.body().get("ids", [])]
    with db() as conn:
        conn.executemany("UPDATE apps SET position = ? WHERE id = ?", list(enumerate(ids)))
    return None


@route("GET", "/api/status")
def api_status(req):
    with status_lock:
        return {str(k): v for k, v in status.items()}


@route("GET", "/api/weather")
def api_weather(req):
    with db() as conn:
        settings = get_settings(conn)
    try:
        return weather_for(settings)
    except Exception as e:
        raise BadRequest(f"Couldn't reach Open-Meteo: {e}", HTTPStatus.BAD_GATEWAY)


@route("GET", "/api/headlines")
def api_headlines(req):
    with db() as conn:
        count = get_settings(conn)["headlines"]
    try:
        return headlines(count)
    except Exception as e:
        raise BadRequest(f"Couldn't reach Hacker News: {e}", HTTPStatus.BAD_GATEWAY)


@route("GET", "/api/feed")
def api_feed(req):
    with db() as conn:
        s = get_settings(conn)
    try:
        return feed(s["feed_url"], s["headlines"])
    except ET.ParseError:
        raise BadRequest("That feed address didn't return an RSS or Atom feed.", HTTPStatus.BAD_GATEWAY)
    except Exception as e:
        raise BadRequest(f"Couldn't load the feed: {e}", HTTPStatus.BAD_GATEWAY)


@route("GET", "/api/geocode")
def api_geocode(req):
    try:
        return geocode(req.query.get("q", [""])[0])
    except Exception as e:
        raise BadRequest(f"Couldn't search for places: {e}", HTTPStatus.BAD_GATEWAY)


def export_data(conn):
    return {
        "version": 1,
        "settings": get_settings(conn),
        "groups": [
            {"name": g["name"], "apps": [
                {k: a[k] for k in ("name", "url", "colour", "icon", "check", "check_url")}
                for a in g["apps"]
            ]}
            for g in get_groups(conn)
        ],
    }


@route("GET", "/api/export", auth=True)
def api_export(req):
    with db() as conn:
        req.extra_headers["Content-Disposition"] = 'attachment; filename="homepage-backup.json"'
        return export_data(conn)


@route("POST", "/api/import", auth=True)
def api_import(req):
    data = req.body()
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise BadRequest("This doesn't look like a Home Base backup file.")
    settings = clean_settings({k: v for k, v in (data.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
    groups = []
    for g in data["groups"]:
        groups.append((clean_text(g.get("name"), "Group name", 40), [clean_app(a) for a in g.get("apps", [])]))
    with db() as conn:
        conn.execute("DELETE FROM apps")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM settings")
        conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)",
                         [(k, json.dumps(v)) for k, v in settings.items()])
        for gpos, (name, apps) in enumerate(groups):
            gid = conn.execute("INSERT INTO groups (name, position) VALUES (?, ?)", (name, gpos)).lastrowid
            conn.executemany(
                "INSERT INTO apps (group_id, name, url, colour, icon, check_enabled, check_url, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(gid, a["name"], a["url"], a["colour"], a["icon"], a["check_enabled"], a["check_url"], i)
                 for i, a in enumerate(apps)],
            )
    status_wake.set()
    return {"groups": len(groups), "apps": sum(len(a) for _, a in groups)}


@route("GET", "/healthz")
def api_health(req):
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeBase/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("HOMEPAGE_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def body(self):
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise BadRequest("Request is too large.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                self._body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                raise BadRequest("Request body must be JSON.")
        return self._body

    def authorised(self):
        if not ADMIN_PASSWORD:
            return True
        given = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return hmac.compare_digest(given.encode(), ADMIN_PASSWORD.encode())

    def send(self, code, payload=None, content_type="application/json", extra=None):
        data = b"" if payload is None else (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        self.send_response(code)
        if data:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if content_type == "application/json" else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def dispatch(self):
        self._body = None
        self.extra_headers = {}
        parts = urllib.parse.urlsplit(self.path)
        self.query = urllib.parse.parse_qs(parts.query)
        path = parts.path
        try:
            for method, pattern, auth, fn in ROUTES:
                m = pattern.match(path)
                if m and method == self.command:
                    if auth and not self.authorised():
                        raise BadRequest("Enter the admin password to change settings.", HTTPStatus.UNAUTHORIZED)
                    result = fn(self, *m.groups())
                    if result is None:
                        return self.send(HTTPStatus.NO_CONTENT)
                    return self.send(HTTPStatus.OK, result, extra=self.extra_headers)
            if path.startswith("/api/"):
                raise BadRequest("Not found.", HTTPStatus.NOT_FOUND)
            if self.command in ("GET", "HEAD"):
                return self.serve_static(path)
            raise BadRequest("Method not allowed.", HTTPStatus.METHOD_NOT_ALLOWED)
        except BadRequest as e:
            self.send(e.status, {"error": str(e)})
        except (ValueError, TypeError, KeyError) as e:
            self.send(HTTPStatus.BAD_REQUEST, {"error": f"Invalid request: {e}"})
        except Exception as e:
            print(f"error handling {self.command} {path}: {e!r}", flush=True)
            self.send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Something went wrong on the server."})

    def serve_static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            raise BadRequest("Not found.", HTTPStatus.NOT_FOUND)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send(HTTPStatus.OK, target.read_bytes(), content_type=ctype)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = dispatch


def main():
    init_db()
    threading.Thread(target=status_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Home Base listening on :{PORT}, data in {DB_PATH}"
          + (" (admin password set)" if ADMIN_PASSWORD else ""), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
