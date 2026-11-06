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


def extract_timeline_dates(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1. PRICE EXTRACTION
    price = ""
    amount = r"(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?)"
    price_patterns = [
        rf"(?:Grand\s+Total|Order\s+Total|Total\s+Paid|Total\s+Amount|Amount\s+Paid|Final\s+Price|Price|Total)\s*[:\-]?\s*{amount}",
        r"₹\s*([\d,]+(?:\.\d{1,2})?)",
    ]
    for pattern in price_patterns:
        match = re.search(pattern, text, re.I)
        if match and match.group(1):
            val = match.group(1).replace(",", "")
            if float(val) > 0:
                price = val
                break

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
    
    def find_date(patterns):
        for pat in patterns:
            for i, line in enumerate(lines):
                if re.search(pat, line, re.I):
                    context = " ".join(lines[max(0, i-1):min(len(lines), i+5)])
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
        ]),
        "replacement_date": find_date([
            r"replacement\s+(?:delivered|completed|shipped)",
            r"replacement\s+item\s+delivered"
        ]),
        "replacement_window": window_match.group(1) if window_match else "",
        "price": price,
        "pincode": pincode,
        "payment_method": pay_method,
    }