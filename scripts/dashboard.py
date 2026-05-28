#!/usr/bin/env python3
"""dashboard.py — Render self-contained HTML dashboards from iMessage metrics.

Rendering layer for the `dashboard` subcommand. Stdlib-only, no chat.db
access, no network: it receives already-computed metric dicts (from
imessage.py) plus an optional model-authored annotations dict and returns a
single self-contained HTML document (inline CSS + inline SVG, one tiny
vanilla-JS filter for the "moments" search). Zero external dependencies so
the output opens offline in any browser.

Two archetypes share one shell:
  * Quantitative (content-free): KPI strip + charts computed from chat.db.
  * Narrative (content-rich): the same charts plus model-supplied themes,
    quotes, and a narrative arc injected via `annotations`.

The HTML is built from the module-name → renderer registry (RENDERERS).
Adding a new chart later is one renderer + one registry entry.
"""
from __future__ import annotations

import html
import math
from pathlib import Path

esc = html.escape

# The visual shell (document skeleton, all CSS, the moments-filter JS) lives
# in templates/dashboard.html — one file, both themes via CSS variables.
# Python only injects the title, theme, and rendered body.
_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"

# Canonical bucket orders (mirror imessage.py so the renderer is robust to
# dict key ordering in the metrics payload).
_RT_ORDER = ["<5m", "5m–1h", "1h–1d", "1–3d", "3–7d", "7d+"]
_TBR_ORDER = ["1", "2", "3", "4", "5+"]
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------- #
# Formatting helpers                                                     #
# --------------------------------------------------------------------- #

def humanize_duration(secs) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _num(n) -> str:
    try:
        return f"{n:,}"
    except (ValueError, TypeError):
        return str(n)


# --------------------------------------------------------------------- #
# Chart primitives                                                       #
# --------------------------------------------------------------------- #

def _card(title: str, body: str, *, sub: str = "", wide: bool = False) -> str:
    cls = "card wide" if wide else "card"
    subhtml = f'<p class="csub">{esc(sub)}</p>' if sub else ""
    return f'<section class="{cls}"><h3>{esc(title)}</h3>{subhtml}{body}</section>'


def _legend(labels: dict) -> str:
    return (
        '<div class="legend">'
        f'<span><i class="dot-me"></i>{esc(labels["me"])}</span>'
        f'<span><i class="dot-other"></i>{esc(labels["other"])}</span>'
        "</div>"
    )


def _hbars(rows, *, fill: str = "me", fmt=_num) -> str:
    """Horizontal bar list. rows = [(label, value)]."""
    mx = max((v for _, v in rows), default=0)
    out = []
    for label, v in rows:
        w = 0 if mx == 0 else v / mx * 100
        out.append(
            '<div class="bar">'
            f'<span class="bar-label">{esc(str(label))}</span>'
            f'<span class="bar-track"><span class="bar-fill fill-{fill}" '
            f'style="width:{w:.1f}%"></span></span>'
            f'<span class="bar-val">{esc(fmt(v))}</span></div>'
        )
    return f'<div class="bars">{"".join(out)}</div>'


def _grouped_hbars(cats, me_vals, other_vals, me_label, other_label) -> str:
    mx = max([*me_vals, *other_vals], default=0)
    rows = []
    for cat, a, b in zip(cats, me_vals, other_vals):
        wa = 0 if mx == 0 else a / mx * 100
        wb = 0 if mx == 0 else b / mx * 100
        rows.append(
            f'<div class="grp"><div class="glabel">{esc(str(cat))}</div>'
            f'<div class="bar"><span class="bar-label">{esc(me_label)}</span>'
            f'<span class="bar-track"><span class="bar-fill fill-me" '
            f'style="width:{wa:.1f}%"></span></span>'
            f'<span class="bar-val">{_num(a)}</span></div>'
            f'<div class="bar"><span class="bar-label">{esc(other_label)}</span>'
            f'<span class="bar-track"><span class="bar-fill fill-other" '
            f'style="width:{wb:.1f}%"></span></span>'
            f'<span class="bar-val">{_num(b)}</span></div></div>'
        )
    return f'<div class="grouped">{"".join(rows)}</div>'


def _donut(me, other, center_top, center_bot, *, size=170, stroke=24) -> str:
    total = me + other
    r = (size - stroke) / 2
    cx = cy = size / 2
    circ = 2 * math.pi * r
    if total == 0:
        segs = [
            f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" '
            f'stroke="var(--line)" stroke-width="{stroke}"/>'
        ]
    else:
        segs = []
        off = 0.0
        for val, cls in ((me, "stroke-me"), (other, "stroke-other")):
            seglen = (val / total) * circ
            segs.append(
                f'<circle cx="{cx}" cy="{cy}" r="{r:.2f}" fill="none" '
                f'class="{cls}" stroke-width="{stroke}" '
                f'stroke-dasharray="{seglen:.2f} {circ - seglen:.2f}" '
                f'stroke-dashoffset="{-off:.2f}"/>'
            )
            off += seglen
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'role="img" class="donut">'
        f'<g transform="rotate(-90 {cx} {cy})">{"".join(segs)}</g>'
        f'<text x="{cx}" y="{cy - 1}" text-anchor="middle" '
        f'class="donut-num">{esc(center_top)}</text>'
        f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" '
        f'class="axis">{esc(center_bot)}</text></svg>'
    )


def _monthly_svg(months, me, other, *, height=176) -> str:
    n = len(months)
    mx = max([*me, *other, 1])
    pad_l, pad_t, pad_b = 6, 8, 18
    width = max(560, n * 16)
    plot_h = height - pad_t - pad_b
    group_w = (width - pad_l) / n
    bw = max(2.0, min(7.0, group_w / 2 - 1))
    rects, labels = [], []
    for i, (mo, a, b) in enumerate(zip(months, me, other)):
        gx = pad_l + i * group_w + (group_w - 2 * bw) / 2
        for val, cls, xoff in ((a, "svg-me", 0.0), (b, "svg-other", bw)):
            h = (val / mx) * plot_h
            y = pad_t + plot_h - h
            rects.append(
                f'<rect class="{cls}" x="{gx + xoff:.1f}" y="{y:.1f}" '
                f'width="{bw:.1f}" height="{h:.1f}" rx="1"/>'
            )
        if mo.endswith("-01") or i == 0:
            lx = pad_l + i * group_w + group_w / 2
            labels.append(
                f'<text class="axis" x="{lx:.1f}" y="{height - 4:.0f}" '
                f'text-anchor="middle">{esc(mo[:4])}</text>'
            )
    return (
        f'<svg class="month-svg" viewBox="0 0 {width:.0f} {height}" '
        f'preserveAspectRatio="none" role="img">'
        f'{"".join(rects)}{"".join(labels)}</svg>'
    )


# --------------------------------------------------------------------- #
# Quantitative module renderers  (value, ctx) -> html                   #
# --------------------------------------------------------------------- #

def render_kpis(v, ctx) -> str:
    other = v.get("other_label", "Them")
    ratio = v.get("ratio")
    ratio_s = f"{ratio:.2f}×" if ratio is not None else "—"
    streak_lbl = "Current streak" if v.get("streak_active") else "Last streak"
    cards = [
        (ratio_s, f"Your volume vs {other}", ""),
        (humanize_duration(v.get("median_s")), f"{other}'s median reply",
         "from your first text"),
        (humanize_duration(v.get("p80_s")), f"{other}'s 80th-pct reply",
         "slowest typical wait"),
        (f"{v.get('streak_days', 0):g}d", streak_lbl, "no gap > 24h"),
    ]
    inner = "".join(
        f'<div class="kpi"><div class="num">{esc(num)}</div>'
        f'<div class="label">{esc(label)}</div>'
        + (f'<div class="sub">{esc(sub)}</div>' if sub else "")
        + "</div>"
        for num, label, sub in cards
    )
    return f'<div class="kpis">{inner}</div>'


def render_message_share(v, ctx) -> str:
    labels = ctx["labels"]
    me = v["me"]["count"]
    other = v["other"]["count"]
    donut = _donut(me, other, _num(v.get("total", 0)), "messages")
    legend = (
        '<div class="share-legend">'
        f'<div class="share-row"><i class="dot-me"></i><b>{_num(me)}</b>'
        f'<span class="csub">{esc(labels["me"])} · {v["me"]["share"] * 100:.1f}%</span></div>'
        f'<div class="share-row"><i class="dot-other"></i><b>{_num(other)}</b>'
        f'<span class="csub">{esc(labels["other"])} · {v["other"]["share"] * 100:.1f}%</span></div>'
        "</div>"
    )
    return _card("Message share", f'<div class="share-wrap">{donut}{legend}</div>')


def render_response_time(v, ctx) -> str:
    other_label = ctx["labels"]["other"]
    side = v.get("other", {})
    n = side.get("n", 0)
    if not n:
        return _card(f"{other_label} response time",
                     '<p class="csub">Not enough back-and-forth to measure.</p>')
    rows = [(k, side["buckets"].get(k, 0)) for k in _RT_ORDER]
    sub = (f"median {humanize_duration(side.get('median_s'))} · "
           f"80th-pct {humanize_duration(side.get('p80_s'))} · {n} replies")
    return _card(f"{other_label} response time", _hbars(rows, fill="other"), sub=sub)


def render_who_restarts(v, ctx) -> str:
    labels = ctx["labels"]
    cats = v["thresholds"]
    me = [v["counts"][t]["me"] for t in cats]
    other = [v["counts"][t]["other"] for t in cats]
    if not any(me + other):
        return _card("Who restarts after silence?",
                     '<p class="csub">No silences over 8h in this range.</p>',
                     wide=True)
    body = _grouped_hbars(cats, me, other, labels["me"], labels["other"]) + _legend(labels)
    return _card("Who restarts after silence?", body,
                 sub="Who sends the first message after a gap of at least N hours.",
                 wide=True)


def render_texts_before_reply(v, ctx) -> str:
    other_label = ctx["labels"]["other"]
    side = v.get("me", {})  # your bursts, before they reply
    n = side.get("n", 0)
    if not n:
        return _card(f"Texts before {other_label} replies",
                     '<p class="csub">Not enough exchanges to measure.</p>')
    rows = [(k, side["buckets"].get(k, 0)) for k in _TBR_ORDER]
    med = side.get("median")
    sub = (f"median {med if med is not None else '—'} text(s) before a reply · "
           f"{n} bursts")
    return _card(f"Texts before {other_label} replies", _hbars(rows, fill="me"), sub=sub)


def render_monthly_volume(v, ctx) -> str:
    months = v.get("months", [])
    if not months:
        return _card("Monthly message volume", '<p class="csub">No data.</p>', wide=True)
    body = _monthly_svg(months, v["me"], v["other"]) + _legend(ctx["labels"])
    return _card("Monthly message volume", body, wide=True)


def render_weekday(v, ctx) -> str:
    rows = [(d, v.get(d, 0)) for d in _WEEKDAYS]
    return _card("By weekday", _hbars(rows, fill="me"))


def render_hour(v, ctx) -> str:
    rows = [(f"{h:02d}", v.get(str(h), 0)) for h in range(24)]
    return _card("By hour of day", _hbars(rows, fill="me"), wide=True)


def render_top_days(v, ctx) -> str:
    days = v.get("days", [])
    if not days:
        return _card("Most active days", '<p class="csub">No data.</p>')
    rows = [(d["date"], d["count"]) for d in days]
    return _card("Most active days", _hbars(rows, fill="other"))


def render_longest_gap(v, ctx) -> str:
    body = f'<div class="bigstat">{v.get("days", 0):g} days</div>'
    if v.get("from"):
        body += f'<div class="bigstat-sub">{esc(v["from"])} → {esc(v["to"])}</div>'
    return _card("Longest silence", body)


def render_streak(v, ctx) -> str:
    lbl = "Active now" if v.get("is_active") else "Ended"
    body = f'<div class="bigstat">{v.get("days", 0):g}d</div>'
    sub = esc(lbl)
    if v.get("start"):
        sub += f' · since {esc(v["start"])}'
    sub += f' · no gap &gt; {v.get("silence_threshold_h", 24):g}h'
    return _card("Conversational streak", f'{body}<div class="bigstat-sub">{sub}</div>')


RENDERERS = {
    "kpis": render_kpis,
    "message_share": render_message_share,
    "response_time": render_response_time,
    "who_restarts": render_who_restarts,
    "texts_before_reply": render_texts_before_reply,
    "monthly_volume": render_monthly_volume,
    "weekday": render_weekday,
    "hour": render_hour,
    "top_days": render_top_days,
    "longest_gap": render_longest_gap,
    "streak": render_streak,
}


# --------------------------------------------------------------------- #
# Narrative renderers (annotations layer)                               #
# --------------------------------------------------------------------- #

def _paras(paras) -> str:
    return '<div class="prose">' + "".join(f"<p>{esc(p)}</p>" for p in paras) + "</div>"


def _render_hero(hero: dict, fallback_title: str, labels: dict, subtitle: str) -> str:
    eb = hero.get("eyebrow", "")
    title = hero.get("title") or fallback_title
    thesis = hero.get("thesis", "")
    parts = ['<header class="header">']
    if eb:
        parts.append(f'<div class="eyebrow">{esc(eb)}</div>')
    parts.append(f"<h1>{esc(title)}</h1>")
    meta = subtitle or f'{labels["me"]} ⇄ {labels["other"]}'
    parts.append(f'<div class="meta">{esc(meta)}</div>')
    if thesis:
        parts.append(f'<p class="thesis">{esc(thesis)}</p>')
    parts.append("</header>")
    return "".join(parts)


def _render_header(title: str, labels: dict, subtitle: str) -> str:
    meta = subtitle or f'{labels["me"]} ⇄ {labels["other"]}'
    return (
        '<header class="header"><div class="eyebrow">iMessage dashboard</div>'
        f"<h1>{esc(title)}</h1><div class=\"meta\">{esc(meta)}</div></header>"
    )


def _section(title: str, body: str) -> str:
    return f'<section class="section"><h2>{esc(title)}</h2>{body}</section>'


def _render_narrative(ann: dict, ctx: dict, metrics: dict) -> str:
    out = []

    ag = ann.get("at_a_glance")
    if ag:
        items = "".join(
            f'<div><dt>{esc(i.get("label", ""))}</dt>'
            f'<dd>{esc(str(i.get("value", "")))}</dd></div>'
            for i in ag
        )
        out.append(_section("At a glance", f'<dl class="kv">{items}</dl>'))

    shape = ann.get("shape")
    if shape:
        out.append(_section(shape.get("title", "The shape of the thread"),
                            _paras(shape.get("paragraphs", []))))

    findings = ann.get("findings")
    if findings:
        cards = "".join(
            f'<div class="tcard"><h4>{esc(f.get("title", ""))}</h4>'
            f'<p>{esc(f.get("body", ""))}</p></div>'
            for f in findings
        )
        out.append(_section("Findings", f'<div class="glist">{cards}</div>'))

    sp = ann.get("speaker_profiles")
    if sp:
        cards = ""
        for p in sp:
            chips = "".join(f'<span class="chip">{esc(t)}</span>'
                            for t in p.get("traits", []))
            chiphtml = f'<div class="chips">{chips}</div>' if chips else ""
            cards += (f'<div class="tcard"><h4>{esc(p.get("name", ""))}</h4>'
                      f'<p>{esc(p.get("body", ""))}</p>{chiphtml}</div>')
        out.append(_section("Speaker profiles", f'<div class="glist">{cards}</div>'))

    arc = ann.get("narrative_arc")
    if arc:
        arts = "".join(
            f'<article><div class="when">{esc(a.get("date_range", ""))}</div>'
            f'<h4>{esc(a.get("title", ""))}</h4>'
            f'<p class="csub">{esc(a.get("paragraph", ""))}</p></article>'
            for a in arc
        )
        out.append(_section("Narrative arc", f'<div class="arc">{arts}</div>'))

    sr = ann.get("spiky_reads")
    if sr:
        cards = "".join(
            f'<div class="tcard"><h4>{esc(s.get("title", ""))}</h4>'
            f'<p>{esc(s.get("body", ""))}</p></div>'
            for s in sr
        )
        out.append(_section("Spiky reads", f'<div class="glist">{cards}</div>'))

    themes = ann.get("themes")
    if themes:
        cards = "".join(
            f'<div class="tcard"><div class="kicker">{esc(t.get("kicker", ""))}</div>'
            f'<h4>{esc(t.get("title", ""))}</h4>'
            f'<p>{esc(t.get("paragraph", ""))}</p></div>'
            for t in themes
        )
        out.append(_section("Interpretive themes", f'<div class="glist">{cards}</div>'))

    moments = ann.get("moments")
    if moments:
        cards = ""
        for m in moments:
            cit = m.get("citation", {}) or {}
            citline = ""
            who = ", ".join(p for p in (cit.get("sender", ""), cit.get("ts", "")) if p)
            if who:
                rid = (f' · rowid {esc(str(cit["rowid"]))}'
                       if cit.get("rowid") is not None else "")
                citline = f"<cite>— {esc(who)}{rid}</cite>"
            cards += (f'<div class="moment"><h4>{esc(m.get("title", ""))}</h4>'
                      f'<blockquote><p>{esc(m.get("quote", ""))}</p>{citline}</blockquote></div>')
        body = ('<input id="momentSearch" class="search" '
                'placeholder="Filter moments…" aria-label="Filter moments">'
                + cards)
        out.append(_section("Moments worth rereading", body))

    dl = ann.get("data_lenses")
    if dl:
        prose = "".join(
            f'<div class="tcard"><h4>{esc(d.get("title", ""))}</h4>'
            f'<p>{esc(d.get("body", ""))}</p></div>'
            for d in dl
        )
        charts = ""
        for d in dl:
            hint = d.get("svg_hint")
            if hint and hint in metrics and hint in RENDERERS:
                charts += RENDERERS[hint](metrics[hint], ctx)
        block = f'<div class="glist">{prose}</div>'
        if charts:
            block += f'<div class="cards" style="margin-top:14px">{charts}</div>'
        out.append(_section("Data lenses", block))

    ef = ann.get("explore_further")
    if ef:
        items = "".join(f"<li>{esc(x)}</li>" for x in ef)
        out.append(_section("Things to explore further", f'<ul class="prose">{items}</ul>'))

    fr = ann.get("final_read")
    if fr:
        out.append(_section(fr.get("title", "Final read"),
                            _paras(fr.get("paragraphs", []))))

    return "\n".join(out)


def _render_footer(ann) -> str:
    if ann and ann.get("footer_note"):
        note = esc(ann["footer_note"])
    elif ann:
        note = ("Generated locally with the imessage-history skill. Contains "
                "quoted message content — handle this file privately.")
    else:
        note = ("Generated locally with the imessage-history skill. No message "
                "content is included — only counts, timestamps, and lengths.")
    return f'<footer class="footer">{note}</footer>'


# --------------------------------------------------------------------- #
# Shell                                                                  #
# --------------------------------------------------------------------- #

def render_dashboard(metrics, modules, *, title, theme="light",
                     annotations=None, labels, subtitle="") -> str:
    ctx = {"labels": labels, "theme": theme}
    parts = []

    if annotations and annotations.get("hero"):
        parts.append(_render_hero(annotations["hero"], title, labels, subtitle))
    else:
        parts.append(_render_header(title, labels, subtitle))

    if "kpis" in modules and "kpis" in metrics:
        parts.append(RENDERERS["kpis"](metrics["kpis"], ctx))

    cards = []
    for name in modules:
        if name == "kpis":
            continue
        renderer = RENDERERS.get(name)
        if renderer and name in metrics:
            cards.append(renderer(metrics[name], ctx))
    if cards:
        parts.append(f'<div class="cards">{"".join(cards)}</div>')

    if annotations:
        parts.append(_render_narrative(annotations, ctx, metrics))

    parts.append(_render_footer(annotations))

    body = f'<div class="container">{"".join(parts)}</div>'
    return _html_shell(title, theme, body)


_template_cache: str | None = None


def _load_template() -> str:
    global _template_cache
    if _template_cache is None:
        try:
            _template_cache = _TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"Dashboard template not found at {_TEMPLATE_PATH}"
            ) from exc
    return _template_cache


def _html_shell(title, theme, body) -> str:
    """Inject the rendered body into the external shell template. Uses plain
    str.replace (not str.format) because the template's CSS is full of
    braces. Order matters: the simple tokens are substituted before BODY, so
    arbitrary HTML inside BODY is never re-scanned for placeholders."""
    theme = theme if theme in ("light", "dark") else "light"
    return (
        _load_template()
        .replace("{{THEME}}", esc(theme))
        .replace("{{TITLE}}", esc(title))
        .replace("{{BODY}}", body)
    )
