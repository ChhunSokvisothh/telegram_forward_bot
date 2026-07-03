import logging
import json
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
MASTER_GROUP_ID = int(os.getenv("MASTER_GROUP_ID"))
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID"))
DATA_FILE = "sales_groups.json"

# --- LOCAL DATABASE PERSISTENCE LAYER ---
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        SALES_MAP = {int(k): v for k, v in json.load(f).items()}
else:
    SALES_MAP = {}

def save_mappings():
    with open(DATA_FILE, "w") as f:
        json.dump(SALES_MAP, f, indent=4)

def find_chat_by_topic(topic_id: int):
    """Helper to reverse-lookup a sales group ID using the Master Topic ID."""
    if not topic_id:
        return None
    for chat_id, data in SALES_MAP.items():
        if data.get("topic_id") == topic_id:
            return chat_id
    return None

# --- ERROR HANDLER ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global Error Handler: Sends a direct notification to the Super Admin if the bot fails."""
    logging.error(f"Exception while handling an update: {context.error}")
    error_msg = f"⚠️ **BOT CRITICAL FAILURE ALERT** ⚠️\n\n❌ Error: `{context.error}`"
    try:
        await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=error_msg)
    except Exception as e:
        logging.error(f"Could not send error alert to Admin: {e}")

# --- COMMAND HANDLERS ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help or /start: Displays clear operational instructions for the end users."""
    instructions = (
        "📖 Telegram Metric Tracker Bot - User Guide\n"
        "───────────────────────────────\n"
        "👋 For Frontline Sales Groups:\n"
        "• Use `/01` anytime to instantly see your own channel's revenue total for today.\n\n"
        "🧮 For Cashiers & Admins (In Master Group Forum Topics):\n"
        "• `/link [Topic_ID]` — Run this *inside a Sales Group* to link it to a specific Master thread topic.\n"
        "• `/01` — Run this inside a specific Master Topic thread to view that specific channel's daily performance.\n"
        "• `/02` — Run this in the Master Group to see a cross-channel performance overview summary across all groups.\n"
        "• `/export` — Run this in the Master Group to fetch an Excel-ready CSV sheet containing *only today's detailed transaction rows*."
    )
    await update.message.reply_text(instructions, parse_mode="Markdown")

async def link_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id == MASTER_GROUP_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Specify Topic ID. Example: `/link 5`")
        return
    try:
        topic_id = int(context.args[0])
        group_title = update.effective_chat.title or f"Topic {topic_id}"
        
        if chat_id not in SALES_MAP:
            SALES_MAP[chat_id] = {"topic_id": topic_id, "group_name": group_title, "sales": {}}
        else:
            SALES_MAP[chat_id]["topic_id"] = topic_id
            SALES_MAP[chat_id]["group_name"] = group_title
            
        save_mappings()
        await update.message.reply_text(f"✅ Linked '{group_title}' to Master Topic ID: {topic_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid Topic ID.")

async def command_01(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/01: Summary of Amount by currency, day, and total tx counts."""
    chat_id = update.effective_chat.id
    message = update.effective_message
    today = datetime.now().strftime("%Y-%m-%d")
    
    if chat_id == MASTER_GROUP_ID:
        chat_id = find_chat_by_topic(message.message_thread_id)
        
    if chat_id in SALES_MAP:
        day_data = SALES_MAP[chat_id].get("sales", {}).get(today, {"USD": 0.0, "KHR": 0.0, "tx_count": 0, "transactions": []})
        usd_total = day_data.get("USD", 0.0)
        khr_total = day_data.get("KHR", 0.0)
        tx_count = day_data.get("tx_count", 0)
        
        await update.message.reply_text(
            f"📋 Daily Summary | សរុបការទទួលប្រាក់ [/01]\n"
            f"📅 Date: {today}\n"
            f"🔢 Total Transactions: {tx_count}\n"
            f"💵 USD Total: ${usd_total:,.2f}\n"
            f"🇰🇭 KHR Total: {khr_total:,.0f}៛"
        )
    else:
        await update.message.reply_text("⚠️ Context unlinked or invalid channel thread.")

async def command_02(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/02: Master Group Finance overview breaking down all individual group stats."""
    chat_id = update.effective_chat.id
    if chat_id != MASTER_GROUP_ID:
        return
        
    today = datetime.now().strftime("%Y-%m-%d")
    report_lines = [f"📊 Total Daily Summary | សរុបការទទួលប្រាក់ទាំងអស់ [/02]\n📅 Date: {today}\n" + "—"*20]
    
    grand_usd = 0.0
    grand_khr = 0.0
    for g_id, data in SALES_MAP.items():
        day_data = data.get("sales", {}).get(today, {"USD": 0.0, "KHR": 0.0, "tx_count": 0, "transactions": []})
        usd = day_data.get("USD", 0.0)
        khr = day_data.get("KHR", 0.0)
        grand_usd += usd
        grand_khr += khr
        
        display_name = data.get("group_name", f"Topic {data['topic_id']}")
        report_lines.append(f"🔹 {display_name}: ${usd:,.2f} | {khr:,.0f}៛ ({day_data['tx_count']} tx)")
        
    report_lines.append("—"*20)
    report_lines.append(f"🏆 Grand Total USD: ${grand_usd:,.2f}")
    report_lines.append(f"🏆 Grand Total KHR: {grand_khr:,.0f} ៛")
    await update.message.reply_text("\n".join(report_lines))

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/export: Outputs CURRENT DAY transaction log rows directly into Excel-ready CSV format."""
    chat_id = update.effective_chat.id
    if chat_id != MASTER_GROUP_ID:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"daily_ledger_{today}.csv"
    
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
        
        for g_id, data in SALES_MAP.items():
            handler_name = data.get("group_name", f"Topic {data.get('topic_id', 'Unknown')}")
            
            if today in data.get("sales", {}):
                stats = data["sales"][today]
                if "transactions" in stats and stats["transactions"]:
                    for tx in stats["transactions"]:
                        has_records = True
                        
                        amount = tx.get("amount", 0.0)
                        is_usd = tx.get("currency") == "USD"
                        is_khr = tx.get("currency") == "KHR"
                        
                        usd_count_cell = 1 if is_usd else 0
                        khr_count_cell = 1 if is_khr else 0
                        
                        if is_usd:
                            total_usd_amount += amount
                            total_usd_count += 1
                        if is_khr:
                            total_khr_amount += amount
                            total_khr_count += 1
                        total_tx_count += 1
                        
                        writer.writerow([
                            today,
                            f'="{tx.get("tx_id", "N/A")}"',
                            tx.get("customer_name", "N/A"),
                            amount if is_usd else 0.0,
                            usd_count_cell,
                            amount if is_khr else 0.0,
                            khr_count_cell,
                            1,
                            handler_name
                        ])

        if has_records:
            writer.writerow([]) 
            writer.writerow([
                "TOTAL",
                "",
                "",
                total_usd_amount,
                total_usd_count,
                total_khr_amount,
                total_khr_count,
                total_tx_count,
            ])
        else:
            writer.writerow([today, "No Transactions", "N/A", 0.0, 0, 0.0, 0, 0, "N/A"])
                    
    with open(filename, 'rb') as csv_file:
        await update.message.reply_document(
            document=InputFile(csv_file, filename=filename),
            filename=filename,
            caption=f"📊 **Current Day Data Ledger Export** ({today}) completed successfully.",
            write_timeout=30
        )
        
    os.remove(filename)

# --- TRANSACTION TRACKER ---
async def forward_and_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = update.effective_chat.id
    today = datetime.now().strftime("%Y-%m-%d")

    if chat_id in SALES_MAP:
        target_topic_id = SALES_MAP[chat_id]["topic_id"]
        
        if message.text:
            # 💵 Grabs the amount and symbol ($ or ៛)
            amt_match = re.search(r"([\$៛])\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", message.text)
            if amt_match:
                try:
                    currency_symbol = amt_match.group(1)
                    currency_key = "USD" if currency_symbol == "$" else "KHR"
                    amount = float(amt_match.group(2).replace(",", ""))
                    
                    # 🔍 UPDATED PARSING ANCHORS FOR THE NEW KHQR FORMAT
                    cust_match = re.search(r"paid\s+by\s+(.+?)\s+(?:\(\*|\bon\b)", message.text, re.IGNORECASE)
                    tx_match = re.search(r"Trx\.\s+ID:\s*(\d+)", message.text, re.IGNORECASE)
                    
                    customer_name = cust_match.group(1).strip() if cust_match else "Unknown Customer"
                    transaction_id = tx_match.group(1).strip() if tx_match else "Unknown ID"
                    
                    # Initialize nested dicts if they don't exist
                    if "sales" not in SALES_MAP[chat_id]:
                        SALES_MAP[chat_id]["sales"] = {}
                    if today not in SALES_MAP[chat_id]["sales"]:
                        SALES_MAP[chat_id]["sales"][today] = {
                            "USD": 0.0, "KHR": 0.0, "tx_count": 0, "transactions": []
                        }
                    if "transactions" not in SALES_MAP[chat_id]["sales"][today]:
                        SALES_MAP[chat_id]["sales"][today]["transactions"] = []

                    tx_record = {
                        "tx_id": transaction_id,
                        "customer_name": customer_name,
                        "currency": currency_key,
                        "amount": amount
                    }
                    
                    # Append and update metrics
                    SALES_MAP[chat_id]["sales"][today]["transactions"].append(tx_record)
                    SALES_MAP[chat_id]["sales"][today][currency_key] += amount
                    SALES_MAP[chat_id]["sales"][today]["tx_count"] += 1
                    save_mappings()
                    
                    logging.info(f"✅ Logged {currency_key} {amount:.2f} | ID: {transaction_id} | Cust: {customer_name}")
                except ValueError as e:
                    logging.error(f"Parsing math error: {e}")

        # Forward the receipt to the Master Group topic thread
        try:
            await context.bot.forward_message(
                chat_id=MASTER_GROUP_ID, 
                from_chat_id=chat_id, 
                message_id=message.message_id, 
                message_thread_id=target_topic_id
            )
        except Exception as e:
            logging.error(f"Transmission error: {e}")

# --- SYSTEM INITIALIZATION ENGINE ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("link", link_group))
    application.add_handler(CommandHandler("01", command_01))
    application.add_handler(CommandHandler("02", command_02))
    application.add_handler(CommandHandler("export", export_csv))
    
    application.add_error_handler(error_handler)

    # 🛠️ FIXED FILTER: Broadened filters to listen to modern SUPERGROUPS alongside normal GROUPS
    group_filter = (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.Chat(MASTER_GROUP_ID) & ~filters.COMMAND
    application.add_handler(MessageHandler(group_filter, forward_and_track))

    print("🚀 Corporate v1.2 engine starting up...")

    try:
        application.run_polling(drop_pending_updates=False)
   
    except Conflict:
        print("🚨 CRITICAL: Double deployment! This token is already running in another terminal tab.")
    except NetworkError as e:
        print(f"📡 TELEGRAM SERVER OR NETWORK ALERT: {e}. Attempting recovery in 10s...")
        time.sleep(10)
    except Exception as e:
        print(f"💥 UNEXPECTED SYSTEM CRASH: {e}")

if __name__ == "__main__":
    main()