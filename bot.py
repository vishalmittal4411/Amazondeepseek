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
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram.error import TelegramError, NetworkError, Conflict

# ================= CONFIG =================

BOT_TOKEN = "8545351383:AAEq1ar_OYzsCK8BfYcG9mdL3GKvyt-A8Wc"  # <-- CHANGE KARO
DATABASE_URL = "postgresql://postgres.edmovkglcqbyoichxxjm:NKmehta#61832@aws-1-ap-south-1.pooler.supabase.com:6543/postgres"  # <-- CHANGE KARO

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
        self.init_pool()
        self.create_tables()

    def init_pool(self):
        try:
            self.pool = pool.SimpleConnectionPool(
                1, 5,
                DATABASE_URL,
                cursor_factory=DictCursor
            )
            logger.info("Database pool created")
        except Exception as e:
            logger.error(f"Pool error: {e}")
            time.sleep(5)
            self.init_pool()

    def execute(self, query, params=None, fetch_one=False, fetch_all=False):
        conn = None
        retries = 3
        for attempt in range(retries):
            try:
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
                    self.pool.putconn(conn)
                if attempt == retries - 1:
                    raise
                time.sleep(2 ** attempt)
            finally:
                if conn:
                    self.pool.putconn(conn)

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

    def add_user(self, user_id, chat_id):
        self.execute("""
        INSERT INTO users (user_id, chat_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET chat_id = EXCLUDED.chat_id
        """, (user_id, chat_id))

    def add_product(self, user_id, asin, title, url):
        self.execute("""
        INSERT INTO products (user_id, asin, title, url)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, asin)
        DO UPDATE SET title=EXCLUDED.title
        """, (user_id, asin, title, url))

    def get_products(self, user_id):
        try:
            return self.execute(
                "SELECT * FROM products WHERE user_id=%s ORDER BY id",
                (user_id,), fetch_all=True
            ) or []
        except:
            return []

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
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            return "UNKNOWN"

        try:
            soup = BeautifulSoup(html_text, "lxml")
            page_text = soup.get_text(" ").lower()

            if "currently unavailable" in page_text or "out of stock" in page_text:
                return "OUT_OF_STOCK"

            if soup.find(id="add-to-cart-button"):
                return "IN_STOCK"

            if soup.find(id="buy-now-button"):
                return "IN_STOCK"

            if "see all buying options" in page_text:
                return "IN_STOCK"
        except:
            pass

        return "UNKNOWN"

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
        logger.warning("Conflict error - sleeping...")
        time.sleep(5)
    except NetworkError:
        logger.warning("Network error - sleeping...")
        time.sleep(10)
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")

def start(update: Update, context: CallbackContext):
    try:
        db.add_user(update.effective_user.id, update.effective_chat.id)
        update.message.reply_text("✅ Bot Activated\n\nUse /add to add product.")
    except Exception as e:
        logger.error(f"Start error: {e}")
        update.message.reply_text("❌ Error occurred. Please try again.")

def status(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    try:
        products = db.get_products(user_id)

        if not products:
            update.message.reply_text("No products added.")
            return

        msg = "📦 Product Status:\n\n"
        for p in products:
            stock = AmazonScraper.check_stock(p["url"])
            emoji = "🟢" if stock == "IN_STOCK" else "🔴"
            msg += f"{emoji} {p['title'][:50]}...\n"

        update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Status error: {e}")
        update.message.reply_text("❌ Error checking status.")

def add(update: Update, context: CallbackContext):
    update.message.reply_text("Send Amazon product link.")

def remove(update: Update, context: CallbackContext):
    try:
        products = db.get_products(update.effective_user.id)
        if not products:
            update.message.reply_text("No products to remove.")
            return

        context.user_data["remove_list"] = products
        msg = "Send number to remove:\n\n"
        for i, p in enumerate(products, 1):
            msg += f"{i}. {p['title'][:50]}...\n"

        update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Remove error: {e}")
        update.message.reply_text("❌ Error occurred.")

def handle_message(update: Update, context: CallbackContext):
    try:
        # Pehle check karo removal mode mein hai ya nahi
        if "remove_list" in context.user_data:
            handle_remove_number(update, context)
            return

        user_id = update.effective_user.id
        
        # Ensure user exists in database
        db.add_user(user_id, update.effective_chat.id)

        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            return

        update.message.reply_text("Fetching product info...")

        info = AmazonScraper.fetch_product_info(asin)
        db.add_product(user_id, asin, info["title"], info["url"])

        emoji = "🟢" if info["status"] == "IN_STOCK" else "🔴"
        update.message.reply_text(
            f"✅ Added:\n\n{info['title'][:100]}\n\nCurrent Status: {emoji} {info['status']}"
        )
    except Exception as e:
        logger.error(f"Message error: {e}")
        update.message.reply_text("❌ Error processing request.")

def handle_remove_number(update: Update, context: CallbackContext):
    try:
        products = context.user_data["remove_list"]
        text = update.message.text.strip()

        if not text.isdigit():
            update.message.reply_text("Please send a valid number.")
            return

        index = int(text) - 1
        if index < 0 or index >= len(products):
            update.message.reply_text("Invalid number.")
            return

        product = products[index]
        db.remove_product(product["id"], update.effective_user.id)

        del context.user_data["remove_list"]
        update.message.reply_text("✅ Product removed.")
    except Exception as e:
        logger.error(f"Remove number error: {e}")
        update.message.reply_text("❌ Error removing product.")

# ================= MAIN WITH AUTO-RESTART =================

def run_bot():
    """Bot ko run karne wala function - crash pe restart"""
    while True:
        try:
            updater = Updater(token=BOT_TOKEN, use_context=True)
            dp = updater.dispatcher
            job_queue = updater.job_queue

            # Command handlers
            dp.add_handler(CommandHandler("start", start))
            dp.add_handler(CommandHandler("status", status))
            dp.add_handler(CommandHandler("add", add))
            dp.add_handler(CommandHandler("list", status))
            dp.add_handler(CommandHandler("remove", remove))
            
            # Message handler
            dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
            
            # Error handler
            dp.add_error_handler(error_handler)

            logger.info("🚀 Bot Running...")
            updater.start_polling(drop_pending_updates=True)
            updater.idle()
            
            logger.info("Bot stopped. Restarting in 5 seconds...")
            time.sleep(5)
            
        except Conflict:
            logger.warning("Conflict error - waiting 10 seconds...")
            time.sleep(10)
        except NetworkError:
            logger.warning("Network error - waiting 15 seconds...")
            time.sleep(15)
        except Exception as e:
            logger.error(f"Bot crashed: {e}")
            logger.info("Restarting in 10 seconds...")
            time.sleep(10)

def main():
    logger.info("=" * 50)
    logger.info("Starting Amazon Stock Tracker Bot - FINAL VERSION")
    logger.info("=" * 50)
    
    # Database initialize
    try:
        db.create_tables()
        logger.info("✅ Database ready")
    except Exception as e:
        logger.critical(f"Database error: {e}")
        time.sleep(5)
        main()
    
    # Run bot with auto-restart
    run_bot()

if __name__ == "__main__":
    main()
