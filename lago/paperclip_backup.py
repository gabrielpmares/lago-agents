"""Reading a Paperclip database backup: every table's rows, without Paperclip or Postgres.

Paperclip backs its database up on its own (hourly by default, in
`~/.paperclip/instances/<name>/data/backups/*.sql.gz`) and with `paperclipai db:backup`. A
backup is plain SQL in one of two shapes: Paperclip's own writer emits one `INSERT INTO
"public"."table" (cols) VALUES (...);` per row with `$paperclip$…$paperclip$` strings, and
pg_dump (used when it's installed) emits `COPY "public"."table" (cols) FROM stdin;` blocks of
tab-separated rows. read() takes either and returns {table: [row dict]}, JSON columns left as
text (json_value() decodes one).
"""

import gzip
import json
import re
from pathlib import Path

INSERT = re.compile(r'INSERT INTO "?(\w+)"?\."?(\w+)"? \(([^)]*)\)(?: OVERRIDING SYSTEM VALUE)? VALUES \(')
COPY = re.compile(r'^COPY "?(\w+)"?\."?(\w+)"? \(([^)]*)\) FROM stdin;$')
MAX_BYTES = 2 * 1024 * 1024 * 1024  # unpacked; a backup bigger than this isn't one we read


class BackupError(Exception):
    pass


def _open(path):
    path = Path(path)
    with path.open("rb") as file:
        head = file.read(2)
    return gzip.open(path, "rt", encoding="utf-8", errors="replace") if head == b"\x1f\x8b" else \
        path.open("r", encoding="utf-8", errors="replace")


def _columns(text):
    return [c.strip().strip('"').replace('""', '"') for c in text.split(",")]


def _values(text, i):
    """The values of one INSERT from position i (just past "VALUES ("): (list, end position)."""
    out = []
    n = len(text)
    while i < n:
        while i < n and text[i] in " \n\t,":
            i += 1
        if text[i] == ")":
            return out, i + 1
        if text[i] == "$":
            tag_end = text.index("$", i + 1) + 1
            tag = text[i:tag_end]
            close = text.index(tag, tag_end)
            out.append(text[tag_end:close])
            i = close + len(tag)
        elif text[i] == "'":
            j, parts = i + 1, []
            while True:
                k = text.index("'", j)
                parts.append(text[j:k])
                if k + 1 < n and text[k + 1] == "'":
                    parts.append("'")
                    j = k + 2
                    continue
                i = k + 1
                break
            out.append("".join(parts))
        else:
            j = i
            depth = 0
            while j < n and (depth or text[j] not in ",)"):
                depth += text[j] == "("
                depth -= text[j] == ")" and depth > 0
                j += 1
            word = text[i:j].strip()
            cast = word.split("::")[0]
            out.append(None if cast.upper() == "NULL" else True if cast == "true" else False if cast == "false"
                       else int(cast) if re.fullmatch(r"-?\d+", cast) else float(cast) if re.fullmatch(r"-?\d+\.\d+(e[-+]?\d+)?", cast) else cast)
            i = j
    raise BackupError("A row in the backup ends early: the file may be cut off.")


COPY_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def _copy_field(field):
    if field == "\\N":
        return None
    return re.sub(r"\\(.)", lambda m: COPY_ESCAPES.get(m.group(1), m.group(1)), field)


def read(path, tables=None):
    """{table: [row]} for every table in the backup (or only `tables`), rows as dicts."""
    path = Path(path)
    if not path.is_file():
        raise BackupError(f"There's no backup at {path}.")
    wanted = set(tables) if tables else None
    rows = {}
    statement = []
    copying = None
    total = 0
    with _open(path) as file:
        for line in file:
            total += len(line)
            if total > MAX_BYTES:
                raise BackupError("The backup is too big to read.")
            if copying:
                if line.rstrip("\n") == "\\.":
                    copying = None
                    continue
                table, columns = copying
                if table is not None:
                    fields = line.rstrip("\n").split("\t")
                    rows[table].append(dict(zip(columns, (_copy_field(f) for f in fields))))
                continue
            match = COPY.match(line.rstrip("\n"))
            if match:
                table = match.group(2) if match.group(1) == "public" else f"{match.group(1)}.{match.group(2)}"
                keep = wanted is None or table in wanted
                if keep:
                    rows.setdefault(table, [])
                copying = (table if keep else None, _columns(match.group(3)))
                continue
            if statement or line.startswith("INSERT INTO"):
                statement.append(line)
                if not line.rstrip().endswith(";"):
                    continue
                text = "".join(statement)
                statement = []
                match = INSERT.match(text)
                if not match:
                    continue
                table = match.group(2) if match.group(1) == "public" else f"{match.group(1)}.{match.group(2)}"
                if wanted is not None and table not in wanted:
                    continue
                try:
                    values, _ = _values(text, match.end())
                except (ValueError, IndexError):
                    # A row whose text ends with ";" mid-string: keep reading lines into it.
                    statement = [text]
                    continue
                rows.setdefault(table, []).append(dict(zip(_columns(match.group(3)), values)))
    if not rows:
        raise BackupError("That file has no Paperclip data in it.")
    return rows


def json_value(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def array_value(value):
    """A Postgres array literal ({a,"b c"}) as a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not (text.startswith("{") and text.endswith("}")):
        return [text]
    inner, out, current, quoted, escape = text[1:-1], [], "", False, False
    for char in inner:
        if escape:
            current += char
            escape = False
        elif char == "\\":
            escape = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            out.append(None if current == "NULL" else current)
            current = ""
        else:
            current += char
    if inner:
        out.append(None if current == "NULL" else current)
    return out
