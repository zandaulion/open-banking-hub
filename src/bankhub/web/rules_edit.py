"""Read, edit and delete the owner's own rules (the private categories.yaml).

Rules are addressed by section and position ("merchants:0", "category_rules:3") plus an
etag of the entry, so an edit never lands on a different rule if the file changed meanwhile.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import yaml

from ..rules import parse_rules  # noqa: F401  (re-exported for the server)

SECTIONS = ("merchants", "category_rules")
HEADER = """# Personal rules (checked before the packaged defaults). Never committed.
# merchants: shown by name. category_rules: categorized, payee stays unnamed.
"""


class RuleConflict(Exception):
    """The rule changed or moved since it was read."""


def load(path: Path) -> dict:
    data = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    for s in SECTIONS:
        data.setdefault(s, [])
    return data


def save(path: Path, data: dict) -> None:
    data = {k: v for k, v in data.items() if v}   # drop empty sections
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(HEADER + "\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120))
    tmp.chmod(0o600)
    tmp.replace(path)


def etag(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:16]


def _clean_patterns(patterns: list[str]) -> list[str]:
    out = []
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        if not p.startswith("re:"):
            p = p.lower()
        if len(p) < 3:
            raise ValueError(f"pattern {p!r} is too short (3 characters minimum)")
        if p not in out:
            out.append(p)
    if not out:
        raise ValueError("at least one pattern is needed")
    return out


def make_entry(*, patterns: list[str], category: str, subcategory: str | None, name: str | None,
               card_only: bool = False) -> tuple[str, dict]:
    """Returns (section, entry): named rules are merchants, the rest are unnamed category rules."""
    entry: dict = {}
    if name and name.strip():
        entry["name"] = name.strip()
        if card_only:
            entry["card_only"] = True
    entry["category"] = category
    if subcategory:
        entry["subcategory"] = subcategory
    entry["patterns"] = _clean_patterns(patterns)
    return ("merchants" if "name" in entry else "category_rules"), entry


def add(path: Path, **fields) -> dict:
    data = load(path)
    section, entry = make_entry(**fields)
    data[section].insert(0, entry)
    save(path, data)
    return {"id": f"{section}:0", "etag": etag(entry), **entry}


def _locate(data: dict, rule_id: str, tag: str) -> tuple[str, int]:
    section, _, index = rule_id.partition(":")
    if section not in SECTIONS or not index.isdigit():
        raise RuleConflict("unknown rule")
    i = int(index)
    if i >= len(data[section]) or etag(data[section][i]) != tag:
        raise RuleConflict("This rule changed since it was loaded. Reload and try again.")
    return section, i


def update(path: Path, rule_id: str, tag: str, **fields) -> dict:
    data = load(path)
    section, i = _locate(data, rule_id, tag)
    fields.setdefault("card_only", bool(data[section][i].get("card_only")))
    new_section, entry = make_entry(**fields)
    if new_section == section:
        data[section][i] = entry
        new_id = rule_id
    else:
        # Name switched on or off: the rule moves to the other section, at the top (it wins).
        del data[section][i]
        data[new_section].insert(0, entry)
        new_id = f"{new_section}:0"
    save(path, data)
    return {"id": new_id, "etag": etag(entry), **entry}


def delete(path: Path, rule_id: str, tag: str) -> None:
    data = load(path)
    section, i = _locate(data, rule_id, tag)
    del data[section][i]
    save(path, data)


def first_match(user_rules, row) -> str | None:
    """Which of the owner's rules decides this transaction, mirroring ledger.classify's order."""
    has_account = bool(row["counterparty_has_account"])
    text = (row["counterparty_name"] or "") if has_account else \
        " | ".join(x for x in (row["counterparty_name"], row["remittance"]) if x)
    if not text:
        return None
    for i, m in enumerate(user_rules.merchants):
        if has_account and m.card_only:
            continue
        if any(p.search(text) for p in m.patterns):
            return f"merchants:{i}"
    if row["amount_minor"] < 0:
        for i, c in enumerate(user_rules.categories):
            if any(p.search(text) for p in c.patterns):
                return f"category_rules:{i}"
    return None


def list_rules(conn: sqlite3.Connection, path: Path) -> list[dict]:
    data = load(path)
    compiled = parse_rules(data)
    stats: dict[str, list] = {}
    for row in conn.execute(
            """SELECT t.counterparty_name, t.remittance, t.counterparty_has_account, t.amount_minor, c.amount_ron
               FROM transactions_raw t JOIN transactions_clean c ON c.id = t.id
               WHERE c.is_internal_transfer = 0 AND t.id NOT IN (SELECT raw_id FROM transaction_overrides
                                                               WHERE category IS NOT NULL)"""):
        rid = first_match(compiled, row)
        if rid:
            s = stats.setdefault(rid, [0, 0.0])
            s[0] += 1
            s[1] += -(row["amount_ron"] or 0)
    out = []
    for section in SECTIONS:
        for i, entry in enumerate(data[section]):
            rid = f"{section}:{i}"
            n, total = stats.get(rid, [0, 0.0])
            out.append({"id": rid, "etag": etag(entry), "named": section == "merchants",
                        "name": entry.get("name"), "category": entry["category"],
                        "subcategory": entry.get("subcategory"), "patterns": entry["patterns"],
                        "card_only": bool(entry.get("card_only")), "matches": n, "outflow_ron": round(total, 2)})
    return sorted(out, key=lambda r: (r["category"], -(r["outflow_ron"])))


def rule_name(pattern: str) -> str:
    """A readable merchant name from a match text: 'google*cloud' -> 'Google Cloud',
    'google *google play' -> 'Google Play'."""
    words = []
    for w in pattern.replace("*", " ").split():
        if not words or words[-1].lower() != w.lower():
            words.append(w)
    return " ".join(w[:1].upper() + w[1:] for w in words)


def preview(conn: sqlite3.Connection, pattern: str) -> dict:
    """What a rule with this match text would catch, with the same matching as real rules:
    card payments match name and description, bank transfers the name only."""
    from ..rules import _compile
    pat = _compile(pattern.strip() if pattern.strip().startswith("re:") else pattern.strip().lower())
    n, out, examples = 0, 0.0, []
    for name, desc, has_acct, ron in conn.execute(
            """SELECT t.counterparty_name, t.remittance, t.counterparty_has_account, c.amount_ron
               FROM transactions_raw t JOIN transactions_clean c ON c.id = t.id
               WHERE c.is_internal_transfer = 0"""):
        text = (name or "") if has_acct else " | ".join(x for x in (name, desc) if x)
        if text and pat.search(text):
            n += 1
            out += -(ron or 0) if (ron or 0) < 0 else 0
            label = name or desc
            if label and label not in examples and len(examples) < 4:
                examples.append(label)
    return {"count": n, "outflow_ron": round(out, 2), "examples": examples}
