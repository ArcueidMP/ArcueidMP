#!/usr/bin/env python3
"""Build the SVG assets for the ArcueidMP profile README.

GitHub READMEs allow no CSS and no web fonts, so every piece of text here is
shaped with HarfBuzz and written out as vector outlines in Source Serif 4 and
Inter. Each glyph outline is defined once
per asset and placed with <use>, which keeps the files small. Every asset is
emitted twice, for GitHub's light and dark colour schemes, and the README
switches between them with <picture>.

Live numbers (repository stars, languages, contributions, recent public
commits and merged pull requests) come from the GitHub API at build time and
are cached in data/live.json so an API outage never produces an empty card.
Only public data is ever requested. The 3D contribution map is not built
here; it comes from yoshi389111/github-profile-3d-contrib in the same workflow.

Usage:  python scripts/build.py            # edit profile.toml first
"""
from __future__ import annotations

import datetime as dt
import json
import os
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

# Colour tokens: warm paper / charcoal with a blue accent, in light and dark.
THEMES = {
    "light": {
        "bg": "#f6f3ec", "surface": "#fffdf8", "text": "#17191d", "soft": "#33373c",
        "muted": "#5a5d60", "accent": "#244a91", "label": "#244a91",
        "rule": "#d5cec1", "rule2": "#aaa397",
    },
    "dark": {
        "bg": "#16171b", "surface": "#212228", "text": "#eae7e0", "soft": "#c9c6c0",
        "muted": "#a09d97", "accent": "#8fb2ee", "label": "#b3cbf5",
        "rule": "#33343b", "rule2": "#6a6d79",
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


class Canvas:
    """One SVG asset: collects glyph definitions and body markup."""

    def __init__(self, f: Fonts, t: dict):
        self.f = f
        self.t = t
        self.defs: dict[str, str] = {}
        self.body: list[str] = []

    def add(self, markup: str) -> None:
        self.body.append(markup)

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

    @staticmethod
    def crescent(cx: float, cy: float, r: float, colour: str, bg: str) -> str:
        """A small waning moon: a disc with a second disc of the background colour biting into it."""
        return (f'<circle cx="{num(cx)}" cy="{num(cy)}" r="{num(r)}" fill="{colour}"/>'
                f'<circle cx="{num(cx + r * 0.5)}" cy="{num(cy - r * 0.35)}" r="{num(r * 0.86)}" fill="{bg}"/>')

    # -- output -------------------------------------------------------------
    def render(self, height: float, title: str, bg: str | None = None) -> str:
        rect = f'<rect width="{W}" height="{num(height)}" fill="{bg}"/>' if bg else ""
        defs = "".join(d for d in self.defs.values() if d)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{num(height)}" '
            f'viewBox="0 0 {W} {num(height)}" role="img" aria-label="{escape(title)}">'
            f"<title>{escape(title)}</title><defs>{defs}</defs>{rect}{''.join(self.body)}</svg>\n"
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
      contributionCalendar { totalContributions }
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
PR_SUFFIX = re.compile(r"\s*\(#\d+\)$")


def recent_activity(login: str, data: dict) -> list[dict]:
    """Merge public commits and merged pull requests into one dated list, newest first."""
    entries: list[dict] = []
    seen_titles: set[str] = set()
    for pr in data["search"]["nodes"]:
        if not pr or pr["repository"]["isPrivate"] or not pr.get("mergedAt"):
            continue
        seen_titles.add(pr["title"].strip().lower())
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
            if PR_SUFFIX.sub("", headline).lower() in seen_titles:
                continue  # the merged pull request already tells this story
            entries.append({"date": commit["committedDate"][:10], "repo": repo["nameWithOwner"],
                            "kind": "commit", "text": headline, "fallback": f"Commit {commit['oid'][:7]}"})
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries[:LOG_ROWS + 2]


def fetch_live(cfg: dict) -> dict:
    prev = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else {}
    live = {k: prev.get(k) for k in ("github_user", "repos", "contributions", "activity")}
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
        except Exception as exc:  # noqa: BLE001
            print(f"warning: activity unavailable ({exc}); using cached values", file=sys.stderr)
    else:
        print("note: no GitHub token; contributions and activity use cached values", file=sys.stderr)

    # Only move the "synced" date when a number actually changed.
    def core(d: dict) -> str:
        return json.dumps({k: d.get(k) for k in ("github_user", "repos", "contributions", "activity")},
                          sort_keys=True)

    today = dt.date.today().isoformat()
    live["changed_at"] = prev.get("changed_at", today) if core(live) == core(prev) else today
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return live


def pretty_date(iso: str, year: bool = True) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b %Y' if year else '%b')}"


# --------------------------------------------------------------------------- assets


def build_hero(f: Fonts, t: dict, cfg: dict, live: dict) -> str:
    c = Canvas(f, t)
    H = 284
    ident, doss = cfg["identity"], cfg["dossier"]

    # Left column: accent rule, eyebrow, handle, tagline.
    c.add(c.accent_rule(PAD, 44, t["accent"], animate=True))
    c.add(c.fade(c.eyebrow(ident["eyebrow"], PAD, 70, t["label"]), 0.10))
    c.add(c.fade(c.text(f.display, ident["name"], PAD - 2, 142, 62, t["text"]), 0.22))
    lines = ident["tagline"]
    c.add(c.fade("".join(c.text(f.serif_regular, line, PAD, 186 + i * 28, 21, t["soft"])
                         for i, line in enumerate(lines)), 0.34))

    # Right column: a card of public GitHub numbers.
    cx, cy, cw, ch = 596, 44, 244, 200
    card = [f'<rect x="{cx}" y="{cy}" width="{cw}" height="{ch}" fill="{t["surface"]}" stroke="{t["rule"]}"/>',
            # corner bracket
            f'<path d="M{cx + cw + 8},{cy + 28} V{cy - 8} H{cx + cw - 28}" fill="none" '
            f'stroke="{t["accent"]}" stroke-width="1.5"/>']
    ix, iy = cx + 20, cy + 32
    card.append(c.eyebrow(doss["eyebrow"], ix, iy, t["muted"], size=10))
    card.append(c.crescent(cx + cw - 30, iy - 4, 7, t["accent"], t["surface"]))
    card.append(c.text(f.serif, doss["title"], ix, iy + 26, 16, t["text"]))
    card.append(c.text(f.sans, doss["detail"], ix, iy + 46, 12, t["muted"]))
    card.append(c.hrule(iy + 60, t["rule"], ix, cx + cw - 20))

    gh = live.get("github_user") or {}
    contrib = live.get("contributions") or {}
    rows = [("Repositories", f"{gh['public_repos']} public · {gh['stars']} stars" if gh else "—"),
            ("Languages", " · ".join(gh.get("languages", [])[:3]) or "—")]
    if contrib.get("total") is not None:
        rows.append(("Past year", f"{contrib['total']:,} contributions"))
    rows.append(("Synced", pretty_date(live["changed_at"])))
    ry = iy + 82
    for label, value in rows:
        card.append(c.eyebrow(label, ix, ry, t["muted"], size=9.5))
        card.append(c.text(f.sans_medium, value, cx + cw - 20, ry, 12.5, t["text"], anchor="end", features=TABULAR))
        ry += 22
    c.add(c.fade("".join(card), 0.30))

    return c.render(H, f"{ident['name']} — {' '.join(lines)}", bg=t["bg"])


def build_label(f: Fonts, t: dict, section: dict) -> str:
    c = Canvas(f, t)
    c.add(c.accent_rule(PAD, 16, t["accent"]))
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


def build_project(f: Fonts, t: dict, index: int, project: dict, live: dict) -> str:
    c = Canvas(f, t)
    LINE = 19
    repo = (live.get("repos") or {}).get(project["repo"], {})
    lines = f.sans.wrap(project["description"], 13.5, 470)
    H = 92 + LINE * (len(lines) - 1)
    c.add(c.hrule(0, t["rule"]))
    c.add(c.text(f.sans_medium, f"{index:02d}", PAD, 38, 11, t["accent"]))
    c.add(c.text(f.serif, project["title"], 90, 40, 20, t["text"]))

    # status pill, top right
    status = project.get("status", "").upper()
    if status:
        pw = f.sans_bold.width(status, 9.5, 0.13) + 20
        px = RIGHT - pw
        c.add(f'<rect x="{num(px)}" y="24" width="{num(pw)}" height="20" rx="2" fill="none" stroke="{t["rule2"]}"/>')
        c.add(c.eyebrow(status, px + 10, 38, t["accent"], size=9.5))

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


# --------------------------------------------------------------------------- main


def main() -> None:
    cfg = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
    fonts = Fonts()
    live = fetch_live(cfg)
    ASSET_DIR.mkdir(exist_ok=True)

    expected: set[str] = set()
    written = 0
    for theme_name, t in THEMES.items():
        files = {
            f"hero-{theme_name}.svg": build_hero(fonts, t, cfg, live),
            f"interests-{theme_name}.svg": build_interests(fonts, t, cfg),
        }
        log = build_log(fonts, t, cfg, live)
        if log:
            files[f"log-{theme_name}.svg"] = log
        for section in cfg["sections"]:
            files[f"label-{section['id']}-{theme_name}.svg"] = build_label(fonts, t, section)
        for i, project in enumerate(cfg["projects"], start=1):
            files[f"project-{i}-{theme_name}.svg"] = build_project(fonts, t, i, project, live)
        for name, content in files.items():
            expected.add(name)
            path = ASSET_DIR / name
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8", newline="\n")
                written += 1
    # keep the log assets if the API was unreachable, drop anything else that is no longer configured
    for stale in ASSET_DIR.glob("*.svg"):
        if stale.name not in expected and not stale.name.startswith("log-"):
            stale.unlink()
            print(f"removed stale {stale.name}")
    print(f"built {written} changed asset(s) into {ASSET_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
