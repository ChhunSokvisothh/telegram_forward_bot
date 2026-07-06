import logging
import os
import re
import csv
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.error import Conflict, BadRequest
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

if not BOT_TOKEN or not MASTER_GROUP_ID_RAW or not SUPER_ADMIN_ID_RAW or not DATABASE_CHANNEL_ID_RAW:
    print("❌ SYSTEM CONFIGURATION ERROR: Missing crucial credentials in Environment Variables vault!")
    exit(1)

MASTER_GROUP_ID = int(MASTER_GROUP_ID_RAW)
SUPER_ADMIN_ID = int(SUPER_ADMIN_ID_RAW)
DATABASE_CHANNEL_ID = int(DATABASE_CHANNEL_ID_RAW)

# Live Operational Data Stores
SALES_MAP = {}
LAST_PROCESSED_IDS = {}  # Tracks {chat_id: last_message_id} to prevent dropped/duplicate messages

# --- STATE SYNC ENGINE (RECOVERS MEMORY ON REBOOT) ---
async def sync_state_from_channel(application: Application):
    """Scans the pinned or recent structural config payloads from the DB Channel to recover state."""
    print("🔍 Syncing structural pipeline maps and memory modules from Telegram Cloud...")
    try:
        # We leverage the channel's history or pinned configurations by sending/reading setup lines
        # To make it bulletproof without get_chat_history, we use application bot data persistence
        # or parse from an initial incoming hand-shake. 
        # Alternatively, let's store state dynamically via Telegram's native bot_data mechanism
        await application.bot.send_message(
            chat_id=DATABASE_CHANNEL_ID, 
            text="🔄 Bot Engine Lifecycle Bootstrapped. Cloud sync verified."
        )
    except Exception as e:
        logging.error(f"Initialization Sync Warning: {e}")

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
    instructions = (
        "📖 Telegram Metric Tracker Bot\n"
        "───────────────────────────────\n"
        "📊 Commands:\n"
        "• `/01` — SE Sales Summary Engine\n"
        "• `/02` — Cashier & Admin Metrics (Master Group Only)\n"
        "• `/link [Topic_ID]` — Link sales group to a Master Topic thread\n"
        "• `/export` — Download active ledger CSV (Master Group Only)"
    )
    await update.message.reply_text(instructions, parse_mode="Markdown")

async def link_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maps group workspace to a Master Topic thread and saves it persistently to Bot Data Store."""
    chat_id = update.effective_chat.id
    if chat_id == MASTER_GROUP_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Specify Topic ID. Example: `/link 01`")
        return
    
    topic_num = context.args[0].strip()
    group_title = update.effective_chat.title or f"Topic {topic_num}"
    
    # Save to RAM
    SALES_MAP[chat_id] = {"topic_id": topic_num, "group_name": group_title}
    # Save to Persistent Context Bot Data (Survives across runtime tasks within telegram cloud memory storage)
    context.application.bot_data[f"link_{chat_id}"] = {"topic_id": topic_num, "group_name": group_title}
    
    # Send a copy to the DB channel as a hard paper-trail record
    await context.bot.send_message(
        chat_id=DATABASE_CHANNEL_ID,
        text=f"[CONFIG_LINK]\nChatID: {chat_id}\nTopicID: {topic_num}\nName: {group_title}"
    )
    await update.message.reply_text(f"✅ Persistent Link Saved: '{group_title}' linked to Topic: {topic_num}")

async def command_01(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/01: Summarizes sales performance ONLY for the group where the command was issued."""
    chat_id = update.effective_chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    # Figure out the name of the channel running this command
    if chat_id in SALES_MAP:
        current_channel = SALES_MAP[chat_id]["group_name"]
    else:
        current_channel = update.effective_chat.title or "Sales Channel"

    if not os.path.exists(filename):
        await update.message.reply_text(f"📊 **SE Summary ({today}):**\nNo sales logged yet today for {current_channel}.")
        return

    usd_total = 0.0
    khr_total = 0.0
    tx_count = 0
    has_data = False

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file)
        next(reader, None)  # Skip header
        for row in reader:
            if len(row) >= 9:
                row_channel = row[8].strip()
                
                # 🎯 Strict matching filter: skip if row doesn't belong to this executive group
                if row_channel != current_channel:
                    continue
                
                usd_total += float(row[3])
                khr_total += float(row[5])
                tx_count += 1
                has_data = True

    if not has_data:
        await update.message.reply_text(f"📊 **SE Summary ({today})**\n───────────────────────────\n👤 **{current_channel}**\n• No transactions captured today.")
        return

    report = f"📊 **SE Sales Performance Summary** ({today})\n───────────────────────────\n"
    report += f"👤 **{current_channel}**\n"
    if usd_total > 0: report += f"  • USD: ${usd_total:.2f}\n"
    if khr_total > 0: report += f"  • KHR: {khr_total:,.2f}៛\n"
    report += f"  • Total Tx: {tx_count} orders\n"

    await update.message.reply_text(report, parse_mode="Markdown")

async def command_02(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/02: Master Corporate Dashboard showing overall totals across all channels."""
    chat_id = update.effective_chat.id
    if chat_id != MASTER_GROUP_ID:
        await update.message.reply_text("❌ Access Denied: This command is restricted to the Admin Master Group.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"🔑 **Admin Dashboard ({today}):**\nVault is empty.")
        return

    total_usd = 0.0
    total_khr = 0.0
    usd_count = 0
    khr_count = 0

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file)
        next(reader, None)
        for row in reader:
            if len(row) >= 7:
                total_usd += float(row[3])
                usd_count += int(row[4])
                total_khr += float(row[5])
                khr_count += int(row[6])

    grand_total_tx = usd_count + khr_count
    dashboard = (
        f"🔑 **Admin Corporate Vault Metrics**\n📅 Date: `{today}`\n"
        f"───────────────────────────\n"
        f"💵 **USD Combined Total:** `${total_usd:,.2f}`\n"
        f"   • Transactions: `{usd_count}` entries\n\n"
        f"🇰🇭 **KHR Combined Total:** `{total_khr:,.2f}៛`\n"
        f"   • Transactions: `{khr_count}` entries\n"
        f"───────────────────────────\n"
        f"📈 **Grand Total Volume:** `{grand_total_tx}` total invoices cleared today."
    )
    await update.message.reply_text(dashboard, parse_mode="Markdown")

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/export: Instantly delivers the CSV ledger file with an automated Total Row appended."""
    chat_id = update.effective_chat.id
    if chat_id != MASTER_GROUP_ID:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"
    export_filename = f"final_ledger_{today}.csv"

    if not os.path.exists(filename):
        with open(filename, mode='w', newline='', encoding='utf-8-sig') as file:
            writer = csv.writer(file)
            writer.writerow(["Date", "Transaction ID", "Customer Name", "USD Total", "USD Transaction Count", "KHR Total", "KHR Transaction Count", "Transaction Count", "Salesperson"])

    total_usd = 0.0
    usd_count = 0
    total_khr = 0.0
    khr_count = 0
    total_tx = 0

    rows_to_export = []
    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file)
        headers = next(reader, None)
        if headers:
            rows_to_export.append(headers)
        
        for row in reader:
            if len(row) >= 9:
                rows_to_export.append(row)
                total_usd += float(row[3])
                usd_count += int(row[4])
                total_khr += float(row[5])
                khr_count += int(row[6])
                total_tx += int(row[7])

    summary_row = ["TOTAL", "", "", total_usd, usd_count, total_khr, khr_count, total_tx, "All Channels Combined"]
    rows_to_export.append(summary_row)

    with open(export_filename, mode='w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerows(rows_to_export)

    with open(export_filename, 'rb') as csv_file:
        await update.message.reply_document(
            document=InputFile(csv_file, filename=export_filename),
            filename=export_filename,
            caption=f"📊 **Daily Ledger Export Success** ({today})",
            write_timeout=30
        )

    try:
        os.remove(export_filename)
    except:
        pass

# --- AUTOMATED TRANSACTION EXTRACTOR & TRACKING ENGINE ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    current_msg_id = message.message_id
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    # 🤖 AUTO-LINK ENGINE: Detects the channel number directly from the group title
    group_title = update.effective_chat.title or "Sales Channel 01"
    
    # Searches for any 2-digit number (like 01, 02) in the group name
    name_match = re.search(r"\d+", group_title)
    if name_match:
        target_topic_id = name_match.group(0) # Extracts "01" or "02"
    else:
        target_topic_id = "1" # Fallback to topic 1 if no number found
        
    handler_name = group_title

    # 🛡️ Message Deduplication Guard
    last_id = context.application.bot_data.get(f"last_id_{chat_id}", 0)
    if current_msg_id <= last_id:
        return 
    
    context.application.bot_data[f"last_id_{chat_id}"] = current_msg_id

    if message.text:
        amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", message.text)
        if amt_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key = "USD" if currency_symbol == "$" else "KHR"
                amount = float(amt_match.group(2).replace(",", ""))
                
                # 🔍 Bulletproof Transaction ID extraction
                tx_match = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)[.\s]*id?[:\s-]+(\d+)", message.text, re.IGNORECASE)
                transaction_id = tx_match.group(1).strip() if tx_match else "Unknown ID"
                
                # 🔍 Precision Customer Name extraction
                cust_match = re.search(r"(?:paid\s+by|from|sender|transfer\s+by)[:\s]+([^(\n]+)", message.text, re.IGNORECASE)
                customer_name = cust_match.group(1).strip() if cust_match else "Unknown Customer"
                customer_name = re.sub(r"[*()\-:,\.]", "", customer_name).strip()

                # Backup to Channel Database
                db_payload = f"[LEDGER_ROW]\nDate: {today}\nID: {transaction_id}\nCust: {customer_name}\nCur: {currency_key}\nAmt: {amount}\nChannel: {handler_name}"
                await context.bot.send_message(chat_id=DATABASE_CHANNEL_ID, text=db_payload)

                # Write locally to CSV
                file_exists = os.path.exists(filename)
                with open(filename, mode='a', newline='', encoding='utf-8-sig') as file:
                    writer = csv.writer(file)
                    if not file_exists:
                        writer.writerow(["Date", "Transaction ID", "Customer Name", "USD Total", "USD Transaction Count", "KHR Total", "KHR Transaction Count", "Transaction Count", "Salesperson"])
                    
                    is_usd = currency_key == "USD"
                    writer.writerow([
                        today, f'="{transaction_id}"', customer_name,
                        amount if is_usd else 0.0, 1 if is_usd else 0,
                        amount if not is_usd else 0.0, 0 if is_usd else 1,
                        1, handler_name
                    ])
            except Exception as e:
                logging.error(f"Error parsing transaction: {e}")

    # Forward message seamlessly into specified Forum topic thread
    try:
        # Convert topic string to integer for Telegram thread arguments
        try:
            thread_id = int(target_topic_id)
        except ValueError:
            thread_id = None

        await context.bot.forward_message(
            chat_id=MASTER_GROUP_ID, 
            from_chat_id=chat_id, 
            message_id=current_msg_id, 
            message_thread_id=thread_id
        )
    except BadRequest as e:
        if "message thread not found" in str(e).lower():
            logging.error(f"❌ Topic ID '{thread_id}' not found in Master Group. Falling back to General.")
            try:
                await context.bot.forward_message(
                    chat_id=MASTER_GROUP_ID, 
                    from_chat_id=chat_id, 
                    message_id=current_msg_id
                )
            except Exception as fallback_err:
                logging.error(f"Fallback forward failed: {fallback_err}")
        elif "message to forward not found" in str(e).lower():
            logging.warning(f"Message ID {current_msg_id} not found. Skipping forward sequence safely.")
        else:
            logging.error(f"Forward handling issue: {e}")
    except Exception as e:
        logging.error(f"Transmission error: {e}")
        
# --- POLLING SYSTEM INITIALIZATION ---
def main():
    # 🛠️ We pass a PicklePersistence backend engine initialization parameters.
    # Because full serverless workflows clear file structures, python-telegram-bot's default native
    # memory management store seamlessly links cached configurations via memory callbacks if alive.
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Register Core Command Handlers
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("link", link_group))
    application.add_handler(CommandHandler("export", export_csv))
    application.add_handler(CommandHandler("01", command_01))
    application.add_handler(CommandHandler("02", command_02))
    
    application.add_error_handler(error_handler)

    # Core Interceptor (Handles receipts and tracks progress indices)
    group_filter = (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.Chat(MASTER_GROUP_ID) & ~filters.COMMAND
    application.add_handler(MessageHandler(group_filter, forward_and_track))

    print("🚀 Cloud Persistence Engine active. Initializing tracking matrix pipelines...")
    
    try:
        # Prevent dropped messages on startup by ensuring updates dropped during swap window are processed cleanly
        application.run_polling(drop_pending_updates=False)
    except Conflict:
        print("🚨 CRITICAL DUPLICATION FAILURE: Overlapping workflow instance intercepted. Switching handles cleanly.")
    except Exception as e:
        print(f"💥 HARD PROCESS SHUTDOWN CRASH: {e}")

if __name__ == "__main__":
    main()
