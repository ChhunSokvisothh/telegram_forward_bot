import os
import re
import csv
import io
import json
import logging
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- 1. INITIALIZATION & LOGGING ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

load_dotenv()

TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
SUPER_ADMIN_ID       = int(os.getenv("SUPER_ADMIN_ID", "0"))
MASTER_GROUP_ID      = int(os.getenv("MASTER_GROUP_ID", "0"))
DATABASE_CHANNEL_ID  = int(os.getenv("DATABASE_CHANNEL_ID", "0"))
RAW_TOPIC_MAPPINGS   = os.getenv("TOPIC_MAPPINGS", "{}")

# GitHub Gist persistence — add these to your GitHub Actions secrets
GIST_TOKEN   = os.getenv("GIST_TOKEN")    # GitHub PAT with gist scope
GIST_ID      = os.getenv("GIST_ID")       # ID of an existing gist (create one manually first)
GIST_FILE    = "ledger_cache.json"

# --- 2. PARSE TOPIC MAPPINGS ---
SALES_MAP = {}
try:
    cleaned_json_str = RAW_TOPIC_MAPPINGS.replace('\xa0', ' ').strip()
    parsed_json = json.loads(cleaned_json_str)
    for group_id_str, details in parsed_json.items():
        SALES_MAP[group_id_str] = details
        try:
            SALES_MAP[int(group_id_str)] = details
        except ValueError:
            pass
    logging.info(f"✅ Loaded {len(parsed_json)} group mappings.")
except Exception as e:
    logging.error(f"❌ Failed to parse TOPIC_MAPPINGS: {e}")

# --- 3. GIST PERSISTENCE ---
async def gist_load() -> dict:
    """Load db_messages from GitHub Gist on startup."""
    if not GIST_TOKEN or not GIST_ID:
        logging.warning("⚠️ GIST_TOKEN or GIST_ID not set — persistence disabled.")
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"token {GIST_TOKEN}"}
            async with session.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers) as resp:
                data = await resp.json()
                content = data["files"][GIST_FILE]["content"]
                parsed = json.loads(content)
                logging.info(f"📂 Loaded {len(parsed)} records from Gist.")
                return {int(k): v for k, v in parsed.items()}
    except Exception as e:
        logging.error(f"❌ Gist load failed: {e}")
        return {}

async def gist_save(db_messages: dict):
    """Persist db_messages to GitHub Gist."""
    if not GIST_TOKEN or not GIST_ID:
        return
    try:
        payload = {
            "files": {
                GIST_FILE: {
                    "content": json.dumps(
                        {str(k): v for k, v in db_messages.items()},
                        ensure_ascii=False
                    )
                }
            }
        }
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"token {GIST_TOKEN}",
                "Content-Type": "application/json"
            }
            async with session.patch(
                f"https://api.github.com/gists/{GIST_ID}",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status == 200:
                    logging.info(f"💾 Gist saved ({len(db_messages)} records).")
                else:
                    logging.error(f"❌ Gist save failed: {resp.status} {await resp.text()}")
    except Exception as e:
        logging.error(f"❌ Gist save exception: {e}")

# --- 4. STARTUP: HYDRATE FROM GIST ---
async def post_init(application: Application):
    cached = await gist_load()
    application.bot_data["db_messages"] = cached
    logging.info(f"🔄 Startup hydrated {len(cached)} messages from Gist.")

# --- 5. HELPERS ---
def safe_num(val, val_type=float):
    if val is None or str(val).strip() == "":
        return val_type(0)
    try:
        return val_type(str(val).replace(",", "").replace('"', '').strip())
    except ValueError:
        return val_type(0)

def resolve_channel_name(update: Update) -> str | None:
    chat_id = update.effective_chat.id
    message = update.effective_message

    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))
    if config:
        return config.get("group_name", "").strip() or (update.effective_chat.title or "").strip()

    if chat_id == MASTER_GROUP_ID and message and message.message_thread_id:
        thread_id = message.message_thread_id
        for details in SALES_MAP.values():
            if details.get("topic_id") == thread_id:
                return details.get("group_name", "").strip()

    return None

def parse_ledger_row(text: str) -> dict | None:
    try:
        record = {}
        for line in text.strip().splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                record[key.strip()] = val.strip()
        if not {"Date", "ID", "Cust", "Cur", "Amt", "Channel"}.issubset(record.keys()):
            return None
        record["Amt"] = safe_num(record["Amt"], float)
        return record
    except Exception as e:
        logging.error(f"parse_ledger_row error: {e}")
        return None

async def read_database_channel(
    context: ContextTypes.DEFAULT_TYPE,
    target_date: str,
    channel_filter: str | None = None
) -> list[dict]:
    db_messages: dict = context.application.bot_data.get("db_messages", {})
    records = []
    for text in db_messages.values():
        if "[LEDGER_ROW]" not in text:
            continue
        record = parse_ledger_row(text)
        if not record:
            continue
        if record.get("Date") != target_date:
            continue
        if channel_filter and record.get("Channel", "").strip().lower() != channel_filter.strip().lower():
            continue
        records.append(record)
    return records

# --- 6. TRACK DB CHANNEL MESSAGES ---
async def track_db_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    text = message.text or message.caption or ""
    if "[LEDGER_ROW]" not in text:
        return
    db_messages: dict = context.application.bot_data.setdefault("db_messages", {})
    db_messages[message.message_id] = text
    await gist_save(db_messages)
    logging.info(f"📥 Tracked DB message {message.message_id}")

# --- 7. INBOUND TRANSACTION ENGINE ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    current_msg_id = message.message_id
    today = datetime.now().strftime("%Y-%m-%d")

    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))
    if not config or config.get("topic_id") is None:
        target_topic_id = None
        handler_name = (update.effective_chat.title or "Sales Channel").strip()
    else:
        target_topic_id = config.get("topic_id")
        handler_name = config.get("group_name", update.effective_chat.title or "Sales Channel").strip()

    last_id = context.application.bot_data.get(f"last_id_{chat_id}", 0)
    if current_msg_id <= last_id:
        return
    context.application.bot_data[f"last_id_{chat_id}"] = current_msg_id

    if message.text or message.caption:
        text_content = message.text or message.caption
        amt_match  = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", text_content)
        tx_match   = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)[.\s]*id?[:\s-]+(\d+)", text_content, re.IGNORECASE)
        cust_match = re.search(r"(?:paid\s+by|from|sender|transfer\s+by)[:\s]+([^(\n]+)", text_content, re.IGNORECASE)

        if amt_match and tx_match and cust_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key    = "USD" if currency_symbol == "$" else "KHR"
                amount          = float(amt_match.group(2).replace(",", ""))
                transaction_id  = tx_match.group(1).strip()
                customer_name   = re.sub(r"[*()\-:,\.]", "", cust_match.group(1)).strip()

                db_payload = (
                    f"[LEDGER_ROW]\n"
                    f"Date: {today}\n"
                    f"ID: {transaction_id}\n"
                    f"Cust: {customer_name}\n"
                    f"Cur: {currency_key}\n"
                    f"Amt: {amount}\n"
                    f"Channel: {handler_name}"
                )
                sent = await context.bot.send_message(chat_id=DATABASE_CHANNEL_ID, text=db_payload)

                # Store in memory + persist to Gist
                db_messages: dict = context.application.bot_data.setdefault("db_messages", {})
                db_messages[sent.message_id] = db_payload
                await gist_save(db_messages)

                # Local CSV backup
                filename = f"daily_ledger_{today}.csv"
                file_exists = os.path.exists(filename)
                with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow(["Date","Transaction ID","Customer Name",
                                         "USD Total","USD Transaction Count",
                                         "KHR Total","KHR Transaction Count",
                                         "Transaction Count","Salesperson"])
                    is_usd = currency_key == "USD"
                    writer.writerow([today, f'="{transaction_id}"', customer_name,
                                     amount if is_usd else 0.0, 1 if is_usd else 0,
                                     amount if not is_usd else 0.0, 0 if is_usd else 1,
                                     1, handler_name])
                logging.info(f"✅ {currency_key} {amount} | {transaction_id} | {handler_name}")
            except Exception as e:
                logging.error(f"Transaction parse error: {e}")

    if target_topic_id is None:
        return
    try:
        await context.bot.forward_message(
            chat_id=MASTER_GROUP_ID, from_chat_id=chat_id,
            message_id=current_msg_id, message_thread_id=int(target_topic_id)
        )
    except BadRequest as e:
        logging.error(f"❌ Forward error: {e}")
    except Exception as e:
        logging.error(f"Transmission error: {e}")

# --- 8. COMMANDS ---
async def command_01_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If in master group, must be inside a topic thread
    if update.effective_chat.id == MASTER_GROUP_ID and not update.effective_message.message_thread_id:
        await update.message.reply_text("⛔ Use this command inside a topic thread.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    channel_name = resolve_channel_name(update)
    if not channel_name:
        await update.message.reply_text("⚠️ This channel is not mapped.")
        return

    records = await read_database_channel(context, today, channel_filter=channel_name)
    usd_total, usd_count, khr_total, khr_count = 0.0, 0, 0.0, 0
    for r in records:
        if r["Cur"] == "USD":
            usd_total += r["Amt"]; usd_count += 1
        elif r["Cur"] == "KHR":
            khr_total += r["Amt"]; khr_count += 1

    await update.message.reply_text(
        f"📊 *Channel Daily Summary: {channel_name}*\n"
        f"📅 Date: {today}\n"
        f"────────────────────────\n"
        f"💵 *USD:* ${usd_total:,.2f} ({usd_count} txs)\n"
        f"៛ *KHR:* ៛{khr_total:,.0f} ({khr_count} txs)\n"
        f"────────────────────────\n"
        f"📈 Total Transactions: {usd_count + khr_count}",
        parse_mode="Markdown"
    )

async def command_02_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
     if update.effective_chat.id != MASTER_GROUP_ID:
        await update.message.reply_text("⛔ This command is only available in the master group.")
        return
    
    if SUPER_ADMIN_ID and update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    records = await read_database_channel(context, today)
    usd_total, usd_count, khr_total, khr_count = 0.0, 0, 0.0, 0
    by_channel: dict = {}

    for r in records:
        ch = r.get("Channel", "Unknown")
        by_channel.setdefault(ch, {"usd": 0.0, "usd_c": 0, "khr": 0.0, "khr_c": 0})
        if r["Cur"] == "USD":
            usd_total += r["Amt"]; usd_count += 1
            by_channel[ch]["usd"] += r["Amt"]; by_channel[ch]["usd_c"] += 1
        elif r["Cur"] == "KHR":
            khr_total += r["Amt"]; khr_count += 1
            by_channel[ch]["khr"] += r["Amt"]; by_channel[ch]["khr_c"] += 1

    breakdown = "".join(
        f"\n  • *{ch}*: ${d['usd']:,.2f} ({d['usd_c']}tx) | ៛{d['khr']:,.0f} ({d['khr_c']}tx)"
        for ch, d in sorted(by_channel.items())
    )

    await update.message.reply_text(
        f"🏛️ *Global Corporate Vault Summary*\n"
        f"📅 Date: {today}\n"
        f"────────────────────────\n"
        f"💵 Total USD: *${usd_total:,.2f}* ({usd_count} txs)\n"
        f"៛ Total KHR: *៛{khr_total:,.0f}* ({khr_count} txs)\n"
        f"────────────────────────\n"
        f"💼 Global Volume: *{usd_count + khr_count} Transactions*\n"
        f"\n📋 *By Channel:*{breakdown or ' None'}",
        parse_mode="Markdown"
    )

async def command_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Must be called from master group only
    if update.effective_chat.id != MASTER_GROUP_ID:
        await update.message.reply_text("⛔ This command is only available in the master group.")
        return
        
    target_date = context.args[0].strip() if context.args else datetime.now().strftime("%Y-%m-%d")
    records = await read_database_channel(context, target_date)
    if not records:
        await update.message.reply_text(f"📂 No records found for *{target_date}*.", parse_mode="Markdown")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Transaction ID", "Customer Name", "Currency", "Amount", "Channel"])
    for r in records:
        writer.writerow([r["Date"], r["ID"], r["Cust"], r["Cur"], r["Amt"], r["Channel"]])

    output.seek(0)
    filename = f"ledger_export_{target_date}.csv"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=output.getvalue().encode("utf-8-sig"),
        filename=filename,
        caption=f"📄 *Ledger Export*: `{filename}`\n{len(records)} records.",
        parse_mode="Markdown"
    )

# --- 9. LAUNCHER ---
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN missing!")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(MessageHandler(
        filters.Chat(DATABASE_CHANNEL_ID) & (filters.TEXT | filters.CAPTION),
        track_db_message
    ))
    app.add_handler(CommandHandler("01", command_01_summary))
    app.add_handler(CommandHandler("02", command_02_vault))
    app.add_handler(CommandHandler(["export", "xport"], command_export))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, forward_and_track))

    logging.info("🚀 Engine Online.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
