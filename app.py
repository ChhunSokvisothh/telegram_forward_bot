import logging
import os
import re
import csv
from datetime import datetime
import time
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.error import NetworkError, Conflict
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# --- CORE LOG HANDLER SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# --- GLOBAL CONFIGURATION ---
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MASTER_GROUP_ID_RAW = os.getenv("MASTER_GROUP_ID")
SUPER_ADMIN_ID_RAW = os.getenv("SUPER_ADMIN_ID")
DATABASE_CHANNEL_ID_RAW = os.getenv("DATABASE_CHANNEL_ID")

# Operational Safeguard Check
if not BOT_TOKEN or not MASTER_GROUP_ID_RAW or not SUPER_ADMIN_ID_RAW or not DATABASE_CHANNEL_ID_RAW:
    print("❌ SYSTEM CONFIGURATION ERROR: Missing crucial credentials in Environment Variables vault!")
    exit(1)

MASTER_GROUP_ID = int(MASTER_GROUP_ID_RAW)
SUPER_ADMIN_ID = int(SUPER_ADMIN_ID_RAW)
DATABASE_CHANNEL_ID = int(DATABASE_CHANNEL_ID_RAW)

# Holds short-term tracking configurations mapping inside live execution memory
if 'SALES_MAP' not in globals():
    SALES_MAP = {}

# --- GLOBAL ERROR HANDLER ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Exception while handling an update: {context.error}")
    error_msg = f"⚠️ **BOT CRITICAL FAILURE ALERT** ⚠️\n\n❌ Error: `{context.error}`"
    try:
        await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=error_msg)
    except Exception as e:
        logging.error(f"Could not send error alert to Admin: {e}")

# --- COMMAND HANDLERS ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help: Displays operational guidance instructions for structural end-users."""
    instructions = (
        "📖 Telegram Metric Tracker Bot - Cloud Edition\n"
        "───────────────────────────────\n"
        "🧮 For Cashiers & Admins:\n"
        "• `/link [Topic_ID]` — Run this *inside a Sales Group* to map it to a specific Master Topic thread.\n"
        "• `/export` — Run this in the Master Group to download an Excel-ready CSV ledger synced directly from the cloud history."
    )
    await update.message.reply_text(instructions, parse_mode="Markdown")

async def link_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Links a specific group chat workspace dynamically to a Master Topic thread."""
    chat_id = update.effective_chat.id
    if chat_id == MASTER_GROUP_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Specify Topic ID. Example: `/link 5`")
        return
    try:
        topic_id = int(context.args[0])
        group_title = update.effective_chat.title or f"Topic {topic_id}"
        
        # Save group configurations mapping locally into tracking runtime memory
        SALES_MAP[chat_id] = {"topic_id": topic_id, "group_name": group_title}
        await update.message.reply_text(f"✅ Linked '{group_title}' to Master Topic ID: {topic_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid Topic ID.")

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/export: Pulls transaction data streams directly out of your private channel database history."""
    chat_id = update.effective_chat.id
    if chat_id != MASTER_GROUP_ID:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"
    await update.message.reply_text("⏳ Syncing real-time records from secure cloud database history... Please hold.")

    total_usd_amount = 0.0
    total_usd_count = 0
    total_khr_amount = 0.0
    total_khr_count = 0
    total_tx_count = 0
    has_records = False

    with open(filename, mode='w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow([
            "Date", "Transaction ID", "Customer Name", 
            "USD Total", "USD Transaction Count", 
            "KHR Total", "KHR Transaction Count", 
            "Transaction Count", "Salesperson"
        ])

        # 🔍 Reads directly backwards from your private channel's messaging history logs
        async for msg in context.bot.get_chat_history(chat_id=DATABASE_CHANNEL_ID, limit=1000):
            if msg.text and "[LEDGER_ROW]" in msg.text:
                lines = msg.text.split("\n")
                tx_date = lines[1].replace("Date: ", "").strip()
                
                # Only match and extract rows belonging to today
                if tx_date != today:
                    continue
                
                has_records = True
                tx_id = lines[2].replace("ID: ", "").strip()
                cust = lines[3].replace("Cust: ", "").strip()
                cur = lines[4].replace("Cur: ", "").strip()
                amt = float(lines[5].replace("Amt: ", "").strip())
                salesperson = lines[6].replace("Channel: ", "").strip()

                is_usd = cur == "USD"
                is_khr = cur == "KHR"
                usd_count_cell = 1 if is_usd else 0
                khr_count_cell = 1 if is_khr else 0

                if is_usd:
                    total_usd_amount += amt
                    total_usd_count += 1
                if is_khr:
                    total_khr_amount += amt
                    total_khr_count += 1
                total_tx_count += 1

                writer.writerow([
                    today, f'="{tx_id}"', cust,
                    amt if is_usd else 0.0, usd_count_cell,
                    amt if is_khr else 0.0, khr_count_cell,
                    1, salesperson
                ])

        if has_records:
            writer.writerow([]) 
            writer.writerow([
                "TOTAL", "", "", 
                total_usd_amount, total_usd_count, 
                total_khr_amount, total_khr_count, 
                total_tx_count, "All Channels"
            ])
        else:
            writer.writerow([today, "No Transactions", "N/A", 0.0, 0, 0.0, 0, 0, "N/A"])

    with open(filename, 'rb') as csv_file:
        await update.message.reply_document(
            document=InputFile(csv_file, filename=filename),
            filename=filename,
            caption=f"📊 **Cloud Ledger Sync Success** ({today}) completed successfully.",
            write_timeout=30
        )
    os.remove(filename)

# --- INBOUND TRANSACTION EXTRACTOR LOOP ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    today = datetime.now().strftime("%Y-%m-%d")

    # Re-instantiate runtime safety defaults if the cloud runner recently restarted
    if chat_id not in SALES_MAP:
        SALES_MAP[chat_id] = {"topic_id": 1, "group_name": update.effective_chat.title or "Sales Channel"}

    target_topic_id = SALES_MAP[chat_id]["topic_id"]
    handler_name = SALES_MAP[chat_id]["group_name"]
    
    if message.text:
        amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", message.text)
        if amt_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key = "USD" if currency_symbol == "$" else "KHR"
                amount = float(amt_match.group(2).replace(",", ""))
                
                tx_match = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)[:\.\s-]+(\w+)", message.text, re.IGNORECASE)
                transaction_id = tx_match.group(1).strip() if tx_match else "Unknown ID"
                
                cust_match = re.search(r"(?:paid\s+by|from|sender|transfer\s+by)[:\s]+(.+?)(?:\s*(?:\(\*|\bon\b|\bat\b|via|vial|\d{2}:\d{2}))", message.text, re.IGNORECASE)
                customer_name = cust_match.group(1).strip() if cust_match else "Unknown Customer"
                customer_name = re.sub(r"[*()\-:,\.]", "", customer_name).strip()

                # 💾 SECURE CLOUD STORAGE WRITE: Send row string directly into the private channel database
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
                logging.info(f"☁️ Streaming record successfully logged -> {currency_key} {amount:.2f}")

            except Exception as e:
                logging.error(f"Error parsing incoming receipt formatting parameters: {e}")

    # Forward the physical notification receipt layout into the Master Forum Group thread
    try:
        await context.bot.forward_message(
            chat_id=MASTER_GROUP_ID, from_chat_id=chat_id, message_id=message.message_id, message_thread_id=target_topic_id
        )
    except Exception as e:
        logging.error(f"Transmission error: {e}")

# --- POLING SYSTEM INITIALIZATION ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("link", link_group))
    application.add_handler(CommandHandler("export", export_csv))
    
    application.add_error_handler(error_handler)

    group_filter = (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.Chat(MASTER_GROUP_ID) & ~filters.COMMAND
    application.add_handler(MessageHandler(group_filter, forward_and_track))

    print("🚀 Corporate Serverless Engine active and monitoring pipelines...")
    try:
        application.run_polling(drop_pending_updates=False)
    except Conflict:
        print("🚨 CRITICAL DUPLICATION FAILURE: Another active session instance is online.")
    except Exception as e:
        print(f"💥 HARD PROCESS SHUTDOWN CRASH: {e}")

if __name__ == "__main__":
    main()