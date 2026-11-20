"""Merchant, own-account and label rules loaded from YAML."""

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml


def _compile(pattern: str) -> re.Pattern:
    if pattern.startswith("re:"):
        return re.compile(pattern[3:], re.IGNORECASE)
    # Plain patterns must start at a word boundary: "hbo" must not match inside another word.
    return re.compile(r"(?<![^\W_])" + re.escape(pattern), re.IGNORECASE)


@dataclass
class Merchant:
    name: str
    category: str
    subcategory: str | None
    patterns: list[re.Pattern]
    card_only: bool = False   # name is also a first name: never match bank transfers


@dataclass
class CategoryRule:
    category: str
    subcategory: str | None
    patterns: list[re.Pattern]


@dataclass
class BankCodeRule:
    """Classifies by the bank's own transaction type (e.g. "Credit Repayment"), not by payee."""
    codes: set[str]
    patterns: list[re.Pattern]  # "re:" entries, for banks whose type text varies
    category: str | None
    subcategory: str | None
    direction: str | None      # "debit" | "credit" | None for both
    internal: bool             # money between my own accounts or deposits


@dataclass
class Rules:
    merchants: list[Merchant] = field(default_factory=list)
    categories: list[CategoryRule] = field(default_factory=list)
    bank_codes: list[BankCodeRule] = field(default_factory=list)
    own_account: dict[str, list[re.Pattern]] = field(default_factory=dict)
    labels: dict[str, list[re.Pattern]] = field(default_factory=dict)

    def match_merchant(self, name: str | None, description: str | None,
                       has_account: bool = False) -> Merchant | None:
        """Bank transfers (has_account) match on the counterparty name only: their free-text
        description is a personal message that can contain anyone's name."""
        if has_account:
            text = name or ""
        else:
            text = " | ".join(x for x in (name, description) if x)
        if not text:
            return None
        for m in self.merchants:
            if has_account and m.card_only:
                continue
            if any(p.search(text) for p in m.patterns):
                return m
        return None

    def match_category(self, name: str | None, description: str | None,
                       has_account: bool = False) -> CategoryRule | None:
        """Sets a category without ever naming the payee (small local businesses, people's practices)."""
        text = (name or "") if has_account else " | ".join(x for x in (name, description) if x)
        if not text:
            return None
        for c in self.categories:
            if any(p.search(text) for p in c.patterns):
                return c
        return None

    def match_bank_code(self, code: str | None, debit: bool) -> BankCodeRule | None:
        if not code:
            return None
        code = code.strip().lower()
        for r in self.bank_codes:
            if (code in r.codes or any(p.search(code) for p in r.patterns)) \
                    and r.direction in (None, "debit" if debit else "credit"):
                return r
        return None

    def is_own_account_move(self, institution: str, description: str) -> bool:
        pats = self.own_account.get(institution, [])
        return any(p.search(description) for p in pats)

    def label(self, text: str) -> str | None:
        for name, pats in self.labels.items():
            if any(p.search(text) for p in pats):
                return name
        return None


def parse_rules(data: dict) -> Rules:
    return Rules(
        merchants=[
            Merchant(m["name"], m["category"], m.get("subcategory"), [_compile(p) for p in m["patterns"]],
                     bool(m.get("card_only")))
            for m in data.get("merchants") or []
        ],
        categories=[
            CategoryRule(c["category"], c.get("subcategory"), [_compile(p) for p in c["patterns"]])
            for c in data.get("category_rules") or []
        ],
        bank_codes=[
            BankCodeRule({c.strip().lower() for c in r["codes"] if not c.startswith("re:")},
                         [_compile(c) for c in r["codes"] if c.startswith("re:")],
                         r.get("category"), r.get("subcategory"), r.get("direction"), bool(r.get("internal")))
            for r in data.get("bank_code_rules") or []
        ],
        own_account={k: [_compile(p) for p in v] for k, v in (data.get("own_account_patterns") or {}).items()},
        labels={k: [_compile(p) for p in v] for k, v in (data.get("labels") or {}).items()},
    )


def merge(defaults: dict, user: dict) -> dict:
    """User rules come first (they win), then the packaged defaults."""
    out = {"merchants": (user.get("merchants") or []) + (defaults.get("merchants") or []),
           "category_rules": (user.get("category_rules") or []) + (defaults.get("category_rules") or []),
           "bank_code_rules": (user.get("bank_code_rules") or []) + (defaults.get("bank_code_rules") or []),
           "own_account_patterns": dict(defaults.get("own_account_patterns") or {}),
           "labels": dict(defaults.get("labels") or {})}
    for section in ("own_account_patterns", "labels"):
        for key, pats in (user.get(section) or {}).items():
            out[section][key] = list(pats) + list(out[section].get(key) or [])
    return out


def load_rules(user_path: Path | None = None) -> Rules:
    """Packaged defaults, extended by the user's rules file in the data directory if present."""
    data = yaml.safe_load((resources.files("bankhub") / "data" / "categories.yaml").read_text())
    if user_path and user_path.exists():
        data = merge(data, yaml.safe_load(user_path.read_text()) or {})
    return parse_rules(data)
