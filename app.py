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

# --- 3. HELPER FOR SAFE NUMERIC CONVERSION ---
def safe_num(val, val_type=float):
    """Safely converts CSV cell values to float or int without throwing errors."""
    if val is None or str(val).strip() == "":
        return val_type(0)
    try:
        clean_str = str(val).replace(",", "").replace('"', '').strip()
        return val_type(clean_str)
    except ValueError:
        return val_type(0)

# --- 4. INBOUND TRANSACTION ENGINE ---
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

    # Strictly parse transaction messages
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

                # Send log message to database channel
                db_payload = (
                    f"[LEDGER_ROW]\n"
                    f"Date: {today}\n"
                    f"ID: {transaction_id}\n"
                    f"Cust: {customer_name}\n"
                    f"Cur: {currency_key}\n"
                    f"Amt: {amount}\n"
                    f"Channel: {handler_name}"
                )
                await context.bot.send_message(chat_id=DATABASE_CHANNEL_ID, text=db_payload)

                # Write to daily ledger CSV
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

# --- 5. COMMAND HANDLERS ---
async def command_01_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel-Specific Transaction Summary"""
    chat_id = update.effective_chat.id
    config = SALES_MAP.get(chat_id) or SALES_MAP.get(str(chat_id))
    
    mapped_name = config.get("group_name", "").strip() if config else ""
    chat_title = (update.effective_chat.title or "").strip()
    
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"📊 No ledger CSV found for today ({today}).")
        return

    usd_total, usd_count = 0.0, 0
    khr_total, khr_count = 0.0, 0

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        for row in reader:
            raw_sp = row.get("Salesperson", "") or ""
            salesperson = raw_sp.strip().lower()
            
            match_mapped = mapped_name and (salesperson == mapped_name.lower())
            match_title = chat_title and (salesperson == chat_title.lower())

            if match_mapped or match_title:
                usd_total += safe_num(row.get("USD Total"), float)
                usd_count += safe_num(row.get("USD Transaction Count"), int)
                khr_total += safe_num(row.get("KHR Total"), float)
                khr_count += safe_num(row.get("KHR Transaction Count"), int)

    display_name = mapped_name or chat_title or "This Channel"

    summary_msg = (
        f"📊 **Channel Daily Summary: {display_name}**\n"
        f"📅 Date: {today}\n"
        f"----------------------------------------\n"
        f"💵 **USD:** ${usd_total:,.2f} ({usd_count} txs)\n"
        f"៛ **KHR:** ៛{khr_total:,.2f} ({khr_count} txs)\n"
        f"----------------------------------------\n"
        f"📈 Total Transactions: {usd_count + khr_count}"
    )
    await update.message.reply_text(summary_msg, parse_mode="Markdown")

async def command_02_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global Corporate Vault Summary (Admin Restricted)"""
    user_id = update.effective_user.id
    if SUPER_ADMIN_ID and user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"🏛️ **Corporate Vault Summary ({today})**\nNo transactions recorded yet today.")
        return

    usd_total, usd_count = 0.0, 0
    khr_total, khr_count = 0.0, 0

    try:
        with open(filename, mode='r', encoding='utf-8-sig') as file:
            reader = csv.DictReader(file)
            for row in reader:
                usd_total += safe_num(row.get("USD Total"), float)
                usd_count += safe_num(row.get("USD Transaction Count"), int)
                khr_total += safe_num(row.get("KHR Total"), float)
                khr_count += safe_num(row.get("KHR Transaction Count"), int)

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

    except Exception as e:
        logging.error(f"Error executing /02 vault command: {e}")
        await update.message.reply_text("❌ Error reading vault ledger file.")

async def command_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export Daily Ledger CSV Document"""
    target_date = datetime.now().strftime("%Y-%m-%d")
    if context.args:
        target_date = context.args[0].strip()

    filename = f"daily_ledger_{target_date}.csv"
    file_path = os.path.abspath(filename)

    if not os.path.exists(file_path):
        await update.message.reply_text(
            f"📂 No ledger CSV file found for **{target_date}**.",
            parse_mode="Markdown"
        )
        return

    try:
        with open(file_path, "rb") as doc:
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

# --- 6. APPLICATION LAUNCHER ---
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is missing!")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("01", command_01_summary))
    app.add_handler(CommandHandler("02", command_02_vault))
    app.add_handler(CommandHandler(["export", "xport"], command_export))

    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, forward_and_track))

    logging.info("🚀 Persistent Engine Online. Tracking daily transactions...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
