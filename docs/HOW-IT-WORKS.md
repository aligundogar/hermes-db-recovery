# How it works - SQLite internals and the carving approach

## What a healthy file looks like

A SQLite file is a sequence of fixed-size pages (header byte 16-17). Every
B-tree page starts with a 1-byte type:

| type  | meaning |
|-------|---------|
| 0x02  | interior index page |
| 0x05  | interior table page |
| 0x0a  | leaf index page |
| 0x0d  | leaf table page (your rows live here) |

A leaf-table page header is 8 bytes: type, first-freeblock, cell-count,
content-start, fragment-count. Each cell is:

```
payload_len (varint) | rowid (varint) | record
record := header_size (varint) | serial types (varints) | values
```

Serial types: 0=NULL, 1..6 = big-endian ints, 7 = float64, 8/9 = constants
0/1, even >= 12 = blob of (n-12)/2 bytes, odd >= 13 = text of (n-13)/2 bytes.
Rows bigger than the page threshold continue on overflow pages
(4-byte next-pointer + raw payload).

## What corruption looks like

In the incident this tool was built for, every leaf-table page lost its
framing while index pages and raw text survived:

```
scan corrupt.db
  0x2        382     interior index
  0xa        165     leaf index
  0x5         79     interior table
  other/text  14017  <- unframed content, no 0x0d at all
  zero         174
```

With zero leaf-table pages there is nothing for SQLite (or `.recover`) to
walk. But the *values* - text - are still in the file.

## Three carving strategies, in order of reliability

1. **Page parsing** (`scan`, `parse_leaf_table_cells`) - when 0x0d pages
   survive, parse the pointer array + records directly. Exact, complete.

2. **Record carving** (`carve`) - scan every file offset for a byte sequence
   that parses as `payload_len | rowid | header | serial types` where the
   declared sizes add up *exactly* and every text column decodes as printable
   UTF-8. Exact-fit byte accounting kills false positives. Cost: O(file size),
   pure Python, ~1 min per 60 MB.

3. **Text-run carving** (`strings`, `messages`) - split the file on control
   bytes, decode UTF-8, keep high-printability runs. Chat rows in agent
   databases happen to serialize as `<session_id><role><content>` with all
   three adjacent, so a single regex reconstructs structured messages from
   unframed bytes. This recovered 3,887 messages from databases where
   strategies 1 and 2 returned nothing.

## Validation heuristics that keep carving honest

- exact byte accounting: serial-type sizes must sum to the declared payload
- printable ratio >= 0.9 per text column (UTF-8 strict decode)
- rowid in a sane range, serial types in a sane range
- duplicate suppression on the first 60/200 bytes

## Practical notes

- `scan` tells you which strategy applies before you spend time.
- Do carving on a **copy**, never the original.
- Expect fragmentation: an unframed file can shift or truncate rows. Gaps are
  normal; what comes out is what physically survived.
- FTS index pages (0x02/0x0a) hold vocabulary, not rows - the `other_text`
  output will be full of index terms. That is expected noise.
