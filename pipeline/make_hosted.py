"""Prepare dist/froggle.html for hosting as a claude.ai Artifact.

    py pipeline/make_hosted.py

The Artifact host wraps the page in its own <html>/<head>/<body>, so this
strips those from froggle.html and sizes the app to the host's frame.
The data/ files are published alongside it unchanged.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = (ROOT / "froggle.html").read_text(encoding="utf8")

page = re.sub(r"<!doctype html>\s*|</?html[^>]*>\s*|</?head>\s*|</?body>\s*|<meta (charset|name=\"viewport\")[^>]*>\s*",
              "", src, flags=re.I)
page = page.replace("height: 100dvh;", "height: 100%;")
page = page.replace(":root {", ":root {\n    color-scheme: dark;", 1)

out = ROOT / "dist" / "froggle.html"
out.parent.mkdir(exist_ok=True)
out.write_text(page, encoding="utf8")
print(f"Wrote {out.relative_to(ROOT)}")
