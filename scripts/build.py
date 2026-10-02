#!/usr/bin/env python3
"""Build the SVG assets for the ArcueidMP profile README.

GitHub READMEs allow no CSS and no web fonts, so every piece of text here is
shaped with HarfBuzz and written out as vector outlines in Source Serif 4 and
Inter, with Shippori Mincho for the Japanese seal and epigraph. Each glyph outline is defined once
per asset and placed with <use>, which keeps the files small. Every asset is
emitted twice, for GitHub's light and dark colour schemes, and the README
switches between them with <picture>.

The hero carries a faceted glass moon, shaded to the real phase of the moon on
the (UTC) build day and turning vermilion on the night of a full moon. Its
perpetual motion is CSS inside the SVG, so prefers-reduced-motion stills it.

Live numbers (repository stars, languages, contributions, the year's daily
contribution counts, recent public commits and merged pull requests) come
from the GitHub API at build time and are cached in data/live.json so an API
outage never produces an empty card. Only public data is ever requested. The
daily counts are drawn as a night sky, one star per day.

Usage:  python scripts/build.py            # edit profile.toml first
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "scripts" / "fonts"
ASSET_DIR = ROOT / "assets"
DATA_FILE = ROOT / "data" / "live.json"

W = 880          # design width of every asset; the README scales them to 100%
PAD = 40         # horizontal gutter
RIGHT = W - PAD  # right edge for right-aligned text
LOG_ROWS = 4     # entries shown in the recent-activity ledger

# Colour tokens: warm paper / night indigo with a blue accent, in light and dark.
# Vermilion ("red") is kept for things that are happening now: the moon, today
# in the calendar, active projects. The hero panel runs from bg to bg2; the
# glass moon is shaded through the four-stop "glass" ramp ("glass_full" on the
# night of a full moon), with facet edges, rim and cracks drawn in their tokens.
THEMES = {
    "light": {
        "bg": "#f6f3ec", "bg2": "#f1ece2", "surface": "#fffdf8", "text": "#17191d", "soft": "#33373c",
        "muted": "#5a5d60", "accent": "#244a91", "label": "#244a91",
        "rule": "#d5cec1", "rule2": "#aaa397", "red": "#c4382c",
        "card_op": 0.72, "card_stroke": "#d5cec1", "card_stroke_op": 1,
        "glass": ["#c3cfe6", "#d9e1f0", "#ecf0f8", "#fbfcfe"],
        "glass_full": ["#ecc9c0", "#f3dad3", "#f9ebe7", "#fffaf8"],
        "facet": "#244a91", "facet_op": 0.34, "rim": "#244a91", "rim_op": 0.75, "crack": "#244a91",
        "glow_op": 0.10, "night": False,
    },
    "dark": {
        "bg": "#0b1022", "bg2": "#141b31", "surface": "#121a30", "text": "#eae7e0", "soft": "#c9c6c0",
        "muted": "#a09d97", "accent": "#8fb2ee", "label": "#b3cbf5",
        "rule": "#33343b", "rule2": "#6a6d79", "red": "#e5573f",
        "card_op": 0.62, "card_stroke": "#b3cbf5", "card_stroke_op": 0.28,
        "glass": ["#1d3768", "#4369ad", "#94b6ef", "#eef4ff"],
        "glass_full": ["#4a1a1d", "#9c3328", "#e5704f", "#ffe2d8"],
        "facet": "#d6e4ff", "facet_op": 0.16, "rim": "#cfe0ff", "rim_op": 0.4, "crack": "#eef4ff",
        "glow_op": 0.34, "night": True,
    },
}

# GitHub linguist colours for the language dot on project rows.
LANG_COLOURS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572A5", "C": "#555555",
    "C++": "#f34b7d", "Rust": "#dea584", "Go": "#00ADD8", "Java": "#b07219", "HTML": "#e34c26",
    "CSS": "#663399", "Shell": "#89e051", "Jupyter Notebook": "#DA5B0B", "Kotlin": "#A97BFF",
    "TeX": "#3D6117",
}

TABULAR = {"tnum": True}  # lining, equal-width figures for dates and counts


# --------------------------------------------------------------------------- text engine


def num(v: float, places: int = 2) -> str:
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Face:
    """A font file that can shape a string and hand out glyph outlines."""

    def __init__(self, tag: str, path: Path):
        self.tag = tag
        self.tt = TTFont(path)
        self.upm = self.tt["head"].unitsPerEm
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.cmap = self.tt.getBestCmap()
        self.font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))

    def supports(self, text: str) -> bool:
        """True when every character has a glyph in the (Latin-only) subset."""
        return all(ord(ch) in self.cmap for ch in text)

    def shape(self, text: str, size: float, tracking: float = 0.0, features: dict | None = None):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.font, buf, {"kern": True, "liga": True, "calt": True, **(features or {})})
        scale = size / self.upm
        track = tracking * size
        x = 0.0
        placed = []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            placed.append((self.order[info.codepoint], x + pos.x_offset * scale, pos.y_offset * scale))
            x += pos.x_advance * scale + track
        width = max(0.0, x - track) if placed else 0.0
        return placed, width

    def width(self, text: str, size: float, tracking: float = 0.0, features: dict | None = None) -> float:
        return self.shape(text, size, tracking, features)[1]

    def outline(self, glyph: str, size: float) -> str:
        scale = size / self.upm
        pen = SVGPathPen(self.glyphs, ntos=lambda v: num(v, 1 if size < 20 else 2))
        self.glyphs[glyph].draw(TransformPen(pen, (scale, 0, 0, -scale, 0, 0)))
        return pen.getCommands()

    def wrap(self, text: str, size: float, max_width: float, max_lines: int = 2) -> list[str]:
        """Greedy word wrap; the last permitted line is ellipsized if text remains."""
        words = text.split()
        lines: list[str] = []
        current: list[str] = []
        for i, word in enumerate(words):
            if current and self.width(" ".join(current + [word]), size) > max_width:
                if len(lines) == max_lines - 1:
                    return lines + [self.ellipsize(" ".join(words[i - len(current):]), size, max_width)]
                lines.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(" ".join(current))
        return lines

    def ellipsize(self, text: str, size: float, max_width: float) -> str:
        if self.width(text, size) <= max_width:
            return text
        words = text.split()
        while len(words) > 1:
            words.pop()
            candidate = " ".join(words).rstrip(",.;:") + "…"
            if self.width(candidate, size) <= max_width:
                return candidate
        return text


class Fonts:
    def __init__(self):
        self.display = Face("d", FONT_DIR / "SourceSerif4-Display.ttf")       # wght 540, opsz 60
        self.serif = Face("s", FONT_DIR / "SourceSerif4-Text.ttf")            # wght 540, opsz 20
        self.serif_regular = Face("r", FONT_DIR / "SourceSerif4-TextRegular.ttf")
        self.sans = Face("i", FONT_DIR / "Inter-Regular.ttf")
        self.sans_medium = Face("m", FONT_DIR / "Inter-Medium.ttf")
        self.sans_bold = Face("b", FONT_DIR / "Inter-Bold.ttf")               # wght 720, the eyebrow weight
        self.mincho = Face("j", FONT_DIR / "ShipporiMincho-Medium.ttf")       # subset to profile.toml's Japanese
        self.mincho_bold = Face("k", FONT_DIR / "ShipporiMincho-ExtraBold.ttf")


class Canvas:
    """One SVG asset: collects glyph definitions and body markup."""

    def __init__(self, f: Fonts, t: dict):
        self.f = f
        self.t = t
        self.defs: dict[str, str] = {}
        self.extra_defs: list[str] = []  # gradients, filters, clip paths
        self.css: list[str] = []
        self.body: list[str] = []

    def add(self, markup: str) -> None:
        self.body.append(markup)

    def define(self, markup: str) -> None:
        self.extra_defs.append(markup)

    def style(self, css: str) -> None:
        self.css.append(css)

    # -- text ---------------------------------------------------------------
    def text(self, face: Face, text: str, x: float, y: float, size: float, fill: str, *,
             tracking: float = 0.0, anchor: str = "start", features: dict | None = None) -> str:
        placed, width = face.shape(text, size, tracking, features)
        if anchor == "end":
            x -= width
        elif anchor == "middle":
            x -= width / 2
        uses = []
        for glyph, gx, gy in placed:
            gid = f"{face.tag}{num(size, 1)}-{glyph}".replace(".", "_")
            if gid not in self.defs:
                d = face.outline(glyph, size)
                self.defs[gid] = f'<path id="{gid}" d="{d}"/>' if d else ""
            if self.defs[gid]:
                uses.append(f'<use href="#{gid}" x="{num(x + gx)}" y="{num(y - gy)}"/>')
        return f'<g fill="{fill}">{"".join(uses)}</g>' if uses else ""

    def eyebrow(self, text: str, x: float, y: float, fill: str, size: float = 11.5, anchor: str = "start") -> str:
        return self.text(self.f.sans_bold, text.upper(), x, y, size, fill, tracking=0.13, anchor=anchor)

    # -- primitives ---------------------------------------------------------
    @staticmethod
    def hrule(y: float, colour: str, x1: float = PAD, x2: float = RIGHT, width: float = 1) -> str:
        return f'<rect x="{num(x1)}" y="{num(y)}" width="{num(x2 - x1)}" height="{num(width)}" fill="{colour}"/>'

    @staticmethod
    def accent_rule(x: float, y: float, colour: str, animate: bool = False) -> str:
        """The short 32px accent line that precedes every eyebrow."""
        anim = ""
        if animate:
            anim = ('<animate attributeName="width" values="0;32" dur="0.8s" begin="0s" fill="freeze" '
                    'calcMode="spline" keySplines="0.2 0.7 0.2 1"/>')
        return f'<rect x="{num(x)}" y="{num(y)}" width="32" height="2" fill="{colour}">{anim}</rect>'

    @staticmethod
    def fade(markup: str, delay: float) -> str:
        """Fade a group in with no flash-before-start: begin at 0 and hold 0 until `delay`."""
        total = delay + 0.7
        k = delay / total
        return (f'<g opacity="1"><animate attributeName="opacity" values="0;0;1" keyTimes="0;{k:.3f};1" '
                f'dur="{total:.2f}s" begin="0s" fill="freeze" calcMode="spline" '
                f'keySplines="0 0 1 1;0.2 0.7 0.2 1"/>{markup}</g>')

    # -- output -------------------------------------------------------------
    def render(self, height: float, title: str, bg: str | None = None) -> str:
        rect = f'<rect width="{W}" height="{num(height)}" fill="{bg}"/>' if bg else ""
        defs = "".join(d for d in self.defs.values() if d) + "".join(self.extra_defs)
        css = f"<style>{''.join(self.css)}</style>" if self.css else ""
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{num(height)}" '
            f'viewBox="0 0 {W} {num(height)}" role="img" aria-label="{escape(title)}">'
            f"<title>{escape(title)}</title>{css}<defs>{defs}</defs>{rect}{''.join(self.body)}</svg>\n"
        )


# --------------------------------------------------------------------------- live data


def http_json(url: str, headers: dict | None = None, data: bytes | None = None, timeout: int = 20):
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "ArcueidMP-profile-build", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def github_token() -> str | None:
    """GITHUB_TOKEN in CI; locally, borrow the gh CLI's token if it is logged in."""
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


ACTIVITY_QUERY = """
query($login: String!, $search: String!) {
  user(login: $login) {
    contributionsCollection {
      totalCommitContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
    repositories(first: 20, privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        nameWithOwner
        defaultBranchRef { target { ... on Commit {
          history(first: 6) { nodes { oid messageHeadline committedDate author { user { login } } } }
        } } }
      }
    }
  }
  search(query: $search, type: ISSUE, first: 8) {
    nodes { ... on PullRequest {
      number title mergedAt repository { nameWithOwner isPrivate }
    } }
  }
}
"""

MERGE_COMMIT = re.compile(r"^Merge (pull request|branch|remote-tracking branch) ")
PR_SUFFIX = re.compile(r"\s*\(#(\d+)\)$")


def recent_activity(login: str, data: dict) -> list[dict]:
    """Merge public commits and merged pull requests into one dated list, newest first."""
    entries: list[dict] = []
    seen_titles: set[str] = set()
    seen_prs: set[tuple[str, int]] = set()
    for pr in data["search"]["nodes"]:
        if not pr or pr["repository"]["isPrivate"] or not pr.get("mergedAt"):
            continue
        seen_titles.add(pr["title"].strip().lower())
        seen_prs.add((pr["repository"]["nameWithOwner"], pr["number"]))
        entries.append({"date": pr["mergedAt"][:10], "repo": pr["repository"]["nameWithOwner"],
                        "kind": "pull request · merged", "text": pr["title"].strip(),
                        "fallback": f"Pull request #{pr['number']} merged"})
    for repo in data["user"]["repositories"]["nodes"]:
        branch = repo.get("defaultBranchRef")
        if not branch:
            continue
        for commit in branch["target"]["history"]["nodes"]:
            author = (commit.get("author") or {}).get("user") or {}
            headline = commit["messageHeadline"].strip()
            if author.get("login") != login or MERGE_COMMIT.match(headline):
                continue
            # A squash merge's headline often differs from its PR title ("fix: improve x (#16)"
            # vs "Fix x"), so match the "(#N)" suffix against the merged PRs first.
            suffix = PR_SUFFIX.search(headline)
            if suffix and (repo["nameWithOwner"], int(suffix.group(1))) in seen_prs:
                continue  # the merged pull request already tells this story
            if PR_SUFFIX.sub("", headline).lower() in seen_titles:
                continue
            entries.append({"date": commit["committedDate"][:10], "repo": repo["nameWithOwner"],
                            "kind": "commit", "text": headline, "fallback": f"Commit {commit['oid'][:7]}"})
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:LOG_ROWS + 2]


def fetch_live(cfg: dict) -> dict:
    prev = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}
    live = {k: prev.get(k) for k in ("github_user", "repos", "contributions", "activity", "calendar")}
    login = cfg["links"]["github"]
    headers = {"Accept": "application/vnd.github+json"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:  # public repositories only, whatever token is in use
        repos = http_json(f"https://api.github.com/users/{login}/repos?per_page=100&type=owner", headers)
        own = [r for r in repos if not r.get("fork")]
        languages: dict[str, int] = {}
        for r in own:
            if r.get("language"):
                languages[r["language"]] = languages.get(r["language"], 0) + 1
        live["github_user"] = {
            "public_repos": len(own),
            "stars": sum(r["stargazers_count"] for r in own),
            "languages": sorted(languages, key=lambda k: (-languages[k], k)),
        }
        live["repos"] = {
            r["full_name"]: {"stars": r["stargazers_count"], "language": r.get("language"),
                             "pushed_at": r["pushed_at"][:10]}
            for r in repos
        }
    except Exception as exc:  # noqa: BLE001 - any failure falls back to the cache
        print(f"warning: github repos unavailable ({exc}); using cached values", file=sys.stderr)

    if token:
        variables = {"login": login, "search": f"author:{login} is:pr is:public is:merged sort:updated-desc"}
        try:
            body = json.dumps({"query": ACTIVITY_QUERY, "variables": variables}).encode()
            data = http_json("https://api.github.com/graphql", headers, body)["data"]
            coll = data["user"]["contributionsCollection"]
            live["contributions"] = {
                "total": coll["contributionCalendar"]["totalContributions"],
                "commits": coll["totalCommitContributions"],
            }
            live["activity"] = recent_activity(login, data)
            days = [d for w in coll["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
            live["calendar"] = {"start": days[0]["date"], "counts": [d["contributionCount"] for d in days]}
        except Exception as exc:  # noqa: BLE001
            print(f"warning: activity unavailable ({exc}); using cached values", file=sys.stderr)
    else:
        print("note: no GitHub token; contributions and activity use cached values", file=sys.stderr)

    # Only move the "synced" date when a number actually changed. The calendar is left
    # out: its window slides every day, and a new contribution moves the total anyway.
    def core(d: dict) -> str:
        return json.dumps({k: d.get(k) for k in ("github_user", "repos", "contributions", "activity")},
                          sort_keys=True)

    today = dt.date.today().isoformat()
    live["changed_at"] = prev.get("changed_at", today) if core(live) == core(prev) else today
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(live, indent=2, ensure_ascii=False)
    # keep arrays of plain numbers (the year of daily counts) on one line rather than 371
    text = re.sub(r"\[\s*(\d+(?:,\s*\d+)*)\s*\]",
                  lambda m: "[" + ", ".join(n.strip() for n in m.group(1).split(",")) + "]", text)
    DATA_FILE.write_text(text + "\n", encoding="utf-8")
    return live


def pretty_date(iso: str, year: bool = True) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b %Y' if year else '%b')}"


# --------------------------------------------------------------------------- the glass moon
#
# "A piece of blue glass moon": a faceted glass disc with one shard chipped out
# and floating beside it, shaded to the real phase of the moon on the build day.
# The geometry is seeded, so it only changes when the code does.

PHASE_NAMES = [(0, "New moon"), (90, "First quarter"), (180, "Full moon"), (270, "Last quarter")]


def moon_phase(day: dt.date) -> dict:
    """The moon at 12:00 UTC on `day`.

    The phase angle uses the leading terms of Meeus, Astronomical Algorithms
    eq. 48.4; that is good to about 0.1 degree, i.e. within minutes of the
    published phase times, which is plenty for a picture.
    """
    noon = dt.datetime(day.year, day.month, day.day, 12, tzinfo=dt.timezone.utc)
    T = (noon - dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc)).total_seconds() / 86400 / 36525
    D = 297.8501921 + 445267.1114034 * T   # mean elongation of the moon
    M = 357.5291092 + 35999.0502909 * T    # sun's mean anomaly
    Mp = 134.9633964 + 477198.8675055 * T  # moon's mean anomaly

    def s(deg: float) -> float:
        return math.sin(math.radians(deg))

    i = (180 - D - 6.289 * s(Mp) + 2.100 * s(M) - 1.274 * s(2 * D - Mp)
         - 0.658 * s(2 * D) - 0.214 * s(2 * Mp) - 0.110 * s(D))
    elong = (180 - i) % 360  # 0 new, 90 first quarter, 180 full, 270 last quarter
    # a principal phase is named for about a day either side (the moon moves ~12.2 deg a day)
    name = next((n for a, n in PHASE_NAMES if min(abs(elong - a), 360 - abs(elong - a)) < 12.2), None)
    if name is None and elong < 180:
        name = "Waxing " + ("crescent" if elong < 90 else "gibbous")
    elif name is None:
        name = "Waning " + ("gibbous" if elong < 270 else "crescent")
    return {"lit": (1 - math.cos(math.radians(elong))) / 2, "waning": elong >= 180,
            "name": name, "full": name == "Full moon"}


def delaunay(pts: list[tuple[float, float]]) -> list[tuple[int, int, int]]:
    """Bowyer-Watson triangulation; plenty fast for the few dozen points of the moon."""
    n = len(pts)
    P = pts + [(-1e4, -1e4), (1e4, -1e4), (0.0, 1e4)]

    def circumcircle(t):
        (ax, ay), (bx, by), (cx, cy) = P[t[0]], P[t[1]], P[t[2]]
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
        uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
        return ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2

    tris = {(n, n + 1, n + 2): circumcircle((n, n + 1, n + 2))}
    for i, (px, py) in enumerate(pts):
        bad = [t for t, (ux, uy, r2) in tris.items() if (px - ux) ** 2 + (py - uy) ** 2 < r2]
        edges: dict[tuple[int, int], int] = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                key = (min(e), max(e))
                edges[key] = edges.get(key, 0) + 1
            del tris[t]
        for (a, b), count in edges.items():
            if count == 1:  # the boundary of the cavity
                tris[(a, b, i)] = circumcircle((a, b, i))
    return [t for t in tris if max(t) < n]


def glass_moon(r: float = 100, seed: int = 7) -> dict:
    """Facets (in disc-centred coordinates), the chipped shard and the cracks around it."""
    rnd = random.Random(seed)
    # Rim points sit outside the disc so the clip path cuts the outer facets; their
    # radius is jittered because exactly co-circular points make the in-circle test unstable.
    pts = []
    for k in range(22):
        a = 2 * math.pi * k / 22 + rnd.uniform(-0.08, 0.08)
        rr = r * 1.08 * (1 + rnd.uniform(-0.025, 0.025))
        pts.append((rr * math.cos(a), rr * math.sin(a)))
    for _ in range(5000):
        if len(pts) >= 60:
            break
        rr, a = r * 0.97 * math.sqrt(rnd.random()), rnd.uniform(0, 2 * math.pi)
        p = (rr * math.cos(a), rr * math.sin(a))
        if all(math.dist(p, q) > 15 for q in pts):
            pts.append(p)

    facets = []
    for t in delaunay(pts):
        poly = [pts[i] for i in t]
        gx, gy = sum(p[0] for p in poly) / 3, sum(p[1] for p in poly) / 3
        nx, ny = gx / r, gy / r  # each facet takes the sphere's normal at its centroid
        facets.append({"poly": poly, "c": (gx, gy), "n": (nx, ny, math.sqrt(max(0.0, 1 - nx * nx - ny * ny))),
                       "jitter": rnd.uniform(-0.08, 0.08), "inside": all(math.hypot(*p) < r * 0.97 for p in poly)})

    def area(p):
        return abs((p[1][0] - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (p[1][1] - p[0][1])) / 2

    def sliver(p):  # longest edge squared over area: high for thin, shard-like triangles
        return max(math.dist(p[i], p[(i + 1) % 3]) for i in range(3)) ** 2 / max(area(p), 1)

    # the shard: the thinnest mid-sized facet near the lit upper-left limb
    target = (-0.62 * r, -0.56 * r)
    near = sorted((fc for fc in facets if fc["inside"] and 90 < area(fc["poly"]) < 260),
                  key=lambda fc: math.dist(fc["c"], target))[:5]
    shard = max(near, key=lambda fc: sliver(fc["poly"]))

    cracks = []
    sx, sy = shard["c"]
    for k, bend in enumerate((0.1, 0.8, -0.5)):  # one crack from each corner of the chip
        x, y = shard["poly"][k]
        ang = math.atan2(y - sy, x - sx) * 0.4 + bend
        path = [(x, y)]
        for _ in range(rnd.randint(7, 12)):
            ang += rnd.uniform(-0.45, 0.45)
            step = rnd.uniform(7, 13)
            x, y = x + step * math.cos(ang), y + step * math.sin(ang)
            path.append((x, y))
        cracks.append(path)
    return {"r": r, "facets": facets, "shard": shard, "cracks": cracks}


def mix(a: str, b: str, k: float) -> str:
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * k):02x}" for x, y in zip(ca, cb))


def ramp(stops: list[str], v: float) -> str:
    v = min(max(v, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(v), len(stops) - 2)
    return mix(stops[i], stops[i + 1], v - i)


def poly_path(poly, dx: float = 0, dy: float = 0) -> str:
    return "M" + "L".join(f"{num(x + dx, 1)},{num(y + dy, 1)}" for x, y in poly) + "Z"


def shadow_path(cx: float, cy: float, r: float, lit: float, waning: bool) -> str:
    """The unlit part of the disc: one limb plus the terminator, a half-ellipse of
    x-radius r*|1-2*lit|. The shadow is on the right of a waning moon."""
    rx = r * abs(1 - 2 * lit)
    gibbous = lit > 0.5
    limb = 1 if waning else 0
    terminator = (0 if gibbous else 1) if waning else (1 if gibbous else 0)
    return (f"M{num(cx)},{num(cy - r)}A{num(r)},{num(r)} 0 0 {limb} {num(cx)},{num(cy + r)}"
            f"A{num(rx)},{num(r)} 0 0 {terminator} {num(cx)},{num(cy - r)}Z")


MOON = glass_moon()

# Perpetual motion lives in CSS so prefers-reduced-motion can switch it off;
# without motion the cracks are simply drawn and the glint is parked off the disc.
MOON_CSS = (
    ".glint{transform:translateX(300px)}"
    "@media (prefers-reduced-motion:no-preference){"
    ".glow{animation:glow 7s ease-in-out infinite}"
    ".glint{animation:glint 9s cubic-bezier(.4,0,.2,1) 1.2s infinite both}"
    ".shard{animation:bob 6s ease-in-out infinite}"
    ".crack{stroke-dasharray:1;animation:crack 1.4s cubic-bezier(.3,.6,.2,1) both}"
    ".tw{animation:twinkle 4.5s ease-in-out infinite}"
    "@keyframes glow{50%{opacity:.7}}"
    "@keyframes glint{0%{transform:translateX(-260px)}28%,100%{transform:translateX(260px)}}"
    "@keyframes bob{50%{transform:translate(-2px,-5px)}}"
    "@keyframes crack{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}"
    "@keyframes twinkle{50%{opacity:.25}}}"
)


def draw_moon(c: Canvas, cx: float, cy: float, phase: dict) -> str:
    """Markup for the glow, the shaded glass disc (id "moon", reusable via <use>) and the shard."""
    t, m = c.t, MOON
    r = m["r"]
    stops = t["glass_full"] if phase["full"] else t["glass"]
    glow = t["red"] if phase["full"] else t["accent"]
    # ink lines turn vermilion with the paper moon; on the night sky they stay pale
    red_ink = phase["full"] and not t["night"]
    facet_ink = t["red"] if red_ink else t["facet"]
    rim_ink = t["red"] if red_ink else t["rim"]
    crack_ink = t["red"] if red_ink else t["crack"]
    # sunlight from above and from the lit side: the right while waxing, the left while waning
    light = (-0.52 if phase["waning"] else 0.52, -0.6, 0.6)
    norm = math.hypot(*light)

    def facet_fill(fc: dict) -> str:
        lambert = max(0.0, sum(a * b for a, b in zip(fc["n"], light)) / norm)
        return ramp(stops, 0.3 + 0.7 * lambert + fc["jitter"])

    c.style(MOON_CSS)
    c.define(f'<radialGradient id="glow"><stop offset="0" stop-color="{glow}" stop-opacity="{t["glow_op"]}"/>'
             f'<stop offset="0.55" stop-color="{glow}" stop-opacity="{num(t["glow_op"] * 0.3)}"/>'
             f'<stop offset="1" stop-color="{glow}" stop-opacity="0"/></radialGradient>')
    c.define(f'<linearGradient id="glint" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
             f'<stop offset="0.5" stop-color="#fff" stop-opacity="{0.38 if t["night"] else 0.7}"/>'
             f'<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    c.define(f'<clipPath id="disc"><circle cx="{num(cx)}" cy="{num(cy)}" r="{num(r)}"/></clipPath>')
    c.define('<filter id="soft" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="5"/></filter>')

    out = [f'<circle class="glow" cx="{num(cx)}" cy="{num(cy)}" r="{num(r * 1.75)}" fill="url(#glow)"/>']
    body = [f'<path d="{poly_path(fc["poly"], cx, cy)}" fill="{facet_fill(fc)}"/>' for fc in m["facets"]]
    edges = "".join(f'<path d="{poly_path(fc["poly"], cx, cy)}"/>' for fc in m["facets"])
    body.append(f'<g fill="none" stroke="{facet_ink}" stroke-opacity="{t["facet_op"]}" stroke-width="0.6" '
                f'stroke-linejoin="round">{edges}</g>')
    night_side = shadow_path(cx, cy, r + 2, phase["lit"], phase["waning"])
    if t["night"]:
        body.append(f'<path d="{night_side}" fill="{t["bg"]}" fill-opacity="0.62" filter="url(#soft)"/>')
    else:  # engraved: hatching plus a faint wash
        c.define(f'<pattern id="hatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
                 f'<rect width="1" height="4" fill="{t["accent"]}" fill-opacity="0.32"/></pattern>')
        body.append(f'<path d="{night_side}" fill="url(#hatch)" filter="url(#soft)"/>'
                    f'<path d="{night_side}" fill="{t["accent"]}" fill-opacity="0.07" filter="url(#soft)"/>')
    body.append(f'<path d="{poly_path(m["shard"]["poly"], cx, cy)}" fill="{t["bg"]}" stroke="{rim_ink}" '
                f'stroke-opacity="0.5" stroke-width="0.8" stroke-linejoin="round"/>')
    for k, crack in enumerate(m["cracks"]):
        d = "M" + "L".join(f"{num(x + cx, 1)},{num(y + cy, 1)}" for x, y in crack)
        body.append(f'<path class="crack" style="animation-delay:{0.8 + k * 0.25:.2f}s" pathLength="1" d="{d}" '
                    f'fill="none" stroke="{crack_ink}" stroke-opacity="0.6" stroke-width="0.8" stroke-linecap="round"/>')
    body.append(f'<g class="glint"><rect x="{num(cx - 30)}" y="{num(cy - r - 40)}" width="60" height="{num(2 * r + 80)}" '
                f'fill="url(#glint)" transform="rotate(22 {num(cx)} {num(cy)})"/></g>')
    out.append(f'<g id="moon" clip-path="url(#disc)">{"".join(body)}</g>')
    out.append(f'<circle cx="{num(cx)}" cy="{num(cy)}" r="{num(r)}" fill="none" stroke="{rim_ink}" '
               f'stroke-opacity="{t["rim_op"]}"/>')

    # the piece of glass that came away, drifting off the upper-left limb
    sx, sy = m["shard"]["c"]
    local = [(x - sx, y - sy) for x, y in m["shard"]["poly"]]
    out.append(f'<g transform="translate({num(cx + sx - 36)} {num(cy + sy - 40)})"><g class="shard">'
               f'<path transform="rotate(-14) scale(1.15)" d="{poly_path(local)}" fill="{ramp(stops, 0.95)}" '
               f'stroke="{rim_ink}" stroke-opacity="0.8" stroke-width="0.8" stroke-linejoin="round"/></g></g>')
    return "".join(out)


# --------------------------------------------------------------------------- assets


def sky_backdrop(c: Canvas, height: float) -> None:
    """The panel shared by the hero and the calendar: a night gradient, or warm paper with a little grain."""
    t = c.t
    c.define(f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{t["bg"]}"/>'
             f'<stop offset="1" stop-color="{t["bg2"]}"/></linearGradient>')
    c.add(f'<rect width="{W}" height="{num(height)}" fill="url(#sky)"/>')
    if not t["night"]:
        c.define('<filter id="grain"><feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" seed="9" '
                 'stitchTiles="stitch"/><feColorMatrix values="0 0 0 0 0.35 0 0 0 0 0.3 0 0 0 0 0.22 0 0 0 0.09 0"/></filter>')
        c.add(f'<rect width="{W}" height="{num(height)}" filter="url(#grain)"/>')


def build_hero(f: Fonts, t: dict, cfg: dict, live: dict, phase: dict) -> str:
    c = Canvas(f, t)
    H = 300
    ident, doss = cfg["identity"], cfg["dossier"]
    mx, my = 548, 150                  # moon centre, between the tagline and the card
    cx, cy, cw, ch = 606, 40, 234, 222  # the card, laid over the moon's right limb

    sky_backdrop(c, H)
    if t["night"]:  # a few faint stars, kept clear of the moon and the card
        rnd = random.Random(3)
        stars = []
        for i in range(70):
            x, y = rnd.uniform(8, W - 8), rnd.uniform(8, H - 8)
            size, alpha = rnd.choice((0.6, 0.8, 1.1)), rnd.uniform(0.12, 0.55)
            dur, delay = rnd.uniform(3, 6), rnd.uniform(0, 3)
            if math.dist((x, y), (mx, my)) < MOON["r"] * 1.35 or (cx - 6 < x < cx + cw + 6 and cy - 6 < y < cy + ch + 6):
                continue
            tw = f' class="tw" style="animation-duration:{dur:.1f}s;animation-delay:{delay:.1f}s"' if i % 9 == 0 else ""
            stars.append(f'<circle{tw} cx="{num(x, 1)}" cy="{num(y, 1)}" r="{size}" fill="#dfe8ff" opacity="{alpha:.2f}"/>')
        c.add("".join(stars))
    c.add(c.fade(draw_moon(c, mx, my, phase), 0.15))

    # Left column: accent rule, eyebrow, handle, tagline.
    c.add(c.accent_rule(PAD, 44, t["accent"], animate=True))
    c.add(c.fade(c.eyebrow(ident["eyebrow"], PAD, 70, t["label"]), 0.10))
    c.add(c.fade(c.text(f.display, ident["name"], PAD - 2, 142, 62, t["text"]), 0.22))
    lines = ident["tagline"]
    c.add(c.fade("".join(c.text(f.serif_regular, line, PAD, 186 + i * 28, 21, t["soft"])
                         for i, line in enumerate(lines)), 0.34))

    # A vermilion seal pressed beside the handle, its edge roughened like a stamp.
    ink = t["bg"] if t["night"] else "#fff8f0"
    sx = PAD - 2 + f.display.width(ident["name"], 62) + 22
    c.define('<filter id="stamp" x="-10%" y="-10%" width="120%" height="120%"><feTurbulence type="fractalNoise" '
             'baseFrequency="0.9" numOctaves="2" seed="4"/><feDisplacementMap in="SourceGraphic" scale="1.8"/></filter>')
    c.add(c.fade(f'<g transform="translate({num(sx)} 104) rotate(-5)" filter="url(#stamp)">'
                 f'<rect width="32" height="32" rx="3" fill="{t["red"]}" fill-opacity="0.92"/>'
                 f'<rect x="3" y="3" width="26" height="26" rx="1.5" fill="none" stroke="{ink}" stroke-opacity="0.85"/>'
                 f'{c.text(f.mincho_bold, ident["seal"], 16, 23.6, 20, ink, anchor="middle")}</g>', 0.5))
    # The epigraph: Japanese in Mincho, its English gloss in the serif.
    ja = c.text(f.mincho, ident["epigraph_ja"], PAD, 258, 13, t["muted"], tracking=0.2, features={"palt": True})
    gx = PAD + f.mincho.width(ident["epigraph_ja"], 13, 0.2, {"palt": True}) + 12
    c.add(c.fade(ja + c.text(f.serif_regular, ident["epigraph_en"], gx, 258, 13.5, t["muted"]), 0.6))

    # Right column: a frosted-glass card of public GitHub numbers. The moon behind
    # it is redrawn blurred inside the card's outline, then veiled by the surface.
    c.define(f'<clipPath id="card"><rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="2"/></clipPath>')
    c.define('<filter id="frost" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="7"/></filter>')
    card = ['<g clip-path="url(#card)"><g filter="url(#frost)"><use href="#moon"/></g></g>',
            f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" rx="2" fill="{t["surface"]}" '
            f'fill-opacity="{t["card_op"]}" stroke="{t["card_stroke"]}" stroke-opacity="{t["card_stroke_op"]}"/>']
    ix, iy = cx + 20, cy + 32
    card.append(c.eyebrow(doss["eyebrow"], ix, iy, t["muted"], size=10))
    card.append(c.text(f.serif, doss["title"], ix, iy + 26, 16, t["text"]))
    card.append(c.text(f.sans, doss["detail"], ix, iy + 46, 12, t["muted"]))
    card.append(c.hrule(iy + 60, t["rule"], ix, cx + cw - 20))

    gh = live.get("github_user") or {}
    contrib = live.get("contributions") or {}
    rows = [("Repositories", f"{gh['public_repos']} public · {gh['stars']} stars" if gh else "—"),
            ("Languages", " · ".join(gh.get("languages", [])[:3]) or "—")]
    if contrib.get("total") is not None:
        rows.append(("Past year", f"{contrib['total']:,} contributions"))
    rows.append(("Moon", f"{phase['name']} · {round(phase['lit'] * 100)}%"))
    rows.append(("Synced", pretty_date(live["changed_at"])))
    ry = iy + 82
    for label, value in rows:
        card.append(c.eyebrow(label, ix, ry, t["muted"], size=9.5))
        colour = t["red"] if label == "Moon" else t["text"]
        card.append(c.text(f.sans_medium, value, cx + cw - 20, ry, 12.5, colour, anchor="end", features=TABULAR))
        ry += 22
    c.add(c.fade("".join(card), 0.30))

    return c.render(H, f"{ident['name']} — {' '.join(lines)}")


def shard_glyph(x: float, y: float, colour: str) -> str:
    """A small two-faceted piece of glass plus a short rule: the mark before each section label.
    One face is lit and one is in shade, which keeps it reading as glass rather than an arrow."""
    lit = [(0, 5), (5, 0), (7, 10)]
    shade = [(5, 0), (12, 3.5), (7, 10)]
    return (f'<path d="{poly_path(lit, x, y)}" fill="{colour}"/>'
            f'<path d="{poly_path(shade, x, y)}" fill="{colour}" fill-opacity="0.5"/>'
            f'<rect x="{num(x + 18)}" y="{num(y + 4.25)}" width="18" height="1.5" fill="{colour}" fill-opacity="0.6"/>')


def build_label(f: Fonts, t: dict, section: dict) -> str:
    c = Canvas(f, t)
    c.add(shard_glyph(PAD, 10, t["accent"]))
    c.add(c.eyebrow(section["eyebrow"], PAD, 40, t["label"]))
    # heading sits in the second column, but never closer than 36px to a long eyebrow
    x = max(220, PAD + f.sans_bold.width(section["eyebrow"].upper(), 11.5, 0.13) + 36)
    c.add(c.text(f.serif, section["heading"], x, 44, 28, t["text"]))
    return c.render(60, f"{section['eyebrow']} — {section['heading']}")


def build_interests(f: Fonts, t: dict, cfg: dict) -> str:
    c = Canvas(f, t)
    H = 150
    items = cfg["research"]["interests"]
    gap = 24
    col = (W - 2 * PAD - gap * (len(items) - 1)) / len(items)
    for i, item in enumerate(items):
        x = PAD + i * (col + gap)
        c.add(c.hrule(8, t["rule2"], x, x + col))
        c.add(c.text(f.sans_medium, f"{i + 1:02d}", x, 34, 11, t["accent"]))
        c.add(c.text(f.serif, item, x, 66, 23, t["text"]))
    c.add(c.hrule(96, t["rule"]))
    c.add(c.eyebrow("Related interests", PAD, 128, t["muted"], size=10))
    x = 220
    for item in cfg["research"]["related"]:
        c.add(c.text(f.serif_regular, item, x, 130, 16, t["soft"]))
        x += f.serif_regular.width(item, 16) + 32
    return c.render(H, "Research interests: " + ", ".join(items))


def build_project(f: Fonts, t: dict, index: int, project: dict, live: dict, last: bool) -> str:
    """One project row. Rows share rules: each draws only its top rule, and the last
    one also closes the list, so stacked cards never show a doubled line."""
    c = Canvas(f, t)
    LINE = 19
    repo = (live.get("repos") or {}).get(project["repo"], {})
    lines = f.sans.wrap(project["description"], 13.5, 470)
    H = 92 + LINE * (len(lines) - 1)
    c.add(c.hrule(0, t["rule"]))
    c.add(c.text(f.sans_medium, f"{index:02d}", PAD, 38, 11, t["accent"]))
    c.add(c.text(f.serif, project["title"], 90, 40, 20, t["text"]))

    # status pill, top right; an active project's pill carries a slowly pulsing vermilion dot
    status = project.get("status", "").upper()
    if status:
        live_dot = status == "ACTIVE"
        pw = f.sans_bold.width(status, 9.5, 0.13) + 20 + (12 if live_dot else 0)
        px = RIGHT - pw
        c.add(f'<rect x="{num(px)}" y="24" width="{num(pw)}" height="20" rx="2" fill="none" stroke="{t["rule2"]}"/>')
        if live_dot:
            c.style("@media (prefers-reduced-motion:no-preference){.pulse{animation:pulse 2.4s ease-in-out infinite}"
                    "@keyframes pulse{50%{opacity:.25}}}")
            c.add(f'<circle class="pulse" cx="{num(px + 13)}" cy="34" r="3" fill="{t["red"]}"/>')
        c.add(c.eyebrow(status, px + (22 if live_dot else 10), 38, t["accent"], size=9.5))

    # meta line, bottom right: language dot + language · stars · updated
    meta = []
    if repo.get("language"):
        meta.append(repo["language"])
    if "stars" in repo:
        meta.append(f"{repo['stars']} stars")
    if repo.get("pushed_at"):
        meta.append("updated " + pretty_date(repo["pushed_at"]))
    meta_text = " · ".join(meta)
    if meta_text:
        meta_w = f.sans.width(meta_text, 12, features=TABULAR)
        c.add(c.text(f.sans, meta_text, RIGHT, 66, 12, t["muted"], anchor="end", features=TABULAR))
        if repo.get("language"):
            dot = LANG_COLOURS.get(repo["language"], t["muted"])
            c.add(f'<circle cx="{num(RIGHT - meta_w - 12)}" cy="62" r="4.5" fill="{dot}"/>')

    for j, line in enumerate(lines):
        c.add(c.text(f.sans, line, 90, 66 + j * LINE, 13.5, t["soft"]))
    if last:
        c.add(c.hrule(H - 1, t["rule"]))
    return c.render(H, f"{project['title']} — {project['description']}")


def build_log(f: Fonts, t: dict, cfg: dict, live: dict) -> str | None:
    """Recent public commits and merged pull requests, newest first."""
    entries = (live.get("activity") or [])[:LOG_ROWS]
    if not entries:
        return None
    c = Canvas(f, t)
    ROW = 66
    login = cfg["links"]["github"]
    for i, e in enumerate(entries):
        top = i * ROW
        owner, name = e["repo"].split("/", 1)
        repo_label = name if owner == login else e["repo"]
        text = e["text"] if f.sans.supports(e["text"]) else e["fallback"]
        c.add(c.hrule(top, t["rule"]))
        c.add(c.text(f.sans_medium, pretty_date(e["date"], year=False), PAD, top + 28, 12, t["muted"],
                     features=TABULAR))
        c.add(c.text(f.serif, repo_label, 130, top + 28, 17, t["text"]))
        c.add(c.text(f.sans, f.sans.ellipsize(text, 13.5, 520), 130, top + 50, 13.5, t["soft"]))
        c.add(c.text(f.sans, e["kind"], RIGHT, top + 28, 12, t["muted"], anchor="end"))
    H = ROW * len(entries) + 1
    c.add(c.hrule(H - 1, t["rule"]))
    return c.render(H, "Recent public activity: " + "; ".join(f"{e['repo']}: {e['text']}" for e in entries))


SKY_CSS = (
    "@media (prefers-reduced-motion:no-preference){"
    ".col{animation:appear .7s ease-out both}"
    ".links{animation:appear 1.2s ease-out 1.1s both}"
    ".tw{animation:twinkle 4.5s ease-in-out infinite}"
    "@keyframes appear{from{opacity:0}}"
    "@keyframes twinkle{50%{opacity:.35}}}"
)


def build_sky(f: Fonts, t: dict, live: dict) -> str | None:
    """A year of contributions as a night sky: one star per day, brighter on busier days,
    with days close together joined into constellations and today ringed in vermilion."""
    cal = live.get("calendar")
    if not cal or not cal["counts"]:
        return None
    c = Canvas(f, t)
    c.style(SKY_CSS)
    H = 228
    counts = cal["counts"]
    start = dt.date.fromisoformat(cal["start"])
    first_row = (start.weekday() + 1) % 7  # GitHub's weeks run Sunday to Saturday
    n_weeks = (first_row + len(counts) + 6) // 7
    x0, y0, dy = PAD + 8, 40, 16
    dx = min(15.0, (RIGHT - 8 - x0) / max(n_weeks - 1, 1))
    peak = max(counts) or 1
    night = t["night"]
    sky_backdrop(c, H)

    days = []
    for i, n in enumerate(counts):
        k = first_row + i
        days.append((start + dt.timedelta(days=i), k // 7, x0 + (k // 7) * dx, y0 + (k % 7) * dy, n))
    active = [d for d in days if d[4]]

    links = "".join(f'<line x1="{num(a[2], 1)}" y1="{num(a[3], 1)}" x2="{num(b[2], 1)}" y2="{num(b[3], 1)}"/>'
                    for a, b in zip(active, active[1:]) if (b[0] - a[0]).days <= 3)
    c.add(f'<g class="links" stroke="{t["label"]}" stroke-opacity="{0.22 if night else 0.3}" stroke-width="0.7">{links}</g>')

    if night:
        c.define(f'<radialGradient id="halo"><stop offset="0" stop-color="#e6eeff" stop-opacity="0.75"/>'
                 f'<stop offset="0.35" stop-color="{t["accent"]}" stop-opacity="0.28"/>'
                 f'<stop offset="1" stop-color="{t["accent"]}" stop-opacity="0"/></radialGradient>')
    bright = sorted(counts, reverse=True)[min(4, len(counts) - 1)]  # the five busiest days twinkle
    columns: dict[int, list[str]] = {}
    for day, col, x, y, n in days:
        marks = columns.setdefault(col, [])
        if not n:
            marks.append(f'<circle cx="{num(x, 1)}" cy="{num(y, 1)}" r="0.9" fill="{t["rule"] if night else "#ddd6c9"}"/>')
            continue
        v = n / peak
        r = 1.4 + 4.2 * math.sqrt(v)
        if night:
            tw = f' class="tw" style="animation-delay:{(col % 5) * 0.7:.1f}s"' if n >= bright else ""
            marks.append(f'<circle{tw} cx="{num(x, 1)}" cy="{num(y, 1)}" r="{num(r * 2.6, 1)}" fill="url(#halo)"/>'
                         f'<circle cx="{num(x, 1)}" cy="{num(y, 1)}" r="{num(r * 0.55, 1)}" fill="{ramp(["#8fb2ee", "#ffffff"], v + 0.3)}"/>')
            spark = "#eaf1ff"
        else:
            marks.append(f'<circle cx="{num(x, 1)}" cy="{num(y, 1)}" r="{num(r * 0.72, 1)}" fill="{t["accent"]}" '
                         f'fill-opacity="{0.35 + 0.65 * v:.2f}"/>')
            spark = t["accent"]
        if v >= 0.4:  # a four-point glint on the brightest stars
            arm = r * 1.9
            marks.append(f'<path d="M{num(x - arm, 1)},{num(y, 1)}H{num(x + arm, 1)}M{num(x, 1)},{num(y - arm, 1)}V{num(y + arm, 1)}" '
                         f'stroke="{spark}" stroke-opacity="0.55" stroke-width="0.7"/>')
    # columns fade in from left to right, like the sky darkening
    c.add("".join(f'<g class="col" style="animation-delay:{col * 0.02:.2f}s">{"".join(m)}</g>'
                  for col, m in sorted(columns.items())))
    _, _, tx, ty, _ = days[-1]
    c.add(f'<circle cx="{num(tx, 1)}" cy="{num(ty, 1)}" r="5" fill="none" stroke="{t["red"]}" stroke-width="1.2"/>')

    # each month is labelled above the week of its first Sunday, so a few stray days of a
    # month at the start of the window get no label to crowd the next one
    for col in range(n_weeks):
        sunday = col * 7 - first_row
        if 0 <= sunday < len(counts) and (day := start + dt.timedelta(days=sunday)).day <= 7:
            c.add(c.text(f.sans_medium, day.strftime("%b").upper(), x0 + col * dx - 3, y0 + 6 * dy + 26, 10,
                         t["muted"], tracking=0.08))

    total = (live.get("contributions") or {}).get("total", sum(counts))
    c.add(c.hrule(178, t["rule2"] if night else t["rule"]))
    x = PAD
    c.add(c.text(f.display, f"{total:,}", x, 208, 22, t["label"], features=TABULAR))
    x += f.display.width(f"{total:,}", 22, features=TABULAR) + 6
    c.add(c.text(f.serif, "contributions", x, 208, 17, t["text"]))
    x += f.serif.width("contributions", 17) + 18
    c.add(c.text(f.sans, f"{len(active)} active days · brightest {peak}", x, 207, 12.5, t["muted"], features=TABULAR))
    tw_ = f.sans.width("today", 12)
    c.add(c.text(f.sans, "today", RIGHT, 207, 12, t["muted"], anchor="end"))
    c.add(f'<circle cx="{num(RIGHT - tw_ - 9, 1)}" cy="203" r="4" fill="none" stroke="{t["red"]}" stroke-width="1.2"/>')
    c.add(c.text(f.sans, "one star per day ·", RIGHT - tw_ - 19, 207, 12, t["muted"], anchor="end"))
    return c.render(H, f"Contributions over the past year: {total:,} on {len(active)} days, drawn as a night sky")


# --------------------------------------------------------------------------- main


def main() -> None:
    cfg = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
    fonts = Fonts()
    japanese = cfg["identity"]["seal"] + cfg["identity"]["epigraph_ja"]
    if not (fonts.mincho.supports(japanese) and fonts.mincho_bold.supports(japanese)):
        sys.exit("profile.toml has Japanese the Mincho subset lacks; run: python scripts/prepare_fonts.py ShipporiMincho")
    live = fetch_live(cfg)
    phase = moon_phase(dt.datetime.now(dt.timezone.utc).date())
    ASSET_DIR.mkdir(exist_ok=True)

    expected: set[str] = set()
    written = 0
    for theme_name, t in THEMES.items():
        files = {
            f"hero-{theme_name}.svg": build_hero(fonts, t, cfg, live, phase),
            f"interests-{theme_name}.svg": build_interests(fonts, t, cfg),
        }
        log = build_log(fonts, t, cfg, live)
        if log:
            files[f"log-{theme_name}.svg"] = log
        sky = build_sky(fonts, t, live)
        if sky:
            files[f"sky-{theme_name}.svg"] = sky
        for section in cfg["sections"]:
            files[f"label-{section['id']}-{theme_name}.svg"] = build_label(fonts, t, section)
        projects = cfg["projects"]
        for i, project in enumerate(projects, start=1):
            files[f"project-{i}-{theme_name}.svg"] = build_project(fonts, t, i, project, live,
                                                                    last=i == len(projects))
        for name, content in files.items():
            expected.add(name)
            path = ASSET_DIR / name
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8", newline="\n")
                written += 1
    # keep the log and sky assets if the API was unreachable, drop anything else no longer configured
    for stale in ASSET_DIR.glob("*.svg"):
        if stale.name not in expected and not stale.name.startswith(("log-", "sky-")):
            stale.unlink()
            print(f"removed stale {stale.name}")
    print(f"built {written} changed asset(s) into {ASSET_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
