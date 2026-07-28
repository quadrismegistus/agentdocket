"""Zero-dependency web viewer for the agent docket.

Read-only. Never advances a cursor, never resolves a seat, never touches claims.
Opens the database in read-only mode so a UI bug cannot corrupt the store.

    docket serve                     # localhost:8484
    docket serve --host 0.0.0.0      # reachable over Tailscale
    docket serve --port 9000
"""
from __future__ import annotations

import json
import os
import sqlite3
from functools import partial
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from . import store

HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>docket</title>
<style>
:root {
  --bg: #f7f5f2;
  --bg-card: #ffffff;
  --bg-bar: #efede9;
  --fg: #1d1b18;
  --fg-dim: #6b6560;
  --fg-faint: #a09a93;
  --border: #ddd8d0;
  --border-card: #e4e0da;
  --accent: #5a6acf;
  --tag-bg: #eae7e2;
  --tag-fg: #4a453f;
  --mention-bg: #dfe6f7;
  --mention-fg: #3a4fa0;
  --search-bg: #ffffff;
  --shadow: 0 1px 2px rgba(0,0,0,.06);
  --mono: 'SF Mono', 'Menlo', 'Consolas', monospace;
  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1117;
    --bg-card: #181b24;
    --bg-bar: #1a1d27;
    --fg: #d4d2ce;
    --fg-dim: #8a857e;
    --fg-faint: #56524c;
    --border: #2a2d38;
    --border-card: #262a35;
    --accent: #7b8aef;
    --tag-bg: #252830;
    --tag-fg: #9a958e;
    --mention-bg: #242840;
    --mention-fg: #8b9aef;
    --search-bg: #1e2130;
    --shadow: 0 1px 3px rgba(0,0,0,.3);
  }
}
:root[data-theme="dark"] {
  --bg: #0f1117;
  --bg-card: #181b24;
  --bg-bar: #1a1d27;
  --fg: #d4d2ce;
  --fg-dim: #8a857e;
  --fg-faint: #56524c;
  --border: #2a2d38;
  --border-card: #262a35;
  --accent: #7b8aef;
  --tag-bg: #252830;
  --tag-fg: #9a958e;
  --mention-bg: #242840;
  --mention-fg: #8b9aef;
  --search-bg: #1e2130;
  --shadow: 0 1px 3px rgba(0,0,0,.3);
}
:root[data-theme="light"] {
  --bg: #f7f5f2;
  --bg-card: #ffffff;
  --bg-bar: #efede9;
  --fg: #1d1b18;
  --fg-dim: #6b6560;
  --fg-faint: #a09a93;
  --border: #ddd8d0;
  --border-card: #e4e0da;
  --accent: #5a6acf;
  --tag-bg: #eae7e2;
  --tag-fg: #4a453f;
  --mention-bg: #dfe6f7;
  --mention-fg: #3a4fa0;
  --search-bg: #ffffff;
  --shadow: 0 1px 2px rgba(0,0,0,.06);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--fg);
  line-height: 1.5;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  background: var(--bg-bar);
  border-bottom: 1px solid var(--border);
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.topbar-title {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
  letter-spacing: 0.04em;
  white-space: nowrap;
}

.topbar-stats {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--fg-faint);
  white-space: nowrap;
}

.search-box {
  flex: 1;
  min-width: 180px;
  max-width: 400px;
}

.search-box input {
  width: 100%;
  font-family: var(--mono);
  font-size: 13px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--search-bg);
  color: var(--fg);
  outline: none;
  transition: border-color .15s;
}
.search-box input:focus {
  border-color: var(--accent);
}
.search-box input::placeholder {
  color: var(--fg-faint);
}

.filters {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.filter-btn {
  font-family: var(--mono);
  font-size: 11px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: transparent;
  color: var(--fg-dim);
  cursor: pointer;
  transition: all .15s;
}
.filter-btn:hover { border-color: var(--accent); color: var(--accent); }
.filter-btn.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.controls {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}

.toggle-btn {
  font-family: var(--mono);
  font-size: 11px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: transparent;
  color: var(--fg-dim);
  cursor: pointer;
  transition: all .15s;
}
.toggle-btn:hover { border-color: var(--accent); color: var(--accent); }
.toggle-btn.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}

.stream {
  max-width: 960px;
  margin: 0 auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.msg {
  background: var(--bg-card);
  border: 1px solid var(--border-card);
  border-left: 3px solid var(--accent);
  border-radius: 4px;
  padding: 10px 14px;
  box-shadow: var(--shadow);
  transition: border-color .15s;
}
.msg:hover {
  border-left-color: var(--accent);
}
.msg.highlight {
  background: var(--mention-bg);
  border-left-color: var(--mention-fg);
}

.msg-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.msg-id {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--fg-faint);
  font-variant-numeric: tabular-nums;
  min-width: 3ch;
}
.msg-id a {
  color: inherit;
  text-decoration: none;
}
.msg-id a:hover {
  color: var(--accent);
}

.msg-sender {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
}

.msg-time {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--fg-faint);
  margin-left: auto;
}

.msg-tag {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--tag-bg);
  color: var(--tag-fg);
}

.msg-mentions {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--mention-fg);
}

.msg-body {
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
  color: var(--fg);
}
.msg-body p { margin: 0 0 8px 0; }
.msg-body p:last-child { margin-bottom: 0; }
.msg-body strong { font-weight: 600; }
.msg-body em { font-style: italic; }
.msg-body code {
  font-family: var(--mono);
  font-size: 12px;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--tag-bg);
  color: var(--tag-fg);
}
.msg-body pre {
  margin: 8px 0;
  padding: 10px 12px;
  border-radius: 4px;
  background: var(--tag-bg);
  overflow-x: auto;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.5;
  color: var(--fg);
}
.msg-body pre code {
  padding: 0;
  background: none;
  font-size: inherit;
  color: inherit;
}
.msg-body ul, .msg-body ol {
  margin: 4px 0 8px 20px;
  padding: 0;
}
.msg-body li { margin: 2px 0; }
.msg-body blockquote {
  border-left: 3px solid var(--border);
  margin: 8px 0;
  padding: 4px 12px;
  color: var(--fg-dim);
}
.msg-body h1, .msg-body h2, .msg-body h3 {
  font-size: 14px;
  font-weight: 700;
  margin: 12px 0 4px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--fg);
}
.msg-body hr {
  border: none;
  border-top: 1px solid var(--border);
  margin: 10px 0;
}

.msg-body .ref {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--accent);
  cursor: pointer;
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 2px;
}

.empty {
  text-align: center;
  padding: 60px 20px;
  color: var(--fg-faint);
  font-family: var(--mono);
  font-size: 13px;
}

.jump-bottom {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  border: none;
  cursor: pointer;
  font-size: 18px;
  line-height: 36px;
  text-align: center;
  box-shadow: 0 2px 8px rgba(0,0,0,.2);
  display: none;
  z-index: 20;
}
.jump-bottom:hover { opacity: .85; }
</style>
</head>
<body>

<div class="topbar">
  <span class="topbar-title">docket</span>
  <span class="topbar-stats" id="stats"></span>
  <div class="search-box">
    <input type="text" id="search" placeholder="search messages..." autocomplete="off">
  </div>
  <div class="filters" id="filters"></div>
  <div class="controls">
    <button class="toggle-btn active" id="tail-toggle" title="Auto-scroll to new messages">tail</button>
  </div>
</div>

<div class="stream" id="stream">
  <div class="empty">loading...</div>
</div>

<button class="jump-bottom" id="jump-btn" title="Jump to bottom">&#8595;</button>

<script>
const SEAT_HUES = {};
const HUE_STEPS = [215, 30, 150, 340, 75, 270, 195, 5, 120, 305];
let hueIndex = 0;

function seatHue(seat) {
  if (!SEAT_HUES[seat]) {
    SEAT_HUES[seat] = HUE_STEPS[hueIndex % HUE_STEPS.length];
    hueIndex++;
  }
  return SEAT_HUES[seat];
}

function seatColor(seat) {
  const h = seatHue(seat);
  const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches ||
                 document.documentElement.dataset.theme === 'dark';
  return isDark ? `hsl(${h}, 55%, 65%)` : `hsl(${h}, 50%, 42%)`;
}

function seatBorder(seat) {
  const h = seatHue(seat);
  const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches ||
                 document.documentElement.dataset.theme === 'dark';
  return isDark ? `hsl(${h}, 45%, 45%)` : `hsl(${h}, 45%, 55%)`;
}

function formatTime(iso) {
  try {
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const time = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    if (sameDay) return time;
    return d.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ' ' + time;
  } catch { return iso; }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function renderMarkdown(raw) {
  const esc = escapeHtml(raw);
  let html = esc;

  // fenced code blocks
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    '<pre><code>' + code.replace(/\n$/, '') + '</code></pre>');

  // indented code blocks (4+ spaces at start of consecutive lines)
  html = html.replace(/((?:^    .+\n?)+)/gm, (block) => {
    const code = block.replace(/^    /gm, '');
    return '<pre><code>' + code.replace(/\n$/, '') + '</code></pre>';
  });

  // blockquotes
  html = html.replace(/((?:^&gt; .+\n?)+)/gm, (block) => {
    const inner = block.replace(/^&gt; /gm, '');
    return '<blockquote>' + inner.trim() + '</blockquote>';
  });

  // headers
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // hr
  html = html.replace(/^---+$/gm, '<hr>');

  // bold + italic
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');

  // inline code (but not inside <pre>)
  html = html.replace(/`([^`\n]+?)`/g, '<code>$1</code>');

  // unordered lists
  html = html.replace(/((?:^[-*] .+\n?)+)/gm, (block) => {
    const items = block.trim().split('\n').map(l =>
      '<li>' + l.replace(/^[-*] /, '') + '</li>').join('\n');
    return '<ul>' + items + '</ul>';
  });

  // ordered lists
  html = html.replace(/((?:^\d+\. .+\n?)+)/gm, (block) => {
    const items = block.trim().split('\n').map(l =>
      '<li>' + l.replace(/^\d+\. /, '') + '</li>').join('\n');
    return '<ol>' + items + '</ol>';
  });

  // paragraphs: split on double newlines, wrap non-block content
  const blocks = html.split(/\n{2,}/);
  html = blocks.map(b => {
    b = b.trim();
    if (!b) return '';
    if (/^<(pre|ul|ol|blockquote|h[1-3]|hr)/.test(b)) return b;
    return '<p>' + b.replace(/\n/g, '<br>') + '</p>';
  }).join('\n');

  // message ID references
  html = html.replace(/\[(\d+)\]/g, '<span class="ref" data-id="$1">[$1]</span>');
  html = html.replace(/#(\d+)\b/g, (match, id) => {
    if (parseInt(id) > 0 && parseInt(id) < 100000)
      return '<span class="ref" data-id="' + id + '">#' + id + '</span>';
    return match;
  });

  return html;
}

function renderMsg(m) {
  const el = document.createElement('div');
  el.className = 'msg';
  el.id = 'msg-' + m.id;
  el.style.borderLeftColor = seatBorder(m.sender);

  let head = `<span class="msg-id"><a href="#msg-${m.id}" title="message ${m.id}">${m.id}</a></span>`;
  head += `<span class="msg-sender" style="color:${seatColor(m.sender)}">${escapeHtml(m.sender)}</span>`;
  if (m.tag) head += `<span class="msg-tag">${escapeHtml(m.tag)}</span>`;
  if (m.mentions && m.mentions.length)
    head += `<span class="msg-mentions">→ ${m.mentions.map(x => '@' + escapeHtml(x)).join(', ')}</span>`;
  head += `<span class="msg-time">${formatTime(m.ts)}</span>`;

  el.innerHTML = `<div class="msg-head">${head}</div><div class="msg-body">${renderMarkdown(m.body)}</div>`;

  el.querySelectorAll('.ref').forEach(r => {
    r.addEventListener('click', () => {
      const target = document.getElementById('msg-' + r.dataset.id);
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'center'});
        target.classList.add('highlight');
        setTimeout(() => target.classList.remove('highlight'), 1500);
      }
    });
  });

  return el;
}

let allMessages = [];
let maxId = 0;
let activeSeat = null;
let searchMode = false;
let tailing = true;
let knownSeats = new Set();
const stream = document.getElementById('stream');
const filtersEl = document.getElementById('filters');
const statsEl = document.getElementById('stats');
const searchInput = document.getElementById('search');
const tailBtn = document.getElementById('tail-toggle');
const jumpBtn = document.getElementById('jump-btn');

function updateFilters() {
  const seats = [...knownSeats].sort();
  filtersEl.innerHTML = '';
  seats.forEach(s => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn' + (activeSeat === s ? ' active' : '');
    btn.textContent = s;
    btn.style.borderColor = activeSeat === s ? seatColor(s) : '';
    btn.style.background = activeSeat === s ? seatColor(s) : '';
    btn.style.color = activeSeat === s ? '#fff' : '';
    btn.addEventListener('click', () => {
      activeSeat = activeSeat === s ? null : s;
      renderFiltered();
      updateFilters();
    });
    filtersEl.appendChild(btn);
  });
}

function renderFiltered() {
  let msgs = allMessages;
  if (activeSeat) msgs = msgs.filter(m => m.sender === activeSeat || (m.mentions && m.mentions.includes(activeSeat)));
  stream.innerHTML = '';
  if (!msgs.length) {
    stream.innerHTML = '<div class="empty">no messages</div>';
    return;
  }
  msgs.forEach(m => stream.appendChild(renderMsg(m)));
  if (tailing) scrollToBottom();
}

function scrollToBottom() {
  window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});
}

tailBtn.addEventListener('click', () => {
  tailing = !tailing;
  tailBtn.classList.toggle('active', tailing);
  if (tailing) scrollToBottom();
});

window.addEventListener('scroll', () => {
  const atBottom = (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 60;
  jumpBtn.style.display = atBottom ? 'none' : 'block';
  if (!atBottom && tailing) {
    tailing = false;
    tailBtn.classList.remove('active');
  }
});

jumpBtn.addEventListener('click', () => {
  tailing = true;
  tailBtn.classList.add('active');
  scrollToBottom();
});

async function poll() {
  if (searchMode) return;
  try {
    const r = await fetch('/api/messages?since=' + maxId);
    const msgs = await r.json();
    if (msgs.length) {
      allMessages.push(...msgs);
      msgs.forEach(m => {
        knownSeats.add(m.sender);
        if (m.id > maxId) maxId = m.id;
      });
      if (!activeSeat) {
        msgs.forEach(m => stream.appendChild(renderMsg(m)));
      } else {
        const filtered = msgs.filter(m => m.sender === activeSeat || (m.mentions && m.mentions.includes(activeSeat)));
        filtered.forEach(m => stream.appendChild(renderMsg(m)));
      }
      updateFilters();
      if (tailing) scrollToBottom();
    }
    const sr = await fetch('/api/stats');
    const st = await sr.json();
    statsEl.textContent = st.messages + ' messages';
  } catch {}
}

let searchTimeout;
searchInput.addEventListener('input', () => {
  clearTimeout(searchTimeout);
  const q = searchInput.value.trim();
  if (!q) {
    searchMode = false;
    renderFiltered();
    return;
  }
  searchTimeout = setTimeout(async () => {
    searchMode = true;
    try {
      const r = await fetch('/api/search?q=' + encodeURIComponent(q));
      const msgs = await r.json();
      stream.innerHTML = '';
      if (!msgs.length) {
        stream.innerHTML = '<div class="empty">no results</div>';
        return;
      }
      msgs.forEach(m => stream.appendChild(renderMsg(m)));
    } catch {}
  }, 300);
});

searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    searchInput.value = '';
    searchMode = false;
    renderFiltered();
  }
});

async function init() {
  try {
    const r = await fetch('/api/messages?tail=200');
    const msgs = await r.json();
    allMessages = msgs;
    msgs.forEach(m => {
      knownSeats.add(m.sender);
      if (m.id > maxId) maxId = m.id;
    });
    stream.innerHTML = '';
    if (!msgs.length) {
      stream.innerHTML = '<div class="empty">docket is empty</div>';
    } else {
      msgs.forEach(m => stream.appendChild(renderMsg(m)));
    }
    updateFilters();
    scrollToBottom();
  } catch (e) {
    stream.innerHTML = '<div class="empty">failed to connect</div>';
  }
  setInterval(poll, 2000);
}

// handle hash links on load
window.addEventListener('hashchange', () => {
  const id = location.hash.replace('#msg-', '');
  const el = document.getElementById('msg-' + id);
  if (el) {
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.classList.add('highlight');
    setTimeout(() => el.classList.remove('highlight'), 1500);
  }
});

init();
</script>
</body>
</html>
"""


def _ro_connect(db_path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _msg_to_dict(m: store.Message) -> dict:
    return {
        "id": m.id,
        "ts": m.ts,
        "sender": m.sender,
        "tag": m.tag,
        "body": m.body,
        "mentions": list(m.mentions),
    }


class Handler(BaseHTTPRequestHandler):
    def __init__(self, db_path, *args, **kwargs):
        self.db_path = db_path
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/" or parsed.path == "":
            self._html(HTML_PAGE)
            return

        conn = _ro_connect(self.db_path)
        try:
            if parsed.path == "/api/messages":
                since = int(qs.get("since", ["0"])[0])
                tail_n = int(qs.get("tail", ["0"])[0])

                if tail_n > 0:
                    msgs = store.tail(conn, tail_n)
                else:
                    rows = conn.execute(
                        "SELECT * FROM messages WHERE id > ? ORDER BY id",
                        (since,),
                    ).fetchall()
                    msgs = store._hydrate(conn, rows)

                self._json([_msg_to_dict(m) for m in msgs])

            elif parsed.path == "/api/search":
                q = qs.get("q", [""])[0]
                if not q:
                    self._json([])
                    return
                msgs = store.search(conn, q, limit=100)
                self._json([_msg_to_dict(m) for m in msgs])

            elif parsed.path == "/api/stats":
                try:
                    s = store.stats(conn)
                except Exception:
                    s = {"messages": 0}
                self._json(s)

            else:
                self._json({"error": "not found"}, 404)
        finally:
            conn.close()


def serve(db_path: str = store.DEFAULT_DB, host: str = "127.0.0.1", port: int = 8484):
    handler = partial(Handler, db_path)
    server = ThreadingHTTPServer((host, port), handler)
    addr = f"http://{host}:{port}"
    if host == "0.0.0.0":
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
        addr += f"  (also http://{local_ip}:{port})"
    print(f"docket viewer at {addr}")
    print(f"reading {db_path} (read-only)")
    print("ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.server_close()
