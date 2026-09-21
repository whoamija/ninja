#!/usr/bin/env python3
"""
Telegram → Invoice Ninja bot.

Required environment
--------------------
  TG_TOKEN     Telegram bot token (BotFather)
  IN_URL       Invoice Ninja origin, no trailing slash
  IN_TOKEN     Invoice Ninja API token

Optional
--------
  ALLOWED_USER_IDS   Comma-separated Telegram user ids. Empty = anyone
                     who can message the bot.

Do NOT set CLIENT_ID. Clients are created and selected in chat.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

import requests
from telegram import Update
from telegram.constants import ParseMode
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
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("invoice-bot")

HELP_TEXT = """\
<b>Invoice bot</b>
Quotes and invoices live in Invoice Ninja. This chat is how you drive them.

<b>Everyday flow</b>
1. Add or pick a client
2. Start a quote or an invoice
3. Add items
4. Reply <b>YES</b> to save and get the PDF

━━━━━━━━━━━━━━━━
<b>Clients</b>

/client <code>Name, email, phone</code>
    Create a client and attach them to the current draft.
    Email or phone can be left out.
    Example:
    /client Miss Pat, pat@email.com, 8765551234

/client <code>Name</code>
    Find that client in Invoice Ninja and attach them.
    If several match, the bot lists them — send the command
    again with more of the name.

/clients
    First page of clients already in Invoice Ninja.

━━━━━━━━━━━━━━━━
<b>Start a document</b>

/quote
    Start a <b>quote</b> draft. Clears previous lines.

/invoice
    Start an <b>invoice</b> draft. Clears previous lines.

A document needs a client before YES. Set the client before
or after /quote or /invoice.

━━━━━━━━━━━━━━━━
<b>Line items</b>

Format for every line:
<code>Name, description, price, qty</code>

One item:
/item <code>Oil filter, car oil filter, 4275, 5</code>

Several items, new line each (send as one message):
<code>/item
Oil filter, car oil filter, 4275, 5
Cabin air filter, premium filter, 3365, 2</code>

Several items, one line:
/item <code>Oil filter, car oil filter, 4275, 5, Cabin air filter, premium filter, 3365, 2</code>

If the product name is new, it is created in Invoice Ninja
and reused next time.

/items     Show the draft
/undo      Remove the last line
/cancel    Throw the draft away

━━━━━━━━━━━━━━━━
<b>Save</b>

Reply <b>YES</b> or <b>Y</b> when the draft looks right.
Reply <b>NO</b> to discard.

━━━━━━━━━━━━━━━━
<b>Look up</b>

/quote <code>0001</code>
/invoice <code>0001</code>
    Summary + PDF. Use the number printed on the document.

/convert <code>0001</code>
    Turn that quote into an invoice. The quote number is
    the one on the quote PDF.

━━━━━━━━━━━━━━━━
<b>Money</b>

/unpaid
    Open invoices, every client.

/unpaid <code>Miss Pat</code>
    Open invoices for that client only.

/paid <code>0001</code>
    Mark that invoice paid in full (cash / Lynk / bank).

━━━━━━━━━━━━━━━━
<b>Tips</b>
• 15k is 15000. Do not put commas inside a name or description.
• /start and /help show this message.
• Nothing is saved until YES (except /client, /convert, /paid).
"""


@dataclass
class LineItem:
    product_key: str
    notes: str
    cost: float
    quantity: float

    @property
    def line_total(self) -> float:
        return self.cost * self.quantity

    def to_ninja(self) -> dict[str, Any]:
        return {
            "product_key": self.product_key,
            "notes": self.notes,
            "cost": self.cost,
            "quantity": self.quantity,
        }


@dataclass
class DraftClient:
    id: str
    name: str
    email: str = ""
    phone: str = ""
    number: str = ""


@dataclass
class Draft:
    kind: str = "invoice"  # invoice | quote
    client: DraftClient | None = None
    items: list[LineItem] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(item.line_total for item in self.items)


pending: dict[int, Draft] = {}


def ninja_headers(json_body: bool = False) -> dict[str, str]:
    headers = {
        "X-API-TOKEN": IN_TOKEN,
        "X-Requested-With": "XMLHttpRequest",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def ninja_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    expect_json: bool = True,
    timeout: int = 90,
) -> Any:
    url = f"{IN_URL}{path}"
    response = requests.request(
        method,
        url,
        headers=ninja_headers(json_body=json_body is not None),
        json=json_body,
        params=params,
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(
            f"{method} {path} failed {response.status_code}: {response.text[:500]}"
        )
    if not expect_json:
        return response.content
    if not response.content:
        return {}
    return response.json()


def parse_number(raw: str) -> float:
    cleaned = (
        raw.strip()
        .replace(",", "")
        .replace("$", "")
        .replace("J$", "")
        .replace("j$", "")
    )
    if cleaned.lower().endswith("k"):
        stem = cleaned[:-1]
        if stem.replace(".", "", 1).isdigit():
            return float(stem) * 1000
    return float(cleaned)


def parse_item_groups(blob: str) -> list[LineItem]:
    """
    Newline = one item.
    On one line, fields come in fours: name, description, price, qty.
    """
    items: list[LineItem] = []
    lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("no items")

    for line in lines:
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) < 3:
            raise ValueError(
                f"Need Name, description, price, qty — got: {line}"
            )
        if len(parts) == 3:
            # Name, price, qty (description omitted)
            groups = [parts]
        elif len(parts) % 4 == 0:
            groups = [parts[i : i + 4] for i in range(0, len(parts), 4)]
        elif len(parts) == 4:
            groups = [parts]
        else:
            raise ValueError(
                f"Use groups of 4 (name, description, price, qty). Got {len(parts)} fields on: {line}"
            )
        for group in groups:
            if len(group) == 3:
                name, price_s, qty_s = group
                desc = name
            else:
                name, desc, price_s, qty_s = group
            cost = parse_number(price_s)
            qty = parse_number(qty_s)
            if cost <= 0 or qty <= 0:
                raise ValueError("price and qty must be greater than zero")
            items.append(
                LineItem(
                    product_key=name.strip(),
                    notes=desc.strip(),
                    cost=cost,
                    quantity=qty,
                )
            )
    return items


def command_body(update: Update, command: str) -> str:
    text = update.message.text or ""
    # Drop "/command" and optional @botname
    return re.sub(rf"^/{re.escape(command)}(@\w+)?\s*", "", text, count=1).strip()


def format_money(amount: float) -> str:
    return f"J${amount:,.2f}"


def format_draft(draft: Draft) -> str:
    kind = "QUOTE" if draft.kind == "quote" else "INVOICE"
    client = draft.client.name if draft.client else "(no client yet — /client Name)"
    extra = ""
    if draft.client:
        bits = [x for x in (draft.client.email, draft.client.phone) if x]
        if bits:
            extra = "\n    " + " · ".join(bits)
    lines = [f"DRAFT {kind} — not saved", f"Client: {client}{extra}", ""]
    if not draft.items:
        lines.append("No items. Add with /item Name, description, price, qty")
    else:
        for idx, item in enumerate(draft.items, start=1):
            lines.append(
                f"{idx}. {item.product_key}\n"
                f"    {item.notes}\n"
                f"    {format_money(item.cost)} × {item.quantity:g} = {format_money(item.line_total)}"
            )
        lines += ["", f"TOTAL  {format_money(draft.total)}"]
    lines += [
        "",
        "YES to save    NO to discard    /undo last item",
    ]
    return "\n".join(lines)


def get_draft(chat_id: int) -> Draft:
    if chat_id not in pending:
        pending[chat_id] = Draft(kind="invoice")
    return pending[chat_id]


def client_from_api(raw: dict[str, Any]) -> DraftClient:
    contacts = raw.get("contacts") or []
    email = ""
    phone = ""
    if contacts:
        email = contacts[0].get("email") or ""
        phone = contacts[0].get("phone") or ""
    return DraftClient(
        id=raw["id"],
        name=raw.get("name") or raw.get("display_name") or "Client",
        email=email,
        phone=phone,
        number=str(raw.get("number") or raw.get("id_number") or ""),
    )


def search_clients(query: str) -> list[DraftClient]:
    data = ninja_request(
        "GET",
        "/api/v1/clients",
        params={"filter": query, "per_page": 20, "include": "contacts"},
    )
    return [client_from_api(row) for row in data.get("data") or []]


def list_clients() -> list[DraftClient]:
    data = ninja_request(
        "GET",
        "/api/v1/clients",
        params={"per_page": 20, "include": "contacts", "sort": "name|asc"},
    )
    return [client_from_api(row) for row in data.get("data") or []]


def create_client(name: str, email: str, phone: str) -> DraftClient:
    payload: dict[str, Any] = {
        "name": name,
        "contacts": [
            {
                "first_name": name.split()[0] if name else "",
                "last_name": " ".join(name.split()[1:]) if name else "",
                "email": email,
                "phone": phone,
                "is_primary": True,
            }
        ],
    }
    data = ninja_request("POST", "/api/v1/clients", json_body=payload)
    return client_from_api(data["data"])


def ensure_product(item: LineItem) -> None:
    existing = ninja_request(
        "GET",
        "/api/v1/products",
        params={"product_key": item.product_key, "per_page": 5},
    )
    rows = existing.get("data") or []
    for row in rows:
        if (row.get("product_key") or "").strip().lower() == item.product_key.lower():
            return
    ninja_request(
        "POST",
        "/api/v1/products",
        json_body={
            "product_key": item.product_key,
            "notes": item.notes,
            "cost": item.cost,
            "price": item.cost,
        },
    )


def create_document(draft: Draft) -> dict[str, Any]:
    if not draft.client:
        raise RuntimeError("Pick a client first with /client Name")
    if not draft.items:
        raise RuntimeError("Add at least one /item first")
    for item in draft.items:
        try:
            ensure_product(item)
        except Exception:
            log.exception("Product create skipped for %s", item.product_key)
    payload = {
        "client_id": draft.client.id,
        "line_items": [item.to_ninja() for item in draft.items],
    }
    path = "/api/v1/quotes" if draft.kind == "quote" else "/api/v1/invoices"
    return ninja_request("POST", path, json_body=payload)["data"]


def invitation_key(entity: dict[str, Any]) -> str:
    invitations = entity.get("invitations") or []
    if not invitations or not invitations[0].get("key"):
        raise RuntimeError("No PDF invitation key on that document")
    return invitations[0]["key"]


def download_pdf(kind: str, key: str) -> bytes:
    path = (
        f"/api/v1/quote/{key}/download"
        if kind == "quote"
        else f"/api/v1/invoice/{key}/download"
    )
    return ninja_request("GET", path, expect_json=False)


def find_by_number(kind: str, number: str) -> dict[str, Any]:
    path = "/api/v1/quotes" if kind == "quote" else "/api/v1/invoices"
    data = ninja_request(
        "GET",
        path,
        params={"number": number, "per_page": 10, "include": "client"},
    )
    rows = data.get("data") or []
    if not rows:
        data = ninja_request(
            "GET",
            path,
            params={"filter": number, "per_page": 20, "include": "client"},
        )
        rows = [
            r
            for r in (data.get("data") or [])
            if str(r.get("number") or "").lstrip("0") == number.lstrip("0")
            or str(r.get("number") or "") == number
        ]
    if not rows:
        raise RuntimeError(f"No {kind} numbered {number}")
    return rows[0]


def summarise_document(kind: str, entity: dict[str, Any]) -> str:
    client = entity.get("client") or {}
    client_name = client.get("name") or client.get("display_name") or "—"
    status = entity.get("status_id")
    balance = float(entity.get("balance") or 0)
    amount = float(entity.get("amount") or 0)
    number = entity.get("number") or entity.get("id")
    lines = [
        f"{kind.upper()} {number}",
        f"Client: {client_name}",
        f"Total: {format_money(amount)}",
        f"Balance: {format_money(balance)}",
    ]
    if status is not None:
        lines.append(f"Status id: {status}")
    items = entity.get("line_items") or []
    if items:
        lines.append("")
        for row in items:
            notes = row.get("product_key") or row.get("notes") or "item"
            cost = float(row.get("cost") or 0)
            qty = float(row.get("quantity") or 0)
            lines.append(f"• {notes}  {format_money(cost)} × {qty:g}")
    return "\n".join(lines)


async def send_pdf(
    update: Update,
    kind: str,
    entity: dict[str, Any],
    caption: str,
) -> None:
    pdf = download_pdf(kind, invitation_key(entity))
    number = entity.get("number") or kind
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf)
        path = tmp.name
    try:
        with open(path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=f"{number}.pdf",
                caption=caption[:1024],
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def guard(update: Update) -> bool:
    if allowed(update):
        return True
    if update.message:
        await update.message.reply_text("This bot is private.")
    return False


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help(update, context)


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        clients = list_clients()
    except Exception as exc:
        await update.message.reply_text(f"Could not load clients: {exc}")
        return
    if not clients:
        await update.message.reply_text(
            "No clients yet. Add one:\n/client Miss Pat, pat@email.com, 8765551234"
        )
        return
    lines = ["Clients in Invoice Ninja:", ""]
    for c in clients:
        bits = [c.name]
        if c.number:
            bits.append(f"#{c.number}")
        extra = " · ".join(x for x in (c.email, c.phone) if x)
        line = " • " + " ".join(bits)
        if extra:
            line += f"\n    {extra}"
        lines.append(line)
    lines.append("\nAttach one with /client Their Name")
    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_client(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    body = command_body(update, "client")
    if not body:
        await update.message.reply_text(
            "Create:\n/client Miss Pat, pat@email.com, 8765551234\n\n"
            "Or attach an existing client:\n/client Miss Pat"
        )
        return

    parts = [p.strip() for p in body.split(",") if p.strip()]
    name = parts[0]
    email = ""
    phone = ""
    for part in parts[1:]:
        if "@" in part and not email:
            email = part
        elif re.search(r"\d", part) and not phone:
            phone = part
        elif not email and not phone:
            name = f"{name} {part}".strip()

    draft = get_draft(update.effective_chat.id)
    creating = bool(email or phone) and len(parts) >= 2

    try:
        if creating:
            client = create_client(name, email, phone)
            draft.client = client
            await update.message.reply_text(
                f"Created client {client.name}"
                + (f" ({client.number})" if client.number else "")
                + f"\n\n{format_draft(draft)}"
            )
            return

        matches = search_clients(name)
        exact = [c for c in matches if c.name.lower() == name.lower()]
        pool = exact or matches
        if not pool:
            await update.message.reply_text(
                f'No client named "{name}". Create them:\n'
                f"/client {name}, email@domain.com, 8760000000"
            )
            return
        if len(pool) > 1:
            listing = "\n".join(f"• {c.name}" + (f" ({c.email})" if c.email else "") for c in pool[:10])
            await update.message.reply_text(
                "Several matches. Be more specific:\n" + listing
            )
            return
        draft.client = pool[0]
        await update.message.reply_text(
            f"Using {draft.client.name}\n\n{format_draft(draft)}"
        )
    except Exception as exc:
        log.exception("client command")
        await update.message.reply_text(f"Client failed: {exc}")


async def start_kind(update: Update, kind: str, body: str) -> None:
    if not await guard(update):
        return
    if body:
        # Lookup existing document
        try:
            entity = find_by_number(kind, body.split()[0])
            await update.message.reply_text(summarise_document(kind, entity))
            await send_pdf(update, kind, entity, f"{kind} {entity.get('number')}")
        except Exception as exc:
            await update.message.reply_text(str(exc))
        return

    chat_id = update.effective_chat.id
    previous = pending.get(chat_id)
    client = previous.client if previous else None
    pending[chat_id] = Draft(kind=kind, client=client, items=[])
    await update.message.reply_text(format_draft(pending[chat_id]))


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_kind(update, "quote", command_body(update, "quote"))


async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_kind(update, "invoice", command_body(update, "invoice"))


async def cmd_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    body = command_body(update, "item")
    if not body:
        await update.message.reply_text(
            "Example:\n/item Oil filter, car oil filter, 4275, 5"
        )
        return
    try:
        items = parse_item_groups(body)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    draft = get_draft(update.effective_chat.id)
    draft.items.extend(items)
    await update.message.reply_text(format_draft(draft))


async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(format_draft(get_draft(update.effective_chat.id)))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    draft = pending.get(update.effective_chat.id)
    if not draft or not draft.items:
        await update.message.reply_text("Nothing to undo.")
        return
    removed = draft.items.pop()
    await update.message.reply_text(
        f"Removed {removed.product_key}\n\n{format_draft(draft)}"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if pending.pop(update.effective_chat.id, None) is None:
        await update.message.reply_text("No draft to cancel.")
        return
    await update.message.reply_text("Draft discarded. Nothing was saved.")


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    body = command_body(update, "convert")
    if not body:
        await update.message.reply_text("Usage: /convert 0001")
        return
    number = body.split()[0]
    try:
        quote = find_by_number("quote", number)
        result = ninja_request(
            "POST",
            "/api/v1/quotes/bulk",
            json_body={"action": "convert", "ids": [quote["id"]]},
        )
        # After convert the quote usually carries invoice_id
        fresh = find_by_number("quote", number)
        invoice_id = fresh.get("invoice_id")
        invoice = None
        if invoice_id:
            invoice = ninja_request(
                "GET",
                f"/api/v1/invoices/{invoice_id}",
                params={"include": "client"},
            ).get("data")
        if not invoice:
            # bulk response may already be the invoice list
            data = result.get("data")
            if isinstance(data, list) and data:
                invoice = data[0]
            elif isinstance(data, dict):
                invoice = data
        if not invoice:
            await update.message.reply_text(
                f"Convert ran for quote {number}. Open Invoice Ninja to confirm the new invoice."
            )
            return
        kind = "invoice"
        await update.message.reply_text(
            f"Quote {number} converted.\n\n{summarise_document(kind, invoice)}"
        )
        try:
            await send_pdf(
                update,
                "invoice",
                invoice,
                f"Invoice {invoice.get('number')} from quote {number}",
            )
        except Exception:
            log.exception("convert pdf")
    except Exception as exc:
        log.exception("convert")
        await update.message.reply_text(f"Convert failed: {exc}")


async def cmd_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    name = command_body(update, "unpaid")
    params: dict[str, Any] = {
        "client_status": "unpaid",
        "per_page": 50,
        "include": "client",
        "sort": "due_date|asc",
    }
    try:
        if name:
            matches = search_clients(name)
            if not matches:
                await update.message.reply_text(f'No client matching "{name}".')
                return
            if len(matches) > 1 and not any(c.name.lower() == name.lower() for c in matches):
                listing = "\n".join(f"• {c.name}" for c in matches[:10])
                await update.message.reply_text("Several clients:\n" + listing)
                return
            chosen = next((c for c in matches if c.name.lower() == name.lower()), matches[0])
            params["client_id"] = chosen.id
            title = f"Unpaid — {chosen.name}"
        else:
            title = "Unpaid — all clients"

        data = ninja_request("GET", "/api/v1/invoices", params=params)
        rows = data.get("data") or []
        if not rows:
            await update.message.reply_text(f"{title}\nNone.")
            return
        lines = [title, ""]
        total = 0.0
        for row in rows:
            client = (row.get("client") or {}).get("name") or "—"
            number = row.get("number") or row.get("id")
            bal = float(row.get("balance") or row.get("amount") or 0)
            total += bal
            lines.append(f"• {number}  {client}  {format_money(bal)}")
        lines += ["", f"TOTAL  {format_money(total)}", "", "Mark paid: /paid 0001"]
        await update.message.reply_text("\n".join(lines)[:4000])
    except Exception as exc:
        log.exception("unpaid")
        await update.message.reply_text(f"Unpaid failed: {exc}")


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    body = command_body(update, "paid")
    if not body:
        await update.message.reply_text("Usage: /paid 0001")
        return
    number = body.split()[0]
    try:
        invoice = find_by_number("invoice", number)
        ninja_request(
            "POST",
            "/api/v1/invoices/bulk",
            json_body={"action": "mark_paid", "ids": [invoice["id"]]},
        )
        await update.message.reply_text(f"Invoice {invoice.get('number', number)} marked paid.")
    except Exception as exc:
        log.exception("paid")
        await update.message.reply_text(f"Could not mark paid: {exc}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not await guard(update):
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
        await update.message.reply_text("No draft. /quote or /invoice, then /item …")
        return
    if not draft.client:
        await update.message.reply_text("Set a client first: /client Name")
        return

    snapshot = draft
    pending.pop(chat_id, None)
    await update.message.reply_text(
        f"Saving {'quote' if snapshot.kind == 'quote' else 'invoice'}…"
    )
    try:
        entity = create_document(snapshot)
        await update.message.reply_text(summarise_document(snapshot.kind, entity))
        await send_pdf(
            update,
            snapshot.kind,
            entity,
            f"Saved {snapshot.kind} {entity.get('number')}",
        )
    except Exception as exc:
        log.exception("save")
        pending[chat_id] = snapshot
        await update.message.reply_text(f"Failed (draft kept): {exc}")


def require_env() -> None:
    missing = [
        name
        for name, value in (
            ("TG_TOKEN", TG_TOKEN),
            ("IN_URL", IN_URL),
            ("IN_TOKEN", IN_TOKEN),
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
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("client", cmd_client))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("quote", cmd_quote))
    app.add_handler(CommandHandler("invoice", cmd_invoice))
    app.add_handler(CommandHandler("item", cmd_item))
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("convert", cmd_convert))
    app.add_handler(CommandHandler("unpaid", cmd_unpaid))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
