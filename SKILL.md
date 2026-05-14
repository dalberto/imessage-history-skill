---
name: imessage-history
description: |
  Search, summarize, and analyze the user's macOS iMessage history by
  querying ~/Library/Messages/chat.db directly with a bundled stdlib
  Python script. Trigger whenever the user wants to recall, search,
  cite, or characterize conversations from Messages — group-chat
  research ("what did we decide about X last spring?"), deep-history
  lookups ("when did we first talk about Y?"), reply-chain context
  ("pull the thread around that Mar 30 message"), participant rosters,
  per-chat statistics, attachment inventories, or cross-chat keyword
  sweeps. Trigger even when the user doesn't explicitly say "search my
  messages" — any recall or synthesis task grounded in their own
  conversation history belongs here, because the script handles
  attributedBody decoding, contact resolution, and exhaustive
  (non-truncated) queries that ad-hoc SQL against chat.db routinely
  gets wrong.
---

# iMessage History

Direct access to `~/Library/Messages/chat.db` via a single stdlib Python
script. Stdlib only — no install. The script bundles every gotcha that
makes naive chat.db queries return wrong answers:

- **`text` is usually NULL on modern macOS.** Since iOS 16 / macOS 13,
  message content moved to the `attributedBody` BLOB (an NSTypedStream
  archive). `CAST(attributedBody AS TEXT) LIKE '%foo%'` does not work —
  SQLite's implicit cast terminates at the first NUL byte, which is near
  the start of every typedstream blob. The script uses
  `instr(attributedBody, CAST('foo' AS BLOB)) > 0` for searching and
  decodes the blob in Python to display content.
- **Reactions inflate result sets.** Messages with
  `associated_message_type >= 2000` are tapbacks ("Loved …", "Liked …",
  etc.). They're filtered by default everywhere except the `reactions`
  subcommand; don't display them as primary content.
- **Multiple chats share a display_name.** The `chat` table accumulates
  historical rows — a single logical group chat may show up as 3–4 rows
  with the same `display_name` but different `ROWID`s, and usually only
  one has active messages. Always pick by `msg_count DESC` when
  resolving by name.
- **Apple timestamps are nanoseconds since 2001-01-01.** The script
  handles the conversion; raw queries need `date/1e9 + 978307200` to
  convert to Unix epoch.
- **A single contact may appear as multiple handles.** Phone + email
  for the same person show up as separate `handle` rows and separate
  chats. Use `chats --participant <name>` to unify them.
- **No hidden LIMIT.** Date-filtered queries return everything in the
  range. Naive chat.db wrappers often cap results after the date
  filter, which silently truncates historical searches.

## When to use

Trigger this skill whenever the user wants to recall, search, or
characterize something from their own Messages history:

- "search my imessage history for X"
- "summarize the <group chat> thread from last month"
- "find the message where <person> said Y"
- "what did we discuss about Z on <date>"
- "pull the thread around that message"
- "who's in the <chat name> group chat"
- "dump the last two weeks of messages in <chat>"
- "how active is the <chat> thread"
- Any recall or synthesis task spanning more than a few days.

## The tool

One Python script: `scripts/imessage.py`. Invoke with:

```fish
python3 <skill>/scripts/imessage.py <subcommand> [opts]
```

All content-returning subcommands accept `--format {text,json,ndjson}`.
Use `ndjson` when piping into other tools.

| Subcommand | Purpose |
| --- | --- |
| `chats` | List chats with counts and date ranges. `--name` filters on display name; `--participant <name>` resolves a contact through AddressBook and returns every chat that contact appears in (unifies phone/email splits). `--since/--until` count only within a window. |
| `participants <chat_id>` | List phone numbers / emails / resolved names in a chat with per-chat message counts. |
| `stats <chat_id>` | Per-participant counts + median length + first/last message, weekday and hour histograms, top-5 most active days, longest dormancy gap. Use when characterizing an unfamiliar chat. |
| `search -k KEYWORD` | Keyword search. Omit `--chat-id` to search all chats. `-k` repeatable (default OR, `--all` for AND). `-K` excludes. `--from`/`--not-from` filter by sender name or handle substring. `--regex` post-filters with a case-insensitive Python regex. |
| `window <chat_id> <timestamp>` | Pull all messages in a time window around a center point — reply-chain recovery. |
| `dump <chat_id>` | Pull every message in a chat over a date range. Same filter flags as `search`. |
| `anchor-sweep <chat_id> -k KEYWORD` | Runs a keyword search, then auto-pulls a window around every hit and merges overlapping windows into contiguous passages. Anchors are marked with ⚓. This is the single highest-leverage move for reply-chain research — replaces the manual search-then-window loop. |
| `attachments <chat_id>` | List attachments (filename, mime type, size, sender) over a date range. `--mime-like` filters by mime type substring. |
| `reactions <chat_id>` | Surface tapbacks (normally filtered) with their target messages. Useful for "how did the chat respond to X" without reading prose. |

Pass `--help` on any subcommand for its full flag list.

## Research recipes

The single-call subcommands above are the building blocks. These are
the workflow moves that compose them — each one is worth knowing
because they address a failure mode the raw subcommand doesn't prevent.

### 1. Characterize an unfamiliar chat

```fish
python3 <skill>/scripts/imessage.py chats --name <substring>
python3 <skill>/scripts/imessage.py participants <chat_id>
python3 <skill>/scripts/imessage.py stats <chat_id>
```

Resolve → roster → size it up. `stats` gives you per-participant
volume, activity rhythm, and dormancy gaps in one pass. Do this before
writing any summary — it grounds you in what the chat actually is.

### 2. Recover reply chains with anchor-sweep

```fish
python3 <skill>/scripts/imessage.py anchor-sweep <chat_id> \
  -k <keyword> --since <date> --before 5 --after 15
```

This is the step most often skipped and most often needed. Replies to
a pivotal message often don't contain the keyword — an "agreed" or
"smart take" response won't show up in a bare keyword search.
`anchor-sweep` finds anchor messages, pulls a window around each, and
merges overlapping windows automatically. Anchors are marked with ⚓.

### 3. Cross-chat sweep

```fish
python3 <skill>/scripts/imessage.py search -k <keyword> --since <date>
```

Side threads happen. Run the keyword sweep without `--chat-id` to
catch discussion that spilled into DMs. This is a required check
whenever you're doing deep research on a named topic.

### 4. Unify split phone/email chats

```fish
python3 <skill>/scripts/imessage.py chats --participant "<name>"
```

A single contact can show up in separate chats — one by phone, one by
email. `--participant` resolves the name through AddressBook and
returns every chat that contact is in.

### 5. Sender-pair conversations inside a group

```fish
python3 <skill>/scripts/imessage.py dump <chat_id> \
  --from "<person A>" --from "<person B>" --since <date>
```

Extract the A↔B subthread from within a noisier group chat. `--from`
accepts either a name (resolved via AddressBook) or a handle substring
and is repeatable — all provided senders are OR'd.

### 6. Dormancy detection and reactivation

```fish
python3 <skill>/scripts/imessage.py stats <chat_id>
python3 <skill>/scripts/imessage.py window <chat_id> "<gap-end date>" \
  --before 60 --after 360
```

`stats` reports the longest gap between consecutive messages. Window
around the reactivation moment to see why the chat came back to life —
a major life event, a new project, a conflict.

### 7. Delta between two time ranges

```fish
python3 <skill>/scripts/imessage.py stats <chat_id> --since <A1> --until <A2>
python3 <skill>/scripts/imessage.py stats <chat_id> --since <B1> --until <B2>
```

Compare two eras to detect a shift — who drives the conversation now
vs. then, whether the chat's cadence has moved earlier or later in the
day, whether a new participant took over. No new subcommand needed;
just run `stats` twice.

### 8. Attachment inventory

```fish
python3 <skill>/scripts/imessage.py attachments <chat_id> \
  --since <date> --mime-like image
```

Find shared media over a range. Pipe through `--format ndjson` into
downstream tools if you need to process the list programmatically.

### 9. Sentiment peek via reactions

```fish
python3 <skill>/scripts/imessage.py reactions <chat_id> --since <date>
```

Tapbacks are a compact signal for how the chat reacted to specific
messages. Pull reactions over a date range when you want to gauge
response without reading the full prose.

## Contact resolution

Sender names are resolved automatically from the macOS AddressBook —
no setup required. The script scans every source under
`~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb`
on first lookup, normalizes phone numbers (strips non-digits, keeps
the last 10) and emails (lowercase), and caches results per-process.

Lookup precedence per handle:

1. **JSON override** — the skill-local `contacts.json` at the skill
   root (next to `SKILL.md`), or a custom path via `--contacts PATH`.
   Use this for contacts not in AddressBook or for custom labels.
2. **macOS AddressBook** — all sources (iCloud, local, Google, etc.)
   are merged automatically.
3. **Raw handle** (phone/email) — fallback when no match is found.

The override file uses `handle.id` values as keys and ships as
`contacts.example.json`:

```json
{
  "me": "YourName",
  "+15551234567": "Alex Example",
  "alex@example.com": "Alex Example"
}
```

The `me` key is the only special one — it controls how the user's own
messages (`is_from_me = 1`) are labeled. Without it they show as "Me".
Everything else is just an override for AddressBook misses or
nicknames.

Copy `contacts.example.json` to `contacts.json` (gitignored) to start
customizing. AddressBook is the source of truth; most users won't need
an override file at all.

## Synthesis rules

When summarizing Messages history for the user:

- **Always cite verbatim** with timestamp + sender:
  `[YYYY-MM-DD HH:MM:SS] Sender: "..."`
- **Report the actual date range you found**, not the range the user
  asked about. If they asked for "the last month" and messages only go
  back to last week, say so explicitly.
- **Use `anchor-sweep` instead of raw `search` + manual windowing**.
  It's one call, handles merging automatically, and you never forget
  to pull context around a hit.
- **Don't conflate threads.** A single message may be a reply to an
  unrelated preceding message that happened to use your keyword.
  `anchor-sweep` mitigates this, but check the window before asserting
  intent.
- **Cross-check DMs** for any named participant when doing deep
  research — side threads happen.
- **Skip reactions in prose summaries** unless the user asked about
  them specifically. The script filters tapbacks from every
  content-returning subcommand except `reactions`.
- **Fact-check before quoting.** Agents that skim large dumps can
  misattribute quotes. Re-run a narrow `search` or `window` to confirm
  exact wording and sender before putting a quote in a final summary.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `unable to open database file` | The process needs Full Disk Access. System Settings → Privacy & Security → Full Disk Access → add your terminal app and restart it. |
| Zero results on a known message | You're probably querying `text` — `text` is NULL on modern macOS. The script handles this via `instr(attributedBody, CAST(? AS BLOB))`. |
| `LIKE '%foo%'` returns nothing on `attributedBody` | SQLite's implicit cast terminates at the first NUL byte. Use the script or `instr(attributedBody, CAST(? AS BLOB))`. |
| Wrong sender shown | Add an override to `contacts.json` next to `SKILL.md`. |
| Same contact appears in multiple chats | Expected — phone and email register as separate handles. Use `chats --participant <name>` to unify them. |
| Search results cap at the newest few days | Check whether the tool you're using silently limits results. The script here has no hidden LIMIT; any convenience wrapper that applies LIMIT *after* the date filter will bottom out on historical queries. |

## Reference

See `references/chatdb_schema.md` for the minimal chat.db schema
(tables, columns, gotchas) this skill depends on.
