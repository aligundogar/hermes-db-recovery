# hermes-db-recovery

Recover readable content from badly corrupted SQLite databases - built for
[Hermes](https://github.com/nousresearch) AI-agent `state.db` files, but usable
on any SQLite file with chat-style rows.

When a database loses its B-tree structure (botched WAL recovery, zeroed page
headers, overwritten leaf pages), everything standard fails:

```
$ sqlite3 corrupt.db "PRAGMA integrity_check"
DatabaseError: database disk image is malformed

$ sqlite3 corrupt.db ".recover"
Error: ... malformed (11)
```

The bytes of your messages are still in the file - only the page framing that
lets SQLite *find* them is gone. This toolkit parses raw pages when framing
survives, and carves records/text by byte-pattern when it does not.

## What you get

| mode       | what it does | needs B-tree framing? |
|------------|--------------|-----------------------|
| `scan`     | page-type histogram + verdict (how dead is the file) | no |
| `strings`  | high-quality UTF-8 text runs | no |
| `carve`    | record-pattern carving with exact byte accounting | no |
| `messages` | extract `<session_id><role><content>` chat rows to JSONL + Markdown | no |

## Quickstart

```bash
# 1. how dead is it?
python3 hermes_db_carver.py scan corrupt.db

# 2. is there any table-leaf structure left? try the standard route first
sqlite3 corrupt.db ".recover" > recover.sql

# 3. structure is gone -> carve the content out
python3 hermes_db_carver.py messages corrupt.db -o recovered/
# -> recovered/corrupt_messages.jsonl
# -> recovered/corrupt_transcript.md
# -> recovered/corrupt_other_text.txt

python3 hermes_db_carver.py strings corrupt.db -o corrupt_strings.txt
```

## Restore carved messages back into a fresh database

Carving produces `<profile>_messages.jsonl`. To put that history back into the
freshly initialized database (so session search finds the old conversations):

```bash
# preview first
python3 hermes_db_import.py profile_messages.jsonl \
    --db ~/.hermes/profiles/profile_x/state.db --dry-run

# stop the owning service, then import (idempotent - safe to re-run)
python3 hermes_db_import.py profile_messages.jsonl \
    --db ~/.hermes/profiles/profile_x/state.db
```

- Sessions are recreated with `source = "carved-import"`, `started_at` derived
  from the session id timestamp.
- Messages get a deterministic `platform_message_id` marker
  (`carved:<sha1[:16]>`) - re-running skips what is already there.
- Message timestamps are reconstructed approximations (carved rows have no
  original timestamps).
- Original roles (user/assistant/system/tool) are preserved.

## The recovery workflow that worked

See [docs/WORKFLOW.md](docs/WORKFLOW.md) for the full operations runbook and
[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) for the file-format background.

Short version:

1. **Stop every writer** (gateways, daemons, stray children). Check with `fuser`.
2. **Back up the corrupt file** before touching anything.
3. `hermes doctor --fix` / vendor-repair first - it is safer than any carving.
4. `hermes sessions recover --allow-partial` with a modern `sqlite3` on PATH.
5. Only then carve: `scan` -> `messages` -> `strings`.
6. Re-initialize the fresh database and restore function; keep the corrupt
   original archived for future forensic passes.

## Results on real corruption

From five corrupted agent-profile databases (WAL-reset bug, all table-leaf
pages destroyed, ~87 MB total): **3,887 chat messages** and ~28k auxiliary text
runs recovered by `messages` + `strings` carving after every structured
recovery path (vendor tool, `.recover`) returned zero usable rows.

## Limitations

- Row framing is destroyed, so roles/sessions are reconstructed by pattern,
  not read from schema. Expect some fragmentation and mixing.
- Overflowed payloads (very large rows) are recovered as fragments.
- Deleted/overwritten content is gone; carving finds bytes that survive.
- UTF-16 databases are not handled by the text carver (UTF-8 only).

## Requirements

Python 3.8+, standard library only.

## License

MIT - see [LICENSE](LICENSE).
