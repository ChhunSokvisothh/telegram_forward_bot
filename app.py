import logging
import os
import re
import csv
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.error import Conflict
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
    instructions = (
        "📖 Telegram Metric Tracker Bot\n"
        "───────────────────────────────\n"
        "📊 Commands:\n"
        "• `/01` — SE Sales Summary Engine\n"
        "• `/02` — Cashier & Admin Metrics (Master Group Only)\n"
        "• `/link [Topic_ID]` — Link sales group to a Master Topic thread\n"
        "• `/export` — Download the active ledger CSV with Totals Row (Master Group Only)"
    )
    await update.message.reply_text(instructions, parse_mode="Markdown")

async def link_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standard method to map a group workspace to a Master Topic thread."""
    chat_id = update.effective_chat.id
    if chat_id == MASTER_GROUP_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Specify Topic ID. Example: `/link 01`")
        return
    
    topic_num = context.args[0].strip()
    group_title = update.effective_chat.title or f"Topic {topic_num}"
    
    SALES_MAP[chat_id] = {"topic_id": topic_num, "group_name": group_title}
    await update.message.reply_text(f"✅ Linked '{group_title}' to Master Topic ID: {topic_num}")

# 📊 Restored /01: SE Sales Summary Engine
async def command_01(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/01: Summarizes sales performance grouped by each Sales Executive channel today."""
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"📊 **SE Summary ({today}):**\nNo sales logged yet today.")
        return

    se_data = {} # Format: { salesperson: { usd: 0.0, khr: 0.0, count: 0 } }

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file)
        next(reader, None) # Skip header
        for row in reader:
            if len(row) >= 9:
                se = row[8].strip()
                usd_amt = float(row[3])
                khr_amt = float(row[5])
                
                if se not in se_data:
                    se_data[se] = {"usd": 0.0, "khr": 0.0, "count": 0}
                
                se_data[se]["usd"] += usd_amt
                se_data[se]["khr"] += khr_amt
                se_data[se]["count"] += 1

    report = f"📊 **SE Sales Performance Summary** ({today})\n───────────────────────────\n"
    for se, metrics in se_data.items():
        report += f"👤 **{se}**\n"
        if metrics["usd"] > 0: report += f"  • USD: ${metrics['usd']:.2f}\n"
        if metrics["khr"] > 0: report += f"  • KHR: {metrics['khr']:,.2f}៛\n"
        report += f"  • Total Tx: {metrics['count']} orders\n\n"

    await update.message.reply_text(report, parse_mode="Markdown")

# 🔒 Restored /02: Cashier & Admin Metrics (Strictly Master Group)
async def command_02(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/02: Master Corporate Dashboard showing overall totals across all channels."""
    chat_id = update.effective_chat.id
    if chat_id != MASTER_GROUP_ID:
        await update.message.reply_text("❌ Access Denied: This command is restricted to the Admin Master Group.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if not os.path.exists(filename):
        await update.message.reply_text(f"🔑 **Admin Dashboard ({today}):**\nVault is empty. No revenue data available for today.")
        return

    total_usd = 0.0
    total_khr = 0.0
    usd_count = 0
    khr_count = 0

    with open(filename, mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file)
        next(reader, None) # Skip header
        for row in reader:
            if len(row) >= 7:
                total_usd += float(row[3])
                usd_count += int(row[4])
                total_khr += float(row[5])
                khr_count += int(row[6])

    grand_total_tx = usd_count + khr_count

    dashboard = (
        f"🔑 **Admin Corporate Vault Metrics**\n"
        f"📅 Date: `{today}`\n"
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
            writer.writerow([
                "Date", "Transaction ID", "Customer Name", 
                "USD Total", "USD Transaction Count", 
                "KHR Total", "KHR Transaction Count", 
                "Transaction Count", "Salesperson"
            ])

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

    summary_row = [
        "TOTAL", 
        "", 
        "", 
        total_usd, 
        usd_count, 
        total_khr, 
        khr_count, 
        total_tx, 
        "All Channels Combined"
    ]
    rows_to_export.append(summary_row)

    with open(export_filename, mode='w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerows(rows_to_export)

    with open(export_filename, 'rb') as csv_file:
        await update.message.reply_document(
            document=InputFile(csv_file, filename=export_filename),
            filename=export_filename,
            caption=f"📊 **Daily Ledger Export Success** ({today})\n✨ Includes dynamically generated financial totals line.",
            write_timeout=30
        )

    try:
        os.remove(export_filename)
    except:
        pass

# --- INBOUND TRANSACTION EXTRACTOR LOOP ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"

    if chat_id not in SALES_MAP:
        SALES_MAP[chat_id] = {"topic_id": "1", "group_name": update.effective_chat.title or "Sales Channel"}

    target_topic_id = SALES_MAP[chat_id]["topic_id"]
    handler_name = SALES_MAP[chat_id]["group_name"]
    
    if message.text:
        amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", message.text)
        if amt_match:
            try:
                currency_symbol = amt_match.group(1)
                currency_key = "USD" if currency_symbol == "$" else "KHR"
                amount = float(amt_match.group(2).replace(",", ""))
                
                # 🔍 Bulletproof Transaction ID extraction (skips words like "by", targets digits)
                tx_match = re.search(r"(?:trx|tx|transaction|ref|reference|no|id)(?:\.|\b)(?:\s*id)?[:\s-]+(\d+)", message.text, re.IGNORECASE)
                transaction_id = tx_match.group(1).strip() if tx_match else "Unknown ID"
                
                # 🔍 Precision Customer Name extraction (stops clean before bank details parentheses)
                cust_match = re.search(r"(?:paid\s+by|from|sender|transfer\s+by)[:\s]+([^(\n]+)", message.text, re.IGNORECASE)
                customer_name = cust_match.group(1).strip() if cust_match else "Unknown Customer"
                customer_name = re.sub(r"[*()\-:,\.]", "", customer_name).strip()

                # 💾 1. BACKUP TO TELEGRAM CHANNEL DATABASE
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

                # 📊 2. WRITE LOCALLY TO RUNNING WORKSPACE CSV
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

                logging.info(f"✅ Double-Logged -> {currency_key} {amount:.2f}")

            except Exception as e:
                logging.error(f"Error parsing transaction: {e}")

    try:
        await context.bot.forward_message(
            chat_id=MASTER_GROUP_ID, 
            from_chat_id=chat_id, 
            message_id=message.message_id, 
            message_thread_id=int(target_topic_id)
        )
    except Exception as e:
        logging.error(f"Transmission error: {e}")

# --- POLLING SYSTEM INITIALIZATION ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Core Command Registration
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("link", link_group))
    application.add_handler(CommandHandler("export", export_csv))
    
    # Explicit Structural Operational Bindings
    application.add_handler(CommandHandler("01", command_01))
    application.add_handler(CommandHandler("02", command_02))
    
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
