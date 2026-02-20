"""
Production-Grade Amazon Stock Tracker Bot
- Scalable architecture with clear separation of concerns
- Smart scraping with rate limiting and anti-blocking measures
- Price tracking and drop alerts
- Efficient database design
- Comprehensive error handling and logging
"""

import logging
import logging.handlers
import re
import html
import json
import random
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional, Tuple, Any
from functools import lru_cache
from collections import defaultdict, deque
import hashlib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from psycopg2 import pool
from psycopg2.extras import DictCursor
from psycopg2.errors import UniqueViolation
from telegram import Update, ParseMode
from telegram.utils.helpers import escape_markdown
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, 
    Filters, CallbackContext, JobQueue
)
from telegram.error import TelegramError, NetworkError, Conflict, TimedOut
from flask import Flask
import threading
import os
import sys

# ================= CONFIGURATION =================
class Config:
    """Centralized configuration management"""
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    DATABASE_URL = os.environ.get("DATABASE_URL")
    PORT = int(os.environ.get("PORT", 8080))
    
    # Scraping settings
    MIN_REQUEST_INTERVAL = 5  # seconds between requests to same domain
    MAX_RETRIES = 3
    REQUEST_TIMEOUT = 15
    PRODUCT_COOLDOWN_MINUTES = 5  # Minimum time between checks for same product
    
    # Scheduler settings
    MAX_PRODUCTS_PER_RUN = 50  # Max products to check in one scheduler run
    SCHEDULER_INTERVAL = 60  # seconds
    
    # Alert thresholds
    PRICE_DROP_THRESHOLD = 5  # percentage
    
    # Rate limiting
    MAX_USER_REQUESTS_PER_MINUTE = 5
    
    # Cache TTL
    PRODUCT_CACHE_TTL = 300  # seconds
    
    @classmethod
    def validate(cls):
        """Validate required configuration"""
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable not set!")
        if not cls.DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable not set!")

# ================= LOGGING CONFIGURATION =================
def setup_logging():
    """Configure production-grade logging"""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Format for logs
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler with rotation (optional)
    if os.path.exists('/tmp'):
        file_handler = logging.handlers.RotatingFileHandler(
            '/tmp/amazon_bot.log',
            maxBytes=10485760,  # 10MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger

logger = setup_logging()

# ================= DATABASE LAYER =================
class DatabaseManager:
    """Thread-safe database connection pool manager"""
    
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
        self._connect_with_retry()
    
    def _connect_with_retry(self, max_retries=5):
        """Establish database connection with retry logic"""
        for attempt in range(max_retries):
            try:
                self.pool = pool.SimpleConnectionPool(
                    1, 10,  # min, max connections
                    Config.DATABASE_URL,
                    cursor_factory=DictCursor
                )
                logger.info("✅ Database pool created")
                self._init_schema()
                return
            except Exception as e:
                logger.error(f"Database connection failed (attempt {attempt+1}): {e}")
                if attempt == max_retries - 1:
                    logger.critical("Cannot connect to database. Exiting...")
                    raise
                time.sleep(5 * (attempt + 1))
    
    def _init_schema(self):
        """Initialize database schema"""
        queries = [
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                last_active TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                asin VARCHAR(10) NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                current_price DECIMAL(10,2) DEFAULT 0,
                last_price DECIMAL(10,2) DEFAULT 0,
                currency VARCHAR(10) DEFAULT '₹',
                last_status VARCHAR(20) DEFAULT 'UNKNOWN',
                last_checked TIMESTAMP,
                next_check TIMESTAMP DEFAULT NOW(),
                check_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, asin)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_products_next_check 
            ON products(next_check) WHERE last_status != 'INACTIVE'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_products_user_id 
            ON products(user_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                price DECIMAL(10,2),
                status VARCHAR(20),
                checked_at TIMESTAMP DEFAULT NOW()
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_price_history_product 
            ON price_history(product_id, checked_at DESC)
            """
        ]
        
        for query in queries:
            self.execute(query)
        
        logger.info("✅ Database schema initialized")
    
    def execute(self, query: str, params: tuple = None, 
                fetch_one: bool = False, fetch_all: bool = False):
        """Execute database query with connection pooling and retry logic"""
        conn = None
        for attempt in range(3):
            try:
                if not self.pool:
                    self._connect_with_retry()
                
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
                        self.pool.putconn(conn, close=True)
                    except:
                        pass
                    conn = None
                
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
                
            finally:
                if conn:
                    try:
                        self.pool.putconn(conn)
                    except:
                        pass
    
    def add_user(self, user_id: int, chat_id: int, username: str = None,
                 first_name: str = None, last_name: str = None):
        """Add or update user"""
        self.execute("""
            INSERT INTO users (user_id, chat_id, username, first_name, last_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) 
            DO UPDATE SET 
                chat_id = EXCLUDED.chat_id,
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                last_active = NOW()
        """, (user_id, chat_id, username, first_name, last_name))
    
    def add_product(self, user_id: int, asin: str, title: str, url: str) -> int:
        """Add product and return product ID"""
        result = self.execute("""
            INSERT INTO products (user_id, asin, title, url)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, asin) 
            DO UPDATE SET 
                title = EXCLUDED.title,
                url = EXCLUDED.url,
                updated_at = NOW()
            RETURNING id
        """, (user_id, asin, title, url), fetch_one=True)
        
        return result['id'] if result else None
    
    def update_product_status(self, product_id: int, status: str, price: float = None):
        """Update product status and optionally price"""
        if price is not None:
            # Get current price for history
            current = self.execute(
                "SELECT current_price FROM products WHERE id = %s",
                (product_id,), fetch_one=True
            )
            
            if current and current['current_price'] != price:
                # Save to price history
                self.execute("""
                    INSERT INTO price_history (product_id, price, status)
                    VALUES (%s, %s, %s)
                """, (product_id, current['current_price'], status))
            
            # Update product
            self.execute("""
                UPDATE products 
                SET last_price = current_price,
                    current_price = %s,
                    last_status = %s,
                    last_checked = NOW(),
                    next_check = NOW() + INTERVAL '%s minutes',
                    check_count = check_count + 1
                WHERE id = %s
            """, (price, status, Config.PRODUCT_COOLDOWN_MINUTES, product_id))
        else:
            self.execute("""
                UPDATE products 
                SET last_status = %s,
                    last_checked = NOW(),
                    next_check = NOW() + INTERVAL '%s minutes',
                    fail_count = fail_count + 1
                WHERE id = %s
            """, (status, Config.PRODUCT_COOLDOWN_MINUTES, product_id))
    
    def get_products_for_checking(self, limit: int = None) -> List[Dict]:
        """Get products that are due for checking"""
        limit = limit or Config.MAX_PRODUCTS_PER_RUN
        return self.execute("""
            SELECT p.*, u.chat_id, u.user_id
            FROM products p
            JOIN users u ON u.user_id = p.user_id
            WHERE p.next_check <= NOW() 
                AND p.last_status != 'INACTIVE'
            ORDER BY p.next_check ASC
            LIMIT %s
        """, (limit,), fetch_all=True) or []
    
    def get_user_products(self, user_id: int) -> List[Dict]:
        """Get all products for a user"""
        return self.execute("""
            SELECT * FROM products 
            WHERE user_id = %s 
            ORDER BY created_at DESC
        """, (user_id,), fetch_all=True) or []
    
    def remove_product(self, product_id: int, user_id: int) -> bool:
        """Remove a product"""
        result = self.execute("""
            DELETE FROM products 
            WHERE id = %s AND user_id = %s
            RETURNING id
        """, (product_id, user_id), fetch_one=True)
        return result is not None
    
    def check_price_drop(self, product_id: int, new_price: float) -> Tuple[bool, float, float]:
        """Check if price dropped significantly"""
        product = self.execute(
            "SELECT current_price FROM products WHERE id = %s",
            (product_id,), fetch_one=True
        )
        
        if not product or not product['current_price']:
            return False, 0, 0
        
        old_price = float(product['current_price'])
        if old_price > 0 and new_price > 0 and new_price < old_price:
            drop_percent = ((old_price - new_price) / old_price) * 100
            if drop_percent >= Config.PRICE_DROP_THRESHOLD:
                return True, drop_percent, old_price
        
        return False, 0, old_price
    
    def close(self):
        """Close database connection pool"""
        if self.pool:
            self.pool.closeall()
            logger.info("Database pool closed")

# ================= SCRAPING LAYER =================
class AmazonScraper:
    """Robust Amazon.in scraper with anti-blocking measures"""
    
    # Rotating user agents
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
    ]
    
    # Common price patterns for Amazon.in
    PRICE_PATTERNS = [
        r'currencyINR.+?>([\d,]+(?:\.\d{2})?)</span>',
        r'<span[^>]*class="a-price-whole"[^>]*>([\d,]+)</span>',
        r'<span[^>]*class="a-price-decimal"[^>]*>\.</span><span[^>]*class="a-price-fraction"[^>]*>(\d{2})</span>',
        r'<span[^>]*class="a-offscreen"[^>]*>([\d,]+(?:\.\d{2})?)</span>',
        r'<span[^>]*id="priceblock_dealprice"[^>]*>([\d,]+(?:\.\d{2})?)</span>',
        r'<span[^>]*id="priceblock_ourprice"[^>]*>([\d,]+(?:\.\d{2})?)</span>',
    ]
    
    def __init__(self):
        self.session = self._create_session()
        self.last_request_time = 0
        self._lock = Lock()
    
    def _create_session(self) -> requests.Session:
        """Create configured requests session with retry strategy"""
        session = requests.Session()
        
        # Retry strategy
        retry_strategy = Retry(
            total=Config.MAX_RETRIES,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def _get_headers(self) -> Dict:
        """Generate random headers for request"""
        return {
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
    
    def _wait_for_rate_limit(self):
        """Ensure minimum time between requests"""
        with self._lock:
            elapsed = time.time() - self.last_request_time
            if elapsed < Config.MIN_REQUEST_INTERVAL:
                sleep_time = Config.MIN_REQUEST_INTERVAL - elapsed + random.uniform(1, 3)
                time.sleep(sleep_time)
            self.last_request_time = time.time()
    
    @staticmethod
    def extract_asin(text: str) -> Optional[str]:
        """Extract ASIN from Amazon URL or text"""
        patterns = [
            r'/dp/([A-Z0-9]{10})',
            r'/product/([A-Z0-9]{10})',
            r'/gp/product/([A-Z0-9]{10})',
            r'/([A-Z0-9]{10})(?:[/?]|$)'
        ]
        
        text = text.upper()
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                asin = match.group(1)
                if re.match(r'^[A-Z0-9]{10}$', asin):
                    return asin
        return None
    
    def fetch_page(self, url: str) -> Optional[str]:
        """Fetch page with rate limiting and retry logic"""
        self._wait_for_rate_limit()
        
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=Config.REQUEST_TIMEOUT,
                allow_redirects=True
            )
            
            # Check for blocking
            if response.status_code == 404:
                logger.warning(f"Product not found: {url}")
                return None
            
            if response.status_code in [403, 503]:
                logger.error(f"Amazon blocking detected (status {response.status_code})")
                time.sleep(random.uniform(10, 20))
                return None
            
            if response.status_code != 200:
                logger.error(f"HTTP {response.status_code} for {url}")
                return None
            
            # Check if we got a captcha or bot check
            if "captcha" in response.text.lower() or "enter the characters" in response.text.lower():
                logger.error("Captcha detected! Increasing delays...")
                time.sleep(random.uniform(30, 60))
                return None
            
            return response.text
            
        except requests.Timeout:
            logger.error(f"Timeout fetching {url}")
            return None
        except requests.ConnectionError:
            logger.error(f"Connection error fetching {url}")
            return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None
    
    def extract_title(self, soup: BeautifulSoup) -> str:
        """Extract product title"""
        # Try multiple title selectors
        title_selectors = [
            'span#productTitle',
            'h1#title',
            '#productTitle',
            'h1.a-spacing-small',
            'meta[name="title"]'
        ]
        
        for selector in title_selectors:
            if selector.startswith('meta'):
                tag = soup.find('meta', attrs={'name': 'title'})
                if tag and tag.get('content'):
                    return html.unescape(tag['content'].strip())
            else:
                tag = soup.select_one(selector)
                if tag:
                    return html.unescape(tag.get_text(strip=True))
        
        # Fallback to title tag
        if soup.title:
            title = soup.title.string
            # Remove Amazon suffix
            title = re.sub(r'\s*:\s*Amazon\.in:.*$', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\s*-\s*Amazon\.in$', '', title, flags=re.IGNORECASE)
            return html.unescape(title.strip())
        
        return "Unknown Product"
    
    def extract_price(self, soup: BeautifulSoup) -> Optional[float]:
        """Extract price from page"""
        # Method 1: Try direct price element
        price_elem = soup.select_one('.a-price .a-offscreen')
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            match = re.search(r'([\d,]+(?:\.\d{2})?)', price_text)
            if match:
                try:
                    return float(match.group(1).replace(',', ''))
                except ValueError:
                    pass
        
        # Method 2: Try whole price
        whole_elem = soup.select_one('.a-price-whole')
        if whole_elem:
            whole_text = whole_elem.get_text(strip=True)
            fraction_elem = soup.select_one('.a-price-fraction')
            fraction = fraction_elem.get_text(strip=True) if fraction_elem else '00'
            
            try:
                whole = whole_text.replace(',', '').strip()
                if whole and whole.isdigit():
                    return float(f"{whole}.{fraction}")
            except ValueError:
                pass
        
        # Method 3: Search for price patterns
        for pattern in self.PRICE_PATTERNS:
            match = re.search(pattern, str(soup), re.IGNORECASE)
            if match:
                try:
                    price_str = match.group(1).replace(',', '')
                    return float(price_str)
                except (ValueError, IndexError):
                    continue
        
        return None
    
    def extract_stock_status(self, soup: BeautifulSoup) -> str:
        """Determine stock status"""
        page_text = soup.get_text(" ", strip=True).lower()
        
        # Check for out of stock indicators
        out_of_stock_phrases = [
            'currently unavailable',
            'out of stock',
            'temporarily out of stock',
            'we don\'t know when or if this item will be back in stock',
            'currently out of stock'
        ]
        
        for phrase in out_of_stock_phrases:
            if phrase in page_text:
                return "OUT_OF_STOCK"
        
        # Check for buy buttons
        if soup.find(id='add-to-cart-button'):
            return "IN_STOCK"
        
        if soup.find(id='buy-now-button'):
            return "IN_STOCK"
        
        if soup.select_one('input[name="submit.add-to-cart"]'):
            return "IN_STOCK"
        
        # Check for availability message
        availability = soup.select_one('#availability span, .availability span')
        if availability:
            avail_text = availability.get_text(strip=True).lower()
            if 'in stock' in avail_text:
                return "IN_STOCK"
        
        return "UNKNOWN"
    
    def scrape_product(self, asin: str) -> Dict[str, Any]:
        """Scrape complete product information"""
        url = f"https://www.amazon.in/dp/{asin}"
        
        html_content = self.fetch_page(url)
        if not html_content:
            return {
                'title': f"Product {asin}",
                'price': None,
                'status': 'UNKNOWN',
                'url': url
            }
        
        try:
            soup = BeautifulSoup(html_content, 'lxml')
            
            title = self.extract_title(soup)
            price = self.extract_price(soup)
            status = self.extract_stock_status(soup)
            
            # Validate price (should be reasonable for Amazon)
            if price and (price < 10 or price > 1000000):
                logger.warning(f"Suspicious price ₹{price} for {asin}, treating as unknown")
                price = None
            
            return {
                'title': title,
                'price': price,
                'status': status,
                'url': url
            }
            
        except Exception as e:
            logger.error(f"Error parsing product {asin}: {e}")
            return {
                'title': f"Product {asin}",
                'price': None,
                'status': 'UNKNOWN',
                'url': url
            }

# ================= CACHE LAYER =================
class ProductCache:
    """Simple TTL cache for product data"""
    
    def __init__(self, ttl_seconds: int = 300):
        self.cache = {}
        self.ttl = ttl_seconds
        self._lock = Lock()
    
    def get(self, asin: str) -> Optional[Dict]:
        """Get cached product data if not expired"""
        with self._lock:
            if asin in self.cache:
                data, timestamp = self.cache[asin]
                if datetime.now() - timestamp < timedelta(seconds=self.ttl):
                    return data
                else:
                    del self.cache[asin]
            return None
    
    def set(self, asin: str, data: Dict):
        """Cache product data"""
        with self._lock:
            self.cache[asin] = (data, datetime.now())
    
    def clear(self):
        """Clear cache"""
        with self._lock:
            self.cache.clear()

# ================= RATE LIMITER =================
class RateLimiter:
    """Per-user rate limiting"""
    
    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self.user_requests = defaultdict(lambda: deque(maxlen=max_requests))
        self._lock = Lock()
    
    def is_allowed(self, user_id: int) -> bool:
        """Check if user request is allowed"""
        with self._lock:
            now = time.time()
            requests = self.user_requests[user_id]
            
            # Clean old requests
            while requests and now - requests[0] > self.window:
                requests.popleft()
            
            if len(requests) >= self.max_requests:
                return False
            
            requests.append(now)
            return True
    
    def get_wait_time(self, user_id: int) -> int:
        """Get seconds until next allowed request"""
        with self._lock:
            requests = self.user_requests[user_id]
            if len(requests) < self.max_requests:
                return 0
            
            oldest = requests[0]
            wait = int(self.window - (time.time() - oldest))
            return max(0, wait)

# ================= MESSAGE FORMATTER =================
class MessageFormatter:
    """Consistent message formatting with markdown escaping"""
    
    @staticmethod
    def escape(text: str) -> str:
        """Escape markdown special characters"""
        return escape_markdown(text, version=2)
    
    @classmethod
    def product_card(cls, product: Dict, show_status: bool = True,
                     show_price: bool = True, show_url: bool = True) -> str:
        """Format product as a card"""
        lines = []
        
        # Title
        title = cls.escape(product['title'][:100])
        lines.append(f"*{title}*")
        
        # Price
        if show_price and product.get('current_price'):
            price = float(product['current_price'])
            lines.append(f"💰 ₹{price:,.0f}")
        
        # Status
        if show_status:
            status_emoji = {
                'IN_STOCK': '🟢',
                'OUT_OF_STOCK': '🔴',
                'UNKNOWN': '⚪'
            }.get(product.get('last_status', 'UNKNOWN'), '⚪')
            lines.append(f"{status_emoji} {product.get('last_status', 'UNKNOWN')}")
        
        # URL
        if show_url and product.get('url'):
            url = cls.escape(product['url'])
            lines.append(f"[View on Amazon]({url})")
        
        return '\n'.join(lines)
    
    @classmethod
    def price_alert(cls, product: Dict, old_price: float, 
                    new_price: float, drop_percent: float) -> str:
        """Format price drop alert"""
        return (
            "╔══════════════════════════╗\n"
            "║      💰 PRICE DROP!       ║\n"
            "╚══════════════════════════╝\n\n"
            f"*{cls.escape(product['title'][:100])}*\n\n"
            f"▸ Old: ~~₹{old_price:,.0f}~~\n"
            f"▸ New: *₹{new_price:,.0f}*\n"
            f"▸ Drop: *{drop_percent:.1f}%*\n\n"
            f"[View on Amazon]({cls.escape(product['url'])})"
        )
    
    @classmethod
    def stock_alert(cls, product: Dict, status: str) -> str:
        """Format stock change alert"""
        if status == 'IN_STOCK':
            return (
                "╔══════════════════════════╗\n"
                "║      🔥 BACK IN STOCK     ║\n"
                "╚══════════════════════════╝\n\n"
                f"*{cls.escape(product['title'][:100])}*\n\n"
                f"💰 ₹{float(product['current_price']):,.0f}\n\n"
                f"[View on Amazon]({cls.escape(product['url'])})"
            )
        else:
            return (
                "╔══════════════════════════╗\n"
                "║      📉 OUT OF STOCK      ║\n"
                "╚══════════════════════════╝\n\n"
                f"*{cls.escape(product['title'][:100])}*\n\n"
                f"[View on Amazon]({cls.escape(product['url'])})"
            )

# ================= BOT LAYER =================
class AmazonBot:
    """Main bot class with command handlers"""
    
    def __init__(self, token: str):
        self.token = token
        self.db = DatabaseManager()
        self.scraper = AmazonScraper()
        self.cache = ProductCache()
        self.rate_limiter = RateLimiter(
            max_requests=Config.MAX_USER_REQUESTS_PER_MINUTE
        )
        self.updater = None
    
    def start(self):
        """Start the bot"""
        # Create updater
        self.updater = Updater(token=self.token, use_context=True)
        
        # Delete webhook
        try:
            self.updater.bot.delete_webhook()
            logger.info("Webhook deleted")
        except:
            pass
        
        # Register handlers
        dp = self.updater.dispatcher
        dp.add_handler(CommandHandler("start", self.cmd_start))
        dp.add_handler(CommandHandler("add", self.cmd_add))
        dp.add_handler(CommandHandler("list", self.cmd_list))
        dp.add_handler(CommandHandler("status", self.cmd_status))
        dp.add_handler(CommandHandler("remove", self.cmd_remove))
        dp.add_handler(CommandHandler("help", self.cmd_help))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, self.handle_message))
        dp.add_error_handler(self.error_handler)
        
        # Setup job queue
        jq = self.updater.job_queue
        jq.run_repeating(self.scheduled_check, interval=Config.SCHEDULER_INTERVAL, first=10)
        
        # Start polling
        self.updater.start_polling()
        logger.info("✅ Bot started!")
        
        # Run until stopped
        self.updater.idle()
    
    def stop(self):
        """Stop the bot gracefully"""
        if self.updater:
            self.updater.stop()
        self.db.close()
        logger.info("Bot stopped")
    
    def cmd_start(self, update: Update, context: CallbackContext):
        """Handle /start command"""
        user = update.effective_user
        self.db.add_user(
            user.id, 
            update.effective_chat.id,
            user.username,
            user.first_name,
            user.last_name
        )
        
        welcome_msg = (
            "╔══════════════════════════╗\n"
            "║   🛒 AMAZON TRACKER PRO   ║\n"
            "╚══════════════════════════╝\n\n"
            "Track Amazon.in products for stock & price changes!\n\n"
            "*Commands:*\n"
            "▸ /add ➕ Add product (send link)\n"
            "▸ /list 📋 Your products\n"
            "▸ /status 📊 Check now\n"
            "▸ /remove 🗑 Remove product\n"
            "▸ /help ℹ️ More info\n\n"
            f"💰 Price drop alerts at {Config.PRICE_DROP_THRESHOLD}%+\n"
            f"🔄 Checks every {Config.PRODUCT_COOLDOWN_MINUTES} minutes"
        )
        
        update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)
    
    def cmd_add(self, update: Update, context: CallbackContext):
        """Handle /add command"""
        update.message.reply_text(
            "🔗 *Send me an Amazon product link*\n\n"
            "Example:\n"
            "`https://www.amazon.in/dp/B0CHX1W1XY`",
            parse_mode=ParseMode.MARKDOWN
        )
    
    def cmd_list(self, update: Update, context: CallbackContext):
        """Handle /list command"""
        products = self.db.get_user_products(update.effective_user.id)
        
        if not products:
            update.message.reply_text(
                "📭 *No products being tracked*\n\n"
                "Use /add to start tracking!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        lines = ["╔══════════════════════════╗",
                 "║      📋 YOUR PRODUCTS     ║",
                 "╚══════════════════════════╝\n"]
        
        for i, product in enumerate(products, 1):
            status_emoji = {
                'IN_STOCK': '🟢',
                'OUT_OF_STOCK': '🔴',
                'UNKNOWN': '⚪'
            }.get(product['last_status'], '⚪')
            
            price = f"₹{float(product['current_price']):,.0f}" if product['current_price'] else "N/A"
            lines.append(f"{i}. {status_emoji} *{MessageFormatter.escape(product['title'][:50])}*")
            lines.append(f"   {price}\n")
        
        update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.MARKDOWN)
    
    def cmd_status(self, update: Update, context: CallbackContext):
        """Handle /status command - immediate check"""
        user_id = update.effective_user.id
        
        # Rate limiting
        if not self.rate_limiter.is_allowed(user_id):
            wait = self.rate_limiter.get_wait_time(user_id)
            update.message.reply_text(
                f"⚠️ Too many requests. Please wait {wait} seconds.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        products = self.db.get_user_products(user_id)
        if not products:
            update.message.reply_text("📭 *No products being tracked*", parse_mode=ParseMode.MARKDOWN)
            return
        
        update.message.reply_text("🔍 *Checking products...*", parse_mode=ParseMode.MARKDOWN)
        
        results = []
        for product in products[:5]:  # Limit to 5 for performance
            try:
                # Check cache first
                cached = self.cache.get(product['asin'])
                if cached:
                    info = cached
                else:
                    info = self.scraper.scrape_product(product['asin'])
                    self.cache.set(product['asin'], info)
                
                # Update database
                if info['price']:
                    has_drop, drop_percent, old_price = self.db.check_price_drop(
                        product['id'], info['price']
                    )
                    self.db.update_product_status(product['id'], info['status'], info['price'])
                    
                    # Format result
                    if info['status'] == 'IN_STOCK' and info['price']:
                        price_text = f"💰 ₹{info['price']:,.0f}"
                        if has_drop:
                            price_text += f" 📉 ({drop_percent:.1f}%)"
                    else:
                        price_text = "💰 N/A"
                    
                    results.append(
                        f"▸ *{MessageFormatter.escape(product['title'][:50])}*\n"
                        f"  {self._status_emoji(info['status'])} {info['status']} · {price_text}"
                    )
                else:
                    self.db.update_product_status(product['id'], info['status'])
                    results.append(
                        f"▸ *{MessageFormatter.escape(product['title'][:50])}*\n"
                        f"  {self._status_emoji(info['status'])} {info['status']} · 💰 N/A"
                    )
                
                time.sleep(2)  # Small delay between checks
                
            except Exception as e:
                logger.error(f"Status check error for {product['asin']}: {e}")
                results.append(f"▸ *{MessageFormatter.escape(product['title'][:50])}*\n  ⚪ ERROR")
        
        msg = "╔══════════════════════════╗\n"
        msg += "║      📊 STATUS UPDATE     ║\n"
        msg += "╚══════════════════════════╝\n\n"
        msg += '\n\n'.join(results)
        
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    def cmd_remove(self, update: Update, context: CallbackContext):
        """Handle /remove command"""
        products = self.db.get_user_products(update.effective_user.id)
        
        if not products:
            update.message.reply_text("📭 *No products to remove*", parse_mode=ParseMode.MARKDOWN)
            return
        
        context.user_data['removing'] = True
        context.user_data['products'] = products
        
        lines = ["🗑 *Select product to remove:*\n"]
        for i, product in enumerate(products, 1):
            lines.append(f"{i}. {MessageFormatter.escape(product['title'][:50])}")
        
        lines.append("\n_Send the number_")
        update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.MARKDOWN)
    
    def cmd_help(self, update: Update, context: CallbackContext):
        """Handle /help command"""
        help_text = (
            "*🤖 Amazon Tracker Pro Help*\n\n"
            "*Commands:*\n"
            "• /start - Start the bot\n"
            "• /add - Add product to track\n"
            "• /list - Show tracked products\n"
            "• /status - Check current status\n"
            "• /remove - Remove product\n"
            "• /help - Show this help\n\n"
            "*Features:*\n"
            f"• Price drop alerts at {Config.PRICE_DROP_THRESHOLD}%+\n"
            f"• Stock change notifications\n"
            f"• Checks every {Config.PRODUCT_COOLDOWN_MINUTES} minutes\n"
            "• Tracks up to unlimited products\n\n"
            "*How to add:*\n"
            "Send any Amazon.in product link\n"
            "Example: `/add https://amazon.in/dp/B0CHX1W1XY`"
        )
        
        update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
    
    def handle_message(self, update: Update, context: CallbackContext):
        """Handle text messages"""
        user = update.effective_user
        
        # Rate limiting
        if not self.rate_limiter.is_allowed(user.id):
            wait = self.rate_limiter.get_wait_time(user.id)
            update.message.reply_text(
                f"⚠️ Too many requests. Please wait {wait} seconds.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Check if in remove mode
        if context.user_data.get('removing'):
            self._handle_remove_selection(update, context)
            return
        
        # Extract ASIN
        asin = AmazonScraper.extract_asin(update.message.text)
        if not asin:
            update.message.reply_text(
                "❌ *Invalid Amazon link*\n\n"
                "Please send a valid Amazon.in product URL.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Add user
        self.db.add_user(
            user.id,
            update.effective_chat.id,
            user.username,
            user.first_name,
            user.last_name
        )
        
        # Send typing action
        context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        
        # Fetch product info
        update.message.reply_text(f"🔍 Fetching `{asin}`...", parse_mode=ParseMode.MARKDOWN)
        
        # Check cache first
        cached = self.cache.get(asin)
        if cached:
            info = cached
        else:
            info = self.scraper.scrape_product(asin)
            self.cache.set(asin, info)
        
        # Add to database
        product_id = self.db.add_product(user.id, asin, info['title'], info['url'])
        
        # Update status and price
        if info['price']:
            self.db.update_product_status(product_id, info['status'], info['price'])
        else:
            self.db.update_product_status(product_id, info['status'])
        
        # Format response
        status_emoji = self._status_emoji(info['status'])
        price_text = f"💰 ₹{info['price']:,.0f}" if info['price'] else "💰 N/A"
        
        response = (
            "╔══════════════════════════╗\n"
            "║        ✅ ADDED           ║\n"
            "╚══════════════════════════╝\n\n"
            f"*{MessageFormatter.escape(info['title'][:100])}*\n\n"
            f"▸ {price_text}\n"
            f"▸ {status_emoji} {info['status']}\n\n"
            f"🔗 [View on Amazon]({MessageFormatter.escape(info['url'])})"
        )
        
        update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
    
    def _handle_remove_selection(self, update: Update, context: CallbackContext):
        """Handle product removal selection"""
        try:
            text = update.message.text.strip()
            if not text.isdigit():
                update.message.reply_text(
                    "❌ *Please send a valid number*",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            index = int(text) - 1
            products = context.user_data.get('products', [])
            
            if index < 0 or index >= len(products):
                update.message.reply_text(
                    "❌ *Invalid number*",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            
            product = products[index]
            success = self.db.remove_product(product['id'], update.effective_user.id)
            
            if success:
                update.message.reply_text(
                    f"✅ Removed: *{MessageFormatter.escape(product['title'][:50])}*",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                update.message.reply_text(
                    "❌ *Failed to remove product*",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            # Clear user data
            context.user_data.pop('removing', None)
            context.user_data.pop('products', None)
            
        except Exception as e:
            logger.error(f"Remove selection error: {e}")
            update.message.reply_text("❌ *Error removing product*", parse_mode=ParseMode.MARKDOWN)
            context.user_data.pop('removing', None)
            context.user_data.pop('products', None)
    
    def scheduled_check(self, context: CallbackContext):
        """Scheduled product check - runs every minute"""
        try:
            # Get products due for checking
            products = self.db.get_products_for_checking()
            
            if not products:
                logger.debug("No products due for checking")
                return
            
            logger.info(f"Checking {len(products)} products")
            
            for product in products:
                try:
                    self._check_single_product(product, context)
                    
                    # Random delay between checks
                    time.sleep(random.uniform(5, 10))
                    
                except Exception as e:
                    logger.error(f"Error checking product {product['id']}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Scheduled check error: {e}")
    
    def _check_single_product(self, product: Dict, context: CallbackContext):
        """Check a single product and send alerts if needed"""
        logger.info(f"Checking {product['asin']} - {product['title'][:50]}")
        
        # Scrape current info
        info = self.scraper.scrape_product(product['asin'])
        
        # Update cache
        self.cache.set(product['asin'], info)
        
        # Check for price drop
        if info['price']:
            has_drop, drop_percent, old_price = self.db.check_price_drop(
                product['id'], info['price']
            )
            
            # Update database
            self.db.update_product_status(product['id'], info['status'], info['price'])
            
            # Send price drop alert
            if has_drop and info['status'] == 'IN_STOCK':
                logger.info(f"💰 Price drop: {product['asin']} - {drop_percent:.1f}%")
                try:
                    context.bot.send_message(
                        chat_id=product['chat_id'],
                        text=MessageFormatter.price_alert(
                            product, old_price, info['price'], drop_percent
                        ),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Failed to send price alert: {e}")
        else:
            # Update without price
            self.db.update_product_status(product['id'], info['status'])
        
        # Check for stock change
        if product['last_status'] != info['status']:
            logger.info(f"📊 Stock change: {product['asin']} {product['last_status']} -> {info['status']}")
            
            if info['status'] == 'IN_STOCK' and product['last_status'] == 'OUT_OF_STOCK':
                try:
                    context.bot.send_message(
                        chat_id=product['chat_id'],
                        text=MessageFormatter.stock_alert(product, 'IN_STOCK'),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Failed to send stock alert: {e}")
            
            elif info['status'] == 'OUT_OF_STOCK' and product['last_status'] == 'IN_STOCK':
                try:
                    context.bot.send_message(
                        chat_id=product['chat_id'],
                        text=MessageFormatter.stock_alert(product, 'OUT_OF_STOCK'),
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Failed to send stock alert: {e}")
    
    def _status_emoji(self, status: str) -> str:
        """Get emoji for status"""
        return {
            'IN_STOCK': '🟢',
            'OUT_OF_STOCK': '🔴',
            'UNKNOWN': '⚪'
        }.get(status, '⚪')
    
    def error_handler(self, update: Update, context: CallbackContext):
        """Handle errors gracefully"""
        try:
            raise context.error
        except Conflict:
            logger.warning("Conflict error - another instance running?")
            time.sleep(5)
        except NetworkError:
            logger.warning("Network error - retrying...")
            time.sleep(10)
        except TimedOut:
            logger.warning("Timeout error")
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")

# ================= HEALTH SERVER =================
health_app = Flask(__name__)

@health_app.route('/')
def home():
    return "Amazon Tracker Pro Running", 200

@health_app.route('/health')
def health():
    return "OK", 200

def run_health_server():
    """Run health check server"""
    health_app.run(host='0.0.0.0', port=Config.PORT, debug=False, use_reloader=False)

# ================= MAIN =================
def main():
    """Main entry point"""
    try:
        # Validate configuration
        Config.validate()
        
        logger.info("=" * 60)
        logger.info("🚀 AMAZON TRACKER PRO")
        logger.info(f"✅ Price drop threshold: {Config.PRICE_DROP_THRESHOLD}%")
        logger.info(f"✅ Check interval: {Config.PRODUCT_COOLDOWN_MINUTES} minutes")
        logger.info(f"✅ Max products per run: {Config.MAX_PRODUCTS_PER_RUN}")
        logger.info("=" * 60)
        
        # Start health server
        health_thread = threading.Thread(target=run_health_server, daemon=True)
        health_thread.start()
        logger.info(f"✅ Health server on port {Config.PORT}")
        
        # Create and start bot
        bot = AmazonBot(Config.BOT_TOKEN)
        
        # Handle shutdown signals
        def signal_handler(sig, frame):
            logger.info("Shutting down...")
            bot.stop()
            sys.exit(0)
        
        import signal
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start bot
        bot.start()
        
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
