#!/usr/bin/env python3
"""Render README.md into .preview.html so the profile can be checked locally.

The README is mostly raw HTML already; this converts the handful of Markdown
constructs it uses (paragraphs, links, bold) and shows the light and dark
variants side by side on GitHub's page colours.

    python scripts/preview.py && python -m http.server 8765
    -> http://localhost:8765/.preview.html
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INLINE = [
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
]


def markdown_to_html(md: str) -> str:
    blocks = re.split(r"\n\s*\n", md.strip())
    out = []
    for block in blocks:
        text = block.strip()
        for pattern, repl in INLINE:
            text = pattern.sub(repl, text)
        if text.startswith("<"):
            out.append(text)
        else:
            out.append(f"<p>{text}</p>")
    return "\n".join(out)


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>README preview</title>
<style>
  body {{ margin: 0; display: grid; grid-template-columns: 1fr;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
         font-size: 16px; line-height: 1.5; word-wrap: break-word; }}
  @media (min-width: 1800px) {{ body {{ grid-template-columns: 1fr 1fr; }} }}
  .pane {{ padding: 24px; }}
  .light {{ background: #ffffff; color: #1f2328; }}
  .dark  {{ background: #0d1117; color: #e6edf3; }}
  .light a {{ color: #0969da; }} .dark a {{ color: #4493f8; }}
  .markdown-body {{ max-width: 896px; margin: 0 auto; border: 1px solid #d0d7de; border-radius: 6px; padding: 32px; }}
  .dark .markdown-body {{ border-color: #3d444d; }}
  .markdown-body p {{ margin: 0 0 16px; }}
  .markdown-body picture, .markdown-body sub, .markdown-body a > picture {{ display: block; margin-bottom: 16px; }}
  .markdown-body img {{ max-width: 100%; box-sizing: content-box; vertical-align: middle; }}
  .markdown-body a {{ text-decoration: none; }}
</style>
<div class="pane light"><div class="markdown-body">{light}</div></div>
<div class="pane dark"><div class="markdown-body">{dark}</div></div>
"""


def main() -> None:
    md = (ROOT / "README.md").read_text(encoding="utf-8")
    html = markdown_to_html(md)
    html = re.sub(r"<source[^>]*>\s*", "", html)
    light = html
    dark = html.replace("-light.svg", "-dark.svg")
    (ROOT / ".preview.html").write_text(PAGE.format(light=light, dark=dark), encoding="utf-8")
    print("wrote .preview.html")


if __name__ == "__main__":
    main()
