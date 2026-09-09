#!/usr/bin/env python3
"""
hermes-db-import - Re-import carved chat messages into a fresh Hermes state.db.

The counterpart to hermes_db_carver.py: after a corrupted database has been
quarantined and a fresh one initialized, this tool puts the recovered
conversation rows back so session search and history can find them again.

Input:  <profile>_messages.jsonl rows shaped
        {"session": "YYYYMMDD_HHMMSS_xxxxxxx", "role": "user|assistant|system|tool",
         "content": "..."}

What it does:
  1. For every session id, derives started_at from the id's timestamp and
     INSERT OR IGNOREs a sessions row (source = --source, default "carved-import").
  2. Re-inserts messages with a deterministic platform_message_id marker
     ("carved:<sha1[:16]>") so re-running the import is idempotent.
  3. Spreads message timestamps at --spacing seconds from session start.
     Carved rows carry no original timestamps - these are approximations.
  4. Updates per-session message_count / tool_call_count / last_activity_at.
  5. Runs PRAGMA integrity_check and prints a summary.

Stop the owning gateway/service before importing (WAL writer contention), and
back up the target database first.

Usage:
    python3 hermes_db_import.py profile_messages.jsonl \
        --db ~/.hermes/profiles/profile_x/state.db [--dry-run] [--source carved-import]
"""

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys


def session_start_epoch(session_id):
    """Derive epoch seconds from a YYYYMMDD_HHMMSS_xxxxxxx session id."""
    try:
        dt = datetime.datetime.strptime(session_id[:15], "%Y%m%d_%H%M%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def rows_from_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("session") and r.get("role") and r.get("content") is not None:
                yield r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", help="carved messages JSONL (from hermes_db_carver.py messages)")
    ap.add_argument("--db", required=True, help="target state.db (must exist)")
    ap.add_argument("--source", default="carved-import", help="sessions.source value for imported sessions")
    ap.add_argument("--spacing", type=float, default=30.0, help="seconds between reconstructed message timestamps")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen, write nothing")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"error: target db not found: {args.db}", file=sys.stderr)
        sys.exit(2)

    rows = list(rows_from_jsonl(args.jsonl))
    con = sqlite3.connect(args.db)
    existing_sessions = {r[0] for r in con.execute("SELECT id FROM sessions")}
    existing_markers = {
        r[0] for r in con.execute(
            "SELECT platform_message_id FROM messages WHERE platform_message_id LIKE 'carved:%'")
    }

    per_session = {}
    order = []
    for r in rows:
        sid = r["session"]
        if sid not in per_session:
            per_session[sid] = []
            order.append(sid)
        per_session[sid].append(r)

    new_sessions = [s for s in order if s not in existing_sessions]
    print(f"jsonl: {len(rows)} rows / {len(per_session)} sessions | "
          f"sessions already in db: {len(per_session) - len(new_sessions)} | to create: {len(new_sessions)}")

    if args.dry_run:
        would_insert = skipped = 0
        for sid in order:
            base = session_start_epoch(sid) or 0
            for i, r in enumerate(per_session[sid]):
                marker = "carved:" + hashlib.sha1(
                    (sid + r["role"] + r["content"]).encode("utf-8", "replace")).hexdigest()[:16]
                if marker in existing_markers:
                    skipped += 1
                else:
                    would_insert += 1
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"[dry-run] would create sessions: {len(new_sessions)} | "
              f"would insert messages: {would_insert} | duplicates skipped: {skipped}")
        print(f"db now: integrity {check} - nothing written")
        sys.exit(0)

    created = inserted = skipped = 0
    touched = set()
    con.execute("BEGIN")
    for sid in order:
        base = session_start_epoch(sid) or os.path.getmtime(args.jsonl)
        if sid not in existing_sessions:
            con.execute(
                "INSERT OR IGNORE INTO sessions (id, source, started_at, last_activity_at) "
                "VALUES (?, ?, ?, ?)", (sid, args.source, base, base))
            created += 1
        for i, r in enumerate(per_session[sid]):
            marker = "carved:" + hashlib.sha1(
                (sid + r["role"] + r["content"]).encode("utf-8", "replace")).hexdigest()[:16]
            if marker in existing_markers:
                skipped += 1
                continue
            cur = con.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, platform_message_id, active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (sid, r["role"], r["content"], base + i * args.spacing, marker))
            if cur.rowcount:
                inserted += 1
                touched.add(sid)
            else:
                skipped += 1
    con.execute(
        "UPDATE sessions SET "
        "  message_count = (SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.id), "
        "  tool_call_count = (SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.id AND m.role='tool'), "
        "  last_activity_at = COALESCE("
        "      (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = sessions.id), last_activity_at) "
        "WHERE id IN (SELECT DISTINCT session_id FROM messages WHERE platform_message_id LIKE 'carved:%')"
    )
    con.execute("COMMIT")

    check = con.execute("PRAGMA integrity_check").fetchone()[0]
    n_msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_sess = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"created sessions: {created} | inserted messages: {inserted} | duplicates skipped: {skipped}")
    print(f"db now: {n_sess} sessions, {n_msgs} messages | integrity: {check}")
    con.close()
    sys.exit(0 if check == "ok" else 1)


if __name__ == "__main__":
    main()
