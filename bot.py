import logging
import re
import html
import requests
from threading import Lock
from bs4 import BeautifulSoup
from psycopg2 import pool
from psycopg2.extras import DictCursor
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ================= CONFIG =================
BOT_TOKEN = "YOUR_BOT_TOKEN"
DATABASE_URL = "YOUR_DATABASE_URL"

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
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
        self.pool = pool.SimpleConnectionPool(
            1, 5,
            DATABASE_URL,
            cursor_factory=DictCursor
        )
        self.create_tables()

    def execute(self, query, params=None, fetch_one=False, fetch_all=False):
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                conn.commit()
                if fetch_one:
                    return cur.fetchone()
                if fetch_all:
                    return cur.fetchall()
        finally:
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
        ON CONFLICT (user_id) DO UPDATE SET chat_id = EXCLUDED.chat_id
        """, (user_id, chat_id))

    def add_product(self, user_id, asin, title, url):
        self.execute("""
        INSERT INTO products (user_id, asin, title, url)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, asin) DO UPDATE SET title = EXCLUDED.title
        """, (user_id, asin, title, url))

    def get_products(self, user_id):
        return self.execute(
            "SELECT * FROM products WHERE user_id=%s",
            (user_id,), fetch_all=True
        ) or []

    def remove_product(self, product_id, user_id):
        self.execute(
            "DELETE FROM products WHERE id=%s AND user_id=%s",
            (product_id, user_id)
        )

# ================= AMAZON SCRAPER =================
class AmazonScraper:
    session = requests.Session()
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive"
    }

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
        try:
            r = AmazonScraper.session.get(url, headers=AmazonScraper.HEADERS, timeout=15)
            if r.status_code == 200:
                return r.text
        except:
            pass
        return None

    @staticmethod
    def fetch_title(url, asin):
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            return f"Product {asin}"
        soup = BeautifulSoup(html_text, "lxml")
        tag = soup.find(id="productTitle")
        if tag:
            return html.unescape(tag.get_text(strip=True))
        if soup.title:
            title = soup.title.get_text()
            title = re.sub(r'\s*-+\s*Amazon.*$', '', title, flags=re.IGNORECASE)
            return html.unescape(title.strip())
        return f"Product {asin}"

    @staticmethod
    def check_stock(url):
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            return "UNKNOWN"
        soup = BeautifulSoup(html_text, "lxml")
        page_text = soup.get_text(" ").lower()
        if "currently unavailable" in page_text or "out of stock" in page_text:
            return "OUT_OF_STOCK"
        if soup.find(id="add-to-cart-button") or soup.find(id="buy-now-button"):
            return "IN_STOCK"
        if "see all buying options" in page_text:
            return "IN_STOCK"
        return "UNKNOWN"

    @staticmethod
    def fetch_product_info(asin):
        url = f"https://www.amazon.in/dp/{asin}"
        title = AmazonScraper.fetch_title(url, asin)
        status = AmazonScraper.check_stock(url)
        return {"title": title, "url": url, "status": status}

# ================= BOT LOGIC =================
db = DatabaseManager()

def start(update: Update, context: CallbackContext):
    db.add_user(update.effective_user.id, update.effective_chat.id)
    update.message.reply_text(
        "✅ Amazon Tracker Active\n\n"
        "Commands:\n"
        "/add - Add product\n"
        "/list - Your products\n"
        "/status - Check stock\n"
        "/remove - Remove product"
    )

def list_products(update: Update, context: CallbackContext):
    products = db.get_products(update.effective_user.id)
    if not products:
        update.message.reply_text("No products added.")
        return
    
    msg = "Your Products:\n"
    for i, p in enumerate(products, 1):
        msg += f"\n{i}. {p['title'][:50]}..."
    update.message.reply_text(msg)

def status_check(update: Update, context: CallbackContext):
    products = db.get_products(update.effective_user.id)
    if not products:
        update.message.reply_text("No products added.")
        return
    
    msg = "Stock Status:\n"
    for p in products:
        stock = AmazonScraper.check_stock(p["url"])
        status_icon = "✅" if stock == "IN_STOCK" else "❌"
        msg += f"\n{status_icon} {p['title'][:50]}..."
    update.message.reply_text(msg)

def add(update: Update, context: CallbackContext):
    update.message.reply_text("Send Amazon product link")

def remove(update: Update, context: CallbackContext):
    products = db.get_products(update.effective_user.id)
    if not products:
        update.message.reply_text("No products to remove.")
        return
    
    context.user_data["remove_list"] = products
    msg = "Send number to remove:\n"
    for i, p in enumerate(products, 1):
        msg += f"\n{i}. {p['title'][:50]}..."
    update.message.reply_text(msg)

def handle_message(update: Update, context: CallbackContext):
    try:
        if "remove_list" in context.user_data:
            handle_remove_number(update, context)
            return

        user_id = update.effective_user.id
        db.add_user(user_id, update.effective_chat.id)

        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            update.message.reply_text("❌ Invalid Amazon link")
            return

        update.message.reply_text(f"🔍 Fetching {asin}...")

        info = AmazonScraper.fetch_product_info(asin)
        db.add_product(user_id, asin, info["title"], info["url"])

        status_text = "IN STOCK" if info["status"] == "IN_STOCK" else "OUT OF STOCK"
        update.message.reply_text(
            f"✅ Product Added\n\n"
            f"{info['title'][:200]}\n\n"
            f"Status: {status_text}"
        )
    except Exception as e:
        logger.error(f"Message error: {e}")
        update.message.reply_text("❌ Error")

def handle_remove_number(update: Update, context: CallbackContext):
    try:
        if "remove_list" not in context.user_data:
            return
        
        products = context.user_data["remove_list"]
        text = update.message.text.strip()

        if not text.isdigit():
            update.message.reply_text("Please send a valid number")
            return

        index = int(text) - 1
        if index < 0 or index >= len(products):
            update.message.reply_text("Invalid number")
            return

        product = products[index]
        db.remove_product(product["id"], update.effective_user.id)

        del context.user_data["remove_list"]
        update.message.reply_text("✅ Product removed")
    except Exception as e:
        logger.error(f"Remove error: {e}")
        update.message.reply_text("❌ Error")

# ================= MAIN =================
def main():
    updater = Updater(token=BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("list", list_products))
    dp.add_handler(CommandHandler("status", status_check))
    dp.add_handler(CommandHandler("remove", remove))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    logger.info("🚀 Bot Starting...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
