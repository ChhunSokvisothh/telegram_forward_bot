import os
import re
import csv
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

# --- 1. INITIALIZATION & LOGGING SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Load local .env if present
load_dotenv()

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
MASTER_GROUP_ID = int(os.getenv("MASTER_GROUP_ID", "0"))
DATABASE_CHANNEL_ID = int(os.getenv("DATABASE_CHANNEL_ID", "0"))
RAW_TOPIC_MAPPINGS = os.getenv("TOPIC_MAPPINGS", "{}")

# --- 2. PARSE TOPIC MAPPINGS ENVIRONMENT VARIABLE ---
SALES_MAP = {}
try:
    # Clean hidden non-breaking spaces or multi-line formatting issues
    cleaned_json_str = RAW_TOPIC_MAPPINGS.replace('\xa0', ' ').strip()
    parsed_json = json.loads(cleaned_json_str)
    
    # Dual-index each group ID (as integer and as string) for foolproof key matching
    for group_id_str, details in parsed_json.items():
        SALES_MAP[group_id_str] = details
        try:
            SALES_MAP[int(group_id_str)] = details
        except ValueError:
            pass

    logging.info(f"✅ Loaded {len(parsed_json)} group-to-topic mappings successfully.")
except Exception as e:
    logging.error(f"❌ Failed to parse TOPIC_MAPPINGS environment variable: {e}")
    logging.error(f"📄 Raw Secret Value Received: '{RAW_TOPIC_MAPPINGS}'")

# --- 3. INBOUND TRANSACTION EXTRACTOR & TRACKING ENGINE ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    current_msg_id = message.message_id
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    # Direct key lookup for channel mapping
    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))
    
    # 🚫 STRICT GUARD: Drop if not explicitly mapped
    if not config or config.get("topic_id") is None:
        logging.warning(f"🛑 Skipping forward: Group ID {chat_id} is not mapped to a valid Topic ID.")
        target_topic_id = None
        handler_name = update.effective_chat.title or "Sales Channel"
    else:
        target_topic_id = config.get("topic_id")
        handler_name = config.get("group_name", update.effective_chat.title or "Sales Channel")
    
    # 🛡️ Message Deduplication Guard
    last_id = context.application.bot_data.get(f"last_id_{chat_id}", 0)
    if current_msg_id <= last_id:
        return 
    context.application.bot_data[f"last_id_{chat_id}"] = current_msg_id

    # --- TRANSACTION EXTRACTION LOGIC ---
    if message.text or message.caption:
        text_content = message.text or message.caption
        
        # 1. Search for Amount ($ or ៛)
        amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", text_content)
        
        # 2. Search for Transaction / Reference ID
        tx_match = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)[.\s]*id?[:\s-]+(\d+)", text_content, re.IGNORECASE)
        
        # 3. Search for Customer / Sender Name
        cust_match = re.search(r"(?:paid\s+by|from|sender|transfer\s+by)[:\s]+([^(\n]+)", text_content, re.IGNORECASE)

        # 🎯 STRICT 3-WAY MATCH: Requires Amount + Transaction ID + Customer Name
        if amt_match and tx_match and cust_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key = "USD" if currency_symbol == "$" else "KHR"
                amount = float(amt_match.group(2).replace(",", ""))
                
                transaction_id = tx_match.group(1).strip()
                
                customer_name = cust_match.group(1).strip()
                customer_name = re.sub(r"[*()\-:,\.]", "", customer_name).strip()

                # Backup entry to Database Channel
                db_payload = f"[LEDGER_ROW]\nDate: {today}\nID: {transaction_id}\nCust: {customer_name}\nCur: {currency_key}\nAmt: {amount}\nChannel: {handler_name}"
                await context.bot.send_message(chat_id=DATABASE_CHANNEL_ID, text=db_payload)

                # Write locally to daily CSV
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
                        today, f'="{transaction_id}"', customer_name,
                        amount if is_usd else 0.0, 1 if is_usd else 0,
                        amount if not is_usd else 0.0, 0 if is_usd else 1,
                        1, handler_name
                    ])
                logging.info(f"✅ Transaction logged: ${amount} | ID: {transaction_id} | Cust: {customer_name}")
            except Exception as e:
                logging.error(f"Error parsing transaction: {e}")
        else:
            if amt_match:
                logging.info(f"ℹ️ Message contains currency symbol ($/៛) but lacks Transaction ID or Customer Name. Skipping CSV log.")

    # 🚀 Forward message ONLY if target_topic_id exists
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
        logging.info(f"➡️ Forwarded message from '{handler_name}' ({chat_id}) to Topic ID: {thread_id}")

    except BadRequest as e:
        logging.error(f"❌ Telegram API Error forwarding to Topic ID {target_topic_id} for '{handler_name}': {e}")
    except Exception as e:
        logging.error(f"Transmission error: {e}")

# --- 4. COMMAND HANDLERS (/01, /02, & /EXPORT) ---
async def command_01_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel-Specific Transaction Summary"""
    chat_id = update.effective_chat.id
    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))
    handler_name = config.get("group_name", "This Channel") if config else "This Channel"
    
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"📊 No transactions logged yet today ({today}) for {handler_name}.")
        return

    usd_total, usd_count = 0.0, 0
    khr_total, khr_count = 0.0, 0

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        for row in reader:
            if row.get("Salesperson") == handler_name:
                usd_total += float(row.get("USD Total", 0))
                usd_count += int(row.get("USD Transaction Count", 0))
                khr_total += float(row.get("KHR Total", 0))
                khr_count += int(row.get("KHR Transaction Count", 0))

    summary_msg = (
        f"📊 **Channel Daily Summary: {handler_name}**\n"
        f"📅 Date: {today}\n"
        f"----------------------------------------\n"
        f"💵 **USD:** ${usd_total:,.2f} ({usd_count} txs)\n"
        f"៛ **KHR:** ៛{khr_total:,.2f} ({khr_count} txs)\n"
        f"----------------------------------------\n"
        f"📈 Total Transactions: {usd_count + khr_count}"
    )
    await update.message.reply_text(summary_msg, parse_mode="Markdown")

async def command_02_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global Corporate Vault Metrics (Admin Restricted)"""
    user_id = update.effective_user.id
    if SUPER_ADMIN_ID and user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"🏛️ **Corporate Vault Summary ({today})**\nNo global transactions recorded yet today.")
        return

    usd_total, usd_count = 0.0, 0
    khr_total, khr_count = 0.0, 0

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        for row in reader:
            usd_total += float(row.get("USD Total", 0))
            usd_count += int(row.get("USD Transaction Count", 0))
            khr_total += float(row.get("KHR Total", 0))
            khr_count += int(row.get("KHR Transaction Count", 0))

    vault_msg = (
        f"🏛️ **Global Corporate Vault Summary**\n"
        f"📅 Date: {today}\n"
        f"----------------------------------------\n"
        f"💵 Total USD Inflow: **${usd_total:,.2f}** ({usd_count} txs)\n"
        f"៛ Total KHR Inflow: **៛{khr_total:,.2f}** ({khr_count} txs)\n"
        f"----------------------------------------\n"
        f"💼 Global Volume: **{usd_count + khr_count} Transactions**"
    )
    await update.message.reply_text(vault_msg, parse_mode="Markdown")

async def command_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export daily ledger CSV document (Usage: /export or /export YYYY-MM-DD)"""
    target_date = datetime.now().strftime("%Y-%m-%d")
    
    # Check if a specific date argument was provided (e.g., /export 2026-07-22)
    if context.args:
        target_date = context.args[0].strip()

    filename = f"daily_ledger_{target_date}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(
            f"📂 No ledger CSV file found for **{target_date}**.\n"
            f"Files are generated automatically when a valid transaction is logged.",
            parse_mode="Markdown"
        )
        return

    try:
        with open(filename, "rb") as doc:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=doc,
                filename=filename,
                caption=f"📄 **Daily Ledger Export**: `{filename}`",
                parse_mode="Markdown"
            )
        logging.info(f"📤 Exported {filename} to chat {update.effective_chat.id}")
    except Exception as e:
        logging.error(f"Failed to export CSV: {e}")
        await update.message.reply_text("❌ Failed to send the ledger CSV file.")

# --- 5. APPLICATION LAUNCHER ---
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is missing!")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("01", command_01_summary))
    app.add_handler(CommandHandler("02", command_02_vault))
    app.add_handler(CommandHandler(["export"], command_export))

    # Inbound Message Processing & Forwarding Handler
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, forward_and_track))

    logging.info("🚀 Persistent Engine Online. Tracking continuous daily transaction frames...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
