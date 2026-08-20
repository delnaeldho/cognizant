"""Rule-based fast path for common questions.

If a question cleanly matches one of the patterns below, we build the SQL
directly from the live schema (no LLM call, no cost, deterministic). We only
ever return a match when every table/column referenced can be resolved
unambiguously against the real schema; otherwise we return None so the
caller falls back to the LLM. This avoids the failure mode of confidently
producing wrong SQL for a question that merely resembles a template.
"""

import re
from typing import Optional

# Each schema table maps to a list of {"name": ..., "type": ...} dicts.
SchemaTables = dict[str, list[dict[str, str]]]


def _singularize(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _pluralize(word: str) -> str:
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy", "uy")):
        return word[:-1] + "ies"
    if word.endswith("s"):
        return word
    return word + "s"


def resolve_table(word: str, tables: SchemaTables) -> Optional[str]:
    word = word.lower().strip()
    candidates = {word, _singularize(word), _pluralize(word)}
    for table_name in tables:
        if table_name.lower() in candidates:
            return table_name
    return None


def resolve_column(word: str, columns: list[dict[str, str]]) -> Optional[str]:
    word = word.lower().strip().replace(" ", "_")
    candidates = {word, _singularize(word), _pluralize(word)}
    for col in columns:
        if col["name"].lower() in candidates:
            return col["name"]
    return None


def _find_numeric_column(columns: list[dict[str, str]]) -> Optional[str]:
    numeric_types = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")
    for col in columns:
        if any(t in col["type"].upper() for t in numeric_types) and col["name"].lower() != "id":
            return col["name"]
    return None


# Trailing "sort/order by" clauses that can be stripped off a question, in the
# form (base question, column word, direction word-or-None). Tried in order;
# the first one that matches wins. All patterns anchor to the end of the
# question so they only ever consume a genuine trailing clause.
_ORDER_SUFFIX_PATTERNS = [
    re.compile(r"^(?P<base>.*?)\s+in (?P<dir>ascending|descending|asc|desc) order of (?P<col>\w+)$"),
    re.compile(r"^(?P<base>.*?)\s+(?:ordered|sorted) by (?P<col>\w+) in (?P<dir>ascending|descending|asc|desc) order$"),
    re.compile(r"^(?P<base>.*?)\s+by (?P<col>\w+) in (?P<dir>ascending|descending|asc|desc) order$"),
    re.compile(r"^(?P<base>.*?)\s+(?:ordered|sorted) by (?P<col>\w+)(?: in)? (?P<dir>ascending|descending|asc|desc)$"),
    re.compile(r"^(?P<base>.*?)\s+by (?P<col>\w+) (?P<dir>ascending|descending|asc|desc)$"),
    re.compile(r"^(?P<base>.*?)\s+(?:ordered|sorted) by (?P<col>\w+)$"),
]


def _normalize_direction(word: Optional[str]) -> str:
    return "DESC" if word and word.lower().startswith("desc") else "ASC"


def _extract_order_suffix(q: str) -> tuple[str, Optional[str], str]:
    """Splits a trailing sort clause off a question, if present.

    Returns (base_question, column_word, direction). If no sort clause is
    detected, column_word is None and base_question is just `q`.
    """
    for pattern in _ORDER_SUFFIX_PATTERNS:
        m = pattern.match(q)
        if m:
            gd = m.groupdict()
            return gd["base"].strip(), gd["col"], _normalize_direction(gd.get("dir"))
    return q, None, "ASC"


def try_template_match(question: str, tables: SchemaTables) -> Optional[str]:
    q = question.strip().lower().rstrip("?.! ")
    base_q, order_col_word, order_dir = _extract_order_suffix(q)

    # "list/show/get all <table>", optionally followed by a sort clause, e.g.
    # "list all customers in ascending order of name" or
    # "list all customers sorted by name descending".
    m = re.fullmatch(r"(?:list|show|get)(?: me)? all(?: the)? (\w+)", base_q)
    if m:
        table = resolve_table(m.group(1), tables)
        if table:
            if order_col_word:
                order_col = resolve_column(order_col_word, tables[table])
                # Only return SQL once the sort column itself resolves
                # unambiguously; otherwise defer to the LLM rather than
                # silently dropping the requested ordering.
                if order_col:
                    return f"SELECT * FROM {table} ORDER BY {order_col} {order_dir} LIMIT 100"
                return None
            return f"SELECT * FROM {table} LIMIT 100"

    # "how many <table> are there" / "count of <table>" / "how many <table> do we have"
    m = re.fullmatch(r"how many(?: the)? (\w+)(?: are there| do we have)?", q) or re.fullmatch(
        r"count(?: of)?(?: the)? (\w+)", q
    )
    if m:
        table = resolve_table(m.group(1), tables)
        if table:
            return f"SELECT COUNT(*) AS count FROM {table}"

    # "top N <table> by <column>"
    m = re.fullmatch(r"top (\d+) (\w+) by (\w+)", q)
    if m:
        n, table_word, column_word = m.groups()
        table = resolve_table(table_word, tables)
        if table:
            column = resolve_column(column_word, tables[table])
            if column:
                return f"SELECT * FROM {table} ORDER BY {column} DESC LIMIT {int(n)}"

    # "total/sum of <column> by <group_column>" (single-table aggregation)
    m = re.fullmatch(r"(?:total|sum of) (\w+) (?:by|per) (\w+)", q)
    if m:
        metric_word, group_word = m.groups()
        for table, columns in tables.items():
            metric_col = resolve_column(metric_word, columns)
            group_col = resolve_column(group_word, columns)
            if metric_col and group_col:
                return (
                    f"SELECT {group_col}, SUM({metric_col}) AS total_{metric_col} "
                    f"FROM {table} GROUP BY {group_col} ORDER BY total_{metric_col} DESC"
                )

    # "average <column> of/in <table>"
    m = re.fullmatch(r"average (\w+) (?:of|in|for)(?: the)? (\w+)", q)
    if m:
        column_word, table_word = m.groups()
        table = resolve_table(table_word, tables)
        if table:
            column = resolve_column(column_word, tables[table])
            if column:
                return f"SELECT AVG({column}) AS average_{column} FROM {table}"

    return None
