"""
Visual identity and small HTML building blocks shared by every page.

Palette (dark navy arena, electric blue + ultraviolet accents):
  night  #0A0F1F  page background         deck  #121A2E  panels
  rail   #1F2A44  borders, tracks          ink   #E6ECFF  text      mist #8F9BBA  secondary text
  volt   #3D8BFF  primary actions, calm countdown
  violet #8A5CFF  secondary accent, tense countdown
  status: green #2FD18B, amber #FFB547, red #FF5470

Type: Space Grotesk for the interface (tabular numbers for scores and the clock),
JetBrains Mono only for code snippets. System fonts are used if Google Fonts is unreachable.

Every string that comes from the network (usernames, room names...) goes through esc().
"""

from __future__ import annotations

import hashlib
import html
import math

import streamlit as st

AVATAR_COLORS = ("#3D8BFF", "#8A5CFF", "#2FD18B", "#FFB547", "#FF6FB5", "#3DD6F5")
TONE_ICONS = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "⛔"}

CSS = """
<style>
/* Fonts ship with the project (static/fonts, SIL Open Font License) so the LAN demo needs no internet. */
@font-face { font-family: 'Space Grotesk'; src: url('app/static/fonts/SpaceGrotesk.ttf') format('truetype');
  font-weight: 300 700; font-display: swap; }
@font-face { font-family: 'JetBrains Mono'; src: url('app/static/fonts/JetBrainsMono.ttf') format('truetype');
  font-weight: 100 800; font-display: swap; }

:root {
  --night:#0A0F1F; --deck:#121A2E; --deck2:#161F37; --rail:#1F2A44; --ink:#E6ECFF; --mist:#8F9BBA;
  --volt:#3D8BFF; --violet:#8A5CFF; --green:#2FD18B; --amber:#FFB547; --red:#FF5470;
  --ui:'Space Grotesk', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --code:'JetBrains Mono', ui-monospace, Consolas, monospace;
}
/* Code must show exactly what was typed: no ligatures (">=" must not become "≥", "==" must not merge). */
code, pre, [data-testid="stRadioOption"] p, .cc-option, .cc-reveal .ans, .cc-locked b {
  font-variant-ligatures: none !important; font-feature-settings: "liga" 0, "calt" 0 !important; }
[data-testid="stMarkdownContainer"] :is(.cc-option > span:first-child, .cc-reveal .ans, .cc-locked b) {
  font-family: var(--code) !important; }
html, body, .stApp, [data-testid="stAppViewContainer"] { background: var(--night) !important; color: var(--ink); }
.stApp, .stApp p, .stApp label, .stApp input, .stApp button, .stApp textarea, .stApp li,
[data-testid="stMarkdownContainer"] :is(div, span, b, strong, em, small, time, h1, h2, h3) { font-family: var(--ui) !important; }
.stApp { background:
   radial-gradient(1200px 500px at 85% -10%, rgba(138,92,255,.10), transparent 60%),
   radial-gradient(900px 420px at -10% 0%, rgba(61,139,255,.09), transparent 60%), var(--night) !important; }
header[data-testid="stHeader"] { background: transparent; height: 0; }
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stStatusWidget"] { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container { max-width: 1240px; padding-top: 1.2rem !important; padding-bottom: 3rem; }
h1, h2, h3, h4 { font-family: var(--ui) !important; color: var(--ink) !important; letter-spacing: -0.01em; }

/* ---------- panels: st.container(border=True, key="panel-...") via theme.panel() ---------- */
[class*="st-key-panel-"] {
  background: var(--deck); border: 1px solid var(--rail) !important; border-radius: 16px !important;
  padding: 1.15rem 1.2rem 1.3rem !important; box-shadow: 0 18px 40px -28px rgba(0,0,0,.9);
}
[class*="st-key-panel-room-"] { padding: .9rem 1.1rem !important; border-radius: 14px !important; }
[class*="st-key-panel-question"] { border-color: #2A3757 !important; }

/* ---------- buttons ---------- */
.stButton > button, .stFormSubmitButton > button {
  font-weight: 600; border-radius: 10px; min-height: 2.6rem; transition: transform .08s ease, background .15s ease;
}
.stButton > button:active, .stFormSubmitButton > button:active { transform: translateY(1px); }
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background: var(--volt) !important; border: 1px solid var(--volt) !important; color: #fff !important;
}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover { background: #5A9CFF !important; }
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {
  background: transparent !important; border: 1px solid var(--rail) !important; color: var(--ink) !important;
}
[data-testid="stBaseButton-secondary"]:hover { border-color: var(--volt) !important; color: #fff !important; }
button:disabled { opacity: .45 !important; }

/* ---------- inputs ---------- */
.stTextInput input, .stNumberInput input { background: var(--night) !important; border-radius: 10px !important; }
[data-baseweb="input"] { background: var(--night) !important; border-color: var(--rail) !important; border-radius: 10px !important; }
[data-baseweb="tab-list"] { gap: .25rem; }
[data-baseweb="tab"] { font-family: var(--ui) !important; font-weight: 600; }

/* ---------- answer options (radio) ---------- */
[data-testid="stRadio"], [data-testid="stRadio"] > div { width: 100% !important; }
[data-testid="stRadioGroup"] { gap: .5rem; width: 100%; align-items: stretch !important; }
[data-testid="stRadioGroup"] > div { width: 100%; display: flex; }
[data-testid="stRadioOption"] {
  background: var(--night); border: 1px solid var(--rail); border-radius: 12px; padding: .85rem 1rem !important;
  margin: 0 !important; width: 100%; cursor: pointer; transition: border-color .12s ease, background .12s ease;
}
[data-testid="stRadioOption"]:hover { border-color: rgba(61,139,255,.7); }
[data-testid="stRadioOption"]:has(input:checked) { border-color: var(--volt); background: rgba(61,139,255,.13); }
[data-testid="stRadioOption"]:has(input:focus-visible) { outline: 2px solid var(--volt); outline-offset: 2px; }
[data-testid="stRadioOption"] p { font-size: 1.02rem !important; font-family: var(--code) !important; white-space: pre; }

/* ---------- code ---------- */
[data-testid="stCode"] pre, .stCode pre { background: #070B17 !important; border: 1px solid var(--rail); border-radius: 12px !important; }
[data-testid="stCode"] code, .stCode code { font-family: var(--code) !important; font-size: .95rem !important; line-height: 1.6 !important; }

/* ---------- custom blocks ---------- */
.cc-brand { display:flex; align-items:center; gap:.6rem; font-weight:700; font-size:1.35rem; letter-spacing:-.02em; }
.cc-brand b { display:inline-grid; place-items:center; width:2rem; height:2rem; border-radius:9px;
  background: linear-gradient(135deg, var(--volt), var(--violet)); color:#fff; font-size:1rem; }
.cc-chips { display:flex; flex-wrap:wrap; gap:.4rem; justify-content:flex-end; align-items:center; min-height:2.6rem; }
.cc-chip { display:inline-flex; align-items:center; gap:.4rem; padding:.22rem .65rem; border-radius:999px;
  background: var(--deck); border:1px solid var(--rail); color: var(--mist); font-size:.82rem; font-weight:500; white-space:nowrap; }
.cc-chip i { width:.5rem; height:.5rem; border-radius:50%; background: var(--mist); display:inline-block; }
.cc-chip--good i { background: var(--green); box-shadow: 0 0 8px var(--green); } .cc-chip--good { color: var(--ink); }
.cc-chip--warn i { background: var(--amber); } .cc-chip--warn { color: var(--ink); }
.cc-chip--bad i { background: var(--red); } .cc-chip--bad { color: var(--ink); }
.cc-chip--volt { border-color: rgba(61,139,255,.5); color:#BFD6FF; } .cc-chip--volt i { background: var(--volt); }
.cc-chip--violet { border-color: rgba(138,92,255,.5); color:#D4C6FF; } .cc-chip--violet i { background: var(--violet); }

.cc-title { font-size: 2rem; font-weight: 700; letter-spacing: -.025em; margin: .2rem 0 .1rem; line-height:1.15; }
.cc-sub { color: var(--mist); margin: 0 0 1rem; font-size: 1rem; max-width: 62ch; }
.cc-h { font-size: 1.05rem; font-weight: 600; margin: 0 0 .6rem; display:flex; justify-content:space-between; align-items:baseline; }
.cc-h small { color: var(--mist); font-weight: 500; font-size:.85rem; }
.cc-muted { color: var(--mist); font-size: .9rem; }
.cc-kv { display:grid; grid-template-columns: auto 1fr; gap:.25rem .9rem; font-size:.88rem; }
.cc-kv span:nth-child(odd) { color: var(--mist); } .cc-kv span:nth-child(even) { text-align:right; font-variant-numeric: tabular-nums; }

.cc-avatar { display:inline-grid; place-items:center; border-radius:50%; font-weight:700; color:#0A0F1F; flex:none; }
.cc-person { display:flex; align-items:center; gap:.65rem; }
.cc-person .n { font-weight:600; } .cc-person .m { color: var(--mist); font-size:.82rem; }

/* hero */
.cc-hero { padding: 2.2rem 0 1rem; }
.cc-wordmark { font-size: clamp(3.2rem, 7.2vw, 6rem); font-weight: 700; letter-spacing: -.055em; line-height: .92; margin:0;
  background: linear-gradient(100deg, #F2F5FF 35%, #BFD6FF 70%, #CDBBFF); -webkit-background-clip: text; background-clip: text; color: transparent; }
.cc-wordmark .caret { display:inline-block; width:.09em; height:.8em; margin-left:.06em; background: var(--volt);
  vertical-align: -.05em; animation: cc-blink 1.1s steps(1) infinite; }
@keyframes cc-blink { 50% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .cc-wordmark .caret { animation: none; } }
.cc-lede { font-size: 1.2rem; color: #C7D1EE; max-width: 34ch; margin: 1.2rem 0 1.6rem; line-height: 1.5; }
.cc-facts { display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: .8rem; max-width: 34rem; }
.cc-facts div { border-left: 2px solid var(--rail); padding-left: .8rem; line-height: 1.3; }
.cc-facts b { display:block; font-size: 1.5rem; font-variant-numeric: tabular-nums; }
.cc-facts span { color: var(--mist); font-size: .85rem; }
.cc-wire { margin-top: 2rem !important; color: var(--mist); font-size: .88rem; max-width: 46ch; line-height:1.55; }
.cc-wire em { color: #BFD6FF; font-style: normal; } .cc-wire strong { color: #D4C6FF; font-weight:600; }

/* rooms list */
.cc-room { display:flex; flex-direction:column; gap:.2rem; }
.cc-room .name { font-weight: 600; font-size: 1.08rem; }
.cc-room .meta { color: var(--mist); font-size: .85rem; }
.cc-seats { display:flex; gap:.3rem; align-items:center; }
.cc-seats i { width: .8rem; height: .8rem; border-radius: 4px; background: var(--rail); display:inline-block; }
.cc-seats i.on { background: var(--volt); }
.cc-seats span { margin-left:.4rem; font-variant-numeric: tabular-nums; color: var(--ink); font-size:.9rem; }

/* seats (waiting room) */
.cc-seat { border: 1px solid var(--rail); border-radius: 16px; padding: 1.3rem 1rem; text-align:center; background: var(--deck);
  min-height: 12.5rem; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:.55rem; }
.cc-seat.me { border-color: rgba(61,139,255,.6); }
.cc-seat.ready { box-shadow: inset 0 -3px 0 var(--green); }
.cc-seat.empty { border-style: dashed; background: transparent; color: var(--mist); }
.cc-seat .who { font-size: 1.15rem; font-weight: 600; }
.cc-seat .tags { display:flex; gap:.35rem; flex-wrap:wrap; justify-content:center; }

/* round pips */
.cc-pips { display:flex; gap:.35rem; align-items:center; flex-wrap:wrap; }
.cc-pips span { width: 1.7rem; height: 1.7rem; border-radius: 8px; display:inline-grid; place-items:center; font-size:.78rem;
  font-weight:600; background: var(--deck); border: 1px solid var(--rail); color: var(--mist); font-variant-numeric: tabular-nums; }
.cc-pips span.done { background: rgba(61,139,255,.16); border-color: rgba(61,139,255,.4); color:#BFD6FF; }
.cc-pips span.now { background: var(--volt); border-color: var(--volt); color:#fff; }

/* the countdown: the one loud element */
.cc-clock { display:flex; flex-direction:column; gap:.45rem; }
.cc-clock .digits { font-size: 4.2rem; font-weight: 700; line-height: .9; letter-spacing: -.04em; font-variant-numeric: tabular-nums; }
.cc-clock .digits small { font-size: 1.2rem; color: var(--mist); margin-left: .2rem; letter-spacing: 0; font-weight:500; }
.cc-clock .fuse { height: .7rem; border-radius: 999px; background: var(--rail); overflow: hidden; }
.cc-clock .fuse div { height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--violet), var(--volt)); }
.cc-clock .meta { color: var(--mist); font-size: .8rem; display:flex; align-items:center; gap:.4rem; }
.cc-clock .meta i { width:.45rem; height:.45rem; border-radius:50%; background: var(--green); display:inline-block; }
.cc-clock.local .meta i { background: var(--amber); }
.cc-clock.tense .digits { color: #CDBBFF; } .cc-clock.tense .fuse div { background: var(--violet); }
.cc-clock.critical .digits { color: var(--red); } .cc-clock.critical .fuse div { background: var(--red); }
.cc-clock.small .digits { font-size: 2.4rem; }

/* question */
.cc-cat { display:inline-flex; gap:.4rem; align-items:center; color:#D4C6FF; font-weight:600; font-size:.9rem; }
.cc-cat i { width:.55rem; height:.55rem; border-radius:2px; background: var(--violet); display:inline-block; }
.cc-prompt { font-size: 1.45rem; font-weight: 600; line-height: 1.3; margin: .4rem 0 .9rem; max-width: 40ch; }
.cc-locked { border: 1px solid rgba(47,209,139,.45); background: rgba(47,209,139,.08); border-radius: 12px; padding: 1rem 1.1rem; }
.cc-locked b { font-family: var(--code); }

/* scoreboard rows */
.cc-board { display:flex; flex-direction:column; gap:.35rem; }
.cc-row { display:grid; grid-template-columns: 1.6rem 1fr auto; align-items:center; gap:.6rem; padding:.55rem .6rem;
  border-radius:10px; background: var(--night); border:1px solid transparent; }
.cc-row.me { border-color: rgba(61,139,255,.45); }
.cc-row .rank { color: var(--mist); font-weight:700; font-variant-numeric: tabular-nums; text-align:center; }
.cc-row .score { font-weight:700; font-size:1.15rem; font-variant-numeric: tabular-nums; }
.cc-row .off { opacity: .55; }

/* feed */
.cc-feed { display:flex; flex-direction:column; gap:.3rem; font-size:.86rem; }
.cc-feed div { display:flex; gap:.6rem; } .cc-feed time { color: var(--mist); font-variant-numeric: tabular-nums; flex:none; }
.cc-feed .warning { color: var(--amber); } .cc-feed .success { color: var(--green); } .cc-feed .error { color: var(--red); }

/* round results */
.cc-reveal { border-radius: 14px; padding: 1rem 1.1rem; background: rgba(47,209,139,.08); border:1px solid rgba(47,209,139,.4); }
.cc-reveal .ans { font-family: var(--code); font-size: 1.25rem; font-weight: 600; color: #BFF5DD; }
.cc-option { display:flex; justify-content:space-between; gap:.8rem; align-items:center; padding:.6rem .8rem; border-radius:10px;
  border:1px solid var(--rail); margin-bottom:.4rem; font-family: var(--code); font-size:.92rem; }
.cc-option.correct { border-color: rgba(47,209,139,.6); background: rgba(47,209,139,.07); }
.cc-option .pickers { display:flex; gap:.3rem; flex-wrap:wrap; font-family: var(--ui); }
.cc-result { display:grid; grid-template-columns: 1fr auto auto; gap: .8rem; align-items:center; padding: .7rem .8rem;
  border-radius: 12px; background: var(--night); margin-bottom:.45rem; }
.cc-result .pts { font-size: 1.3rem; font-weight: 700; font-variant-numeric: tabular-nums; text-align:right; min-width: 3.5rem; }
.cc-result .pts.zero { color: var(--mist); }
.cc-result .detail { color: var(--mist); font-size:.84rem; text-align:right; font-variant-numeric: tabular-nums; }
.cc-mark { font-weight:700; } .cc-mark.ok { color: var(--green); } .cc-mark.no { color: var(--red); }
.cc-history { display:flex; justify-content:space-between; gap:.8rem; padding:.55rem 0; border-bottom:1px solid var(--rail); }
.cc-history:last-child { border-bottom: 0; } .cc-history .t { font-weight:600; font-size:.92rem; }
.cc-history .s { color: var(--mist); font-size:.82rem; } .cc-history .p { font-weight:700; font-variant-numeric: tabular-nums; }

/* final */
.cc-winner { padding: 1.6rem 0 .6rem; }
.cc-winner .crown { font-size: 2.6rem; }
.cc-winner .headline { font-size: clamp(2.6rem, 5.4vw, 4.2rem); font-weight: 700; margin: .2rem 0 .3rem; letter-spacing: -.045em; line-height: 1; }
.cc-podium { display:grid; grid-template-columns: 2.2rem 1fr repeat(3, minmax(4.5rem, auto)); gap:.9rem; align-items:center;
  padding: .85rem 1rem; border-radius: 14px; background: var(--deck); border:1px solid var(--rail); margin-bottom:.5rem; }
.cc-podium.first { border-color: rgba(138,92,255,.7); background: linear-gradient(90deg, rgba(138,92,255,.14), var(--deck) 60%); }
.cc-podium .rank { font-size:1.4rem; font-weight:700; color: var(--mist); text-align:center; }
.cc-podium.first .rank { color: #D4C6FF; }
.cc-podium .stat { text-align:right; font-variant-numeric: tabular-nums; }
.cc-podium .stat b { display:block; font-size:1.25rem; } .cc-podium .stat span { color: var(--mist); font-size:.78rem; }
.cc-bar { height:.4rem; background: var(--rail); border-radius:999px; overflow:hidden; margin-top:.35rem; }
.cc-bar div { height:100%; background: var(--green); border-radius:999px; }

@media (max-width: 760px) {
  .cc-facts { grid-template-columns: 1fr 1fr; }
  .cc-podium { grid-template-columns: 2rem 1fr auto; } .cc-podium .stat.opt { display:none; }
  .cc-clock .digits { font-size: 3rem; }
}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def panel(key: str):
    """A styled panel: a bordered container whose key gives it the CSS class st-key-panel-<key>."""
    return st.container(border=True, key=f"panel-{key}")


def html_block(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def avatar(username: str, size: float = 2.2) -> str:
    digest = int(hashlib.sha1((username or "?").lower().encode()).hexdigest(), 16)
    color = AVATAR_COLORS[digest % len(AVATAR_COLORS)]
    initial = esc((username or "?")[0].upper())
    return (f'<span class="cc-avatar" style="width:{size}rem;height:{size}rem;background:{color};'
            f'font-size:{size * 0.45:.2f}rem">{initial}</span>')


def chip(text: str, tone: str = "") -> str:
    return f'<span class="cc-chip{" cc-chip--" + tone if tone else ""}"><i></i>{esc(text)}</span>'


def person(username: str, meta: str = "", size: float = 2.2, suffix: str = "") -> str:
    meta_html = f'<div class="m">{esc(meta)}</div>' if meta else ""
    return (f'<div class="cc-person">{avatar(username, size)}<div><div class="n">{esc(username)}{esc(suffix)}</div>'
            f'{meta_html}</div></div>')


def title(text: str, sub: str | None = None) -> None:
    html_block(f'<div class="cc-title">{esc(text)}</div>' + (f'<p class="cc-sub">{esc(sub)}</p>' if sub else ""))


def heading(text: str, aside: str | None = None) -> None:
    html_block(f'<div class="cc-h">{esc(text)}{f"<small>{esc(aside)}</small>" if aside else ""}</div>')


def room_status_chip(status: str) -> str:
    return {"WAITING": chip("Waiting", "volt"), "IN_PROGRESS": chip("In progress", "violet"),
            "FINISHED": chip("Finished", "")}.get(status, chip(status))


def clock(countdown: dict | None, label: str, small: bool = False) -> str:
    """The countdown block: big digits over a fuse that burns down and changes colour."""
    if not countdown:
        return f'<div class="cc-clock{" small" if small else ""}"><div class="digits">–</div><div class="meta">{esc(label)}</div></div>'
    left, duration = countdown["seconds_left"], max(1, countdown["duration"] or 1)
    shown = math.ceil(left - 1e-6) if left > 0 else 0
    fraction = max(0.0, min(1.0, left / duration))
    mood = "critical" if left <= 5 and countdown["phase"] == "QUESTION" else ("tense" if fraction <= 0.5 else "calm")
    source = countdown.get("source")
    via = "Live countdown over UDP" if source == "udp" else "Estimated locally (no UDP tick yet)"
    classes = f"cc-clock {mood}{' local' if source != 'udp' else ''}{' small' if small else ''}"
    return (f'<div class="{classes}" role="timer" aria-label="{shown} seconds left">'
            f'<div class="digits">{shown}<small>s</small></div>'
            f'<div class="fuse"><div style="width:{fraction * 100:.1f}%"></div></div>'
            f'<div class="meta"><i></i>{esc(label)} — {via}</div></div>')


def scoreboard(rows: list[dict], me_id: str | None, connected: dict[str, bool] | None = None) -> str:
    connected = connected or {}
    out = []
    for r in rows:
        online = connected.get(r["player_id"], r.get("connected", True))
        meta = f'{r.get("correct", 0)} correct' + ("" if online else " — offline")
        out.append(
            f'<div class="cc-row{" me" if r["player_id"] == me_id else ""}">'
            f'<span class="rank">{r.get("rank", "")}</span>'
            f'<span class="{"" if online else "off"}">{person(r["username"], meta, 1.8)}</span>'
            f'<span class="score">{r.get("score", 0)}</span></div>')
    return '<div class="cc-board">' + "".join(out) + "</div>"


def feed(notices: list[dict], limit: int = 6) -> str:
    if not notices:
        return '<div class="cc-muted">Room activity will show up here.</div>'
    items = [f'<div class="{esc(n["kind"])}"><time>{esc(n["time"])}</time><span>{esc(n["text"])}</span></div>'
             for n in reversed(notices[-limit:])]
    return '<div class="cc-feed">' + "".join(items) + "</div>"


def show_toasts(snap: dict) -> None:
    """Toast every notice we have not shown yet (only ones that arrived after this browser tab opened)."""
    notices = snap.get("notices", [])
    newest = notices[-1]["id"] if notices else 0
    seen = st.session_state.get("_seen_notice")
    if seen is None:
        st.session_state["_seen_notice"] = newest
        return
    for n in notices:
        if n["id"] > seen and n["kind"] != "info":      # routine room activity stays in the activity feed
            st.toast(n["text"], icon=TONE_ICONS.get(n["kind"], "ℹ️"))
    st.session_state["_seen_notice"] = max(seen, newest)
