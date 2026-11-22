"""Standard statement files that many banks export: ISO 20022 CAMT.053 (XML, the European bank statement) and
OFX/QFX (SGML in version 1, XML in version 2). No bank-specific code: the payee, its IBAN and the description
come from the file's own fields.

Both are read from text. XML with a DOCTYPE is refused (no entity tricks from an uploaded file).
"""

import re
import xml.etree.ElementTree as ET
from datetime import datetime

from .base import AccountLine, AccountStatement, Reader, StatementError, register, warn_unless


def _cents(text: str | None) -> int | None:
    s = (text or "").strip().replace(" ", "")
    if not re.fullmatch(r"[+-]?\d+([.,]\d{1,4})?", s):
        return None
    sign = -1 if s.startswith("-") else 1
    whole, _, frac = s.lstrip("+-").replace(",", ".").partition(".")
    return sign * (int(whole) * 100 + int((frac + "00")[:2]))


# ---------------------------------------------------------------- CAMT.053

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, path: str):
    """`a/b/c` by local names, whatever the namespace (camt.053.001.02 to .08 differ only in it)."""
    for part in path.split("/"):
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == part), None)
    return el


def _text(el, path: str) -> str | None:
    found = _find(el, path)
    return found.text.strip() if found is not None and found.text else None


def _all(el, name: str):
    return [c for c in el.iter() if _local(c.tag) == name]


def _party(details, role: str) -> tuple[str | None, str | None]:
    parties = _find(details, "RltdPties") if details is not None else None
    if parties is None:
        return None, None
    name = _text(parties, f"{role}/Nm") or _text(parties, f"{role}/Pty/Nm")
    iban = _text(parties, f"{role}Acct/Id/IBAN")
    return name, iban


def _iso_date(el, path: str) -> str | None:
    value = _text(el, f"{path}/Dt") or (_text(el, f"{path}/DtTm") or "")[:10] or None
    return value


def parse_camt(text: str) -> AccountStatement:
    if "<!DOCTYPE" in text[:2000].upper():
        raise StatementError("That file declares a document type, which is not accepted.")
    try:
        root = ET.fromstring(text.lstrip("﻿").strip().encode("utf-8"))
    except ET.ParseError:
        raise StatementError("That CAMT file is not valid XML.")
    stmts = _all(root, "Stmt")
    if not stmts:
        raise StatementError("The file has no statement in it.")
    ibans = {_text(s, "Acct/Id/IBAN") for s in stmts}
    if len(ibans) != 1 or None in ibans:
        raise StatementError("The file holds several accounts, or none with an IBAN: export one account at a time.")
    first = stmts[0]
    currency = _text(first, "Acct/Ccy") or (_find(first, "Ntry/Amt").get("Ccy") if _find(first, "Ntry/Amt") is not None else None)
    st = AccountStatement(bank="CAMT", iban=ibans.pop(), currency=currency or "EUR", period_start="", period_end="")
    days = []
    for s in stmts:
        booked = 0
        for n in (c for c in s if _local(c.tag) == "Ntry"):
            status = _text(n, "Sts/Cd") or _text(n, "Sts") or "BOOK"
            amount = _cents(_text(n, "Amt"))
            day = _iso_date(n, "BookgDt") or _iso_date(n, "ValDt")
            if status != "BOOK" or amount is None or not day:
                continue
            debit = _text(n, "CdtDbtInd") == "DBIT"
            signed = -amount if debit else amount
            details = _find(n, "NtryDtls/TxDtls")
            name, iban = _party(details, "Cdtr" if debit else "Dbtr")
            words = [w for w in (*(e.text or "" for e in (_all(details, "Ustrd") if details is not None else [])),
                                 _text(details, "AddtlTxInf") if details is not None else None,
                                 _text(n, "AddtlNtryInf")) if w]
            st.lines.append(AccountLine(date=day, value_date=_iso_date(n, "ValDt") or day, amount_minor=signed,
                                        text=" · ".join(dict.fromkeys(w.strip() for w in words))[:300] or (name or ""),
                                        counterparty=name, iban=iban))
            booked += signed
            days.append(day)
        # Opening plus what was booked must give the closing balance, statement by statement.
        bal = {}
        for b in (c for c in s if _local(c.tag) == "Bal"):
            code, value = _text(b, "Tp/CdOrPrtry/Cd"), _cents(_text(b, "Amt"))
            if value is not None and code:
                bal[code] = -value if _text(b, "CdtDbtInd") == "DBIT" else value
        opening, closing = bal.get("OPBD", bal.get("PRCD")), bal.get("CLBD")
        warn_unless(st, opening is None or closing is None or opening + booked == closing,
                    "The payments read from this statement do not take its opening balance to its closing balance.")
        if opening is not None and st.start_minor is None:
            st.start_minor = opening
        if closing is not None:
            st.end_minor = closing
    frm, to = _find(first, "FrToDt/FrDtTm"), _find(stmts[-1], "FrToDt/ToDtTm")
    st.period_start = (frm.text[:10] if frm is not None and frm.text else min(days, default=""))
    st.period_end = (to.text[:10] if to is not None and to.text else max(days, default=""))
    if not st.period_start or not st.period_end:
        raise StatementError("The statement has no dates.")
    warn_unless(st, bool(st.lines), "No booked transactions were found in this statement.")
    return st


# ---------------------------------------------------------------- OFX / QFX

def _ofx_field(block: str, tag: str) -> str | None:
    """A leaf value: `<TAG>value` (OFX 1, no closing tag) or `<TAG>value</TAG>` (OFX 2)."""
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m and m.group(1).strip() else None


def _ofx_date(value: str | None) -> str | None:
    if not value or not re.match(r"\d{8}", value):
        return None
    return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()


def parse_ofx(text: str) -> AccountStatement:
    if "<!DOCTYPE" in text[:2000].upper():
        raise StatementError("That file declares a document type, which is not accepted.")
    body = text[text.upper().find("<OFX>"):] if "<OFX>" in text.upper() else ""
    if not body:
        raise StatementError("That OFX file has no <OFX> section.")
    if len(re.findall(r"<(?:STMTRS|CCSTMTRS)>", body, re.I)) > 1:
        raise StatementError("The file holds several accounts: export one account at a time.")
    account = _ofx_field(body, "ACCTID")
    currency = _ofx_field(body, "CURDEF") or "EUR"
    st = AccountStatement(bank="OFX", currency=currency.upper(), period_start="", period_end="",
                          iban=account.replace(" ", "").upper() if account else None)
    days = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", body, re.I | re.S):
        amount = _cents(_ofx_field(block, "TRNAMT"))
        day = _ofx_date(_ofx_field(block, "DTPOSTED"))
        if amount is None or not day or amount == 0:
            continue
        name = _ofx_field(block, "NAME")
        memo = _ofx_field(block, "MEMO")
        st.lines.append(AccountLine(date=day, value_date=_ofx_date(_ofx_field(block, "DTUSER")) or day, amount_minor=amount,
                                    text=" · ".join(w for w in (memo, _ofx_field(block, "TRNTYPE")) if w)[:300] or (name or ""),
                                    counterparty=name))
        days.append(day)
    start, end = _ofx_date(_ofx_field(body, "DTSTART")), _ofx_date(_ofx_field(body, "DTEND"))
    st.period_start, st.period_end = start or min(days, default=""), end or max(days, default="")
    if not st.period_start:
        raise StatementError("The statement has no dates.")
    closing = _cents(_ofx_field(re.search(r"<LEDGERBAL>(.*?)(</LEDGERBAL>|<AVAILBAL>|$)", body, re.I | re.S).group(1), "BALAMT")) \
        if re.search(r"<LEDGERBAL>", body, re.I) else None
    st.end_minor = closing
    # OFX carries a closing balance but no opening one or totals, so there is nothing to add the lines up to.
    warn_unless(st, bool(st.lines), "No transactions were found in this statement.")
    return st


register(Reader(id="camt053", bank="any", kind="account", label="CAMT.053 bank statement (XML)",
                detect=lambda text: "BkToCstmrStmt" in text[:5000] and "<!DOCTYPE" not in text[:2000].upper(),
                parse=parse_camt))
register(Reader(id="ofx", bank="any", kind="account", label="OFX or QFX statement",
                detect=lambda text: ("OFXHEADER" in text[:500] or "<OFX>" in text[:3000].upper()) and "BkToCstmrStmt" not in text[:5000],
                parse=parse_ofx))
