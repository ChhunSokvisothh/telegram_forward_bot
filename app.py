import logging
import os
import re
import csv
import json
import threading
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from http.server import SimpleHTTPRequestHandler, HTTPServer


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
TOPIC_MAPPINGS_RAW = os.getenv("TOPIC_MAPPINGS", "{}")

if not BOT_TOKEN or not MASTER_GROUP_ID_RAW or not SUPER_ADMIN_ID_RAW or not DATABASE_CHANNEL_ID_RAW:
    print("❌ SYSTEM CONFIGURATION ERROR: Missing crucial credentials!")
    exit(1)

MASTER_GROUP_ID = int(MASTER_GROUP_ID_RAW)
SUPER_ADMIN_ID = int(SUPER_ADMIN_ID_RAW)
DATABASE_CHANNEL_ID = int(DATABASE_CHANNEL_ID_RAW)

# 🧠 Parse Hardcoded Topic Mappings from Environment Variable
SALES_MAP = {}
try:
    raw_dict = json.loads(TOPIC_MAPPINGS_RAW)
    for k, v in raw_dict.items():
        # Cast group Chat ID and Topic ID strictly to integers for exact matching
        chat_id_int = int(str(k).strip())
        v["topic_id"] = int(v["topic_id"]) if v.get("topic_id") is not None else None
        SALES_MAP[chat_id_int] = v
        
    logging.info(f"✅ Loaded {len(SALES_MAP)} group-to-topic mappings successfully.")
except Exception as e:
    logging.error(f"❌ Failed to parse TOPIC_MAPPINGS environment variable: {e}")

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
        "• `/01` — SE Sales Summary Engine (Works in Cashier Groups & Master Topics)\n"
        "• `/02` — Cashier & Admin Metrics (Master Group Only)\n"
        "• `/export` — Download the active ledger CSV (Master Group Only)"
    )
    await update.message.reply_text(instructions, parse_mode="Markdown")

# 📊 /01: Smart SE Sales Summary Engine
async def command_01(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/01: Summarizes sales performance for current group, or auto-detects channel inside a Master Topic thread."""
    chat_id = update.effective_chat.id
    message = update.effective_message
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"
    
    current_channel = None

    if chat_id == MASTER_GROUP_ID:
        thread_id = message.message_thread_id if message.is_topic_message else None
        if not thread_id:
            await update.message.reply_text("❌ Run this command inside a specific Topic Thread to see its summary.")
            return
            
        for cid, config in SALES_MAP.items():
            if config.get("topic_id") == int(thread_id):
                current_channel = config.get("group_name")
                break
                
        if not current_channel:
            await update.message.reply_text(f"⚠️ This topic thread (ID: `{thread_id}`) is not registered in TOPIC_MAPPINGS.")
            return
    else:
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
        next(reader, None)
        for row in reader:
            if len(row) >= 9:
                if row[8].strip() == current_channel:
                    usd_total += float(row[3])
                    khr_total += float(row[5])
                    tx_count += 1
                    has_data = True

    if not has_data:
        await update.message.reply_text(f"📊 **SE Summary ({today})**\n───────────────────────────\n👤 **{current_channel}**\n• No transactions captured today.")
        return

    report = f"📊 **SE Sales Performance Summary** ({today})\n───────────────────────────\n👤 **{current_channel}**\n"
    if usd_total > 0: report += f"  • USD: ${usd_total:.2f}\n"
    if khr_total > 0: report += f"  • KHR: {khr_total:,.2f}៛\n"
    report += f"  • Total Tx: {tx_count} orders\n"

    await update.message.reply_text(report, parse_mode="Markdown")

# 🔒 /02: Cashier & Admin Metrics (Strictly Master Group)
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
    except Exception:
        pass

# --- INBOUND TRANSACTION EXTRACTOR & TRACKING ENGINE ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    current_msg_id = message.message_id
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    # Match SE group directly against integer keys in SALES_MAP
    config = SALES_MAP.get(chat_id)
    
    if not config:
        logging.warning(f"⚠️ Message received from unmapped Group ID: {chat_id}. Forwarding to General topic.")
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

    if message.text:
        amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", message.text)
        if amt_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key = "USD" if currency_symbol == "$" else "KHR"
                amount = float(amt_match.group(2).replace(",", ""))
                
                tx_match = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)[.\s]*id?[:\s-]+(\d+)", message.text, re.IGNORECASE)
                transaction_id = tx_match.group(1).strip() if tx_match else "Unknown ID"
                
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

    # Forward message using target_topic_id
    try:
        thread_id = int(target_topic_id) if target_topic_id is not None else None
        
        await context.bot.forward_message(
            chat_id=MASTER_GROUP_ID, 
            from_chat_id=chat_id, 
            message_id=current_msg_id, 
            message_thread_id=thread_id
        )
        logging.info(f"➡️ Forwarded message from '{handler_name}' ({chat_id}) to Topic ID: {thread_id}")

    except BadRequest as e:
        if "message thread not found" in str(e).lower() or "thread_id_invalid" in str(e).lower():
            logging.warning(f"⚠️ Channel '{handler_name}' has an invalid Topic ID ({target_topic_id}). Forwarding to General.")
            try:
                await context.bot.forward_message(chat_id=MASTER_GROUP_ID, from_chat_id=chat_id, message_id=current_msg_id)
            except Exception as f_err: 
                logging.error(f"Fallback failure: {f_err}")
        elif "message to forward not found" in str(e).lower():
            logging.warning(f"Message ID {current_msg_id} not found. Skipping safely.")
        else:
            logging.error(f"Forward handling issue: {e}")
    except Exception as e:
        logging.error(f"Transmission error: {e}")

# --- 🔄 LIFECYCLE INITIALIZATION ---
async def post_init(application: Application) -> None:
    """Pre-generates CSV header if missing upon bot startup."""
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"
    
    if not os.path.exists(filename):
        with open(filename, mode='w', newline='', encoding='utf-8-sig') as file:
            writer = csv.writer(file)
            writer.writerow(["Date", "Transaction ID", "Customer Name", "USD Total", "USD Transaction Count", "KHR Total", "KHR Transaction Count", "Transaction Count", "Salesperson"])

def run_fake_web_server():
    """Spins up a tiny HTTP server for web hosts requiring open port checks."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"📡 Keeping Web Host Happy: Listening on port {port}")
    server.serve_forever()

# --- POLLING SYSTEM INITIALIZATION ---
def main():
    threading.Thread(target=run_fake_web_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("export", export_csv))
    application.add_handler(CommandHandler("01", command_01, filters=filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP))
    application.add_handler(CommandHandler("02", command_02))
    
    application.add_error_handler(error_handler)

    group_filter = (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.Chat(MASTER_GROUP_ID) & ~filters.COMMAND
    application.add_handler(MessageHandler(group_filter, forward_and_track))

    print("🚀 Persistent Engine Online. Tracking continuous daily transaction frames...")
    application.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()