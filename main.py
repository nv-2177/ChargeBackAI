import argparse
import hashlib
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
from pydantic import BaseModel, field_validator, ValidationError

from agents.browser_agent import PlatformBrowserAgent, BrowserAgentError
from agents.ticket_agent import TicketBrowserAgent, TicketAgentError
from agents.evidence_utils import utc_now, write_json, build_file_manifest

BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_ROOT = BASE_DIR / "evidence"
LOG_DIR = BASE_DIR / "logs"
PROFILE_ROOT = BASE_DIR / "credentials" / "profiles"

# Ticket Subject mismatch: you didn't specify, so by default it is only RECORDED
# (subject_match column = similarity score). Set True to mark such cases Failed.
STRICT_SUBJECT_MATCH = False

INPUT_FILES = {
    "orders": ("orders.csv", "orders.xlsx"),
    "tickets": ("tickets.csv", "tickets.xlsx"),
}

OUTPUT_COLS = [
    "processing_status", "error_message", "evidence_folder", "product_match",
    "order_date", "price", "pincode", "payment_method",
    "replacement_initialization", "replacement_date", "replacement_window",
]
TICKET_OUTPUT_COLS = [
    "processing_status", "error_message", "evidence_folder", "subject_match",
    "ticket_status", "priority", "created_date", "resolved_date",
]


class OrderCase(BaseModel):
    Case_ID: str
    Platform: str
    Platform_URL: str = ""
    Order_ID: str
    Product_Name: str
    Username: str
    Password: str = ""

    @field_validator("Case_ID", "Platform", "Order_ID", "Product_Name", "Username", mode="before")
    @classmethod
    def not_blank(cls, v):
        if v is None or str(v).strip() == "" or str(v).lower() == "nan":
            raise ValueError("must not be empty")
        return str(v).strip()

    @field_validator("Password", "Platform_URL", mode="before")
    @classmethod
    def clean_optional(cls, v):
        return "" if v is None or str(v).strip().lower() in ("", "nan", "none") else str(v).strip()


class TicketCase(BaseModel):
    Case_ID: str
    Platform: str
    Platform_URL: str
    Ticket_ID: str
    Subject: str = ""
    Username: str
    Password: str = ""

    # Platform_URL is required for tickets: the Zendesk subdomain differs per row.
    @field_validator("Case_ID", "Platform", "Platform_URL", "Ticket_ID", "Username", mode="before")
    @classmethod
    def not_blank(cls, v):
        if v is None or str(v).strip() == "" or str(v).lower() == "nan":
            raise ValueError("must not be empty")
        return str(v).strip()

    @field_validator("Password", "Subject", mode="before")
    @classmethod
    def clean_optional(cls, v):
        return "" if v is None or str(v).strip().lower() in ("", "nan", "none") else str(v).strip()


# ------------------------------------------------------------------ console menu / files
def choose_mode() -> str:
    print("\n=== ChargebackAI ===")
    print("  1) E-commerce platforms  (data/orders.csv)")
    print("  2) Ticketing platforms   (data/tickets.csv)")
    while True:
        choice = input("Enter 1 or 2: ").strip()
        if choice == "1":
            return "orders"
        if choice == "2":
            return "tickets"
        print("Invalid choice, please enter 1 or 2.")


def find_input_file(mode: str, override_path: str = None):
    if override_path:
        p = Path(override_path)
        if p.exists():
            return p
        sys.exit(f"File not found: {override_path}")
    for name in INPUT_FILES[mode]:
        p = BASE_DIR / "data" / name
        if p.exists():
            return p
    return None


def read_table(path: Path, mode: str = "orders") -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        df = pd.read_excel(path, engine="openpyxl", dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    df = df.fillna("")
    df.columns = [str(c).strip().lower() for c in df.columns]

    cols_to_check = TICKET_OUTPUT_COLS if mode == "tickets" else OUTPUT_COLS
    for col in cols_to_check:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype(object)
    return df


def save_table(df: pd.DataFrame, path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        df.to_excel(path, index=False, engine="openpyxl")
    else:
        df.to_csv(path, index=False)


def update_row(path: Path, idx: int, mode: str = "orders", **fields):
    df = read_table(path, mode=mode)  # was always orders mode before
    for col, val in fields.items():
        if col not in df.columns:
            df[col] = ""
        df.at[idx, col] = val
    save_table(df, path)


def create_evidence_dirs(case_id: str) -> Path:
    case_dir = EVIDENCE_ROOT / case_id
    (case_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (case_dir / "documents").mkdir(parents=True, exist_ok=True)
    return case_dir


def write_case_files(case_dir: Path, case, agent, started_at: str, outcome: str, error: str = ""):
    details = dict(agent.details) if agent else {}
    is_ticket = isinstance(case, TicketCase)

    write_json(case_dir / ("ticket_details.json" if is_ticket else "order_details.json"), details)

    meta = {
        "case_id": case.Case_ID,
        "platform": case.Platform,
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "outcome": outcome,
        "error": error or None,
    }
    if is_ticket:
        meta.update({
            "ticket_id": case.Ticket_ID,
            "ticket_details": details.get("ticket_details", {}),
            "field_sources": details.get("field_sources", {}),
            "attachments": details.get("attachments"),
        })
    else:
        meta.update({
            "order_id": case.Order_ID,
            "timeline": details.get("timeline", {}),
            "invoices": details.get("invoices"),
        })
    meta["files"] = build_file_manifest(case_dir)
    write_json(case_dir / "metadata.json", meta)


def _profile_dir(prefix: str, row: dict, platform_url: str = "") -> Path:
    # Ticketing: include the subdomain so two Zendesk accounts with the same email don't share a session.
    key = row.get("username", "").lower()
    if platform_url:
        p = urlsplit(platform_url)
        key += "|" + p.netloc.lower()
    who = hashlib.sha1(key.encode()).hexdigest()[:10]
    d = PROFILE_ROOT / f"{prefix}{row.get('platform', '').lower()}_{who}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ e-commerce (unchanged logic)
def process_case(table_path: Path, idx: int, row: dict):
    fetch_started = time.perf_counter()
    case_id = row.get("case id")
    try:
        case = OrderCase(
            Case_ID=case_id,
            Platform=row.get("platform"),
            Platform_URL=row.get("platform_url", ""),
            Order_ID=row.get("order id"),
            Product_Name=row.get("product name"),
            Username=row.get("username"),
            Password=row.get("password", ""),
        )
    except ValidationError as e:
        msg = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
        update_row(table_path, idx, processing_status="Failed", error_message=f"Validation error: {msg}")
        print(f"[{case_id}] Validation failed: {msg}")
        print(f"[{case_id}] Product fetch took {time.perf_counter() - fetch_started:.1f} seconds.")
        return

    case_dir = create_evidence_dirs(case.Case_ID)
    update_row(table_path, idx, processing_status="Processing", error_message="", evidence_folder=str(case_dir))

    profile_dir = _profile_dir("", row)

    started_at = utc_now()
    agent = PlatformBrowserAgent(
        platform_name=case.Platform,
        evidence_dir=case_dir,
        headless=False,
        profile_dir=profile_dir
    )
    status, error = "Failed", ""

    try:
        agent.start()
        agent.login(case.Platform_URL, case.Username, case.Password)
        agent.go_to_orders()
        agent.collect_evidence(case.Case_ID, case.Order_ID, case.Product_Name)

        pv = agent.details.get("product_verification", {})
        if pv.get("matched"):
            status = "Completed"
        else:
            error = f"Product mismatch: expected '{case.Product_Name}', found '{pv.get('matched_title')}'."

        tm = agent.details.get("timeline", {})
        update_row(
            table_path, idx,
            product_match=f"{pv.get('score', '')}",
            order_date=tm.get("order_date", ""),
            price=tm.get("price", ""),
            pincode=tm.get("pincode", ""),
            payment_method=tm.get("payment_method", ""),
            replacement_initialization=tm.get("replacement_initialization", ""),
            replacement_date=tm.get("replacement_date", ""),
            replacement_window=tm.get("replacement_window", "")
        )
        print(f"[{case.Case_ID}] {status}. Data & evidence collected successfully.")
    except BrowserAgentError as e:
        error = str(e)
        print(f"[{case.Case_ID}] Platform Failed: {e}")
    except Exception as e:
        error = f"Unexpected Error: {e}"
        traceback.print_exc()
    finally:
        try:
            write_case_files(case_dir, case, agent, started_at, status, error)
        except Exception as e:
            error += f" | metadata write failed: {e}"
        update_row(table_path, idx, processing_status=status, error_message=error)
        try:
            agent.close()
        except Exception:
            pass
        print(f"[{case.Case_ID}] Product fetch took {time.perf_counter() - fetch_started:.1f} seconds.")


# ------------------------------------------------------------------ ticketing
def process_ticket_case(table_path: Path, idx: int, row: dict):
    fetch_started = time.perf_counter()
    case_id = row.get("case id")
    try:
        case = TicketCase(
            Case_ID=case_id,
            Platform=row.get("platform"),
            Platform_URL=row.get("platform_url"),
            Ticket_ID=row.get("ticket id"),
            Subject=row.get("subject", ""),
            Username=row.get("username"),
            Password=row.get("password", ""),
        )
    except ValidationError as e:
        msg = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
        update_row(table_path, idx, "tickets", processing_status="Failed", error_message=f"Validation error: {msg}")
        print(f"[{case_id}] Validation failed: {msg}")
        return

    case_dir = create_evidence_dirs(case.Case_ID)
    update_row(table_path, idx, "tickets", processing_status="Processing", error_message="",
               evidence_folder=str(case_dir))

    profile_dir = _profile_dir("ticket_", row, case.Platform_URL)

    started_at = utc_now()
    status, error = "Failed", ""
    agent = None
    try:
        agent = TicketBrowserAgent(
            platform_name=case.Platform,
            evidence_dir=case_dir,
            headless=False,
            profile_dir=profile_dir
        )
        agent.start()
        agent.login(case.Platform_URL, case.Username, case.Password)
        agent.collect_ticket_evidence(case.Case_ID, case.Ticket_ID, case.Subject)

        td = agent.details.get("ticket_details", {})
        sv = agent.details.get("subject_verification") or {}
        status = "Completed"
        if sv and not sv.get("matched"):
            note = f"Subject mismatch: expected '{case.Subject}', found '{agent.details.get('subject_actual', '')}'."
            if STRICT_SUBJECT_MATCH:
                status, error = "Failed", note
            else:
                error = f"Warning: {note}"
        update_row(
            table_path, idx, "tickets",
            subject_match=f"{sv.get('score', '')}" if sv else "",
            ticket_status=td.get("status", ""),
            priority=td.get("priority", ""),
            created_date=td.get("created_date", ""),
            resolved_date=td.get("resolved_date", ""),
        )
        print(f"[{case.Case_ID}] {status}. Ticket data & evidence collected "
              f"({len(agent.details.get('attachments', []))} attachment(s)).")
    except TicketAgentError as e:
        error = str(e)
        print(f"[{case.Case_ID}] Ticketing Failed: {e}")
    except Exception as e:
        error = f"Unexpected Error: {e}"
        traceback.print_exc()
    finally:
        try:
            write_case_files(case_dir, case, agent, started_at, status, error)
        except Exception as e:
            error += f" | metadata write failed: {e}"
        update_row(table_path, idx, "tickets", processing_status=status, error_message=error)
        try:
            if agent:
                agent.close()
        except Exception:
            pass
        print(f"[{case.Case_ID}] Ticket fetch took {time.perf_counter() - fetch_started:.1f} seconds.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to input file (.csv or .xlsx)")
    parser.add_argument("--type", choices=["orders", "tickets"],
                        help="Skip the menu: orders = e-commerce, tickets = ticketing")
    args = parser.parse_args()

    mode = args.type or choose_mode()

    table_path = find_input_file(mode, args.file)
    if not table_path:
        names = " or ".join(INPUT_FILES[mode])
        sys.exit(f"Input file not found. Place {names} in the data/ folder.")
    print(f"Using {table_path}")

    df = read_table(table_path, mode=mode)
    pending = df[df["processing_status"].str.strip().str.lower() == "pending"]
    if pending.empty:
        print("No Pending cases found.")
        return

    for idx, row in pending.iterrows():
        print(f"\n--- Processing Case: {row.get('case id')} [{row.get('platform')}] ({mode}) ---")
        if mode == "orders":
            process_case(table_path, idx, row.to_dict())
        else:
            process_ticket_case(table_path, idx, row.to_dict())


if __name__ == "__main__":
    main()