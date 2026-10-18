"""Reading database identifiers as words — for any schema's conventions.

Names in the wild are snake_case (customer_id), PascalCase (CustomerID),
camelCase (orderQty), abbreviated (cust_no, prod_cd, qty), star-schema
(dim_customer.customer_sk), or prefixed (fk_account). The metadata graph's
relationship inference, its cross-source entities and question matching all
need the same answers from them:

  words("SalesOrderDetailID") → ("sales", "order", "detail", "id")
  key_stem("fk_account")      → ("account",)
  key_stem("acct_id", vocab)  → ("account",)       (abbreviation, see below)
  table_words("dim_customer") → ("customer",)
  names_entity("SalesOrderHeader", ("sales", "order")) → True

Design:
- A LEAF. Imports nothing from the app; pure functions, no I/O.
- NO SCHEMA IS SPECIAL. Nothing here knows a table name. Abbreviations come
  from a small list of standard data-modelling short forms, or from the
  schema itself: a short token stands for a word the same schema spells out
  when its letters run through that word in order (acct → account) and only
  one such word exists — never a guess between two.
- Words are singular, so customers / customer / Customer all read alike.
"""
import re

# Standard data-modelling short forms (not tied to any schema).
ABBREVIATIONS = {
    "acct": "account", "addr": "address", "amt": "amount", "attr": "attribute",
    "avg": "average", "bal": "balance", "cat": "category", "cnt": "count",
    "cust": "customer", "dept": "department", "desc": "description", "dt": "date",
    "emp": "employee", "ln": "line", "loc": "location", "mgr": "manager",
    "msg": "message", "nm": "name", "ord": "order", "org": "organization",
    "pct": "percent", "pmt": "payment", "prd": "product", "prod": "product",
    "qty": "quantity", "seg": "segment", "tx": "transaction", "txn": "transaction",
    "ts": "timestamp", "usr": "user", "vol": "volume", "whse": "warehouse",
    "wh": "warehouse",
}
# Deliberately absent: short forms that mean two things (inv: invoice or
# inventory; sub, val, stat). The schema decides those (expand()).
# Tokens that mark a key column wherever they sit in its name.
KEY_MARKERS = {"id", "key", "sk", "pk", "fk", "no", "nbr", "num", "number", "code", "cd",
               "ref", "uuid", "guid", "bk", "nk"}
# Table-name words that classify a table rather than name its subject.
TABLE_NOISE = {"dim", "fact", "fct", "tbl", "tb", "vw", "view", "stg", "staging", "raw",
               "src", "lkp", "lookup", "d", "f", "t", "v", "master", "mstr", "main"}
# A parent ("header") table may carry these after its subject; a CHILD carries
# detail/line/item words, so SalesOrderDetail is not the home of a sales order.
HEADER_WORDS = {"header", "hdr", "master", "main"}

_SPLIT = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _singular(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def raw_words(identifier):
    """Lower-case pieces of an identifier: snake, Pascal, camel and digits."""
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", str(identifier or "")):
        out += [m.group(0).lower() for m in _SPLIT.finditer(part)]
    return [w for w in out if w]


def _is_subsequence(short, word):
    it = iter(word)
    return all(ch in it for ch in short)


def expand(token, vocab=None):
    """The full word a token stands for: the standard short form, else the one
    word of `vocab` it abbreviates (same first letter, letters in order, at
    least three of them, and unambiguous), else itself."""
    if token in ABBREVIATIONS:
        return ABBREVIATIONS[token]
    if vocab and len(token) >= 3 and token not in vocab:
        matches = {w for w in vocab
                   if len(w) > len(token) and w[0] == token[0] and _is_subsequence(token, w)}
        if len(matches) == 1:
            return matches.pop()
    return token


def words(identifier, vocab=None):
    """Singular, expanded words of an identifier."""
    return tuple(_singular(expand(w, vocab)) for w in raw_words(identifier))


def vocabulary(identifiers):
    """The full words a schema spells out (≥4 letters, not short forms) — what
    its abbreviations may stand for."""
    vocab = set()
    for ident in identifiers:
        for w in raw_words(ident):
            if len(w) >= 4 and w not in ABBREVIATIONS and w not in KEY_MARKERS:
                vocab.add(_singular(w))
    return vocab


def key_stem(column, vocab=None):
    """The subject a key column refers to, as words — ("customer",) for
    customer_id, CustomerID, customer_sk, fk_customer, cust_no — or None for a
    column that does not look like a key. A bare `id` has no subject."""
    parts = [_singular(expand(w, vocab)) for w in raw_words(column)]
    if len(parts) < 2:
        return None
    if parts[-1] in KEY_MARKERS:
        stem = parts[:-1]
    elif parts[0] in KEY_MARKERS:
        stem = parts[1:]
    else:
        return None
    stem = [w for w in stem if w not in KEY_MARKERS]
    return tuple(stem) or None


def table_words(table, vocab=None):
    """A table's subject words: its last dotted part, without classifier words
    (dim_, fact_, tbl, vw_ …)."""
    bare = str(table).split(".")[-1]
    out = [w for w in words(bare, vocab) if w not in TABLE_NOISE]
    return tuple(out) or tuple(words(bare, vocab))


def names_entity(table, stem, vocab=None):
    """Is `table` the home of the subject `stem`? Its subject words end with
    the stem — a domain prefix is fine (SalesTerritory, retail_customers) —
    optionally followed by one header word (SalesOrderHeader). A detail /
    line / item table is never the home of its parent's key."""
    if not stem:
        return False
    tw = table_words(table, vocab)
    n = len(stem)
    if tw[-n:] == tuple(stem):
        return True
    return len(tw) > n and tw[-1] in HEADER_WORDS and tw[-n - 1:-1] == tuple(stem)


def entity_name(stem):
    return " ".join(stem).capitalize()
