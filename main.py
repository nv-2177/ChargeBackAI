import argparse
import hashlib
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, field_validator, ValidationError

from agents.browser_agent import PlatformBrowserAgent, BrowserAgentError
from agents.evidence_utils import utc_now, write_json, build_file_manifest
from config.platforms import get_platform_config

BASE_DIR = Path(__file__).resolve().parent
EVIDENCE_ROOT = BASE_DIR / "evidence"
LOG_DIR = BASE_DIR / "logs"
PROFILE_ROOT = BASE_DIR / "credentials" / "profiles"

OUTPUT_COLS = [
    "processing_status", "error_message", "evidence_folder", "product_match",
    "order_date", "price", "pincode", "payment_method",
    "replacement_initialization", "replacement_date", "replacement_window",
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


def find_input_file(override_path: str = None) -> Path:
    if override_path:
        p = Path(override_path)
        if p.exists():
            return p
    for name in ("orders.csv", "orders.xlsx"):
        p = BASE_DIR / "data" / name
        if p.exists():
            return p
    return None


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        df = pd.read_excel(path, engine="openpyxl", dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    df = df.fillna("")
    df.columns = [str(c).strip().lower() for c in df.columns]
    for col in OUTPUT_COLS:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].astype(object)
    return df


def save_table(df: pd.DataFrame, path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        df.to_excel(path, index=False, engine="openpyxl")
    else:
        df.to_csv(path, index=False)


def update_row(path: Path, idx: int, **fields):
    df = read_table(path)
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


def write_case_files(case_dir: Path, case: OrderCase, agent, started_at: str, outcome: str, error: str = ""):
    details = dict(agent.details) if agent else {}
    write_json(case_dir / "order_details.json", details)
    write_json(case_dir / "metadata.json", {
        "case_id": case.Case_ID,
        "order_id": case.Order_ID,
        "platform": case.Platform,
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "outcome": outcome,
        "error": error or None,
        "timeline": details.get("timeline", {}),
        "invoices": details.get("invoices"),
        "files": build_file_manifest(case_dir),
    })


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

    # User Profile Directory to permanently save session/cookies
    who = hashlib.sha1(row.get("username", "").lower().encode()).hexdigest()[:10]
    profile_dir = PROFILE_ROOT / f"{row.get('platform', '').lower()}_{who}"
    profile_dir.mkdir(parents=True, exist_ok=True)

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to input orders file (.csv or .xlsx)")
    args = parser.parse_args()

    table_path = find_input_file(args.file)
    if not table_path:
        sys.exit("Input file not found. Place orders.csv or orders.xlsx in data/ folder.")

    df = read_table(table_path)
    pending = df[df["processing_status"].str.strip().str.lower() == "pending"]
    if pending.empty:
        print("No Pending cases found.")
        return

    for idx, row in pending.iterrows():
        print(f"\n--- Processing Case: {row.get('case id')} [{row.get('platform')}] ---")
        process_case(table_path, idx, row.to_dict())


if __name__ == "__main__":
    main()