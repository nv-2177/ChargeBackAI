import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def build_file_manifest(case_dir: Path, exclude=("metadata.json",)) -> list[dict]:
    items = []
    for p in sorted(case_dir.rglob("*")):
        if p.is_file() and p.name not in exclude:
            st = p.stat()
            items.append({
                "path": p.relative_to(case_dir).as_posix(),
                "size_bytes": st.st_size,
                "sha256": sha256_file(p),
                "modified_at_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            })
    return items


def _tokens(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()


def match_product(expected: str, candidates: list[str], threshold: float = 0.55) -> dict:
    exp = _tokens(expected)
    best_score, best_title = 0.0, None
    for title in candidates:
        cand = _tokens(title)
        if not exp or not cand:
            continue
        coverage = len(set(exp) & set(cand)) / len(set(exp))
        ratio = SequenceMatcher(None, " ".join(exp), " ".join(cand)).ratio()
        score = max(coverage, ratio)
        if score > best_score:
            best_score, best_title = score, title
    return {
        "expected": expected,
        "matched": best_score >= threshold,
        "score": round(best_score, 3),
        "threshold": threshold,
        "matched_title": best_title,
        "candidates": candidates,
    }


_AMT = r"(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d{1,2})?)"
# Priority order: the FIRST label that yields an amount wins (not the first one that appears on the page).
_PRICE_LABELS = [r"Grand\s+Total", r"Order\s+Total", r"Total\s+Paid", r"Total\s+Amount",
                 r"Amount\s+Paid", r"Final\s+Price"]
_GENERIC_LABELS = [r"Price", r"Total"]
_NOT_A_PRICE = re.compile(r"discount|saved|saving|refund|coupon|cashback|\bmrp\b|listing|shipping|delivery|"
                          r"\bfees?\b|\btax|\bgst\b|\bemi\b|offer|reward|supercoin|sub\s*total|before", re.I)
_NOT_A_PRICE_STRICT = re.compile(r"discount|saved|saving|refund", re.I)


def _amount_after(lines: list, i: int, label: str):
    """Amount on the same line after `label`, or alone on the next line when the label stands alone."""
    m = re.search(rf"(?<![A-Za-z]){label}\b[^\d\n]{{0,25}}?{_AMT}", lines[i], re.I)
    if m and re.search(rf"(?<![A-Za-z]){label}\b\s*[:\-]?\s*(?:\(.*?\))?\s*(?:₹|Rs\.?|INR)?\s*[\d,]", lines[i], re.I):
        return m.group(1)
    if re.fullmatch(rf"(?i)(?<![A-Za-z]){label}\b\s*(?:\(.*?\))?\s*[:\-]?", lines[i]) and i + 1 < len(lines):
        m2 = re.match(rf"^\s*{_AMT}\s*$", lines[i + 1])
        if m2:
            return m2.group(1)
    return None


def extract_price(text: str) -> str:
    """Amount actually paid. Priority labels first; generic 'Price'/'Total' only if none matched,
    and never from discount / MRP / shipping / fee / tax / sub-total lines."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    def valid(v):
        v = (v or "").replace(",", "")
        try:
            return v if float(v) > 0 else ""
        except ValueError:
            return ""

    for label in _PRICE_LABELS:
        for i, line in enumerate(lines):
            if _NOT_A_PRICE_STRICT.search(line):
                continue
            v = valid(_amount_after(lines, i, label))
            if v:
                return v
    for label in _GENERIC_LABELS:
        for i, line in enumerate(lines):
            if _NOT_A_PRICE.search(line):
                continue
            v = valid(_amount_after(lines, i, label))
            if v:
                return v
    for line in lines:                                   # last resort: first rupee amount on a plain line
        if _NOT_A_PRICE.search(line):
            continue
        m = re.search(r"₹\s*([\d,]+(?:\.\d{1,2})?)", line)
        if m and valid(m.group(1)):
            return valid(m.group(1))
    return ""


def extract_timeline_dates(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1. PRICE EXTRACTION
    price = extract_price(text)

    # 2. PINCODE EXTRACTION
    pincode = ""
    addr_block = re.search(
        r"(?:Shipping Address|Delivery Address|Ship To|Deliver To|Delivery Details)[\s\S]{1,350}?\b([1-9][0-9]{5})\b",
        text, re.I
    )
    if addr_block:
        pincode = addr_block.group(1)
    else:
        state_pin = re.search(r"\b(?:TN|Tamil Nadu|KA|Karnataka|MH|IN|PIN|Pincode)?\s*[-–:]?\s*([1-9][0-9]{5})\b", text, re.I)
        if state_pin:
            pincode = state_pin.group(1)

    # 3. PAYMENT METHOD EXTRACTION
    payment_contexts = []
    payment_heading = re.compile(
        r"^(?:payment\s+(?:method|information|details|mode|summary)|mode\s+of\s+payment)"
        r"(?:\s*:\s*(.*))?$",
        re.I,
    )
    paid_by_pattern = re.compile(r"\bpaid\b.{0,60}\b(?:by|via|using)\b", re.I)
    for i, line in enumerate(lines):
        heading_match = payment_heading.search(line)
        if heading_match:
            context_lines = [heading_match.group(1) or ""]
            context_lines.extend(lines[i + 1:min(len(lines), i + 5)])
            payment_contexts.append(" ".join(context_lines))
        elif paid_by_pattern.search(line):
            payment_contexts.append(line)

    payment_text = " ".join(payment_contexts)
    if re.search(r"Pay on Delivery|Cash on Delivery|\bCOD\b|\bPOD\b", payment_text, re.I):
        pay_method = "Cash on Delivery"
    elif re.search(r"\bUPI\b|Google Pay|PhonePe|Paytm|\bBHIM\b|Unified Payment|VPA", payment_text, re.I):
        pay_method = "UPI"
    elif re.search(r"Credit Card|Debit Card|Card ending|\bVisa\b|MasterCard|RuPay", payment_text, re.I):
        pay_method = "Card"
    elif re.search(r"Net Banking|Internet Banking", payment_text, re.I):
        pay_method = "Net Banking"
    elif re.search(r"Amazon Pay Balance|Wallet|Flipkart Pay Later|SuperCoins|Gift Card|E-Gift", payment_text, re.I):
        pay_method = "Wallet / Pay Later"
    else:
        pay_method = ""

    # 4. ORDER DATE EXTRACTION (Enhanced for Flipkart timeline formats)
    _MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    _DATE_SRC = r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?(?:\s+\d{4})?|" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"
    
    def find_date(patterns, back: int = 1):
        # back=0 for status lines like "Replacement delivered": the date AFTER the label belongs to it, and the
        # line before is usually the previous milestone's date.
        for pat in patterns:
            for i, line in enumerate(lines):
                if re.search(pat, line, re.I):
                    context = " ".join(lines[max(0, i-back):min(len(lines), i+5)])
                    m = re.search(_DATE_SRC, context, re.I)
                    if m:
                        return m.group()
        return ""

    order_date = find_date([
        r"order\s+(?:placed|confirmed|date)",
        r"ordered\s+on",
        r"ordered",
        r"placed\s+on",
        r"order\s+details",
    ])
    
    # Direct Fallback for Flipkart "Ordered on Mon, DD Month" string
    if not order_date:
        m = re.search(r"(?:Ordered|Placed)\s*(?:on)?\s*,?\s*([A-Za-z]{3},\s*" + _DATE_SRC + r")", text, re.I)
        if m:
            order_date = m.group(1)
        else:
            m2 = re.search(_DATE_SRC, text, re.I)
            if m2:
                order_date = m2.group(0)

    window_match = re.search(r"(?:Return|Replacement)\s+window\s+(?:closed|closes|eligible\s+until)\s+on?\s*(" + _DATE_SRC + r")", text, re.I)

    return {
        "order_date": order_date,
        "replacement_initialization": find_date([
            r"replacement\s+(?:requested|initiated|approved|ordered)",
            r"return\s+(?:requested|initiated|approved)"
        ], back=0),
        "replacement_date": find_date([
            r"replacement\s+(?:delivered|completed|shipped)",
            r"replacement\s+item\s+delivered"
        ], back=0),
        "replacement_window": window_match.group(1) if window_match else "",
        "price": price,
        "pincode": pincode,
        "payment_method": pay_method,
    }


def normalize_iso(value) -> str:
    """'2026-11-10T05:45:48.000Z' -> '2026-11-10T05:45:48+00:00' (same style as utc_now)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):     # Jira: 2026-11-10T11:15:48.000+0530
            try:
                return datetime.strptime(str(value).strip(), fmt).astimezone(timezone.utc).isoformat(timespec="seconds")
            except ValueError:
                continue
        return str(value).strip()


# Vocabularies of common ticketing tools (Zendesk, Jira, Freshdesk, ServiceNow, generic helpdesks).
_STATUS_WORDS = (r"New|Open|Pending|On[- ]hold|Solved|Closed|Resolved|Reopened|To Do|In Progress|Done|Waiting(?: for \w+)?|"
                 r"Backlog|In Review|Canceled|Cancelled|Escalated|Work in progress|Awaiting [\w ]{3,25}")
_PRIORITY_WORDS = r"Lowest|Low|Normal|Medium|High|Highest|Urgent|Critical|Blocker|Major|Minor|Trivial|P[0-5]"


def _labelled_value(text: str, label: str, words: str) -> str:
    """Value of a 'Label: value' pair, or of a label that stands alone with the value on the next line."""
    m = re.search(rf"\b{label}\b\s*[:\-]?\s*({words})\b", text, re.I)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines[:-1]):
        if re.fullmatch(rf"{label}\s*:?", line, re.I):
            nxt = lines[i + 1]
            if re.fullmatch(words, nxt, re.I):
                return nxt
    return ""


def extract_ticket_details(text: str, timestamps: list | None = None) -> dict:
    """
    DOM fallback, used only when the JSON API didn't supply a value.
    status/priority come from the page text; created_date is the earliest
    <time datetime="..."> on the page. resolved_date is NOT guessed.
    """
    status = _labelled_value(text, "Status", _STATUS_WORDS)
    priority = _labelled_value(text, "Priority", _PRIORITY_WORDS)

    parsed = [iso for iso in (normalize_iso(ts) for ts in (timestamps or [])) if iso]
    created_date = min(parsed) if parsed else ""

    return {
        "status": status,
        "priority": priority,
        "created_date": created_date,
        "resolved_date": "",
    }