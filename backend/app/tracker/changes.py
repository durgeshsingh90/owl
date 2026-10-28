"""What changed in a Confluence page between two versions, as one standalone HTML page.

Only the added and removed lines are shown (no unchanged text). A line that was edited
shows as removed then added, with the changed words marked.
"""

import difflib
import re
from html import escape

WORDS = re.compile(r"\s+|\w+|[^\w\s]")


def marked(old, new):
    """The two lines with the words that differ wrapped in <mark>."""
    a, b = WORDS.findall(old), WORDS.findall(new)
    left, right = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        before, after = escape("".join(a[i1:i2])), escape("".join(b[j1:j2]))
        if tag == "equal":
            left.append(before)
            right.append(after)
        else:
            left.append(f"<mark>{before}</mark>" if before else "")
            right.append(f"<mark>{after}</mark>" if after else "")
    return "".join(left), "".join(right)


def hunks(before, after):
    """Groups of removed and added lines, in page order."""
    a, b = before.splitlines(), after.splitlines()
    groups = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        removed, added = a[i1:i2], b[j1:j2]
        groups.append({"line": j1 + 1, "rows": paired(removed, added), "added": len(added), "removed": len(removed)})
    return groups


def similar(old, new):
    return difflib.SequenceMatcher(None, old, new, autojunk=False).ratio() >= 0.5


def paired(removed, added):
    """Removed and added lines in order. An edited line (a removed line much like a later
    added one) is shown next to its new form with the changed words marked."""
    rows, rest = [], list(added)
    for old in removed:
        match = next((index for index, new in enumerate(rest) if similar(old, new)), None)
        if match is None:
            rows.append(("removed", escape(old)))
            continue
        rows += [("added", escape(line)) for line in rest[:match]]
        left, right = marked(old, rest[match])
        rows += [("removed", left), ("added", right)]
        rest = rest[match + 1:]
    return rows + [("added", escape(line)) for line in rest]


STYLE = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#5d6678;--line:#dde1e8;
--add-bg:#e6f6ec;--add-fg:#14532d;--add-mark:#a7e3bb;--del-bg:#fdecec;--del-fg:#7f1d1d;--del-mark:#f6b4b4;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1e26;--text:#e6e9ef;--muted:#9aa3b2;--line:#2b313c;
--add-bg:#12301f;--add-fg:#a7f3c0;--add-mark:#1f6b3d;--del-bg:#3a1618;--del-fg:#fecaca;--del-mark:#8a2a2e;--accent:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:28px 16px 60px}
.eyebrow{margin:0;color:var(--muted);font-size:12px;letter-spacing:.08em;text-transform:uppercase}
h1{margin:4px 0 6px;font-size:24px;line-height:1.25;overflow-wrap:anywhere}
.meta{color:var(--muted);margin:0 0 14px}.meta a{color:var(--accent)}
.summary{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 20px}
.pill{border-radius:999px;padding:3px 10px;font-weight:600;font-size:13px}
.pill.added{background:var(--add-bg);color:var(--add-fg)}.pill.removed{background:var(--del-bg);color:var(--del-fg)}
.actions{margin-left:auto;display:flex;gap:8px}
.actions a,.actions button{font:inherit;font-size:13px;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 12px;text-decoration:none;cursor:pointer}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;color:var(--muted);margin:0 0 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 14px;overflow:hidden}
section h2{margin:0;padding:8px 14px;font-size:12px;font-weight:600;color:var(--muted);border-bottom:1px solid var(--line)}
.row{display:grid;grid-template-columns:28px 1fr;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.row span{padding:2px 0;text-align:center;font-weight:700;user-select:none}
.row div{padding:2px 12px 2px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.row.added{background:var(--add-bg);color:var(--add-fg)}.row.removed{background:var(--del-bg);color:var(--del-fg)}
.row.added mark{background:var(--add-mark);color:inherit;border-radius:3px}.row.removed mark{background:var(--del-mark);color:inherit;border-radius:3px;text-decoration:line-through}
.empty{padding:28px;text-align:center;color:var(--muted)}
@media print{.actions{display:none}section{break-inside:avoid}}
"""


def render(*, title, url, before_label, after_label, before, after, updated, note="", download_url=""):
    groups = hunks(before, after)
    added = sum(group["added"] for group in groups)
    removed = sum(group["removed"] for group in groups)
    body = []
    for group in groups:
        rows = "".join(
            f'<div class="row {kind}"><span>{"+" if kind == "added" else "−"}</span><div>{text or " "}</div></div>'
            for kind, text in group["rows"]
        )
        body.append(f'<section><h2>Near line {group["line"]}</h2>{rows}</section>')
    if not groups:
        body.append('<p class="note empty">No text was added or removed between these versions.</p>')
    actions = '<button type="button" onclick="print()">Print / PDF</button>'
    if download_url:
        actions = f'<a href="{escape(download_url)}">Download</a>' + actions
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Changes · {escape(title)}</title><style>{STYLE}</style></head>
<body><main>
<p class="eyebrow">What changed · {escape(before_label)} → {escape(after_label)}</p>
<h1>{escape(title)}</h1>
<p class="meta">{escape(updated)} · <a href="{escape(url)}" target="_blank" rel="noopener noreferrer">Open in Confluence ↗</a></p>
<div class="summary"><span class="pill added">+{added} added</span><span class="pill removed">−{removed} removed</span><div class="actions">{actions}</div></div>
{f'<p class="note">{escape(note)}</p>' if note else ""}
{"".join(body)}
</main></body></html>"""
