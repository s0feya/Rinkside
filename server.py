#!/usr/bin/env python3
"""
Rinkside — a personal NHL / PWHL / IIHF / Olympic hockey schedule tracker.

Pure Python 3 standard library, no installs needed:
    python3 server.py            # then open http://localhost:8765

Live data sources (refreshed automatically in the background):
  * NHL   – api-web.nhle.com (official NHL web API), search.d3.nhle.com for players
  * PWHL  – thepwhl.com/en/schedule (schedule JSON embedded in the official page)
  * IIHF  – stats.iihf.com/hydra/<id>/ (official IIHF stats & schedule pages,
            includes World Juniors, Worlds, Women's Worlds, U18s, Olympic tournaments)
  * PWHL player -> team / nation lookups via Wikipedia infoboxes

Calendar feeds are served at /cal.ics?f=<follow keys> so Apple Calendar can
subscribe (webcal://) and keep refreshing on its own.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

UA = "Rinkside/1.0 (personal non-commercial hockey schedule app)"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

NHL_TTL = 5 * 60          # NHL team schedules / scores
PWHL_TTL = 15 * 60        # whole PWHL season page
IIHF_TTL = 15 * 60        # each IIHF tournament
DISCOVERY_TTL = 7 * 86400 # rescan IIHF tournament ids weekly
PLAYER_TTL = 12 * 3600    # player -> team resolution
PAST_DAYS = 14            # how far back to keep finished games in feeds

STATUS = {}               # source -> {ok, at, error}
_lock = threading.RLock()


# ----------------------------------------------------------------- utilities
def http_get(url, ua=UA, timeout=20, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last


def http_json(url, ua=UA):
    return json.loads(http_get(url, ua=ua))


def mark(source, ok, error=None):
    with _lock:
        prev = STATUS.get(source, {})
        STATUS[source] = {
            "ok": ok,
            "at": time.time() if ok else prev.get("at"),
            "checked": time.time(),
            "error": None if ok else str(error)[:200],
        }


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")


def save_json(name, obj):
    tmp = os.path.join(DATA_DIR, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, os.path.join(DATA_DIR, name))


def load_json(name, default):
    try:
        with open(os.path.join(DATA_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return default


class TTLCache:
    def __init__(self):
        self.d = {}
        self.lock = threading.Lock()

    def get(self, key, ttl, fn):
        with self.lock:
            hit = self.d.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
            with self.lock:
                self.d[key] = (time.time(), val)
            return val
        except Exception:
            if hit:  # serve stale on failure
                return hit[1]
            raise


CACHE = TTLCache()

# ------------------------------------------------------------- nation codes
NATIONS = {
    "CAN": ("Canada", "CA"), "USA": ("United States", "US"), "SWE": ("Sweden", "SE"),
    "FIN": ("Finland", "FI"), "CZE": ("Czechia", "CZ"), "SVK": ("Slovakia", "SK"),
    "SUI": ("Switzerland", "CH"), "GER": ("Germany", "DE"), "LAT": ("Latvia", "LV"),
    "NOR": ("Norway", "NO"), "DEN": ("Denmark", "DK"), "AUT": ("Austria", "AT"),
    "FRA": ("France", "FR"), "KAZ": ("Kazakhstan", "KZ"), "SLO": ("Slovenia", "SI"),
    "HUN": ("Hungary", "HU"), "ITA": ("Italy", "IT"), "GBR": ("Great Britain", "GB"),
    "POL": ("Poland", "PL"), "JPN": ("Japan", "JP"), "CHN": ("China", "CN"),
    "KOR": ("Korea", "KR"), "UKR": ("Ukraine", "UA"), "RUS": ("Russia", "RU"),
    "BLR": ("Belarus", "BY"), "NED": ("Netherlands", "NL"), "EST": ("Estonia", "EE"),
    "LTU": ("Lithuania", "LT"), "ROU": ("Romania", "RO"), "ESP": ("Spain", "ES"),
    "CRO": ("Croatia", "HR"), "SRB": ("Serbia", "RS"), "AUS": ("Australia", "AU"),
}
# NHL birthCountry (ISO-3) -> IIHF code
ISO3_TO_IIHF = {"CHE": "SUI", "DEU": "GER", "LVA": "LAT", "DNK": "DEN", "SVN": "SLO",
                "NLD": "NED", "HRV": "CRO", "GRC": "GRE"}
NAME_TO_IIHF = {v[0].lower(): k for k, v in NATIONS.items()}
NAME_TO_IIHF.update({"czech republic": "CZE", "united states of america": "USA",
                     "usa": "USA", "great britain": "GBR", "south korea": "KOR",
                     "cz": "CZE", "swi": "SUI", "sui": "SUI", "us": "USA", "ca": "CAN",
                     "se": "SWE", "fi": "FIN", "de": "GER", "ch": "SUI"})

LEVELS = {"M": "Men", "W": "Women", "U20": "Men's U20 (World Juniors)",
          "U18": "Men's U18", "U18W": "Women's U18"}


# --------------------------------------------------------------------- NHL
NHL_FALLBACK = {
    "ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes", "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets", "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montréal Canadiens",
    "NSH": "Nashville Predators", "NJD": "New Jersey Devils", "NYI": "New York Islanders",
    "NYR": "New York Rangers", "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins", "SJS": "San Jose Sharks", "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
}


def nhl_teams():
    def load():
        try:
            data = http_json("https://api-web.nhle.com/v1/standings/now")
            teams = []
            for t in data.get("standings", []):
                ab = t["teamAbbrev"]["default"]
                teams.append({"key": "team:NHL:" + ab, "code": ab,
                              "name": t["teamName"]["default"],
                              "logo": "https://assets.nhle.com/logos/nhl/svg/%s_light.svg" % ab,
                              "group": t.get("divisionName", "")})
            if len(teams) >= 30:
                return sorted(teams, key=lambda x: x["name"])
        except Exception:
            pass
        return [{"key": "team:NHL:" + k, "code": k, "name": v, "group": "",
                 "logo": "https://assets.nhle.com/logos/nhl/svg/%s_light.svg" % k}
                for k, v in sorted(NHL_FALLBACK.items(), key=lambda kv: kv[1])]
    return CACHE.get("nhl_teams", 86400, load)


def _nhl_team_obj(t):
    name = (t.get("placeName", {}).get("default", "") + " " + t.get("commonName", {}).get("default", "")).strip()
    return {"code": t.get("abbrev"), "name": name or t.get("abbrev"),
            "logo": t.get("logo")}


NHL_STATE = {"FUT": "scheduled", "PRE": "pregame", "LIVE": "live", "CRIT": "live",
             "FINAL": "final", "OFF": "final"}


def nhl_team_schedule(abbr):
    def load():
        try:
            data = http_json("https://api-web.nhle.com/v1/club-schedule-season/%s/now" % abbr)
            mark("NHL", True)
        except Exception as e:
            mark("NHL", False, e)
            raise
        out = []
        for g in data.get("games", []):
            gt = {1: "Preseason", 2: "Regular season", 3: "Playoffs", 4: "All-Star"}.get(g.get("gameType"), "")
            away, home = _nhl_team_obj(g["awayTeam"]), _nhl_team_obj(g["homeTeam"])
            ev = {
                "id": "nhl-%s" % g["id"], "league": "NHL", "comp": "NHL " + gt,
                "start": g["startTimeUTC"], "tbd": g.get("gameScheduleState") == "TBD",
                "away": away, "home": home, "sep": "@",
                "venue": g.get("venue", {}).get("default", ""),
                "status": NHL_STATE.get(g.get("gameState"), "scheduled"),
                "teams": ["NHL:" + away["code"], "NHL:" + home["code"]],
                "url": "https://www.nhl.com/gamecenter/%s" % g["id"],
            }
            if g.get("gameScheduleState") in ("PPD", "CNCL"):
                ev["status"] = "postponed" if g["gameScheduleState"] == "PPD" else "cancelled"
            if "score" in g["awayTeam"]:
                ev["score"] = [g["awayTeam"].get("score"), g["homeTeam"].get("score")]
                lp = (g.get("gameOutcome") or {}).get("lastPeriodType")
                if lp in ("OT", "SO"):
                    ev["note"] = lp
            ev["broadcasts"] = [{"n": (b.get("network") or "").strip(), "c": b.get("countryCode"),
                                 "m": b.get("market")}
                                for b in g.get("tvBroadcasts", []) if b.get("network")]
            out.append(ev)
        return out
    return CACHE.get("nhl:" + abbr, NHL_TTL, load)


def nhl_player_search(q):
    url = ("https://search.d3.nhle.com/api/v1/search/player?culture=en-us&limit=25&active=true&q="
           + urllib.parse.quote(q))
    res = []
    for p in http_json(url):
        team = p.get("teamAbbrev")
        if team:
            detail = "%s · #%s · %s" % (team, p.get("sweaterNumber") or "–", p.get("positionCode", ""))
        else:
            detail = "Prospect / not on an NHL roster · national team games only"
        res.append({"key": "player:NHL:%s" % p["playerId"], "name": p["name"], "league": "NHL",
                    "detail": detail, "hasTeam": bool(team),
                    "img": "https://assets.nhle.com/mugs/nhl/latest/%s.png" % p["playerId"]})
    res.sort(key=lambda r: not r["hasTeam"])  # rostered players first
    return res[:15]


def junior_eligible(birth_date):
    """World Juniors eligibility for the current/next tournament (under 20 on Dec 31)."""
    try:
        by = int(birth_date[:4])
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    wjc_year = now.year + 1 if now.month >= 6 else now.year
    return by >= wjc_year - 20


def nhl_player(pid):
    def load():
        d = http_json("https://api-web.nhle.com/v1/player/%s/landing" % pid)
        c = d.get("birthCountry", "")
        nation = ISO3_TO_IIHF.get(c, c) if c else None
        levels = ["M"] + (["U20"] if junior_eligible(d.get("birthDate", "")) else [])
        return {"name": "%s %s" % (d["firstName"]["default"], d["lastName"]["default"]),
                "team": "NHL:" + d["currentTeamAbbrev"] if d.get("currentTeamAbbrev") else None,
                "nation": nation, "level": "M", "levels": levels,
                "img": d.get("headshot")}
    return CACHE.get("nhlp:" + pid, PLAYER_TTL, load)


# -------------------------------------------------------------------- PWHL
PWHL_ABBR = {"boston-fleet": "BOS", "detroit-clutch": "DET", "hamilton-havoc": "HAM",
             "minnesota-frost": "MIN", "montreal-victoire": "MTL", "new-york-sirens": "NY",
             "ottawa-charge": "OTT", "san-jose-sea-lions": "SJ", "seattle-torrent": "SEA",
             "toronto-sceptres": "TOR", "vancouver-goldeneyes": "VAN", "vegas-vanguards": "VGS"}


def _titlecase(s):
    small = {"of", "the"}
    return " ".join(w if w.lower() in small else w.capitalize() for w in s.lower().split())


def _logo(lst):
    try:
        return lst[0]["secure_url"].replace("\\/", "/")
    except Exception:
        return None


def pwhl_data():
    """Returns {'games': [...], 'teams': {slug: {...}}} scraped from thepwhl.com."""
    def load():
        try:
            html = http_get("https://www.thepwhl.com/en/schedule", ua=BROWSER_UA)
            dec = json.JSONDecoder()
            games, teams, seen = [], {}, set()
            for m in re.finditer(r'\{"schedule_block"', html):
                obj, _ = dec.raw_decode(html, m.start())
                for blk in obj["schedule_block"].get("schedule", []):
                    phase = blk.get("title", "")
                    for g in blk.get("games", []):
                        if not g.get("game_id") or not g.get("date_played") or g["game_id"] in seen:
                            continue
                        if "home_team" not in g or "visiting_team" not in g:
                            continue
                        seen.add(g["game_id"])
                        side = {}
                        for role, k in (("home", "home_team"), ("away", "visiting_team")):
                            t = g[k]
                            nm = _titlecase(t.get(k + "_name", ""))
                            sl = slugify(nm)
                            side[role] = {"code": PWHL_ABBR.get(sl, nm[:3].upper()), "name": nm,
                                          "logo": _logo(t.get(k + "_logo"))}
                            teams[sl] = {"key": "team:PWHL:" + sl, "code": side[role]["code"],
                                         "name": nm, "logo": side[role]["logo"], "group": ""}
                        st = (g.get("game_status") or "").strip()
                        status = "final" if st.lower().startswith("final") else (
                            "live" if re.search(r"\b(1st|2nd|3rd|OT|SO|Intermission|In Progress)\b", st) else "scheduled")
                        ev = {"id": "pwhl-%s" % g["game_id"], "league": "PWHL",
                              "comp": "PWHL " + re.sub(r"^\d{4}[-–]\d{2,4}\s*", "", phase),
                              "start": g["date_played"], "tbd": st.upper() == "TBA",
                              "away": side["away"], "home": side["home"], "sep": "@",
                              "venue": g.get("venue_name", ""), "status": status,
                              "teams": ["PWHL:" + slugify(side["away"]["name"]),
                                        "PWHL:" + slugify(side["home"]["name"])],
                              "url": "https://www.thepwhl.com/en/stats/game-center/%s" % g["game_id"]}
                        bc = []
                        for b in g.get("broadcasters") or []:
                            bt = (b.get("title") or "")
                            if not bt.lower().startswith("broadcaster"):
                                continue
                            nm = re.sub(r"^Broadcaster\s*-\s*", "", bt).strip()
                            link = ((b.get("logo_link") or {}).get("external_link") or "").replace("\\/", "/")
                            bc.append({"n": nm, "c": "US" if nm.upper() in ("ION", "SCRIPPS") else "CA",
                                       "url": link or None})
                        ev["broadcasts"] = bc
                        tm = re.search(r"(\d{4}-\d{2}-\d{2})\s*$", g.get("title", ""))
                        if tm:
                            ev["localDate"] = tm.group(1)
                        if status != "scheduled":
                            ev["score"] = [g["visiting_team"].get("visiting_goal_count"),
                                           g["home_team"].get("home_goal_count")]
                        if status == "live":
                            ev["note"] = st
                        games.append(ev)
            if not games:
                raise RuntimeError("no games found on PWHL schedule page")
            mark("PWHL", True)
            save_json("pwhl.json", {"games": games, "teams": teams})
            return {"games": games, "teams": teams}
        except Exception as e:
            mark("PWHL", False, e)
            disk = load_json("pwhl.json", None)
            if disk:
                return disk
            raise
    return CACHE.get("pwhl", PWHL_TTL, load)


# ---------------------------------------------------------------- Wikipedia
def wiki_api(params):
    params = dict(params, format="json", formatversion="2")
    return http_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params))


def _wiki_link_text(v):
    v = re.sub(r"<ref.*?(</ref>|/>)", "", v or "")
    m = re.search(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", v)
    if m:
        return m.group(1).strip(), (m.group(2) or m.group(1)).strip()
    v = re.sub(r"\{\{.*?\}\}", "", v).strip()
    return v, v


def wiki_infobox(title):
    w = wiki_api({"action": "parse", "page": title, "prop": "wikitext", "section": 0,
                  "redirects": 1})["parse"]["wikitext"]
    fields = {}
    for line in w.splitlines():
        m = re.match(r"\s*\|\s*([a-z_0-9]+)\s*=\s*(.*)$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2)
    return fields


ROSTER_TTL = 24 * 3600
_roster_lock = threading.Lock()


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()).strip()


def _parse_roster(wikitext, slug):
    w = re.sub(r"<!--.*?-->", "", wikitext, flags=re.S)
    i = w.lower().find("current roster")
    if i < 0:
        return []
    sec = w[i:]
    nxt = re.search(r"\n==[^=]", sec[20:])  # stop at the next level-2 heading
    if nxt:
        sec = sec[:nxt.start() + 20]
    players = []
    for m in re.finditer(r"\{\{\s*Ice hockey team player(.*?)\}\}", sec, re.S | re.I):
        f = {}
        for part in m.group(1).split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                f[k.strip()] = re.sub(r"\[\[([^|\]]*\|)?([^\]]*)\]\]", r"\2", v).strip()
        first, last = f.get("first", ""), f.get("last", "")
        if not last:
            continue
        name = ("%s %s" % (first, last)).strip()
        title = name + (" (%s)" % f["dab"] if f.get("dab") else "")
        nat = f.get("nat", "").strip()
        nat = nat.upper() if nat.upper() in NATIONS else NAME_TO_IIHF.get(nat.lower(), nat.upper())
        players.append({"title": title.replace(" ", "_"), "name": name, "team": slug,
                        "num": f.get("num", ""), "pos": f.get("pos", ""), "nat": nat})
    return players


def pwhl_roster(force=False):
    """Every rostered PWHL player, built from the 12 team articles' roster tables (refreshed daily)."""
    with _roster_lock:
        disk = load_json("pwhl_roster.json", {"at": 0, "teams": {}})
        if not force and time.time() - disk.get("at", 0) < ROSTER_TTL and disk.get("teams"):
            return disk
        teams = pwhl_data()["teams"]
        out = dict(disk.get("teams", {}))
        ok = 0
        for slug, t in sorted(teams.items()):
            for attempt in range(2):
                try:
                    w = wiki_api({"action": "parse", "page": t["name"], "prop": "wikitext",
                                  "redirects": 1})["parse"]["wikitext"]
                    ps = _parse_roster(w, slug)
                    if ps:
                        out[slug] = ps
                        ok += 1
                    break
                except Exception as e:
                    if "429" in str(e):
                        time.sleep(15)
                    else:
                        break
            time.sleep(1.2)  # be gentle with Wikipedia
        data = {"at": time.time() if ok else disk.get("at", 0), "teams": out}
        if ok:
            save_json("pwhl_roster.json", data)
            mark("PWHL rosters", True)
        else:
            mark("PWHL rosters", False, "could not load team rosters from Wikipedia")
        return data


def _roster_players():
    data = load_json("pwhl_roster.json", {"teams": {}})
    if not data.get("teams"):
        data = pwhl_roster()
    return [p for ps in data.get("teams", {}).values() for p in ps]


def pwhl_player_search(q):
    words = _norm(q).split()
    teams = pwhl_data()["teams"]
    hits = []
    for p in _roster_players():
        nm = _norm(p["name"])
        if all(w in nm for w in words):
            last = _norm(p["name"].split()[-1])
            score = (0 if any(last.startswith(w) for w in words) else 1, nm)
            hits.append((score, p))
    hits.sort(key=lambda x: x[0])
    out = []
    for _, p in hits[:15]:
        bits = [teams.get(p["team"], {}).get("name", p["team"])]
        if p.get("num"):
            bits.append("#" + p["num"])
        if p.get("pos"):
            bits.append(p["pos"])
        if p.get("nat"):
            bits.append(p["nat"])
        out.append({"key": "player:PWHL:" + p["title"], "name": p["name"], "league": "PWHL",
                    "detail": " · ".join(bits), "img": None})
    return out


def pwhl_player(title):
    def load():
        want = _norm(title.replace("_", " "))
        for p in _roster_players():
            if _norm(p["title"].replace("_", " ")) == want or _norm(p["name"]) == want:
                nat = p.get("nat")
                return {"name": p["name"], "team": "PWHL:" + p["team"],
                        "nation": nat if nat in NATIONS else None, "level": "W", "levels": ["W"], "img": None}
        # not on a current roster table: fall back to the player's own article
        f = wiki_infobox(title)
        team_link, team_text = _wiki_link_text(f.get("team") or f.get("current_team") or "")
        teams = pwhl_data()["teams"]
        team = None
        for cand in (slugify(team_link), slugify(team_text)):
            if cand in teams:
                team = "PWHL:" + cand
        nation = None
        nt_link, nt_text = _wiki_link_text(f.get("ntl_team") or f.get("national_team") or f.get("nationality") or "")
        for txt in (nt_text, nt_link):
            if txt.strip().upper() in NATIONS:
                nation = txt.strip().upper()
                break
            k = txt.lower().replace("women's national ice hockey team", "").replace("national women's ice hockey team", "").strip()
            if k in NAME_TO_IIHF:
                nation = NAME_TO_IIHF[k]
                break
        return {"name": title.replace("_", " ").split(" (")[0], "team": team,
                "nation": nation, "level": "W", "levels": ["W"], "img": None}
    return CACHE.get("pwhlp:" + title, PLAYER_TTL, load)


# -------------------------------------------------------------------- IIHF
SEED_IIHF = [773, 779, 781, 782, 783, 852, 851, 853]


def classify_tournament(title):
    """-> (league, level, short name) or None if not a top-division event."""
    t = title.strip()
    if re.search(r"\b(Div|Division|Group|Qualification|Qualifying|Test|Continental|First Round|Second Round|Third Round|Asia|Oceania|Challenge Cup|Development Cup)\b", t, re.I):
        return None
    m = re.match(r"^(\d{4})\s+(Men|Women)[’']s Tournament$", t)
    if m:  # Olympic tournaments are named this way on stats.iihf.com
        return ("OLY", "W" if m.group(2) == "Women" else "M", "%s Olympics (%s)" % (m.group(1), m.group(2)))
    if "Olympic" in t:
        return ("OLY", "W" if "Women" in t else "M", t)
    if "IIHF" not in t:
        return None
    if re.search(r"World Junior|U20", t):
        lvl, short = "U20", "World Juniors"
    elif "U18" in t and "Women" in t:
        lvl, short = "U18W", "Women's U18 Worlds"
    elif "U18" in t:
        lvl, short = "U18", "U18 Worlds"
    elif "Women" in t:
        lvl, short = "W", "Women's Worlds"
    else:
        lvl, short = "M", "World Championship"
    yr = re.match(r"^(\d{4})", t)
    return ("IIHF", lvl, ("%s %s" % (yr.group(1), short)) if yr else t)


def hydra_parse(tid, html):
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    title = title.group(1).strip() if title else ""
    cls = classify_tournament(title)
    if not cls:
        return title, None, []
    league, level, short = cls
    games = []
    for m in re.finditer(r"<tr>(.*?)</tr>", html, re.S):
        b = m.group(1)
        um = re.search(r'utc="(\d+)"', b)
        if not um:
            continue
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "|", c)).replace("&nbsp;", " ")).strip()
                 for a, c in re.findall(r"<td([^>]*)>(.*?)</td>", b, re.S) if 'width="1"' not in a]
        tbd_teams = False
        if len(cells) >= 8:
            t1, t2 = cells[3], cells[5]
            result, stat = cells[6].split("|")[0].strip(), cells[7]
        elif len(cells) >= 6:  # knockout game, opponents not decided yet
            t1 = t2 = "TBD"
            tbd_teams = True
            result, stat = cells[4].split("|")[0].strip(), cells[5]
        else:
            continue
        loc = [x.strip() for x in cells[1].split("|")]
        num_phase = [x.strip() for x in cells[2].split("|")]
        stat = stat.replace("|", " ").strip().lower()
        start = datetime.fromtimestamp(int(um.group(1)) * 60, tz=timezone.utc)
        status = "final" if ("complete" in stat or "final" in stat) else (
            "live" if ("progress" in stat or "live" in stat or "period" in stat) else "scheduled")
        def team(c):
            return {"code": c, "name": NATIONS.get(c, (c, None))[0],
                    "flag": NATIONS.get(c, (None, None))[1]}
        ev = {"id": "iihf-%s-%s" % (tid, num_phase[0] if num_phase else um.group(1)),
              "league": league, "comp": short, "level": level,
              "phase": num_phase[1] if len(num_phase) > 1 else "",
              "start": iso(start), "tbd": False,
              "away": team(t1), "home": team(t2), "sep": "vs",
              "venue": ", ".join([x for x in reversed(loc) if x]),
              "status": status,
              "teams": ["INTL:%s:%s" % (t1, level), "INTL:%s:%s" % (t2, level)],
              "url": "https://stats.iihf.com/hydra/%s/" % tid}
        if tbd_teams:
            ev["maybe"] = True
            ev["away"] = {"code": "TBD", "name": "TBD", "flag": None}
            ev["home"] = {"code": "TBD", "name": "TBD", "flag": None}
            ev["teams"] = []
        sm = re.match(r"(\d+)\s*-\s*(\d+)", result)
        if sm:
            ev["score"] = [int(sm.group(1)), int(sm.group(2))]
            if "OT" in result or "SO" in result or "GWS" in result:
                ev["note"] = "OT/SO"
        games.append(ev)
    # undecided knockout games: show to followers of every nation in the event
    nations = sorted({t for g in games for t in g["teams"]})
    for g in games:
        if g.get("maybe"):
            g["teams"] = nations
    return title, cls, games


SEED_FILE = os.path.join(ROOT, "iihf_seed.json")


def iihf_registry():
    reg = load_json("iihf_registry.json", None)
    if reg and reg.get("tournaments"):
        return reg
    try:  # fresh deploy: start from the list shipped with the code
        with open(SEED_FILE) as f:
            seed = json.load(f)
        return {"scanned": seed.get("scanned", 0), "tournaments": seed.get("tournaments", {})}
    except Exception:
        return {"scanned": 0, "tournaments": {}}


def iihf_discover(force=False):
    """Scan stats.iihf.com/hydra/<id> ids to find current top-level tournaments."""
    reg = iihf_registry()
    if not force and time.time() - reg.get("scanned", 0) < DISCOVERY_TTL and reg["tournaments"]:
        return
    year = datetime.now(timezone.utc).year
    ids = sorted(set(SEED_IIHF) | set(range(700, 1000)))
    found = dict(reg.get("tournaments", {}))
    misses_after_max = 0
    for tid in ids:
        try:
            html = http_get("https://stats.iihf.com/hydra/%d/" % tid, ua=BROWSER_UA, timeout=10, retries=0)
        except Exception:
            time.sleep(0.2)
            continue
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        title = title.group(1).strip() if title else ""
        ym = re.match(r"^(\d{4})", title)
        if ym and int(ym.group(1)) >= year - 1 and classify_tournament(title):
            found[str(tid)] = title
        time.sleep(0.25)
    reg = {"scanned": time.time(), "tournaments": found}
    save_json("iihf_registry.json", reg)
    mark("IIHF discovery", True)


def iihf_tournament(tid):
    def load():
        html = http_get("https://stats.iihf.com/hydra/%s/" % tid, ua=BROWSER_UA)
        return hydra_parse(tid, html)
    return CACHE.get("iihf:%s" % tid, IIHF_TTL, load)


def iihf_games():
    reg = iihf_registry()
    tids = set(reg["tournaments"].keys()) | {str(x) for x in SEED_IIHF}
    games, events, ok, err = [], [], False, None
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    for tid in sorted(tids, key=int):
        try:
            title, cls, gs = iihf_tournament(tid)
            ok = True
        except Exception as e:
            err = e
            continue
        if not cls or not gs:
            if cls and title:
                events.append({"id": tid, "title": cls[2], "league": cls[0], "level": cls[1], "games": 0})
            continue
        last = max(parse_iso(g["start"]) for g in gs)
        if last < cutoff:
            continue
        games.extend(gs)
        events.append({"id": tid, "title": cls[2], "league": cls[0], "level": cls[1],
                       "games": len(gs), "first": min(g["start"] for g in gs),
                       "last": max(g["start"] for g in gs)})
    mark("IIHF", ok or not tids, err)
    return games, events


def intl_teams():
    games, events = iihf_games()
    seen = {}
    for g in games:
        for side in (g["away"], g["home"]):
            if side["code"] in NATIONS:
                k = "team:INTL:%s:%s" % (side["code"], g["level"])
                seen[k] = {"key": k, "code": side["code"], "name": "%s · %s" % (side["name"], LEVELS[g["level"]]),
                           "flag": side["flag"], "group": LEVELS[g["level"]]}
    # always offer the big hockey nations even before rosters are posted
    for c in ("CAN", "USA", "SWE", "FIN", "CZE", "SUI", "SVK", "GER"):
        for lvl in ("M", "W", "U20"):
            k = "team:INTL:%s:%s" % (c, lvl)
            seen.setdefault(k, {"key": k, "code": c, "name": "%s · %s" % (NATIONS[c][0], LEVELS[lvl]),
                                "flag": NATIONS[c][1], "group": LEVELS[lvl]})
    order = list(LEVELS.keys())
    return sorted(seen.values(), key=lambda t: (order.index(t["key"].split(":")[3]), t["name"])), events


# ------------------------------------------------------------ where to watch
# Official 2026-27 rights holders. NHL games carry their own per-game network
# list (US/CA); PWHL games carry their own Canadian/US broadcaster list.
NET_LINKS = [
    # (regex on network code, display name, url)
    (r"^(ESPN|ESPN2|ESPNU|ABC)$", None, "https://www.espn.com/watch/"),
    (r"^ESPN\+$", "ESPN+", "https://www.espn.com/watch/"),
    (r"^(TNT|truTV|TBS|HBO MAX|MAX)$", None, "https://www.hbomax.com/"),
    (r"^Disney\+$", None, "https://www.disneyplus.com/"),
    (r"^HULU$", "Hulu", "https://www.hulu.com/"),
    (r"^NHLN$", "NHL Network", "https://www.nhl.com/info/nhl-network"),
    (r"^Prime( Video)?$", "Prime Video", "https://www.primevideo.com/"),
    (r"^(SN|SN1|SNE|SNO|SNP|SNW|SN360|SN\+|CITY|Sportsnet)$", None, "https://watch.sportsnet.ca/"),
    (r"^TSN\d?$", None, "https://www.tsn.ca/live"),
    (r"^RDS(\d|I)?( \(FR\))?$", None, "https://www.rds.ca/"),
    (r"^TVAS\d?$", "TVA Sports", "https://www.tvasports.ca/"),
    (r"^CBC$", "CBC Gem", "https://gem.cbc.ca/"),
    (r"^Radio Canada$", "ICI TOU.TV", "https://ici.tou.tv/"),
    (r"^NESN", "NESN", "https://nesn.com/"),
    (r"^NBCS", None, "https://www.nbcsports.com/"),
    (r"^MNMT", "Monumental", "https://www.monumentalsports.com/"),
    (r"^ALT$", "Altitude", "https://www.altitudesports.com/"),
    (r"^MSG", None, "https://www.msgnetworks.com/"),
    (r"^SN-PIT$", "SportsNet Pittsburgh", "https://www.sportsnetpittsburgh.com/"),
    (r"^ION$", "ION", "https://iontelevision.com/find-us"),
]

# Outside the US/Canada (NHL): local rights holder, else NHL.TV on DAZN
NHL_INTL = {
    "GB": [("NHL.TV on DAZN", "https://www.dazn.com/")],
    "SE": [("Viaplay", "https://viaplay.com/")], "FI": [("Viaplay", "https://viaplay.com/")],
    "NO": [("Viaplay", "https://viaplay.com/")], "DK": [("Viaplay", "https://viaplay.com/")],
    "IS": [("Viaplay", "https://viaplay.com/")],
    "DE": [("Sky Sport", "https://www.sky.de/"), ("NHL.TV on DAZN", "https://www.dazn.com/")],
    "CH": [("MySports", "https://www.mysports.ch/")],
    "FR": [("beIN Sports", "https://www.beinsports.com/france/")],
    "MX": [("Sky México", "https://www.sky.com.mx/")],
    "AR": [("ESPN on Disney+", "https://www.disneyplus.com/")], "BR": [("ESPN on Disney+", "https://www.disneyplus.com/")],
    "CL": [("ESPN on Disney+", "https://www.disneyplus.com/")], "CO": [("ESPN on Disney+", "https://www.disneyplus.com/")],
    "SI": [("Arena Sport", "https://www.arenasport.com/")], "HR": [("Arena Sport", "https://www.arenasport.com/")],
    "RS": [("Arena Sport", "https://www.arenasport.com/")], "BA": [("Arena Sport", "https://www.arenasport.com/")],
}
NHL_FREE = {"GB": ("ITVX (selected weekend games)", "https://www.itv.com/watch"),
            "AU": ("9Now (one game a week)", "https://www.9now.com.au/")}


def _net_link(code):
    for pat, label, url in NET_LINKS:
        if re.search(pat, code, re.I):
            return {"label": label or code, "url": url}
    return {"label": code, "url": None}


def _yt(q):
    return "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q)


def _short(t):
    # "Toronto Maple Leafs" -> "Maple Leafs"-ish for highlight searches
    return t.get("name", "")


def watch_links(e, region):
    """-> {"official": [...], "free": [...]} each item {label, url, note?}"""
    region = (region or "").upper()
    off, free = [], []
    seen = set()

    def add(lst, label, url, note=None):
        k = (label, url)
        if k in seen:
            return
        seen.add(k)
        item = {"label": label, "url": url}
        if note:
            item["note"] = note
        lst.append(item)

    final = e.get("status") == "final"
    lg = e["league"]
    if lg == "NHL":
        if region in ("US", "CA"):
            bcs = [b for b in e.get("broadcasts", []) if b.get("c") == region]
            for b in sorted(bcs, key=lambda b: {"N": 0, "H": 1, "A": 2}.get(b.get("m"), 3)):
                l = _net_link(b["n"])
                add(off, l["label"], l["url"], {"N": "national", "H": "home feed", "A": "away feed"}.get(b.get("m")))
            if region == "US" and not any(b.get("m") == "N" for b in bcs):
                add(off, "ESPN+", "https://www.espn.com/watch/", "out-of-market")
            if region == "CA" and not off:
                add(off, "Sportsnet+", "https://watch.sportsnet.ca/")
        else:
            for label, url in NHL_INTL.get(region, [("NHL.TV on DAZN", "https://www.dazn.com/")]):
                add(off, label, url)
            if region in NHL_FREE:
                add(free, NHL_FREE[region][0], NHL_FREE[region][1])
        if final:
            add(free, "Recap on NHL.com", e.get("url"))
            add(free, "Highlights on NHL YouTube",
                _yt("NHL Highlights %s vs %s" % (_short(e["away"]), _short(e["home"]))))
        else:
            add(free, "Highlights on NHL YouTube (after the game)", "https://www.youtube.com/nhl")
    elif lg == "PWHL":
        ca = [b for b in e.get("broadcasts", []) if b.get("c") == "CA"]
        us = [b for b in e.get("broadcasts", []) if b.get("c") == "US"]
        if region == "CA":
            for b in ca:
                l = _net_link(b["n"])
                add(off, l["label"], b.get("url") or l["url"])
            if not ca and not final:
                add(free, "PWHL YouTube (live, free)", "https://www.youtube.com/@thepwhlofficial/streams")
        else:
            if region == "US":
                for b in us:
                    l = _net_link(b["n"])
                    add(off, l["label"], b.get("url") or l["url"])
            add(free, "PWHL YouTube (live, free)" if not final else "Full replay on PWHL YouTube",
                "https://www.youtube.com/@thepwhlofficial/streams" if not final else
                "https://www.youtube.com/@thepwhlofficial/search?query=" +
                urllib.parse.quote_plus("%s %s" % (e["away"]["name"], e["home"]["name"])))
            add(free, "thepwhl.com", "https://www.thepwhl.com/")
        if final:
            add(free, "Highlights on PWHL YouTube",
                "https://www.youtube.com/@thepwhlofficial/search?query=" +
                urllib.parse.quote_plus("highlights %s %s" % (e["away"]["name"], e["home"]["name"])))
    else:  # IIHF / Olympics
        comp = e.get("comp", "")
        lvl = e.get("level")
        if region == "CA":
            add(off, "TSN", "https://www.tsn.ca/live")
            add(off, "RDS (FR)", "https://www.rds.ca/")
        elif region == "US":
            if lvl == "U20":
                add(off, "NHL Network", "https://www.nhl.com/info/nhl-network", "2026 partner")
                add(off, "ESPN+", "https://www.espn.com/watch/", "2026 partner")
            elif lvl == "M" and lg == "IIHF":
                add(off, "FloHockey", "https://www.flohockey.tv/")
        if not e.get("maybe"):
            add(free, "IIHF highlights on YouTube",
                _yt("IIHF %s %s vs %s highlights" % (comp, e["away"]["name"], e["home"]["name"])))
        add(free, "Official stats & game sheets", e.get("url"))
    return {"official": off, "free": [f for f in free if f.get("url")]}


# --------------------------------------------------------------- aggregation
def resolve_follow(key):
    """follow key -> (label, set of team tokens, extra)"""
    parts = key.split(":")
    if parts[0] == "team" and len(parts) >= 3:
        return {"tokens": {":".join(parts[1:])}}
    if parts[0] == "player" and len(parts) >= 3:
        league, pid = parts[1], ":".join(parts[2:])
        info = nhl_player(pid) if league == "NHL" else pwhl_player(pid)
        tokens = set()
        if info.get("team"):
            tokens.add(info["team"])
        if info.get("nation"):
            for lvl in info.get("levels") or [info["level"]]:
                tokens.add("INTL:%s:%s" % (info["nation"], lvl))
        return {"tokens": tokens, "player": info}
    return {"tokens": set()}


def gather(follow_keys, include_past_days=PAST_DAYS, region=None):
    resolved = {}
    tokens_by_follow = {}
    for k in follow_keys:
        try:
            r = resolve_follow(k)
        except Exception as e:
            r = {"tokens": set(), "error": str(e)}
        resolved[k] = r
        tokens_by_follow[k] = r["tokens"]
    all_tokens = set().union(*tokens_by_follow.values()) if tokens_by_follow else set()

    pool = []
    nhl_codes = {t.split(":")[1] for t in all_tokens if t.startswith("NHL:")}
    for code in nhl_codes:
        try:
            pool.extend(nhl_team_schedule(code))
        except Exception:
            pass
    if any(t.startswith("PWHL:") for t in all_tokens):
        try:
            pool.extend(pwhl_data()["games"])
        except Exception:
            pass
    if any(t.startswith("INTL:") for t in all_tokens):
        try:
            pool.extend(iihf_games()[0])
        except Exception:
            pass

    cutoff = datetime.now(timezone.utc) - timedelta(days=include_past_days)
    out, seen = [], set()
    for ev in pool:
        if ev["id"] in seen:
            continue
        if parse_iso(ev["start"]) < cutoff:
            continue
        because = [k for k, toks in tokens_by_follow.items() if toks & set(ev["teams"])]
        if not because:
            continue
        seen.add(ev["id"])
        e = dict(ev)
        e["because"] = because
        try:
            e["watch"] = watch_links(e, region)
        except Exception:
            e["watch"] = {"official": [], "free": []}
        out.append(e)
    out.sort(key=lambda e: e["start"])
    meta = {k: {"player": v.get("player"), "error": v.get("error")} for k, v in resolved.items()}
    return out, meta


# ---------------------------------------------------------------------- ICS
def ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += cb
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


EMOJI = {"NHL": "🏒", "PWHL": "🏒", "IIHF": "🌍", "OLY": "🏅"}


def event_title(e, with_score=True):
    a, h = e["away"], e["home"]
    if e.get("maybe"):
        ph = {"QF": "Quarterfinal", "SF": "Semifinal", "BMG": "Bronze medal game",
              "GMG": "Gold medal game", "REL": "Relegation"}.get(e.get("phase"), e.get("phase") or "Playoff")
        return "%s %s %s (if qualified)" % (EMOJI.get(e["league"], "🏒"), e["comp"], ph)
    if e["league"] in ("IIHF", "OLY"):
        core = "%s vs %s" % (a["name"], h["name"])
    else:
        core = "%s @ %s" % (a["name"], h["name"])
    s = "%s %s" % (EMOJI.get(e["league"], "🏒"), core)
    if with_score and e.get("score") and e["status"] in ("final", "live") and None not in e["score"]:
        s += " (%s–%s%s%s)" % (e["score"][0], e["score"][1], " " + e["note"] if e.get("note") and e["status"] == "final" else "",
                               ", LIVE" if e["status"] == "live" else "")
    if e["status"] in ("postponed", "cancelled"):
        s += " [%s]" % e["status"].upper()
    return s


def build_ics(events, name, alarm_min):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Rinkside//Hockey Schedule//EN",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         "X-WR-CALNAME:" + ics_escape(name), "X-WR-CALDESC:" + ics_escape("Live hockey schedule from Rinkside"),
         "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for e in events:
        st = parse_iso(e["start"])
        L.append("BEGIN:VEVENT")
        L.append("UID:%s@rinkside" % e["id"])
        L.append("DTSTAMP:" + now)
        summary = event_title(e)
        if e.get("tbd"):
            d = e.get("localDate") or st.strftime("%Y-%m-%d")
            d0 = datetime.strptime(d, "%Y-%m-%d")
            L.append("DTSTART;VALUE=DATE:" + d0.strftime("%Y%m%d"))
            L.append("DTEND;VALUE=DATE:" + (d0 + timedelta(days=1)).strftime("%Y%m%d"))
            summary += " (time TBA)"
        else:
            L.append("DTSTART:" + st.strftime("%Y%m%dT%H%M%SZ"))
            L.append("DTEND:" + (st + timedelta(minutes=150)).strftime("%Y%m%dT%H%M%SZ"))
        L.append("SUMMARY:" + ics_escape(summary))
        if e.get("venue"):
            L.append("LOCATION:" + ics_escape(e["venue"]))
        desc = [e.get("comp", "")]
        if e.get("phase"):
            desc.append("Phase: " + e["phase"])
        w = e.get("watch") or {}
        if w.get("official"):
            desc.append("Watch: " + ", ".join(x["label"] for x in w["official"]))
        for x in (w.get("free") or [])[:2]:
            desc.append("%s: %s" % (x["label"], x["url"]))
        desc.append(e.get("url", ""))
        L.append("DESCRIPTION:" + ics_escape("\n".join(x for x in desc if x)))
        if e.get("url"):
            L.append("URL:" + e["url"])
        L.append("CATEGORIES:" + e["league"])
        if e["status"] == "cancelled":
            L.append("STATUS:CANCELLED")
        if alarm_min and not e.get("tbd") and e["status"] == "scheduled":
            L += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + ics_escape(event_title(e, False)),
                  "TRIGGER:-PT%dM" % alarm_min, "END:VALARM"]
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(x) for x in L) + "\r\n"


# ------------------------------------------------------------ background jobs
def refresher():
    """Keeps the shared caches warm so pages and calendar feeds are always fresh."""
    while True:
        try:
            pwhl_data()
        except Exception:
            pass
        try:
            iihf_games()
        except Exception:
            pass
        try:
            pwhl_roster()
        except Exception:
            pass
        try:
            for t in nhl_teams():
                try:
                    nhl_team_schedule(t["code"])
                except Exception:
                    pass
                time.sleep(0.3)
        except Exception:
            pass
        time.sleep(240)


def discovery_loop():
    time.sleep(int(os.environ.get("DISCOVERY_DELAY", "120")))  # let the site serve first
    while True:
        try:
            iihf_discover()
        except Exception as e:
            mark("IIHF discovery", False, e)
        time.sleep(6 * 3600)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# ---------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Rinkside/1.0"

    def log_message(self, fmt, *args):
        # Keep a small request log so calendar-app problems can be diagnosed.
        try:
            ua = self.headers.get("User-Agent", "-") if getattr(self, "headers", None) else "-"
            line = "%s %s %s | UA=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.client_address[0], fmt % args, ua)
            if os.environ.get("RENDER"):
                print(line.rstrip(), flush=True)
                return
            p = os.path.join(DATA_DIR, "requests.log")
            if os.path.exists(p) and os.path.getsize(p) > 512 * 1024:
                os.replace(p, p + ".1")
            with open(p, "a") as f:
                f.write(line)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/subscribe":
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return self.send(400, {"error": "bad request"})
        # Only the Mac running Rinkside may ask it to open Calendar.
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return self.send(403, {"opened": False, "error": "only available on this Mac"})
        keys = [k for k in str(body.get("f", "")).split(",") if re.match(r"^(team|player):[A-Za-z0-9:_\-().%'À-ž]+$", k)]
        if not keys:
            return self.send(400, {"opened": False, "error": "nothing to subscribe to"})
        q = {"f": ",".join(keys), "name": str(body.get("name") or "Hockey")[:80],
             "alarm": str(int(body.get("alarm") or 0)), "region": re.sub(r"[^A-Za-z]", "", str(body.get("region") or ""))[:2]}
        url = "webcal://localhost:%d/cal.ics?%s" % (PORT, urllib.parse.urlencode(q))
        if body.get("dry"):
            return self.send(200, {"opened": False, "dry": True, "url": url})
        if sys.platform != "darwin":
            return self.send(200, {"opened": False, "url": url, "error": "not a Mac"})
        try:
            subprocess.run(["open", url], check=True, timeout=10)
            return self.send(200, {"opened": True, "url": url})
        except Exception as e:
            return self.send(200, {"opened": False, "url": url, "error": str(e)[:120]})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            if u.path in ("/", "/index.html"):
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/teams":
                lg = q.get("league", "NHL")
                if lg == "NHL":
                    return self.send(200, {"teams": nhl_teams()})
                if lg == "PWHL":
                    return self.send(200, {"teams": sorted(pwhl_data()["teams"].values(), key=lambda t: t["name"])})
                if lg == "INTL":
                    teams, events = intl_teams()
                    return self.send(200, {"teams": teams, "events": events})
                return self.send(400, {"error": "unknown league"})
            if u.path == "/api/search":
                lg, term = q.get("league", "NHL"), q.get("q", "").strip()
                if len(term) < 2:
                    return self.send(200, {"results": []})
                if lg == "NHL":
                    res = nhl_player_search(term)
                elif lg == "PWHL":
                    res = pwhl_player_search(term)
                else:  # both leagues at once
                    with ThreadPoolExecutor(max_workers=2) as ex:
                        fn = ex.submit(nhl_player_search, term)
                        fp = ex.submit(pwhl_player_search, term)
                        res = []
                        for fut in (fp, fn):
                            try:
                                res.extend(fut.result())
                            except Exception:
                                pass
                    # exact-ish name matches first, rostered NHL players before prospects
                    words = _norm(term).split()
                    res.sort(key=lambda r: (not all(w in _norm(r["name"]) for w in words),
                                            r.get("hasTeam") is False))
                return self.send(200, {"results": res})
            if u.path == "/api/schedule":
                keys = [k for k in q.get("f", "").split(",") if k]
                try:
                    past = max(0, min(400, int(q.get("past", PAST_DAYS))))
                except ValueError:
                    past = PAST_DAYS
                events, meta = gather(keys, include_past_days=past, region=q.get("region"))
                return self.send(200, {"events": events, "meta": meta, "status": STATUS,
                                       "generated": iso(datetime.now(timezone.utc))})
            if u.path == "/api/status":
                ip = lan_ip()
                return self.send(200, {"status": STATUS, "lan": ("http://%s:%d" % (ip, PORT)) if ip else None,
                                       "iihf": iihf_registry().get("tournaments", {})})
            if u.path in ("/cal.ics", "/cal") or u.path.startswith("/cal/"):
                keys = [k for k in q.get("f", "").split(",") if k]
                name = q.get("name") or "Hockey"
                alarm = int(q.get("alarm", "30") or 0)
                events, _ = gather(keys, include_past_days=45, region=q.get("region"))
                body = build_ics(events, name, alarm)
                return self.send(200, body, "text/calendar; charset=utf-8",
                                 {"Content-Disposition": 'inline; filename="%s.ics"' % slugify(name)})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(502, {"error": str(e)})


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 at once. Apple Calendar resolves "localhost" to ::1
    and does not fall back to 127.0.0.1 the way browsers do."""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()


def make_server(port):
    try:
        return DualStackServer(("::", port), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):  # address in use
            raise
        return ThreadingHTTPServer(("0.0.0.0", port), Handler)  # no IPv6 on this machine


def main():
    global PORT
    srv = None
    for p in range(PORT, PORT + 15):
        try:
            srv = make_server(p)
            PORT = p
            break
        except OSError:
            continue
    if not srv:
        raise SystemExit("No free port found near %d" % PORT)
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=refresher, daemon=True).start()
    ip = lan_ip()
    if os.environ.get("RINKSIDE_OPEN"):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open("http://localhost:%d" % PORT)).start()
    print("Rinkside running:  http://localhost:%d" % PORT)
    if ip:
        print("On your Wi-Fi (iPhone/iPad):  http://%s:%d" % (ip, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
