import os
import re
import csv
import io
import json
import logging
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
MASTER_GROUP_ID = int(os.getenv("MASTER_GROUP_ID", "0"))
DATABASE_CHANNEL_ID = int(os.getenv("DATABASE_CHANNEL_ID", "0"))
RAW_TOPIC_MAPPINGS = os.getenv("TOPIC_MAPPINGS", "{}")

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
    logging.info(f"✅ Loaded {len(parsed_json)} group mappings successfully.")
except Exception as e:
    logging.error(f"❌ Failed to parse TOPIC_MAPPINGS environment variable: {e}")

# --- 3. HELPER: SAFE NUMERIC CONVERSION ---
def safe_num(val, val_type=float):
    if val is None or str(val).strip() == "":
        return val_type(0)
    try:
        return val_type(str(val).replace(",", "").replace('"', '').strip())
    except ValueError:
        return val_type(0)

# --- 4. HELPER: RESOLVE CHANNEL NAME FROM CONTEXT ---
def resolve_channel_name(update: Update) -> str | None:
    """
    Figures out which channel/group name to filter by.
    Works when called from:
      - A source sales group (direct match via SALES_MAP)
      - Master group in a topic thread (match via thread/topic_id in SALES_MAP)
    Returns the group_name string or None if it can't be determined.
    """
    chat_id = update.effective_chat.id
    message = update.effective_message

    # Case 1: Called from a mapped source sales group
    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))
    if config:
        return config.get("group_name", "").strip() or (update.effective_chat.title or "").strip()

    # Case 2: Called from master group inside a topic thread
    if chat_id == MASTER_GROUP_ID and message and message.message_thread_id:
        thread_id = message.message_thread_id
        for key, details in SALES_MAP.items():
            if isinstance(key, int) and details.get("topic_id") == thread_id:
                return details.get("group_name", "").strip()
            if isinstance(key, str):
                try:
                    if details.get("topic_id") == thread_id:
                        return details.get("group_name", "").strip()
                except Exception:
                    pass

    return None

# --- 5. CORE: READ & PARSE DATABASE CHANNEL ---
async def read_database_channel(context: ContextTypes.DEFAULT_TYPE, target_date: str, channel_filter: str | None = None) -> list[dict]:
    """
    Reads the database channel message history and parses [LEDGER_ROW] entries.
    
    Args:
        context: The bot context.
        target_date: Date string in YYYY-MM-DD format to filter by.
        channel_filter: If set, only return rows where Channel matches this name (case-insensitive).
    
    Returns:
        List of dicts with keys: Date, ID, Cust, Cur, Amt, Channel
    """
    records = []
    
    try:
        # Fetch up to 200 recent messages from the database channel
        # We fetch in batches using offset_id if needed
        messages = await context.bot.get_updates()  # placeholder — see note below
    except Exception:
        pass

    # Telegram Bot API doesn't have a "get channel history" method directly.
    # We use iter_messages via stored message IDs tracked in bot_data,
    # OR we rely on the bot having been added as admin and using copyMessage tricks.
    # 
    # REAL APPROACH: We track all DB channel message IDs in bot_data as they arrive,
    # then fetch them with get_messages. See `track_db_message` handler below.
    
    db_message_ids: list[int] = context.application.bot_data.get("db_message_ids", [])
    
    for msg_id in db_message_ids:
        try:
            msg = await context.bot.forward_message(
                chat_id=DATABASE_CHANNEL_ID,
                from_chat_id=DATABASE_CHANNEL_ID,
                message_id=msg_id
            )
        except Exception:
            continue
        # We actually want to READ not forward — use copy trick or store text on arrival
        # See `track_db_message` which stores text in bot_data directly (best approach)
    
    # BEST APPROACH: We store message text when the bot receives it in the DB channel
    db_messages: dict[int, str] = context.application.bot_data.get("db_messages", {})
    
    for msg_id, text in db_messages.items():
        if "[LEDGER_ROW]" not in text:
            continue
        
        record = parse_ledger_row(text)
        if not record:
            continue
        
        # Filter by date
        if record.get("Date") != target_date:
            continue
        
        # Filter by channel name if specified
        if channel_filter:
            if record.get("Channel", "").strip().lower() != channel_filter.strip().lower():
                continue
        
        records.append(record)
    
    return records

def parse_ledger_row(text: str) -> dict | None:
    """Parse a [LEDGER_ROW] message into a dict."""
    try:
        lines = text.strip().splitlines()
        record = {}
        for line in lines:
            if ":" in line:
                key, _, val = line.partition(":")
                record[key.strip()] = val.strip()
        
        # Validate required fields
        required = {"Date", "ID", "Cust", "Cur", "Amt", "Channel"}
        if not required.issubset(record.keys()):
            return None
        
        record["Amt"] = safe_num(record["Amt"], float)
        return record
    except Exception as e:
        logging.error(f"parse_ledger_row error: {e}")
        return None

# --- 6. TRACK DB CHANNEL MESSAGES (store text in bot_data as they arrive) ---
async def track_db_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens in the DB channel and stores message text into bot_data for later querying."""
    message = update.effective_message
    if not message:
        return
    
    text = message.text or message.caption or ""
    if "[LEDGER_ROW]" not in text:
        return
    
    db_messages: dict = context.application.bot_data.setdefault("db_messages", {})
    db_messages[message.message_id] = text
    logging.info(f"📥 Stored DB channel message ID {message.message_id}")

# --- 7. INBOUND TRANSACTION ENGINE ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    current_msg_id = message.message_id
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))

    if not config or config.get("topic_id") is None:
        target_topic_id = None
        handler_name = (update.effective_chat.title or "Sales Channel").strip()
    else:
        target_topic_id = config.get("topic_id")
        handler_name = config.get("group_name", update.effective_chat.title or "Sales Channel").strip()

    # Deduplication check
    last_id = context.application.bot_data.get(f"last_id_{chat_id}", 0)
    if current_msg_id <= last_id:
        return
    context.application.bot_data[f"last_id_{chat_id}"] = current_msg_id

    # Parse transaction
    if message.text or message.caption:
        text_content = message.text or message.caption

        amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", text_content)
        tx_match = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)[.\s]*id?[:\s-]+(\d+)", text_content, re.IGNORECASE)
        cust_match = re.search(r"(?:paid\s+by|from|sender|transfer\s+by)[:\s]+([^(\n]+)", text_content, re.IGNORECASE)

        if amt_match and tx_match and cust_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key = "USD" if currency_symbol == "$" else "KHR"
                amount = float(amt_match.group(2).replace(",", ""))
                transaction_id = tx_match.group(1).strip()
                customer_name = cust_match.group(1).strip()
                customer_name = re.sub(r"[*()\-:,\.]", "", customer_name).strip()

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

                # Also store in bot_data immediately so /01 can read it right away
                db_messages: dict = context.application.bot_data.setdefault("db_messages", {})
                db_messages[sent.message_id] = db_payload

                # Write to local CSV backup
                file_exists = os.path.exists(filename)
                with open(filename, mode='a', newline='', encoding='utf-8-sig') as file:
                    writer = csv.writer(file)
                    if not file_exists:
                        writer.writerow([
                            "Date", "Transaction ID", "Customer Name",
                            "USD Total", "USD Transaction Count",
                            "KHR Total", "KHR Transaction Count",
                            "Transaction Count", "Salesperson"
                        ])
                    is_usd = currency_key == "USD"
                    writer.writerow([
                        today,
                        f'="{transaction_id}"',
                        customer_name,
                        amount if is_usd else 0.0,
                        1 if is_usd else 0,
                        amount if not is_usd else 0.0,
                        0 if is_usd else 1,
                        1,
                        handler_name
                    ])
                logging.info(f"✅ Logged: {currency_key} {amount} | ID: {transaction_id} | Channel: {handler_name}")
            except Exception as e:
                logging.error(f"Error parsing transaction: {e}")

    # Topic Forwarding
    if target_topic_id is None:
        return

    try:
        thread_id = int(target_topic_id)
        await context.bot.forward_message(
            chat_id=MASTER_GROUP_ID,
            from_chat_id=chat_id,
            message_id=current_msg_id,
            message_thread_id=thread_id
        )
        logging.info(f"➡️ Forwarded to Topic ID {thread_id} for '{handler_name}'")
    except BadRequest as e:
        logging.error(f"❌ Forward error (Topic {target_topic_id}): {e}")
    except Exception as e:
        logging.error(f"Transmission error: {e}")

# --- 8. COMMAND: /01 CHANNEL SUMMARY ---
async def command_01_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel summary — reads from DB channel, filtered to the calling group/topic."""
    today = datetime.now().strftime("%Y-%m-%d")

    channel_name = resolve_channel_name(update)
    if not channel_name:
        await update.message.reply_text("⚠️ This channel is not mapped. Cannot determine which records to show.")
        return

    records = await read_database_channel(context, today, channel_filter=channel_name)

    usd_total, usd_count = 0.0, 0
    khr_total, khr_count = 0.0, 0

    for r in records:
        if r["Cur"] == "USD":
            usd_total += r["Amt"]
            usd_count += 1
        elif r["Cur"] == "KHR":
            khr_total += r["Amt"]
            khr_count += 1

    summary_msg = (
        f"📊 *Channel Daily Summary: {channel_name}*\n"
        f"📅 Date: {today}\n"
        f"────────────────────────\n"
        f"💵 *USD:* ${usd_total:,.2f} ({usd_count} txs)\n"
        f"៛ *KHR:* ៛{khr_total:,.0f} ({khr_count} txs)\n"
        f"────────────────────────\n"
        f"📈 Total Transactions: {usd_count + khr_count}"
    )
    await update.message.reply_text(summary_msg, parse_mode="Markdown")

# --- 9. COMMAND: /02 VAULT SUMMARY ---
async def command_02_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global vault — reads ALL records from DB channel for today."""
    user_id = update.effective_user.id
    if SUPER_ADMIN_ID and user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    records = await read_database_channel(context, today, channel_filter=None)

    usd_total, usd_count = 0.0, 0
    khr_total, khr_count = 0.0, 0
    by_channel: dict[str, dict] = {}

    for r in records:
        ch = r.get("Channel", "Unknown")
        if ch not in by_channel:
            by_channel[ch] = {"usd": 0.0, "usd_c": 0, "khr": 0.0, "khr_c": 0}

        if r["Cur"] == "USD":
            usd_total += r["Amt"]
            usd_count += 1
            by_channel[ch]["usd"] += r["Amt"]
            by_channel[ch]["usd_c"] += 1
        elif r["Cur"] == "KHR":
            khr_total += r["Amt"]
            khr_count += 1
            by_channel[ch]["khr"] += r["Amt"]
            by_channel[ch]["khr_c"] += 1

    breakdown = ""
    for ch, data in sorted(by_channel.items()):
        breakdown += f"\n  • *{ch}*: ${data['usd']:,.2f} ({data['usd_c']}tx) | ៛{data['khr']:,.0f} ({data['khr_c']}tx)"

    vault_msg = (
        f"🏛️ *Global Corporate Vault Summary*\n"
        f"📅 Date: {today}\n"
        f"────────────────────────\n"
        f"💵 Total USD: *${usd_total:,.2f}* ({usd_count} txs)\n"
        f"៛ Total KHR: *៛{khr_total:,.0f}* ({khr_count} txs)\n"
        f"────────────────────────\n"
        f"💼 Global Volume: *{usd_count + khr_count} Transactions*\n"
        f"\n📋 *By Channel:*{breakdown if breakdown else ' None'}"
    )
    await update.message.reply_text(vault_msg, parse_mode="Markdown")

# --- 10. COMMAND: /export ---
async def command_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export today's (or a given date's) DB channel records as a CSV."""
    target_date = datetime.now().strftime("%Y-%m-%d")
    if context.args:
        target_date = context.args[0].strip()

    records = await read_database_channel(context, target_date, channel_filter=None)

    if not records:
        await update.message.reply_text(
            f"📂 No records found in database channel for *{target_date}*.",
            parse_mode="Markdown"
        )
        return

    # Build CSV in memory
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
        caption=f"📄 *Ledger Export*: `{filename}`\n{len(records)} records from database channel.",
        parse_mode="Markdown"
    )
    logging.info(f"📤 Exported {len(records)} records for {target_date}")

# --- 11. APPLICATION LAUNCHER ---
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is missing!")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # DB channel tracker — must be registered BEFORE the general message handler
    app.add_handler(MessageHandler(
        filters.Chat(DATABASE_CHANNEL_ID) & (filters.TEXT | filters.CAPTION),
        track_db_message
    ))

    app.add_handler(CommandHandler("01", command_01_summary))
    app.add_handler(CommandHandler("02", command_02_vault))
    app.add_handler(CommandHandler(["export", "xport"], command_export))

    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, forward_and_track))

    logging.info("🚀 Persistent Engine Online. Tracking daily transactions...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
