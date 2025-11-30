#!/usr/bin/env python3
"""
agentThreshold.py
Monitors Hyperliquid vault perpetual positions and emails alerts when any position
value (Size × Mark Price) exceeds the configured USD threshold.
"""

from __future__ import annotations

import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from typing import List, Optional

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeoutError, sync_playwright

VAULT_URL = "https://app.hyperliquid.xyz/vaults/0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"

EMAIL_SENDER = "cryptosscalp@gmail.com"
EMAIL_PASSWORD = "gfke olcu ulud zpnh"
EMAIL_RECEIVER = "25harshitgarg12345@gmail.com"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

POSITION_VALUE_THRESHOLD = Decimal("50000")
TABLE_WAIT_SECONDS = 45
ROW_POLL_INTERVAL_MS = 1500

NUMERIC_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")

@dataclass
class Position:
    coin: str
    leverage: str
    size: Decimal
    mark_price: Decimal

    @property
    def position_value(self) -> Decimal:
        return self.size * self.mark_price

    @property
    def absolute_position_value(self) -> Decimal:
        return self.position_value.copy_abs()


def parse_decimal_from_text(raw_text: str) -> Optional[Decimal]:
    if raw_text is None:
        return None
    sanitized = (
        raw_text.replace(",", "")
        .replace("$", "")
        .replace("USD", "")
        .strip()
    )
    match = NUMERIC_PATTERN.search(sanitized)
    if not match:
        return None
    try:
        return Decimal(match.group())
    except InvalidOperation:
        return None


def format_decimal(value: Decimal) -> str:
    formatted = f"{value:,.6f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def format_currency(value: Decimal) -> str:
    return f"${value:,.2f}"


def locate_perp_table(page) -> Optional[Locator]:
    try:
        heading_locator = page.get_by_text("Perpetual Positions", exact=False)
        heading_count = heading_locator.count()
        for idx in range(heading_count):
            heading = heading_locator.nth(idx)
            table_candidate = heading.locator("xpath=ancestor-or-self::*[.//table][1]//table").first
            if table_candidate.count() > 0:
                return table_candidate
    except Exception as exc:
        print(f"[WARN] Unable to resolve table via heading: {exc}")

    table_locator = page.locator("table")
    table_count = table_locator.count()
    print(f"[DEBUG] Scanning {table_count} table element(s) for PERP data.")
    for idx in range(table_count):
        table = table_locator.nth(idx)
        headers = table.locator("th").all_inner_texts()
        headers_upper = " ".join(text.strip().upper() for text in headers)
        if headers_upper and all(keyword in headers_upper for keyword in ("COIN", "SIZE", "MARK")):
            print(f"[DEBUG] Using table #{idx} with headers: {headers}")
            return table
    return None


def wait_for_perp_table(page) -> Locator:
    deadline = time.time() + TABLE_WAIT_SECONDS
    attempt = 1
    last_error: Optional[Exception] = None

    while time.time() < deadline:
        table = locate_perp_table(page)
        if table:
            for locator in ("tbody tr", "tr", "[role='row']"):
                try:
                    row_count = table.locator(locator).count()
                except Exception as exc:
                    last_error = exc
                    row_count = 0
                if row_count > 0:
                    print(f"[DEBUG] Located PERP table with {row_count} row(s) on attempt {attempt}.")
                    return table
        print(f"[DEBUG] Perpetual positions table not ready (attempt {attempt}). Retrying...")
        attempt += 1
        page.wait_for_timeout(ROW_POLL_INTERVAL_MS)

    raise RuntimeError("Perpetual positions table not found before timeout.") from last_error


def collect_table_rows(table: Locator) -> List[List[str]]:
    search_order = ["tbody tr", "tr", "[role='row']"]
    for selector in search_order:
        row_locator = table.locator(selector)
        row_count = row_locator.count()
        if row_count == 0:
            continue
        rows: List[List[str]] = []
        for idx in range(row_count):
            row = row_locator.nth(idx)
            cell_locator = row.locator("td")
            if cell_locator.count() == 0:
                cell_locator = row.locator("[role='cell']")
            cell_count = cell_locator.count()
            if cell_count == 0:
                continue
            cells = [cell_locator.nth(c).inner_text().strip() for c in range(cell_count)]
            rows.append(cells)
        if rows:
            return rows
    return []


def scrape_perp_positions(url: str) -> List[Position]:
    print(f"[INFO] Navigating to vault page: {url}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                print("[WARN] Network idle state not reached; continuing.")
            table = wait_for_perp_table(page)
            raw_rows = collect_table_rows(table)
            positions: List[Position] = []

            for row in raw_rows:
                if not row:
                    continue
                normalized = [cell.strip() for cell in row]
                if normalized[0].upper() == "COIN":
                    continue
                while len(normalized) < 4:
                    normalized.append("")
                coin, leverage, size_text, mark_text = normalized[:4]
                size_value = parse_decimal_from_text(size_text)
                mark_value = parse_decimal_from_text(mark_text)

                if not coin or size_value is None or mark_value is None:
                    print(f"[WARN] Skipping row due to missing data: {normalized}")
                    continue

                positions.append(
                    Position(
                        coin=coin,
                        leverage=leverage,
                        size=size_value,
                        mark_price=mark_value,
                    )
                )
            print(f"[INFO] Extracted {len(positions)} perpetual position(s).")
            return positions
        finally:
            context.close()
            browser.close()


def build_alert_body(exceeding_positions: List[Position]) -> str:
    lines = [
        "🚨 Hyperliquid PERP Position Threshold Triggered 🚨",
        "",
        f"The following positions exceeded the {format_currency(POSITION_VALUE_THRESHOLD)} threshold (absolute value):",
        "",
    ]
    for pos in exceeding_positions:
        lines.extend(
            [
                f"- Coin: {pos.coin}",
                f"  Leverage: {pos.leverage or 'N/A'}",
                f"  Size: {format_decimal(pos.size)}",
                f"  Mark Price: {format_currency(pos.mark_price)}",
                f"  Position Value (Size × Mark): {format_currency(pos.position_value)}",
                f"  Absolute Value: {format_currency(pos.absolute_position_value)}",
                "",
            ]
        )
    lines.append("Monitoring agent: agentThreshold.py")
    return "\n".join(lines).strip()


def build_no_alert_body() -> str:
    return "No coin exceeds the \$50,000 position value threshold."


def send_email(subject: str, body: str) -> None:
    print(f"[INFO] Sending email notification: {subject}")
    message = EmailMessage()
    message["From"] = EMAIL_SENDER
    message["To"] = EMAIL_RECEIVER
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.send_message(message)
    print("[INFO] Email dispatched successfully.")


def main() -> None:
    print("[INFO] Starting Hyperliquid threshold monitor.")
    positions = scrape_perp_positions(VAULT_URL)

    for pos in positions:
        print(
            "[DEBUG] "
            f"{pos.coin} | leverage={pos.leverage} | "
            f"size={format_decimal(pos.size)} | "
            f"mark={format_currency(pos.mark_price)} | "
            f"value={format_currency(pos.position_value)} | "
            f"abs={format_currency(pos.absolute_position_value)}"
        )

    exceeding = [
        pos for pos in positions if pos.absolute_position_value > POSITION_VALUE_THRESHOLD
    ]

    if exceeding:
        subject = "⚠️ Hyperliquid PERP Position Alert"
        body = build_alert_body(exceeding)
    else:
        subject = "Hyperliquid PERP Position Status"
        body = build_no_alert_body()

    send_email(subject, body)
    print("[INFO] Monitoring cycle complete.")


if __name__ == "__main__":
    main()
