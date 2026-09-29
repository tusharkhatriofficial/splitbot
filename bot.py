"""Telegram household expenses bot. Python 3.10+. Requires python-telegram-bot, openai, python-dotenv.
Set TELEGRAM_TOKEN and GEMINI_API_KEY in .env beside this file. Optional GEMINI_MODEL.
Database: expenses.db beside this file. Do not run two instances against one Telegram token.
"""
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.WARNING)
TZ = ZoneInfo("Asia/Kolkata")
USERS = {8383783285: "Tushar", 1351361482: "Yogesh", 1237509791: "Divyansh"}
NAMES = list(USERS.values())
CATEGORIES = ["Dairy", "Groceries", "Vegetables", "Gas", "Wifi", "Electricity", "Rent", "Food delivery", "Household", "Transport", "Misc"]
KEYWORDS = {"Dairy": ["milk", "curd", "paneer", "butter", "dahi"],
            "Groceries": ["bread", "atta", "rice", "dal", "grocery", "oil", "sugar", "egg"],
            "Vegetables": ["sabzi", "vegetable", "onion", "potato", "tomato"],
            "Gas": ["gas", "cylinder", "lpg"], "Wifi": ["wifi", "internet", "broadband"],
            "Electricity": ["electricity", "bijli"], "Rent": ["rent"],
            "Food delivery": ["zomato", "swiggy", "pizza"],
            "Household": ["soap", "detergent", "cleaning", "bulb"],
            "Transport": ["uber", "ola", "petrol", "cab"]}
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

if not os.getenv("TELEGRAM_TOKEN"):
    raise RuntimeError("Set TELEGRAM_TOKEN in .env")

db = sqlite3.connect(str(BASE / "data" / "expenses.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS cycles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
 opened_at TEXT NOT NULL, closed_at TEXT, report_name TEXT, report_csv TEXT,
 total_paise INTEGER, transfers_json TEXT,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_cycle ON cycles(chat_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS source_messages (
 chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
 raw TEXT NOT NULL, user_id INTEGER NOT NULL, reply_id INTEGER,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','undone')),
 PRIMARY KEY(chat_id,message_id)
);
CREATE TABLE IF NOT EXISTS expenses_v2 (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
 source_message_id INTEGER NOT NULL, item_index INTEGER NOT NULL,
 payer TEXT NOT NULL, amount_paise INTEGER NOT NULL, category TEXT NOT NULL,
 note TEXT NOT NULL, split_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(chat_id,source_message_id,item_index)
);
CREATE INDEX IF NOT EXISTS expense_cycle_idx ON expenses_v2(chat_id,cycle_id);
CREATE TABLE IF NOT EXISTS pending_close (
 token TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
 requested_by INTEGER NOT NULL, fingerprint TEXT NOT NULL, preview_message_id INTEGER NOT NULL,
 created_at TEXT NOT NULL
);
""")
db.commit()

client = None
if os.getenv("GEMINI_API_KEY"):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["GEMINI_API_KEY"],
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    timeout=15, max_retries=0)


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def money(paise):
    return f"₹{paise / 100:,.2f}"


def paise_of(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Invalid amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 1_000_000:
            raise ValueError("Amount out of range")
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        paise = int(amount * 100)
        if paise < 1:
            raise ValueError("Amount too small")
        return paise
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("Invalid amount") from exc


def shares(paise, people):
    base, extra = divmod(paise, len(people))
    return {person: base + (i < extra) for i, person in enumerate(people)}


def categorize(note):
    for category, words in KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(word)}s?\b", note, re.I) for word in words):
            return category
    return "Misc"


def get_open_cycle(chat_id):
    row = db.execute("SELECT id FROM cycles WHERE chat_id=? AND status='open'", (chat_id,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO cycles(chat_id,opened_at) VALUES(?,?)", (chat_id, now_iso()))
    db.commit()
    return cur.lastrowid


def cycle_rows(chat_id, cycle_id):
    return db.execute("""SELECT e.*, m.raw FROM expenses_v2 e
        JOIN source_messages m ON m.chat_id=e.chat_id AND m.message_id=e.source_message_id
        WHERE e.chat_id=? AND e.cycle_id=? AND m.status='active'
        ORDER BY e.created_at,e.id""", (chat_id, cycle_id)).fetchall()


def fingerprint(rows):
    values = [(r["id"], r["payer"], r["amount_paise"], r["category"],
               r["note"], r["split_json"], r["created_at"]) for r in rows]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def summarize(rows):
    paid = {n: 0 for n in NAMES}
    consumed = {n: 0 for n in NAMES}
    for r in rows:
        paid[r["payer"]] += r["amount_paise"]
        for n, part in shares(r["amount_paise"], json.loads(r["split_json"])).items():
            consumed[n] += part
    net = {n: paid[n] - consumed[n] for n in NAMES}
    debtors = [[n, -net[n]] for n in NAMES if net[n] < 0]
    creditors = [[n, net[n]] for n in NAMES if net[n] > 0]
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i][1], creditors[j][1])
        transfers.append((debtors[i][0], creditors[j][0], amt))
        debtors[i][1] -= amt
        creditors[j][1] -= amt
        if not debtors[i][1]:
            i += 1
        if not creditors[j][1]:
            j += 1
    return paid, consumed, net, transfers


def transfer_text(transfers):
    return "\n".join(f"{a} → {b}: {money(v)}" for a, b, v in transfers) or "No payments needed."


def receipt(rows, edited=False, fallback=False):
    title = "✏️ UPDATED" if edited else "🧾 EXPENSE ADDED"
    total = sum(r["amount_paise"] for r in rows)
    lines = [f"{title} — {len(rows)} item(s), total {money(total)}"]
    if fallback:
        lines.append("⚠️ Gemini could not process this message; basic parser used. Please verify every detail.")
    for r in rows:
        parts = shares(r["amount_paise"], json.loads(r["split_json"]))
        lines += ["", f"#{r['id']} {r['note']} — {money(r['amount_paise'])}",
                  f"Category: {r['category']} | Paid by: {r['payer']}"]
        for n in NAMES:
            if n in parts:
                suffix = " (payer's share)" if n == r["payer"] else f" owes {r['payer']} for this item"
                lines.append(f"• {n}: {money(parts[n])}{suffix}")
        lines.append(f"{r['payer']} should receive {money(r['amount_paise'] - parts.get(r['payer'], 0))} for this item")
    lines += ["", "Use /balance for the net across all purchases; /sheet for the full sheet."]
    return "\n".join(lines)


def fallback_parse(text, sender):
    stripped = text.strip()
    amounts = re.findall(r"(?<![\w.])\d+(?:\.\d{1,2})?(?![\w.])", stripped)
    if len(amounts) != 1:
        return {"intent": "unknown", "reason": "Basic parser requires exactly one price; no expense saved."}
    if not re.search(r"[A-Za-z]", stripped) or len(stripped) > 250:
        return {"intent": "unknown", "reason": "Please use a clear message like '150 milk bread'."}
    if re.search(r"\b(?:balance|sheet|report|history|hisab|hisaab|clear|settle|owe|total)\b", stripped, re.I):
        return {"intent": "unknown", "reason": "Use /balance, /sheet or /clearhisab while AI is down."}
    if re.search(r"\b(?:how|why|when|who|what|where)\b|\?", stripped, re.I):
        return {"intent": "unknown", "reason": "Unsure this is an expense; nothing saved."}
    if re.search(r"\b(?:not|didn't|did not|except|without|exclude|excluding|unequal|percent|%)\b", stripped, re.I):
        return {"intent": "unknown", "reason": "Exclusions/unequal shares need AI. No expense saved; retry later or say 'only Tushar Yogesh'."}
    payer = sender
    for n in NAMES:
        if re.search(rf"\b{n}\s+paid\b", stripped, re.I):
            payer = n
    people = NAMES.copy()
    only = re.search(r"\bonly\s+(.+)$", stripped, re.I)
    if only:
        listed = [n for n in NAMES if re.search(rf"\b{n}\b", only.group(1), re.I)]
        if re.search(r"\bme\b", only.group(1), re.I) and sender not in listed:
            listed.append(sender)
        if not listed:
            return {"intent": "unknown", "reason": "Could not identify people after 'only'."}
        people = [n for n in NAMES if n in listed]
    note = re.sub(r"^\s*(?:₹|rs\.?\s*)?\d+(?:\.\d{1,2})?\s*(?:for\s+)?", "", stripped, flags=re.I).strip() or stripped
    return {"intent": "add_expenses", "items": [{"payer": payer,
        "amount": amounts[0], "category": categorize(note), "note": note,
        "split_among": people}], "fallback": True}


def ai_route(text, sender):
    prompt = f"""You classify one Telegram message in a private household expense group. Sender: {sender}.
Members: {NAMES}. Allowed categories: {CATEGORIES}. Return only a JSON object.
Intents: add_expenses, show_balance, send_sheet, request_close, history, unknown.
For add_expenses, return items array; each item has payer (one member; default sender), amount (INR positive numeric), category (one allowed value), note (short item description), split_among (list of member names; default all three).
IMPORTANT: '150 for milk and 80 for bread' means TWO separate items (150 milk, 80 bread). 'milk and bread for 150' means ONE item. Do not invent amounts or people. If prices cannot reliably be assigned, return unknown with short reason. Explicit uneven shares/percentages are unsupported: return unknown.
If one member did not use it / was excluded / is not included, remove that person from split_among. Example: 'Tushar paid 100 for something but Divyansh did not use it' => payer Tushar, amount 100, split_among Tushar and Yogesh, category Misc, note household item.
'I paid' refers to sender. Handle English and Hindi/Hinglish. Ordinary unrelated chat is unknown. Close/hisab clear means request_close ONLY: the bot must show preview and confirmation; do not claim transfers happened. For show_balance set period 'all', 'week', or 'month' (default all; rolling 7/30 days within current cycle)."""
    result = client.chat.completions.create(
        model=MODEL, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}],
    )
    return json.loads(result.choices[0].message.content)


def validated_action(raw):
    if not isinstance(raw, dict):
        raise ValueError("AI returned non-object")
    intent = raw.get("intent")
    if intent in ("show_balance", "send_sheet", "request_close", "history", "unknown"):
        period = raw.get("period", "all")
        if period not in ("all", "week", "month"):
            period = "all"
        return {"intent": intent, "period": period, "reason": str(raw.get("reason", ""))[:150],
                "fallback": bool(raw.get("fallback", False))}
    if intent != "add_expenses":
        raise ValueError("Unsupported intent")
    items = raw.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 15:
        raise ValueError("Invalid item count")
    checked = []
    for item in items:
        if not isinstance(item, dict) or item.get("payer") not in NAMES:
            raise ValueError("Invalid payer")
        people = item.get("split_among")
        if not isinstance(people, list) or not people or len(people) != len(set(map(str, people))) or any(p not in NAMES for p in people):
            raise ValueError("Invalid split")
        if item.get("category") not in CATEGORIES:
            raise ValueError("Invalid category")
        note = item.get("note")
        if not isinstance(note, str) or not note.strip() or len(note) > 150:
            raise ValueError("Invalid note")
        checked.append({"payer": item["payer"], "paise": paise_of(item.get("amount")),
                        "category": item["category"], "note": note.strip(),
                        "split": [n for n in NAMES if n in people]})
    return {"intent": intent, "items": checked, "fallback": bool(raw.get("fallback", False))}


def safe_literal_action(text):
    t = re.sub(r"\s+", " ", text.strip().lower())
    if t in {"hisab clear", "hisaab clear", "clear hisab", "clear hisaab", "settle hisab"}:
        return {"intent": "request_close"}
    if t in {"hisab sheet", "hisaab sheet", "full hisab", "full hisaab"}:
        return {"intent": "send_sheet"}
    return None


async def route(text, sender):
    literal = safe_literal_action(text)
    if literal:
        return literal
    if client is None:
        raw = fallback_parse(text, sender)
    else:
        try:
            raw = await asyncio.to_thread(ai_route, text, sender)
        except Exception as exc:
            logging.warning("Gemini request failed: %s", type(exc).__name__)
            raw = fallback_parse(text, sender)
            raw["fallback"] = True
    return validated_action(raw)


def report_csv(rows, stamp):
    output = io.StringIO(newline="")
    w = csv.writer(output)
    paid, consumed, net, transfers = summarize(rows)
    w.writerow(["HOUSEHOLD HISAB", "Times are Asia/Kolkata (IST)"])
    w.writerow(["Generated", stamp])
    w.writerow([])
    w.writerow(["Expense ID", "Date/time IST", "Payer", "Amount INR", "Category", "Item", "Split among",
                "Tushar share INR", "Yogesh share INR", "Divyansh share INR", "Original message"])
    for r in rows:
        portions = shares(r["amount_paise"], json.loads(r["split_json"]))
        w.writerow([r["id"], r["created_at"], r["payer"], f"{r['amount_paise']/100:.2f}", r["category"],
                    r["note"], ", ".join(json.loads(r["split_json"])),
                    *(f"{portions.get(n, 0)/100:.2f}" for n in NAMES), r["raw"]])
    w.writerow([])
    w.writerow(["TOTAL", "", "", f"{sum(r['amount_paise'] for r in rows)/100:.2f}", "", "",
                "", *(f"{consumed[n]/100:.2f}" for n in NAMES)])
    w.writerow([])
    w.writerow(["PERSON", "PAID INR", "OWN SHARE INR", "NET INR (positive: receive)"])
    for n in NAMES:
        w.writerow([n, f"{paid[n]/100:.2f}", f"{consumed[n]/100:.2f}", f"{net[n]/100:.2f}"])
    w.writerow([])
    w.writerow(["WHO PAYS WHOM", "AMOUNT INR"])
    for a, b, value in transfers:
        w.writerow([f"{a} pays {b}", f"{value/100:.2f}"])
    w.writerow(["Note", "Closing a cycle is bookkeeping; this bot cannot verify real payments."])
    return output.getvalue()


async def send_csv(message, filename, content, caption):
    data = io.BytesIO(content.encode("utf-8-sig"))
    data.name = filename
    await message.reply_document(document=data, filename=filename, caption=caption)


def allowed(update):
    return update.effective_user is not None and update.effective_user.id in USERS


def current_rows(chat_id):
    cycle = get_open_cycle(chat_id)
    return cycle, cycle_rows(chat_id, cycle)


async def balance(update, period="all"):
    if not allowed(update):
        return
    _, rows = current_rows(update.effective_chat.id)
    if period in ("week", "month"):
        cutoff = datetime.now(timezone.utc) - timedelta(days=7 if period == "week" else 30)
        rows = [r for r in rows if datetime.fromisoformat(r["created_at"]).astimezone(timezone.utc) >= cutoff]
    paid, consumed, net, tx = summarize(rows)
    text = [f"💰 CURRENT HISAB ({period}) — {len(rows)} item(s)",
            f"Total spent: {money(sum(r['amount_paise'] for r in rows))}"]
    for n in NAMES:
        text.append(f"{n}: paid {money(paid[n])} | share {money(consumed[n])} | net {'+' if net[n]>=0 else '-'}{money(abs(net[n]))}")
    text += ["", "Who pays whom:", transfer_text(tx), "", "/sheet: every item and time | /clearhisab: close after payment"]
    await update.effective_message.reply_text("\n".join(text))


async def sheet(update):
    if not allowed(update):
        return
    _, rows = current_rows(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("No expenses in the current cycle yet.")
        return
    name = f"hisab_current_{datetime.now(TZ):%Y%m%d_%H%M%S}.csv"
    await send_csv(update.effective_message, name, report_csv(rows, now_iso()),
                   f"Full current hisab: {len(rows)} items; total {money(sum(r['amount_paise'] for r in rows))}.")


async def history(update):
    if not allowed(update):
        return
    _, rows = current_rows(update.effective_chat.id)
    recent = rows[-10:][::-1]
    text = "\n".join(f"#{r['id']} {r['created_at']} | {r['payer']} paid {money(r['amount_paise'])} | {r['note']}"
                     for r in recent)
    await update.effective_message.reply_text(text or "No current expenses.")


async def request_close(update):
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    cycle, rows = current_rows(chat_id)
    if not rows:
        await update.effective_message.reply_text("Nothing to close; current hisab is empty.")
        return
    token = uuid.uuid4().hex[:16]
    _, _, _, tx = summarize(rows)
    text = (f"🔒 REVIEW BEFORE CLOSING\n{len(rows)} items | total {money(sum(r['amount_paise'] for r in rows))}\n\n"
            f"Who pays whom:\n{transfer_text(tx)}\n\n"
            "Confirm only after real payments are done. The bot will send the full CSV and archive this cycle. "
            "It cannot verify transfers. /sheet shows all details before closing.")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Confirm close", callback_data=f"close:{token}"),
                                    InlineKeyboardButton("Cancel", callback_data=f"cancel:{token}")]])
    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    db.execute("""INSERT INTO pending_close(token,chat_id,cycle_id,requested_by,fingerprint,preview_message_id,created_at)
                  VALUES(?,?,?,?,?,?,?)""",
               (token, chat_id, cycle, update.effective_user.id, fingerprint(rows), sent.message_id, now_iso()))
    db.commit()


async def close_callback(update, ctx):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Only housemates can do this.", show_alert=True)
        return
    token = query.data.split(":", 1)[1]
    pending = db.execute("SELECT * FROM pending_close WHERE token=?", (token,)).fetchone()
    if not pending or pending["chat_id"] != query.message.chat_id or pending["preview_message_id"] != query.message.message_id:
        await query.answer("This button is no longer valid.", show_alert=True)
        return
    if query.data.startswith("cancel:"):
        db.execute("DELETE FROM pending_close WHERE token=?", (token,))
        db.commit()
        await query.answer()
        await query.edit_message_text("Cancelled; current hisab is still open.")
        return
    if update.effective_user.id != pending["requested_by"]:
        await query.answer("The person who requested close must confirm it.", show_alert=True)
        return
    if datetime.now(TZ) - datetime.fromisoformat(pending["created_at"]) > timedelta(minutes=30):
        await query.answer("Preview expired. Send /clearhisab again.", show_alert=True)
        return
    cycle, rows = current_rows(query.message.chat_id)
    if cycle != pending["cycle_id"] or not rows or fingerprint(rows) != pending["fingerprint"]:
        await query.answer("Expenses changed. Send /clearhisab for a fresh preview.", show_alert=True)
        return
    await query.answer("Preparing report…")
    stamp = now_iso()
    csv_text = report_csv(rows, stamp)
    filename = f"hisab_closed_{datetime.now(TZ):%Y%m%d_%H%M%S}_cycle{cycle}.csv"
    try:
        await send_csv(query.message, filename, csv_text, "Closed hisab details. Save this CSV.")
    except Exception as exc:
        logging.error("Report sending failed: %s", type(exc).__name__)
        await query.message.reply_text("Could not send report. Nothing closed; retry the confirmation.")
        return
    _, _, _, tx = summarize(rows)
    total = sum(r["amount_paise"] for r in rows)
    try:
        with db:
            result = db.execute("""UPDATE cycles SET status='closed',closed_at=?,report_name=?,report_csv=?,
                total_paise=?,transfers_json=? WHERE id=? AND chat_id=? AND status='open'""",
                (stamp, filename, csv_text, total, json.dumps(tx), cycle, query.message.chat_id))
            if result.rowcount != 1:
                raise RuntimeError("Cycle already closed")
            db.execute("DELETE FROM pending_close WHERE chat_id=? AND cycle_id=?", (query.message.chat_id, cycle))
    except Exception:
        logging.exception("Archive failed after report delivery")
        await query.message.reply_text("Report sent but archive failed. Check /balance and retry only if still open.")
        return
    try:
        await query.edit_message_text(f"✅ Hisab closed — {len(rows)} items, total {money(total)}.\n"
                                      + transfer_text(tx) + "\nNew expenses start a fresh cycle. /reports retrieves this sheet.")
    except Exception:
        logging.exception("Closed, but could not edit preview")
        await query.message.reply_text("Hisab closed and sheet sent. /reports can retrieve it.")


async def reports(update):
    if not allowed(update):
        return
    rows = db.execute("SELECT id,closed_at,total_paise FROM cycles WHERE chat_id=? AND status='closed' ORDER BY id DESC LIMIT 10",
                      (update.effective_chat.id,)).fetchall()
    text = "\n".join(f"Cycle {r['id']} | {r['closed_at']} | {money(r['total_paise'])} | /report {r['id']}" for r in rows)
    await update.effective_message.reply_text(text or "No closed cycles yet.")


async def report(update, args):
    if not allowed(update):
        return
    if len(args) != 1 or not args[0].isdigit():
        await update.effective_message.reply_text("Use /reports, then /report <cycle number>.")
        return
    r = db.execute("SELECT report_name,report_csv FROM cycles WHERE id=? AND chat_id=? AND status='closed'",
                   (int(args[0]), update.effective_chat.id)).fetchone()
    if not r:
        await update.effective_message.reply_text("No such closed cycle in this chat.")
        return
    await send_csv(update.effective_message, r["report_name"], r["report_csv"], "Past closed hisab.")


async def undo_callback(update, ctx):
    query = update.callback_query
    if not allowed(update):
        await query.answer("Only housemates can undo.", show_alert=True)
        return
    source_id = int(query.data.split(":", 1)[1])
    row = db.execute("SELECT * FROM source_messages WHERE chat_id=? AND message_id=?",
                     (query.message.chat_id, source_id)).fetchone()
    if not row or row["reply_id"] != query.message.message_id or row["status"] != "active":
        await query.answer("This button is no longer active.", show_alert=True)
        return
    cycle = db.execute("SELECT status FROM cycles WHERE id=?", (row["cycle_id"],)).fetchone()
    if not cycle or cycle["status"] != "open":
        await query.answer("Closed cycles cannot be edited.", show_alert=True)
        return
    db.execute("UPDATE source_messages SET status='undone' WHERE chat_id=? AND message_id=?",
               (query.message.chat_id, source_id))
    db.commit()
    await query.answer()
    await query.edit_message_text("❌ Expense message undone (all its items).")


async def save_expenses(update, action, edited=False):
    msg = update.effective_message
    chat_id, source_id = msg.chat_id, msg.message_id
    old = db.execute("SELECT * FROM source_messages WHERE chat_id=? AND message_id=?",
                     (chat_id, source_id)).fetchone()
    if edited and not old:
        return
    if old and old["status"] != "active":
        await msg.reply_text("This message was undone. Send a new expense message instead.")
        return
    if old:
        cycle_state = db.execute("SELECT status FROM cycles WHERE id=?", (old["cycle_id"],)).fetchone()
        if not cycle_state or cycle_state["status"] != "open":
            await msg.reply_text("This hisab was closed. Old entries cannot be changed; add a correction in the new cycle.")
            return
        if old["user_id"] != update.effective_user.id:
            return
        cycle = old["cycle_id"]
    else:
        cycle = get_open_cycle(chat_id)
    timestamp = now_iso()
    items = action["items"]
    with db:
        if old:
            db.execute("DELETE FROM expenses_v2 WHERE chat_id=? AND source_message_id=?", (chat_id, source_id))
            db.execute("UPDATE source_messages SET raw=? WHERE chat_id=? AND message_id=?", (msg.text, chat_id, source_id))
        else:
            db.execute("""INSERT INTO source_messages(chat_id,message_id,cycle_id,raw,user_id)
                          VALUES(?,?,?,?,?)""", (chat_id, source_id, cycle, msg.text, update.effective_user.id))
        for i, item in enumerate(items):
            db.execute("""INSERT INTO expenses_v2(chat_id,cycle_id,source_message_id,item_index,payer,amount_paise,
                          category,note,split_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (chat_id, cycle, source_id, i, item["payer"], item["paise"], item["category"],
                        item["note"], json.dumps(item["split"]), timestamp))
    rows = db.execute("SELECT * FROM expenses_v2 WHERE chat_id=? AND source_message_id=? ORDER BY item_index",
                      (chat_id, source_id)).fetchall()
    response = receipt(rows, edited=edited, fallback=action.get("fallback", False))
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Undo all items", callback_data=f"undo:{source_id}")]])
    if old and old["reply_id"]:
        try:
            await update.get_bot().edit_message_text(chat_id=chat_id, message_id=old["reply_id"],
                                                      text=response, reply_markup=markup)
            return
        except Exception as exc:
            logging.warning("Could not edit old receipt: %s", type(exc).__name__)
    sent = await msg.reply_text(response, reply_markup=markup)
    db.execute("UPDATE source_messages SET reply_id=? WHERE chat_id=? AND message_id=?",
               (sent.message_id, chat_id, source_id))
    db.commit()


async def on_message(update, ctx):
    if not allowed(update):
        return
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if len(msg.text) > 2000:
        if re.search(r"\d|₹|\brs\b", msg.text, re.I):
            await msg.reply_text("Message too long to parse safely; nothing saved. Please split it into smaller expenses.")
        return
    edited = update.edited_message is not None
    old = db.execute("SELECT raw,status,cycle_id FROM source_messages WHERE chat_id=? AND message_id=?",
                     (msg.chat_id, msg.message_id)).fetchone()
    if edited and not old:
        return
    if old and old["raw"] == msg.text:
        return
    if edited and old:
        cycle = db.execute("SELECT status FROM cycles WHERE id=?", (old["cycle_id"],)).fetchone()
        if not cycle or cycle["status"] != "open" or old["status"] != "active":
            await msg.reply_text("This expense is undone or in a closed hisab; edits cannot change it.")
            return
    try:
        action = await route(msg.text, USERS[update.effective_user.id])
    except (ValueError, KeyError, TypeError) as exc:
        logging.warning("Could not validate AI result: %s", type(exc).__name__)
        await msg.reply_text("⚠️ Couldn't safely understand that message; nothing saved. Try a clearer expense or /help.")
        return
    intent = action["intent"]
    if edited:
        if intent != "add_expenses":
            await msg.reply_text("Edited expense wasn't recognized; previous version stays saved. Use Undo and send a new message if needed.")
            return
        await save_expenses(update, action, edited=True)
        return
    if intent == "add_expenses":
        await save_expenses(update, action)
    elif intent == "show_balance":
        await balance(update, action.get("period", "all"))
    elif intent == "send_sheet":
        await sheet(update)
    elif intent == "request_close":
        await request_close(update)
    elif intent == "history":
        await history(update)
    elif action.get("fallback") or client is None:
        if re.search(r"\d|₹|\brs\b|\b(?:hisab|hisaab|balance|sheet|report|clear)\b", msg.text, re.I):
            await msg.reply_text("⚠️ Gemini could not process this message. Nothing saved. "
                                 + (action.get("reason") or "Please try /help."))
    elif re.search(r"\d|₹|\brs\b", msg.text, re.I):
        await msg.reply_text("⚠️ Possible expense not understood. Nothing saved. "
                             + (action.get("reason") or "Try: Tushar paid 100 for household item, only Tushar and Yogesh."))


async def cmd_balance(update, ctx):
    period = ctx.args[0].lower() if ctx.args else "all"
    if period not in ("all", "week", "month"):
        await update.message.reply_text("Use /balance [week|month|all]")
        return
    await balance(update, period)


async def cmd_sheet(update, ctx):
    await sheet(update)


async def cmd_history(update, ctx):
    await history(update)


async def cmd_clear(update, ctx):
    await request_close(update)


async def cmd_reports(update, ctx):
    await reports(update)


async def cmd_report(update, ctx):
    await report(update, ctx.args)


async def help_command(update, ctx):
    if allowed(update):
        await update.message.reply_text(
            "Examples: 150 for milk and 80 for bread; Tushar paid 100 for something, Divyansh did not use it.\n"
            "/balance [week|month] — current hisab\n/sheet — full current CSV\n"
            "/clearhisab — review then confirm close (after payment)\n/history — last 10 items\n"
            "/reports — closed cycles; /report <cycle ID> — old CSV\n"
            "Gemini routes natural language; commands work when AI is offline.")


async def on_error(update, ctx):
    logging.error("Bot handler failed: %s", type(ctx.error).__name__)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong; check /balance before retrying.")
        except Exception:
            pass


if __name__ == "__main__":
    app = ApplicationBuilder().token(os.environ["TELEGRAM_TOKEN"]).concurrent_updates(False).build()
    app.add_handler(CommandHandler(["start", "help"], help_command))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("sheet", cmd_sheet))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("clearhisab", cmd_clear))
    app.add_handler(CommandHandler("reports", cmd_reports))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CallbackQueryHandler(undo_callback, pattern=r"^undo:\d+$"))
    app.add_handler(CallbackQueryHandler(close_callback, pattern=r"^(close|cancel):[a-f0-9]{16}$"))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    app.run_polling()
