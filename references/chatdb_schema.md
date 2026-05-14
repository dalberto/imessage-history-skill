# `chat.db` schema — the parts that matter

macOS stores iMessage history in a SQLite database at
`~/Library/Messages/chat.db`. The full schema is large; these are the tables
and columns the `imessage-history` tool actually uses, plus the gotchas that
trip up ad-hoc SQL.

## Why naive queries return wrong answers

Most "just SELECT from chat.db" tutorials get two things wrong:

1. **`message.text` is usually NULL on modern macOS.** Since iOS 16 /
   macOS 13, message content lives in `attributedBody` — an
   NSTypedStream-archived `NSAttributedString` blob.
   `CAST(attributedBody AS TEXT) LIKE '%foo%'` returns nothing because
   SQLite's implicit cast terminates at the first NUL byte, which is
   near the start of every typedstream blob. To search content,
   `instr(attributedBody, CAST(? AS BLOB)) > 0` works in SQL, and the
   actual text has to be decoded from the typedstream in Python (or
   another typedstream-aware client).

2. **Convenience wrappers silently cap results.** Many GUI tools and
   tutorials apply a `LIMIT` *after* the date filter, so a query for
   "the last year" quietly returns only the newest few hundred rows.
   This tool never applies hidden limits.

The decoder in `scripts/imessage.py` reads the typedstream byte stream
directly, extracting only the primary NSString payload and ignoring the
OpenGraph link-preview metadata that often shares the blob.

## `message`

| Column | Notes |
| --- | --- |
| `ROWID` | Primary key |
| `date` | Apple nanoseconds since 2001-01-01. Convert: `date/1e9 + 978307200` → Unix epoch |
| `text` | Plain-text content. **NULL on modern macOS** — use `attributedBody` |
| `attributedBody` | BLOB: NSTypedStream-serialized NSAttributedString with the message content |
| `is_from_me` | 1 = sent by me, 0 = received |
| `handle_id` | FK to `handle.ROWID` (sender identity when not from me) |
| `associated_message_guid` | If this message is a reaction, points to its target |
| `associated_message_type` | **≥ 2000 indicates a reaction/tapback** (Loved, Liked, Emphasized, …). Filter these out for clean content. |
| `cache_roomnames` | Chat identifier (backup when join tables are incomplete) |

## `chat`

| Column | Notes |
| --- | --- |
| `ROWID` | Primary key — what the skill calls `chat_id` |
| `display_name` | User-set group chat name (often empty for 1:1s) |
| `chat_identifier` | Stable ID — phone number for 1:1s, `chat<n>` or `<uuid>` for groups |

**Gotcha**: the `chat` table accumulates historical rows — a single logical
group chat can appear as 3–4 rows with the same `display_name` but different
`ROWID`s. Always pick the one with the highest `msg_count`.

## `chat_message_join`

| Column | Notes |
| --- | --- |
| `chat_id` | FK to `chat.ROWID` |
| `message_id` | FK to `message.ROWID` |

The join that connects messages to chats.

## `handle`

| Column | Notes |
| --- | --- |
| `ROWID` | Primary key |
| `id` | Phone number (`+1NNNNNNNNNN`) or email |
| `service` | `iMessage`, `SMS`, `RCS`, etc. |

The same phone number can have multiple `handle` rows across services
(iMessage vs SMS). Join through `chat_handle_join` to find chat
participants.

## `chat_handle_join`

| Column | Notes |
| --- | --- |
| `chat_id` | FK to `chat.ROWID` |
| `handle_id` | FK to `handle.ROWID` |

## `attachment`

| Column | Notes |
| --- | --- |
| `ROWID` | Primary key |
| `filename` | On-disk filename of the cached attachment |
| `transfer_name` | Original filename as sent |
| `mime_type` | `image/png`, `image/heic`, `video/mp4`, etc. May be NULL for plugin payloads (e.g. link previews, stickers) |
| `total_bytes` | File size |

Joined to messages via `message_attachment_join` — one message can have
multiple attachments.

## `message_attachment_join`

| Column | Notes |
| --- | --- |
| `message_id` | FK to `message.ROWID` |
| `attachment_id` | FK to `attachment.ROWID` |

## Reactions (tapbacks)

Reactions are stored as first-class `message` rows with
`associated_message_type >= 2000`. The `associated_message_guid`
points to the target message and is typically prefixed (`p:0/`,
`p:1/`, `bp:`) before the target's `guid`. Strip the prefix and look
up `message.guid` to resolve the target.

Type numbers:

| Range | Meaning |
| --- | --- |
| 2000–2005 | loved, liked, disliked, laughed at, emphasized, questioned |
| 2006 | custom emoji / sticker tapback |
| 3000–3006 | corresponding "removed" variants |

## `attributedBody` decoding

The modern content column is an NSTypedStream archive. The embedded string
appears after the `NSString` type marker, framed as:

```
\x01\x2B [length] [utf-8 bytes]
```

Length encoding:

- Default: next byte is the length (`0x00`–`0x7F`)
- `0x81`: next 2 bytes are the length (little-endian uint16)
- `0x82`: next 4 bytes are the length (little-endian uint32)

Typedstream also uses `0x84` as a class-type marker and `0x85` as an object
marker — not relevant for extracting the primary string but useful to know
if you're debugging a malformed blob.

## Common gotchas

1. **`CAST(attributedBody AS TEXT) LIKE '%foo%'` does not work.** SQLite's
   implicit cast terminates at the first NUL byte, which typedstream has
   near the start. Use `instr(attributedBody, CAST('foo' AS BLOB)) > 0`.
2. **`text` is NULL for most recent messages.** Since iOS 16 / macOS 13,
   content lives in `attributedBody`. Old messages may still have `text`
   populated, so query both with `COALESCE`/`OR`.
3. **Full Disk Access is required** to open `chat.db`. Grant it to the
   terminal process or you'll see `unable to open database file`.
4. **Messages.app write-locks the DB** in some workflows. Always open
   read-only (`file:path?mode=ro`).
5. **Very old chat.db versions stored `date` in seconds**, not nanoseconds.
   Heuristic: `len(str(date)) > 10` → nanoseconds, else seconds.
6. **Link-preview metadata inflates `attributedBody` size**. A 1500-byte
   blob decoding to a 50-char URL is normal — OpenGraph metadata lives in
   the same blob.
