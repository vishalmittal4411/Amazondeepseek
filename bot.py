import logging
import re
import requests
import time
import random
from threading import Lock
from bs4 import BeautifulSoup
from psycopg2 import pool
from psycopg2.extras import DictCursor
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from flask import Flask
import threading
import os
import sys

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN or not DATABASE_URL:
    print("Missing environment variables")
    sys.exit(1)

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
        return cls._instance

    def __init__(self):
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

# ================= SCRAPER =================

class AmazonScraper:

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
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
    def fetch_product_data(url):
        html_text = AmazonScraper.fetch_page(url)
        if not html_text:
            return {"stock": "OUT_OF_STOCK", "price": None, "title": None}

        soup = BeautifulSoup(html_text, "lxml")
        page_text = soup.get_text(" ").lower()

        # TITLE
        title = None
        title_tag = soup.find(id="productTitle")
        if title_tag:
            title = title_tag.get_text(strip=True)

        # STOCK
        if "currently unavailable" in page_text or "out of stock" in page_text:
            stock = "OUT_OF_STOCK"
        elif ("add to cart" in page_text
              or "buy now" in page_text
              or "see all buying options" in page_text
              or "1 option from" in page_text):
            stock = "IN_STOCK"
        else:
            stock = "OUT_OF_STOCK"

        # PRICE
        price = None
        price_container = soup.find("span", class_="a-price-whole")
        if price_container:
            whole = price_container.get_text().replace(",", "")
            if whole.isdigit():
                price = int(whole)

        return {"stock": stock, "price": price, "title": title}

# ================= BOT =================

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

def handle_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    db.add_user(user_id, update.effective_chat.id)

    asin = AmazonScraper.extract_asin(update.message.text)
    if not asin:
        update.message.reply_text("❌ Invalid Amazon link.")
        return

    url = f"https://www.amazon.in/dp/{asin}"
    update.message.reply_text("🔍 Fetching product details...")

    data = AmazonScraper.fetch_product_data(url)

    title = data["title"] if data["title"] else f"Product {asin}"
    stock = data["stock"]
    price = data["price"]

    db.add_product(user_id, asin, title, url)

    emoji = "🟢" if stock == "IN_STOCK" else "🔴"
    status_text = "In Stock" if stock == "IN_STOCK" else "Out of Stock"

    if stock == "IN_STOCK" and price:
        price_text = f"₹{price:,}"
    else:
        price_text = "N/A"

    message = (
        "━━━━━━━━━━━━━━\n"
        "✅ *PRODUCT ADDED*\n\n"
        f"📦 *{title[:70]}*\n\n"
        f"{emoji} `{status_text}`\n"
        f"💰 *{price_text}*\n\n"
        f"🔗 [View on Amazon]({url})\n"
        "━━━━━━━━━━━━━━"
    )

    update.message.reply_text(
        message,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

def status_check(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    products = db.get_products(user_id)

    if not products:
        update.message.reply_text("📭 No products added.")
        return

    msg = "📊 *STOCK STATUS*\n\n"

    for index, p in enumerate(products, 1):

        data = AmazonScraper.fetch_product_data(p["url"])
        stock = data["stock"]
        price = data["price"]

        emoji = "🟢" if stock == "IN_STOCK" else "🔴"
        status_text = "In Stock" if stock == "IN_STOCK" else "Out of Stock"

        if stock == "IN_STOCK" and price:
            price_text = f"₹{price:,}"
        else:
            price_text = "N/A"

        title = p["title"]
        if len(title) > 60:
            title = title[:60] + "..."

        msg += (
            f"{index}. 📦 *{title}*\n"
            f"{emoji} `{status_text}`\n"
            f"💰 *{price_text}*\n"
            f"🔗 [View Product]({p['url']})\n\n"
            "━━━━━━━━━━━━━━\n\n"
        )

        db.update_product_status(p['id'], stock)
        time.sleep(1)

    update.message.reply_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

def scheduled_stock_check(context: CallbackContext):
    products = db.get_all_products_with_users()
    if not products:
        return

    for product in products:
        data = AmazonScraper.fetch_product_data(product["url"])
        new_status = data["stock"]
        old_status = product.get("last_status", "OUT_OF_STOCK")

        if old_status == "OUT_OF_STOCK" and new_status == "IN_STOCK":
            context.bot.send_message(
                chat_id=product["chat_id"],
                text=f"🔥 BACK IN STOCK!\n\n🔗 {product['url']}"
            )

        db.update_product_status(product["id"], new_status)
        time.sleep(2)

# ================= MAIN =================

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("status", status_check))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.job_queue.run_repeating(scheduled_stock_check, interval=60, first=30)

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
