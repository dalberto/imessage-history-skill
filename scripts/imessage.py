#!/usr/bin/env python3
"""imessage.py — Query macOS iMessage history directly from chat.db.

Stdlib-only Python for exhaustive historical queries against
~/Library/Messages/chat.db (read-only). Decodes the attributedBody
typedstream blobs that the `text` column no longer populates on modern
macOS (iOS 16+ / macOS 13+), resolves handles to human names via the
macOS AddressBook, and has no hidden LIMITs — date-filtered queries
return everything in the range.

Subcommands:
  chats          List chats with counts and date ranges (supports
                 --participant <name> to unify split phone/email rows)
  participants   List participants of a chat
  stats          Per-participant stats for a chat: counts, median
                 length, activity histogram, longest dormancy
  search         Keyword search (one chat or all chats), with
                 --from/--not-from, --all, -K, --regex
  window         Reply-chain context around a timestamp
  dump           All messages in a chat over a date range
  anchor-sweep   Keyword search followed by auto-windowed expansion
                 and merge — the reply-chain recovery move as one call
  attachments    List attachments in a chat over a date range
  reactions      Surface tapbacks (normally filtered out) with their
                 target messages

Output formats: every content-returning subcommand honors
--format {text,json,ndjson}. Default is text.

Grant Full Disk Access to the running process if you see 'unable to
open database file'.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

DB_DEFAULT = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_SOURCES = os.path.expanduser(
    "~/Library/Application Support/AddressBook/Sources"
)
SKILL_ROOT = Path(__file__).resolve().parent.parent
CONTACTS_DEFAULT = str(SKILL_ROOT / "contacts.json")
APPLE_EPOCH_OFFSET = 978307200  # seconds from Unix epoch to 2001-01-01 UTC

REACTION_TYPES = {
    2000: "loved",
    2001: "liked",
    2002: "disliked",
    2003: "laughed at",
    2004: "emphasized",
    2005: "questioned",
    2006: "reacted to",  # custom emoji/sticker tapback
    3000: "removed love from",
    3001: "removed like from",
    3002: "removed dislike from",
    3003: "removed laugh from",
    3004: "removed emphasis from",
    3005: "removed question from",
    3006: "removed reaction from",
}


def open_db(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def to_apple_ns(dt: datetime) -> int:
    return int((dt.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def fmt_ts(apple_ns: int | None) -> str | None:
    if apple_ns is None:
        return None
    val = apple_ns / 1e9 if len(str(apple_ns)) > 10 else float(apple_ns)
    return datetime.fromtimestamp(val + APPLE_EPOCH_OFFSET).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def to_datetime(apple_ns: int | None) -> datetime | None:
    if apple_ns is None:
        return None
    val = apple_ns / 1e9 if len(str(apple_ns)) > 10 else float(apple_ns)
    return datetime.fromtimestamp(val + APPLE_EPOCH_OFFSET)


def parse_date(s: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {s!r}")


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract string content from an attributedBody NSTypedStream blob.

    The payload lives after the NSString class marker, framed as:
        \\x01\\x2b [length] [utf-8 bytes]
    where length defaults to a single byte (0x00-0x7f), or uses a 0x81
    (uint16 LE) / 0x82 (uint32 LE) extension prefix for longer strings.
    """
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx < 0:
        return None
    tail = blob[idx + len(b"NSString"):]
    m = re.search(rb"\x01\x2b", tail)
    if not m:
        return None
    p = m.end()
    if p >= len(tail):
        return None
    lb = tail[p]
    p += 1
    if lb == 0x81:
        length = int.from_bytes(tail[p:p + 2], "little")
        p += 2
    elif lb == 0x82:
        length = int.from_bytes(tail[p:p + 4], "little")
        p += 4
    else:
        length = lb
    return tail[p:p + length].decode("utf-8", errors="replace")


def get_message_text(text: str | None, att: bytes | None) -> str | None:
    if text:
        return text
    return decode_attributed_body(att)


def _normalize_handle(handle: str) -> str:
    """Canonicalize a handle for lookup.

    Phone numbers: strip non-digits, take the last 10 (US-friendly).
    Emails: lowercase + strip whitespace.
    """
    h = handle.strip()
    if "@" in h:
        return h.lower()
    digits = re.sub(r"\D", "", h)
    return digits[-10:] if len(digits) >= 10 else digits


class ContactResolver:
    """Resolve iMessage handles to human names.

    Lookup order per handle:
      1. Explicit JSON overrides (skill-local ``contacts.json`` by
         default, or ``--contacts PATH``). The ``me`` key overrides how
         the user's own messages are labeled.
      2. macOS AddressBook (all sources under
         ``~/Library/Application Support/AddressBook/Sources/*``).
      3. Raw handle id (phone/email) as a fallback.

    AddressBook scanning is lazy — we only touch it the first time an
    unknown handle needs to be resolved. Results are cached per-process.
    """

    def __init__(self, overrides_path: str | None = None):
        self.overrides = self._load_overrides(overrides_path)
        self._cache: dict[str, str] = {}
        self._scanned = False

    @staticmethod
    def _load_overrides(path: str | None) -> dict[str, str]:
        if path is None and os.path.exists(CONTACTS_DEFAULT):
            path = CONTACTS_DEFAULT
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _ensure_scanned(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        if not os.path.isdir(ADDRESSBOOK_SOURCES):
            return
        for source in os.listdir(ADDRESSBOOK_SOURCES):
            db_path = os.path.join(
                ADDRESSBOOK_SOURCES, source, "AddressBook-v22.abcddb"
            )
            if os.path.exists(db_path):
                self._scan_source(db_path)

    def _scan_source(self, db_path: str) -> None:
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            self._ingest(
                con,
                """
                SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION,
                       p.ZFULLNUMBER
                FROM ZABCDRECORD r
                JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE p.ZFULLNUMBER IS NOT NULL
                """,
            )
            self._ingest(
                con,
                """
                SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION,
                       e.ZADDRESS
                FROM ZABCDRECORD r
                JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
                WHERE e.ZADDRESS IS NOT NULL
                """,
            )
        finally:
            con.close()

    def _ingest(self, con: sqlite3.Connection, query: str) -> None:
        try:
            rows = con.execute(query).fetchall()
        except sqlite3.Error:
            return
        for first, last, org, value in rows:
            name = self._format_name(first, last, org)
            if not name or not value:
                continue
            key = _normalize_handle(value)
            # First match wins — don't clobber a good entry with a worse one
            self._cache.setdefault(key, name)

    @staticmethod
    def _format_name(first, last, org) -> str | None:
        parts = [p for p in (first, last) if p]
        if parts:
            return " ".join(parts)
        return org or None

    def resolve(self, is_me: int, handle_id: str | None) -> str:
        if is_me:
            return self.overrides.get("me", "Me")
        if not handle_id:
            return "?"
        if handle_id in self.overrides:
            return self.overrides[handle_id]
        self._ensure_scanned()
        key = _normalize_handle(handle_id)
        if key in self._cache:
            return self._cache[key]
        return handle_id

    def handles_for_name(self, name_substring: str) -> set[str]:
        """Return normalized handle keys whose AddressBook name matches.

        Used by ``--participant`` filters so a name like "Alex" resolves
        to every phone/email under that contact — unifying chats that
        show up as separate rows for phone vs email handles.
        """
        self._ensure_scanned()
        sub = name_substring.strip().lower()
        if not sub:
            return set()
        matched = {k for k, v in self._cache.items() if sub in v.lower()}
        # Honor overrides too, so a JSON-labeled handle is findable by name
        for k, v in self.overrides.items():
            if k == "me" or not isinstance(v, str):
                continue
            if sub in v.lower():
                matched.add(_normalize_handle(k))
        return matched


# ---------------------------------------------------------------------- #
# Filter resolution                                                      #
# ---------------------------------------------------------------------- #

def _resolve_sender_filter(
    con: sqlite3.Connection,
    resolver: ContactResolver,
    values: list[str] | None,
) -> set[int] | None:
    """Return chat.db handle.ROWIDs matching any of the given values.

    Each value is matched three ways:
      1. As a name substring via AddressBook (``resolver.handles_for_name``)
      2. As a substring of the raw ``handle.id`` text
      3. As a substring of the normalized form of ``handle.id``
    """
    if not values:
        return None
    rows = con.execute("SELECT ROWID, id FROM handle").fetchall()
    matched: set[int] = set()
    for raw in values:
        needles = resolver.handles_for_name(raw)
        lowered = raw.strip().lower()
        for rid, hid in rows:
            if hid is None:
                continue
            if lowered and lowered in hid.lower():
                matched.add(rid)
                continue
            norm = _normalize_handle(hid)
            if norm and (norm in needles or (lowered and lowered in norm)):
                matched.add(rid)
    return matched


# ---------------------------------------------------------------------- #
# Query builders                                                         #
# ---------------------------------------------------------------------- #

def _build_where(
    chat_ids: list[int] | None,
    keywords: list[str] | None,
    since: datetime | None,
    until: datetime | None,
    *,
    exclude_keywords: list[str] | None = None,
    require_all: bool = False,
    from_rowids: set[int] | None = None,
    not_from_rowids: set[int] | None = None,
    include_reactions: bool = False,
) -> tuple[str, list]:
    wheres: list[str] = []
    params: list = []
    if chat_ids:
        placeholder = ",".join("?" * len(chat_ids))
        wheres.append(f"cmj.chat_id IN ({placeholder})")
        params.extend(chat_ids)
    if since:
        wheres.append("m.date > ?")
        params.append(to_apple_ns(since))
    if until:
        wheres.append("m.date < ?")
        params.append(to_apple_ns(until))
    if keywords:
        kw_clauses = []
        for kw in keywords:
            kw_clauses.append(
                "(instr(m.attributedBody, CAST(? AS BLOB)) > 0 "
                "OR m.text LIKE ?)"
            )
            params.append(kw)
            params.append(f"%{kw}%")
        joiner = " AND " if require_all else " OR "
        wheres.append("(" + joiner.join(kw_clauses) + ")")
    if exclude_keywords:
        for kw in exclude_keywords:
            wheres.append(
                "NOT (instr(m.attributedBody, CAST(? AS BLOB)) > 0 "
                "OR m.text LIKE ?)"
            )
            params.append(kw)
            params.append(f"%{kw}%")
    if from_rowids is not None:
        if not from_rowids:
            wheres.append("1=0")
        else:
            ph = ",".join("?" * len(from_rowids))
            wheres.append(f"m.handle_id IN ({ph})")
            params.extend(from_rowids)
    if not_from_rowids:
        ph = ",".join("?" * len(not_from_rowids))
        wheres.append(f"(m.handle_id IS NULL OR m.handle_id NOT IN ({ph}))")
        params.extend(not_from_rowids)
    if not include_reactions:
        wheres.append(
            "(m.associated_message_type IS NULL OR m.associated_message_type < 2000)"
        )
    return " AND ".join(wheres) if wheres else "1=1", params


def _query_messages(con: sqlite3.Connection, where: str, params: list):
    sql = f"""
        SELECT m.ROWID, cmj.chat_id, m.date, m.is_from_me, h.id,
               m.text, m.attributedBody, m.associated_message_type
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE {where}
        ORDER BY m.date ASC
    """
    return con.execute(sql, params).fetchall()


# ---------------------------------------------------------------------- #
# Output formatting                                                      #
# ---------------------------------------------------------------------- #

def _row_to_record(row, resolver: ContactResolver, include_chat: bool = False) -> dict | None:
    rowid, chat_id, ns, is_me, hid, text, att, amt = row
    content = get_message_text(text, att)
    if content is None:
        return None
    content = content.strip()
    if not content:
        return None
    rec = {
        "rowid": rowid,
        "ts": fmt_ts(ns),
        "sender": resolver.resolve(is_me, hid),
        "handle": hid,
        "is_from_me": bool(is_me),
        "content": content,
        "associated_message_type": amt,
    }
    if include_chat:
        rec["chat_id"] = chat_id
    return rec


def _format_text(rec: dict, max_len: int = 0) -> str:
    content = rec["content"].replace("\n", " / ")
    if max_len and len(content) > max_len:
        content = content[:max_len] + "…"
    prefix = f"[{rec['ts']}]"
    if "chat_id" in rec:
        prefix += f" chat{rec['chat_id']}"
    return f"{prefix} {rec['sender']}: {content}"


def _passes_regex(content: str, pattern: re.Pattern | None) -> bool:
    if pattern is None:
        return True
    return bool(pattern.search(content))


def _emit_messages(
    rows,
    resolver: ContactResolver,
    fmt: str,
    *,
    include_chat: bool = False,
    max_len: int = 0,
    regex: re.Pattern | None = None,
) -> int:
    count = 0
    json_buffer: list[dict] = []
    for row in rows:
        rec = _row_to_record(row, resolver, include_chat=include_chat)
        if rec is None:
            continue
        if not _passes_regex(rec["content"], regex):
            continue
        if fmt == "ndjson":
            print(json.dumps(rec, ensure_ascii=False))
        elif fmt == "json":
            json_buffer.append(rec)
        else:
            print(_format_text(rec, max_len=max_len))
        count += 1
    if fmt == "json":
        print(json.dumps(json_buffer, ensure_ascii=False, indent=2))
    return count


# ---------------------------------------------------------------------- #
# Metrics — 1:1 conversation analysis (powers `metrics` + `dashboard`)   #
# ---------------------------------------------------------------------- #
#
# The dashboard capability is grounded in two artifacts a power user built
# with this skill: a content-free quantitative one-pager and a richer
# narrative "field report". Every metric below is *content-free* — it uses
# only timestamps, senders, and message lengths, never message text. The
# only place text enters a dashboard is the optional narrative annotations
# layer (see scripts/dashboard.py), which the model supplies separately.

MODULES = [
    "kpis", "message_share", "response_time", "who_restarts",
    "texts_before_reply", "monthly_volume", "weekday", "hour",
    "top_days", "longest_gap", "streak",
]

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Response-time + texts-before-reply buckets. Edges are left-closed,
# right-open seconds. Labels match the artifacts they reproduce.
RT_BUCKETS = [
    ("<5m", 0, 300),
    ("5m–1h", 300, 3600),
    ("1h–1d", 3600, 86400),
    ("1–3d", 86400, 259200),
    ("3–7d", 259200, 604800),
    ("7d+", 604800, None),
]
TBR_BUCKETS = ["1", "2", "3", "4", "5+"]
RESTART_THRESHOLDS_H = [8, 12, 24, 48, 72]


class MMsg(NamedTuple):
    """A normalized message for metric computation.

    Lighter than `_row_to_record`; carries only what the metrics need.
    `kind` is "text" when the decoded body is non-empty, else "media"
    (attachment-only / sticker / empty). Burst & gap metrics count media
    messages (a photo reply *is* a reply); share & length metrics use only
    text messages so totals agree with `stats`.
    """

    ns: int            # apple nanoseconds (normalized)
    is_me: bool
    side: str          # "me" | "other"
    hid: str | None    # raw handle.id of the sender (None for me)
    kind: str          # "text" | "media"
    text_len: int


class Burst(NamedTuple):
    """A maximal run of consecutive messages from the same side."""

    side: str
    start_ns: int
    end_ns: int
    count: int


def _normalize_ns(date_val: int) -> int:
    """chat.db stores nanoseconds on modern macOS but seconds on very old
    DBs. Normalize to nanoseconds with the same digit heuristic fmt_ts uses,
    so downstream arithmetic (gaps, latencies) is always in nanoseconds."""
    return date_val if len(str(date_val)) > 10 else date_val * 1_000_000_000


def load_metric_messages(con, chat_id, since, until):
    """Return (msgs_all, msgs_content, other_handles).

    msgs_all      — every message in range, date-ordered (incl. media).
    msgs_content  — subset with non-empty decoded text.
    other_handles — distinct raw handle.id strings for non-me senders that
                    actually sent a message in range (feeds the 1:1 guard).
    Reactions are already excluded by `_build_where`.
    """
    where, params = _build_where([chat_id], None, since, until)
    rows = _query_messages(con, where, params)
    msgs_all: list[MMsg] = []
    msgs_content: list[MMsg] = []
    other_handles: set[str] = set()
    for _rowid, _cid, ns, is_me, hid, text, att, _amt in rows:
        ns = _normalize_ns(ns)
        is_me_b = bool(is_me)
        content = get_message_text(text, att)
        content = content.strip() if content else ""
        m = MMsg(
            ns=ns,
            is_me=is_me_b,
            side="me" if is_me_b else "other",
            hid=hid,
            kind="text" if content else "media",
            text_len=len(content),
        )
        msgs_all.append(m)
        if content:
            msgs_content.append(m)
        if not is_me_b and hid:
            other_handles.add(hid)
    return msgs_all, msgs_content, other_handles


def detect_bursts(msgs: list[MMsg]) -> list[Burst]:
    """Collapse a date-ordered message list into alternating bursts.

    A burst starts whenever the sender changes; no time threshold is
    applied (a multi-day monologue is still one burst — gap logic lives in
    who_restarts/streak). Adjacent bursts therefore always differ in side.
    """
    bursts: list[Burst] = []
    for m in msgs:
        if bursts and bursts[-1].side == m.side:
            b = bursts[-1]
            bursts[-1] = b._replace(end_ns=m.ns, count=b.count + 1)
        else:
            bursts.append(Burst(m.side, m.ns, m.ns, 1))
    return bursts


def _bucket_label(secs: float) -> str:
    for label, lo, hi in RT_BUCKETS:
        if secs >= lo and (hi is None or secs < hi):
            return label
    return RT_BUCKETS[-1][0]


def _percentile(vals: list[float], q: float):
    """Nearest-rank percentile; q in [0,1]. Returns None for empty input."""
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return None
    if n == 1:
        return s[0]
    rank = math.ceil(q * n)
    return s[min(rank, n) - 1]


# --- individual metric modules (all content-free) --------------------- #

def message_share(msgs_content: list[MMsg]) -> dict:
    me = sum(1 for m in msgs_content if m.side == "me")
    other = sum(1 for m in msgs_content if m.side == "other")
    total = me + other
    return {
        "total": total,
        "me": {"count": me, "share": round(me / total, 4) if total else 0},
        "other": {"count": other, "share": round(other / total, 4) if total else 0},
        "ratio_me_over_other": round(me / other, 2) if other else None,
    }


def response_time(bursts: list[Burst]) -> dict:
    """Reply latency = ts(first message of the responder's burst) −
    ts(first message of the preceding burst), i.e. the wait measured from
    the *first* text in the opener's burst. Attributed to the responder.
    Computed for both directions; `other` is the headline (e.g. "their
    response time")."""
    lat: dict[str, list[float]] = {"me": [], "other": []}
    for prev, cur in zip(bursts, bursts[1:]):
        if prev.side == cur.side:
            continue
        lat[cur.side].append((cur.start_ns - prev.start_ns) / 1e9)
    out = {}
    for side, vals in lat.items():
        buckets = {label: 0 for label, _, _ in RT_BUCKETS}
        for v in vals:
            buckets[_bucket_label(v)] += 1
        out[side] = {
            "buckets": buckets,
            "median_s": statistics.median(vals) if vals else None,
            "p80_s": _percentile(vals, 0.8),
            "n": len(vals),
        }
    return out


def who_restarts(msgs: list[MMsg]) -> dict:
    """Who breaks a silence? For each consecutive-message gap, attribute the
    restart to whoever sent the next message. Counts are cumulative across
    thresholds — a 50h gap counts toward 8h/12h/24h/48h — so the bars are
    monotone non-increasing in the threshold."""
    counts = {f"{h}h": {"me": 0, "other": 0} for h in RESTART_THRESHOLDS_H}
    for prev, cur in zip(msgs, msgs[1:]):
        gap_s = (cur.ns - prev.ns) / 1e9
        for h in RESTART_THRESHOLDS_H:
            if gap_s >= h * 3600:
                counts[f"{h}h"][cur.side] += 1
    return {"thresholds": [f"{h}h" for h in RESTART_THRESHOLDS_H], "counts": counts}


def texts_before_reply(bursts: list[Burst]) -> dict:
    """Distribution of burst lengths that were then answered: how many texts
    in a row a side sends before the other replies. The final burst is
    excluded (nothing replied to it). Bucketed 1/2/3/4/5+ with a median."""
    lengths: dict[str, list[int]] = {"me": [], "other": []}
    for b in bursts[:-1]:
        lengths[b.side].append(b.count)
    out = {}
    for side, vals in lengths.items():
        buckets = {k: 0 for k in TBR_BUCKETS}
        for c in vals:
            buckets[str(c) if c < 5 else "5+"] += 1
        out[side] = {
            "buckets": buckets,
            "median": statistics.median(vals) if vals else None,
            "n": len(vals),
        }
    return out


def _dense_months(first: str, last: str) -> list[str]:
    fy, fm = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    out, y, mo = [], fy, fm
    while (y, mo) <= (ly, lm):
        out.append(f"{y:04d}-{mo:02d}")
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    return out


def monthly_volume(msgs_content: list[MMsg]) -> dict:
    per: dict[str, list[int]] = {}
    for m in msgs_content:
        dt = to_datetime(m.ns)
        if not dt:
            continue
        cell = per.setdefault(dt.strftime("%Y-%m"), [0, 0])
        cell[0 if m.side == "me" else 1] += 1
    if not per:
        return {"months": [], "me": [], "other": []}
    months = _dense_months(min(per), max(per))
    return {
        "months": months,
        "me": [per.get(k, [0, 0])[0] for k in months],
        "other": [per.get(k, [0, 0])[1] for k in months],
    }


def streak(msgs_all: list[MMsg], silence_h: float = 24.0, now_ns: int | None = None) -> dict:
    """Current conversational streak: the span of the latest unbroken run
    with no gap longer than `silence_h`. Now-anchored when the thread is
    still active (so it grows in real time, yielding fractional days), else
    reports the span of the last run with is_active=False."""
    if not msgs_all:
        return {
            "days": 0.0, "start": None, "silence_threshold_h": silence_h,
            "is_active": False, "broken": True,
        }
    if now_ns is None:
        now_ns = to_apple_ns(datetime.now())
    now_ns = _normalize_ns(now_ns)
    silence_ns = silence_h * 3600 * 1e9
    run_start = msgs_all[0].ns
    for prev, cur in zip(msgs_all, msgs_all[1:]):
        if (cur.ns - prev.ns) > silence_ns:
            run_start = cur.ns  # last break wins → start of the final run
    last_ns = msgs_all[-1].ns
    is_active = (now_ns - last_ns) <= silence_ns
    end_ref = now_ns if is_active else last_ns
    return {
        "days": round((end_ref - run_start) / 1e9 / 86400, 1),
        "start": fmt_ts(run_start),
        "silence_threshold_h": silence_h,
        "is_active": is_active,
        "broken": not is_active,
    }


# --- shared aggregations (also used by cmd_stats) --------------------- #

def weekday_hour_histograms(ns_list: list[int]) -> tuple[dict, dict]:
    weekday = [0] * 7
    hour = [0] * 24
    for ns in ns_list:
        dt = to_datetime(ns)
        if dt:
            weekday[dt.weekday()] += 1
            hour[dt.hour] += 1
    return (
        dict(zip(WEEKDAY_LABELS, weekday)),
        {str(h): hour[h] for h in range(24)},
    )


def top_days(ns_list: list[int], k: int = 5) -> list[dict]:
    day_counts: dict[str, int] = {}
    for ns in ns_list:
        dt = to_datetime(ns)
        if dt:
            key = dt.strftime("%Y-%m-%d")
            day_counts[key] = day_counts.get(key, 0) + 1
    top = sorted(day_counts.items(), key=lambda kv: -kv[1])[:k]
    return [{"date": d, "count": c} for d, c in top]


def longest_gap(ns_list: list[int]) -> dict:
    longest = 0.0
    gstart = gend = None
    for prev, cur in zip(ns_list, ns_list[1:]):
        delta = (cur - prev) / 1e9 / 86400
        if delta > longest:
            longest, gstart, gend = delta, prev, cur
    return {"days": round(longest, 2), "from": fmt_ts(gstart), "to": fmt_ts(gend)}


def compute_kpis(share: dict, rt: dict, strk: dict, labels: dict) -> dict:
    """Derived 4-up summary: volume ratio, the other side's median + p80
    reply wait, and the current streak."""
    rt_other = rt.get("other", {})
    return {
        "other_label": labels.get("other", "Them"),
        "ratio": share.get("ratio_me_over_other"),
        "median_s": rt_other.get("median_s"),
        "p80_s": rt_other.get("p80_s"),
        "streak_days": strk.get("days"),
        "streak_active": strk.get("is_active"),
    }


def compute_metrics(msgs_all, msgs_content, selected, *, labels, streak_silence_h=24.0):
    """Compute exactly the selected modules into a dict. Dependencies of the
    derived `kpis` module (share / response_time / streak) are computed and
    cached even when not individually selected."""
    bursts = detect_bursts(msgs_all)
    all_ns = [m.ns for m in msgs_all]
    content_ns = [m.ns for m in msgs_content]
    cache: dict[str, dict] = {}

    def share():
        return cache.setdefault("message_share", message_share(msgs_content))

    def rt():
        return cache.setdefault("response_time", response_time(bursts))

    def strk():
        return cache.setdefault(
            "streak", streak(msgs_all, silence_h=streak_silence_h)
        )

    def wh():
        return cache.setdefault("_wh", weekday_hour_histograms(content_ns))

    out: dict[str, dict] = {}
    for name in selected:
        if name == "message_share":
            out[name] = share()
        elif name == "response_time":
            out[name] = rt()
        elif name == "streak":
            out[name] = strk()
        elif name == "kpis":
            out[name] = compute_kpis(share(), rt(), strk(), labels)
        elif name == "who_restarts":
            out[name] = who_restarts(msgs_all)
        elif name == "texts_before_reply":
            out[name] = texts_before_reply(bursts)
        elif name == "monthly_volume":
            out[name] = monthly_volume(msgs_content)
        elif name == "weekday":
            out[name] = wh()[0]
        elif name == "hour":
            out[name] = wh()[1]
        elif name == "top_days":
            out[name] = {"days": top_days(content_ns)}  # wrapped for a stable object shape
        elif name == "longest_gap":
            out[name] = longest_gap(all_ns)
    return out


# --- dashboard CLI support ------------------------------------------- #

def _parse_modules(arg: str | None) -> list[str]:
    if not arg:
        return list(MODULES)
    requested = [m.strip() for m in arg.split(",") if m.strip()]
    unknown = [m for m in requested if m not in MODULES]
    if unknown:
        sys.exit(
            f"Unknown module(s): {', '.join(unknown)}.\n"
            f"Available: {', '.join(MODULES)}"
        )
    return requested


def _other_names(resolver: ContactResolver, other_handles: set[str]) -> set[str]:
    return {resolver.resolve(0, h) for h in other_handles}


def _labels(resolver: ContactResolver, other_handles: set[str]) -> dict:
    names = _other_names(resolver, other_handles)
    return {
        "me": resolver.resolve(1, None),
        "other": sorted(names)[0] if names else "Them",
    }


def _guard_one_to_one(resolver: ContactResolver, other_handles: set[str], chat_id: int) -> None:
    """Dashboards are 1:1-only for now. Two handles that resolve to the same
    name (phone + email of one contact) are treated as one person."""
    names = _other_names(resolver, other_handles)
    if len(names) > 1:
        sys.exit(
            f"chat_id={chat_id} has {len(names)} other participants "
            f"({', '.join(sorted(names))}); dashboards are 1:1-only for now.\n"
            f'Find a 1:1 chat with:  imessage.py chats --participant "<name>"'
        )


# ---------------------------------------------------------------------- #
# Subcommands                                                            #
# ---------------------------------------------------------------------- #

def cmd_chats(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None

    # Count clause: either all-time counts or counts within the window.
    if since or until:
        count_wheres = ["m.associated_message_type IS NULL OR m.associated_message_type < 2000"]
        count_params: list = []
        if since:
            count_wheres.insert(0, "m.date > ?")
            count_params.append(to_apple_ns(since))
        if until:
            count_wheres.insert(0, "m.date < ?")
            count_params.append(to_apple_ns(until))
        msg_filter = " AND ".join(count_wheres)
        count_sub = f"""
            SELECT cmj.chat_id, COUNT(*) AS n,
                   MIN(m.date) AS oldest, MAX(m.date) AS newest
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            WHERE {msg_filter}
            GROUP BY cmj.chat_id
        """
        query = f"""
            SELECT c.ROWID, c.display_name, c.chat_identifier,
                   COALESCE(s.n, 0), s.oldest, s.newest
            FROM chat c
            LEFT JOIN ({count_sub}) s ON s.chat_id = c.ROWID
        """
        params = list(count_params)
    else:
        query = """
            SELECT c.ROWID, c.display_name, c.chat_identifier,
                   COUNT(cmj.message_id) AS msgs,
                   MIN(m.date) AS oldest, MAX(m.date) AS newest
            FROM chat c
            LEFT JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            LEFT JOIN message m ON cmj.message_id = m.ROWID
        """
        params = []

    wheres: list[str] = []
    if args.name:
        wheres.append("(c.display_name LIKE ? OR c.chat_identifier LIKE ?)")
        params.extend([f"%{args.name}%", f"%{args.name}%"])
    if args.handle:
        wheres.append(
            "c.ROWID IN ("
            " SELECT chj.chat_id FROM chat_handle_join chj"
            " JOIN handle h ON chj.handle_id = h.ROWID"
            " WHERE h.id = ?"
            ")"
        )
        params.append(args.handle)
    if args.participant:
        needles = resolver.handles_for_name(args.participant)
        all_handles = con.execute("SELECT ROWID, id FROM handle").fetchall()
        matched: list[int] = []
        sub = args.participant.strip().lower()
        for rid, hid in all_handles:
            if hid is None:
                continue
            norm = _normalize_handle(hid)
            if (norm and norm in needles) or (sub and sub in hid.lower()):
                matched.append(rid)
        if not matched:
            print(
                f"No handles matched participant {args.participant!r}",
                file=sys.stderr,
            )
            return
        ph = ",".join("?" * len(matched))
        wheres.append(
            f"c.ROWID IN (SELECT chj.chat_id FROM chat_handle_join chj"
            f" WHERE chj.handle_id IN ({ph}))"
        )
        params.extend(matched)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    if since or until:
        query += " GROUP BY c.ROWID ORDER BY COALESCE(s.n, 0) DESC"
    else:
        query += " GROUP BY c.ROWID ORDER BY msgs DESC"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"

    rows = con.execute(query, params).fetchall()
    records = [
        {
            "chat_id": r[0],
            "display_name": r[1] or "",
            "identifier": r[2],
            "msg_count": r[3],
            "oldest": fmt_ts(r[4]) if r[4] else None,
            "newest": fmt_ts(r[5]) if r[5] else None,
        }
        for r in rows
    ]

    if args.format == "json":
        print(json.dumps(records, indent=2))
        return
    if args.format == "ndjson":
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return
    for rec in records:
        label = rec["display_name"] or rec["identifier"]
        print(
            f"chat_id={rec['chat_id']:<6} msgs={rec['msg_count']:<7} [{label}]  "
            f"{rec['oldest']} → {rec['newest']}"
        )


def cmd_participants(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    rows = con.execute(
        """
        SELECT h.ROWID, h.id, h.service,
               (SELECT COUNT(*) FROM message m
                JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                WHERE cmj.chat_id = ? AND m.handle_id = h.ROWID) AS mc
        FROM chat_handle_join chj
        JOIN handle h ON chj.handle_id = h.ROWID
        WHERE chj.chat_id = ?
        ORDER BY mc DESC
        """,
        (args.chat_id, args.chat_id),
    ).fetchall()
    records = [
        {
            "handle_id": r[0],
            "id": r[1],
            "service": r[2],
            "msg_count": r[3],
            "name": resolver.resolve(0, r[1]),
        }
        for r in rows
    ]
    if args.format == "json":
        print(json.dumps(records, indent=2))
        return
    if args.format == "ndjson":
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return
    for rec in records:
        print(
            f"handle_id={rec['handle_id']} msgs={rec['msg_count']:<5} "
            f"{rec['id']} ({rec['service']}) → {rec['name']}"
        )


def cmd_stats(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    msgs_all, msgs_content, _ = load_metric_messages(
        con, args.chat_id, since, until
    )

    per_sender: dict[str, dict] = {}
    for m in msgs_content:
        sender = resolver.resolve(m.is_me, m.hid)
        bucket = per_sender.setdefault(
            sender, {"count": 0, "lengths": [], "first": m.ns, "last": m.ns}
        )
        bucket["count"] += 1
        bucket["lengths"].append(m.text_len)
        if m.ns < bucket["first"]:
            bucket["first"] = m.ns
        if m.ns > bucket["last"]:
            bucket["last"] = m.ns

    content_ns = [m.ns for m in msgs_content]
    total = sum(b["count"] for b in per_sender.values())
    senders_out = []
    for sender, b in sorted(per_sender.items(), key=lambda kv: -kv[1]["count"]):
        senders_out.append(
            {
                "sender": sender,
                "count": b["count"],
                "share": round(b["count"] / total, 4) if total else 0,
                "median_length": (
                    int(statistics.median(b["lengths"])) if b["lengths"] else 0
                ),
                "first": fmt_ts(b["first"]),
                "last": fmt_ts(b["last"]),
            }
        )

    weekday, hour = weekday_hour_histograms(content_ns)
    report = {
        "chat_id": args.chat_id,
        "total_messages": total,
        "range": {
            "since": fmt_ts(content_ns[0]) if content_ns else None,
            "until": fmt_ts(content_ns[-1]) if content_ns else None,
        },
        "senders": senders_out,
        "weekday": weekday,
        "hour": hour,
        "top_days": top_days(content_ns),
        "longest_gap": longest_gap(content_ns),
    }

    if args.format in ("json", "ndjson"):
        print(json.dumps(report, indent=2 if args.format == "json" else None))
        return

    print(f"chat_id={args.chat_id}  total={total}")
    if content_ns:
        print(f"range={report['range']['since']} → {report['range']['until']}")
    print()
    print("Senders:")
    for s in senders_out:
        pct = f"{s['share'] * 100:.1f}%"
        print(
            f"  {s['sender']:<25} {s['count']:>6} ({pct:>6})  "
            f"median={s['median_length']}ch  first={s['first']}  last={s['last']}"
        )
    print()
    print("Weekday histogram:")
    weekday_peak = max(report["weekday"].values())
    for label in WEEKDAY_LABELS:
        n = report["weekday"][label]
        bar = "█" * max(1, int(40 * n / weekday_peak)) if n else ""
        print(f"  {label}  {n:>6}  {bar}")
    print()
    print("Hour-of-day histogram:")
    peak = max(report["hour"].values())
    for h in range(24):
        n = report["hour"][str(h)]
        bar = "█" * max(1, int(40 * n / peak)) if n and peak else ""
        print(f"  {h:02d}  {n:>6}  {bar}")
    print()
    print("Top days:")
    for d in report["top_days"]:
        print(f"  {d['date']}  {d['count']}")
    print()
    print(
        f"Longest gap: {report['longest_gap']['days']} days  "
        f"{report['longest_gap']['from']} → {report['longest_gap']['to']}"
    )


def cmd_search(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    chat_ids = args.chat_id or []
    from_rowids = _resolve_sender_filter(con, resolver, args.from_)
    not_from_rowids = _resolve_sender_filter(con, resolver, args.not_from)
    regex = re.compile(args.regex, re.IGNORECASE) if args.regex else None
    where, params = _build_where(
        chat_ids,
        args.keyword,
        since,
        until,
        exclude_keywords=args.not_keyword,
        require_all=args.all,
        from_rowids=from_rowids,
        not_from_rowids=not_from_rowids,
    )
    rows = _query_messages(con, where, params)
    include_chat = len(chat_ids) != 1
    count = _emit_messages(
        rows,
        resolver,
        args.format,
        include_chat=include_chat,
        max_len=args.max_len,
        regex=regex,
    )
    print(f"--- {count} messages ---", file=sys.stderr)


def cmd_window(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    center = parse_date(args.center)
    since = center - timedelta(minutes=args.before)
    until = center + timedelta(minutes=args.after)
    where, params = _build_where([args.chat_id], None, since, until)
    rows = _query_messages(con, where, params)
    count = _emit_messages(
        rows, resolver, args.format, max_len=args.max_len
    )
    print(f"--- {count} messages ---", file=sys.stderr)


def cmd_dump(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    from_rowids = _resolve_sender_filter(con, resolver, args.from_)
    not_from_rowids = _resolve_sender_filter(con, resolver, args.not_from)
    regex = re.compile(args.regex, re.IGNORECASE) if args.regex else None
    where, params = _build_where(
        [args.chat_id],
        args.keyword,
        since,
        until,
        exclude_keywords=args.not_keyword,
        require_all=args.all,
        from_rowids=from_rowids,
        not_from_rowids=not_from_rowids,
    )
    rows = _query_messages(con, where, params)
    count = _emit_messages(
        rows, resolver, args.format, max_len=args.max_len, regex=regex
    )
    print(f"--- {count} messages ---", file=sys.stderr)


def _merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping (start_ns, end_ns) pairs."""
    if not windows:
        return []
    windows.sort()
    merged = [windows[0]]
    for start, end in windows[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def cmd_anchor_sweep(args) -> None:
    """Keyword-search then auto-window around every hit.

    Solves the "reply-chain miss" problem in a single call: the keyword
    finds anchor messages, but acknowledgments ("yep", "agreed") don't
    contain the keyword — so we pull a window around each anchor and
    merge overlapping windows into contiguous passages.
    """
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    where, params = _build_where(
        [args.chat_id], args.keyword, since, until, require_all=args.all
    )
    anchors = _query_messages(con, where, params)
    if not anchors:
        print("--- 0 anchors, 0 messages ---", file=sys.stderr)
        return

    before_ns = args.before * 60 * 1_000_000_000
    after_ns = args.after * 60 * 1_000_000_000
    windows = [
        (int(row[2]) - before_ns, int(row[2]) + after_ns) for row in anchors
    ]
    merged = _merge_windows(windows)
    anchor_ns = {int(row[2]) for row in anchors}

    total = 0
    json_buffer: list[dict] = []
    for i, (start, end) in enumerate(merged, 1):
        sub_where = (
            "cmj.chat_id = ? AND m.date >= ? AND m.date <= ? "
            "AND (m.associated_message_type IS NULL "
            "OR m.associated_message_type < 2000)"
        )
        sub_params = [args.chat_id, start, end]
        rows = _query_messages(con, sub_where, sub_params)
        if args.format == "text":
            label = (
                f"--- sweep {i}/{len(merged)}  "
                f"{fmt_ts(start)} → {fmt_ts(end)} ---"
            )
            print(label)
        for row in rows:
            rec = _row_to_record(row, resolver)
            if rec is None:
                continue
            rec["sweep"] = i
            rec["is_anchor"] = int(row[2]) in anchor_ns
            if args.format == "ndjson":
                print(json.dumps(rec, ensure_ascii=False))
            elif args.format == "json":
                json_buffer.append(rec)
            else:
                marker = "⚓ " if rec["is_anchor"] else "  "
                line = _format_text(rec, max_len=args.max_len)
                print(f"{marker}{line}")
            total += 1
        if args.format == "text":
            print()
    if args.format == "json":
        print(json.dumps(json_buffer, ensure_ascii=False, indent=2))
    print(
        f"--- {len(anchors)} anchors, {len(merged)} merged windows, "
        f"{total} messages ---",
        file=sys.stderr,
    )


def cmd_attachments(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    wheres = ["cmj.chat_id = ?"]
    params: list = [args.chat_id]
    if since:
        wheres.append("m.date > ?")
        params.append(to_apple_ns(since))
    if until:
        wheres.append("m.date < ?")
        params.append(to_apple_ns(until))
    if args.mime_like:
        wheres.append("a.mime_type LIKE ?")
        params.append(f"%{args.mime_like}%")
    sql = f"""
        SELECT m.date, m.is_from_me, h.id,
               a.filename, a.transfer_name, a.mime_type, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        JOIN message m ON m.ROWID = maj.message_id
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE {' AND '.join(wheres)}
        ORDER BY m.date ASC
    """
    rows = con.execute(sql, params).fetchall()
    records = []
    for ns, is_me, hid, filename, transfer, mime, size in rows:
        records.append(
            {
                "ts": fmt_ts(ns),
                "sender": resolver.resolve(is_me, hid),
                "filename": filename,
                "transfer_name": transfer,
                "mime_type": mime,
                "bytes": size,
            }
        )
    if args.format == "json":
        print(json.dumps(records, indent=2))
        return
    if args.format == "ndjson":
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return
    for rec in records:
        size_kb = f"{(rec['bytes'] or 0) / 1024:.0f}KB"
        name = rec["transfer_name"] or rec["filename"] or "?"
        print(
            f"[{rec['ts']}] {rec['sender']:<20} {rec['mime_type'] or '?':<20} "
            f"{size_kb:>8}  {name}"
        )
    print(f"--- {len(records)} attachments ---", file=sys.stderr)


def _strip_guid_prefix(guid: str | None) -> str | None:
    if not guid:
        return None
    for prefix in ("p:0/", "p:1/", "bp:"):
        if guid.startswith(prefix):
            return guid[len(prefix):]
    for sep in ("/", ":"):
        if sep in guid:
            return guid.rsplit(sep, 1)[1]
    return guid


def cmd_reactions(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    wheres = [
        "cmj.chat_id = ?",
        "m.associated_message_type >= 2000",
    ]
    params: list = [args.chat_id]
    if since:
        wheres.append("m.date > ?")
        params.append(to_apple_ns(since))
    if until:
        wheres.append("m.date < ?")
        params.append(to_apple_ns(until))
    sql = f"""
        SELECT m.date, m.is_from_me, h.id,
               m.associated_message_type, m.associated_message_guid
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE {' AND '.join(wheres)}
        ORDER BY m.date ASC
    """
    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("--- 0 reactions ---", file=sys.stderr)
        return

    target_guids = {_strip_guid_prefix(r[4]) for r in rows}
    target_guids.discard(None)
    targets: dict[str, tuple[str, int, str | None]] = {}
    if target_guids:
        ph = ",".join("?" * len(target_guids))
        target_rows = con.execute(
            f"""
            SELECT t.guid, t.text, t.attributedBody, t.is_from_me, th.id
            FROM message t
            LEFT JOIN handle th ON t.handle_id = th.ROWID
            WHERE t.guid IN ({ph})
            """,
            list(target_guids),
        ).fetchall()
        for guid, text, att, is_me, hid in target_rows:
            content = get_message_text(text, att) or ""
            content = content.strip().replace("\n", " / ")
            targets[guid] = (content, is_me, hid)

    records = []
    for ns, is_me, hid, amt, amg in rows:
        label = REACTION_TYPES.get(amt, f"reacted ({amt})")
        target_guid = _strip_guid_prefix(amg)
        target_content, target_is_me, target_hid = targets.get(
            target_guid, ("", 0, None)
        )
        records.append(
            {
                "ts": fmt_ts(ns),
                "sender": resolver.resolve(is_me, hid),
                "reaction": label,
                "target_sender": resolver.resolve(target_is_me, target_hid),
                "target": target_content[: args.max_len] + ("…" if len(target_content) > args.max_len else ""),
            }
        )
    if args.format == "json":
        print(json.dumps(records, indent=2))
        return
    if args.format == "ndjson":
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return
    for rec in records:
        print(
            f"[{rec['ts']}] {rec['sender']} {rec['reaction']} "
            f"{rec['target_sender']}: \"{rec['target']}\""
        )
    print(f"--- {len(records)} reactions ---", file=sys.stderr)


def _human_secs(secs) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    if secs < 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _metrics_text(out: dict) -> None:
    m = out["metrics"]
    L = out["labels"]
    print(f"chat_id={out['chat_id']}  {L['me']} ⇄ {L['other']}")
    if out["range"]["since"]:
        print(f"range={out['range']['since']} → {out['range']['until']}")
    if "kpis" in m:
        k = m["kpis"]
        ratio = f"{k['ratio']:.2f}×" if k.get("ratio") is not None else "—"
        streak = f"{k.get('streak_days')}d ({'active' if k.get('streak_active') else 'ended'})"
        print()
        print(f"volume you/{L['other']}: {ratio}")
        print(f"{L['other']} reply  median={_human_secs(k.get('median_s'))}  "
              f"p80={_human_secs(k.get('p80_s'))}")
        print(f"streak: {streak}")
    if "message_share" in m:
        s = m["message_share"]
        print()
        print(f"messages: {s['total']}  "
              f"({L['me']} {s['me']['count']} / {L['other']} {s['other']['count']})")
    if "response_time" in m:
        rt = m["response_time"]["other"]["buckets"]
        print()
        print(f"{L['other']} response time: "
              + "  ".join(f"{k}={v}" for k, v in rt.items()))
    if "who_restarts" in m:
        wr = m["who_restarts"]
        print()
        print("restarts after silence (you/them): "
              + "  ".join(f"{t} {wr['counts'][t]['me']}/{wr['counts'][t]['other']}"
                          for t in wr["thresholds"]))
    if "texts_before_reply" in m:
        tb = m["texts_before_reply"]["me"]["buckets"]
        print()
        print("your texts before a reply: " + "  ".join(f"{k}={v}" for k, v in tb.items()))


def cmd_metrics(args) -> None:
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    selected = _parse_modules(args.modules)
    msgs_all, msgs_content, other_handles = load_metric_messages(
        con, args.chat_id, since, until
    )
    _guard_one_to_one(resolver, other_handles, args.chat_id)
    labels = _labels(resolver, other_handles)
    metrics = compute_metrics(
        msgs_all, msgs_content, selected,
        labels=labels, streak_silence_h=args.streak_silence_h,
    )
    out = {
        "chat_id": args.chat_id,
        "labels": labels,
        "range": {
            "since": fmt_ts(msgs_all[0].ns) if msgs_all else None,
            "until": fmt_ts(msgs_all[-1].ns) if msgs_all else None,
        },
        "modules": selected,
        "metrics": metrics,
    }
    if args.format == "ndjson":
        print(json.dumps(out, ensure_ascii=False))
    elif args.format == "json":
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _metrics_text(out)


def cmd_dashboard(args) -> None:
    # `dashboard` is a sibling module. Running this file as a script puts its
    # own directory on sys.path[0], so a plain import resolves it — no path
    # munging needed. Lazy so the query subcommands never import the renderer.
    try:
        import dashboard
    except ImportError:
        sys.exit(
            "Dashboard renderer not found. Expected scripts/dashboard.py "
            "next to this script."
        )
    con = open_db(args.db)
    resolver = ContactResolver(args.contacts)
    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until) if args.until else None
    selected = _parse_modules(args.modules)
    msgs_all, msgs_content, other_handles = load_metric_messages(
        con, args.chat_id, since, until
    )
    if not msgs_all:
        sys.exit(
            f"chat_id={args.chat_id} has no messages in range; nothing to render."
        )
    _guard_one_to_one(resolver, other_handles, args.chat_id)
    labels = _labels(resolver, other_handles)
    metrics = compute_metrics(
        msgs_all, msgs_content, selected,
        labels=labels, streak_silence_h=args.streak_silence_h,
    )

    annotations = None
    if args.annotations:
        with open(args.annotations) as f:
            annotations = json.load(f)
        schema = annotations.get("schema", "")
        if schema and not schema.startswith("imessage-dashboard-annotations/"):
            print(f"warning: unexpected annotations schema {schema!r}",
                  file=sys.stderr)

    first, last = fmt_ts(msgs_all[0].ns), fmt_ts(msgs_all[-1].ns)
    subtitle = f"{first[:10]} → {last[:10]}" if first and last else ""
    title = args.title or f"{labels['me']} ⇄ {labels['other']} — iMessage Dashboard"

    html_doc = dashboard.render_dashboard(
        metrics, selected, title=title, theme=args.theme,
        annotations=annotations, labels=labels, subtitle=subtitle,
    )
    if args.out:
        Path(args.out).write_text(html_doc, encoding="utf-8")
        print(f"--- wrote {args.out} ({len(html_doc)} bytes) ---", file=sys.stderr)
    else:
        sys.stdout.write(html_doc)


# ---------------------------------------------------------------------- #
# CLI                                                                    #
# ---------------------------------------------------------------------- #

def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("text", "json", "ndjson"),
        default="text",
        help="Output format (default text). ndjson streams one JSON "
             "object per line for piping.",
    )


def _add_date_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="Start date (YYYY-MM-DD[ HH:MM[:SS]])")
    parser.add_argument("--until", help="End date (exclusive)")


def _add_modules(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--modules",
        help="Comma-separated module names (default: all). Available: "
             + ", ".join(MODULES),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imessage", description=__doc__)
    parser.add_argument("--db", default=DB_DEFAULT, help="Path to chat.db")
    parser.add_argument(
        "--contacts",
        default=None,
        help=(
            "Optional JSON override mapping handle -> name. Contacts are "
            "auto-resolved from macOS AddressBook; this file is only for "
            "overrides / nicknames / the 'me' label. "
            f"Default if present: {CONTACTS_DEFAULT}"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("chats", help="List chats with counts and date ranges")
    p.add_argument("--name", help="Substring filter on display_name/identifier")
    p.add_argument(
        "--handle", help="Restrict to chats containing this phone/email"
    )
    p.add_argument(
        "--participant",
        help="Restrict to chats containing a contact resolved by name "
             "(via AddressBook). Unifies chats split across phone/email handles.",
    )
    p.add_argument("--limit", type=int)
    _add_date_range(p)
    _add_format(p)
    p.set_defaults(func=cmd_chats)

    p = sub.add_parser("participants", help="List participants of a chat")
    p.add_argument("chat_id", type=int)
    _add_format(p)
    p.set_defaults(func=cmd_participants)

    p = sub.add_parser(
        "stats", help="Per-participant stats, activity histograms, dormancy"
    )
    p.add_argument("chat_id", type=int)
    _add_date_range(p)
    _add_format(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("search", help="Keyword search across one or all chats")
    p.add_argument(
        "-k", "--keyword", action="append", required=True,
        help="Keyword to search (repeatable — default OR, use --all for AND)",
    )
    p.add_argument(
        "-K", "--not-keyword", action="append",
        help="Exclude messages containing this keyword (repeatable)",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Require all -k keywords to match (default is OR)",
    )
    p.add_argument(
        "--regex",
        help="Post-filter hits with a case-insensitive Python regex",
    )
    p.add_argument(
        "--chat-id", action="append", type=int,
        help="Restrict to chat_id (repeatable). Omit to search all chats.",
    )
    p.add_argument(
        "--from", dest="from_", action="append",
        help="Restrict to messages from a sender (name or handle substring; "
             "repeatable)",
    )
    p.add_argument(
        "--not-from", action="append",
        help="Exclude messages from a sender (name or handle substring; "
             "repeatable)",
    )
    _add_date_range(p)
    p.add_argument(
        "--max-len", type=int, default=500,
        help="Truncate long messages in text format (0 = no truncation)",
    )
    _add_format(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser(
        "window", help="Reply-chain context around a timestamp"
    )
    p.add_argument("chat_id", type=int)
    p.add_argument("center", help="Center timestamp (YYYY-MM-DD[ HH:MM[:SS]])")
    p.add_argument("--before", type=int, default=5, help="Minutes before (default 5)")
    p.add_argument("--after", type=int, default=30, help="Minutes after (default 30)")
    p.add_argument("--max-len", type=int, default=500)
    _add_format(p)
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("dump", help="All messages in a chat over a date range")
    p.add_argument("chat_id", type=int)
    _add_date_range(p)
    p.add_argument(
        "-k", "--keyword", action="append",
        help="Optional keyword filter (repeatable; default OR, --all for AND)",
    )
    p.add_argument("-K", "--not-keyword", action="append")
    p.add_argument("--all", action="store_true")
    p.add_argument("--regex")
    p.add_argument(
        "--from", dest="from_", action="append",
        help="Restrict to messages from a sender (name or handle substring)",
    )
    p.add_argument(
        "--not-from", action="append",
        help="Exclude messages from a sender (name or handle substring)",
    )
    p.add_argument("--max-len", type=int, default=500)
    _add_format(p)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser(
        "anchor-sweep",
        help="Keyword search → auto-windowed expansion → merged passages",
    )
    p.add_argument("chat_id", type=int)
    p.add_argument(
        "-k", "--keyword", action="append", required=True,
        help="Keyword to anchor on (repeatable; default OR, --all for AND)",
    )
    p.add_argument("--all", action="store_true")
    p.add_argument(
        "--before", type=int, default=5,
        help="Minutes before each anchor (default 5)",
    )
    p.add_argument(
        "--after", type=int, default=15,
        help="Minutes after each anchor (default 15)",
    )
    _add_date_range(p)
    p.add_argument("--max-len", type=int, default=500)
    _add_format(p)
    p.set_defaults(func=cmd_anchor_sweep)

    p = sub.add_parser(
        "attachments", help="List attachments in a chat over a date range"
    )
    p.add_argument("chat_id", type=int)
    _add_date_range(p)
    p.add_argument("--mime-like", help="Filter by mime_type substring")
    _add_format(p)
    p.set_defaults(func=cmd_attachments)

    p = sub.add_parser(
        "reactions", help="Surface tapbacks with their target messages"
    )
    p.add_argument("chat_id", type=int)
    _add_date_range(p)
    p.add_argument("--max-len", type=int, default=80)
    _add_format(p)
    p.set_defaults(func=cmd_reactions)

    p = sub.add_parser(
        "metrics",
        help="Compute 1:1 conversation metrics (response time, share, "
             "streaks, …) as JSON",
    )
    p.add_argument("chat_id", type=int)
    _add_modules(p)
    _add_date_range(p)
    p.add_argument(
        "--streak-silence-h", type=float, default=24.0,
        help="Gap (hours) that breaks a conversational streak (default 24)",
    )
    _add_format(p)
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser(
        "dashboard",
        help="Render a self-contained HTML dashboard for a 1:1 chat",
    )
    p.add_argument("chat_id", type=int)
    _add_modules(p)
    _add_date_range(p)
    p.add_argument(
        "--annotations",
        help="Path to a narrative annotations JSON (themes, quotes, arc) to "
             "merge into the dashboard",
    )
    p.add_argument("--out", help="Write HTML to this path (default: stdout)")
    p.add_argument("--title", help="Dashboard title")
    p.add_argument(
        "--theme", choices=("light", "dark"), default="light",
        help="Color theme (default light)",
    )
    p.add_argument(
        "--streak-silence-h", type=float, default=24.0,
        help="Gap (hours) that breaks a conversational streak (default 24)",
    )
    p.set_defaults(func=cmd_dashboard)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
