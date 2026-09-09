#!/usr/bin/env python3
"""
hermes-db-carver - Recover readable content from badly corrupted SQLite databases.

When a SQLite file loses its B-tree structure (botched WAL recovery, zeroed page
headers, overwritten leaf pages), normal tools fail:

    sqlite3 corrupt.db "PRAGMA integrity_check"  ->  database disk image is malformed
    sqlite3 corrupt.db ".recover"                ->  often aborts outright

This tool does not need page framing to work. It scans the raw file for
byte patterns that look like SQLite record cells and for readable UTF-8
text runs, then reconstructs whatever it can:

  scan    page-type histogram + header sanity (tells you *how* dead the file is)
  strings high-quality UTF-8 text runs (schema, config, message fragments)
  carve   record-pattern carving: (payload, rowid, header, serial-types) tuples
          validated by exact byte accounting, keyed on embedded text runs
  messages  extract chat-message rows of the shape
            "<session_id><role><content>" (roles: user/assistant/system/tool)
            commonly found in agent/chat session databases

Output is written as JSONL (structured) and Markdown (human-readable).

This was built to recover session data from corrupted Hermes (AI agent)
state databases, but it works on any SQLite file with similar row shapes.

Usage:
    python3 hermes_db_carver.py scan    corrupt.db
    python3 hermes_db_carver.py strings corrupt.db [--min-len 25] [-o out.txt]
    python3 hermes_db_carver.py carve   corrupt.db [-o outdir]
    python3 hermes_db_carver.py messages corrupt.db [-o outdir]

Exit codes: 0 = some content recovered, 1 = nothing recovered, 2 = bad input.
"""

import argparse
import json
import os
import re
import struct
import sys
from collections import Counter, defaultdict

TEXT_RUN = re.compile(rb"[\x20-\x7e\xc3][\x20-\x7e\x80-\xbf\xc2-\xf5]{60,}")
MSG_RX = re.compile(r"^(\d{8}_\d{6}_[0-9a-f]{4,})(user|assistant|system|tool)\b(.*)$", re.S)
SCHEMA_NOISE = re.compile(
    r"sqlite_autoindex|CREATE (TABLE|INDEX|TRIGGER|VIEW)|^index|^table|^trigger|"
    r"FOREIGN KEY|REFERENCES|PRAGMA|_fts|INTEGER PRIMARY KEY|NOT NULL|"
    r"AFTER (INSERT|DELETE|UPDATE)|COALESCE",
    re.I,
)


# --------------------------------------------------------------------------
# SQLite page-level parser (works only if B-tree framing survived)
# --------------------------------------------------------------------------
class SQLitePageParser:
    def __init__(self, path):
        self.data = open(path, "rb").read()
        if self.data[:16] != b"SQLite format 3\x00":
            raise ValueError("not a SQLite file (bad magic header)")
        ps = struct.unpack(">H", self.data[16:18])[0]
        self.page_size = 65536 if ps == 1 else ps
        self.reserved = self.data[20]
        self.usable = self.page_size - self.reserved
        self.n_pages = len(self.data) // self.page_size

    def page(self, n):
        off = (n - 1) * self.page_size
        return self.data[off : off + self.page_size]

    @staticmethod
    def varint(buf, i):
        v = 0
        for k in range(9):
            if i + k >= len(buf):
                return None, None
            b = buf[i + k]
            if k == 8:
                return (v << 8) | b, i + 9
            v = (v << 7) | (b & 0x7F)
            if not b & 0x80:
                return v, i + k + 1
        return None, None

    def read_serial(self, buf, i, st):
        if st == 0:
            return None, i
        if st in (1, 2, 3, 4, 5, 6):
            n = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[st]
            return int.from_bytes(buf[i : i + n], "big", signed=True), i + n
        if st == 7:
            return struct.unpack(">d", buf[i : i + 8])[0], i + 8
        if st == 8:
            return 0, i
        if st == 9:
            return 1, i
        if st >= 12:
            n = (st - 12) // 2 if st % 2 == 0 else (st - 13) // 2
            raw = buf[i : i + n]
            return (raw.decode("utf-8", "replace") if st % 2 else raw), i + n
        return None, i

    def type_histogram(self):
        hist = Counter()
        for n in range(1, self.n_pages + 1):
            pg = self.page(n)
            t = pg[100] if n == 1 else pg[0]
            if t in (0x0D, 0x05, 0x0A, 0x02):
                hist[hex(t)] += 1
            elif pg == b"\x00" * len(pg):
                hist["zero"] += 1
            else:
                hist["other/text"] += 1
        return hist

    def parse_leaf_table_cells(self, pageno):
        """Yield (pageno, rowid, serial_types, values) for live cells (0x0D pages)."""
        pg = self.page(pageno)
        base = 100 if pageno == 1 else 0
        if pg[base] != 0x0D:
            return
        ncells = struct.unpack(">H", pg[base + 3 : base + 5])[0]
        for c in range(ncells):
            off = struct.unpack(">H", pg[base + 8 + 2 * c : base + 10 + 2 * c])[0]
            try:
                K, i = self.varint(pg, off)
                rowid, i = self.varint(pg, i)
                payload = self._payload(pg, i, K)
                hsz, j = self.varint(payload, 0)
                stypes, j2 = [], j
                while j2 < hsz:
                    st, j2 = self.varint(payload, j2)
                    stypes.append(st)
                vals, k = [], hsz
                for st in stypes:
                    v, k = self.read_serial(payload, k, st)
                    vals.append(v)
                yield pageno, rowid, stypes, vals
            except Exception:
                continue

    def _payload(self, pg, off, K):
        X = self.usable - 35
        if K <= X:
            return pg[off : off + K]
        M = ((self.usable - 12) * 32 // 255) - 23
        L = M + (K - M) % (self.usable - 4)
        if L > X:
            L = M
        chunks = [pg[off : off + L]]
        nxt = struct.unpack(">I", pg[off + L : off + L + 4])[0]
        seen = set()
        while nxt and nxt not in seen and nxt <= self.n_pages and len(chunks) < 100000:
            seen.add(nxt)
            opg = self.page(nxt)
            nxt = struct.unpack(">I", opg[:4])[0]
            take = min(self.usable - 4, K - sum(map(len, chunks)))
            chunks.append(opg[4 : 4 + take])
            if sum(map(len, chunks)) >= K:
                break
        return b"".join(chunks)


# --------------------------------------------------------------------------
# Text-run carving (works even when page framing is destroyed)
# --------------------------------------------------------------------------
def carve_text_runs(data, min_len=25):
    runs, seen = [], set()
    for seg in re.split(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]+", data):
        if len(seg) < min_len:
            continue
        s = seg.decode("utf-8", "ignore").strip()
        if len(s) < min_len:
            continue
        printable = sum(c.isprintable() or c in "\n\t" for c in s) / max(1, len(s))
        if printable < 0.93:
            continue
        key = hash(s[:60])
        if key not in seen:
            seen.add(key)
            runs.append(s)
    return runs


# --------------------------------------------------------------------------
# Record-pattern carving (exact byte accounting)
# --------------------------------------------------------------------------
def _varint(buf, i):
    v = 0
    for k in range(9):
        if i + k >= len(buf):
            return None, None
        b = buf[i + k]
        if k == 8:
            return (v << 8) | b, i + 9
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i + k + 1
    return None, None


def carve_records(path, min_text=20):
    data = open(path, "rb").read()
    n = len(data)
    found = []
    for o in range(0, n - 40):
        if data[o] < 30:
            continue
        K, j = _varint(data, o)
        if K is None or not (30 <= K <= 300000) or j + K > n:
            continue
        rowid, j2 = _varint(data, j)
        if rowid is None or not (0 < rowid < 10 ** 9):
            continue
        hsz, j3 = _varint(data, j2)
        if hsz is None or not (2 <= hsz <= 80) or j2 + hsz > j + K:
            continue
        stypes, k = [], j3
        ok = True
        while k < j2 + hsz:
            st, k2 = _varint(data, k)
            if st is None or st in (10, 11) or st > 13 + 2 * 200000:
                ok = False
                break
            stypes.append(st)
            k = k2
        if not ok or k != j2 + hsz or not stypes:
            continue
        vals_end = j2 + hsz
        texts = []
        for st in stypes:
            if st in (1, 2, 3, 4):
                vals_end += st
            elif st in (5, 6, 7):
                vals_end += 6 if st == 5 else 8
            elif st >= 12:
                ln = (st - 12) // 2 if st % 2 == 0 else (st - 13) // 2
                vals_end += ln
                if st % 2 and ln >= min_text:
                    texts.append((vals_end - ln, ln))
            if vals_end > j + K:
                ok = False
                break
        if not ok or vals_end != j + K or not texts:
            continue
        good = []
        for tstart, tlen in texts:
            raw = data[tstart : tstart + tlen]
            try:
                s = raw.decode("utf-8")
            except UnicodeDecodeError:
                ok = False
                break
            if sum(c.isprintable() or c in "\n\t" for c in s) / max(1, len(s)) < 0.9:
                ok = False
                break
            good.append(s)
        if ok:
            found.append({"offset": o, "rowid": rowid, "ncols": len(stypes), "texts": good})
    return found


# --------------------------------------------------------------------------
# Chat-message extraction (session_id + role + content runs)
# --------------------------------------------------------------------------
def extract_messages(data, min_len=30):
    messages, others = [], []
    seen = set()
    for seg in re.split(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]+", data):
        if len(seg) < min_len:
            continue
        s = seg.decode("utf-8", "ignore").strip()
        if len(s) < min_len:
            continue
        if sum(c.isprintable() or c in "\n\t" for c in s) / max(1, len(s)) < 0.93:
            continue
        if SCHEMA_NOISE.search(s[:500]):
            continue
        m = MSG_RX.match(s)
        if m and len(m.group(3)) >= 20:
            key = (m.group(1), m.group(2), hash(m.group(3)[:200]))
            if key in seen:
                continue
            seen.add(key)
            messages.append({"session": m.group(1), "role": m.group(2), "content": m.group(3).strip()})
        else:
            others.append(s)
    return messages, others


def write_markdown(out, prof, msgs, sessions_order):
    md = os.path.join(out, f"{prof}_transcript.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# {prof} - carved conversation transcripts\n\n")
        f.write(f"Recovered {len(msgs)} messages across {len(sessions_order)} sessions.\n")
        f.write("Order follows raw file layout; some rows may be truncated.\n\n")
        by = defaultdict(list)
        for m in msgs:
            by[m["session"]].append(m)
        for sess in sessions_order:
            f.write(f"## session {sess}\n\n")
            for m in by[sess]:
                c = m["content"]
                if len(c) > 3000:
                    c = c[:3000] + "\n... [truncated]\n"
                f.write(f"### {m['role']}\n\n{c}\n\n---\n")
    return md


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["scan", "strings", "carve", "messages"])
    ap.add_argument("dbfile")
    ap.add_argument("-o", "--out", default=None, help="output file (strings) or directory (carve/messages)")
    ap.add_argument("--min-len", type=int, default=25, help="minimum text run length")
    args = ap.parse_args()

    if not os.path.isfile(args.dbfile):
        print(f"error: no such file: {args.dbfile}", file=sys.stderr)
        sys.exit(2)
    if args.mode == "scan":
        p = SQLitePageParser(args.dbfile)
        hist = p.type_histogram()
        print(f"file: {args.dbfile}")
        print(f"page_size={p.page_size} pages={p.n_pages} usable={p.usable}")
        for k, v in hist.most_common():
            print(f"  {k:12} {v:6}")
        leaf = hist.get("0x0d", 0) + hist.get("0xd", 0)
        print()
        if leaf == 0:
            print("verdict: NO table-leaf pages survive -> use 'messages'/'strings' carving")
        else:
            print("verdict: table-leaf pages exist -> try sqlite3 .recover first")
        return

    data = open(args.dbfile, "rb").read()
    prof = os.path.splitext(os.path.basename(args.dbfile))[0]

    if args.mode == "strings":
        runs = carve_text_runs(data, args.min_len)
        out = args.out or (prof + "_strings.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n=====\n".join(runs))
        print(f"{len(runs)} text runs -> {out}")

    elif args.mode == "carve":
        rows = carve_records(args.dbfile)
        outdir = args.out or "."
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, f"{prof}_carved_records.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{len(rows)} carved records -> {out}")

    elif args.mode == "messages":
        msgs, others = extract_messages(data)
        outdir = args.out or "."
        os.makedirs(outdir, exist_ok=True)
        jl = os.path.join(outdir, f"{prof}_messages.jsonl")
        with open(jl, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        order = list(dict.fromkeys(m["session"] for m in msgs))
        write_markdown(outdir, prof, msgs, order)
        ot = os.path.join(outdir, f"{prof}_other_text.txt")
        with open(ot, "w", encoding="utf-8") as f:
            f.write("\n=====\n".join(others))
        roles = Counter(m["role"] for m in msgs)
        print(f"{len(msgs)} messages ({dict(roles)}), {len(order)} sessions, {len(others)} other runs -> {outdir}")
        sys.exit(0 if msgs else 1)


if __name__ == "__main__":
    main()
