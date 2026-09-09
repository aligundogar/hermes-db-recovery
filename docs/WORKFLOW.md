# Recovery workflow (operations runbook)

Context: an AI-agent platform ("Hermes") keeps per-profile session databases
(`state.db`) in WAL mode. A latent SQLite bug corrupted the table-leaf pages
of every profile database. This is the exact workflow that recovered the data.

## Phase 0 - inventory and safety

```bash
# who is holding the databases open? stop them all, including daemons
fuser -v ~/.hermes/state.db ~/.hermes/profiles/*/state.db

# archive every corrupt file before ANY tool touches it
mkdir -p backups/corrupt-$(date +%Y%m%d)
cp -a --parents <corrupt files...> backups/corrupt-$(date +%Y%m%d)/
```

Rule: every recovery tool must work on a **copy**. Never run `sqlite3
.recover` against a live file.

## Phase 1 - vendor repair first

Always try the platform's own repair before carving. It is schema-aware and
safer.

```bash
<platform-cli> doctor --fix          # e.g. hermes doctor --fix
<platform-cli> sessions recover \
    --source  <profile>/state.db \
    --inspect-only
```

The inspector tells you which tables are readable. If it reports
`recoverable: false`, check whether a partial mode exists:

```bash
# needs a sqlite3 CLI >= 3.51 with the dbpage extension on PATH
<platform-cli> sessions recover --source state.db \
    --output recovered-state.db --allow-partial
```

If partial recovery yields 0 rows, the page content itself is shredded.

## Phase 2 - structured salvage with sqlite3 .recover

```bash
sqlite3 --version          # must be >= 3.51.x (older builds can worsen damage)
sqlite3 corrupt.db ".recover" > recover.sql 2> recover.err
```

- If this fails (`rc=11`, malformed) for both default and `--ignore-freelist`
  modes, the B-tree framing is destroyed. Go to Phase 3.
- If it succeeds, load `recover.sql` into a fresh DB and mine the
  `lost_and_found` table before anything else.

## Phase 3 - carve

```bash
python3 hermes_db_carver.py scan     corrupt.db     # verdict + page stats
python3 hermes_db_carver.py messages corrupt.db -o recovered/
python3 hermes_db_carver.py strings  corrupt.db -o corrupt_strings.txt
```

Decision table from `scan`:

| observation | meaning | action |
|---|---|---|
| `0x0d` (table leaf) pages present | framing alive | Phase 1/2 will work |
| only `0x02/0x0a/0x05` (index) pages | row data unframed | Phase 3 carving |
| mostly `zero` pages | content overwritten | little hope; carve anyway |
| `other/text` dominates | raw text survives unframed | carving is the only path |

## Phase 4 - restore function

1. Quarantine the corrupt file: `<db>/_corrupt-<date>/state.db.corrupt`
2. Remove stale sidecars: `state.db-wal`, `state.db-shm`, lock files
3. Start the service; it initializes a fresh database
4. `PRAGMA integrity_check` on the fresh file (expect `ok`)
5. Watch service logs for residual `malformed` errors

## Phase 5 - preserve the evidence

Keep, forever (or until explicitly destroyed):

- the corrupt originals
- recovery reports (inspect/JSON reports)
- carved output (`*_messages.jsonl`, `*_transcript.md`, `*_other_text.txt`)

Memories/personality files (e.g. `MEMORY.md`, `USER.md`, persona prompts) are
usually plain files next to the database and survive database corruption -
check them before declaring data loss.
