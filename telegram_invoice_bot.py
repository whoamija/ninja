#!/usr/bin/env python3
"""
Telegram → Invoice Ninja bot with multiple line items.

Commands
--------
  /start
  /invoice              start or reset a draft for this chat
  /item NOTES, COST, QTY
                        add a line. QTY defaults to 1. Avoid commas in the item name.
  /items                show the current draft
  /undo                 remove the last line
  /cancel               drop the whole draft
  YES / Y               create the invoice and send the PDF
  NO                    same as /cancel

Examples
--------
  /invoice
  /item AC service, 15000, 1
  /item Gas recharge, 8000
  /item Labour, 4500, 2
  YES

Setup
-----
    python3 -m pip install "python-telegram-bot>=21" requests

    export TG_TOKEN="..."
    export IN_URL="https://your-ninja-host"
    export IN_TOKEN="..."
    export CLIENT_ID="..."

    python3 telegram_invoice_bot.py
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
IN_URL = os.environ.get("IN_URL", "").rstrip("/")
IN_TOKEN = os.environ.get("IN_TOKEN", "").strip()
CLIENT_ID = os.environ.get("CLIENT_ID", "").strip()

DEFAULT_CLIENT_NAME = "Miss Pat"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("invoice-bot")


@dataclass
class LineItem:
    notes: str
    cost: float
    quantity: float

    @property
    def line_total(self) -> float:
        return self.cost * self.quantity

    def to_ninja(self) -> dict[str, Any]:
        return {
            "notes": self.notes,
            "cost": self.cost,
            "quantity": self.quantity,
        }


@dataclass
class Draft:
    items: list[LineItem] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(item.line_total for item in self.items)


# chat_id → draft  (lost on restart; fine for testing)
pending: dict[int, Draft] = {}


def ninja_headers(json_body: bool = False) -> dict[str, str]:
    headers = {
        "X-API-TOKEN": IN_TOKEN,
        "X-Requested-With": "XMLHttpRequest",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def create_invoice(items: list[LineItem]) -> dict[str, Any]:
    payload = {
        "client_id": CLIENT_ID,
        "line_items": [item.to_ninja() for item in items],
    }
    url = f"{IN_URL}/api/v1/invoices"
    response = requests.post(
        url,
        headers=ninja_headers(json_body=True),
        json=payload,
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(
            f"Create invoice failed {response.status_code}: {response.text}"
        )
    return response.json()["data"]


def download_invoice_pdf(invitation_key: str) -> bytes:
    url = f"{IN_URL}/api/v1/invoice/{invitation_key}/download"
    response = requests.get(
        url,
        headers=ninja_headers(json_body=False),
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(
            f"PDF download failed {response.status_code}: {response.text}"
        )
    return response.content


def parse_number(raw: str) -> float:
    cleaned = (
        raw.strip()
        .replace(",", "")
        .replace("$", "")
        .replace("J$", "")
        .replace("j$", "")
    )
    if cleaned.lower().endswith("k") and cleaned[:-1].replace(".", "", 1).isdigit():
        return float(cleaned[:-1]) * 1000
    return float(cleaned)


def parse_item_chunks(args: list[str]) -> list[LineItem]:
    """
    Accepts:
      notes, cost, qty
      notes, cost
      and repeated groups after that.
    Split on commas. Do not put commas inside the item name.
    """
    blob = " ".join(args).strip()
    if not blob:
        raise ValueError("empty")

    parts = [p.strip() for p in blob.split(",")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        raise ValueError("need at least NOTES, COST")

    items: list[LineItem] = []
    i = 0
    while i < len(parts):
        notes = parts[i]
        if i + 1 >= len(parts):
            raise ValueError(f"item '{notes}' is missing a cost")
        cost = parse_number(parts[i + 1])
        qty = 1.0
        nxt = i + 2
        if nxt < len(parts):
            try:
                qty = parse_number(parts[nxt])
                nxt += 1
            except ValueError:
                qty = 1.0
        if cost <= 0 or qty <= 0:
            raise ValueError("cost and qty must be greater than zero")
        items.append(LineItem(notes=notes, cost=cost, quantity=qty))
        i = nxt
    return items


def format_draft(draft: Draft) -> str:
    if not draft.items:
        return (
            f"DRAFT for {DEFAULT_CLIENT_NAME} is empty.\n"
            "Add a line:\n"
            "/item AC service, 15000, 1"
        )
    lines = [f"DRAFT (not saved) — {DEFAULT_CLIENT_NAME}", ""]
    for idx, item in enumerate(draft.items, start=1):
        lines.append(
            f"{idx}. {item.notes}\n"
            f"    J${item.cost:,.2f} × {item.quantity:g} = J${item.line_total:,.2f}"
        )
    lines.extend(
        [
            "",
            f"TOTAL  J${draft.total:,.2f}",
            "",
            "Add another: /item NOTES, COST, QTY",
            "Remove last: /undo",
            "Save: YES     Discard: NO or /cancel",
        ]
    )
    return "\n".join(lines)


def get_or_create_draft(chat_id: int) -> Draft:
    draft = pending.get(chat_id)
    if draft is None:
        draft = Draft()
        pending[chat_id] = draft
    return draft


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Invoice bot ready.\n\n"
        "1. /invoice\n"
        "2. /item AC service, 15000, 1\n"
        "3. /item Gas, 8000\n"
        "4. YES\n\n"
        "Format: /item NOTES, UNIT_COST, QTY\n"
        "QTY is optional (default 1). 15k means 15000."
    )


async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    pending[chat_id] = Draft()
    extra = ""
    if context.args:
        try:
            items = parse_item_chunks(context.args)
            pending[chat_id].items.extend(items)
        except ValueError as exc:
            extra = (
                f"\nCould not parse items after /invoice ({exc}). "
                "Use /item NOTES, COST, QTY"
            )
    await update.message.reply_text(format_draft(pending[chat_id]) + extra)


async def cmd_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/item AC service, 15000, 1\n\n"
            "QTY is optional. Separate fields with a comma."
        )
        return
    try:
        items = parse_item_chunks(context.args)
    except ValueError as exc:
        await update.message.reply_text(
            f"Could not read that line: {exc}\n"
            "Example: /item Labour, 4500, 2"
        )
        return
    draft = get_or_create_draft(update.effective_chat.id)
    draft.items.extend(items)
    await update.message.reply_text(format_draft(draft))


async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = pending.get(update.effective_chat.id)
    if draft is None:
        await update.message.reply_text("No draft. Send /invoice first.")
        return
    await update.message.reply_text(format_draft(draft))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = pending.get(update.effective_chat.id)
    if not draft or not draft.items:
        await update.message.reply_text("Nothing to undo.")
        return
    removed = draft.items.pop()
    await update.message.reply_text(
        f"Removed: {removed.notes}\n\n{format_draft(draft)}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if pending.pop(update.effective_chat.id, None) is None:
        await update.message.reply_text("No draft to cancel.")
        return
    await update.message.reply_text("Draft discarded. Nothing was saved.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip().lower()
    chat_id = update.effective_chat.id

    if text in {"no", "n", "cancel"}:
        await cmd_cancel(update, context)
        return

    if text not in {"yes", "y"}:
        return

    draft = pending.get(chat_id)
    if draft is None or not draft.items:
        await update.message.reply_text(
            "No draft with items. Send /invoice then /item NOTES, COST, QTY"
        )
        return

    pending.pop(chat_id, None)
    await update.message.reply_text("Creating invoice…")

    try:
        invoice = create_invoice(draft.items)
        invitations = invoice.get("invitations") or []
        if not invitations or not invitations[0].get("key"):
            raise RuntimeError(
                "Invoice created but no invitation key returned. "
                f"Raw keys: {list(invoice.keys())}"
            )
        pdf_bytes = download_invoice_pdf(invitations[0]["key"])
    except Exception as exc:
        log.exception("Invoice Ninja call failed")
        await update.message.reply_text(f"Failed: {exc}")
        return

    number = invoice.get("number") or invoice.get("id") or "invoice"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=f"{number}.pdf",
                caption=f"Saved as {number} — J${draft.total:,.2f}",
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def require_env() -> None:
    missing = [
        name
        for name, value in (
            ("TG_TOKEN", TG_TOKEN),
            ("IN_URL", IN_URL),
            ("IN_TOKEN", IN_TOKEN),
            ("CLIENT_ID", CLIENT_ID),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))


def main() -> None:
    require_env()
    log.info("Starting bot. Invoice Ninja base: %s", IN_URL)
    app = Application.builder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("invoice", cmd_invoice))
    app.add_handler(CommandHandler("item", cmd_item))
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
