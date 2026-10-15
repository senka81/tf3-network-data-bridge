#!/usr/bin/env python3
"""Transport Fever 3 MCP server (read-only).

Asks the "Network Data Bridge (MCP)" game mod for live data and exposes it as MCP tools over stdio, so an AI
assistant can look at your lines, vehicles, towns and the vehicle models you can buy. Standard library only.

The game's Lua cannot open sockets or files directly, so the exchange goes through the game's userdata files, in a
folder the game owns (the mod picks it and writes mcp_bridge__hello_<stamp>.lua there):
  - this server rewrites mcp_bridge__request_<stamp>.lua (named after the newest hello file)
    { id = <number>, what = "snapshot" | "models" }
  - the mod (reading it twice a second) answers with mcp_bridge__<what>_<id>_p1..pN.lua (JSON in parts) and then
    the manifest mcp_bridge__<what>_<id>.lua  { seq = id, parts = N }
The game cannot overwrite or delete files, so this server removes older answers and hello files.
Set TF3_MCP_BRIDGE_DIR to the exchange folder to skip the search, TF3_MCP_TIMEOUT to change the 20 s wait.
"""
import glob
import hashlib
import tempfile
from pathlib import Path
import json
import os
import re
import sys
import time

SERVER_NAME = "tf3-mcp-bridge"
SERVER_VERSION = "2.0.0-dev.1"
APP_ID = "3493540"
EXCHANGE_DIRS = ("recordings", "keyframes", "mod_presets")
PREFIX = "mcp_bridge__"
REQUEST_TIMEOUT_SEC = float(os.environ.get("TF3_MCP_TIMEOUT", "20"))
SNAPSHOT_REUSE_SEC = 3  # several tool calls in one answer share one snapshot
MODELS_REUSE_SEC = 600
# newer request kinds: (seconds an answer is reused, first mod bridgeVersion that answers it)
KINDS = {"industries": (60, 5), "stations": (10, 5), "finances": (10, 5), "vehicles": (2, 5), "catchment": (30, 6), "cargo_access": (30, 76), "town_delivery": (30, 78)}

# ----------------------------------------------------------------------------------------------------------------
# locating the exchange folder


def steam_dirs():
    dirs = []
    if sys.platform == "win32":
        try:
            import winreg
            for root, key, value in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(root, key) as k:
                        dirs.append(winreg.QueryValueEx(k, value)[0])
                except OSError:
                    pass
        except ImportError:
            pass
        dirs.append(r"C:\Program Files (x86)\Steam")
    else:
        home = os.path.expanduser("~")
        if sys.platform == "darwin":
            dirs.append(os.path.join(home, "Library", "Application Support", "Steam"))
        dirs += [os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam")]
    seen, out = set(), []
    for d in dirs:
        n = os.path.normcase(os.path.normpath(d))
        if n not in seen and os.path.isdir(d):
            seen.add(n)
            out.append(d)
    return out


def userdata_dirs():
    out = []
    for steam in steam_dirs():
        out += glob.glob(os.path.join(steam, "userdata", "*", APP_ID, "local"))
    return out


def hello_files(folder):
    return sorted(glob.glob(os.path.join(folder, PREFIX + "hello*.lua")), key=os.path.getmtime, reverse=True)


def bridge_dir():
    """The exchange folder with the newest hello file (written by the mod each time a game loads)."""
    env = os.environ.get("TF3_MCP_BRIDGE_DIR")
    if env:
        return env
    found = []
    for local in userdata_dirs():
        for d in EXCHANGE_DIRS:
            folder = os.path.join(local, d)
            hellos = hello_files(folder)
            if hellos:
                found.append((os.path.getmtime(hellos[0]), folder))
    if not found:
        raise RuntimeError(
            "The game has not been connected yet: start Transport Fever 3, enable the mod 'Network Data Bridge (MCP)' "
            "for your save and load it. Searched: " + "; ".join(userdata_dirs() or ["(no Steam user data found)"]))
    found.sort(reverse=True)
    return found[0][1]


# ----------------------------------------------------------------------------------------------------------------
# reading the game's serialized Lua files ("function data() return { ... } end")


class LuaReader:
    SIMPLE_ESCAPES = {ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("v"): 11,
                      ord("\\"): 92, ord('"'): 34, ord("'"): 39, ord("\n"): 10, ord("\r"): 10}

    def __init__(self, data):
        self.s = data
        self.i = 0

    def error(self, msg):
        raise ValueError(f"{msg} at byte {self.i}")

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def skip(self):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c in b" \t\r\n":
                self.i += 1
            elif s.startswith(b"--", self.i):
                level = self.long_bracket_level(self.i + 2)
                if level is not None:
                    close = b"]" + b"=" * level + b"]"
                    end = s.find(close, self.i)
                    self.i = len(s) if end < 0 else end + len(close)
                else:
                    nl = s.find(b"\n", self.i)
                    self.i = len(s) if nl < 0 else nl + 1
            else:
                break

    def long_bracket_level(self, pos):
        s = self.s
        if s[pos:pos + 1] == b"[":
            j = pos + 1
            while s[j:j + 1] == b"=":
                j += 1
            if s[j:j + 1] == b"[":
                return j - pos - 1
        return None

    def long_string(self, level):
        start = self.i + level + 2
        close = b"]" + b"=" * level + b"]"
        end = self.s.find(close, start)
        if end < 0:
            self.error("unclosed long string")
        self.i = end + len(close)
        text = self.s[start:end]
        if text.startswith(b"\r\n"):
            text = text[2:]
        elif text.startswith(b"\n"):
            text = text[1:]
        return text

    def quoted(self):
        s, q = self.s, self.s[self.i]
        self.i += 1
        out = bytearray()
        while True:
            if self.i >= len(s):
                self.error("unclosed string")
            c = s[self.i]
            if c == q:
                self.i += 1
                return bytes(out)
            if c != 92:  # plain run up to the next backslash or quote
                stops = [x for x in (s.find(b"\\", self.i), s.find(bytes([q]), self.i)) if x >= 0]
                stop = min(stops) if stops else len(s)
                out += s[self.i:stop]
                self.i = stop
                continue
            e = s[self.i + 1]
            if e in self.SIMPLE_ESCAPES:
                out.append(self.SIMPLE_ESCAPES[e])
                self.i += 2
                if e == 13 and s[self.i:self.i + 1] == b"\n":
                    self.i += 1
            elif e == ord("z"):
                self.i += 2
                while self.i < len(s) and s[self.i] in b" \t\r\n":
                    self.i += 1
            elif e == ord("x"):
                out.append(int(s[self.i + 2:self.i + 4], 16))
                self.i += 4
            elif e == ord("u"):
                end = s.index(b"}", self.i)
                out += chr(int(s[self.i + 3:end], 16)).encode("utf-8")
                self.i = end + 1
            elif 48 <= e <= 57:
                j = self.i + 1
                while j < self.i + 4 and j < len(s) and 48 <= s[j] <= 57:
                    j += 1
                out.append(int(s[self.i + 1:j]))
                self.i = j
            else:
                self.error("bad escape")

    def name(self):
        s, j = self.s, self.i
        while j < len(s) and (chr(s[j]).isalnum() or s[j] == 95):
            j += 1
        word = s[self.i:j].decode("ascii")
        self.i = j
        return word

    def value(self):
        self.skip()
        c = self.peek()
        if not c:
            self.error("unexpected end")
        if c == b"{":
            return self.table()
        if c in (b'"', b"'"):
            return self.quoted()
        if c == b"[":
            level = self.long_bracket_level(self.i)
            if level is not None:
                return self.long_string(level)
        if c == b"-" and self.s.startswith(b"-math.huge", self.i):
            self.i += len(b"-math.huge")
            return float("-inf")
        if c in (b"-", b".") or c.isdigit():
            s, j = self.s, self.i + 1
            while j < len(s) and (chr(s[j]).isalnum() or s[j:j + 1] in (b".", b"+", b"-")):
                if s[j:j + 1] in (b"+", b"-") and s[j - 1:j] not in (b"e", b"E", b"p", b"P"):
                    break
                j += 1
            text = s[self.i:j].decode("ascii")
            self.i = j
            try:
                return int(text, 0)
            except ValueError:
                return float(text)
        if self.s.startswith(b"math.huge", self.i):
            self.i += len(b"math.huge")
            return float("inf")
        word = self.name()
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "nil":
            return None
        self.error(f"unexpected {word or c!r}")

    def table(self):
        self.i += 1
        items, keyed = [], {}
        while True:
            self.skip()
            c = self.peek()
            if not c:
                self.error("unclosed table")
            if c == b"}":
                self.i += 1
                break
            if c == b"[" and self.long_bracket_level(self.i) is None:
                self.i += 1
                key = self.value()
                self.skip()
                self.i += 1  # ]
                self.skip()
                self.i += 1  # =
                keyed[key.decode("utf-8") if isinstance(key, bytes) else key] = self.value()
            elif c.isalpha() or c == b"_":
                save = self.i
                word = self.name()
                self.skip()
                if self.peek() == b"=" and self.peek(2) != b"==":
                    self.i += 1
                    keyed[word] = self.value()
                else:
                    self.i = save
                    items.append(self.value())
            else:
                items.append(self.value())
            self.skip()
            if self.peek() in (b",", b";"):
                self.i += 1
        if keyed:
            for n, v in enumerate(items, 1):
                keyed[n] = v
            return keyed
        return items


def read_lua_data(path):
    with open(path, "rb") as f:
        data = f.read()
    at = data.find(b"return")
    if at < 0:
        raise ValueError(f"{path}: no 'return'")
    r = LuaReader(data)
    r.i = at + len(b"return")
    return r.value()


# ----------------------------------------------------------------------------------------------------------------
# requests


def answer_ids(folder, what):
    """Ids of the answers of one kind whose manifest exists, newest first."""
    pat = re.compile(re.escape(PREFIX + what) + r"_(\d+)\.lua$")
    found = []
    for name in os.listdir(folder):
        m = pat.match(name)
        if m:
            found.append((os.path.getmtime(os.path.join(folder, name)), int(m.group(1))))
    return [rid for _, rid in sorted(found, reverse=True)]


def read_answer(folder, what, rid):
    """(data, age seconds) of one complete answer, or None when it is absent or incomplete."""
    base = os.path.join(folder, f"{PREFIX}{what}_{rid}")
    try:
        manifest = read_lua_data(base + ".lua")
        if manifest.get("seq") != rid:
            return None
        chunks = []
        for i in range(1, int(manifest.get("parts")) + 1):
            part = read_lua_data(f"{base}_p{i}.lua")
            if part.get("seq") != rid:
                return None
            chunks.append(part.get("data") or b"")
        age = time.time() - os.path.getmtime(base + ".lua")
        return json.loads(b"".join(chunks).decode("utf-8")), age
    except Exception:  # incomplete, being written or malformed
        return None


def request_name(folder):
    """The request file of the current game load: the mod creates mcp_bridge__request_<stamp> with its hello file."""
    hellos = hello_files(folder)
    if not hellos:
        raise RuntimeError("No hello file from the mod in " + folder)
    stamp = os.path.basename(hellos[0])[len(PREFIX + "hello_"):-len(".lua")]
    return PREFIX + "request_" + stamp


def cleanup(folder, what, keep_rid):
    """Remove the other answers of this kind and all but the newest hello file (the game cannot delete files)."""
    pat = re.compile(re.escape(PREFIX + what) + r"_(\d+)(_p\d+)?\.lua$")
    for name in os.listdir(folder):
        m = pat.match(name)
        if m and int(m.group(1)) != keep_rid:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    for old in hello_files(folder)[1:]:
        try:
            os.remove(old)
        except OSError:
            pass
    current = request_name(folder) + ".lua"
    for old in glob.glob(os.path.join(folder, PREFIX + "request*.lua")):
        if os.path.basename(old) != current:
            try:
                os.remove(old)
            except OSError:
                pass


def write_request(folder, rid, what):
    path = os.path.join(folder, request_name(folder) + ".lua")
    tmp = os.path.join(folder, PREFIX + "request.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
        f.write('function data()\nreturn {\n\tid = %d,\n\twhat = "%s",\n}\nend\n' % (rid, what))
    for _ in range(20):  # the game may be reading the file at this instant
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def request(what, timeout=None):
    timeout = REQUEST_TIMEOUT_SEC if timeout is None else timeout
    folder = bridge_dir()
    rid = int(time.time() * 1000) % 2_000_000_000
    write_request(folder, rid, what)
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = read_answer(folder, what, rid)
        if got is not None:
            cleanup(folder, what, rid)
            return got[0]
        time.sleep(0.1)
    raise TimeoutError(what)


def last_answer(what):
    folder = bridge_dir()
    for rid in answer_ids(folder, what):
        got = read_answer(folder, what, rid)
        if got is not None:
            return got
    return None


_cache = {}


def fetch(what, reuse_sec):
    """Live data from the game; the last answer (with its age and a warning) when the game does not answer."""
    hit = _cache.get(what)
    if hit and time.time() - hit[1] < reuse_sec:
        return hit[0], time.time() - hit[1], None
    try:
        data = request(what)
        _cache[what] = (data, time.time())
        return data, 0.0, None
    except TimeoutError:
        old = last_answer(what)
        warning = (f"The game did not answer within {REQUEST_TIMEOUT_SEC:g} s (closed, paused in a menu or loading, or "
                   "the mod is not enabled for this save).")
        if old is None:
            raise RuntimeError(warning)
        return old[0], old[1], warning + f" Showing the last answer, {round(old[1])} s old."


def load_snapshot():
    snap, age, warning = fetch("snapshot", SNAPSHOT_REUSE_SEC)
    return snap, warning, age


def mod_version():
    """bridgeVersion from the newest hello file (written by the mod when a save loads)."""
    try:
        hellos = hello_files(bridge_dir())
        return int(read_lua_data(hellos[0]).get("bridgeVersion") or 0) if hellos else 0
    except Exception:
        return 0


def load_kind(what):
    reuse, needs = KINDS[what]
    have = mod_version()
    if have and have < needs:
        raise RuntimeError(f"This needs a newer game mod (bridge version {needs}, the game has {have}). Update "
                           "'Network Data Bridge (MCP)' from the Mod Hub, then restart the game and load the save.")
    data, age, warning = fetch(what, reuse)
    return data, warning, age


def load_models():
    models, age, warning = fetch("models", MODELS_REUSE_SEC)
    return models, warning


def freshness(warning, age):
    out = {"dataAgeSec": round(age)}
    if warning:
        out["warning"] = warning
    return out


# ----------------------------------------------------------------------------------------------------------------
# helpers


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def find_line(snap, ref):
    lines = snap.get("lines", [])
    ref_s = str(ref).strip()
    for line in lines:
        if str(line.get("id")) == ref_s:
            return line
    low = ref_s.lower()
    exact = [l for l in lines if (l.get("name") or "").lower() == low]
    if exact:
        return exact[0]
    partial = [l for l in lines if low in (l.get("name") or "").lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(f"{l.get('name')} (id {l.get('id')})" for l in partial[:15])
        raise ValueError(f"'{ref}' matches several lines: {names}")
    raise ValueError(f"No line with id or name '{ref}'")


def interval_sec(l):
    """Seconds between departures (the line window's interval); the game reports frequency = 1 / interval."""
    f = l.get("frequency")
    return round(1 / f) if isinstance(f, (int, float)) and f > 0 else None


def line_row(l):
    return {
        "id": l.get("id"),
        "name": l.get("name"),
        "carriers": l.get("carriers") or [],  # an empty Lua table arrives as {}
        "cargos": l.get("cargos") or None,
        "towns": l.get("towns") or None,
        "stops": len(l.get("stops") or []),
        "vehicles": l.get("vehicleCount"),
        "usagePct": l.get("usagePct"),
        "profit12m": l.get("profit12m"),
        "profitPerVehicle12m": l.get("profitPerVehicle12m"),
        "estIncome12m": l.get("estIncome12m"),
        "runningCostsPerYear": l.get("runningCostsPerYear"),
        "intervalSec": interval_sec(l),
        "problem": l.get("problem"),
    }


def vehicle_row(v):
    units = v.get("units") or []
    models = {}
    for u in units:
        key = u.get("name") or str(u.get("model"))
        models[key] = models.get(key, 0) + 1
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "models": models,
        "state": v.get("state"),
        "load": v.get("load"),
        "capacity": v.get("capacity"),
        "ageYears": round(v["ageDays"] / 365.0, 1) if v.get("ageDays") is not None else None,
        "lifespanYears": round(v["lifespanDays"] / 365.0, 1) if v.get("lifespanDays") else None,
        "ageOfLifespan": v.get("ageOfLifespan"),
        "worstMaintenance": v.get("worstMaintenance"),
        "runningCostPerYear": v.get("runningCostPerYear"),
        "value": v.get("value"),
        "profit12m": v.get("profit12m"),
        "userStopped": v.get("userStopped") or None,
        "noPath": v.get("noPath") or None,
        "sellOnArrival": v.get("sellOnArrival") or None,
    }


def compact(d):
    return {k: v for k, v in d.items() if v is not None}


# ----------------------------------------------------------------------------------------------------------------
# tools


def tool_status(args):
    snap, warning, age = load_snapshot()
    meta = snap.get("meta", {})
    cached_models = _cache.get("models")
    out = {
        "date": meta.get("date"),
        "money": meta.get("money"),
        "lines": meta.get("lineCount"),
        "vehicles": meta.get("vehicleCount"),
        "towns": meta.get("townCount"),
        "exchangeFolder": bridge_dir(),
        "vehicleModels": cached_models[0].get("meta", {}).get("modelCount") if cached_models else None,
        "performance": {
            "slowestFrameMs": meta.get("slowestFrameMs"),
            "slowestIdleCheckMs": meta.get("slowestIdleCheckMs"),
            "averageIdleCheckMs": meta.get("averageIdleCheckMs"),
            "slowestFramePhase": meta.get("slowestFramePhase"),
        },
        "collect": {"frames": meta.get("collectFrames"), "cpuMs": meta.get("collectCpuMs")},
        "lineLogNames": meta.get("lineLogNames"),
        "diagnostics": meta.get("diagnostics") or None,
        "bridgeVersion": meta.get("bridgeVersion"),
    }
    out.update(freshness(warning, age))
    return compact(out)


SORT_KEYS = {"profit12m", "profitPerVehicle12m", "usagePct", "vehicleCount", "estIncome12m", "runningCostsPerYear", "name"}


def tool_list_lines(args):
    snap, warning, age = load_snapshot()
    lines = snap.get("lines", [])
    carrier = (args.get("carrier") or "").upper()
    if carrier:
        lines = [l for l in lines if carrier in (l.get("carriers") or [])]
    query = (args.get("name_contains") or "").lower()
    if query:
        lines = [l for l in lines if query in (l.get("name") or "").lower()]
    town = (args.get("town") or "").lower()
    if town:
        lines = [l for l in lines if any(town in (t or "").lower() for t in (l.get("towns") or []))]
    key = args.get("sort_by") or "profit12m"
    if key not in SORT_KEYS:
        raise ValueError(f"sort_by must be one of {sorted(SORT_KEYS)}")
    desc = not args.get("ascending", key == "name")
    if key == "name":
        lines.sort(key=lambda l: (l.get("name") or "").lower(), reverse=desc)
    else:
        lines.sort(key=lambda l: (l.get(key) is None, -(l.get(key) or 0) if desc else (l.get(key) or 0)))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {
        "totalMatching": len(lines),
        "lines": [compact(line_row(l)) for l in lines[:limit]],
        "notes": "profit12m = income minus running costs over the last game year (purchases excluded). "
                 "usagePct = share of capacity used on recent trips. Use tf3_get_line for vehicles and stops.",
    }
    out.update(freshness(warning, age))
    return out


def tool_get_line(args):
    snap, warning, age = load_snapshot()
    line = find_line(snap, args.get("line", ""))
    out = {k: v for k, v in line.items() if k != "vehicles"}
    vehicles = [compact(vehicle_row(v)) for v in line.get("vehicles") or []]
    vehicles.sort(key=lambda v: -(v.get("ageOfLifespan") or 0))
    out["vehicles"] = vehicles
    by_model = {}
    for v in vehicles:
        for name, n in (v.get("models") or {}).items():
            by_model[name] = by_model.get(name, 0) + n
    out["unitsByModel"] = by_model
    out.update(freshness(warning, age))
    return compact(out)


def tool_list_vehicle_models(args):
    models, warning = load_models()
    items = models.get("models", [])
    if args.get("available_only", True):
        items = [m for m in items if m.get("available")]
    if not args.get("include_hidden", False):
        items = [m for m in items if m.get("visible", True)]
    carrier = (args.get("carrier") or "").upper()
    if carrier:
        items = [m for m in items if (m.get("carrier") or "").upper() == carrier]
    mode = (args.get("transport_mode") or "").upper()
    if mode:
        items = [m for m in items if mode in [x.upper() for x in (m.get("transportModes") or [])]]
    query = (args.get("name_contains") or "").lower()
    if query:
        items = [m for m in items if query in (m.get("name") or "").lower() or query in (m.get("file") or "").lower()]
    if args.get("min_capacity"):
        items = [m for m in items if (m.get("capacity") or 0) >= int(args["min_capacity"])]
    key = args.get("sort_by") or "capacity"
    items.sort(key=lambda m: (m.get(key) is None, -(m.get(key) or 0) if isinstance(m.get(key), (int, float)) else 0))
    limit = max(1, min(int(args.get("limit") or 60), 1000))
    fields = ("id", "name", "carrier", "transportModes", "capacity", "topSpeedKmh", "powerKw", "price",
              "lifespanDays", "yearFrom", "yearTo", "comfortFactor", "loadSpeed", "weightEmptyKg", "file")
    rows = [compact({f: m.get(f) for f in fields}) for m in items[:limit]]
    return {
        "gameDate": models.get("meta", {}).get("date"),
        "warning": warning,
        "totalMatching": len(items),
        "models": rows,
        "notes": "One row per vehicle unit (a locomotive and a wagon are separate units). price is the current "
                 "purchase price of one unit. capacity is the largest load config per compartment, summed.",
    }


TOWN_SORT_KEYS = {"population", "size", "lineUsage", "linesServing", "name"}


def tool_list_towns(args):
    snap, warning, age = load_snapshot()
    towns = snap.get("towns")
    if towns is None:
        raise RuntimeError("No towns in the data: the mod setting 'Town list' is off, or the mod is outdated.")
    serving = {}
    for l in snap.get("lines", []):
        for tid in {s.get("townId") for s in l.get("stops") or [] if s.get("townId") is not None}:
            serving.setdefault(tid, []).append(l)
    query = (args.get("name_contains") or "").lower()
    rows = []
    for t in towns:
        if query and query not in (t.get("name") or "").lower():
            continue
        lines = serving.get(t.get("id"), [])
        row = {
            "id": t.get("id"),
            "name": t.get("name"),
            "population": t.get("population"),
            "size": t.get("size"),
            "capacity": t.get("capacity"),
            "used": t.get("used"),
            "lineUsage": t.get("lineUsage"),
            "reachability": t.get("reachability"),
            "developmentActive": t.get("developmentActive"),
            "linesServing": len(lines),
            "position": t.get("position"),
        }
        if args.get("include_cargo", bool(query)):
            row["cargoNeeds"] = t.get("cargoNeeds")
            row["cargoSupply"] = t.get("cargoSupply")
        if args.get("include_lines", bool(query)):
            row["lines"] = [{"id": l.get("id"), "name": l.get("name"), "carriers": l.get("carriers"),
                             "vehicles": l.get("vehicleCount"), "usagePct": l.get("usagePct"),
                             "profit12m": l.get("profit12m")} for l in lines]
        rows.append(compact(row))
    key = args.get("sort_by") or "population"
    if key not in TOWN_SORT_KEYS:
        raise ValueError(f"sort_by must be one of {sorted(TOWN_SORT_KEYS)}")
    if key == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower(), reverse=bool(args.get("descending")))
    else:
        asc = bool(args.get("ascending"))
        rows.sort(key=lambda r: (r.get(key) is None, (r.get(key) or 0) if asc else -(r.get(key) or 0)))
    limit = max(1, min(int(args.get("limit") or 100), 1000))
    out = {
        "totalMatching": len(rows),
        "towns": rows[:limit],
        "notes": "population = residential capacity in use; size = residential + commercial + industrial capacity. "
                 "linesServing = your lines with a stop in the town. lineUsage is the game's town line-usage value. "
                 "reachability: destinations reachable by public transport vs. by car. position = estimated town "
                 "centre in world metres (x, y).",
    }
    out.update(freshness(warning, age))
    return out


def tool_route_ideas(args):
    """Town pairs for new lines: straight-line distance, populations, whether a line already links them."""
    import math
    snap, warning, age = load_snapshot()
    towns = [t for t in snap.get("towns") or [] if t.get("position")]
    if not towns:
        raise RuntimeError("No town positions in the data: the mod setting 'Town list' is off, or the mod is outdated.")
    min_pop = float(args.get("min_population") or 0)
    max_km = float(args.get("max_distance_km") or 0)
    min_km = float(args.get("min_distance_km") or 0)
    town_filter = (args.get("town") or "").lower()
    linked = {}  # (townA, townB) -> line names with stops in both
    for l in snap.get("lines", []):
        ids = sorted({st.get("townId") for st in l.get("stops") or [] if st.get("townId") is not None})
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                linked.setdefault((a, b), []).append(l.get("name"))
    towns = [t for t in towns if (t.get("population") or 0) >= min_pop]
    pairs = []
    for i, a in enumerate(towns):
        for b in towns[i + 1:]:
            if town_filter and town_filter not in (a.get("name") or "").lower() \
                    and town_filter not in (b.get("name") or "").lower():
                continue
            km = math.hypot(a["position"]["x"] - b["position"]["x"], a["position"]["y"] - b["position"]["y"]) / 1000
            if (max_km and km > max_km) or km < min_km:
                continue
            lines = linked.get(tuple(sorted((a.get("id"), b.get("id")))), [])
            pa, pb = a.get("population") or 0, b.get("population") or 0
            pairs.append(compact({
                "towns": [a.get("name"), b.get("name")],
                "populations": [pa, pb],
                "distanceKm": round(km, 1),
                "linkedBy": lines or None,
                # gravity-style demand estimate: bigger and closer towns score higher (relative, not a forecast)
                "potential": round(math.sqrt(pa * pb) / max(km, 1), 1),
            }))
    if not args.get("include_linked"):
        pairs = [p for p in pairs if "linkedBy" not in p]
    pairs.sort(key=lambda p: -p["potential"])
    limit = max(1, min(int(args.get("limit") or 25), 1000))
    out = {
        "totalMatching": len(pairs),
        "pairs": pairs[:limit],
        "notes": "distanceKm is straight-line between estimated town centres; real routes are longer and depend on "
                 "terrain. potential = sqrt(popA * popB) / distanceKm, a relative ranking only. linkedBy = your lines "
                 "with stops in both towns (hidden unless include_linked). Use tf3_list_vehicle_models for vehicles.",
    }
    out.update(freshness(warning, age))
    return out



def town_positions():
    """Town names and positions from the snapshot (for nearest-town lookups), or {} when unavailable."""
    try:
        snap, _, _ = load_snapshot()
    except Exception:
        return []
    return [(t.get("name"), t["position"]) for t in snap.get("towns") or [] if t.get("position")]


def nearest_town(pos, towns):
    import math
    if not pos or not towns:
        return None
    name, best = None, None
    for n, tp in towns:
        d = math.hypot(pos["x"] - tp["x"], pos["y"] - tp["y"])
        if best is None or d < best:
            name, best = n, d
    return {"name": name, "distanceKm": round(best / 1000, 1)}


INDUSTRY_STATUS = ("missing_inputs", "not_producing", "closing", "unshipped", "all")


def industry_flags(i):
    flags = []
    ins = i.get("inputs") or []
    if ins and all((x.get("deliveredPerYear") or 0) == 0 for x in ins):
        flags.append("missing_inputs")
    elif any((x.get("deliveredPerYear") or 0) == 0 for x in ins):
        flags.append("some_inputs_missing")
    if i.get("producing") is False:
        flags.append("not_producing")
    if i.get("closing"):
        flags.append("closing")
    outs = i.get("outputs") or []
    if any((o.get("producedPerYear") or 0) > 0 and (o.get("shippedPerYear") or 0) == 0 for o in outs):
        flags.append("unshipped")
    return flags


def tool_list_industries(args):
    data, warning, age = load_kind("industries")
    items = data.get("industries") or []
    cargo = (args.get("cargo") or "").upper()
    town = (args.get("town") or "").lower()
    status = args.get("status") or "all"
    if status not in INDUSTRY_STATUS:
        raise ValueError(f"status must be one of {list(INDUSTRY_STATUS)}")
    by_cargo = {}
    rows = []
    for i in items:
        for o in i.get("outputs") or []:
            c = by_cargo.setdefault(o["cargo"], {"producedPerYear": 0, "shippedPerYear": 0, "producers": 0,
                                                 "industryDemandPerYear": 0, "deliveredToIndustriesPerYear": 0,
                                                 "consumers": 0})
            c["producedPerYear"] += o.get("producedPerYear") or 0
            c["shippedPerYear"] += o.get("shippedPerYear") or 0
            c["producers"] += 1
        for x in i.get("inputs") or []:
            c = by_cargo.setdefault(x["cargo"], {"producedPerYear": 0, "shippedPerYear": 0, "producers": 0,
                                                 "industryDemandPerYear": 0, "deliveredToIndustriesPerYear": 0,
                                                 "consumers": 0})
            c["industryDemandPerYear"] += x.get("maxConsumptionPerYear") or 0
            c["deliveredToIndustriesPerYear"] += x.get("deliveredPerYear") or 0
            c["consumers"] += 1
        flags = industry_flags(i)
        if status != "all" and status not in flags:
            continue
        if town and town not in (i.get("town") or "").lower():
            continue
        if cargo and not any(cargo in (x.get("cargo") or "") for x in (i.get("inputs") or []) + (i.get("outputs") or [])):
            continue
        row = dict(i)
        row["flags"] = flags
        row["producedPerYear"] = sum(o.get("producedPerYear") or 0 for o in i.get("outputs") or [])
        row["shippedPerYear"] = sum(o.get("shippedPerYear") or 0 for o in i.get("outputs") or [])
        rows.append(compact(row))
    key = args.get("sort_by") or "producedPerYear"
    if key not in ("producedPerYear", "shippedPerYear", "level", "name"):
        raise ValueError("sort_by must be producedPerYear, shippedPerYear, level or name")
    if key == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower())
    else:
        rows.sort(key=lambda r: -(r.get(key) or 0))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {
        "totalMatching": len(rows),
        "industries": rows[:limit],
        "notes": "Per year figures are the game's own (last 12 months). flags: missing_inputs = no input delivered, "
                 "unshipped = produces but nothing is transported, closing = the game will close it. town = closest "
                 "town. position = world metres (x, y).",
    }
    if args.get("include_cargo_summary", True):
        out["byCargo"] = by_cargo
    out.update(freshness(warning, age))
    return out


def line_cargos():
    """Line id -> cargo names it carries (from the snapshot), or {} when unavailable."""
    try:
        snap, _, _ = load_snapshot()
    except Exception:
        return {}
    return {l.get("id"): set(l.get("cargos") or []) for l in snap.get("lines", [])}


def station_kind(st, cargos_by_line):
    """passenger / cargo / mixed from what the stopping lines carry; terminal flags when no line stops there
    (road and tram stops accept cargo by default, so their flags alone say "mixed")."""
    carried = set()
    for l in st.get("lines") or []:
        carried |= cargos_by_line.get(l.get("id"), set())
    if not carried:
        return st.get("kind")
    has_pax = "PASSENGERS" in carried
    if has_pax and len(carried) > 1:
        return "mixed"
    return "passenger" if has_pax else "cargo"


def tool_list_stations(args):
    data, warning, age = load_kind("stations")
    cargos_by_line = line_cargos()
    rows = []
    kind = (args.get("kind") or "").lower()
    town = (args.get("town") or "").lower()
    query = (args.get("name_contains") or "").lower()
    for st in data.get("stations") or []:
        st = dict(st, kind=station_kind(st, cargos_by_line))
        if kind and st.get("kind") != kind:
            continue
        if town and town not in (st.get("town") or "").lower():
            continue
        if query and query not in (st.get("name") or "").lower():
            continue
        row = dict(st)
        row["waitingTotal"] = sum((st.get("waiting") or {}).values())
        u = st.get("usage") or {}
        cap = (u.get("poolCapacity") or 0) + (u.get("terminalCapacity") or 0)
        row["usagePct"] = pct(u.get("used") or 0, cap)
        row["lines"] = [l.get("name") for l in st.get("lines") or []]
        rows.append(compact(row))
    key = args.get("sort_by") or "waitingTotal"
    if key not in ("waitingTotal", "usagePct", "terminals", "name"):
        raise ValueError("sort_by must be waitingTotal, usagePct, terminals or name")
    if key == "name":
        rows.sort(key=lambda r: (r.get("name") or "").lower())
    else:
        rows.sort(key=lambda r: -(r.get(key) or 0))
    limit = max(1, min(int(args.get("limit") or 50), 1000))
    out = {
        "totalMatching": len(rows),
        "stations": rows[:limit],
        "notes": "One row per station group (stations that share a name). waiting = passengers or cargo waiting now, "
                 "per cargo; longWaits = cargo the game marks as waiting very long. usage = people in the station vs "
                 "its waiting capacity (overflow = over capacity).",
    }
    out.update(freshness(warning, age))
    return out


def inspect_full_ids(part):
    """Validate exported complete IDs and report why exact cross-group overlap may be impossible."""
    if not isinstance(part, dict):
        return None, "missing_mode", None, 0
    count = part.get("count")
    full = part.get("fullEntityIds")
    if not isinstance(count, int) or count < 0:
        return None, "missing_or_invalid_count", count, 0
    # Lua serializers may omit empty arrays (nil / null) when a station has
    # no catchable entities. An explicitly complete zero-count export is an
    # exact empty set, not missing coverage. Require the export's confirmation
    # flag so malformed/older exports are not silently treated as complete.
    # Lua JSON encoders may represent an empty table as {}, [] or null.
    # Only accept this as a complete empty set with an explicit exactness flag.
    if count == 0 and part.get("exactIdsAvailable") is True and (full is None or full == [] or full == {}):
        return set(), None, 0, 0
    if not isinstance(full, list):
        return None, "full_ids_missing", count, 0
    ids = set(str(x) for x in full)
    if len(ids) != count:
        return None, "full_ids_length_mismatch", count, len(ids)
    return ids, None, count, len(ids)


def aggregate_station_groups(rows, include_sample_ids=False):
    """Group stations without mistaking capped ID samples for complete unions."""
    groups = {}
    for row in rows:
        gid = row.get("stationGroupId")
        key = str(gid) if gid is not None else "station:" + str(row.get("stationId"))
        groups.setdefault(key, []).append(row)
    result = []
    for key, members in groups.items():
        first = members[0]
        out = {
            "stationGroupId": first.get("stationGroupId"),
            "name": first.get("name"), "town": first.get("town"), "townId": first.get("townId"),
            "stationEntityCount": len(members),
            "stationIds": [m.get("stationId") for m in members],
            "positions": [m.get("position") for m in members if m.get("position") is not None],
        }
        for kind in ("passenger", "cargo"):
            parts = [m.get(kind) for m in members if isinstance(m.get(kind), dict)]
            counts = [p.get("count") for p in parts if isinstance(p.get("count"), int)]
            samples = [set(str(i) for i in p.get("sampleEntityIds", [])) for p in parts]
            sampled_union = set().union(*samples) if samples else set()
            overlap_in_samples = sum(map(len, samples)) - len(sampled_union)
            validated = [inspect_full_ids(m.get(kind)) for m in members]
            has_full = all(reason is None for _, reason, _, _ in validated)
            # With complete IDs we can compute an exact set union. The 64-ID samples
            # remain a backward-compatible fallback for older Lua collectors.
            if has_full:
                complete_union = set().union(*(ids for ids, _, _, _ in validated))
                lower = upper = unique = len(complete_union)
            elif counts and len(counts) == len(members):
                lower = max(max(counts), len(sampled_union))
                upper = sum(counts) - overlap_in_samples
                upper = max(lower, upper)
                unique = lower if lower == upper else None
            else:
                lower = upper = unique = None
            detail = {"memberCounts": counts, "uniqueEntityCount": unique,
                      "uniqueEntityLowerBound": lower, "uniqueEntityUpperBound": upper,
                      "sampledDistinctEntityIds": len(sampled_union),
                      "sampledDuplicateOccurrences": overlap_in_samples,
                      "fullyMeasured": unique is not None,
                      "completeIdSetsAvailable": has_full,
                      "incompleteMembers": len(counts) != len(members)}
            if include_sample_ids:
                detail["sampleEntityIds"] = sorted(sampled_union)[:64]
            out[kind] = detail
        result.append(out)
    return sorted(result, key=lambda r: ((r.get("town") or "").casefold(), (r.get("name") or "").casefold(), str(r.get("stationGroupId"))))


def tool_station_catchment(args):
    """Bounded station coverage samples from the game, grouped by station group."""
    data, warning, age = load_kind("catchment")
    rows = data.get("stationCatchments") or []
    query = str(args.get("name_contains") or "").casefold()
    town = str(args.get("town") or "").casefold()
    group_id = str(args.get("station_group_id") or "")
    rows = [r for r in rows if (not query or query in (r.get("name") or "").casefold())
            and (not town or town in (r.get("town") or "").casefold())
            and (not group_id or str(r.get("stationGroupId")) == group_id)]
    rows.sort(key=lambda r: (r.get("name") or "", str(r.get("stationId"))))
    mode = str(args.get("view") or "entities").lower()
    if mode not in ("entities", "groups"):
        raise ValueError("view must be entities or groups")
    limit = max(1, min(int(args.get("limit") or 25), 100))
    include_samples = bool(args.get("include_sample_ids", False))
    if mode == "groups":
        grouped = aggregate_station_groups(rows, include_samples)
        out = {"totalMatching": len(grouped), "stationGroups": grouped[:limit],
               "view": "groups",
               "notes": "Counts measure catchable game entities, NOT buildings, population, passengers or area. "
                        "Exact group union is calculated when every member provides complete IDs (max 1024 each); "
                        "otherwise conservative lower/upper bounds are used. These are same-group counts, "
                        "NOT deduplicated town-wide coverage. "
                        "Different groups and stations can overlap. Bounds may be broad."}
        out.update(freshness(warning, age))
        return out
    selected = []
    for row in rows[:limit]:
        r = dict(row)
        for k in ("passenger", "cargo"):
            if isinstance(r.get(k), dict):
                r[k] = {n: v for n, v in r[k].items() if n != "fullEntityIds" and (include_samples or n != "sampleEntityIds")}
        selected.append(r)
    out = {"totalMatching": len(rows), "stationCatchments": selected,
           "notes": "One row per STATION entity, not per station group. Counts are catchable entity counts, "
                    "NOT population, ridership, building counts, or catchment area. Shared station-group "
                    "catchments can overlap. sampleEntityIds are capped and cannot prove complete overlap."}
    out.update(freshness(warning, age))
    return out



def town_coverage_from_rows(rows, town):
    """Compute exclusive, shared and unique reachability from FULL station IDs.

    A town here describes associated STATIONS, not the town boundary. Exact
    statistics are withheld for a mode if any station lacks complete ID data.
    """
    selected = [r for r in rows if (r.get("town") or "").casefold() == town.casefold()]
    groups = {}
    for r in selected:
        gid = r.get("stationGroupId")
        key = str(gid) if gid is not None else "station:" + str(r.get("stationId"))
        groups.setdefault(key, []).append(r)
    summary = aggregate_station_groups(selected)
    group_details = {str(g.get("stationGroupId")) if g.get("stationGroupId") is not None else "station:" + str(g.get("stationIds", [None])[0]): g for g in summary}
    result = {"town": town, "totalStationGroups": len(groups), "totalStationEntities": len(selected)}
    for kind in ("passenger", "cargo"):
        group_ids = {}
        complete = True
        total_member_counts = 0
        max_member_count = 0
        sampled = set()
        diagnostics = []
        for key, members in groups.items():
            ids = set()
            group_complete = True
            for r in members:
                part = r.get(kind)
                validated, reason, count, exported = inspect_full_ids(part)
                if isinstance(count, int) and count >= 0:
                    total_member_counts += count
                    max_member_count = max(max_member_count, count)
                if isinstance(part, dict):
                    sampled.update(str(i) for i in part.get("sampleEntityIds", []) or [])
                if reason is None:
                    ids.update(validated)
                else:
                    group_complete = False
                    diagnostics.append({"stationId": r.get("stationId"),
                                        "stationGroupId": r.get("stationGroupId"),
                                        "name": r.get("name"), "reason": reason,
                                        "count": count, "exportedUniqueIds": exported,
                                        "exactIdsAvailableFlag": (part or {}).get("exactIdsAvailable") if isinstance(part, dict) else None})
            complete = complete and group_complete
            if group_complete:
                group_ids[key] = ids
        if complete:
            union = set().union(*group_ids.values()) if group_ids else set()
            exact = len(union)
            lower = upper = exact
        else:
            # Each observed sample is guaranteed, but unobserved overlap is unknown.
            # Sum of station-level counts is always an upper bound.
            exact = None
            lower = max(max_member_count, len(sampled))
            upper = max(lower, total_member_counts)
        result[kind] = {"uniqueEntityCount": exact,
                        "uniqueEntityLowerBound": lower,
                        "uniqueEntityUpperBound": upper,
                        "fullyMeasured": complete,
                        "sampledDistinctEntityIds": len(sampled),
                        "incompleteStationCount": len(diagnostics),
                        "incompleteStations": diagnostics}
        for key, members in groups.items():
            g = group_details[key]
            # If any town station is incomplete, a reliable exclusive count is impossible.
            if complete:
                others = set().union(*(v for k, v in group_ids.items() if k != key)) if len(group_ids) > 1 else set()
                here = group_ids[key]
                g[kind]["exclusiveToGroup"] = len(here - others)
                g[kind]["sharedWithOtherGroups"] = len(here & others)
                g[kind]["fractionUniqueToGroup"] = round(len(here - others) / len(here), 4) if here else None
            else:
                g[kind]["exclusiveToGroup"] = None
                g[kind]["sharedWithOtherGroups"] = None
                g[kind]["fractionUniqueToGroup"] = None
    result["stationGroups"] = summary
    result["notes"] = ("Town means stations associated with that town, not entities inside town boundaries. "
                       "These are catchable game entities, NOT population, ridership, buildings, land area, "
                       "service demand, or actual passenger journeys. 'exclusiveToGroup' means unreachable "
                       "by OTHER existing station groups associated with this town, not necessarily "
                       "unserved in the broader network. Overlap is based on entity IDs, per passenger/cargo "
                       "separately. If ANY station lacks complete IDs, exact town and exclusive values "
                       "are null instead of misleading estimates. Adding a station's exclusive count "
                       "is NOT a prediction of additional ridership.")
    return result


def tool_town_coverage(args):
    """Cross-station-group deduplication for stations associated with one town."""
    town_query = str(args.get("town") or "").strip()
    if not town_query:
        raise ValueError("town is required; provide one town name")
    data, warning, age = load_kind("catchment")
    all_rows = data.get("stationCatchments") or []
    town_matches = sorted({r.get("town") for r in all_rows if r.get("town") and town_query.casefold() in r["town"].casefold()})
    if not town_matches:
        raise ValueError("No station catchments found for town: " + town_query)
    exact_towns = [t for t in town_matches if t.casefold() == town_query.casefold()]
    if exact_towns:
        town = exact_towns[0]
    elif len(town_matches) == 1:
        town = town_matches[0]
    else:
        raise ValueError("Ambiguous town; choose an exact name from: " + ", ".join(town_matches[:20]))
    result = town_coverage_from_rows(all_rows, town)
    sort_by = str(args.get("sort_by") or "name")
    choices = {"name", "passenger_exclusive", "cargo_exclusive", "passenger_coverage", "cargo_coverage"}
    if sort_by not in choices:
        raise ValueError("sort_by must be one of: " + ", ".join(sorted(choices)))
    if sort_by != "name":
        kind, metric = sort_by.split("_", 1)
        field = "exclusiveToGroup" if metric == "exclusive" else "uniqueEntityCount"
        result["stationGroups"].sort(key=lambda g: (g[kind][field] is None, -(g[kind][field] or 0), (g.get("name") or "").casefold()))
    limit = max(1, min(int(args.get("limit") or 50), 250))
    result["returnedStationGroups"] = min(limit, len(result["stationGroups"]))
    result["stationGroups"] = result["stationGroups"][:limit]
    result.update(freshness(warning, age))
    return result




def tool_network_coverage_audit(args):
    """Compare coverage across all towns represented in the live catchment export.

    Counts are reachable entity IDs, not residents, demand or spatial coverage.
    Cross-town overlap is not removed; each town is evaluated independently.
    """
    data, warning, age = load_kind("catchment")
    rows = data.get("stationCatchments") or []
    names = sorted({r.get("town") for r in rows if isinstance(r.get("town"), str) and r.get("town")}, key=str.casefold)
    needle = str(args.get("town_contains") or "").casefold()
    if needle:
        names = [name for name in names if needle in name.casefold()]
    detail = []
    for name in names:
        result = town_coverage_from_rows(rows, name)
        item = {"town": name, "stationGroups": result["totalStationGroups"], "stationEntities": result["totalStationEntities"]}
        for kind in ("passenger", "cargo"):
            r = result[kind]
            groups = result["stationGroups"]
            item[kind] = {
                "uniqueEntityCount": r["uniqueEntityCount"],
                "uniqueEntityLowerBound": r["uniqueEntityLowerBound"],
                "uniqueEntityUpperBound": r["uniqueEntityUpperBound"],
                "fullyMeasured": r["fullyMeasured"],
                "incompleteStationCount": r["incompleteStationCount"],
                "groupsWithCoverage": sum((g[kind].get("uniqueEntityCount") or 0) > 0 for g in groups),
            }
            if r["fullyMeasured"]:
                exclusive_sum = sum(g[kind]["exclusiveToGroup"] for g in groups)
                count = r["uniqueEntityCount"]
                item[kind]["exclusiveAcrossGroups"] = exclusive_sum
                item[kind]["sharedAcrossGroups"] = count - exclusive_sum
                item[kind]["sharedFraction"] = round((count - exclusive_sum) / count, 4) if count else None
            else:
                item[kind]["exclusiveAcrossGroups"] = None
                item[kind]["sharedAcrossGroups"] = None
                item[kind]["sharedFraction"] = None
        detail.append(item)
    sort_by = str(args.get("sort_by") or "town")
    permitted = {"town", "station_groups", "passenger_coverage", "cargo_coverage", "passenger_shared_fraction", "cargo_shared_fraction"}
    if sort_by not in permitted:
        raise ValueError("sort_by must be one of: " + ", ".join(sorted(permitted)))
    if sort_by == "town":
        detail.sort(key=lambda x: x["town"].casefold())
    elif sort_by == "station_groups":
        detail.sort(key=lambda x: (-x["stationGroups"], x["town"].casefold()))
    else:
        kind, measure = sort_by.split("_", 1)
        field = "sharedFraction" if measure == "shared_fraction" else "uniqueEntityCount"
        detail.sort(key=lambda x: (x[kind][field] is None, -(x[kind][field] or 0), x["town"].casefold()))
    limit = max(1, min(int(args.get("limit") or 50), 200))
    response = {
        "totalTownsWithStations": len(names),
        "returnedTowns": min(limit, len(detail)),
        "totalStationEntitiesAnalyzed": sum(r["stationEntities"] for r in detail),
        "townsWithCompletePassengerData": sum(r["passenger"]["fullyMeasured"] for r in detail),
        "townsWithCompleteCargoData": sum(r["cargo"]["fullyMeasured"] for r in detail),
        "towns": detail[:limit],
        "notes": "Each town means stations associated with that town, not its boundaries or population. "
                 "Unique entities are deduplicated only WITHIN each town. Shared fraction measures entities "
                 "reachable from 2+ different station groups within that town. Town counts MUST NOT be summed "
                 "to claim network-wide unique coverage because nearby towns may overlap. "
                 "No demand, ridership, population, or profitability is inferred. Incomplete exports yield null exact metrics."
    }
    response.update(freshness(warning, age))
    return response




# v6.8 development: cross-reference station catchments, station queues and town/line snapshot.
def tool_network_opportunities(args):
    """Evidence-led investigation queue, not a forecast or automated building plan."""
    snap, snap_warning, snap_age = load_snapshot()
    catch, catch_warning, catch_age = load_kind("catchment")
    station_data, station_warning, station_age = load_kind("stations")
    catch_rows = catch.get("stationCatchments") or []
    towns = snap.get("towns") or []
    lines = snap.get("lines") or []
    station_rows = station_data.get("stations") or []
    town_filter = str(args.get("town_contains") or "").casefold()
    focus = str(args.get("focus") or "all")
    if focus not in ("all", "passenger", "cargo", "unserved"):
        raise ValueError("focus must be all, passenger, cargo, or unserved")
    min_waiting = max(0, int(args.get("min_waiting") or 25))
    limit = max(1, min(int(args.get("limit") or 50), 100))
    catch_names = {r.get("town") for r in catch_rows if isinstance(r.get("town"), str) and r.get("town")}
    names_by_id = {t.get("id"): t.get("name") for t in towns}
    town_by_name = {str(t.get("name") or "").casefold(): t for t in towns if t.get("name")}
    live_names = set(town_by_name)
    # Use town IDs rather than parsing line/station names to establish service.
    lines_by_town = {}
    for line in lines:
        t_ids = {stop.get("townId") for stop in (line.get("stops") or []) if isinstance(stop, dict)}
        for tid in t_ids:
            name = names_by_id.get(tid)
            if name:
                lines_by_town.setdefault(name.casefold(), []).append(line)
    stations_by_town = {}
    for st in station_rows:
        name = st.get("town")
        if isinstance(name, str) and name:
            stations_by_town.setdefault(name.casefold(), []).append(st)
    results = []
    for town in towns:
        name = str(town.get("name") or "")
        if not name or (town_filter and town_filter not in name.casefold()):
            continue
        key = name.casefold()
        town_lines = lines_by_town.get(key, [])
        stns = stations_by_town.get(key, [])
        have_catchment = name in catch_names
        cover = town_coverage_from_rows(catch_rows, name) if have_catchment else None
        counts = {}
        for kind in ("passenger", "cargo"):
            c = cover[kind] if cover else None
            counts[kind] = {
                "uniqueEntities": c.get("uniqueEntityCount") if c else None,
                "fullyMeasured": bool(c.get("fullyMeasured")) if c else False,
                "stationGroups": cover["totalStationGroups"] if cover else 0,
            }
            if cover and c["fullyMeasured"] and c["uniqueEntityCount"]:
                groups = cover["stationGroups"]
                exclusive = sum(g[kind]["exclusiveToGroup"] for g in groups)
                counts[kind]["sharedFraction"] = round((c["uniqueEntityCount"]-exclusive)/c["uniqueEntityCount"],4)
        waiting_by_type = {}
        for st in stns:
            for cargo, num in (st.get("waiting") or {}).items():
                if isinstance(num, (int,float)):
                    waiting_by_type[cargo] = waiting_by_type.get(cargo,0) + num
        pax_waiting = sum(n for k,n in waiting_by_type.items() if "PASSENGER" in str(k).upper())
        cargo_waiting = sum(n for k,n in waiting_by_type.items() if "PASSENGER" not in str(k).upper())
        indications = []
        def add(code, category, label, evidence):
            if focus in ("all", category):
                indications.append({"code":code,"category":category,"label":label,"evidence":evidence})
        if not have_catchment:
            add("no_station_catchment", "unserved", "No town-associated station catchment records", "This is a data/infrastructure gap, not proof of zero demand; neighboring-town stops may serve this town")
        elif counts["passenger"]["fullyMeasured"] and counts["passenger"]["uniqueEntities"] == 0:
            add("zero_passenger_catchment", "passenger", "Zero local passenger catchment", "Check neighboring-town stops and whether passenger infrastructure exists")
        if have_catchment and counts["passenger"].get("sharedFraction",0) is not None and (counts["passenger"].get("sharedFraction") or 0)>=0.6:
            add("passenger_overlap", "passenger", "High passenger catchment overlap", "At least 60% of reachable passenger entities are covered by 2+ local station groups; inspect transfers and stop spacing")
        if have_catchment and counts["cargo"].get("sharedFraction",0) is not None and (counts["cargo"].get("sharedFraction") or 0)>=0.75:
            add("cargo_overlap", "cargo", "High cargo catchment overlap", "At least 75% of reachable cargo entities are covered by 2+ local station groups; inspect actual freight usage")
        if pax_waiting >= min_waiting:
            add("passenger_waiting", "passenger", "Passenger queue worth investigating", "Town-associated station groups currently show %s waiting passengers; snapshot only" % pax_waiting)
        if cargo_waiting >= min_waiting:
            add("cargo_waiting", "cargo", "Cargo queue worth investigating", "Town-associated station groups currently show %s waiting cargo items; snapshot only" % cargo_waiting)
        if not town_lines and have_catchment:
            add("no_town_stop_lines", "unserved", "No lines with a stop tagged to this town", "Catchment exists, but no line stop has this town ID in the snapshot")
        # These are evidence flags, not weighted profitability/opportunity scores.
        if not indications and not args.get("include_unflagged",False):
            continue
        results.append({
            "town": name,
            "population": town.get("population"),
            "linesServing": len(town_lines),
            "stationGroups": cover["totalStationGroups"] if cover else 0,
            "coverageRecordPresent": have_catchment,
            "passenger": counts["passenger"],"cargo": counts["cargo"],
            "waiting": {"passengers":pax_waiting,"otherCargo":cargo_waiting},
            "indications":indications,
            "investigationCount": len(indications),
        })
    results.sort(key=lambda r:(-r["investigationCount"],-(r["waiting"]["passengers"]+r["waiting"]["otherCargo"]),r["town"].casefold()))
    out={"totalGameTowns":len(towns),"townsWithCatchmentRecords":len({str(n).casefold() for n in catch_names}),
         "matchingTowns":len(results),"returnedTowns":min(len(results),limit),"towns":results[:limit],
         "dataAgeSec":round(max(snap_age,catch_age,station_age),2),
         "warnings":[x for x in (snap_warning,catch_warning,station_warning) if x],
         "notes":"Investigation flags are deterministic thresholds, not demand, route feasibility, ridership or profit predictions. Zero/no local coverage does not exclude nearby-town stations. Waiting is a momentary station-group snapshot and may include multiple services. Town-level catchments do not deduplicate across towns. No construction recommendation is implied."}
    return out

DEPOT_WORDS = [("ship", "WATER"), ("harbo", "WATER"), ("port", "WATER"), ("tram", "TRAM"), ("train", "RAIL"),
               ("rail", "RAIL"), ("air", "AIR"), ("hangar", "AIR"), ("heli", "AIR"), ("road", "ROAD"),
               ("bus", "ROAD"), ("truck", "ROAD")]


def depot_carrier(d):
    """The game does not expose a depot's carrier to scripts in this build; fall back to its name."""
    if d.get("carrier"):
        return d["carrier"], None
    name = (d.get("name") or "").lower()
    if "maintenance" in name:
        return None, "maintenance building"
    for word, carrier in DEPOT_WORDS:
        if word in name:
            return carrier, None
    return None, None


def tool_list_depots(args):
    data, warning, age = load_kind("stations")
    towns = town_positions()
    carrier = (args.get("carrier") or "").upper()
    rows = []
    for d in data.get("depots") or []:
        row = dict(d)
        row["carrier"], kind = depot_carrier(d)
        if kind:
            row["kind"] = kind
        if carrier and row.get("carrier") != carrier:
            continue
        row["nearestTown"] = nearest_town(d.get("position"), towns)
        rows.append(compact(row))
    rows.sort(key=lambda r: ((r.get("carrier") or ""), (r.get("name") or "").lower()))
    out = {"totalMatching": len(rows), "depots": rows,
           "notes": "Your depots with carrier (from the depot's name when the game does not report it), position "
                    "(world metres), vehicles parked inside and the nearest town. Maintenance buildings are marked."}
    out.update(freshness(warning, age))
    return out


JOURNAL_CARRIERS = ["ROAD", "RAIL", "TRAM", "OTHER", "AIR", "WATER"]  # the finance journal's own order


def summarize_finance_table(t):
    """Per carrier and year: income, costs and net from the game's finance table (row codes are undocumented)."""
    years = t.get("header") or []
    out = {}
    for code, rows in (t.get("transportByCarrier") or {}).items():
        try:
            name = JOURNAL_CARRIERS[int(code)]
        except (ValueError, IndexError):
            name = str(code)
        income = [0] * len(years)
        costs = [0] * len(years)
        for values in (rows or {}).values():
            for i, v in enumerate((values or [])[:len(years)]):
                if v >= 0:
                    income[i] += v
                else:
                    costs[i] += v
        if any(income) or any(costs):
            out[name] = {"income": income, "costs": costs, "net": [a + b for a, b in zip(income, costs)]}
    investment = [0] * len(years)
    for values in (t.get("investment") or {}).values():
        for i, v in enumerate((values or [])[:len(years)]):
            investment[i] += v
    return {"years": years, "byCarrier": out, "construction": investment, "total": t.get("total"),
            "balance": t.get("balance"), "loan": t.get("loan"), "interest": t.get("interest")}


def tool_finances(args):
    data, warning, age = load_kind("finances")
    fin = (data.get("finances") or [{}])[0]
    out = dict(fin)
    if isinstance(fin.get("financeTable"), dict):
        out["financeTable"] = summarize_finance_table(fin["financeTable"])
    try:
        snap, _, _ = load_snapshot()
        per = {}
        for l in snap.get("lines", []):
            for c in l.get("carriers") or ["OTHER"]:
                r = per.setdefault(c, {"lines": 0, "vehicles": 0, "profit12m": 0, "runningCostsPerYear": 0})
                r["lines"] += 1
                r["vehicles"] += l.get("vehicleCount") or 0
                r["profit12m"] += l.get("profit12m") or 0
                r["runningCostsPerYear"] += l.get("runningCostsPerYear") or 0
        out["linesByCarrier"] = {k: {kk: round(vv) for kk, vv in v.items()} for k, v in per.items()}
    except Exception as e:
        out["linesByCarrier"] = f"unavailable: {e}"
    out["notes"] = ("balance and loan are now; earningsThisYear is since 1 January. linesByCarrier sums your lines' "
                    "12-month profit (income minus running costs) per carrier. financeTable is the game's finance "
                    "window per year (the current year so far): income, costs and net per carrier, construction "
                    "spending, total result and balance.")
    out.update(freshness(warning, age))
    return out


def headway_check(vehicles, day_ticks):
    """Spacing of a line's vehicles from their latest departures at the busiest stop: gaps in game days."""
    best = []
    n_stops = max((len(v.get("stopDepartures") or []) for v in vehicles), default=0)
    for k in range(n_stops):
        times = sorted(v["stopDepartures"][k] for v in vehicles
                       if len(v.get("stopDepartures") or []) > k and v["stopDepartures"][k] > 0)
        if len(times) > len(best):
            best = times
    if len(best) < 3:
        return None
    gaps = [b - a for a, b in zip(best, best[1:])]
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return None
    ratio = min(gaps) / mean
    out = {"vehiclesCompared": len(best), "evenness": round(ratio, 2), "bunched": ratio < 0.25}
    if day_ticks:
        out["gapsDays"] = [round(g / day_ticks, 1) for g in gaps]
    return out


def tool_live_vehicles(args):
    data, warning, age = load_kind("vehicles")
    meta = data.get("meta") or {}
    day = (meta.get("yearTicks") or 0) / 365 or None
    by_line = {}
    for v in data.get("vehicles") or []:
        by_line.setdefault(v.get("line"), []).append(v)
    ref = str(args.get("line") or "").strip().lower()
    if ref:
        match = [vs for lid, vs in by_line.items()
                 if str(lid) == ref or (vs[0].get("lineName") or "").lower() == ref]
        if not match:
            match = [vs for vs in by_line.values() if ref in (vs[0].get("lineName") or "").lower()]
        if not match:
            raise LookupError(f"No line matches {args.get('line')!r}")
        vs = match[0]
        out = {"line": vs[0].get("lineName"), "lineId": vs[0].get("line"), "spacing": headway_check(vs, day),
               "vehicles": [compact({k: v.get(k) for k in ("id", "name", "position", "speed", "state", "stopIndex",
                                                            "doorsOpen", "load", "noPath", "userStopped",
                                                            "timeUntilDeparture", "daysAtTerminal")}) for v in vs]}
        out.update(freshness(warning, age))
        return out
    lines, stuck = [], []
    for lid, vs in by_line.items():
        h = headway_check(vs, day)
        if h:
            lines.append(dict(h, line=vs[0].get("lineName"), lineId=lid, vehicles=len(vs)))
        for v in vs:
            why = []
            if v.get("noPath"):
                why.append("no path")
            if v.get("userStopped"):
                why.append("stopped by player")
            if (v.get("daysAtTerminal") or 0) >= 5:
                why.append(f"{v['daysAtTerminal']} days at a terminal")
            if why:
                stuck.append({"vehicle": v.get("name"), "id": v.get("id"), "line": v.get("lineName"),
                              "position": v.get("position"), "reasons": why})
    lines.sort(key=lambda r: r["evenness"])
    limit = max(1, min(int(args.get("limit") or 25), 1000))
    out = {
        "linesChecked": len(lines),
        "bunchedLines": sum(1 for r in lines if r["bunched"]),
        "lines": lines[:limit],
        "stuckVehicles": stuck[:limit],
        "notes": "evenness = smallest gap / average gap between the vehicles' latest departures at the busiest stop "
                 "(1 = perfectly even, below 0.25 = bunched). Needs 3+ vehicles. Pass line for one line's vehicles "
                 "with positions (world metres), speed, state and load.",
    }
    out.update(freshness(warning, age))
    return out


def tool_fleet_issues(args):
    snap, warning, age = load_snapshot()
    min_usage = float(args.get("low_usage_pct", 40))
    max_usage = float(args.get("high_usage_pct", 95))
    old_ratio = float(args.get("old_ratio", 1.0))
    lines_out, vehicles_out = [], []
    for l in snap.get("lines", []):
        reasons = []
        if l.get("problem") and l.get("problem") != "NOTHING":
            reasons.append(f"problem: {l['problem']}")
        if (l.get("vehicleCount") or 0) == 0:
            reasons.append("no vehicles")
        if l.get("profit12m") is not None and l["profit12m"] < 0:
            reasons.append(f"losing money: {l['profit12m']} over 12 months")
        if l.get("usagePct") is not None and l["usagePct"] < min_usage and (l.get("vehicleCount") or 0) > 1:
            reasons.append(f"low usage: {l['usagePct']}% with {l['vehicleCount']} vehicles")
        if l.get("usagePct") is not None and l["usagePct"] >= max_usage:
            reasons.append(f"full: {l['usagePct']}% usage, may need more capacity")
        if reasons:
            row = compact(line_row(l))
            row["reasons"] = reasons
            lines_out.append(row)
        for v in l.get("vehicles") or []:
            vr = []
            if v.get("ageOfLifespan") is not None and v["ageOfLifespan"] >= old_ratio:
                vr.append(f"at {int(v['ageOfLifespan'] * 100)}% of lifespan")
            if v.get("noPath"):
                vr.append("no path")
            if v.get("userStopped"):
                vr.append("stopped by player")
            if v.get("worstMaintenance") is not None and v["worstMaintenance"] < 0.3:
                vr.append(f"poor maintenance ({v['worstMaintenance']:.2f})")
            if vr:
                row = compact(vehicle_row(v))
                row["line"] = l.get("name")
                row["lineId"] = l.get("id")
                row["reasons"] = vr
                vehicles_out.append(row)
    limit = max(1, min(int(args.get("limit") or 100), 1000))
    out = {
        "lines": lines_out[:limit],
        "linesFlagged": len(lines_out),
        "vehicles": vehicles_out[:limit],
        "vehiclesFlagged": len(vehicles_out),
    }
    out.update(freshness(warning, age))
    return out



# v6.9 DEV: read-only cargo supply-chain candidates (not logistics feasibility or profit forecast).
def tool_cargo_supply_opportunities(args):
    import math
    snap, swarn, sage = load_snapshot()
    industries_data, iwarn, iage = load_kind("industries")
    towns = snap.get("towns")
    industries = industries_data.get("industries")
    lines = snap.get("lines")
    if not isinstance(towns, list) or not isinstance(industries, list) or not isinstance(lines, list):
        raise RuntimeError("Complete towns, industries and lines are required for cargo analysis")
    town_query = str(args.get("town") or "").casefold().strip()
    cargo_query = str(args.get("cargo") or "").upper().strip().replace(" ", "_")
    max_suppliers = max(1, min(int(args.get("suppliers_per_need") or 5), 20))
    max_needs = max(1, min(int(args.get("limit") or 50), 200))
    rows = []
    for t in towns:
        name = str(t.get("name") or "")
        if town_query and town_query not in name.casefold():
            continue
        needs = t.get("cargoNeeds")
        supply = t.get("cargoSupply")
        if not isinstance(needs, dict) or not isinstance(supply, dict):
            continue
        demanded = sorted(set(sum((v for v in needs.values() if isinstance(v, list)), [])))
        for cargo in demanded:
            if cargo_query and cargo != cargo_query: continue
            supplied = supply.get(cargo) or {}
            if not isinstance(supplied, dict): continue
            provided = supplied.get("supply")
            limit = supplied.get("limit")
            if not isinstance(provided, (int,float)) or not isinstance(limit,(int,float)): continue
            deficit = max(0.0, float(limit)-float(provided))
            dest_pos = t.get("position") or {}
            candidates = []
            for ind in industries:
                outputs = [o for o in ind.get("outputs") or [] if o.get("cargo") == cargo]
                if not outputs: continue
                o = outputs[0]
                ipos = ind.get("position") or {}
                dist = None
                if all(isinstance(p.get(k),(int,float)) for p in (dest_pos, ipos) for k in ("x","y")):
                    dist = round(math.hypot(ipos["x"]-dest_pos["x"], ipos["y"]-dest_pos["y"])/1000,1)
                produced = o.get("producedPerYear")
                shipped = o.get("shippedPerYear")
                unshipped = max(0, produced-shipped) if isinstance(produced,(int,float)) and isinstance(shipped,(int,float)) else None
                same_town = ind.get("townId") == t.get("id")
                # This detects a DIRECT line with both the producer's nearest town and the destination as stops.
                # It cannot prove industry-to-station catchment, right-way flow, or delivered cargo.
                direct = []
                for line in lines:
                    if cargo not in (line.get("cargos") or []): continue
                    stop_ids = {s.get("townId") for s in line.get("stops") or []}
                    if ind.get("townId") in stop_ids and t.get("id") in stop_ids:
                        direct.append({"id":line.get("id"),"name":line.get("name")})
                candidates.append({"industryId":ind.get("id"),"industry":ind.get("name"),"nearestTown":ind.get("town"),
                    "distanceKmStraightLine":dist,"sameTown":same_town,"producedPerYear":produced,
                    "shippedPerYear":shipped,"unshippedOutputIndicatorPerYear":unshipped,
                    "industryFlags":industry_flags(ind),"inputs": [{"cargo":x.get("cargo"),"deliveredPerYear":x.get("deliveredPerYear"),"maxConsumptionPerYear":x.get("maxConsumptionPerYear")} for x in ind.get("inputs") or []],
                    "directTownToTownLinesCarryingCargo":direct[:10]})
            candidates.sort(key=lambda c:(c["distanceKmStraightLine"] is None,c["distanceKmStraightLine"] if c["distanceKmStraightLine"] is not None else 1e12))
            rows.append({"town":name,"townId":t.get("id"),"districtsNeedingCargo":[k for k,v in needs.items() if cargo in (v or [])],
                "cargo":cargo,"currentSupply":round(provided,2),"supplyLimit":round(limit,2),"unmetSupplyToLimit":round(deficit,2),
                "currentSupplyFraction":round(provided/limit,4) if limit>0 else None,
                "industryProducerCount":len(candidates),"potentialSuppliers":candidates[:max_suppliers]})
    sort_by = str(args.get("sort_by") or "unmet_supply")
    if sort_by not in ("unmet_supply","town","supply_fraction"):
        raise ValueError("sort_by must be unmet_supply, town, or supply_fraction")
    if sort_by=="unmet_supply": rows.sort(key=lambda r:(-r["unmetSupplyToLimit"],r["town"],r["cargo"]))
    elif sort_by=="supply_fraction": rows.sort(key=lambda r: (r["currentSupplyFraction"] is None,r["currentSupplyFraction"] if r["currentSupplyFraction"] is not None else 1,r["town"]))
    else: rows.sort(key=lambda r:(r["town"],r["cargo"]))
    out={"totalGameTowns":len(towns),"totalIndustries":len(industries),"totalMatchingCargoNeeds":len(rows),
        "returnedCargoNeeds":len(rows[:max_needs]),"cargoNeeds":rows[:max_needs],
        "warnings":[w for w in (swarn,iwarn) if w],"dataAgeSec":round(max(sage,iage),1),
        "notes":"Town cargoSupply supply/limit are game-reported measures, not guaranteed annual shipment deficits. Industry produced/shipped figures are last-12-month values and unshipped differences are only an indicator, NOT confirmed free capacity. Nearest industry towns and straight-line distances do not establish route feasibility, catchment, production inputs or profit. A direct town-to-town cargo line is NOT proof it delivers this commodity to the town. Inspect industry inputs, line stops, station catchments, terrain and actual logistics before building."}
    return out


# v7.0 DEV: chain-level research shortlist. Zero historical production is NOT a ranking penalty.
def tool_cargo_chain_planner(args):
    import math
    snap, swarn, sage = load_snapshot()
    inddata, iwarn, iage = load_kind("industries")
    towns, industries, lines = snap.get("towns"), inddata.get("industries"), snap.get("lines")
    if not all(isinstance(x, list) for x in (towns, industries, lines)):
        raise RuntimeError("Complete town, industry and line data required")
    townquery = str(args.get("town") or "").strip().casefold()
    if not townquery:
        raise ValueError("town is required")
    matches=[t for t in towns if townquery in str(t.get("name") or "").casefold()]
    if len(matches)!=1:
        raise ValueError("town must match exactly one town; matches: "+", ".join(str(t.get("name")) for t in matches[:15]))
    town=matches[0]
    commodity=str(args.get("cargo") or "").strip().upper().replace(" ","_")
    max_final=max(1,min(20,int(args.get("suppliers_per_need") or 10)))
    max_inputs=max(1,min(12,int(args.get("input_suppliers_per_cargo") or 3)))
    def distance(a,b):
        a=a or {}; b=b or {}
        if all(isinstance(p.get(k),(int,float)) for p in (a,b) for k in ("x","y")):
            return round(math.hypot(a["x"]-b["x"],a["y"]-b["y"])/1000,2)
        return None
    def direct_line(town_a,town_b,cargo):
        result=[]
        if town_a is None or town_b is None: return result
        for line in lines:
            ids={s.get("townId") for s in line.get("stops") or [] if isinstance(s,dict)}
            if town_a in ids and town_b in ids and cargo in (line.get("cargos") or []):
                result.append({"lineId":line.get("id"),"lineName":line.get("name")})
        return result[:8]
    needs=town.get("cargoNeeds") or {}; supply=town.get("cargoSupply") or {}
    wanted=sorted({v for values in needs.values() if isinstance(values,list) for v in values})
    if commodity: wanted=[v for v in wanted if v==commodity]
    results=[]
    for cargo in wanted:
        s=supply.get(cargo) or {}
        suppliers=[i for i in industries if any(o.get("cargo")==cargo for o in i.get("outputs") or [])]
        candidates=[]
        for ind in suppliers:
            inputs=[]
            for inp in ind.get("inputs") or []:
                input_cargo=inp.get("cargo")
                producers=[]
                for src in industries:
                    if src.get("id")==ind.get("id") or not any(o.get("cargo")==input_cargo for o in src.get("outputs") or []):continue
                    producers.append({"industryId":src.get("id"),"industry":src.get("name"),"nearestTown":src.get("town"),
                         "distanceToFactoryKmStraightLine":distance(src.get("position"),ind.get("position")),
                         "candidateExistingLine":direct_line(src.get("townId"),ind.get("townId"),input_cargo),
                         "historicalProducedPerYear":next((o.get("producedPerYear") for o in src.get("outputs") or [] if o.get("cargo")==input_cargo),None),
                         "requiresInputs":[x.get("cargo") for x in src.get("inputs") or []]})
                producers.sort(key=lambda p:(p["distanceToFactoryKmStraightLine"] is None, p["distanceToFactoryKmStraightLine"] if p["distanceToFactoryKmStraightLine"] is not None else float('inf')))
                inputs.append({"cargo":input_cargo,"historicalDeliveredPerYear":inp.get("deliveredPerYear"),
                    "maxConsumptionPerYear":inp.get("maxConsumptionPerYear"),"availableProducerCount":len(producers),
                    "potentialInputSuppliers":producers[:max_inputs],"requiresFurtherChainInvestigation":any(p["requiresInputs"] for p in producers[:max_inputs])})
            d=distance(ind.get("position"),town.get("position"))
            input_dist=sum((x["potentialInputSuppliers"][0]["distanceToFactoryKmStraightLine"] or 0) for x in inputs if x["potentialInputSuppliers"])
            missing_sources=sum(1 for x in inputs if not x["potentialInputSuppliers"])
            direct=direct_line(ind.get("townId"),town.get("id"),cargo)
            # Heuristic sorting: straight-line final leg + shortest input legs (distance proxy ONLY), not real costs.
            # Never use production/shipment histories or operating state in the sort.
            candidates.append({"industryId":ind.get("id"),"industry":ind.get("name"),"nearestTown":ind.get("town"),
                "sameTown":ind.get("townId")==town.get("id"),"distanceToTownKmStraightLine":d,
                "directTownLineCandidates":direct,
                "inputs":inputs,"missingInputProducerTypes":missing_sources,
                "indicativeChainDistanceKm":round(d+input_dist,2) if d is not None and all(x["potentialInputSuppliers"] and x["potentialInputSuppliers"][0]["distanceToFactoryKmStraightLine"] is not None for x in inputs) else None,
                "historicalProducedPerYear":next((o.get("producedPerYear") for o in ind.get("outputs") or [] if o.get("cargo")==cargo),None),
                "historicalShippedPerYear":next((o.get("shippedPerYear") for o in ind.get("outputs") or [] if o.get("cargo")==cargo),None),
                "industryFlagsInformationalOnly":industry_flags(ind),
                "verificationNeeded":["industry-to-station catchment", "road/rail/terrain feasibility", "line direction and cargo compatibility", "construction and operating cost", "production capacity and chain continuity"]})
        candidates.sort(key=lambda c:(c["missingInputProducerTypes"],c["indicativeChainDistanceKm"] is None,c["indicativeChainDistanceKm"] if c["indicativeChainDistanceKm"] is not None else float('inf'),c["distanceToTownKmStraightLine"] if c["distanceToTownKmStraightLine"] is not None else float('inf')))
        for k,c in enumerate(candidates):
            c["researchOrder"]=k+1
        results.append({"town":town.get("name"),"townId":town.get("id"),"cargo":cargo,
            "currentSupply":s.get("supply"),"supplyLimit":s.get("limit"),
            "candidateCount":len(candidates),"candidates":candidates[:max_final]})
    out={"town":town.get("name"),"totalIndustries":len(industries),"cargoChains":results,"returnedCargoNeeds":len(results),
        "rankingMethod":"Research order by input producer availability and illustrative straight-line input plus final legs; historical production, shipments, and idle state have NO rank effect.",
        "notes":"Not a verified route, station catchment, available capacity, transport cost or profit forecast. Shortest input suppliers are independent choices and may not form a feasible chain. A zero production history does not imply unsuitable supplier. Distances are straight-line proxies and need physical topology and cost checks. Existing town-to-town line hints are not proof of freight delivery. Input chains are only one level deep. Supply/limit are game-reported and not guaranteed yearly quantities.",
        "warnings":[w for w in (swarn,iwarn) if w],"dataAgeSec":round(max(sage,iage),1)}
    return out


# v7.2 DEV: defensible freight infrastructure evidence. No physical geometry exported.
def tool_cargo_infrastructure_audit(args):
    import math
    snap,swarn,sage=load_snapshot()
    idata,iwarn,iage=load_kind("industries")
    sdata,stwarn,stage=load_kind("stations")
    cdata,cwarn,cage=load_kind("catchment")
    towns,industries,lines,stations=snap.get("towns"),idata.get("industries"),snap.get("lines"),sdata.get("stations")
    if not all(isinstance(x,list) for x in (towns,industries,lines,stations)):
        raise RuntimeError("Requires towns, industries, lines, and stations")
    q=str(args.get("town") or "").strip().casefold()
    matches=[t for t in towns if str(t.get("name") or "").casefold()==q]
    if len(matches)!=1: raise ValueError("town must exactly match a game town")
    town=matches[0]
    code_filter=str(args.get("cargo") or "").strip().upper()
    needs=sorted({str(c).upper() for v in (town.get("cargoNeeds") or {}).values() if isinstance(v,list) for c in v})
    if code_filter: needs=[c for c in needs if c==code_filter]
    supplier_limit=max(1,min(10,int(args.get("suppliers_per_need") or 2)))
    input_limit=max(1,min(8,int(args.get("input_suppliers_per_cargo") or 1)))
    station_limit=max(1,min(12,int(args.get("nearby_stations") or 3)))
    catchrows=cdata.get("stationCatchments") or []
    def distance(a,b):
        if not isinstance(a,dict) or not isinstance(b,dict):return None
        if any(not isinstance(p.get(k),(int,float)) for p in (a,b) for k in ('x','y')):return None
        return round(math.hypot(a['x']-b['x'],a['y']-b['y'])/1000,3)
    def group_kind(st):
        return str(st.get('kind') or '').lower()
    # Only explicitly cargo-capable station kinds. "mixed" is not adequate proof.
    def cargo_stations(pos):
        out=[]
        for st in stations:
            if group_kind(st) not in ('cargo','freight'):continue
            d=distance(pos,st.get('position'))
            if d is None:continue
            gid=st.get('id')
            serving=[l for l in lines if any(isinstance(stop,dict) and stop.get('stationGroup')==gid for stop in (l.get('stops') or []))]
            out.append({'stationGroupId':gid,'name':st.get('name'),'town':st.get('town'),'distanceKmStraightLine':d,
                        'stationKind':st.get('kind'),'servingFreightLines':[{'id':l.get('id'),'name':l.get('name'),'cargos':l.get('cargos') or [],'carriers':l.get('carriers') or [],'vehicles':l.get('vehicleCount')} for l in serving if any(c!='PASSENGERS' for c in l.get('cargos') or [])][:12]})
        return sorted(out,key=lambda x:x['distanceKmStraightLine'])[:station_limit]
    def catchment_industry_evidence(groups,industry_id):
        # An entity ID match is evidence only. Catchable entity namespaces and exports must be validated in game.
        if industry_id is None:return {'status':'not_applicable'}
        ret=[]
        for group in groups:
            gid=group['stationGroupId']
            members=[r for r in catchrows if r.get('stationGroupId')==gid]
            found=False; complete=True
            for r in members:
                cargo=r.get('cargo') or {}
                ids=cargo.get('fullEntityIds')
                if isinstance(ids,dict) and not ids:ids=[]
                if not isinstance(ids,list):
                    if cargo.get('count')==0 and cargo.get('exactIdsAvailable'):ids=[]
                    else:complete=False;continue
                if industry_id in ids or str(industry_id) in ids:found=True
                if cargo.get('exactIdsAvailable') is False:complete=False
            ret.append({'stationGroupId':gid,'cargoEntityIdPresent':found,'exportCompleteForGroup':complete and bool(members)})
        return {'status':'entity_id_check_only_not_confirmed_catchment','groups':ret}
    def route_evidence(orig,dest,cargo):
        starts={s['stationGroupId'] for s in orig}; ends={s['stationGroupId'] for s in dest}
        out=[]
        for line in lines:
            stated=line.get('cargos') or []
            if cargo not in stated:continue
            ordered=[s.get('stationGroup') for s in (line.get('stops') or []) if isinstance(s,dict) and s.get('stationGroup') is not None]
            if not ordered:continue
            # Prove distinct group occurrences in stop ordering, but NOT cargo loading/unloading.
            pairs=[]
            for i,a in enumerate(ordered):
                if a not in starts:continue
                for j,b in enumerate(ordered):
                    if b not in ends or a==b or i==j:continue
                    pairs.append({'originGroup':a,'destinationGroup':b,'originStopIndex':i,'destinationStopIndex':j,'forwardInExportedOrder':j>i})
            if pairs:
                out.append({'lineId':line.get('id'),'lineName':line.get('name'),'listedCargo':True,'carriers':line.get('carriers') or [],'vehicleCount':line.get('vehicleCount'),
                            'differentStationGroupPairs':pairs[:12], 'stopSequenceCyclicOrBidirectionalUnknown':True,'status':'distinct_stops_on_cargo_listed_line_unverified_delivery'})
        return out[:12]
    def make_leg(origin,output,cargo,end_name,end_pos,end_id=None):
        origin_name=origin.get('name');origin_pos=origin.get('position')
        a=cargo_stations(origin_pos);b=cargo_stations(end_pos)
        routes=route_evidence(a,b,cargo)
        return {'from':origin_name,'to':end_name,'cargo':cargo,'directKmStraightLine':distance(origin_pos,end_pos),
                'nearestExplicitCargoStationsAtOrigin':a,'nearestExplicitCargoStationsAtDestination':b,
                'originIndustryIdCatchmentEvidence':catchment_industry_evidence(a,origin.get('id')),
                'destinationIndustryIdCatchmentEvidence':catchment_industry_evidence(b,end_id),
                'distinctStopCargoLineEvidence':routes,'verificationStatus':'possible_existing_line_not_verified' if routes else 'no_matching_line_in_nearest_station_sample',
                'unverified':['industry-to-station catchment semantics','connected physical track or road geometry','actual loading/unloading','commodity carriage by vehicle','route direction and transfers','cost and profitability']}
    results=[]
    for cargo in needs:
        factories=[i for i in industries if any(o.get('cargo')==cargo for o in (i.get('outputs') or []))]
        factories.sort(key=lambda i: (distance(i.get('position'),town.get('position')) is None, distance(i.get('position'),town.get('position')) or 0))
        candidates=[]
        for factory in factories[:supplier_limit]:
            inputs=[]
            for inp in factory.get('inputs') or []:
                input_code=inp.get('cargo')
                providers=[i for i in industries if i.get('id')!=factory.get('id') and any(o.get('cargo')==input_code for o in (i.get('outputs') or []))]
                providers.sort(key=lambda i:(distance(i.get('position'),factory.get('position')) is None,distance(i.get('position'),factory.get('position')) or 0))
                inputs.append({'cargo':input_code,'possibleSources':[{'id':p.get('id'),'name':p.get('name'),'leg':make_leg(p,None,input_code,factory.get('name'),factory.get('position'),factory.get('id'))} for p in providers[:input_limit]],'availableProducers':len(providers)})
            candidates.append({'industryId':factory.get('id'),'industry':factory.get('name'),'historicalProducedPerYearInformationalOnly':next((o.get('producedPerYear') for o in factory.get('outputs') or [] if o.get('cargo')==cargo),None),
                               'inputs':inputs,'deliveryLeg':make_leg(factory,None,cargo,town.get('name'),town.get('position'))})
        results.append({'cargo':cargo,'supplierCount':len(factories),'candidates':candidates})
    return {'town':town.get('name'),'cargoAudits':results,'warnings':[x for x in (swarn,iwarn,stwarn,cwarn) if x], 'dataAgeSec':round(max(sage,iage,stage,cage),1),
            'notes':'Read-only v7.2. Only station kind cargo/freight is admitted. Distinct station groups and ordered stops are checked. Rail/road geometry, catchment semantics, direction, actual delivery and costs remain unverified. Missing nearest-station candidate is NOT proof no freight path exists. Idle industries are not penalized.'}



# v7.3 DEV: save-independent cargo investment scan. No town or industry hardcoding.
def tool_cargo_network_scan(args):
    import math
    snap,sw,sa=load_snapshot()
    inddata,iw,ia=load_kind('industries')
    stdata,stw,sta=load_kind('stations')
    towns,industries,lines,stations=(snap.get('towns'),inddata.get('industries'),snap.get('lines'),stdata.get('stations'))
    if any(not isinstance(v,list) for v in (towns,industries,lines,stations)):
        raise RuntimeError('Cargo scan requires game towns, industries, stations, lines')
    town_query=str(args.get('town_contains') or '').casefold().strip()
    cargo_query=str(args.get('cargo') or '').upper().strip().replace(' ','_')
    limit=max(1,min(100,int(args.get('limit') or 20)))
    max_factories=max(1,min(5,int(args.get('factories_per_need') or 3)))
    max_input_sources=max(1,min(5,int(args.get('input_sources_per_factory') or 2)))
    only_unmet=bool(args.get('only_unmet',True))
    def dist(a,b):
        if not isinstance(a,dict) or not isinstance(b,dict):return None
        if not all(isinstance(z.get(k),(int,float)) for z in (a,b) for k in ('x','y')):return None
        return round(math.hypot(a['x']-b['x'],a['y']-b['y'])/1000,3)
    # Key by commodity. Industry output history plays NO role in eligibility or sorting.
    output_sources={}
    for i in industries:
        for o in i.get('outputs') or []:
            if isinstance(o,dict) and o.get('cargo'):
                output_sources.setdefault(str(o['cargo']).upper(),[]).append(i)
    cargo_stations=[s for s in stations if str(s.get('kind') or '').lower() in ('cargo','freight') and s.get('id') is not None]
    # Real exported station-group stops on commodity-listed lines. No name guessing.
    cargo_edges={}
    for line in lines:
        stops=[s.get('stationGroup') for s in line.get('stops') or [] if isinstance(s,dict) and s.get('stationGroup') is not None]
        distinct=list(dict.fromkeys(stops))
        if len(distinct)<2:continue
        for cargo in line.get('cargos') or []:
            if cargo=='PASSENGERS':continue
            cg=cargo_edges.setdefault(str(cargo).upper(),{})
            for start in distinct:
                cg.setdefault(start,[]).append((line,tuple(x for x in distinct if x!=start)))
    # Nearest distance sampled only for ranking likely station access; all cargo stations participate in matching.
    def station_access(position):
        ds=[(dist(position,s.get('position')),s) for s in cargo_stations]
        ds=[(d,s) for d,s in ds if d is not None]
        ds.sort(key=lambda v:v[0])
        return ds
    def freight_evidence(startpos,endpos,cargo):
        a=station_access(startpos); b=station_access(endpos)
        # All stations checked, but do not assert these are actual industry catchments.
        dest={s.get('id'):(d,s) for d,s in b}
        found=[]
        for da,sa in a:
            for line,ends in cargo_edges.get(cargo,{}).get(sa.get('id'),[]):
                for eid in ends:
                    if eid not in dest:continue
                    db,sb=dest[eid]
                    found.append((round(da+db,3),{
                        'lineId':line.get('id'),'lineName':line.get('name'),
                        'originStationGroupId':sa.get('id'),'originStation':sa.get('name'),
                        'destinationStationGroupId':eid,'destinationStation':sb.get('name'),
                        'originAccessKmStraightLine':da,'destinationAccessKmStraightLine':db,
                        'carrier':line.get('carriers') or [],'vehicles':line.get('vehicleCount'),
                        'confidence':'shared_distinct_stops_and_listed_cargo_only'}))
        found.sort(key=lambda x:x[0])
        dedup=[]; seen=set()
        for _,row in found:
            key=(row['lineId'],row['originStationGroupId'],row['destinationStationGroupId'])
            if key not in seen:
                dedup.append(row);seen.add(key)
            if len(dedup)==2:break
        return {'directLineCandidates':dedup, 'candidateLineCountInAllStationGroups':len(found),
                'closestExplicitCargoStationAtOrigin':{'stationGroupId':a[0][1].get('id'),'name':a[0][1].get('name'),'distanceKm':a[0][0]} if a else None,
                'closestExplicitCargoStationAtDestination':{'stationGroupId':b[0][1].get('id'),'name':b[0][1].get('name'),'distanceKm':b[0][0]} if b else None}
    results=[]
    for t in towns:
        townname=str(t.get('name') or '')
        if town_query and town_query not in townname.casefold():continue
        needs=t.get('cargoNeeds') or {}; supply=t.get('cargoSupply') or {}
        demand={str(c).upper() for values in needs.values() if isinstance(values,list) for c in values}
        for cargo in sorted(demand):
            if cargo_query and cargo!=cargo_query:continue
            s=supply.get(cargo) or {}
            current=s.get('supply'); cap=s.get('limit')
            if not isinstance(current,(int,float)) or not isinstance(cap,(int,float)):continue
            shortfall=max(0.,float(cap)-float(current))
            if only_unmet and shortfall<=0.00001:continue
            choices=[]
            for factory in output_sources.get(cargo,[]):
                dp=dist(factory.get('position'),t.get('position'))
                inp=[]; input_dist=0.; lacking=0
                for item in factory.get('inputs') or []:
                    incargo=str(item.get('cargo') or '').upper()
                    sources=[]
                    for source in output_sources.get(incargo,[]):
                        if source.get('id')==factory.get('id'):continue
                        d=dist(source.get('position'),factory.get('position'))
                        sources.append((float('inf') if d is None else d,source,d))
                    sources.sort(key=lambda x:(x[0],str(x[1].get('name') or '')))
                    if sources and sources[0][2] is not None:input_dist+=sources[0][2]
                    if not sources:lacking+=1
                    picked=[]
                    for _,source,d in sources[:max_input_sources]:
                        ev=freight_evidence(source.get('position'),factory.get('position'),incargo)
                        picked.append({'industryId':source.get('id'),'industry':source.get('name'),
                            'distanceKmStraightLine':d,'directLineCandidates':ev['directLineCandidates'],
                            'candidateLineCountInAllStationGroups':ev['candidateLineCountInAllStationGroups'],
                            'furtherInputs':[x.get('cargo') for x in source.get('inputs') or []]})
                    inp.append({'cargo':incargo,'availableProducerCount':len(sources),'candidateInputProducers':picked})
                # Shortlist proximity only; NO output/activity ranking.
                choices.append((lacking, float('inf') if dp is None else dp+input_dist, str(factory.get('name') or ''), factory,dp,inp))
            choices.sort(key=lambda x:x[:3])
            ranked=[]
            for lack,indicative,_,factory,dp,inp in choices[:max_factories]:
                delivery=freight_evidence(factory.get('position'),t.get('position'),cargo)
                ranked.append({'industryId':factory.get('id'),'industry':factory.get('name'),
                    'factoryToTownKmStraightLine':dp,'indicativeInputPlusDeliveryKmStraightLine':round(indicative,3) if math.isfinite(indicative) else None,
                    'missingInputProducerTypes':lack,'inputs':inp,
                    'deliveryFreightLineCandidates':delivery['directLineCandidates'],
                    'deliveryCandidateLineCountInAllStationGroups':delivery['candidateLineCountInAllStationGroups'],
                    'closestCargoStationToFactory':delivery['closestExplicitCargoStationAtOrigin'],
                    'closestCargoStationToTownCenter':delivery['closestExplicitCargoStationAtDestination']})
            results.append({'town':townname,'townId':t.get('id'),'cargo':cargo,'districtTypes':[k for k,v in needs.items() if isinstance(v,list) and cargo in v],
                'gameSupply':round(current,2),'gameSupplyLimit':round(cap,2),'unmetSupplyToLimit':round(shortfall,2),
                'totalFactoriesProducingCargo':len(choices),'candidateChains':ranked})
    sort=str(args.get('sort_by') or 'unmet_supply')
    if sort not in ('unmet_supply','town'):raise ValueError('sort_by must be unmet_supply or town')
    if sort=='unmet_supply':results.sort(key=lambda x:(-x['unmetSupplyToLimit'],x['town'],x['cargo']))
    else:results.sort(key=lambda x:(x['town'],x['cargo']))
    return {'scope':'all_towns' if not town_query else 'town_name_filter','totalSaveTowns':len(towns),
        'totalSaveIndustries':len(industries),'totalExplicitCargoStationGroups':len(cargo_stations),
        'matchingNeeds':len(results),'returnedNeeds':min(limit,len(results)),'opportunities':results[:limit],
        'method':'Generic discovery based on game cargoNeeds and game supply. Ranks idle and active factories equally: missing input supplier types and straight-line input-plus-delivery distance only. Searches ALL explicit cargo station groups for direct cargo-listed line candidates; not limited to nearest stations.',
        'notVerified':['industry station catchment','track/road topology','vehicle actual cargo support and direction','station loading/unloading','multi-line transfers','construction and operating costs','profitability','annual demand equivalence of supply-limit'],
        'warnings':[x for x in (sw,iw,stw) if x],'dataAgeSec':round(max(sa,ia,sta),1)}


def tool_cargo_reachability(args):
    """Whole-save, commodity-aware transfer graph with strict endpoint access gates.

    Read-only. A path through served stops is evidence, not confirmation of industry loading.
    """
    import math
    from collections import defaultdict, deque
    snap,sw,sa=load_snapshot()
    inddata,iw,ia=load_kind('industries')
    stdata,stw,sta=load_kind('stations')
    towns,industries,lines,stations=(snap.get('towns'),inddata.get('industries'),snap.get('lines'),stdata.get('stations'))
    if any(not isinstance(x,list) for x in (towns,industries,lines,stations)):
        raise RuntimeError('Requires live towns, industries, lines and stations exports')
    def distance(a,b):
        if not isinstance(a,dict) or not isinstance(b,dict):return None
        if any(not isinstance(p.get(k),(int,float)) for p in (a,b) for k in ('x','y')):return None
        return round(math.hypot(a['x']-b['x'],a['y']-b['y'])/1000,3)
    radius=max(.05,min(10.,float(args.get('max_access_km',1.0))))
    max_hops=max(1,min(5,int(args.get('max_lines_per_leg',3))))
    limit=max(1,min(50,int(args.get('limit',10))))
    factory_limit=max(1,min(5,int(args.get('factories_per_need',2))))
    source_limit=max(1,min(4,int(args.get('input_sources_per_factory',2))))
    town_filter=str(args.get('town_contains') or '').casefold().strip()
    cargo_filter=str(args.get('cargo') or '').upper().strip().replace(' ','_')
    explicit={s['id']:s for s in stations if isinstance(s,dict) and s.get('id') is not None and str(s.get('kind') or '').lower() in ('cargo','freight')}
    output=defaultdict(list)
    for i in industries:
        for o in i.get('outputs') or []:
            if isinstance(o,dict) and o.get('cargo'):output[str(o['cargo']).upper()].append(i)
    # Edges only when same freight line lists the cargo and two *distinct* cargo stations appear in stops.
    # Consecutive stops are enough for graph; bidirectionality and cyclic order remain unverified.
    graph=defaultdict(lambda:defaultdict(list))
    for line in lines:
        stops=[stop.get('stationGroup') for stop in line.get('stops') or [] if isinstance(stop,dict)]
        stops=[sid for sid in stops if sid in explicit]
        for commodity in line.get('cargos') or []:
            commodity=str(commodity).upper()
            if commodity=='PASSENGERS':continue
            for a,b in zip(stops,stops[1:]):
                if a==b:continue
                label={'lineId':line.get('id'),'lineName':line.get('name'),'carrier':line.get('carriers') or [],'vehicles':line.get('vehicleCount')}
                graph[commodity][a].append((b,label))
                graph[commodity][b].append((a,label))
    def access(pos):
        result=[]
        for sid,s in explicit.items():
            d=distance(pos,s.get('position'))
            if d is not None and d<=radius:result.append((d,sid))
        return sorted(result)[:12]
    def path_for(src_pos,dst_pos,commodity):
        starts=access(src_pos);ends=access(dst_pos)
        if not starts or not ends:
            return {'status':'endpoint_access_unverified','originStationsWithinRadius':len(starts),'destinationStationsWithinRadius':len(ends)}
        dest={sid:d for d,sid in ends}
        best=None
        for da,start in starts:
            # line-aware state: transfers count when changing lines, not stations; bounded BFS.
            q=deque([(start,None,[],0)])
            visited={(start,None):0}
            while q:
                node,last,steps,changes=q.popleft()
                if node in dest and steps and changes+1<=max_hops:
                    penalty=da+dest[node]+changes*.25+len(steps)*.001
                    cand=(penalty,da,start,node,steps,changes)
                    if best is None or cand[0]<best[0]:best=cand
                    continue
                if len(steps)>=40:continue
                for next_node,edge in graph.get(commodity,{}).get(node,[]):
                    lineid=edge['lineId']
                    new_changes=changes+int(last is not None and last!=lineid)
                    if new_changes+1>max_hops:continue
                    key=(next_node,lineid)
                    if visited.get(key,99)<=new_changes:continue
                    visited[key]=new_changes
                    q.append((next_node,lineid,steps+[(node,next_node,edge)],new_changes))
        if best is None:
            return {'status':'no_commodity_path_among_accessible_stations','originStationsWithinRadius':len(starts),'destinationStationsWithinRadius':len(ends)}
        _,da,start,end,steps,changes=best
        legs=[]
        for a,b,line in steps:
            if legs and legs[-1]['lineId']==line['lineId']:
                legs[-1]['toStationGroupId']=b;legs[-1]['toStation']=explicit[b].get('name')
            else:
                legs.append({'lineId':line['lineId'],'lineName':line['lineName'],'fromStationGroupId':a,'fromStation':explicit[a].get('name'),'toStationGroupId':b,'toStation':explicit[b].get('name'),'carrier':line['carrier'],'vehicles':line['vehicles']})
        return {'status':'possible_existing_path_not_verified','originStationGroupId':start,'destinationStationGroupId':end,
                'originAccessKm':da,'destinationAccessKm':dest[end],'linesUsed':len(legs),'transfers':max(0,len(legs)-1),
                'lineSequence':legs,'assumptions':['bidirectional service not verified','cargo listed on line is not vehicle capability','transfers require actual compatible interchange','industry and district catchment unverified']}
    def chain(factory,town,commodity):
        delivery=path_for(factory.get('position'),town.get('position'),commodity)
        inputs=[]; missing=0; plausible=0
        for inp in factory.get('inputs') or []:
            ic=str(inp.get('cargo') or '').upper()
            sources=[]
            for src in output.get(ic,[]):
                if src.get('id')==factory.get('id'):continue
                d=distance(src.get('position'),factory.get('position'))
                sources.append((float('inf') if d is None else d,str(src.get('name') or ''),src,d))
            sources.sort(key=lambda x:x[:2])
            if not sources:missing+=1
            candidates=[]
            for _,__,src,d in sources[:source_limit]:
                evidence=path_for(src.get('position'),factory.get('position'),ic)
                plausible+=int(evidence['status']=='possible_existing_path_not_verified')
                candidates.append({'industryId':src.get('id'),'industry':src.get('name'),'straightLineKm':d,'connection':evidence,'furtherInputs':[x.get('cargo') for x in src.get('inputs') or []]})
            inputs.append({'cargo':ic,'availableProducerCount':len(sources),'sources':candidates})
        return {'industryId':factory.get('id'),'industry':factory.get('name'),'deliveryStraightLineKm':distance(factory.get('position'),town.get('position')),
                'delivery':delivery,'inputs':inputs,'missingInputProducerTypes':missing,
                'readiness':{'existingDeliveryLinePathHint':delivery['status']=='possible_existing_path_not_verified','inputLinePathHints':plausible,'requiredInputTypes':len(inputs)},
                'notVerified':['industry cargo catchment','town commercial/industrial delivery catchment','vehicle cargo capability','track/road physical feasibility','costs','capacity','actual directions']}
    allneeds=[]
    for town in towns:
        name=str(town.get('name') or '')
        if town_filter and town_filter not in name.casefold():continue
        needs=town.get('cargoNeeds') or {}; supply=town.get('cargoSupply') or {}
        cargo_set={str(c).upper() for values in needs.values() if isinstance(values,list) for c in values}
        for commodity in cargo_set:
            if cargo_filter and commodity!=cargo_filter:continue
            s=supply.get(commodity) or {}
            now,cap=s.get('supply'),s.get('limit')
            if not isinstance(now,(int,float)) or not isinstance(cap,(int,float)):continue
            gap=max(0,float(cap)-float(now))
            if gap<=0:continue
            # Idle production is NEVER penalized. Choose based on possible input availability and indicative distances.
            scored=[]
            for f in output.get(commodity,[]):
                missing=sum(not output.get(str(inp.get('cargo') or '').upper()) for inp in f.get('inputs') or [])
                d=distance(f.get('position'),town.get('position'))
                inputdistance=0
                for inp in f.get('inputs') or []:
                    ds=[distance(src.get('position'),f.get('position')) for src in output.get(str(inp.get('cargo') or '').upper(),[]) if src.get('id')!=f.get('id')]
                    inputdistance+=min((x for x in ds if x is not None),default=0)
                scored.append((missing,float('inf') if d is None else d+inputdistance,str(f.get('name') or ''),f))
            scored.sort(key=lambda row:row[:3])
            allneeds.append({'town':name,'townId':town.get('id'),'cargo':commodity,'unmetSupplyToLimit':round(gap,2),'totalCandidateFactories':len(scored),'_town':town,'_factories':[x[3] for x in scored[:factory_limit]]})
    allneeds.sort(key=lambda row:(-row['unmetSupplyToLimit'],row['town'],row['cargo']))
    # bounded expensive reachability only for returned opportunities
    out=[]
    for row in allneeds[:limit]:
        town=row.pop('_town');factories=row.pop('_factories')
        row['candidateChains']=[chain(f,town,row['cargo']) for f in factories]
        out.append(row)
    return {'scope':'whole_save' if not town_filter else 'filtered_towns','saveTowns':len(towns),'industries':len(industries),
            'explicitCargoStationGroups':len(explicit),'matchingUnmetNeeds':len(allneeds),'returnedNeeds':len(out),
            'endpointAccessRadiusKm':radius,'maxLinesPerLeg':max_hops,'opportunities':out,
            'interpretation':'Potential commodity-specific graph paths via existing line stops only. NOT verified deliveries or build/no-build decisions. Missing path may indicate absent catchment or sample restrictions, not necessarily missing track.',
            'ranking':'Unmet supply ranks town/cargo needs. Factory shortlist ranks input-type availability and indicative straight-line chain distance. Production history has zero rank weight.',
            'limitations':['Town centers are destination proxies, not district cargo targets','No actual industry loading confirmation','No station transfer compatibility','Cargo line lists do not verify vehicles or stop direction','No topology construction costs or profitability','Top factory shortlist may exclude viable infrastructure-favored candidates'],
            'warnings':[x for x in (sw,iw,stw) if x],'dataAgeSec':round(max(sa,ia,sta),1)}


# v7.5: read-only, no Lua upgrade.
def tool_cargo_reachability_v75(args):
    """Whole-save, commodity-aware transfer graph with strict endpoint access gates.

    Read-only. A path through served stops is evidence, not confirmation of industry loading.
    """
    import math
    from collections import defaultdict, deque
    snap,sw,sa=load_snapshot()
    inddata,iw,ia=load_kind('industries')
    stdata,stw,sta=load_kind('stations')
    catchdata,cw,ca=load_kind('catchment')
    catchrows=catchdata.get('stationCatchments') or []
    towns,industries,lines,stations=(snap.get('towns'),inddata.get('industries'),snap.get('lines'),stdata.get('stations'))
    if any(not isinstance(x,list) for x in (towns,industries,lines,stations)):
        raise RuntimeError('Requires live towns, industries, lines and stations exports')
    def distance(a,b):
        if not isinstance(a,dict) or not isinstance(b,dict):return None
        if any(not isinstance(p.get(k),(int,float)) for p in (a,b) for k in ('x','y')):return None
        return round(math.hypot(a['x']-b['x'],a['y']-b['y'])/1000,3)
    radius=max(.05,min(10.,float(args.get('max_access_km',1.0))))
    max_hops=max(1,min(5,int(args.get('max_lines_per_leg',3))))
    limit=max(1,min(50,int(args.get('limit',10))))
    factory_limit=max(1,min(5,int(args.get('factories_per_need',2))))
    source_limit=max(1,min(4,int(args.get('input_sources_per_factory',2))))
    town_filter=str(args.get('town_contains') or '').casefold().strip()
    cargo_filter=str(args.get('cargo') or '').upper().strip().replace(' ','_')
    explicit={s['id']:s for s in stations if isinstance(s,dict) and s.get('id') is not None and str(s.get('kind') or '').lower() in ('cargo','freight')}
    output=defaultdict(list)
    for i in industries:
        for o in i.get('outputs') or []:
            if isinstance(o,dict) and o.get('cargo'):output[str(o['cargo']).upper()].append(i)
    # Edges only when same freight line lists the cargo and two *distinct* cargo stations appear in stops.
    # Consecutive stops are enough for graph; bidirectionality and cyclic order remain unverified.
    graph=defaultdict(lambda:defaultdict(list))
    for line in lines:
        stops=[stop.get('stationGroup') for stop in line.get('stops') or [] if isinstance(stop,dict)]
        stops=[sid for sid in stops if sid in explicit]
        for commodity in line.get('cargos') or []:
            commodity=str(commodity).upper()
            if commodity=='PASSENGERS':continue
            for a,b in zip(stops,stops[1:]):
                if a==b:continue
                label={'lineId':line.get('id'),'lineName':line.get('name'),'carrier':line.get('carriers') or [],'vehicles':line.get('vehicleCount')}
                graph[commodity][a].append((b,label))
                graph[commodity][b].append((a,label))
    def access(pos):
        result=[]
        for sid,s in explicit.items():
            d=distance(pos,s.get('position'))
            if d is not None and d<=radius:result.append((d,sid))
        return sorted(result)[:12]
    catch_by_group=defaultdict(list)
    for record in catchrows:
        if isinstance(record,dict) and record.get('stationGroupId') is not None:
            catch_by_group[record['stationGroupId']].append(record)
    def entity_evidence(group_id, industry_id):
        if industry_id is None or group_id is None: return 'not_applicable'
        records=catch_by_group.get(group_id,[])
        if not records:return 'not_exported'
        complete=True;present=False
        for record in records:
            info=record.get('cargo') or {}
            ids=info.get('fullEntityIds')
            if isinstance(ids,dict) and not ids: ids=[]
            if not isinstance(ids,list):
                if info.get('count')==0 and info.get('exactIdsAvailable') is True: ids=[]
                else:complete=False;continue
            if any(str(x)==str(industry_id) for x in ids):present=True
            if info.get('exactIdsAvailable') is not True:complete=False
        if present:return 'industry_id_present_semantics_unverified'
        return 'industry_id_absent_complete_export' if complete else 'incomplete_export_unknown'
    def path_for(src_pos,dst_pos,commodity,src_industry_id=None,dst_industry_id=None):
        starts=access(src_pos);ends=access(dst_pos)
        if not starts or not ends:
            return {'status':'endpoint_access_unverified','originStationsWithinRadius':len(starts),'destinationStationsWithinRadius':len(ends)}
        dest={sid:d for d,sid in ends}
        shared=set(sid for _,sid in starts)&set(dest)
        # A single station serving both endpoints can indicate a possible local pickup/dropoff,
        # but is NOT evidence of freight transport between two stations.
        if shared:
            selected=min(shared,key=lambda sid:next(d for d,s in starts if s==sid)+dest[sid])
            return {'status':'shared_local_station_access_unverified','stationGroupId':selected,
                    'station':explicit[selected].get('name'),
                    'originAccessKm':next(d for d,s in starts if s==selected),
                    'destinationAccessKm':dest[selected],
                    'originIndustryIdEvidence':entity_evidence(selected,src_industry_id),
                    'destinationIndustryIdEvidence':entity_evidence(selected,dst_industry_id),
                    'caveat':'No transport leg asserted. Industry pickup/delivery needs game validation.'}
        best=None
        for da,start in starts:
            # line-aware state: transfers count when changing lines, not stations; bounded BFS.
            q=deque([(start,None,[],0)])
            visited={(start,None):0}
            while q:
                node,last,steps,changes=q.popleft()
                if node in dest and node!=start and steps and changes+1<=max_hops:
                    penalty=da+dest[node]+changes*.25+len(steps)*.001
                    cand=(penalty,da,start,node,steps,changes)
                    if best is None or cand[0]<best[0]:best=cand
                    continue
                if len(steps)>=40:continue
                for next_node,edge in graph.get(commodity,{}).get(node,[]):
                    lineid=edge['lineId']
                    new_changes=changes+int(last is not None and last!=lineid)
                    if new_changes+1>max_hops:continue
                    key=(next_node,lineid)
                    if visited.get(key,99)<=new_changes:continue
                    visited[key]=new_changes
                    q.append((next_node,lineid,steps+[(node,next_node,edge)],new_changes))
        if best is None:
            return {'status':'no_commodity_path_among_accessible_stations','originStationsWithinRadius':len(starts),'destinationStationsWithinRadius':len(ends)}
        _,da,start,end,steps,changes=best
        legs=[]
        for a,b,line in steps:
            if legs and legs[-1]['lineId']==line['lineId']:
                legs[-1]['toStationGroupId']=b;legs[-1]['toStation']=explicit[b].get('name')
            else:
                legs.append({'lineId':line['lineId'],'lineName':line['lineName'],'fromStationGroupId':a,'fromStation':explicit[a].get('name'),'toStationGroupId':b,'toStation':explicit[b].get('name'),'carrier':line['carrier'],'vehicles':line['vehicles']})
        return {'status':'possible_existing_path_not_verified','originStationGroupId':start,'destinationStationGroupId':end,
                'originAccessKm':da,'destinationAccessKm':dest[end],'linesUsed':len(legs),'transfers':max(0,len(legs)-1),
                'originIndustryIdEvidence':entity_evidence(start,src_industry_id),'destinationIndustryIdEvidence':entity_evidence(end,dst_industry_id),
                'lineSequence':legs,'assumptions':['bidirectional service not verified','cargo listed on line is not vehicle capability','transfers require actual compatible interchange','industry and district catchment unverified']}
    def chain(factory,town,commodity):
        delivery=path_for(factory.get('position'),town.get('position'),commodity,factory.get('id'),None)
        inputs=[]; missing=0; plausible=0
        for inp in factory.get('inputs') or []:
            ic=str(inp.get('cargo') or '').upper()
            sources=[]
            for src in output.get(ic,[]):
                if src.get('id')==factory.get('id'):continue
                d=distance(src.get('position'),factory.get('position'))
                sources.append((float('inf') if d is None else d,str(src.get('name') or ''),src,d))
            sources.sort(key=lambda x:x[:2])
            if not sources:missing+=1
            candidates=[]
            evaluated=[]
            for _,__,src,d in sources:
                evidence=path_for(src.get('position'),factory.get('position'),ic,src.get('id'),factory.get('id'))
                state=evidence['status']
                rank={'possible_existing_path_not_verified':0,'shared_local_station_access_unverified':1,'no_commodity_path_among_accessible_stations':2,'endpoint_access_unverified':3}.get(state,4)
                evaluated.append((rank,float('inf') if d is None else d,src,evidence,d))
            evaluated.sort(key=lambda item:(item[0],item[1],str(item[2].get('name') or '')))
            for _,__,src,evidence,d in evaluated[:source_limit]:
                plausible+=int(evidence['status'] in ('possible_existing_path_not_verified','shared_local_station_access_unverified'))
                candidates.append({'industryId':src.get('id'),'industry':src.get('name'),'straightLineKm':d,'connection':evidence,'furtherInputs':[x.get('cargo') for x in src.get('inputs') or []]})
            inputs.append({'cargo':ic,'availableProducerCount':len(sources),'sources':candidates})
        delivery_rank={'possible_existing_path_not_verified':0,'shared_local_station_access_unverified':1,'no_commodity_path_among_accessible_stations':2,'endpoint_access_unverified':3}.get(delivery['status'],4)
        input_states=[s['connection']['status'] for item in inputs for s in item['sources']]
        inputs_ready=sum(state in ('possible_existing_path_not_verified','shared_local_station_access_unverified') for state in input_states)
        total_legs=1+len(inputs)
        # Research preference only. Never a verified route, cost, or profit score.
        evidence_grade='stronger_line_evidence' if delivery_rank==0 and inputs_ready>=len(inputs) else 'partial_evidence' if delivery_rank<=1 or inputs_ready else 'no_path_evidence_yet'
        return {'readinessEvidenceGrade':evidence_grade,'readyInputTypesInShownSources':inputs_ready,'requiredLegs':total_legs,
                'industryId':factory.get('id'),'industry':factory.get('name'),'deliveryStraightLineKm':distance(factory.get('position'),town.get('position')),
                'delivery':delivery,'inputs':inputs,'missingInputProducerTypes':missing,
                'readiness':{'existingDeliveryLinePathHint':delivery['status']=='possible_existing_path_not_verified','inputLinePathHints':plausible,'requiredInputTypes':len(inputs)},
                'notVerified':['industry cargo catchment','town commercial/industrial delivery catchment (town-center proxy only)','vehicle cargo capability','track/road physical feasibility','costs','capacity','actual directions']}
    allneeds=[]
    for town in towns:
        name=str(town.get('name') or '')
        if town_filter and town_filter not in name.casefold():continue
        needs=town.get('cargoNeeds') or {}; supply=town.get('cargoSupply') or {}
        cargo_set={str(c).upper() for values in needs.values() if isinstance(values,list) for c in values}
        for commodity in cargo_set:
            if cargo_filter and commodity!=cargo_filter:continue
            s=supply.get(commodity) or {}
            now,cap=s.get('supply'),s.get('limit')
            if not isinstance(now,(int,float)) or not isinstance(cap,(int,float)):continue
            gap=max(0,float(cap)-float(now))
            if gap<=0:continue
            # Idle production is NEVER penalized. Choose based on possible input availability and indicative distances.
            scored=[]
            for f in output.get(commodity,[]):
                missing=sum(not output.get(str(inp.get('cargo') or '').upper()) for inp in f.get('inputs') or [])
                d=distance(f.get('position'),town.get('position'))
                inputdistance=0
                for inp in f.get('inputs') or []:
                    ds=[distance(src.get('position'),f.get('position')) for src in output.get(str(inp.get('cargo') or '').upper(),[]) if src.get('id')!=f.get('id')]
                    inputdistance+=min((x for x in ds if x is not None),default=0)
                scored.append((missing,float('inf') if d is None else d+inputdistance,str(f.get('name') or ''),f))
            scored.sort(key=lambda row:row[:3])
            allneeds.append({'town':name,'townId':town.get('id'),'cargo':commodity,'unmetSupplyToLimit':round(gap,2),'totalCandidateFactories':len(scored),'_town':town,'_factories':[x[3] for x in scored]})
    allneeds.sort(key=lambda row:(-row['unmetSupplyToLimit'],row['town'],row['cargo']))
    # bounded expensive reachability only for returned opportunities
    out=[]
    for row in allneeds[:limit]:
        town=row.pop('_town');factories=row.pop('_factories')
        # Evaluate the entire factory pool before shortlisting: avoid discarding a factory
        # with established infrastructure just because another is geometrically closer.
        reviewed=[chain(f,town,row['cargo']) for f in factories]
        grades={'stronger_line_evidence':0,'partial_evidence':1,'no_path_evidence_yet':2}
        reviewed.sort(key=lambda c:(grades.get(c['readinessEvidenceGrade'],3),
                                    -c['readyInputTypesInShownSources'],
                                    c['deliveryStraightLineKm'] is None,
                                    c['deliveryStraightLineKm'] if c['deliveryStraightLineKm'] is not None else float('inf'),
                                    str(c['industry'])))
        row['candidateChains']=reviewed[:factory_limit]
        out.append(row)
    return {'scope':'whole_save' if not town_filter else 'filtered_towns','saveTowns':len(towns),'industries':len(industries),
            'explicitCargoStationGroups':len(explicit),'matchingUnmetNeeds':len(allneeds),'returnedNeeds':len(out),
            'endpointAccessRadiusKm':radius,'maxLinesPerLeg':max_hops,'opportunities':out,
            'interpretation':'Potential commodity-specific graph paths via existing line stops only. NOT verified deliveries or build/no-build decisions. Missing path may indicate absent catchment or sample restrictions, not necessarily missing track.',
            'ranking':'Unmet supply ranks town/cargo needs. Factory shortlist evaluates every candidate factory for available endpoint line evidence and displayed input supplier evidence, then uses distance as tie-breaker. Idle or zero production never penalized.',
            'limitations':['Town centers are destination proxies, not district cargo targets; no reliable district positions exported','No actual industry loading confirmation','No station transfer compatibility','Cargo line lists do not verify vehicles or stop direction','No topology construction costs or profitability','Top factory shortlist may exclude viable infrastructure-favored candidates'],
            'warnings':[x for x in (sw,iw,stw,cw) if x],'dataAgeSec':round(max(sa,ia,sta,ca),1)}

TOOLS = [
    {
        "name":"tf3_cargo_reachability_v75",
        "description":"Whole-save freight route evidence audit v7.5. Distinguishes same-station local access from transport connections, attaches exported industry-ID evidence, and ranks factory shortlist using tentative network readiness. Generic, read-only; no construction inference.",
        "inputSchema":{"type":"object","properties":{
            "town_contains":{"type":"string"},"cargo":{"type":"string"},
            "limit":{"type":"integer","default":5},"factories_per_need":{"type":"integer","default":2},
            "input_sources_per_factory":{"type":"integer","default":2},
            "max_access_km":{"type":"number","default":1.0},"max_lines_per_leg":{"type":"integer","default":3}
        }},"handler":tool_cargo_reachability_v75
    },
    {
        "name":"tf3_cargo_reachability",
        "description":"Whole-save cargo opportunity shortlist with endpoint-distance-gated freight stop graph, possible multi-line transfer paths, input suppliers and explicit uncertainty. Read-only, generic for any save.",
        "inputSchema":{"type":"object","properties":{
            "town_contains":{"type":"string"},"cargo":{"type":"string"},
            "limit":{"type":"integer","default":10},"factories_per_need":{"type":"integer","default":2},
            "input_sources_per_factory":{"type":"integer","default":2},
            "max_access_km":{"type":"number","default":1.0},"max_lines_per_leg":{"type":"integer","default":3}
        }},"handler":tool_cargo_reachability
    },
    {
        "name": "tf3_status",
        "description": "Transport Fever 3: game date, money, line and vehicle counts, and how fresh the data is. "
                       "Call first to check the game is running with the MCP Bridge mod.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    {
        "name": "tf3_list_lines",
        "description": "Transport Fever 3: list the player's lines with vehicles, capacity usage, 12-month profit, "
                       "profit per vehicle and problems. Sortable and filterable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort_by": {"type": "string", "enum": sorted(SORT_KEYS), "description": "default profit12m"},
                "ascending": {"type": "boolean", "description": "default false (largest first), except for name"},
                "carrier": {"type": "string", "enum": ["ROAD", "RAIL", "TRAM", "AIR", "WATER"]},
                "name_contains": {"type": "string"},
                "town": {"type": "string", "description": "only lines with a stop in a town whose name contains this"},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
        "handler": tool_list_lines,
    },
    {
        "name": "tf3_list_towns",
        "description": "Transport Fever 3: towns and cities with name, population, size per district, public "
                       "transport reach and how many of your lines serve each. Filter by name to see those lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
                "sort_by": {"type": "string", "enum": sorted(TOWN_SORT_KEYS), "description": "default population"},
                "ascending": {"type": "boolean", "description": "default false (largest first)"},
                "include_lines": {"type": "boolean", "description": "list serving lines (default: only when filtering by name)"},
                "include_cargo": {"type": "boolean", "description": "goods each district needs and the supply vs limit (default: only when filtering by name)"},
                "limit": {"type": "integer", "description": "default 100"},
            },
        },
        "handler": tool_list_towns,
    },
    {
        "name": "tf3_route_ideas",
        "description": "Transport Fever 3: plan new lines. Town pairs ranked by a simple demand estimate (bigger and "
                       "closer towns first), with straight-line distance, populations and whether one of your lines "
                       "already links them. By default only pairs not yet linked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "town": {"type": "string", "description": "only pairs involving towns whose name contains this"},
                "min_population": {"type": "number", "description": "ignore smaller towns"},
                "min_distance_km": {"type": "number"},
                "max_distance_km": {"type": "number", "description": "e.g. 15 for buses, 150 for regional rail"},
                "include_linked": {"type": "boolean", "description": "also list pairs your lines already link"},
                "limit": {"type": "integer", "description": "default 25"},
            },
        },
        "handler": tool_route_ideas,
    },
    {
        "name": "tf3_get_line",
        "description": "Transport Fever 3: one line in detail: stops, capacity usage per cargo, profit, and every "
                       "vehicle with its model, age versus lifespan, load, running cost, value and profit.",
        "inputSchema": {
            "type": "object",
            "properties": {"line": {"type": "string", "description": "line id or (part of) its name"}},
            "required": ["line"],
        },
        "handler": tool_get_line,
    },
    {
        "name": "tf3_list_vehicle_models",
        "description": "Transport Fever 3: vehicle models, by default only those buyable in the current game year, "
                       "with price, capacity, top speed, power and lifespan. Use to pick replacements.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "carrier": {"type": "string", "enum": ["ROAD", "RAIL", "TRAM", "AIR", "WATER"]},
                "transport_mode": {"type": "string", "description": "e.g. BUS, TRUCK, TRAIN, ELECTRIC_TRAIN, TRAM"},
                "name_contains": {"type": "string"},
                "min_capacity": {"type": "integer"},
                "available_only": {"type": "boolean", "description": "default true"},
                "include_hidden": {"type": "boolean", "description": "default false"},
                "sort_by": {"type": "string", "enum": ["capacity", "price", "topSpeedKmh", "powerKw", "lifespanDays"]},
                "limit": {"type": "integer", "description": "default 60"},
            },
        },
        "handler": tool_list_vehicle_models,
    },
    {
        "name": "tf3_list_industries",
        "description": "Transport Fever 3: industries with location, closest town, level, inputs (delivered, "
                       "consumed, maximum per year) and outputs (produced, maximum, shipped per year), flags for "
                       "missing inputs, unshipped output and closing, plus a per-cargo supply and demand summary. "
                       "Use it to plan cargo chains.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cargo": {"type": "string", "description": "only industries using or making this cargo, e.g. COAL"},
                "town": {"type": "string", "description": "closest town name contains"},
                "status": {"type": "string", "enum": list(INDUSTRY_STATUS), "description": "default all"},
                "sort_by": {"type": "string", "enum": ["producedPerYear", "shippedPerYear", "level", "name"]},
                "include_cargo_summary": {"type": "boolean", "description": "default true"},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
        "handler": tool_list_industries,
    },
    {
        "name": "tf3_list_stations",
        "description": "Transport Fever 3: your stations with passengers and cargo waiting now, cargo waiting very "
                       "long, terminals, how full they are, their town and the lines stopping there.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["passenger", "cargo"]},
                "town": {"type": "string"},
                "name_contains": {"type": "string"},
                "sort_by": {"type": "string", "enum": ["waitingTotal", "usagePct", "terminals", "name"]},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
        "handler": tool_list_stations,
    },
    {
        "name": "tf3_station_catchment",
        "description": "Transport Fever 3: experimental read-only passenger/cargo catchable entity counts per "
                       "station entity or station-group summary with exact union when complete IDs are available, otherwise bounded estimates. Not population or ridership.",
        "inputSchema": {"type": "object", "properties": {
            "town": {"type": "string"},
            "name_contains": {"type": "string"},
            "station_group_id": {"type": "string"},
            "include_sample_ids": {"type": "boolean", "description": "default false"},
            "view": {"type": "string", "enum": ["entities", "groups"], "description": "default entities; groups combines station entities with conservative unique coverage bounds"},
            "limit": {"type": "integer", "description": "default 25, maximum 100"}
        }},
        "handler": tool_station_catchment,
    },
    {
        "name": "tf3_town_coverage",
        "description": "Experimental read-only TF3: exact union and cross-group passenger/cargo catchment overlap for all station groups associated with a named town, when full IDs are available. Includes each group's exclusive reachable entities and safe bounds for incomplete data. Not town population or ridership.",
        "inputSchema": {"type": "object", "properties": {
            "town": {"type": "string", "description": "Required town name; substring accepted only if unambiguous"},
            "sort_by": {"type": "string", "enum": ["name", "passenger_exclusive", "cargo_exclusive", "passenger_coverage", "cargo_coverage"], "description": "default name"},
            "limit": {"type": "integer", "description": "default 50; max 250 groups"}
        }, "required": ["town"]},
        "handler": tool_town_coverage,
    },
    {
        "name": "tf3_network_coverage_audit",
        "description": "Read-only comparison of station catchment coverage and overlap for all towns in the running TF3 save. Town-associated stations only, counts are reachable entities not population or demand. No cross-town deduplication.",
        "inputSchema": {"type": "object", "properties": {
            "town_contains": {"type": "string", "description": "Optional town name substring"},
            "sort_by": {"type": "string", "enum": ["town", "station_groups", "passenger_coverage", "cargo_coverage", "passenger_shared_fraction", "cargo_shared_fraction"], "description": "default town"},
            "limit": {"type": "integer", "description": "default 50, maximum 200 towns"}
        }},
        "handler": tool_network_coverage_audit,
    },
    {
        "name": "tf3_network_opportunities",
        "description": "Experimental read-only TF3 investigation queue across ALL game towns, combining station catchment, town/line service and live waiting. Flags missing local coverage, high overlap and queues; NOT demand or profit forecasts.",
        "inputSchema": {"type":"object","properties":{
            "town_contains":{"type":"string","description":"optional town substring"},
            "focus":{"type":"string","enum":["all","passenger","cargo","unserved"],"description":"default all"},
            "min_waiting":{"type":"integer","description":"queue flag threshold, default 25"},
            "include_unflagged":{"type":"boolean","description":"default false"},
            "limit":{"type":"integer","description":"default 50, maximum 100"}
        }},
        "handler":tool_network_opportunities,
    },
    {
        "name":"tf3_cargo_network_scan",
        "description":"v7.3 save-independent read-only scan of all game towns for unmet cargo supply and candidate complete first-level production chains. Checks all explicit freight station groups for distinct-stop cargo line hints. No actual delivery, road/rail topology or ROI verification. Idle factories are never penalized.",
        "inputSchema":{"type":"object","properties":{
            "town_contains":{"type":"string","description":"Optional town substring; omit to scan whole save"},
            "cargo":{"type":"string","description":"Optional cargo code, e.g. MEAT"},
            "only_unmet":{"type":"boolean","description":"Default true"},
            "limit":{"type":"integer","description":"Maximum cargo needs in output, default 20, max 100"},
            "factories_per_need":{"type":"integer","description":"Default 3, max 5"},
            "input_sources_per_factory":{"type":"integer","description":"Default 2, max 5"},
            "sort_by":{"type":"string","enum":["unmet_supply","town"]}
        }},
        "handler":tool_cargo_network_scan,
    },
    {
        "name":"tf3_cargo_infrastructure_audit",
        "description":"v7.2 read-only freight evidence: filter explicit cargo stations, require distinct station groups on cargo-listed lines and expose stop order plus tentative industry-ID catchment matches. No physical geometry or verified delivery.",
        "inputSchema":{"type":"object","properties":{
            "town":{"type":"string","description":"Exact destination town e.g. Timmins"},
            "cargo":{"type":"string","description":"Optional commodity e.g. MEAT"},
            "suppliers_per_need":{"type":"integer","description":"Default 2; max 10"},
            "input_suppliers_per_cargo":{"type":"integer","description":"Default 1; max 8"},
            "nearby_stations":{"type":"integer","description":"Default 3; max 10"}
        },"required":["town"]},
        "handler":tool_cargo_infrastructure_audit,
    },
    {
        "name":"tf3_cargo_chain_planner",
        "description":"Read-only cargo chain investigation for a destination town. Ranks candidate factories by availability of input producers and illustrative total straight-line input/final-leg distances; ZERO production is not a penalty. Not build feasibility or ROI.",
        "inputSchema":{"type":"object","properties":{
            "town":{"type":"string","description":"Required destination town, e.g. Timmins"},
            "cargo":{"type":"string","description":"Optional cargo code, e.g. MEAT"},
            "suppliers_per_need":{"type":"integer","description":"Default 10, max 20"},
            "input_suppliers_per_cargo":{"type":"integer","description":"Default 3, max 12"}
        },"required":["town"]},
        "handler":tool_cargo_chain_planner,
    },
    {
        "name": "tf3_cargo_supply_opportunities",
        "description": "Read-only commodity supply gaps for TF3 towns and possible producing industries, with straight-line proximity and existing direct town-to-town cargo lines. NOT a route or profit forecast.",
        "inputSchema": {"type":"object","properties":{
            "town":{"type":"string","description":"optional case-insensitive town substring, e.g. Timmins"},
            "cargo":{"type":"string","description":"optional cargo code, e.g. MEAT or BRICKS"},
            "sort_by":{"type":"string","enum":["unmet_supply","town","supply_fraction"]},
            "suppliers_per_need":{"type":"integer","description":"default 5, max 20"},
            "limit":{"type":"integer","description":"default 50, max 200"}
        }},
        "handler":tool_cargo_supply_opportunities,
    },
    {
        "name": "tf3_list_depots",
        "description": "Transport Fever 3: your depots with carrier, location, vehicles inside and nearest town.",
        "inputSchema": {"type": "object", "properties": {
            "carrier": {"type": "string", "enum": ["ROAD", "RAIL", "TRAM", "AIR", "WATER"]}}},
        "handler": tool_list_depots,
    },
    {
        "name": "tf3_finances",
        "description": "Transport Fever 3: company finances: balance, loan, earnings this year, company value and "
                       "statistics, passengers and cargo transported, the finance table per period and carrier, and "
                       "12-month line profit summed per carrier.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_finances,
    },
    {
        "name": "tf3_live_vehicles",
        "description": "Transport Fever 3: live vehicle check. Without a line: which lines have bunched vehicles "
                       "(uneven spacing) and which vehicles are stuck (no path, stopped, waiting long). With a line: "
                       "every vehicle's position, speed, state, next stop and load.",
        "inputSchema": {"type": "object", "properties": {
            "line": {"type": "string", "description": "line id or name"},
            "limit": {"type": "integer", "description": "default 25"}}},
        "handler": tool_live_vehicles,
    },
    {
        "name": "tf3_fleet_issues",
        "description": "Transport Fever 3: lines losing money, with low or full usage, problems or no vehicles, and vehicles "
                       "past their lifespan, without a path, stopped or poorly maintained.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "low_usage_pct": {"type": "number", "description": "flag lines below this usage (default 40)"},
                "high_usage_pct": {"type": "number", "description": "flag lines at or above this usage (default 95)"},
                "old_ratio": {"type": "number", "description": "flag vehicles at this share of lifespan (default 1.0)"},
                "limit": {"type": "integer", "description": "default 100 per list"},
            },
        },
        "handler": tool_fleet_issues,
    },
]

# v7.6 first-party diagnostic for the Lua station cargo-access membership export.
def tool_cargo_access_audit(args):
    data, warning, age = load_kind("cargo_access")
    rows = data.get("cargoAccessStations", []) if isinstance(data, dict) else []
    town = str(args.get("town") or "").strip().casefold()
    industry_id = args.get("industry_id")
    if industry_id is not None:
        industry_id = int(industry_id)
    station_groups = {}
    inspected = 0
    for item in rows:
        if not isinstance(item, dict):
            continue
        inspected += 1
        caps = item.get("terminalCapabilities") or {}
        c = item.get("catchables") or {}
        ids = c.get("matchingIndustryIds") or []
        if town and town not in str(item.get("town") or "").casefold() and industry_id is None:
            continue
        if industry_id is not None and industry_id not in ids and (not town or town not in str(item.get("town") or "").casefold()):
            continue
        g = item.get("stationGroupId")
        key = str(g)
        group = station_groups.setdefault(key, {"stationGroupId":g,"name":item.get("stationName"),
            "town":item.get("town"),"stationEntities":0,"cargoLoadTerminal":False,
            "cargoUnloadTerminal":False,"matchingIndustryIds":set(),"catchmentExportSupported":True})
        group["stationEntities"] += 1
        group["cargoLoadTerminal"] |= bool(caps.get("cargoLoad"))
        group["cargoUnloadTerminal"] |= bool(caps.get("cargoUnload"))
        group["matchingIndustryIds"].update(ids)
        group["catchmentExportSupported"] &= bool(c.get("supported"))
    groups=[]
    for g in station_groups.values():
        g["matchingIndustryIds"] = sorted(g["matchingIndustryIds"])
        groups.append(g)
    groups.sort(key=lambda g: (g.get("town") or "",g.get("name") or ""))
    return {"scope":"station_cargo_access_membership", "bridgeVersionRequired":76,
        "allStationEntitiesInspected":inspected,"matchingStationGroups":len(groups),
        "groups":groups[:max(1,min(int(args.get("limit",30)),100))],
        "semantics":"Industry entity ID present in getStationCatchables(st, true) and cargo terminal flags are independent observations. Neither alone nor together proves a working cargo loading/unloading path. No district delivery coordinates, track/road topology, costs, or wagon capacity are exported.",
        "warning":warning,"dataAgeSec":age}

TOOLS.append({"name":"tf3_cargo_access_audit",
 "description":"v7.6 Lua-exported cargo-station terminal flags and exact industry ID membership; diagnostic only, no verified delivery. Filter by town or industry_id.",
 "inputSchema":{"type":"object","properties":{"town":{"type":"string"},"industry_id":{"type":"integer"},"limit":{"type":"integer"}}},
 "handler":tool_cargo_access_audit})


# v7.7: integrate v7.6 cargo-access export with the generic v7.5 graph.
# Station flags and official cargo catchables are observations, not confirmed deliveries.
def tool_cargo_reachability_v77(args):
    from collections import defaultdict
    base=tool_cargo_reachability_v75(args)
    access_data,access_warning,access_age=load_kind('cargo_access')
    records=access_data.get('cargoAccessStations') if isinstance(access_data,dict) else None
    if not isinstance(records,list):
        raise RuntimeError('v7.7 requires v7.6 Lua cargo_access export; verify bridgeVersion >=76 and live save')
    groups=defaultdict(lambda:{'stationEntities':0,'cargoLoadTerminal':False,'cargoUnloadTerminal':False,
                               'matchingIndustryIds':set(),'allCatchableReadsSupported':True})
    for row in records:
        if not isinstance(row,dict) or row.get('stationGroupId') is None:continue
        g=groups[str(row['stationGroupId'])]
        g['stationEntities']+=1
        caps=row.get('terminalCapabilities') or {}
        g['cargoLoadTerminal'] |= bool(caps.get('cargoLoad'))
        g['cargoUnloadTerminal'] |= bool(caps.get('cargoUnload'))
        c=row.get('catchables') or {}
        g['matchingIndustryIds'].update(str(x) for x in (c.get('matchingIndustryIds') or []))
        g['allCatchableReadsSupported'] &= c.get('supported') is True
    def endpoint(station_id,industry_id,direction):
        if station_id is None:return {'state':'not_evaluated','reason':'no_station_endpoint'}
        g=groups.get(str(station_id))
        if not g:return {'state':'no_exported_station_record','stationGroupId':station_id}
        terminal=g['cargoLoadTerminal'] if direction=='origin' else g['cargoUnloadTerminal']
        membership = None if industry_id is None else str(industry_id) in g['matchingIndustryIds']
        if industry_id is None:
            state='terminal_observed_district_not_verified' if terminal else 'terminal_not_observed_district_not_verified'
        elif membership and terminal:
            state='terminal_and_industry_id_observed'
        elif membership:
            state='industry_id_only_terminal_not_observed'
        elif not g['allCatchableReadsSupported']:
            state='catchables_incomplete_unknown'
        elif terminal:
            state='terminal_observed_industry_id_not_found'
        else:
            state='no_cargo_access_evidence_observed'
        return {'state':state,'stationGroupId':station_id,'cargoTerminalFlagObserved':terminal,
                'industryIdPresentInCatchables':membership,'catchableReadsSupported':g['allCatchableReadsSupported']}
    def check_leg(leg,src_id,dst_id):
        state=leg.get('status')
        if state=='shared_local_station_access_unverified':
            origin_id=dest_id=leg.get('stationGroupId')
        elif state=='possible_existing_path_not_verified':
            origin_id=leg.get('originStationGroupId');dest_id=leg.get('destinationStationGroupId')
        else:
            origin_id=dest_id=None
        origin=endpoint(origin_id,src_id,'origin')
        dest=endpoint(dest_id,dst_id,'destination')
        terminal_and_ids=(origin['state']=='terminal_and_industry_id_observed' and
                          (dst_id is None or dest['state'] in ('terminal_and_industry_id_observed','terminal_observed_district_not_verified')))
        if state=='possible_existing_path_not_verified':
            grade='line_with_source_and_endpoint_observations' if terminal_and_ids else 'line_without_complete_endpoint_evidence'
        elif state=='shared_local_station_access_unverified':
            grade='local_station_overlap_only'
        elif state=='endpoint_access_unverified':
            grade='endpoint_not_located_within_radius'
        else:grade='no_line_path_in_exported_network'
        leg['cargoAccessEvidence']={'origin':origin,'destination':dest,'evidenceGrade':grade,
            'operationalDeliveryVerified':False}
        return 2 if grade=='line_with_source_and_endpoint_observations' else 1 if grade=='line_without_complete_endpoint_evidence' else 0
    for need in base.get('opportunities',[]):
        for chain in need.get('candidateChains',[]):
            chain_score=check_leg(chain['delivery'],chain['industryId'],None)
            input_summary=[]
            for inp in chain.get('inputs',[]):
                candidate_ratings=[]
                for src in inp.get('sources',[]):
                    candidate_ratings.append(check_leg(src['connection'],src['industryId'],chain['industryId']))
                input_summary.append(max(candidate_ratings,default=0))
            chain['endpointEvidenceGrade']='stronger_observed_endpoint_and_line_evidence' if chain_score==2 and all(v==2 for v in input_summary) else 'partial_observed_evidence' if chain_score or any(input_summary) else 'insufficient_evidence'
            chain['evidenceCounts']={'deliveryGrade':chain_score,'inputGrades':input_summary}
        # Rerank only returned candidates. No production-history weighting.
        labels={'stronger_observed_endpoint_and_line_evidence':0,'partial_observed_evidence':1,'insufficient_evidence':2}
        need['candidateChains'].sort(key=lambda c:(labels.get(c['endpointEvidenceGrade'],3),
            -c['evidenceCounts']['deliveryGrade'],-sum(c['evidenceCounts']['inputGrades']),
            c['deliveryStraightLineKm'] is None,c['deliveryStraightLineKm'] or 0,str(c['industry'])))
    base['scope']='whole_save_cargo_endpoint_evidence' if base.get('scope')=='whole_save' else 'filtered_cargo_endpoint_evidence'
    base['cargoAccessStationEntities']=len(records)
    base['cargoAccessStationGroups']=len(groups)
    base['evidenceSemantics']='Actual game-exported station cargo load/unload flags and getStationCatchables cargo industry-ID membership. These are independent observations and do NOT verify a working route, vehicle compatibility, loading, district delivery, costs or capacity.'
    base['rankLimit']='Re-ranks candidates already shortlisted by v7.5; not yet an exhaustive global optimum.'
    base['warnings']=base.get('warnings',[])+([access_warning] if access_warning else [])
    base['dataAgeSec']=max(base.get('dataAgeSec',0),access_age)
    if not bool(args.get('detailed',False)):
        for need in base['opportunities']:
            for chain in need['candidateChains']:
                for leg in [chain['delivery']]+[src['connection'] for i in chain.get('inputs',[]) for src in i.get('sources',[])]:
                    sequence=leg.pop('lineSequence',None)
                    if sequence is not None:
                        leg['lineNames']=[x.get('lineName') for x in sequence]
                    leg.pop('assumptions',None)
                chain.pop('notVerified',None)
    return base

TOOLS.append({'name':'tf3_cargo_reachability_v77',
    'description':'Read-only whole-save freight advisor v7.7. Combines v7.5 commodity network paths with v7.6 Lua cargo terminal flags and cargo catchable industry IDs. Grades evidence, never verifies delivery or costs.',
    'inputSchema':{'type':'object','properties':{
        'town_contains':{'type':'string'},'cargo':{'type':'string'},
        'limit':{'type':'integer','default':5},'factories_per_need':{'type':'integer','default':2},
        'input_sources_per_factory':{'type':'integer','default':1},
        'max_access_km':{'type':'number','default':1.0},'max_lines_per_leg':{'type':'integer','default':3},
        'detailed':{'type':'boolean','default':False}}},'handler':tool_cargo_reachability_v77})

# v7.8: read-only destination town-building/cargo-station membership diagnostic.
# This does NOT identify cargo-specific buildings or verify successful deliveries.
def tool_town_delivery_audit(args):
    data, warning, age = load_kind("town_delivery")
    records = data.get("townDeliveryStations", []) if isinstance(data, dict) else []
    if not isinstance(records, list):
        records=[]
    target = str(args.get("town") or "").strip().casefold()
    groups = {}
    for r in records:
        if not isinstance(r, dict): continue
        group_id = r.get("stationGroupId")
        if group_id is None: continue
        for t in (r.get("matchedTowns") or []):
            if not isinstance(t, dict): continue
            town_name = str(t.get("town") or "")
            if target and target not in town_name.casefold(): continue
            key=(t.get("townId"),group_id)
            g=groups.setdefault(key,{"town":town_name,"townId":t.get("townId"),
                "stationGroupId":group_id,"station":r.get("name"),
                "stationEntities":set(),"cargoLoadTerminal":False,"cargoUnloadTerminal":False,
                "buildingMatchesByStationEntity":0,"sampleBuildings":[]})
            g["stationEntities"].add(r.get("stationId"))
            g["cargoLoadTerminal"] |= bool(r.get("cargoLoadTerminal"))
            g["cargoUnloadTerminal"] |= bool(r.get("cargoUnloadTerminal"))
            g["buildingMatchesByStationEntity"] += int(t.get("matchingTownBuildingIds") or 0)
            for item in (t.get("sampleBuildings") or []):
                if isinstance(item,dict) and item.get("buildingId") not in {i.get("buildingId") for i in g["sampleBuildings"]} and len(g["sampleBuildings"])<12:
                    g["sampleBuildings"].append(item)
    out=[]
    for g in groups.values():
        g["stationEntities"] = len(g["stationEntities"])
        g["countsMayOverlapAcrossStationEntities"] = g["stationEntities"]>1
        out.append(g)
    out.sort(key=lambda g:(-g["buildingMatchesByStationEntity"],g["town"],g["station"]))
    limit=max(1,min(100,int(args.get("limit",30))))
    return {"scope":"town_building_station_cargo_catchment_diagnostic",
        "bridgeVersionRequired":78,"stationEntitiesInspected":len(records),
        "matchingTownStationPairs":len(out),"groups":out[:limit],
        "semantics":"Actual town-building entity IDs intersect station cargo catchables. Station flags are independent. Counts across station entities may double-count. Building type, cargo acceptance, station-to-building path, and operational delivery are NOT verified.",
        "warning":warning,"dataAgeSec":age}

TOOLS.append({"name":"tf3_town_delivery_audit",
    "description":"Read-only v7.8 Lua-derived town building membership in cargo station catchables. Diagnostics only; NOT cargo-type acceptance or verified delivery.",
    "inputSchema":{"type":"object","properties":{"town":{"type":"string"},"limit":{"type":"integer"}}},
    "handler":tool_town_delivery_audit})

# v7.9: cross-check station-group service against vehicle-derived line commodity lists.
# CargoUnload alone is not freight evidence: passenger bus stops may report it.
def tool_freight_delivery_audit(args):
    data, warning, age = load_kind("town_delivery")
    station_data, st_warning, _ = load_kind("stations")
    snapshot, snap_warning, _ = load_snapshot()
    target = str(args.get("town") or "").casefold().strip()
    raw = data.get("townDeliveryStations", []) if isinstance(data,dict) else []
    raw_stations = station_data.get("stations", []) if isinstance(station_data,dict) else []
    raw_lines = snapshot.get("lines", []) if isinstance(snapshot,dict) else []
    line_cargo = {str(l.get("id")):set(l.get("cargos") or []) for l in raw_lines if isinstance(l,dict)}
    by_group = {str(st.get("id")):st for st in raw_stations if isinstance(st,dict)}
    groups={}
    for r in raw:
        if not isinstance(r,dict) or r.get("stationGroupId") is None:continue
        for town in r.get("matchedTowns") or []:
            if not isinstance(town,dict):continue
            if target and target not in str(town.get("town") or "").casefold():continue
            key=(town.get("townId"),str(r["stationGroupId"]))
            g=groups.setdefault(key,{"town":town.get("town"),"townId":town.get("townId"),"stationGroupId":r["stationGroupId"],"station":r.get("name"),"stationEntities":set(),"cargoLoadFlag":False,"cargoUnloadFlag":False,"buildingMatchesSummed":0,"sampleBuildingIds":set()})
            g["stationEntities"].add(r.get("stationId"))
            g["cargoLoadFlag"] |= bool(r.get("cargoLoadTerminal"))
            g["cargoUnloadFlag"] |= bool(r.get("cargoUnloadTerminal"))
            g["buildingMatchesSummed"] += int(town.get("matchingTownBuildingIds") or 0)
            g["sampleBuildingIds"].update(x.get("buildingId") for x in town.get("sampleBuildings") or [] if isinstance(x,dict) and x.get("buildingId") is not None)
    included=[];excluded=[];unknown=[]
    for (_,gid),g in groups.items():
        st=by_group.get(gid)
        g["stationEntities"]=len(g["stationEntities"])
        g["sampleBuildingIds"]=sorted(g["sampleBuildingIds"])[:12]
        g["countsMayOverlap"]=g["stationEntities"]>1
        if st is None:
            g["classification"]="unknown_station_group_not_in_station_export";unknown.append(g);continue
        g["exportedTerminalKind"]=st.get("kind")
        lines=[]; freight=[]; passenger=[]; missing=[]
        for l in st.get("lines") or []:
            lid=l.get("id") if isinstance(l,dict) else l
            lc=line_cargo.get(str(lid))
            if lc is None:
                missing.append(lid);continue
            if any(c!="PASSENGERS" for c in lc):
                freight.append({"line":l.get("name") if isinstance(l,dict) else None,"cargoTypes":sorted(c for c in lc if c!="PASSENGERS")})
            if "PASSENGERS" in lc:passenger.append(lid)
        g["freightServingLines"]=freight[:6];g["freightServingLineCount"]=len(freight)
        g["passengerServingLineCount"]=len(passenger)
        g["unknownServingLineCount"]=len(missing)
        if freight:
            g["classification"]="freight_line_served_catchment_not_verified"; included.append(g)
        elif passenger and not missing:
            g["classification"]="passenger_only_no_freight_line";excluded.append(g)
        elif missing:
            g["classification"]="unknown_line_cargo_data";unknown.append(g)
        elif st.get("kind")=="passenger":
            g["classification"]="passenger_terminal_no_freight_line";excluded.append(g)
        else:
            g["classification"]="unserved_or_unverified_no_freight_line";unknown.append(g)
    for lst in (included,excluded,unknown):lst.sort(key=lambda g:(g.get("town") or "",g.get("station") or ""))
    limit=max(1,min(int(args.get("limit",30)),100))
    return {"scope":"freight_served_town_building_catchment_diagnostic","bridgeVersionRequired":78,"sourceStationGroups":len(groups),"freightCandidateCount":len(included),"excludedPassengerOnlyCount":len(excluded),"unknownCount":len(unknown),"freightCandidates":included[:limit],"excludedPassengerOnly":excluded[:limit],"unknownOrUnserved":unknown[:limit],"warnings":[w for w in (warning,st_warning,snap_warning) if w],"interpretation":"Only groups with at least one serving line whose exported vehicle cargo list includes non-PASSENGERS are included as freight candidates. Cargo terminal flags ALONE never qualify. Exclusion does not prove no local truck delivery. Building IDs are undifferentiated, may overlap and do NOT establish commodity-specific district coverage, actual cargo unloading, transfer, or profitability.","dataAgeSec":age}

TOOLS.append({"name":"tf3_freight_delivery_audit","description":"v7.9 filters passenger-only bus/tram stops out of town-building freight candidate evidence using actual serving line cargo types. Read-only; v7.8 Lua mod.","inputSchema":{"type":"object","properties":{"town":{"type":"string"},"limit":{"type":"integer"}}},"handler":tool_freight_delivery_audit})


# v8.0: read-only evidence-based investigation shortlist. No fabricated ROI/build costs.
def tool_network_investment_advisor(args):
    limit=max(1,min(int(args.get("limit",8)),25))
    cargo_scan_limit=max(limit*4,min(int(args.get("cargo_scan_limit",20)),75))
    passenger_scan_limit=max(limit*4,min(int(args.get("passenger_scan_limit",35)),100))
    max_access_km=float(args.get("max_access_km",1.0))
    town_contains=str(args.get("town_contains") or "")
    base_args={"limit":cargo_scan_limit,"factories_per_need":max(1,min(int(args.get("factories_per_need",3)),5)),
               "input_sources_per_factory":max(1,min(int(args.get("input_sources_per_factory",2)),5)),
               "max_access_km":max_access_km,"max_lines_per_leg":max(1,min(int(args.get("max_lines_per_leg",3)),5))}
    if town_contains:base_args['town_contains']=town_contains
    cargo=tool_cargo_reachability_v77(base_args)
    passenger=tool_network_opportunities({"limit":passenger_scan_limit,"town_contains":town_contains,
                                           "focus":"passenger","min_waiting":int(args.get("min_waiting",50))})
    report=[]
    def add_item(item,sortkey):
        item['_rank_key']=sortkey;report.append(item)
    for need in cargo.get('opportunities') or []:
        chains=need.get('candidateChains') or []
        if not chains:continue
        top=chains[0]
        delivery=top.get('delivery') or {}
        e=top.get('evidenceCounts') or {}
        inputs=top.get('inputs') or []
        input_hints=[]
        for inp in inputs:
            srcs=inp.get('sources') or []
            if srcs:
                src=srcs[0];connection=src.get('connection') or {}
                input_hints.append({'cargo':inp.get('cargo'),'source':src.get('industry'),
                    'pathStatus':connection.get('status'),'lineNames':connection.get('lineNames') or [],
                    'endpointEvidence':(connection.get('cargoAccessEvidence') or {}).get('evidenceGrade'),
                    'furtherInputsNotChecked':src.get('furtherInputs') or []})
            else:
                input_hints.append({'cargo':inp.get('cargo'),'source':None,'pathStatus':'no_shown_supplier'})
        delivery_status=delivery.get('status')
        grades=[e.get('deliveryGrade') or 0]+(e.get('inputGrades') or [])
        required=len(grades)
        observed=sum(1 for x in grades if x>=2)
        linelegs=sum(1 for x in grades if x>=1)
        if linelegs==required and observed==required:
            readiness='stronger_existing_network_evidence'
            next_action='Verify commodity delivery buildings and actual shipments; inspect whether existing service needs more capacity'
            effort='inspect_or_optimize_existing_service'
        elif linelegs==required:
            readiness='partial_cargo_endpoint_evidence'
            next_action='Check industry loading catchments and freight endpoints before adding trains or stations'
            effort='verify_access_before_building'
        elif linelegs:
            readiness='some_legs_without_path_evidence'
            next_action='Inspect the missing cargo legs and nearby unused freight terminals; do not assume new construction is required'
            effort='possible_service_or_infrastructure_gap'
        else:
            readiness='no_usable_line_path_shown'
            next_action='Investigate local collection and delivery options, including road freight, before planning construction'
            effort='unknown_may_require_new_service'
        # A cargo line observed in a station group is not proof of a compatible freight vehicle or delivery.
        report_item={'category':'cargo','town':need.get('town'),'cargo':need.get('cargo'),
            'gameSupplyGap':need.get('unmetSupplyToLimit'),'candidateFactory':top.get('industry'),
            'factoryId':top.get('industryId'),'requiredShownLegs':required,'legsWithLineEvidence':linelegs,
            'legsWithStrongerEndpointObservations':observed,'readiness':readiness,
            'deliveryStatus':delivery_status,'deliveryLineNames':delivery.get('lineNames') or [],
            'inputLegs':input_hints,'suggestedNextAction':next_action,'effortCategory':effort,
            'isVerifiedDelivering':False,'estimatedConstructionCost':None,'expectedProfit':None,
            'evidenceLimitations':['Town center is a delivery proxy; cargo-specific demand buildings not verified',
                'Line cargo listings are not vehicle capability or successful shipments',
                'Upstream dependencies may extend beyond shown inputs']}
        # Rank by actionable evidence first, then degree of deficit. Never imply an ROI estimate.
        gap=float(need.get('unmetSupplyToLimit') or 0)
        add_item(report_item,(0,-observed,-linelegs,-gap,str(need.get('town') or '')))
    for t in passenger.get('towns') or []:
        flags=[x for x in (t.get('indications') or []) if x.get('category')=='passenger']
        if not flags:continue
        waiting=(t.get('waiting') or {}).get('passengers') or 0
        codes=[x.get('code') for x in flags]
        reasons=[x.get('label') for x in flags]
        next_action=('Inspect vehicle count, stop spacing, headways and platform queues before adding service'
                     if 'passenger_waiting' in codes else 'Inspect passenger catchment coverage and overlapping stops')
        add_item({'category':'passenger','town':t.get('town'),'waitingPassengersAtSnapshot':waiting,
            'indications':reasons,'indicationCodes':codes,'suggestedNextAction':next_action,
            'effortCategory':'inspect_or_optimize_existing_service','estimatedConstructionCost':None,
            'expectedProfit':None,'isVerifiedInvestmentOpportunity':False,
            'evidenceLimitations':['Single-time queue observation','Not a route demand forecast or profit estimate']},
            (1,-len(codes),-waiting,0,str(t.get('town') or '')))
    report.sort(key=lambda x:x.pop('_rank_key'))
    # v7.9 freight audit cross-check: never use passenger-only bus stop buildings as cargo coverage.
    freight_summaries={}
    for item in report[:limit]:
        if item['category']!='cargo':continue
        town=item.get('town')
        if town in freight_summaries:continue
        freight=tool_freight_delivery_audit({'town':town,'limit':100})
        exact=[g for g in freight.get('freightCandidates') or [] if str(g.get('town') or '').casefold()==str(town or '').casefold()]
        exclusions=[g for g in freight.get('excludedPassengerOnly') or [] if str(g.get('town') or '').casefold()==str(town or '').casefold()]
        unknown=[g for g in freight.get('unknownOrUnserved') or [] if str(g.get('town') or '').casefold()==str(town or '').casefold()]
        freight_summaries[town]={'freightServedGroups':len(exact),'passengerOnlyExcluded':len(exclusions),
             'unservedOrUnknownGroups':len(unknown),'freightStations':[{'name':g.get('station'),
             'groupId':g.get('stationGroupId'),'reportedBuildingMatchesNotCommoditySpecific':g.get('buildingMatchesSummed'),
             'listedCargoTypes':sorted({c for l in g.get('freightServingLines') or [] for c in l.get('cargoTypes') or []})} for g in exact[:10]]}
    for item in report[:limit]:
        if item['category']=='cargo':item['destinationFreightEvidence']=freight_summaries.get(item.get('town'))
    return {'scope':'whole_save_read_only_investigation_shortlist','version':'8.0-dev',
        'totalSaveTowns':cargo.get('saveTowns'),'totalIndustries':cargo.get('industries'),
        'matchingCargoNeeds':cargo.get('matchingUnmetNeeds'),'cargoNeedsEvaluated':len(cargo.get('opportunities') or []),
        'passengerTownsConsidered':len(passenger.get('towns') or []),
        'returnedRecommendations':min(limit,len(report)),'recommendations':report[:limit],
        'ordering':'Cargo evidence-first shortlist, then passenger investigation flags. NOT ROI-ranked. Not an exhaustive optimum.',
        'knownLimitations':['No actual commodity-specific destination building acceptance','No reliable construction cost or profitability forecasts',
            'No confirmed vehicle cargo capability, cargo movements, direction, or transfers',
            'Passenger checks use momentary queues and station catchment indicators',
            'Only top cargo needs and shortlisted factories are considered; scan limits can omit opportunities'],
        'warnings':list(dict.fromkeys((cargo.get('warnings') or [])+(passenger.get('warnings') or []))),
        'dataAgeSec':max(float(cargo.get('dataAgeSec') or 0),float(passenger.get('dataAgeSec') or 0))}

TOOLS.append({'name':'tf3_network_investment_advisor',
    'description':'v8.0 DEV whole-save evidence-led, read-only investigation shortlist combining cargo-chain reachability, v7.9 freight-only delivery filtering, and passenger flags. No fabricated ROI or building costs.',
    'inputSchema':{'type':'object','properties':{
        'limit':{'type':'integer','default':8},'town_contains':{'type':'string'},
        'cargo_scan_limit':{'type':'integer','default':20},'passenger_scan_limit':{'type':'integer','default':35},
        'factories_per_need':{'type':'integer','default':3},'input_sources_per_factory':{'type':'integer','default':2},
        'max_access_km':{'type':'number','default':1.0},'max_lines_per_leg':{'type':'integer','default':3},
        'min_waiting':{'type':'integer','default':50}}},'handler':tool_network_investment_advisor})

def tool_network_investment_advisor_v81(args):
    limit=max(1,min(int(args.get("limit",8)),25))
    cargo_scan_limit=max(limit*4,min(int(args.get("cargo_scan_limit",20)),75))
    passenger_scan_limit=max(limit*4,min(int(args.get("passenger_scan_limit",35)),100))
    max_access_km=float(args.get("max_access_km",1.0))
    town_contains=str(args.get("town_contains") or "")
    base_args={"limit":cargo_scan_limit,"factories_per_need":max(1,min(int(args.get("factories_per_need",3)),5)),
               "input_sources_per_factory":max(1,min(int(args.get("input_sources_per_factory",2)),5)),
               "max_access_km":max_access_km,"max_lines_per_leg":max(1,min(int(args.get("max_lines_per_leg",3)),5))}
    if town_contains:base_args['town_contains']=town_contains
    cargo=tool_cargo_reachability_v77(base_args)
    passenger=tool_network_opportunities({"limit":passenger_scan_limit,"town_contains":town_contains,
                                           "focus":"passenger","min_waiting":int(args.get("min_waiting",50))})
    report=[]
    def add_item(item,sortkey):
        item['_rank_key']=sortkey;report.append(item)
    for need in cargo.get('opportunities') or []:
        chains=need.get('candidateChains') or []
        if not chains:continue
        top=chains[0]
        delivery=top.get('delivery') or {}
        e=top.get('evidenceCounts') or {}
        inputs=top.get('inputs') or []
        input_hints=[]
        for inp in inputs:
            srcs=inp.get('sources') or []
            if srcs:
                src=srcs[0];connection=src.get('connection') or {}
                input_hints.append({'cargo':inp.get('cargo'),'source':src.get('industry'),
                    'pathStatus':connection.get('status'),'lineNames':connection.get('lineNames') or [],
                    'endpointEvidence':(connection.get('cargoAccessEvidence') or {}).get('evidenceGrade'),
                    'furtherInputsNotChecked':src.get('furtherInputs') or []})
            else:
                input_hints.append({'cargo':inp.get('cargo'),'source':None,'pathStatus':'no_shown_supplier'})
        delivery_status=delivery.get('status')
        grades=[e.get('deliveryGrade') or 0]+(e.get('inputGrades') or [])
        required=len(grades)
        observed=sum(1 for x in grades if x>=2)
        linelegs=sum(1 for x in grades if x>=1)
        if linelegs==required and observed==required:
            readiness='stronger_existing_network_evidence'
            next_action='Verify commodity delivery buildings and actual shipments; inspect whether existing service needs more capacity'
            effort='inspect_or_optimize_existing_service'
        elif linelegs==required:
            readiness='partial_cargo_endpoint_evidence'
            next_action='Check industry loading catchments and freight endpoints before adding trains or stations'
            effort='verify_access_before_building'
        elif linelegs:
            readiness='some_legs_without_path_evidence'
            next_action='Inspect the missing cargo legs and nearby unused freight terminals; do not assume new construction is required'
            effort='possible_service_or_infrastructure_gap'
        else:
            readiness='no_usable_line_path_shown'
            next_action='Investigate local collection and delivery options, including road freight, before planning construction'
            effort='unknown_may_require_new_service'
        # A cargo line observed in a station group is not proof of a compatible freight vehicle or delivery.
        report_item={'category':'cargo','town':need.get('town'),'cargo':need.get('cargo'),
            'gameSupplyGap':need.get('unmetSupplyToLimit'),'candidateFactory':top.get('industry'),
            'factoryId':top.get('industryId'),'requiredShownLegs':required,'legsWithLineEvidence':linelegs,
            'legsWithStrongerEndpointObservations':observed,'readiness':readiness,
            'deliveryStatus':delivery_status,'deliveryLineNames':delivery.get('lineNames') or [],
            'inputLegs':input_hints,'suggestedNextAction':next_action,'effortCategory':effort,
            'isVerifiedDelivering':False,'estimatedConstructionCost':None,'expectedProfit':None,
            'evidenceLimitations':['Town center is a delivery proxy; cargo-specific demand buildings not verified',
                'Line cargo listings are not vehicle capability or successful shipments',
                'Upstream dependencies may extend beyond shown inputs']}
        # Rank by actionable evidence first, then degree of deficit. Never imply an ROI estimate.
        gap=float(need.get('unmetSupplyToLimit') or 0)
        add_item(report_item,(0,-observed,-linelegs,-gap,str(need.get('town') or '')))
    for t in passenger.get('towns') or []:
        flags=[x for x in (t.get('indications') or []) if x.get('category')=='passenger']
        if not flags:continue
        waiting=(t.get('waiting') or {}).get('passengers') or 0
        codes=[x.get('code') for x in flags]
        reasons=[x.get('label') for x in flags]
        next_action=('Inspect vehicle count, stop spacing, headways and platform queues before adding service'
                     if 'passenger_waiting' in codes else 'Inspect passenger catchment coverage and overlapping stops')
        add_item({'category':'passenger','town':t.get('town'),'waitingPassengersAtSnapshot':waiting,
            'indications':reasons,'indicationCodes':codes,'suggestedNextAction':next_action,
            'effortCategory':'inspect_or_optimize_existing_service','estimatedConstructionCost':None,
            'expectedProfit':None,'isVerifiedInvestmentOpportunity':False,
            'evidenceLimitations':['Single-time queue observation','Not a route demand forecast or profit estimate']},
            (1,-len(codes),-waiting,0,str(t.get('town') or '')))
    report.sort(key=lambda x:x.pop('_rank_key'))
    cargo_pool=[x for x in report if x['category']=='cargo']
    passenger_pool=[x for x in report if x['category']=='passenger']
    # Allocate roughly half the slots to each category when both have evidence.
    # Preserve source-specific ordering inside each category, never compare supply
    # gap numbers against passenger queue numbers as if they were the same unit.
    if cargo_pool and passenger_pool and limit > 1:
        passenger_target=min(len(passenger_pool),limit//2)
        cargo_target=min(len(cargo_pool),limit-passenger_target)
        spare=limit-passenger_target-cargo_target
        cargo_target+=min(spare,len(cargo_pool)-cargo_target)
        spare=limit-passenger_target-cargo_target
        passenger_target+=min(spare,len(passenger_pool)-passenger_target)
        selected=[]
        c_it=iter(cargo_pool[:cargo_target]);p_it=iter(passenger_pool[:passenger_target])
        # Alternate to make the mixed shortlist easier to read.
        for _ in range(max(cargo_target,passenger_target)):
            c=next(c_it,None);v=next(p_it,None)
            if c is not None:selected.append(c)
            if v is not None:selected.append(v)
        report=selected
    else:
        report=report[:limit]
    category_breakdown={'cargo':sum(x['category']=='cargo' for x in report),
                        'passenger':sum(x['category']=='passenger' for x in report)}
    # v7.9 freight audit cross-check: never use passenger-only bus stop buildings as cargo coverage.
    freight_summaries={}
    for item in report[:limit]:
        if item['category']!='cargo':continue
        town=item.get('town')
        if town in freight_summaries:continue
        freight=tool_freight_delivery_audit({'town':town,'limit':100})
        exact=[g for g in freight.get('freightCandidates') or [] if str(g.get('town') or '').casefold()==str(town or '').casefold()]
        exclusions=[g for g in freight.get('excludedPassengerOnly') or [] if str(g.get('town') or '').casefold()==str(town or '').casefold()]
        unknown=[g for g in freight.get('unknownOrUnserved') or [] if str(g.get('town') or '').casefold()==str(town or '').casefold()]
        freight_summaries[town]={'freightServedGroups':len(exact),'passengerOnlyExcluded':len(exclusions),
             'unservedOrUnknownGroups':len(unknown),'freightStations':[{'name':g.get('station'),
             'groupId':g.get('stationGroupId'),'reportedBuildingMatchesNotCommoditySpecific':g.get('buildingMatchesSummed'),
             'listedCargoTypes':sorted({c for l in g.get('freightServingLines') or [] for c in l.get('cargoTypes') or []})} for g in exact[:10]]}
    for item in report[:limit]:
        if item['category']=='cargo':item['destinationFreightEvidence']=freight_summaries.get(item.get('town'))
    return {'scope':'whole_save_read_only_investigation_shortlist','version':'8.1-dev',
        'totalSaveTowns':cargo.get('saveTowns'),'totalIndustries':cargo.get('industries'),
        'matchingCargoNeeds':cargo.get('matchingUnmetNeeds'),'cargoNeedsEvaluated':len(cargo.get('opportunities') or []),
        'passengerTownsConsidered':len(passenger.get('towns') or []),
        'returnedRecommendations':min(limit,len(report)),'recommendations':report[:limit],
        'ordering':'Balanced category slots when both passenger and cargo findings exist; evidence-ordered within each category, not ROI-ranked.',
        'categoryBreakdown':category_breakdown,
        'passengerFindingsAvailable':len(passenger_pool),'cargoFindingsAvailable':len(cargo_pool),
        'knownLimitations':['No actual commodity-specific destination building acceptance','No reliable construction cost or profitability forecasts',
            'No confirmed vehicle cargo capability, cargo movements, direction, or transfers',
            'Passenger checks use momentary queues and station catchment indicators',
            'Only top cargo needs and shortlisted factories are considered; scan limits can omit opportunities'],
        'warnings':list(dict.fromkeys((cargo.get('warnings') or [])+(passenger.get('warnings') or []))),
        'dataAgeSec':max(float(cargo.get('dataAgeSec') or 0),float(passenger.get('dataAgeSec') or 0))}


TOOLS.append({'name':'tf3_network_investment_advisor_v81',
    'description':'v8.1 DEV: evidence-led balanced passenger and cargo investigation shortlist. Separate category quotas avoid cargo drowning out passenger service issues. Read-only, no ROI claims.',
    'inputSchema':{'type':'object','properties':{
        'limit':{'type':'integer','default':8},'town_contains':{'type':'string'},
        'cargo_scan_limit':{'type':'integer','default':40},'passenger_scan_limit':{'type':'integer','default':100},
        'factories_per_need':{'type':'integer','default':3},'input_sources_per_factory':{'type':'integer','default':2},
        'max_access_km':{'type':'number','default':1.0},'max_lines_per_leg':{'type':'integer','default':3},
        'min_waiting':{'type':'integer','default':50}}},'handler':tool_network_investment_advisor_v81})


# v8.2 DEV: station-to-line diagnostic with explicit queue attribution uncertainty.
def tool_passenger_line_diagnostic_v82(args):
    snap, w1, age1 = load_snapshot()
    data, w2, age2 = load_kind('stations')
    town_filter = str(args.get('town') or '').strip().casefold()
    line_filter = str(args.get('line_contains') or '').strip().casefold()
    limit = max(1, min(int(args.get('limit', 15)), 100))
    lines = [l for l in snap.get('lines') or [] if 'PASSENGERS' in (l.get('cargos') or [])]
    indexed={str(l.get('id')):l for l in lines}
    results=[]
    for st in data.get('stations') or []:
        town=str(st.get('town') or '')
        if town_filter and town_filter not in town.casefold(): continue
        serving=[]
        for record in st.get('lines') or []:
            lid=record.get('id') if isinstance(record,dict) else record
            line=indexed.get(str(lid))
            if line is not None and (not line_filter or line_filter in str(line.get('name') or '').casefold()):
                serving.append(line)
        if not serving: continue
        wait=st.get('waiting') or {}
        pax=sum(float(v) for k,v in wait.items() if 'PASSENGER' in str(k).upper() and isinstance(v,(int,float)))
        usage=st.get('usage') or {}
        capacity=(usage.get('poolCapacity') or 0)+(usage.get('terminalCapacity') or 0)
        rows=[]
        for line in serving:
            rows.append({'lineId':line.get('id'),'line':line.get('name'),
                'carriers':line.get('carriers') or [],'vehicles':line.get('vehicleCount'),
                'intervalSec':interval_sec(line),'usagePct':line.get('usagePct'),
                'profit12m':line.get('profit12m'), 'problem':line.get('problem'),
                'queueAttribution':'shared_station_queue_not_assigned_to_line'})
        rows.sort(key=lambda x:(x.get('intervalSec') is None,-(x.get('intervalSec') or 0),str(x.get('line') or '')))
        results.append({'town':town,'stationGroupId':st.get('id'),'station':st.get('name'),
            'waitingPassengersSnapshot':round(pax,2),'stationUsagePct':pct(usage.get('used') or 0,capacity),
            'passengerLinesServing':len(rows),'lines':rows[:12],
            'interpretation':'Waiting passengers belong to the station group, not necessarily any displayed line.'})
    results.sort(key=lambda x:(-x['waitingPassengersSnapshot'],x['town'],str(x['station'])))
    return {'scope':'passenger_station_line_diagnostic','version':'8.2-dev',
        'matchingPassengerStations':len(results),'returnedStations':min(limit,len(results)),
        'stations':results[:limit],
        'caveats':['Station queue shared among all serving lines, never attributed to one line',
            'Line usage is aggregate recent load, not station-specific','Interval is derived from reported line frequency',
            'Profit is last 12 game months, not incremental profit from adding vehicles',
            'Single snapshot cannot establish chronic congestion'],
        'warnings':[x for x in (w1,w2) if x], 'dataAgeSec':round(max(age1,age2),2)}

TOOLS.append({'name':'tf3_passenger_line_diagnostic_v82',
  'description':'v8.2 DEV read-only passenger queue by station, with serving lines, headway proxy, vehicles, usage and 12-month profit; queues explicitly NOT attributed to individual lines.',
  'inputSchema':{'type':'object','properties':{'town':{'type':'string'},'line_contains':{'type':'string'},'limit':{'type':'integer','default':15}}},
  'handler':tool_passenger_line_diagnostic_v82})

def tool_network_investment_advisor_v82(args):
    original=tool_network_investment_advisor_v81(args)
    # Enrich passenger recommendations with their own town's actual passenger station/line evidence.
    diagnostics={}
    for recommendation in original.get('recommendations') or []:
        if recommendation.get('category')!='passenger':continue
        town=recommendation.get('town')
        if town not in diagnostics:
            diag=tool_passenger_line_diagnostic_v82({'town':town,'limit':50})
            # Exact town matching protects against substring-ambiguous town names.
            stations=[s for s in diag.get('stations') or [] if str(s.get('town') or '').casefold()==str(town or '').casefold()]
            diagnostics[town]=sorted(stations,key=lambda s:-s.get('waitingPassengersSnapshot',0))
        stations=diagnostics[town]
        recommendation['busyStations']=[{'station':s.get('station'),'stationGroupId':s.get('stationGroupId'),
            'waitingPassengersSnapshot':s.get('waitingPassengersSnapshot'),
            'passengerLinesServing':s.get('passengerLinesServing'),
            'servingLines':s.get('lines',[])[:6]} for s in stations[:5]]
        recommendation['stationQueueAttribution']='Shared station queue. Cannot determine which service passengers are waiting for.'
        recommendation['suggestedNextAction']='Inspect busiest station and listed services; compare line utilization, headway and profit before adding capacity'
    original['version']='8.2-dev'
    original['ordering']='Balanced cargo/passenger investigation shortlist, with passenger station-and-line details. Not ROI ranked.'
    original['lineDiagnosticCoverage']='Passenger rows enriched with actual serving-line telemetry, not line-specific passenger queues.'
    original['knownLimitations']=(original.get('knownLimitations') or [])+['Station waiting passengers cannot be attributed to an individual serving line']
    return original

TOOLS.append({'name':'tf3_network_investment_advisor_v82',
 'description':'v8.2 DEV balanced cargo and passenger investment investigation advisor with detailed passenger stations and lines, read-only and no invented ROI.',
 'inputSchema':{'type':'object','properties':{
 'limit':{'type':'integer','default':8},'town_contains':{'type':'string'},
 'cargo_scan_limit':{'type':'integer','default':40},'passenger_scan_limit':{'type':'integer','default':100},
 'factories_per_need':{'type':'integer','default':3},'input_sources_per_factory':{'type':'integer','default':2},
 'max_access_km':{'type':'number','default':1.0},'max_lines_per_leg':{'type':'integer','default':3},
 'min_waiting':{'type':'integer','default':50}}},'handler':tool_network_investment_advisor_v82})


# v8.3 DEV: unique-line passenger capacity screening. A station queue is NEVER
# allocated to any particular line. This is an investigation priority, not a fleet order.
def tool_passenger_capacity_advisor_v83(args):
    diag=tool_passenger_line_diagnostic_v82({'town':args.get('town') or '',
        'line_contains':args.get('line_contains') or '', 'limit':100})
    limit=max(1,min(int(args.get('limit',15)),100))
    min_wait=max(0,float(args.get('min_station_waiting',20)))
    by_line={}
    for st in diag.get('stations') or []:
        waiting=float(st.get('waitingPassengersSnapshot') or 0)
        for ln in st.get('lines') or []:
            lid=str(ln.get('lineId'))
            item=by_line.setdefault(lid,{'lineId':ln.get('lineId'),'line':ln.get('line'),
                'carriers':ln.get('carriers') or [],'vehicles':ln.get('vehicles'),
                'intervalSec':ln.get('intervalSec'),'usagePct':ln.get('usagePct'),
                'profit12m':ln.get('profit12m'),'problem':ln.get('problem'),
                'servingStations':[]})
            item['servingStations'].append({'station':st.get('station'),
                'stationGroupId':st.get('stationGroupId'), 'town':st.get('town'),
                'sharedQueueSnapshot':round(waiting,2)})
    results=[]
    for item in by_line.values():
        usage=item.get('usagePct')
        try: usage=float(usage) if usage is not None else None
        except (TypeError,ValueError):usage=None
        stations=sorted(item.pop('servingStations'),key=lambda st:-st['sharedQueueSnapshot'])
        busy=[st for st in stations if st['sharedQueueSnapshot']>=min_wait]
        if usage is None:
            grade='unknown_utilization'; action='Verify line occupancy and station waiting trends before changing the fleet'
            priority=2
        elif usage>=95 and busy:
            grade='capacity_review_high_priority';action='Inspect repeated load factors, left-behind passengers, and platform queues; compare longer vehicles versus another vehicle'
            priority=0
        elif usage>=95:
            grade='full_line_review';action='Review line load history and vehicle capacity; no large shared queue observed at its displayed stops'
            priority=1
        elif usage>=80 and busy:
            grade='monitor_near_full_and_busy_stations';action='Monitor line capacity alongside busy stops; confirm which service is generating each queue'
            priority=1
        elif usage<60 and busy:
            grade='busy_stops_but_line_not_full';action='Investigate transfers, timetable mismatch, other serving lines and direction before increasing this fleet'
            priority=3
        elif usage<60:
            grade='no_capacity_increase_indicated';action='No fleet increase indicated from this snapshot; monitor and inspect service pattern if needed'
            priority=5
        else:
            grade='monitor_no_confirmed_capacity_shortage';action='Check repeated station queues and line load history before changing capacity'
            priority=4
        item.update({'investigationGrade':grade,'suggestedNextAction':action,
            'busySharedQueueStations':busy[:5],'stationsObserved':len(stations),
            'topStations':stations[:5],'queueAttribution':'shared_station_queue_not_line_specific',
            'capacityIncreaseRecommended':False,'upgradeCost':None,'incrementalProfit':None,
            'evidenceLimitations':['Station queue is shared by all serving services; not line-specific demand',
                'Line utilization is an aggregate snapshot/recent metric, not directional or peak-hour loading',
                'Vehicle seating/capacity and increment economics not verified',
                'A single snapshot cannot prove persistent crowding']})
        results.append((priority,-max(0,usage if usage is not None else 0),
            -max([st['sharedQueueSnapshot'] for st in stations],default=0),str(item.get('line') or ''),item))
    results.sort(key=lambda x:x[:4])
    return {'scope':'unique_passenger_line_capacity_investigations','version':'8.3-dev',
      'linesEvaluated':len(results),'returnedLines':min(limit,len(results)),
      'lines':[it[-1] for it in results[:limit]],
      'prioritySemantics':'Investigations prioritized by line usage and shared station queue corroboration; not automatic vehicle purchase decisions or ROI ranking',
      'warnings':diag.get('warnings') or [],'dataAgeSec':diag.get('dataAgeSec'),
      'sourceStationCount':diag.get('matchingPassengerStations')}

TOOLS.append({'name':'tf3_passenger_capacity_advisor_v83',
 'description':'v8.3 DEV unique passenger-line capacity review shortlist using utilization, headway, vehicles, profit and observed station queues without assigning queues to a line.',
 'inputSchema':{'type':'object','properties':{'town':{'type':'string'},'line_contains':{'type':'string'},
 'limit':{'type':'integer','default':15},'min_station_waiting':{'type':'number','default':20}}},
 'handler':tool_passenger_capacity_advisor_v83})

def tool_network_investment_advisor_v83(args):
    report=tool_network_investment_advisor_v82(args)
    capacity_cache={}
    for rec in report.get('recommendations') or []:
        if rec.get('category')!='passenger':continue
        town=rec.get('town')
        if town not in capacity_cache:
            capacity_cache[town]=tool_passenger_capacity_advisor_v83({'town':town,'limit':30,
                'min_station_waiting':args.get('min_station_waiting',20)})
        cap=capacity_cache[town]
        rec['capacityReviewLines']=[{'lineId':ln.get('lineId'),'line':ln.get('line'),
            'usagePct':ln.get('usagePct'),'vehicles':ln.get('vehicles'),
            'intervalSec':ln.get('intervalSec'),'profit12m':ln.get('profit12m'),
            'investigationGrade':ln.get('investigationGrade'),
            'suggestedNextAction':ln.get('suggestedNextAction'),
            'busySharedQueueStations':ln.get('busySharedQueueStations')} for ln in cap.get('lines',[])[:5]]
        rec['suggestedNextAction']='Review the flagged individual lines and shared stations; confirm repeated capacity shortage before adding vehicles'
    report['version']='8.3-dev'
    report['passengerLineCapacityEnrichment']='Deduplicated line-level capacity investigations based on observed metrics; station queues NOT attributed to lines'
    report['knownLimitations']=(report.get('knownLimitations') or [])+['No confirmed incremental capacity, crowding persistence, or ROI for buying vehicles']
    return report

TOOLS.append({'name':'tf3_network_investment_advisor_v83',
 'description':'v8.3 DEV combined balanced passenger/cargo advisor enriched with unique-line passenger capacity reviews.',
 'inputSchema':{'type':'object','properties':{'limit':{'type':'integer','default':8},
 'town_contains':{'type':'string'},'cargo_scan_limit':{'type':'integer','default':40},
 'passenger_scan_limit':{'type':'integer','default':100},
 'factories_per_need':{'type':'integer','default':3},'input_sources_per_factory':{'type':'integer','default':2},
 'max_access_km':{'type':'number','default':1.0},'max_lines_per_leg':{'type':'integer','default':3},
 'min_waiting':{'type':'integer','default':50},'min_station_waiting':{'type':'number','default':20}}},
 'handler':tool_network_investment_advisor_v83})


# v8.4 DEV: persistent read-only game telemetry history. Writes ONLY to a local advisory cache.
def _v84_history_root():
    root=Path(os.environ.get('TF3_HISTORY_DIR') or (Path.home()/'.tf3-network-planner'/'dev-history'))
    root.mkdir(parents=True,exist_ok=True)
    return root

def _v84_identity(snap, args):
    # Town fingerprint is not a unique save ID; require an explicit playthrough key.
    towns=sorted((str(t.get('id')),str(t.get('name'))) for t in (snap.get('towns') or []))
    # Snapshot does not always include industries. Stable town identity alone is not guaranteed unique across saves.
    payload=json.dumps(towns,separators=(',',':'),ensure_ascii=False)
    fingerprint=hashlib.sha256(payload.encode()).hexdigest()[:20]
    key=str(args.get('save_key') or '').strip()
    if key and (len(key)>96 or not all(c.isalnum() or c in ' _.-' for c in key)):
        raise ValueError('save_key must be 1-96 characters using letters, numbers, spaces, dots, hyphens or underscores')
    if not key:
        raise ValueError('save_key is required for history; use a different key for each independent save/playthrough')
    label=key
    ident=hashlib.sha256((fingerprint+'|'+key).encode()).hexdigest()[:24]
    return ident, label, fingerprint, bool(key)

def _v84_history_file(identity):
    return _v84_history_root()/('tf3_v84_'+identity+'.json')

def _v84_read(path):
    if not path.exists():return {'schema':1,'samples':[]}
    with path.open('r',encoding='utf-8') as fh: value=json.load(fh)
    if not isinstance(value,dict) or not isinstance(value.get('samples'),list):
        raise RuntimeError('History file is invalid: '+str(path))
    return value

def _v84_write(path,value):
    fd,temp=tempfile.mkstemp(prefix='.tf3_v84_',suffix='.json',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as fh:
            json.dump(value,fh,ensure_ascii=False,separators=(',',':'))
            fh.flush(); os.fsync(fh.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def _v84_extract(snap,stations):
    lines={}
    for line in snap.get('lines') or []:
        if 'PASSENGERS' not in (line.get('cargos') or []):continue
        lid=str(line.get('id'))
        if lid=='None':continue
        val=line.get('usagePct')
        try: val=float(val) if val is not None else None
        except (ValueError,TypeError):val=None
        lines[lid]={'id':line.get('id'),'name':line.get('name'),'usagePct':val,
                    'vehicleCount':line.get('vehicleCount'),'profit12m':line.get('profit12m'),
                    'intervalSec':interval_sec(line)}
    all_stations={}
    for st in stations.get('stations') or []:
        serving=[]
        for rec in st.get('lines') or []:
            lid=rec.get('id') if isinstance(rec,dict) else rec
            if str(lid) in lines:serving.append(str(lid))
        if not serving:continue
        wait=st.get('waiting') or {}
        pax=sum(float(v) for k,v in wait.items() if 'PASSENGER' in str(k).upper() and isinstance(v,(int,float)))
        sid=str(st.get('id'))
        if sid=='None':continue
        all_stations[sid]={'id':st.get('id'),'name':st.get('name'),'town':st.get('town'),
                           'waiting':round(pax,2),'lineIds':sorted(set(serving))}
    return lines,all_stations

def tool_passenger_history_record_v84(args):
    snap,sw,sa=load_snapshot()
    stations,tw,ta=load_kind('stations')
    meta=snap.get('meta') or {}
    game_time=meta.get('gameTime')
    if not isinstance(game_time,(int,float)):
        return {'status':'game_time_unavailable','recorded':False,'reason':'Game snapshot has no numeric meta.gameTime; cannot deduplicate paused reads'}
    if sw or tw:
        return {'status':'stale_export_not_recorded','recorded':False,'warnings':[x for x in (sw,tw) if x],
                'reason':'A live game response is required for historical recording'}
    identity,label,fingerprint,explicit=_v84_identity(snap,args)
    path=_v84_history_file(identity)
    history=_v84_read(path)
    samples=history['samples']
    gt=float(game_time)
    if samples and gt==float(samples[-1]['gameTime']):
        return {'status':'duplicate_game_time_skipped','recorded':False,'saveKey':label,'gameTime':gt,
                'sampleCount':len(samples),'historyPath':str(path)}
    if samples and gt<float(samples[-1]['gameTime']):
        return {'status':'game_time_rollback_not_recorded','recorded':False,'saveKey':label,'gameTime':gt,
                'lastRecordedGameTime':samples[-1]['gameTime'],
                'reason':'Loaded an earlier time or different save using same identity. Provide a different save_key to isolate history.',
                'historyPath':str(path)}
    year=meta.get('yearTicks')
    min_days=float(args.get('min_game_days',0) or 0)
    if min_days<0:raise ValueError('min_game_days must be >= 0')
    if min_days and samples and isinstance(year,(int,float)) and year>0 and gt-float(samples[-1]['gameTime'])<year*min_days/365.0:
        return {'status':'minimum_game_interval_not_reached','recorded':False,'saveKey':label,
                'gameTime':gt,'sampleCount':len(samples),'minimumGameDays':min_days,'historyPath':str(path)}
    lines,st=_v84_extract(snap,stations)
    sample={'gameTime':gt,'date':meta.get('date'),'recordedAtUnix':round(time.time(),3),
            'lines':lines,'stations':st}
    samples.append(sample)
    max_samples=max(2,min(500,int(args.get('max_samples',120))))
    history.update({'schema':1,'label':label,'fingerprint':fingerprint,'explicitSaveKey':explicit,
                    'samples':samples[-max_samples:]})
    _v84_write(path,history)
    return {'status':'recorded','recorded':True,'saveKey':label,'fingerprint':fingerprint,
            'explicitSaveKey':explicit,'gameTime':gt,'sampleCount':len(history['samples']),
            'passengerLines':len(lines),'passengerStations':len(st),'historyPath':str(path),
            'caution':'Auto fingerprint cannot guarantee separate histories for separate saves on the same map. Supply save_key to distinguish playthroughs. Recording is manual; time must advance in-game.'}

def tool_passenger_history_report_v84(args):
    snap,sw,sa=load_snapshot()
    identity,label,fingerprint,explicit=_v84_identity(snap,args)
    history=_v84_read(_v84_history_file(identity))
    samples=history['samples']
    town=str(args.get('town') or '').strip().casefold()
    min_wait=float(args.get('min_station_waiting',20))
    line_filter=str(args.get('line_contains') or '').strip().casefold()
    limit=max(1,min(100,int(args.get('limit',20))))
    series={}
    for sample in samples:
        for lid,line in (sample.get('lines') or {}).items():
            if line_filter and line_filter not in str(line.get('name') or '').casefold():continue
            associated=[st for st in (sample.get('stations') or {}).values() if lid in st.get('lineIds',[]) and
                        (not town or town in str(st.get('town') or '').casefold())]
            if town and not associated:continue
            obj=series.setdefault(lid,{'lineId':line.get('id'),'line':line.get('name'),'observations':[]})
            obj['observations'].append({'gameTime':sample['gameTime'],'usagePct':line.get('usagePct'),
                'busySharedStations':sum(1 for st in associated if st.get('waiting',0)>=min_wait),
                'maxSharedStationQueue':max([st.get('waiting',0) for st in associated],default=0),
                'vehicles':line.get('vehicleCount'),'profit12m':line.get('profit12m')})
    rows=[]
    for obj in series.values():
        obs=obj.pop('observations')
        vals=[o['usagePct'] for o in obs if isinstance(o.get('usagePct'),(int,float))]
        high=sum(1 for o in obs if isinstance(o.get('usagePct'),(int,float)) and o['usagePct']>=95 and o['busySharedStations']>0)
        if len(obs)<3: status='insufficient_distinct_game_times'
        elif high>=3 and high/len(obs)>=0.6: status='repeated_full_line_and_busy_shared_stops'
        elif vals and max(vals)<60: status='consistently_below_60pct_in_samples'
        else: status='mixed_or_unconfirmed'
        obj.update({'samplesObserved':len(obs),'firstGameTime':obs[0]['gameTime'],
            'lastGameTime':obs[-1]['gameTime'],'meanUtilizationPct':round(sum(vals)/len(vals),1) if vals else None,
            'minUtilizationPct':min(vals) if vals else None,'maxUtilizationPct':max(vals) if vals else None,
            'observationsFullAndBusySharedStops':high,'maxSharedStationQueueObserved':max(o['maxSharedStationQueue'] for o in obs),
            'trendStatus':status,'lastVehicles':obs[-1]['vehicles']})
        rows.append(obj)
    order={'repeated_full_line_and_busy_shared_stops':0,'mixed_or_unconfirmed':1,'insufficient_distinct_game_times':2,'consistently_below_60pct_in_samples':3}
    rows.sort(key=lambda r:(order[r['trendStatus']],-r['maxSharedStationQueueObserved'],str(r['line'])))
    return {'scope':'game_time_passenger_congestion_history','version':'8.4-dev','saveKey':label,
        'storedSnapshots':len(samples),'firstGameTime':samples[0]['gameTime'] if samples else None,
        'lastGameTime':samples[-1]['gameTime'] if samples else None,'returnedLines':min(limit,len(rows)),
        'lines':rows[:limit],'warnings':[sw] if sw else [],
        'interpretation':'Multiple distinct game times are needed. Busy station queues are shared, never attributed to a line. 3+ observations are only preliminary persistence evidence.',
        'caution':'Auto fingerprint is not a guaranteed unique save ID. Use explicit save_key for independent save histories. History recording is manual.'}

TOOLS.append({'name':'tf3_passenger_history_record_v84',
 'description':'v8.4 DEV manually record passenger lines and station queues at a new simulation gameTime. Never records stale or paused duplicate snapshots. Persists local per-save history.',
 'inputSchema':{'type':'object','properties':{'save_key':{'type':'string'},'min_game_days':{'type':'number','default':0},'max_samples':{'type':'integer','default':120}}},
 'handler':tool_passenger_history_record_v84})
TOOLS.append({'name':'tf3_passenger_history_report_v84',
 'description':'v8.4 DEV analyze repeated passenger utilization and shared-station queues from recorded snapshots; no automatic purchase decisions.',
 'inputSchema':{'type':'object','properties':{'save_key':{'type':'string'},'town':{'type':'string'},'line_contains':{'type':'string'},'min_station_waiting':{'type':'number','default':20},'limit':{'type':'integer','default':20}}},
 'handler':tool_passenger_history_report_v84})


# v8.5 DEV: historical station persistence and history-aware investment advisories.
# Reads the v8.4 history schema directly, without modifying recorded snapshots.
def _v85_history(args):
    snap, warning, age = load_snapshot()
    identity, label, fingerprint, explicit = _v84_identity(snap,args)
    data = _v84_read(_v84_history_file(identity))
    return data.get('samples') or [], label, [warning] if warning else []

def _v85_station_rows(samples, town='', min_wait=20, limit=20):
    town = str(town or '').casefold()
    joined={}
    for sample in samples:
        for sid, st in (sample.get('stations') or {}).items():
            if town and town not in str(st.get('town') or '').casefold():continue
            key=str(sid)
            row=joined.setdefault(key, {'stationGroupId':st.get('id'),'station':st.get('name'),'town':st.get('town'),'points':[]})
            try: waiting=float(st.get('waiting') or 0)
            except (ValueError,TypeError):continue
            row['points'].append({'gameTime':sample.get('gameTime'),'waiting':waiting,'servingLineIds':st.get('lineIds') or []})
    rows=[]
    for row in joined.values():
        points=row.pop('points')
        q=[p['waiting'] for p in points]
        busy=sum(v>=min_wait for v in q)
        valid=len(points)
        fraction=round(busy/valid,3) if valid else 0
        if valid<3: grade='insufficient_history'
        elif busy>=3 and fraction>=0.6: grade='persistent_busy_station'
        elif busy==0: grade='no_busy_queue_in_samples'
        else: grade='intermittent_or_unconfirmed'
        row.update({'samplesObserved':valid,'busyObservations':busy,'busyFraction':fraction,
                    'meanWaiting':round(sum(q)/valid,1) if valid else None,
                    'minWaiting':min(q) if q else None,'maxWaiting':max(q) if q else None,
                    'lastWaiting':q[-1] if q else None,
                    'trendStatus':grade,'queueThreshold':min_wait,
                    'servingLineIdsLastSnapshot':points[-1]['servingLineIds'] if points else [],
                    'firstGameTime':points[0]['gameTime'] if points else None,
                    'lastGameTime':points[-1]['gameTime'] if points else None})
        rows.append(row)
    priority={'persistent_busy_station':0,'intermittent_or_unconfirmed':1,
              'insufficient_history':2,'no_busy_queue_in_samples':3}
    rows.sort(key=lambda r:(priority[r['trendStatus']],-r['meanWaiting'],str(r['station'])))
    return rows[:limit],len(rows)

def tool_passenger_station_history_v85(args):
    samples,label,warnings=_v85_history(args)
    threshold=float(args.get('min_station_waiting',20))
    if threshold<0:raise ValueError('min_station_waiting must be >= 0')
    limit=max(1,min(150,int(args.get('limit',20))))
    rows,total=_v85_station_rows(samples,args.get('town'),threshold,limit)
    return {'scope':'historical_shared_station_queue_persistence','version':'8.5-dev',
            'saveKey':label,'storedSnapshots':len(samples),'stationsEvaluated':total,
            'returnedStations':len(rows),'stations':rows,'warnings':warnings,
            'interpretation':'Persistent means >=3 busy observations and >=60% of observed samples at that station. Queues are shared across serving lines and are NOT line-specific demand. Three observations are preliminary.',
            'historyReadOnly':True}

def _v85_line_history_index(samples,town=None,min_wait=20):
    history={}
    filter_town=str(town or '').casefold()
    for sample in samples:
        stlist=list((sample.get('stations') or {}).values())
        for lid,line in (sample.get('lines') or {}).items():
            joined=[st for st in stlist if str(lid) in (st.get('lineIds') or []) and
                    (not filter_town or filter_town in str(st.get('town') or '').casefold())]
            if filter_town and not joined:continue
            try:usage=float(line.get('usagePct'))
            except (ValueError,TypeError):usage=None
            row=history.setdefault(str(lid),[])
            row.append({'usage':usage,'busyStations':sum(1 for st in joined if float(st.get('waiting') or 0)>=min_wait),
                        'maxQueue':max((float(st.get('waiting') or 0) for st in joined),default=0)})
    result={}
    for lid,pts in history.items():
        valid=[p['usage'] for p in pts if p['usage'] is not None]
        full_busy=sum(p['usage'] is not None and p['usage']>=95 and p['busyStations']>0 for p in pts)
        full=sum(p['usage'] is not None and p['usage']>=95 for p in pts)
        if len(pts)<3:grade='insufficient_history'
        elif full_busy>=3 and full_busy/len(pts)>=0.6:grade='persistent_full_and_busy_shared_stations'
        elif full>=3 and full/len(pts)>=0.6:grade='repeated_high_utilization_unattributed_queue'
        elif valid and max(valid)<60:grade='consistently_below_60pct'
        else:grade='mixed_or_unconfirmed'
        result[lid]={'historyGrade':grade,'samplesObserved':len(pts),
                     'fullObservations':full,'fullAndBusySharedObservations':full_busy,
                     'meanUtilizationPct':round(sum(valid)/len(valid),1) if valid else None,
                     'maxSharedQueueObserved':max(p['maxQueue'] for p in pts)}
    return result

def tool_network_investment_advisor_v85(args):
    report=tool_network_investment_advisor_v83(args)
    samples,label,warnings=_v85_history(args)
    min_wait=float(args.get('min_station_waiting',20))
    town_data={}
    line_data={}
    for rec in report.get('recommendations') or []:
        if rec.get('category')!='passenger':continue
        town=rec.get('town') or ''
        if town not in town_data:
            station_rows,_=_v85_station_rows(samples,town,min_wait,200)
            town_data[town]=station_rows
            line_data[town]=_v85_line_history_index(samples,town,min_wait)
        persistent=[r for r in town_data[town] if r['trendStatus']=='persistent_busy_station']
        rec['historicalCongestion']={'saveKey':label,'recordedSnapshots':len(samples),
             'persistentBusyStationCount':len(persistent),
             'persistentBusyStations':[{'station':st['station'],'meanWaiting':st['meanWaiting'],
                'busyObservations':st['busyObservations'],'samplesObserved':st['samplesObserved']} for st in persistent[:5]]}
        grades=[]
        for ln in rec.get('capacityReviewLines') or []:
            info=line_data[town].get(str(ln.get('lineId')))
            if info:
                ln['historicalEvidence']=info
                grades.append(info['historyGrade'])
                if info['historyGrade']=='persistent_full_and_busy_shared_stations':
                    ln['historicalNextAction']='Prioritize a capacity review; verify vehicle limits and directional left-behind counts before purchase'
                elif info['historyGrade']=='repeated_high_utilization_unattributed_queue':
                    ln['historicalNextAction']='Inspect repeated high loads; confirm where demand accumulates before increasing capacity'
                elif info['historyGrade']=='consistently_below_60pct':
                    ln['historicalNextAction']='No capacity addition supported by recorded utilization; investigate connections or schedules instead'
                else:
                    ln['historicalNextAction']='Keep monitoring; current snapshots do not establish chronic line congestion'
            else:
                ln['historicalEvidence']={'historyGrade':'not_in_recorded_history','samplesObserved':0}
        if len(samples)<3: rec['historicalAssessment']='insufficient_recorded_snapshots'
        elif 'persistent_full_and_busy_shared_stations' in grades:
            rec['historicalAssessment']='persistent_capacity_investigation_priority'
            rec['suggestedNextAction']='Investigate repeatedly full passenger lines and persistent shared station queues; validate directional need before adding capacity'
        elif persistent:
            rec['historicalAssessment']='persistent_station_queues_without_confirmed_line_capacity_shortage'
            rec['suggestedNextAction']='Investigate connections, transfer waits and line direction at persistently busy stations; no fleet expansion yet'
        else:
            rec['historicalAssessment']='no_persistent_station_congestion_confirmed'
            rec['suggestedNextAction']='Monitor and inspect service patterns; observed history does not justify a capacity increase yet'
    report['version']='8.5-dev'
    report['historySaveKey']=label
    report['historySnapshotsUsed']=len(samples)
    report['historyWarnings']=warnings
    report['historicalRanking']='Balanced cargo/passenger ordering preserved; historical evidence changes passenger urgency and recommended investigations, not ROI ranking'
    report['knownLimitations']=(report.get('knownLimitations') or [])+[
        'History may be sparse, non-uniformly spaced, or manually sampled',
        'Historical shared-station queues are not attributable to a particular serving line',
        'No confirmed incremental vehicle costs or purchase ROI']
    return report

TOOLS.append({'name':'tf3_passenger_station_history_v85',
 'description':'v8.5 DEV report station-level repeated passenger queues using previously recorded v8.4 history; no changes to history files.',
 'inputSchema':{'type':'object','properties':{'save_key':{'type':'string'},'town':{'type':'string'},
 'min_station_waiting':{'type':'number','default':20},'limit':{'type':'integer','default':20}}},
 'handler':tool_passenger_station_history_v85})
TOOLS.append({'name':'tf3_network_investment_advisor_v85',
 'description':'v8.5 DEV balanced cargo/passenger investment advisor enriched with multi-snapshot station queue and line utilization evidence; not ROI-ranked.',
 'inputSchema':{'type':'object','properties':{'save_key':{'type':'string'},'limit':{'type':'integer','default':8},
 'town_contains':{'type':'string'},'cargo_scan_limit':{'type':'integer','default':40},
 'passenger_scan_limit':{'type':'integer','default':100},
 'factories_per_need':{'type':'integer','default':3},'input_sources_per_factory':{'type':'integer','default':2},
 'max_access_km':{'type':'number','default':1.0},'max_lines_per_leg':{'type':'integer','default':3},
 'min_waiting':{'type':'integer','default':50},'min_station_waiting':{'type':'number','default':20}}},
 'handler':tool_network_investment_advisor_v85})


# v8.6 DEV: historical passenger service-pattern investigation, avoiding inferred passenger destinations.
def _v86_service_diagnosis(town, args, samples=None, current=None):
    threshold=float(args.get('min_station_waiting',20))
    if threshold<0:raise ValueError('min_station_waiting must be >= 0')
    if samples is None:
        samples,label,warnings=_v85_history(args)
    else:
        label=str(args.get('save_key') or 'automatic');warnings=[]
    stationrows,_=_v85_station_rows(samples,town,threshold,2000)
    persistent=[st for st in stationrows if st['trendStatus']=='persistent_busy_station']
    linehist=_v85_line_history_index(samples,town,threshold)
    if current is None:
        current=tool_passenger_line_diagnostic_v82({'town':town,'limit':150})
    stations=current.get('stations') or []
    current_by_id={str(st.get('stationGroupId')):st for st in stations}
    findings=[]
    for st in persistent:
        cur=current_by_id.get(str(st.get('stationGroupId'))) or {}
        sl=cur.get('lines') or []
        annotated=[]
        for line in sl:
            lid=str(line.get('lineId'))
            hist=linehist.get(lid) or {}
            annotated.append({'lineId':line.get('lineId'),'line':line.get('line'),'vehicles':line.get('vehicles'),
                'intervalSec':line.get('intervalSec'),'usagePct':line.get('usagePct'),
                'historyGrade':hist.get('historyGrade','history_unavailable'),
                'meanUtilizationPct':hist.get('meanUtilizationPct')})
        persistent_capacity=any(ln.get('historyGrade') in ('persistent_full_and_busy_shared_stations','repeated_high_utilization_unattributed_queue') for ln in annotated)
        if len(samples)<3 or st['samplesObserved']<3: action='monitor_insufficient_history'
        elif persistent_capacity:action='capacity_review_with_service_validation'
        else:action='service_pattern_review_before_capacity'
        intervals=[v['intervalSec'] for v in annotated if isinstance(v.get('intervalSec'),(int,float)) and v['intervalSec']>0]
        findings.append({'station':st['station'],'stationGroupId':st['stationGroupId'],
            'meanWaiting':st['meanWaiting'],'busyObservations':st['busyObservations'],
            'samplesObserved':st['samplesObserved'],'busyFraction':st['busyFraction'],
            'currentSharedWaiting':cur.get('waitingPassengersSnapshot'),
            'servingLines':annotated,'lineCount':len(annotated),
            'reportedIntervalSecRange':[min(intervals),max(intervals)] if intervals else None,
            'investigation':action,
            'nextCheck':('Check repeated directional vehicle loads and left-behind passengers before changing fleet.' if persistent_capacity else
              'Inspect intended destinations, transfer access, route direction, and scheduled arrivals; do not add vehicles from shared queues alone.'),
            'queueAttribution':'shared_station_not_line_specific'})
    findings.sort(key=lambda r:-r['meanWaiting'])
    return {'scope':'historical_passenger_service_pattern_investigation','version':'8.6-dev',
       'town':town,'saveKey':label,'storedSnapshots':len(samples),'persistentStationsFound':len(findings),
       'stations':findings[:max(1,min(150,int(args.get('limit',15))))],
       'servicePatternReviewCount':sum(r['investigation']=='service_pattern_review_before_capacity' for r in findings),
       'capacityReviewCount':sum(r['investigation']=='capacity_review_with_service_validation' for r in findings),
       'warnings':warnings,
       'caveats':['Shared station queues do not identify intended boarding line, direction, journey or transfer.',
         'Line intervals are service-level reported estimates, not actual departures at this stop.',
         'Current line details and stored historical samples may be from different game times.',
         'No route geometry, guaranteed timetable connection, vehicle capacity or ROI is inferred.']}

def tool_passenger_service_diagnosis_v86(args):
    town=str(args.get('town') or '').strip()
    if not town:raise ValueError('town is required')
    return _v86_service_diagnosis(town,args)

def tool_network_investment_advisor_v861(args):
    """Enrich the combined shortlist with complete passenger-line observations.

    v8.6 supplied busyStations whose line array was named servingLines; the
    standalone diagnosis expects lines. It silently misread all serving lines.
    Use the existing complete v8.2 station diagnostic for each town instead.
    """
    report=tool_network_investment_advisor_v85(args)
    samples,label,warnings=_v85_history(args)
    town_cache={}
    for rec in report.get('recommendations') or []:
        if rec.get('category')!='passenger':continue
        town=rec.get('town') or ''
        if town not in town_cache:
            current=tool_passenger_line_diagnostic_v82({'town':town,'limit':150})
            diag=_v86_service_diagnosis(town,args,samples,current=current)
            town_cache[town]=diag
        diag=town_cache[town]
        rec['servicePatternDiagnosis']={'persistentStationsFound':diag['persistentStationsFound'],
              'capacityReviewCount':diag['capacityReviewCount'],
              'servicePatternReviewCount':diag['servicePatternReviewCount'],
              'priorityStations':diag['stations'][:3]}
        historic_capacity=(rec.get('historicalAssessment')=='persistent_capacity_investigation_priority')
        diag_capacity=diag['capacityReviewCount']>0
        # Failing loudly is safer than emitting contradictory passenger advice.
        if historic_capacity and not diag_capacity:
            raise RuntimeError('Historical passenger capacity and service-pattern classifications disagree for '+town)
        if diag_capacity:
            rec['suggestedNextAction']='Review repeatedly full lines and busy station patterns; verify directional left-behind passengers and vehicle capacity before buying'
        elif diag['servicePatternReviewCount']:
            rec['suggestedNextAction']='Investigate persistent shared queues, departure directions, and transfer patterns before adding vehicles'
    report['version']=SERVER_VERSION
    report['passengerPriorityPolicy']='Historical shared-queue magnitude prioritizes station checks. Existing balanced shortlist is not ROI-ranked.'
    report['knownLimitations']=(report.get('knownLimitations') or [])+[
        'No individual passenger origins/destinations or stop-level line-specific boarding demand',
        'Service-pattern hypotheses are investigations, not verified transfer faults']
    return report

TOOLS.append({'name':'tf3_passenger_service_diagnosis_v86',
 'description':'v8.6 DEV diagnose persistent shared station queues versus historical line fullness and reported service intervals; no passenger destination inference.',
 'inputSchema':{'type':'object','properties':{'save_key':{'type':'string'},'town':{'type':'string'},
 'min_station_waiting':{'type':'number','default':20},'limit':{'type':'integer','default':15}},'required':['town']},
 'handler':tool_passenger_service_diagnosis_v86})
TOOLS.append({'name':'tf3_network_investment_advisor_v861',
 'description':'2.0.0-dev.1 corrected balanced advisor with full passenger line data, consistent capacity and service-pattern grades.',
 'inputSchema':{'type':'object','properties':{'save_key':{'type':'string'},'limit':{'type':'integer','default':8},
 'town_contains':{'type':'string'},'cargo_scan_limit':{'type':'integer','default':40},
 'passenger_scan_limit':{'type':'integer','default':100},'factories_per_need':{'type':'integer','default':3},
 'input_sources_per_factory':{'type':'integer','default':2},'max_access_km':{'type':'number','default':1.0},
 'max_lines_per_leg':{'type':'integer','default':3},'min_waiting':{'type':'integer','default':50},
 'min_station_waiting':{'type':'number','default':20}}},'handler':tool_network_investment_advisor_v861})

for tool in TOOLS:
    schema = tool["inputSchema"]
    if "save_key" in schema.get("properties", {}):
        schema["required"] = list(dict.fromkeys(schema.get("required", []) + ["save_key"]))
        schema["properties"]["save_key"].update({"minLength": 1, "maxLength": 96,
            "description": "Unique key for this independent save/playthrough; reuse it for later reads."})

HANDLERS = {t["name"]: t["handler"] for t in TOOLS}

# ----------------------------------------------------------------------------------------------------------------
# MCP over stdio (newline-delimited JSON-RPC 2.0)


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Read-only data from a running Transport Fever 3 game (MCP Bridge mod). Start with "
                            "tf3_status; money values are in game currency; times are game time. "
                            "Passenger history recording writes local telemetry files; use a unique save_key per playthrough.",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            raise LookupError(f"unknown tool {name}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return {"content": [{"type": "text", "text": "Error: arguments must be an object"}], "isError": True}
        try:
            result = handler(arguments)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}]}
        except Exception as e:  # tool errors go back to the model, not as protocol errors
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    if rid is None:
        return None  # notification (e.g. notifications/initialized)
    raise LookupError(f"method not found: {method}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            continue
        rid = req.get("id")
        if "params" in req and not isinstance(req["params"], dict):
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "params must be an object"}})
            continue
        try:
            result = handle(req)
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "result": result})
        except LookupError as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": str(e)}})
        except Exception as e:
            if rid is not None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})


# ----------------------------------------------------------------------------------------------------------------
# --setup: registers this server with the Claude desktop app and Claude Code


def desktop_configs():
    """Claude desktop config files: the regular install and the Microsoft Store (MSIX) one, where present."""
    found = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                                        "claude_desktop_config.json"))
    if sys.platform == "darwin":
        found.append(os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json"))
    return [p for p in found if os.path.isdir(os.path.dirname(p))]


def setup():
    import shutil
    import subprocess
    script = os.path.abspath(__file__)
    entry = {"command": sys.executable, "args": ["-I", script]}
    print("Server:", script)
    print("Python:", sys.executable)
    done = False
    for path in desktop_configs():
        config = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    config = json.load(f)
            except ValueError:
                print("Claude desktop: could not read", path, "(not valid JSON); add the entry below by hand")
                continue
            backup = path + ".bak-tf3"
            if not os.path.exists(backup):  # keep the config from before the first setup
                shutil.copyfile(path, backup)
        config.setdefault("mcpServers", {})["tf3"] = entry
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print("Claude desktop: added 'tf3' to", path, "(backup: .bak-tf3). Quit Claude from the tray and reopen it.")
        done = True
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "-s", "user", "tf3"], capture_output=True)
        r = subprocess.run([claude, "mcp", "add", "-s", "user", "tf3", "--", sys.executable, "-I", script],
                           capture_output=True, text=True)
        print("Claude Code:", "added 'tf3' for all folders" if r.returncode == 0 else "failed: " + r.stderr.strip())
        done = done or r.returncode == 0
    if not done:
        print("No Claude app found. Add this to your MCP client's config under \"mcpServers\":")
        print(json.dumps({"tf3": entry}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        setup()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--print":  # quick look without an MCP client
        tool = sys.argv[2] if len(sys.argv) > 2 else "tf3_status"
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(HANDLERS[tool](args), ensure_ascii=False, indent=1))
    else:
        main()
