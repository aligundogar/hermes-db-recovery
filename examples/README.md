# Examples

Sanitized example of `scan` output on a file whose table-leaf pages were all
destroyed (profile names and identifiers redacted):

```
$ python3 hermes_db_carver.py scan profile_a.db.corrupt
file: profile_a.db.corrupt
page_size=4096 pages=14817 usable=4096
  other/text  14017
  0x2           382
  0xa           165
  0x5            79
  zero          174

verdict: NO table-leaf pages survive -> use 'messages'/'strings' carving
```

Then:

```
$ python3 hermes_db_carver.py messages profile_a.db.corrupt -o recovered/
2775 messages ({'tool': 1942, 'assistant': 796, 'user': 37}), 20 sessions, 23162 other runs -> recovered/
```

The JSONL rows look like:

```json
{"session": "YYYYMMDD_HHMMSS_xxxxxxx", "role": "user", "content": "..."}
```

(The session id pattern is an artifact of the agent platform this was built
for; adapt `MSG_RX` in the script for other row shapes.)
