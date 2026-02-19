import logging
import re
import html
import requests
import time
import random
from threading import Lock
from bs4 import BeautifulSoup
from psycopg2 import pool
from psycopg2.extras import DictCursor
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, JobQueue
from telegram.error import TelegramError, NetworkError, Conflict, TimedOut
from flask import Flask
import threading
import os
import sys

# ================= CONFIG FROM ENVIRONMENT =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    print("❌ BOT_TOKEN environment variable not set!")
    sys.exit(1)

if not DATABASE_URL:
    print("❌ DATABASE_URL environment variable not set!")
    sys.exit(1)

# ================= LOGGING =================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= DATABASE =================

class DatabaseManager:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.pool = None
        self.connect_with_retry()

    def connect_with_retry(self):
        max_retries = 10
        for attempt in range(max_retries):
            try:
                self.pool = pool.SimpleConnectionPool(
                    1, 5,
                    DATABASE_URL,
                    cursor_factory=DictCursor
                )
                logger.info("✅ Database pool created")
                self.create_tables()
                self.add_missing_columns()
                return
            except Exception as e:
                logger.error(f"Database connection failed (attempt {attempt+1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    logger.critical("❌ Cannot connect to database. Exiting...")
                    raise
                time.sleep(5 * (attempt + 1))

    def execute(self, query, params=None, fetch_one=False, fetch_all=False):
        conn = None
        retries = 3
        for attempt in range(retries):
            try:
                if not self.pool:
                    self.connect_with_retry()
                conn = self.pool.getconn()
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    conn.commit()
                    if fetch_one:
                        return cur.fetchone()
                    if fetch_all:
                        return cur.fetchall()
                    return None
            except Exception as e:
                logger.error(f"DB error (attempt {attempt+1}): {e}")
                if conn:
                    try:
                        self.pool.putconn(conn)
                    except:
                        pass
                if attempt == retries - 1:
                    try:
                        self.connect_with_retry()
                    except:
                        pass
                    return None if not fetch_one and not fetch_all else []
                time.sleep(2 ** attempt)
            finally:
                if conn:
                    try:
                        self.pool.putconn(conn)
                    except:
                        pass

    def create_tables(self):
        self.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            chat_id BIGINT NOT NULL
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            asin VARCHAR(10),
            title TEXT,
            url TEXT,
            UNIQUE(user_id, asin)
        );
        """)

    def add_missing_columns(self):
        """Add last_status and last_checked columns if they don't exist"""
        try:
            # Check if last_status column exists
            result = self.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='products' AND column_name='last_status'
            """, fetch_all=True)
            
            if not result:
                logger.info("Adding last_status column to products table...")
                self.execute("ALTER TABLE products ADD COLUMN last_status VARCHAR(20) DEFAULT 'OUT_OF_STOCK';")
            
            # Check if last_checked column exists
            result = self.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='products' AND column_name='last_checked'
            """, fetch_all=True)
            
            if not result:
                logger.info("Adding last_checked column to products table...")
                self.execute("ALTER TABLE products ADD COLUMN last_checked TIMESTAMP;")
                
            logger.info("✅ Database columns verified")
        except Exception as e:
            logger.error(f"Error adding columns: {e}")

    def add_user(self, user_id, chat_id):
        self.execute("""
        INSERT INTO users (user_id, chat_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET chat_id = EXCLUDED.chat_id
        """, (user_id, chat_id))

    def add_product(self, user_id, asin, title, url):
        self.execute("""
        INSERT INTO products (user_id, asin, title, url, last_status)
        VALUES (%s, %s, %s, %s, 'OUT_OF_STOCK')
        ON CONFLICT (user_id, asin)
        DO UPDATE SET title = EXCLUDED.title, url = EXCLUDED.url
        """, (user_id, asin, title, url))

    def get_products(self, user_id):
        try:
            return self.execute(
                "SELECT * FROM products WHERE user_id=%s ORDER BY id",
                (user_id,), fetch_all=True
            ) or []
        except:
            return []

    def get_all_products_with_users(self):
        """Sabhi products with user chat_id fetch karo"""
        try:
            return self.execute("""
                SELECT p.*, u.chat_id 
                FROM products p
                JOIN users u ON u.user_id = p.user_id
                ORDER BY p.id
            """, fetch_all=True) or []
        except Exception as e:
            logger.error(f"Error fetching all products: {e}")
            return []

    def update_product_status(self, product_id, status):
        """Product ka status update karo"""
        self.execute("""
            UPDATE products 
            SET last_status = %s, last_checked = NOW() 
            WHERE id = %s
        """, (status, product_id))

    def remove_product(self, product_id, user_id):
        self.execute(
            "DELETE FROM products WHERE id=%s AND user_id=%s",
            (product_id, user_id)
        )

# ================= AMAZON SCRAPER =================

class AmazonScraper:
    
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ]

    @staticmethod
    def extract_asin(text):
        patterns = [
            r"/dp/([A-Z0-9]{10})",
            r"/gp/product/([A-Z0-9]{10})",
            r"/([A-Z0-9]{10})(?:[/?]|$)"
        ]
        text = text.upper()
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def fetch_page(url):
        for attempt in range(3):
            try:
                headers = {
                    "User-Agent": random.choice(AmazonScraper.USER_AGENTS),
                    "Accept-Language": "en-IN,en;q=0.9",
                    "Connection": "keep-alive"
                }
                r = requests.get(url, headers=headers, timeout=15)
                if r.status_code == 200:
                    return r.text
                else:
                    time.sleep(random.uniform(2, 5))
            except:
                time.sleep(random.uniform(1, 3))
        return None

    @staticmethod
    def fetch_title(url, asin):
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            return f"Product {asin}"

        try:
            soup = BeautifulSoup(html_text, "lxml")
            tag = soup.find(id="productTitle")
            if tag:
                return html.unescape(tag.get_text(strip=True))

            if soup.title:
                title = soup.title.get_text()
                title = re.sub(r'\s*-+\s*Amazon.*$', '', title, flags=re.IGNORECASE)
                return html.unescape(title.strip())
        except:
            pass

        return f"Product {asin}"

    @staticmethod
    def check_stock(url):
        """Sirf IN_STOCK aur OUT_OF_STOCK return karega"""
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            # Agar page load nahi hua to OUT_OF_STOCK
            return "OUT_OF_STOCK"

        try:
            soup = BeautifulSoup(html_text, "lxml")
            
            # Check for IN_STOCK indicators
            if soup.find(id="add-to-cart-button"):
                return "IN_STOCK"
            
            if soup.find(id="buy-now-button"):
                return "IN_STOCK"
            
            # Check page text for buying options
            page_text = soup.get_text(" ").lower()
            if "see all buying options" in page_text:
                return "IN_STOCK"
            
            # Check for out of stock indicators
            if "currently unavailable" in page_text or "out of stock" in page_text:
                return "OUT_OF_STOCK"
            
            # Agar kuch bhi match nahi hua to OUT_OF_STOCK
            return "OUT_OF_STOCK"
            
        except Exception as e:
            logger.error(f"Error checking stock: {e}")
            return "OUT_OF_STOCK"

    @staticmethod
    def fetch_product_info(asin):
        url = f"https://www.amazon.in/dp/{asin}"
        title = AmazonScraper.fetch_title(url, asin)
        status = AmazonScraper.check_stock(url)
        return {"title": title, "url": url, "status": status}

# ================= BOT LOGIC =================

db = DatabaseManager()

def error_handler(update: Update, context: CallbackContext):
    try:
        raise context.error
    except Conflict:
        logger.warning("⚠️ Conflict error - 5 sec sleep")
        time.sleep(5)
    except (NetworkError, TimedOut):
        logger.warning("⚠️ Network error - 10 sec sleep")
        time.sleep(10)
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")

def start(update: Update, context: CallbackContext):
    try:
        db.add_user(update.effective_user.id, update.effective_chat.id)
        
        msg = "✅ *Bot Activated*\n\n"
        msg += "Commands:\n"
        msg += "/add ➕ Add product\n"
        msg += "/list 📋 Show products\n"
        msg += "/status 📊 Check stock\n"
        msg += "/remove 🗑 Remove product\n"
        
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Start error: {e}")
        update.message.reply_text("❌ Error occurred. Please try again.")

def list_products(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    try:
        products = db.get_products(user_id)

        if not products:
            update.message.reply_text("📭 *No products added.*", parse_mode=ParseMode.MARKDOWN)
            return

        msg = "📋 *Your Products:*\n\n"
        for i, p in enumerate(products, 1):
            status_emoji = "🟢" if p.get('last_status') == 'IN_STOCK' else "🔴"
            msg += f"{i}. {status_emoji} {p['title'][:50]}...\n"

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"List error: {e}")
        update.message.reply_text("❌ Error fetching list.")

def status_check(update: Update, context: CallbackContext):
    """Products ki stock status check karo with clickable links"""
    user_id = update.effective_user.id
    try:
        products = db.get_products(user_id)

        if not products:
            update.message.reply_text("📭 *No products added.*", parse_mode=ParseMode.MARKDOWN)
            return

        msg = "📊 *Stock Status:*\n\n"
        for p in products:
            stock = AmazonScraper.check_stock(p["url"])
            emoji = "🟢" if stock == "IN_STOCK" else "🔴"
            
            # Chota clickable link - sirf "🔗 Link" dikhega
            msg += f"{emoji} {p['title'][:50]}... [🔗 Link]({p['url']}) - `{stock}`\n"
            
            # Update status in database
            db.update_product_status(p['id'], stock)
            time.sleep(2)

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Status error: {e}")
        update.message.reply_text("❌ Error checking status.")

def add(update: Update, context: CallbackContext):
    update.message.reply_text("🔗 *Send Amazon product link*", parse_mode=ParseMode.MARKDOWN)

def remove(update: Update, context: CallbackContext):
    try:
        products = db.get_products(update.effective_user.id)
        if not products:
            update.message.reply_text("📭 *No products to remove.*", parse_mode=ParseMode.MARKDOWN)
            return

        context.user_data["remove_list"] = products
        msg = "🗑 *Send number to remove:*\n\n"
        for i, p in enumerate(products, 1):
            msg += f"{i}. {p['title'][:50]}...\n"

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Remove error: {e}")
        update.message.reply_text("❌ Error occurred.")

def handle_message(update: Update, context: CallbackContext):
    try:
        if "remove_list" in context.user_data:
            handle_remove_number(update, context)
            return

        user_id = update.effective_user.id
        db.add_user(user_id, update.effective_chat.id)

        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            update.message.reply_text("❌ *Invalid Amazon link*", parse_mode=ParseMode.MARKDOWN)
            return

        update.message.reply_text(f"🔍 Fetching `{asin}`...", parse_mode=ParseMode.MARKDOWN)

        info = AmazonScraper.fetch_product_info(asin)
        db.add_product(user_id, asin, info["title"], info["url"])

        emoji = "🟢" if info["status"] == "IN_STOCK" else "🔴"
        update.message.reply_text(
            f"✅ *Product Added*\n\n"
            f"📦 {info['title'][:100]}\n\n"
            f"📊 Status: {emoji} {info['status']}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Message error: {e}")
        update.message.reply_text("❌ Error processing request.")

def handle_remove_number(update: Update, context: CallbackContext):
    try:
        products = context.user_data["remove_list"]
        text = update.message.text.strip()

        if not text.isdigit():
            update.message.reply_text("❌ *Please send a valid number*", parse_mode=ParseMode.MARKDOWN)
            return

        index = int(text) - 1
        if index < 0 or index >= len(products):
            update.message.reply_text("❌ *Invalid number*", parse_mode=ParseMode.MARKDOWN)
            return

        product = products[index]
        db.remove_product(product["id"], update.effective_user.id)

        del context.user_data["remove_list"]
        update.message.reply_text("✅ *Product removed*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Remove number error: {e}")
        update.message.reply_text("❌ Error removing product.")

# ================= STOCK CHECKER FUNCTION =================
# Sirf IN_STOCK aur OUT_OF_STOCK ke alerts

def scheduled_stock_check(context: CallbackContext):
    """Har 2 minute mein stock check karega"""
    logger.info("🔄 Running scheduled stock check...")
    
    try:
        products = db.get_all_products_with_users()
        
        if not products:
            logger.info("No products to check")
            return
            
        logger.info(f"Checking {len(products)} products")
        
        for product in products:
            try:
                old_status = product.get('last_status', 'OUT_OF_STOCK')
                new_status = AmazonScraper.check_stock(product['url'])
                
                # Status update karo database mein
                db.update_product_status(product['id'], new_status)
                
                # Sirf tab alert jab status actually change hua ho
                if old_status != new_status:
                    
                    # Agar OUT_OF_STOCK se IN_STOCK hua to 10 alerts
                    if old_status == 'OUT_OF_STOCK' and new_status == 'IN_STOCK':
                        logger.info(f"🔥 STOCK ALERT: {product['asin']} is back in stock!")
                        
                        # Pehla alert
                        context.bot.send_message(
                            chat_id=product['chat_id'],
                            text=(
                                f"🔥 *BACK IN STOCK!*\n\n"
                                f"📦 *{product['title']}*\n\n"
                                f"🔗 [View on Amazon]({product['url']})"
                            ),
                            parse_mode=ParseMode.MARKDOWN
                        )
                        
                        # Extra alerts (9 more = total 10)
                        for i in range(9):
                            time.sleep(2)
                            context.bot.send_message(
                                chat_id=product['chat_id'],
                                text=(
                                    f"🔥 *BACK IN STOCK!* (Alert {i+2}/10)\n\n"
                                    f"📦 *{product['title']}*\n\n"
                                    f"🔗 [View on Amazon]({product['url']})"
                                ),
                                parse_mode=ParseMode.MARKDOWN
                            )
                        
                        logger.info(f"✅ Sent 10 alerts for {product['asin']}")
                    
                    # Agar IN_STOCK se OUT_OF_STOCK hua to 1 alert
                    elif old_status == 'IN_STOCK' and new_status == 'OUT_OF_STOCK':
                        logger.info(f"📉 OUT OF STOCK: {product['asin']}")
                        
                        context.bot.send_message(
                            chat_id=product['chat_id'],
                            text=(
                                f"📉 *OUT OF STOCK*\n\n"
                                f"📦 *{product['title']}*\n\n"
                                f"🔗 [View on Amazon]({product['url']})"
                            ),
                            parse_mode=ParseMode.MARKDOWN
                        )
                
                # Random delay to avoid Amazon blocking
                time.sleep(random.randint(5, 10))
                
            except Exception as e:
                logger.error(f"Error checking product {product.get('asin', 'unknown')}: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Stock check error: {e}")

# ================= HEALTH CHECK ENDPOINT =================

health_app = Flask(__name__)

@health_app.route('/')
def home():
    return "Bot is running!", 200

@health_app.route('/health')
def health():
    return "OK", 200

def run_health_server():
    """Health check server alag thread mein chalao"""
    health_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ================= MAIN =================

def main():
    logger.info("=" * 60)
    logger.info("🔥 AMAZON STOCK TRACKER BOT - FINAL VERSION")
    logger.info("✅ IN_STOCK: 10 alerts | OUT_OF_STOCK: 1 alert")
    logger.info("=" * 60)
    
    # Health server start karo
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    logger.info(f"✅ Health server running on port {PORT}")
    
    # Database check
    try:
        db.create_tables()
        logger.info("✅ Database ready")
    except Exception as e:
        logger.critical(f"Database error: {e}")
        time.sleep(5)
        main()
        return
    
    # Bot setup
    updater = Updater(token=BOT_TOKEN, use_context=True)
    
    # Delete webhook to avoid conflicts
    try:
        updater.bot.delete_webhook()
        logger.info("✅ Webhook deleted")
    except:
        pass
    
    dp = updater.dispatcher
    job_queue = updater.job_queue
    
    # Command handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("list", list_products))
    dp.add_handler(CommandHandler("status", status_check))
    dp.add_handler(CommandHandler("remove", remove))
    
    # Message handler
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    
    # Error handler
    dp.add_error_handler(error_handler)
    
    # 🔥 Schedule stock check EVERY 120 SECONDS (2 minutes)
    job_queue.run_repeating(scheduled_stock_check, interval=120, first=10)
    logger.info("✅ Stock checker scheduled (every 120 seconds / 2 minutes)")
    
    # Start bot
    updater.start_polling()
    logger.info("✅ Bot is running!")
    
    # Keep running
    updater.idle()

if __name__ == "__main__":
    main()
