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
        self.create_tables()
        self.add_price_columns_if_not_exist()

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
            last_status VARCHAR(20) DEFAULT 'UNKNOWN',
            last_checked TIMESTAMP,
            UNIQUE(user_id, asin)
        );
        """)

    def add_price_columns_if_not_exist(self):
        try:
            check_query = """
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='products' AND column_name='current_price'
            """
            result = self.execute(check_query, fetch_all=True)
            
            if not result:
                logger.info("💰 Adding price columns to database...")
                
                alter_queries = [
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS current_price DECIMAL(10,2) DEFAULT 0",
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS last_price DECIMAL(10,2) DEFAULT 0",
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS currency VARCHAR(10) DEFAULT '₹'"
                ]
                
                for query in alter_queries:
                    try:
                        self.execute(query)
                    except:
                        pass
                
                logger.info("✅ Price columns added")
        except Exception as e:
            logger.error(f"Error adding price columns: {e}")

    def add_user(self, user_id, chat_id):
        self.execute("""
        INSERT INTO users (user_id, chat_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET chat_id = EXCLUDED.chat_id
        """, (user_id, chat_id))

    def add_product(self, user_id, asin, title, url, price=0):
        self.execute("""
        INSERT INTO products (user_id, asin, title, url, last_status, current_price, last_price)
        VALUES (%s, %s, %s, %s, 'UNKNOWN', %s, %s)
        ON CONFLICT (user_id, asin)
        DO UPDATE SET 
            title = EXCLUDED.title, 
            url = EXCLUDED.url,
            current_price = EXCLUDED.current_price
        """, (user_id, asin, title, url, price, price))

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

    def update_product_price(self, product_id, current_price):
        product = self.execute("SELECT current_price, last_status FROM products WHERE id = %s", (product_id,), fetch_one=True)
        
        if not product:
            return 0
        
        if product['last_status'] == 'OUT_OF_STOCK':
            return product['current_price'] or 0
        
        last_price = product['current_price'] if product else 0
        
        self.execute("""
            UPDATE products 
            SET last_price = %s, current_price = %s, last_checked = NOW() 
            WHERE id = %s
        """, (last_price, current_price, product_id))
        
        return last_price

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
                title = tag.get_text(strip=True)
                title = html.unescape(title)
                title = title.replace('*', '•').replace('_', ' ').replace('[', '(').replace(']', ')')
                return title

            if soup.title:
                title = soup.title.get_text()
                title = re.sub(r'\s*-+\s*Amazon.*$', '', title, flags=re.IGNORECASE)
                title = html.unescape(title.strip())
                title = title.replace('*', '•').replace('_', ' ')
                return title
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
                'span.a-price[data-a-size="xl"] span.a-offscreen',
                '#priceblock_dealprice',
                '#priceblock_ourprice',
                '.a-price .a-offscreen'
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
                r'Rs\.?\s*([\d,]+(?:\.\d{2})?)',
                r'Price:\s*₹\s*([\d,]+(?:\.\d{2})?)'
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

    # 🔥 FIXED: More accurate stock detection
    @staticmethod
    def check_stock(url, html_text=None):
        """More accurate stock detection - checks main product area only"""
        if not html_text:
            html_text = AmazonScraper.fetch_page(url)
            if not html_text:
                return "UNKNOWN"

        try:
            soup = BeautifulSoup(html_text, "lxml")
            
            # METHOD 1: Check for buy buttons first (most reliable)
            if soup.find(id="add-to-cart-button"):
                logger.info("✅ Found add-to-cart button → IN_STOCK")
                return "IN_STOCK"
            
            if soup.find(id="buy-now-button"):
                logger.info("✅ Found buy-now button → IN_STOCK")
                return "IN_STOCK"
            
            if soup.find(id="buy-now"):
                logger.info("✅ Found buy-now → IN_STOCK")
                return "IN_STOCK"
            
            # METHOD 2: Check availability span
            availability = soup.find(id="availability")
            if availability:
                avail_text = availability.get_text().lower()
                if "in stock" in avail_text:
                    logger.info("✅ Availability span says IN STOCK")
                    return "IN_STOCK"
                elif "out of stock" in avail_text or "currently unavailable" in avail_text:
                    logger.info("❌ Availability span says OUT OF STOCK")
                    return "OUT_OF_STOCK"
            
            # METHOD 3: Check main product area only (not footer)
            main_content = soup.find('div', {'id': 'centerCol'}) or soup.find('div', {'id': 'rightCol'}) or soup.find('div', {'class': 'a-section'})
            
            if main_content:
                main_text = main_content.get_text().lower()
                
                # IN_STOCK indicators in main area
                if "add to cart" in main_text:
                    logger.info("✅ 'add to cart' in main area → IN_STOCK")
                    return "IN_STOCK"
                
                if "buy now" in main_text:
                    logger.info("✅ 'buy now' in main area → IN_STOCK")
                    return "IN_STOCK"
                
                # OUT_OF_STOCK indicators in main area
                if "out of stock" in main_text:
                    logger.info("❌ 'out of stock' in main area → OUT_OF_STOCK")
                    return "OUT_OF_STOCK"
                
                if "currently unavailable" in main_text:
                    logger.info("❌ 'currently unavailable' in main area → OUT_OF_STOCK")
                    return "OUT_OF_STOCK"
            
            # METHOD 4: Check page title/header
            product_title = soup.find(id="productTitle")
            if product_title:
                title_area = product_title.find_next('div')
                if title_area:
                    area_text = title_area.get_text().lower()
                    if "out of stock" in area_text or "currently unavailable" in area_text:
                        logger.info("❌ Found OOS near title → OUT_OF_STOCK")
                        return "OUT_OF_STOCK"
            
            # METHOD 5: Last resort - check whole page but with caution
            page_text = soup.get_text(" ").lower()
            
            # Count occurrences
            oos_count = page_text.count("out of stock") + page_text.count("currently unavailable")
            instock_count = page_text.count("add to cart") + page_text.count("buy now")
            
            # If we see buy buttons more than OOS text, assume IN_STOCK
            if instock_count > oos_count:
                logger.info(f"✅ Buy buttons ({instock_count}) > OOS text ({oos_count}) → IN_STOCK")
                return "IN_STOCK"
            
            # If OOS text is prominent, return OUT_OF_STOCK
            if oos_count > 0:
                logger.info(f"❌ Found OOS text ({oos_count} times) → OUT_OF_STOCK")
                return "OUT_OF_STOCK"
            
            # Default
            logger.info("⚠️ Could not determine, defaulting to UNKNOWN")
            return "UNKNOWN"
            
        except Exception as e:
            logger.error(f"Error checking stock: {e}")
            return "UNKNOWN"

    @staticmethod
    def fetch_product_info(asin):
        url = f"https://www.amazon.in/dp/{asin}"
        html_text = AmazonScraper.fetch_page(url)
        title = AmazonScraper.fetch_title(url, asin) if html_text else f"Product {asin}"
        status = AmazonScraper.check_stock(url, html_text)
        price = AmazonScraper.extract_price(url, html_text)
        
        return {
            "title": title, 
            "url": url, 
            "status": status,
            "price": price
        }

# ================= BOT HANDLERS =================
db = DatabaseManager()

def error_handler(update: Update, context: CallbackContext):
    try:
        raise context.error
    except Conflict:
        logger.warning("⚠️ Conflict error - will auto-resolve")
        time.sleep(2)
    except Exception as e:
        logger.error(f"Error: {e}")

def start(update: Update, context: CallbackContext):
    try:
        db.add_user(update.effective_user.id, update.effective_chat.id)
        
        msg = (
            "✅ *Amazon Tracker Activated*\n\n"
            "Commands:\n"
            "/add - Add product\n"
            "/list - Show products\n"
            "/status - Check stock\n"
            "/remove - Remove product"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Start error: {e}")
        update.message.reply_text("✅ Bot activated! Use /add to add products.")

def list_products(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    try:
        products = db.get_products(user_id)

        if not products:
            update.message.reply_text("No products added.")
            return

        msg = "*Your Products:*\n"
        for i, p in enumerate(products, 1):
            status = "IN" if p.get('last_status') == 'IN_STOCK' else "OUT"
            price = f"₹{p['current_price']:,.0f}" if p.get('current_price') and p['current_price'] > 0 else "N/A"
            msg += f"\n{i}. {p['title'][:50]}...\n   {status} | {price}"

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"List error: {e}")
        update.message.reply_text("Error fetching list.")

def status_check(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    try:
        products = db.get_products(user_id)

        if not products:
            update.message.reply_text("No products added.")
            return

        msg = "*Stock Status:*\n"
        for p in products:
            info = AmazonScraper.fetch_product_info(p['asin'])
            stock = info['status']
            price = info['price']
            
            if price > 0 and stock == 'IN_STOCK':
                db.update_product_price(p['id'], price)
            db.update_product_status(p['id'], stock)
            
            status_emoji = "✅" if stock == "IN_STOCK" else "❌"
            if stock == "IN_STOCK" and price:
                price_display = f"₹{price:,.0f}"
                msg += f"\n{status_emoji} {p['title'][:50]}...\n   {price_display} | [View]({p['url']})"
            else:
                msg += f"\n{status_emoji} {p['title'][:50]}...\n   Out of Stock | [View]({p['url']})"
            
            time.sleep(2)

        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Status error: {e}")
        update.message.reply_text("Error checking status.")

def add(update: Update, context: CallbackContext):
    update.message.reply_text("Send Amazon product link")

def remove(update: Update, context: CallbackContext):
    try:
        products = db.get_products(update.effective_user.id)
        if not products:
            update.message.reply_text("No products to remove.")
            return

        context.user_data["remove_list"] = products
        msg = "Send number to remove:\n"
        for i, p in enumerate(products, 1):
            msg += f"\n{i}. {p['title'][:50]}..."

        update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Remove error: {e}")
        update.message.reply_text("Error occurred.")

def handle_message(update: Update, context: CallbackContext):
    try:
        if "remove_list" in context.user_data:
            handle_remove_number(update, context)
            return

        user_id = update.effective_user.id
        db.add_user(user_id, update.effective_chat.id)

        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            update.message.reply_text("Invalid Amazon link")
            return

        update.message.reply_text(f"Fetching {asin}...")

        info = AmazonScraper.fetch_product_info(asin)
        db.add_product(user_id, asin, info["title"], info["url"], info["price"])

        status_text = "IN STOCK" if info["status"] == "IN_STOCK" else "OUT OF STOCK" if info["status"] == "OUT_OF_STOCK" else "UNKNOWN"
        price_text = f"₹{info['price']:,.0f}" if info['price'] else "Price N/A"
        
        update.message.reply_text(
            f"✅ Product Added\n\n"
            f"{info['title'][:100]}\n\n"
            f"{price_text}\n"
            f"Status: {status_text}"
        )
    except Exception as e:
        logger.error(f"Message error: {e}")
        update.message.reply_text("Error processing request.")

def handle_remove_number(update: Update, context: CallbackContext):
    try:
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
        logger.error(f"Remove number error: {e}")
        update.message.reply_text("Error removing product.")

# ================= STOCK CHECKER =================
def scheduled_stock_check(context: CallbackContext):
    logger.info("🔄 Running stock check...")
    
    try:
        products = db.get_all_products_with_users()
        if not products:
            return
            
        for product in products:
            try:
                old_status = product.get('last_status', 'UNKNOWN')
                info = AmazonScraper.fetch_product_info(product['asin'])
                new_status = info['status']
                new_price = info['price']
                
                db.update_product_status(product['id'], new_status)
                
                if new_status == 'IN_STOCK' and new_price > 0:
                    last_price = db.update_product_price(product['id'], new_price)
                    
                    if last_price > 0 and new_price < last_price:
                        drop = ((last_price - new_price) / last_price) * 100
                        if drop >= 5:
                            context.bot.send_message(
                                chat_id=product['chat_id'],
                                text=(
                                    f"💰 Price Drop!\n\n"
                                    f"{product['title'][:100]}\n\n"
                                    f"Old: ₹{last_price:,.0f}\n"
                                    f"New: ₹{new_price:,.0f}\n"
                                    f"Drop: {drop:.1f}%\n\n"
                                    f"{product['url']}"
                                )
                            )
                
                if old_status != new_status:
                    if old_status == 'OUT_OF_STOCK' and new_status == 'IN_STOCK':
                        for i in range(10):
                            price_info = f"\nPrice: ₹{new_price:,.0f}" if new_price else ""
                            context.bot.send_message(
                                chat_id=product['chat_id'],
                                text=(
                                    f"🔥 BACK IN STOCK! ({i+1}/10)\n\n"
                                    f"{product['title'][:100]}{price_info}\n\n"
                                    f"{product['url']}"
                                )
                            )
                            if i < 9:
                                time.sleep(2)
                    
                    elif old_status == 'IN_STOCK' and new_status == 'OUT_OF_STOCK':
                        context.bot.send_message(
                            chat_id=product['chat_id'],
                            text=(
                                f"📉 OUT OF STOCK\n\n"
                                f"{product['title'][:100]}\n\n"
                                f"{product['url']}"
                            )
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
    return "Amazon Bot Running", 200

@health_app.route('/health')
def health():
    return "OK", 200

def run_health_server():
    health_app.run(host='0.0.0.0', port=PORT)

# ================= MAIN =================
def main():
    logger.info("=" * 50)
    logger.info("AMAZON BOT - FIXED STOCK DETECTION")
    logger.info("=" * 50)
    
    # Health server
    threading.Thread(target=run_health_server, daemon=True).start()
    
    # Database
    try:
        db.create_tables()
        db.add_price_columns_if_not_exist()
        logger.info("✅ Database ready")
    except Exception as e:
        logger.error(f"Database error: {e}")
        time.sleep(5)
        main()
        return
    
    # Bot
    updater = Updater(token=BOT_TOKEN, use_context=True)
    
    # Clear webhook
    try:
        updater.bot.delete_webhook()
        time.sleep(1)
    except:
        pass
    
    dp = updater.dispatcher
    job_queue = updater.job_queue
    
    # Handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("add", add))
    dp.add_handler(CommandHandler("list", list_products))
    dp.add_handler(CommandHandler("status", status_check))
    dp.add_handler(CommandHandler("remove", remove))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_error_handler(error_handler)
    
    # Schedule
    job_queue.run_repeating(scheduled_stock_check, interval=120, first=10)
    
    # Start
    updater.start_polling()
    logger.info("✅ Bot running!")
    updater.idle()

if __name__ == "__main__":
    main()
