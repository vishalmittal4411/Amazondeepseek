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
                self.add_price_columns()
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

    def add_price_columns(self):
        try:
            result = self.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='products' AND column_name='current_price'
            """, fetch_all=True)
            
            if not result:
                logger.info("💰 Adding price columns...")
                self.execute("ALTER TABLE products ADD COLUMN current_price DECIMAL(10,2) DEFAULT 0;")
                self.execute("ALTER TABLE products ADD COLUMN last_price DECIMAL(10,2) DEFAULT 0;")
                self.execute("ALTER TABLE products ADD COLUMN currency VARCHAR(10) DEFAULT '₹';")
                logger.info("✅ Price columns added")
        except Exception as e:
            logger.error(f"Error adding price columns: {e}")

    def add_missing_columns(self):
        try:
            result = self.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='products' AND column_name='last_status'
            """, fetch_all=True)
            
            if not result:
                logger.info("Adding last_status column...")
                self.execute("ALTER TABLE products ADD COLUMN last_status VARCHAR(20) DEFAULT 'UNKNOWN';")
            
            result = self.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='products' AND column_name='last_checked'
            """, fetch_all=True)
            
            if not result:
                logger.info("Adding last_checked column...")
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
        VALUES (%s, %s, %s, %s, 'UNKNOWN')
        ON CONFLICT (user_id, asin)
        DO UPDATE SET title = EXCLUDED.title, url = EXCLUDED.url
        """, (user_id, asin, title, url))

    def update_product_price(self, product_id, new_price):
        product = self.execute("SELECT current_price FROM products WHERE id = %s", (product_id,), fetch_one=True)
        old_price = product['current_price'] if product else 0
        
        self.execute("""
            UPDATE products 
            SET last_price = %s, current_price = %s, last_checked = NOW() 
            WHERE id = %s
        """, (old_price, new_price, product_id))
        
        return old_price

    def get_products(self, user_id):
        try:
            return self.execute(
                "SELECT * FROM products WHERE user_id=%s ORDER BY id",
                (user_id,), fetch_all=True
            ) or []
        except:
            return []

    def get_all_products_with_users(self):
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
    def extract_price(url, html_text=None):
        if not html_text:
            html_text = AmazonScraper.fetch_page(url)
            if not html_text:
                return 0
        
        try:
            soup = BeautifulSoup(html_text, "lxml")
            
            price_selectors = [
                'span.a-price-whole',
                'span.a-price span.a-offscreen',
                'span.priceBlockBuyingPriceString',
                '.a-price .a-offscreen',
                '#priceblock_dealprice',
                '#priceblock_ourprice'
            ]
            
            for selector in price_selectors:
                price_elem = soup.select_one(selector)
                if price_elem:
                    price_text = price_elem.get_text(strip=True)
                    price_match = re.search(r'[\d,]+', price_text)
                    if price_match:
                        price_str = price_match.group().replace(',', '')
                        try:
                            return float(price_str)
                        except:
                            pass
            
            page_text = soup.get_text()
            price_patterns = [
                r'₹\s*([\d,]+(?:\.\d{2})?)',
                r'Rs\.?\s*([\d,]+(?:\.\d{2})?)'
            ]
            
            for pattern in price_patterns:
                match = re.search(pattern, page_text)
                if match:
                    price_str = match.group(1).replace(',', '')
                    try:
                        return float(price_str)
                    except:
                        pass
                        
        except Exception as e:
            logger.error(f"Price extraction error: {e}")
        
        return 0

    @staticmethod
    def check_stock(url, html_text=None):
        if not html_text:
            html_text = AmazonScraper.fetch_page(url)
            if not html_text:
                return "UNKNOWN"

        try:
            soup = BeautifulSoup(html_text, "lxml")
            page_text = soup.get_text(" ").lower()

            if "currently unavailable" in page_text or "out of stock" in page_text:
                return "OUT_OF_STOCK"

            if soup.find(id="add-to-cart-button") or soup.find(id="buy-now-button"):
                return "IN_STOCK"

            if "see all buying options" in page_text:
                return "IN_STOCK"
        except:
            pass

        return "UNKNOWN"

    @staticmethod
    def fetch_product_info(asin):
        url = f"https://www.amazon.in/dp/{asin}"
        html_text = AmazonScraper.fetch_page(url)
        title = AmazonScraper.fetch_title(url, asin)
        status = AmazonScraper.check_stock(url, html_text)
        price = AmazonScraper.extract_price(url, html_text)
        return {"title": title, "url": url, "status": status, "price": price}

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
        update.message.reply_text(
            "▸ *Amazon Tracker v3.1*\n\n"
            "▸ /add · add product\n"
            "▸ /status · check stock\n"
            "▸ /remove · remove product\n\n"
            "▸ price alerts at 5% drop",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Start error: {e}")
        update.message.reply_text("❌ error, try again")

def status_check(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    try:
        products = db.get_products(user_id)

        if not products:
            update.message.reply_text("📭 no products added", parse_mode=ParseMode.MARKDOWN)
            return

        msg = "▸ *your products*\n\n"
        
        for p in products:
            info = AmazonScraper.fetch_product_info(p['asin'])
            stock = info['status']
            price = info['price']
            
            old_price = db.update_product_price(p['id'], price)
            db.update_product_status(p['id'], stock)
            
            # 🔥 Premium circles - only green/red
            if stock == "IN_STOCK":
                status_icon = "🟢"
            elif stock == "OUT_OF_STOCK":
                status_icon = "🔴"
            else:
                status_icon = "○"  # hollow circle for unknown
            
            price_display = f"₹{price:,.0f}" if price else "price n/a"
            
            if old_price > 0 and price > 0 and price < old_price:
                drop = ((old_price - price) / old_price) * 100
                price_display += f"  ↓{drop:.0f}%"
            
            msg += f"▸ *{p['title'][:50]}...*\n"
            msg += f"  {price_display}  ·  {status_icon}\n\n"
            
            time.sleep(2)

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Status error: {e}")
        update.message.reply_text("❌ error checking status")

def add(update: Update, context: CallbackContext):
    update.message.reply_text(
        "▸ send amazon link\n\n"
        "eg: `amazon.in/dp/B0CHX1W1XY`",
        parse_mode=ParseMode.MARKDOWN
    )

def remove(update: Update, context: CallbackContext):
    try:
        products = db.get_products(update.effective_user.id)
        if not products:
            update.message.reply_text("📭 no products to remove", parse_mode=ParseMode.MARKDOWN)
            return

        context.user_data["remove_list"] = products
        msg = "▸ send number to remove\n\n"
        for i, p in enumerate(products, 1):
            msg += f"▸ `{i}` {p['title'][:30]}...\n"

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Remove error: {e}")
        update.message.reply_text("❌ error")

def handle_message(update: Update, context: CallbackContext):
    try:
        if "remove_list" in context.user_data:
            handle_remove_number(update, context)
            return

        user_id = update.effective_user.id
        db.add_user(user_id, update.effective_chat.id)

        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            update.message.reply_text("❌ invalid link", parse_mode=ParseMode.MARKDOWN)
            return

        update.message.reply_text(f"🔍 fetching `{asin}`...", parse_mode=ParseMode.MARKDOWN)

        info = AmazonScraper.fetch_product_info(asin)
        db.add_product(user_id, asin, info["title"], info["url"])
        db.update_product_price(p['id'] if 'p' in locals() else None, info["price"])

        # 🔥 Premium circles
        if info["status"] == "IN_STOCK":
            status_icon = "🟢"
        elif info["status"] == "OUT_OF_STOCK":
            status_icon = "🔴"
        else:
            status_icon = "○"

        price_text = f"₹{info['price']:,.0f}" if info['price'] else "price n/a"

        update.message.reply_text(
            f"▸ *{info['title'][:100]}*\n\n"
            f"▸ {price_text}\n"
            f"▸ {status_icon} {info['status']}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Message error: {e}")
        update.message.reply_text("❌ error")

def handle_remove_number(update: Update, context: CallbackContext):
    try:
        products = context.user_data["remove_list"]
        text = update.message.text.strip()

        if not text.isdigit():
            update.message.reply_text("❌ send a number", parse_mode=ParseMode.MARKDOWN)
            return

        index = int(text) - 1
        if index < 0 or index >= len(products):
            update.message.reply_text("❌ invalid number", parse_mode=ParseMode.MARKDOWN)
            return

        product = products[index]
        db.remove_product(product["id"], update.effective_user.id)

        del context.user_data["remove_list"]
        update.message.reply_text("✅ product removed", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Remove error: {e}")
        update.message.reply_text("❌ error")

# ================= STOCK CHECKER =================
def scheduled_stock_check(context: CallbackContext):
    logger.info("🔄 checking stock...")
    
    try:
        products = db.get_all_products_with_users()
        if not products:
            return
            
        for product in products:
            try:
                old_status = product.get('last_status', 'UNKNOWN')
                old_price = product.get('current_price', 0)
                
                info = AmazonScraper.fetch_product_info(product['asin'])
                new_status = info['status']
                new_price = info['price']
                
                db.update_product_status(product['id'], new_status)
                
                if new_status == 'IN_STOCK' and new_price > 0:
                    old_price = db.update_product_price(product['id'], new_price)
                    
                    if old_price > 0 and new_price < old_price:
                        drop = ((old_price - new_price) / old_price) * 100
                        if drop >= 5:
                            context.bot.send_message(
                                chat_id=product['chat_id'],
                                text=(
                                    f"▸ *{product['title'][:100]}*\n\n"
                                    f"▸ price dropped {drop:.0f}%\n"
                                    f"▸ now ₹{new_price:,.0f}\n"
                                    f"▸ was ₹{old_price:,.0f}\n\n"
                                    f"🔗 [view]({product['url']})"
                                ),
                                parse_mode=ParseMode.MARKDOWN
                            )
                
                if old_status != new_status:
                    if old_status == 'OUT_OF_STOCK' and new_status == 'IN_STOCK':
                        for i in range(10):
                            price_info = f"\n▸ ₹{new_price:,.0f}" if new_price else ""
                            context.bot.send_message(
                                chat_id=product['chat_id'],
                                text=(
                                    f"▸ *{product['title'][:100]}*\n\n"
                                    f"▸ back in stock! {i+1}/10{price_info}\n\n"
                                    f"🔗 [view]({product['url']})"
                                ),
                                parse_mode=ParseMode.MARKDOWN
                            )
                            if i < 9:
                                time.sleep(2)
                    
                    elif old_status == 'IN_STOCK' and new_status == 'OUT_OF_STOCK':
                        context.bot.send_message(
                            chat_id=product['chat_id'],
                            text=(
                                f"▸ *{product['title'][:100]}*\n\n"
                                f"▸ out of stock\n\n"
                                f"🔗 [view]({product['url']})"
                            ),
                            parse_mode=ParseMode.MARKDOWN
                        )
                
                time.sleep(random.randint(5, 10))
                
            except Exception as e:
                logger.error(f"Error checking {product.get('asin', '')}: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Stock check error: {e}")

# ================= HEALTH CHECK =================
health_app = Flask(__name__)

@health_app.route('/')
def home():
    return "bot running", 200

@health_app.route('/health')
def health():
    return "OK", 200

def run_health_server():
    health_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

# ================= MAIN =================
def main():
    logger.info("=" * 40)
    logger.info("AMAZON TRACKER v3.1")
    logger.info("Premium Circles Edition")
    logger.info("=" * 40)
    
    threading.Thread(target=run_health_server, daemon=True).start()
    
    try:
        db.create_tables()
        logger.info("✅ database ready")
    except Exception as e:
        logger.error(f"Database error: {e}")
        time.sleep(5)
        main()
        return
    
    updater = Updater(token=BOT_TOKEN, use_context=True)
    
    try:
        updater.bot.delete_webhook()
    except:
        pass
    
    dp = updater.dispatcher
    job_queue = updater.job_queue
    
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("status", status_check))
    dp.add_handler(CommandHandler("remove", remove))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_error_handler(error_handler)
    
    job_queue.run_repeating(scheduled_stock_check, interval=60, first=10)
    
    updater.start_polling()
    logger.info("✅ bot running!")
    updater.idle()

if __name__ == "__main__":
    main()
