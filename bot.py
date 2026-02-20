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
from telegram.error import TelegramError, NetworkError, Conflict, TimedOut
from flask import Flask
import threading
import os
import sys

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN or not DATABASE_URL:
    print("Environment variables missing!")
    sys.exit(1)

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
            last_status VARCHAR(20) DEFAULT 'OUT_OF_STOCK',
            last_checked TIMESTAMP,
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
        DO UPDATE SET title = EXCLUDED.title, url = EXCLUDED.url
        """, (user_id, asin, title, url))

    def get_products(self, user_id):
        return self.execute(
            "SELECT * FROM products WHERE user_id=%s ORDER BY id",
            (user_id,), fetch_all=True
        ) or []

    def get_all_products_with_users(self):
        return self.execute("""
            SELECT p.*, u.chat_id
            FROM products p
            JOIN users u ON u.user_id = p.user_id
        """, fetch_all=True) or []

    def update_product_status(self, product_id, status):
        self.execute("""
            UPDATE products
            SET last_status=%s, last_checked=NOW()
            WHERE id=%s
        """, (status, product_id))

db = DatabaseManager()

# ================= AMAZON SCRAPER =================

class AmazonScraper:

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
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
        headers = {
            "User-Agent": random.choice(AmazonScraper.USER_AGENTS),
            "Accept-Language": "en-IN,en;q=0.9"
        }
        try:
            r = requests.get(url, headers=headers, timeout=15)
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
        return f"Product {asin}"

    @staticmethod
    def check_stock(url):
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            return "OUT_OF_STOCK"

        soup = BeautifulSoup(html_text, "lxml")
        page_text = soup.get_text(" ").lower()

        if "currently unavailable" in page_text:
            return "OUT_OF_STOCK"

        if "out of stock" in page_text:
            return "OUT_OF_STOCK"

        if "add to cart" in page_text:
            return "IN_STOCK"

        if "buy now" in page_text:
            return "IN_STOCK"

        if "see all buying options" in page_text:
            return "IN_STOCK"

        if "1 option from" in page_text:
            return "IN_STOCK"

        return "OUT_OF_STOCK"

# ================= BOT COMMANDS =================

def start(update: Update, context: CallbackContext):
    db.add_user(update.effective_user.id, update.effective_chat.id)
    update.message.reply_text(
        "✅ Bot Activated\n\n"
        "/add - Add product\n"
        "/status - Check stock\n"
        "/remove - Remove product"
    )

def add(update: Update, context: CallbackContext):
    update.message.reply_text("Send Amazon product link.")

def status_check(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    products = db.get_products(user_id)

    if not products:
        update.message.reply_text("No products added.")
        return

    msg = "📊 *STOCK STATUS*\n\n"

    for index, p in enumerate(products, 1):
        stock = AmazonScraper.check_stock(p["url"])

        if stock == "IN_STOCK":
            emoji = "🟢"
            status_text = "In Stock"
        else:
            emoji = "🟠"
            status_text = "Out of Stock"

        title = p["title"]
        if len(title) > 60:
            title = title[:60] + "..."

        msg += (
            f"{index}. 📦 *{title}*\n"
            f"{emoji} `{status_text}`\n"
            f"🔗 [View Product]({p['url']})\n\n"
            f"──────────────\n\n"
        )

        db.update_product_status(p['id'], stock)
        time.sleep(1)

    update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

def handle_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    db.add_user(user_id, update.effective_chat.id)

    asin = AmazonScraper.extract_asin(update.message.text)
    if not asin:
        update.message.reply_text("Invalid Amazon link.")
        return

    url = f"https://www.amazon.in/dp/{asin}"
    title = AmazonScraper.fetch_title(url, asin)

    db.add_product(user_id, asin, title, url)

    update.message.reply_text(f"Product added:\n{title}")

# ================= HEALTH SERVER =================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot running", 200

def run_health():
    app.run(host="0.0.0.0", port=PORT)

# ================= MAIN =================

def main():
    threading.Thread(target=run_health, daemon=True).start()

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("status", status_check))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
