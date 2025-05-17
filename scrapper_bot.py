import os
import json
import time
import logging
import random
import requests
import re
from apify_client import ApifyClient
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.error import TelegramError
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from urllib.parse import quote
import aiohttp
import traceback
import asyncio
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from typing import Dict, List, Tuple, Any, Optional, Union

# Конфигурация логирования
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Настройки
DEFAULT_CHECK_INTERVAL = 600  # секунд (10 минут)
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
TWITTER_BEARER = os.getenv("TWITTER_BEARER", "")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

# Обновленный список Nitter-инстансов с рабочими серверами
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.fdn.fr",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.unixfox.eu",
    "https://nitter.domain.glass",
    "https://nitter.net",
    "https://nitter.lacontrevoie.fr",
    "https://nitter.pussthecat.org"
]

# Пути к файлам
DATA_DIR = "data"
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
API_LIMITS_FILE = os.path.join(DATA_DIR, "api_limits.json")
PROXIES_FILE = os.path.join(DATA_DIR, "proxies.json")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")

# Создаем директорию, если её нет
os.makedirs(DATA_DIR, exist_ok=True)


class HTMLSession:
    def __init__(self):
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--start-maximized")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")

        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )
        self.driver.implicitly_wait(10)

    def get(self, url, proxies=None, timeout=30):
        try:
            self.driver.get(url)
            # Ждем загрузку контента
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            # Явно ждем для загрузки динамического JS контента
            time.sleep(3)
            return self
        except Exception as e:
            logger.error(f"Error loading page {url}: {e}")
            return self

    @property
    def html(self):
        return self.driver

    def close(self):
        try:
            self.driver.quit()
        except:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Утилиты для работы с JSON
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_accounts(accounts_data):
    """Сохраняет данные аккаунтов в JSON файл"""
    save_json(ACCOUNTS_FILE, accounts_data)


def get_cache():
    """Загружает кеш из файла"""
    cache = load_json(CACHE_FILE, {"tweets": {}, "users": {}, "timestamp": int(time.time())})

    # Очистка устаревших данных кеша (старше суток)
    current_time = int(time.time())
    day_ago = current_time - 86400

    # Очистка кеша твитов
    tweets_cache = cache.get("tweets", {})
    for username, data in list(tweets_cache.items()):
        if data.get("timestamp", 0) < day_ago:
            del tweets_cache[username]

    # Очистка кеша пользователей
    users_cache = cache.get("users", {})
    for username, data in list(users_cache.items()):
        if data.get("timestamp", 0) < day_ago:
            del users_cache[username]

    cache["timestamp"] = current_time
    return cache


def update_cache(category, key, data):
    """Обновляет кеш данными"""
    cache = get_cache()

    if category not in cache:
        cache[category] = {}

    cache[category][key] = {
        "data": data,
        "timestamp": int(time.time())
    }

    save_json(CACHE_FILE, cache)


def get_from_cache(category, key, max_age=3600):
    """Получает данные из кеша, если не устарели"""
    cache = get_cache()

    if category in cache and key in cache[category]:
        item = cache[category][key]
        if int(time.time()) - item.get("timestamp", 0) < max_age:
            return item.get("data")

    return None


# Управление настройками
def get_settings():
    return load_json(SETTINGS_FILE, {
        "check_interval": DEFAULT_CHECK_INTERVAL,
        "enabled": True,
        "use_proxies": False,
        "scraper_methods": ["api", "apify", "web", "nitter"] if APIFY_API_TOKEN else ["api", "web", "nitter"],
        "max_retries": 3,
        "cache_expiry": 3600,
        "randomize_intervals": True,
        "min_interval_factor": 0.8,
        "max_interval_factor": 1.2,
        "parallel_checks": 3,
        "nitter_instances": NITTER_INSTANCES,
        "health_check_interval": 3600,  # Интервал проверки здоровья инстансов (1 час)
        "last_health_check": 0  # Время последней проверки инстансов
    })


def update_setting(key, value):
    settings = get_settings()
    settings[key] = value
    save_json(SETTINGS_FILE, settings)
    return settings


# Управление прокси
def get_proxies():
    return load_json(PROXIES_FILE, {"proxies": []})


def get_random_proxy():
    proxies_data = get_proxies()
    proxy_list = proxies_data.get("proxies", [])

    if not proxy_list:
        return None

    proxy = random.choice(proxy_list)
    if proxy.startswith("http"):
        return {"http": proxy, "https": proxy}
    else:
        return {"http": f"http://{proxy}", "https": f"http://{proxy}"}


# Инициализация данных аккаунтов
def init_accounts():
    """Инициализирует или мигрирует данные аккаунтов"""
    try:
        if not os.path.exists(ACCOUNTS_FILE):
            save_json(ACCOUNTS_FILE, {})
            return {}

        accounts = load_json(ACCOUNTS_FILE, {})

        if isinstance(accounts, list):
            logger.info("Мигрируем аккаунты из списка в словарь")
            new_accounts = {}
            for account in accounts:
                username = account.get("username", "")
                if username:
                    new_accounts[username.lower()] = {
                        "username": username,
                        "added_at": account.get("added_at", datetime.now().isoformat()),
                        "last_check": account.get("last_check"),
                        "last_tweet_id": None,
                        "check_count": 0,
                        "success_rate": 100.0,
                        "fail_count": 0,
                        "check_method": None,
                        "priority": 1.0,
                        "first_check": True
                    }
            save_json(ACCOUNTS_FILE, new_accounts)
            return new_accounts

        updated = False
        for username, account in accounts.items():
            if "check_count" not in account:
                account["check_count"] = 0
                updated = True
            if "success_rate" not in account:
                account["success_rate"] = 100.0
                updated = True
            if "fail_count" not in account:
                account["fail_count"] = 0
                updated = True
            if "check_method" not in account:
                account["check_method"] = None
                updated = True
            if "priority" not in account:
                account["priority"] = 1.0
                updated = True
            if "first_check" not in account:
                account["first_check"] = True
                updated = True
            if "last_tweet_text" not in account:
                account["last_tweet_text"] = ""
                updated = True
            if "last_tweet_url" not in account:
                account["last_tweet_url"] = ""
                updated = True

        if updated:
            save_json(ACCOUNTS_FILE, accounts)

        return accounts
    except Exception as e:
        logger.error(f"Ошибка при инициализации данных аккаунтов: {e}")
        save_json(ACCOUNTS_FILE, {})
        return {}


async def check_instance(session, instance):
    """Проверяет доступность Nitter-инстанса"""
    try:
        async with session.get(
                f"{instance}/twitter",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
        ) as response:
            if response.status != 200:
                return False

            # Проверка содержимого страницы, чтобы убедиться, что это работающий инстанс
            page_content = await response.text()
            return 'twitter' in page_content.lower() and len(page_content) > 1000
    except:
        return False


async def update_nitter_instances():
    """Проверяет и обновляет список рабочих Nitter-инстансов"""
    working_instances = []

    async with aiohttp.ClientSession() as session:
        tasks = []
        for instance in NITTER_INSTANCES:
            tasks.append(check_instance(session, instance))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for instance, is_working in zip(NITTER_INSTANCES, results):
            if is_working and not isinstance(is_working, Exception):
                working_instances.append(instance)
                logger.info(f"Nitter instance available: {instance}")

    if not working_instances:
        logger.warning("No Nitter instances available, using the default list")
        working_instances = NITTER_INSTANCES[:3]  # Берем хотя бы первые 3 инстанса по умолчанию

    settings = get_settings()
    settings["nitter_instances"] = working_instances
    settings["last_health_check"] = int(time.time())
    save_json(SETTINGS_FILE, settings)

    return working_instances


# Методы для работы с Twitter
class TwitterClient:
    def __init__(self, bearer_token):
        self.bearer_token = bearer_token
        self.rate_limited = False
        self.rate_limit_reset = 0
        self.user_agent = UserAgent().random
        self.cache = {}
        self.session = requests.Session()

    def clear_cache(self):
        self.cache = {}

    def update_user_agent(self):
        self.user_agent = UserAgent().random

    def check_rate_limit(self):
        if self.rate_limited:
            now = time.time()
            if now < self.rate_limit_reset:
                return False
            else:
                self.rate_limited = False
        return True

    def set_rate_limit(self, reset_time):
        self.rate_limited = True
        self.rate_limit_reset = reset_time

        # Сохраняем информацию о лимите в файл
        limits = load_json(API_LIMITS_FILE, {})
        limits["twitter_api"] = {
            "rate_limited": True,
            "reset_time": reset_time,
            "updated_at": int(time.time())
        }
        save_json(API_LIMITS_FILE, limits)

    def get_cache_key(self, method, identifier):
        return f"{method}:{identifier}"

    def get_cached_data(self, cache_key, max_age=3600):
        if cache_key in self.cache:
            item = self.cache[cache_key]
            if time.time() - item['timestamp'] < max_age:
                return item['data']
        return None

    def set_cache(self, cache_key, data):
        self.cache[cache_key] = {
            'data': data,
            'timestamp': time.time()
        }

    def get_user_by_username(self, username):
        if not self.bearer_token or not self.check_rate_limit():
            return None

        # Проверяем кеш пользователей
        cached_user = get_from_cache("users", username.lower(), 86400)  # Кеш на сутки
        if cached_user:
            return cached_user

        url = f"https://api.twitter.com/2/users/by/username/{username}"
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": self.user_agent
        }

        try:
            response = self.session.get(url, headers=headers, timeout=10)

            if response.status_code == 429:
                reset_time = int(response.headers.get("x-rate-limit-reset", time.time() + 900))
                self.set_rate_limit(reset_time)
                remaining = int(response.headers.get("x-rate-limit-remaining", 0))
                limit = int(response.headers.get("x-rate-limit-limit", 0))
                logger.warning(
                    f"API лимит пользователей: {remaining}/{limit}. Сброс в {reset_time}"
                )
                return None

            if response.status_code == 200:
                data = response.json()
                if "data" in data:
                    # Обновляем кеш
                    update_cache("users", username.lower(), data["data"])
                    return data["data"]
            else:
                logger.error(f"Ошибка при получении пользователя: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"Ошибка запроса к API: {e}")

        return None

    def get_user_tweets(self, user_id, use_proxies=False):
        if not self.bearer_token or not self.check_rate_limit():
            return None

        cache_key = self.get_cache_key("tweets", user_id)
        cached = self.get_cached_data(cache_key, 300)
        if cached:
            return cached

        url = f"https://api.twitter.com/2/users/{user_id}/tweets"
        params = {
            "max_results": 5,
            "tweet.fields": "created_at,text",
            "exclude": "retweets,replies"
        }
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": self.user_agent
        }

        try:
            proxies = get_random_proxy() if use_proxies else None
            response = self.session.get(url, headers=headers, params=params, proxies=proxies, timeout=10)

            if response.status_code == 429:
                reset_time = int(response.headers.get("x-rate-limit-reset", time.time() + 900))
                self.set_rate_limit(reset_time)
                remaining = int(response.headers.get("x-rate-limit-remaining", 0))
                limit = int(response.headers.get("x-rate-limit-limit", 0))
                logger.warning(
                    f"API лимит твитов: {remaining}/{limit}. Сброс в {reset_time}"
                )
                return None

            if response.status_code == 200:
                data = response.json()
                tweets = data.get("data", [])
                self.set_cache(cache_key, tweets)
                return tweets
            else:
                logger.error(f"Ошибка при получении твитов: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"Ошибка запроса к API: {e}")

        return None

    def get_latest_tweet(self, username, use_proxies=False):
        # Проверяем кеш твитов
        cached_tweet = get_from_cache("tweets", username.lower(), 300)  # Кеш на 5 минут
        if cached_tweet:
            return cached_tweet.get("user_id"), cached_tweet.get("tweet_id"), cached_tweet.get("tweet_data")

        user = self.get_user_by_username(username)
        if not user:
            return None, None, None

        user_id = user["id"]
        tweets = self.get_user_tweets(user_id, use_proxies)

        if not tweets or len(tweets) == 0:
            return user_id, None, None

        latest = tweets[0]
        tweet_id = latest["id"]
        tweet_text = latest["text"]
        tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

        # Обновляем кеш
        tweet_data = {"text": tweet_text, "url": tweet_url}
        update_cache("tweets", username.lower(), {
            "user_id": user_id,
            "tweet_id": tweet_id,
            "tweet_data": tweet_data
        })

        return user_id, tweet_id, tweet_data


# Скраперы для получения твитов
class TwitterScrapers:
    def __init__(self):
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:96.0) Gecko/20100101 Firefox/96.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
        ]
        self.cache = {}
        self.session = requests.Session()
        self.async_session = None
        self.nitter_failures = {}  # Счетчик неудачных попыток для каждого инстанса

    def get_random_user_agent(self):
        return random.choice(self.user_agents)

    def get_cache_key(self, method, username):
        return f"{method}:{username.lower()}"

    def get_cached_data(self, cache_key, max_age=300):
        if cache_key in self.cache:
            item = self.cache[cache_key]
            if time.time() - item['timestamp'] < max_age:
                return item['data']
        return None

    def set_cache(self, cache_key, data):
        self.cache[cache_key] = {
            'data': data,
            'timestamp': time.time()
        }

    async def init_async_session(self):
        if self.async_session is None:
            self.async_session = aiohttp.ClientSession()

    async def close_async_session(self):
        if self.async_session:
            await self.async_session.close()
            self.async_session = None

    def validate_tweet_id(self, username, tweet_id):
        if not tweet_id:
            return False
        if len(str(tweet_id)) < 15:
            logger.warning(f"Слишком короткий ID твита для @{username}: {tweet_id}")
            return False
        return True

    def report_nitter_failure(self, instance):
        if instance not in self.nitter_failures:
            self.nitter_failures[instance] = 0
        self.nitter_failures[instance] += 1

    def get_healthy_nitter_instances(self, max_failures=3):
        settings = get_settings()
        nitter_instances = settings.get("nitter_instances", NITTER_INSTANCES)

        # Если прошло больше часа с последней проверки, обновляем инстансы
        current_time = int(time.time())
        last_check = settings.get("last_health_check", 0)
        health_check_interval = settings.get("health_check_interval", 3600)

        if current_time - last_check > health_check_interval:
            # Запустим асинхронное обновление в фоне
            asyncio.create_task(update_nitter_instances())

        # Отфильтруем инстансы с большим количеством неудач
        healthy_instances = [
            instance for instance in nitter_instances
            if self.nitter_failures.get(instance, 0) < max_failures
        ]

        # Если все инстансы имеют слишком много неудач, сбросим счетчики и используем все
        if not healthy_instances:
            self.nitter_failures = {}
            healthy_instances = nitter_instances

        # Перемешиваем для равномерной нагрузки
        random.shuffle(healthy_instances)
        return healthy_instances

    async def get_latest_tweet_apify(self, username, use_proxies=False):
        """Получает последний твит через Apify API"""
        cache_key = self.get_cache_key("apify", username)
        cached = self.get_cached_data(cache_key)
        if cached:
            return cached

        if not APIFY_API_TOKEN:
            return None, None

        try:
            # Проверяем кеш твитов
            cached_tweet = get_from_cache("tweets", f"apify_{username.lower()}", 1800)  # Кеш на 30 минут для Apify
            if cached_tweet:
                return cached_tweet.get("tweet_id"), cached_tweet.get("tweet_data")

            apify_client = ApifyClient(APIFY_API_TOKEN)

            # Исправленная структура запроса
            run_input = {
                "usernames": [username],
                "maxTweets": 1,
                "includeReplies": False,
                "includeRetweets": False,
                # Правильный формат для startUrls
                "startUrls": [{"url": f"https://twitter.com/{username}"}],
                # Корректное значение proxyConfig, которое требуется Apify
                "proxyConfig": {
                    "useApifyProxy": True,  # Всегда True, это требование API
                    # Используем RESIDENTIAL для реальных прокси, AUTOMATIC для тестовых/бесплатных
                    "apifyProxyGroups": ["RESIDENTIAL"] if use_proxies else ["AUTOMATIC"]
                }
            }

            # Синхронный вызов API
            run = apify_client.actor("quacker/twitter-scraper").call(run_input=run_input)

            # Проверка успешности запуска
            if not run or not run.get("defaultDatasetId"):
                logger.warning(f"Apify run failed for {username}")
                return None, None

            # Получение результатов
            items = apify_client.dataset(run["defaultDatasetId"]).list_items().items

            # Обработка результатов... [оставьте остальной код без изменений]

            if items and len(items) > 0:
                for item in items:
                    # Убеждаемся, что это не ретвит и не ответ
                    if item.get("isReply") or item.get("isRetweet"):
                        continue

                    tweet_id = item["id"]
                    tweet_text = item.get("text", "[Текст недоступен]")
                    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

                    # Обновляем кеш
                    tweet_data = {"text": tweet_text, "url": tweet_url}
                    update_cache("tweets", f"apify_{username.lower()}", {
                        "tweet_id": tweet_id,
                        "tweet_data": tweet_data
                    })

                    result = (tweet_id, {"text": tweet_text, "url": tweet_url})
                    self.set_cache(cache_key, result)
                    logger.info(f"Найден твит ID {tweet_id} для @{username} через Apify")
                    return result

        except Exception as e:
            logger.error(f"Apify error for @{username}: {e}")

        return None, None

    def get_latest_tweet_web(self, username, use_proxies=False):
        """Улучшенный веб-парсинг с Selenium для получения свежих твитов и игнорирования закрепленных"""
        cache_key = self.get_cache_key("web", username)
        cached = self.get_cached_data(cache_key)
        if cached:
            return cached

        # Проверяем кеш твитов
        cached_tweet = get_from_cache("tweets", f"web_{username.lower()}", 600)  # Кеш на 10 минут
        if cached_tweet:
            return cached_tweet.get("tweet_id"), cached_tweet.get("tweet_data")

        try:
            session = HTMLSession()
            url = f"https://twitter.com/{username}"

            # Добавляем случайный user agent
            session.driver.execute_cdp_cmd('Network.setUserAgentOverride',
                                           {"userAgent": self.get_random_user_agent()})

            session.get(url)

            # Ждем загрузку твитов (используем явный WebDriverWait)
            try:
                WebDriverWait(session.driver, 20).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'article[data-testid="tweet"], div[data-testid="tweetText"]'))
                )
            except:
                logger.warning(f"Не удалось найти твиты на странице @{username}")

            # Прокручиваем страницу для загрузки контента
            session.driver.execute_script("window.scrollTo(0, 1000)")
            time.sleep(3)  # Увеличиваем время ожидания

            # Сделаем снимок экрана для отладки
            debug_screenshot = f"debug_{username}_{int(time.time())}.png"
            try:
                session.driver.save_screenshot(debug_screenshot)
                logger.info(f"Снимок экрана сохранен в {debug_screenshot}")
            except Exception as e:
                logger.warning(f"Не удалось сохранить снимок экрана: {e}")

            # Собираем ВСЕ твиты для анализа - улучшенная версия с отладкой
            all_tweets_data = session.driver.execute_script(r"""
                // Выводим диагностическую информацию
                console.log("URL страницы:", window.location.href);
                console.log("Загрузка DOM:", document.readyState);
                console.log("Видимость страницы:", document.visibilityState);

                // Функция для надежного извлечения времени твита
                const findTimestamp = (article) => {
                    // Проверяем все элементы time
                    const timeElements = article.querySelectorAll('time');
                    for (let timeElement of timeElements) {
                        const datetime = timeElement.getAttribute('datetime');
                        if (datetime) {
                            return datetime;
                        }
                    }

                    // Пробуем альтернативный метод - ищем атрибуты с датами
                    const possibleDateAttrs = ['data-time', 'data-time-ms', 'data-date'];
                    for (let attr of possibleDateAttrs) {
                        const elements = article.querySelectorAll(`[${attr}]`);
                        if (elements.length > 0) {
                            const value = elements[0].getAttribute(attr);
                            if (value) return value;
                        }
                    }

                    return '';
                };

                // Пробуем разные селекторы для поиска твитов
                let articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
                console.log("Найдено твитов (основной селектор):", articles.length);

                if (articles.length === 0) {
                    // Пробуем альтернативные селекторы
                    articles = Array.from(document.querySelectorAll('[aria-labelledby][role="article"]'));
                    console.log("Найдено твитов (альтернативный селектор 1):", articles.length);

                    if (articles.length === 0) {
                        // Еще один альтернативный селектор
                        articles = Array.from(document.querySelectorAll('div[data-testid="cellInnerDiv"] > div'));
                        console.log("Найдено потенциальных твитов (альтернативный селектор 2):", articles.length);
                    }
                }

                const results = [];

                // Обработка найденных твитов через селекторы
                if (articles.length > 0) {
                    for (let i = 0; i < articles.length; i++) {
                        const article = articles[i];
                        let tweetData = {
                            id: null,
                            text: '',
                            isPinned: false,
                            isRetweet: false,
                            hasMedia: false,
                            timestamp: '',
                            timeElement: null
                        };

                        // Проверка на закрепленный твит и ретвиты
                        const socialContext = article.querySelector('[data-testid="socialContext"]');
                        if (socialContext && socialContext.textContent) {
                            const text = socialContext.textContent.toLowerCase();
                            tweetData.isPinned = text.includes('pinned') || text.includes('закрепл');
                            tweetData.isRetweet = text.includes('retweeted') || text.includes('ретвит');
                        }

                        // Дополнительная проверка на закрепленные
                        if (!tweetData.isPinned) {
                            const pinnedIndicator = article.querySelector('.pinned, [data-is-pinned="true"], svg[data-testid="icon-pin"]');
                            if (pinnedIndicator) {
                                tweetData.isPinned = true;
                            }
                        }

                        // Получаем ID твита из ссылок
                        const links = article.querySelectorAll('a[href*="/status/"]');
                        for (let j = 0; j < links.length; j++) {
                            const href = links[j].getAttribute('href');
                            const match = href.match(/\/status\/(\d+)/);
                            if (match && match[1]) {
                                tweetData.id = match[1];
                                break;
                            }
                        }

                        // Получаем временную метку твита
                        tweetData.timestamp = findTimestamp(article);

                        // Получаем текст твита
                        const textDiv = article.querySelector('div[data-testid="tweetText"]');
                        if (textDiv) {
                            tweetData.text = textDiv.innerText;
                        } else {
                            // Альтернативный поиск текста
                            const altTextDiv = article.querySelector('[dir="auto"][lang]');
                            if (altTextDiv) {
                                tweetData.text = altTextDiv.innerText;
                            }
                        }

                        // Проверяем наличие медиа
                        const mediaElements = article.querySelectorAll('[data-testid="tweetPhoto"], [data-testid="videoPlayer"]');
                        tweetData.hasMedia = mediaElements.length > 0;

                        // Добавляем результат если нашли ID твита
                        if (tweetData.id) {
                            results.push(tweetData);
                        }
                    }
                }

                // Запасной метод - поиск твитов через статусные ссылки
                if (results.length === 0) {
                    const statusLinks = document.querySelectorAll('a[href*="/status/"]');
                    console.log("Найдено статусных ссылок:", statusLinks.length);

                    for (let i = 0; i < statusLinks.length; i++) {
                        const link = statusLinks[i];
                        const href = link.getAttribute('href');
                        const match = href.match(/\/status\/(\d+)/);

                        if (match && match[1]) {
                            // Находим родительский контейнер ссылки
                            const container = link.closest('div[role="article"]') || 
                                              link.closest('div[data-testid]') || 
                                              link.parentElement;

                            let tweetText = '';
                            let isPinned = false;

                            if (container) {
                                // Ищем текст твита
                                const textEl = container.querySelector('div[dir="auto"]') ||
                                              container.querySelector('span[data-text]');

                                if (textEl) {
                                    tweetText = textEl.innerText;
                                }

                                // Проверяем на закрепленный статус
                                const pinnedEl = container.querySelector('.pinned, [data-testid="socialContext"]');
                                if (pinnedEl && pinnedEl.textContent) {
                                    isPinned = pinnedEl.textContent.toLowerCase().includes('pinned') ||
                                               pinnedEl.textContent.toLowerCase().includes('закрепл');
                                }
                            }

                            results.push({
                                id: match[1],
                                text: tweetText,
                                isPinned: isPinned,
                                isRetweet: false,
                                hasMedia: false,
                                timestamp: findTimestamp(link.parentElement || document) || ''
                            });
                        }
                    }
                }

                console.log("Итоговое количество найденных твитов:", results.length);
                return results;
            """)

            logger.info(f"Найдено {len(all_tweets_data) if all_tweets_data else 0} твитов для @{username}")

            # Добавляем отладочный лог для анализа твитов
            if all_tweets_data and len(all_tweets_data) > 0:
                logger.info(f"Анализ твитов для @{username}:")
                for i, tweet in enumerate(all_tweets_data):
                    logger.info(f"Твит #{i + 1}: ID={tweet.get('id')}, Время={tweet.get('timestamp', 'нет')}, " +
                                f"Закреплен={tweet.get('isPinned')}, Ретвит={tweet.get('isRetweet')}")

            # Если нашли твиты, выбираем самый подходящий
            selected_tweet = None

            if all_tweets_data and len(all_tweets_data) > 0:
                # 1. Отфильтруем закрепленные твиты
                non_pinned_tweets = [t for t in all_tweets_data if not t.get('isPinned')]
                logger.info(f"Найдено не закрепленных твитов: {len(non_pinned_tweets)}")

                # 2. Отфильтруем ретвиты среди не закрепленных
                regular_tweets = [t for t in non_pinned_tweets if not t.get('isRetweet')]
                logger.info(f"Найдено обычных твитов (не ретвитов): {len(regular_tweets)}")

                # 3. Выбираем твит по приоритету
                if regular_tweets:
                    # Берем первый твит как базовый для сравнения
                    newest_tweet = regular_tweets[0]
                    newest_timestamp = newest_tweet.get('timestamp', '')

                    # Проверяем каждый твит и выбираем самый свежий
                    for tweet in regular_tweets:
                        current_timestamp = tweet.get('timestamp', '')

                        # Если у текущего твита нет timestamp, пропускаем
                        if not current_timestamp:
                            continue

                        # Если у самого нового твита нет timestamp, берем текущий
                        if not newest_timestamp:
                            newest_tweet = tweet
                            newest_timestamp = current_timestamp
                        # Сравниваем таймстампы (больше = новее)
                        elif current_timestamp > newest_timestamp:
                            newest_tweet = tweet
                            newest_timestamp = current_timestamp

                    selected_tweet = newest_tweet
                    logger.info(f"Выбран самый свежий твит от {newest_timestamp} для @{username}")

                elif non_pinned_tweets:
                    # Если нет обычных твитов, берем ретвит, но не закрепленный
                    selected_tweet = non_pinned_tweets[0]
                    logger.info(f"Выбран ретвит (нет обычных твитов) для @{username}")
                else:
                    # В крайнем случае берем закрепленный
                    selected_tweet = all_tweets_data[0]
                    logger.info(f"Выбран закрепленный твит (других нет) для @{username}")

            session.close()

            # Обработка выбранного твита
            if selected_tweet and selected_tweet.get('id'):
                tweet_id = selected_tweet.get('id')
                tweet_text = selected_tweet.get('text', '[Текст недоступен]')

                if len(str(tweet_id)) < 15:
                    logger.warning(f"Подозрительно короткий ID твита: {tweet_id} для @{username}")
                    return None, None

                tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

                # Обновляем кеш
                tweet_data = {
                    "text": tweet_text,
                    "url": tweet_url,
                    "is_pinned": selected_tweet.get('isPinned', False),
                    "has_media": selected_tweet.get('hasMedia', False)
                }

                update_cache("tweets", f"web_{username.lower()}", {
                    "tweet_id": tweet_id,
                    "tweet_data": tweet_data
                })

                result = (tweet_id, tweet_data)
                self.set_cache(cache_key, result)
                logger.info(f"Найден твит ID {tweet_id} для @{username} через веб-парсинг")
                return result

        except Exception as e:
            logger.error(f"Web scraping error for @{username}: {e}")
            traceback.print_exc()

        return None, None

    def get_latest_tweet_nitter(self, username, use_proxies=False):
        """Получает последний твит через Nitter с улучшенной обработкой ошибок"""
        cache_key = self.get_cache_key("nitter", username)
        cached = self.get_cached_data(cache_key)
        if cached:
            return cached

        # Проверяем кеш твитов
        cached_tweet = get_from_cache("tweets", f"nitter_{username.lower()}", 600)  # Кеш на 10 минут
        if cached_tweet:
            return cached_tweet.get("tweet_id"), cached_tweet.get("tweet_data")

        # Получаем здоровые инстансы Nitter
        healthy_instances = self.get_healthy_nitter_instances()

        for base_url in healthy_instances[:3]:  # Пробуем только первые 3 инстанса
            url = f"{base_url}/{username}"
            headers = {
                "User-Agent": self.get_random_user_agent(),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }

            try:
                proxies = get_random_proxy() if use_proxies else None
                response = self.session.get(url, headers=headers, proxies=proxies, timeout=15)

                # После получения HTML страницы с Nitter
                soup = BeautifulSoup(response.text, 'html.parser')

                # Проверяем, является ли первый твит закрепленным
                pinned_tweet = soup.select_one('.pinned')
                if pinned_tweet:
                    logger.info(f"Игнорирование закрепленного твита для @{username} на Nitter")
                    # Ищем следующий твит (не закрепленный)
                    next_tweet = soup.select_one('.timeline-item:not(.pinned)')

                    if next_tweet_data and next_tweet_data.get('id'):
                        tweet_id = next_tweet_data['id']
                        tweet_text = next_tweet_data.get('text', '')
                        logger.info(f"Найден следующий твит ID {tweet_id} для @{username} через веб-парсинг")
                    else:
                        logger.info(f"Не удалось найти твиты после закрепленного для @{username} на Nitter")

                if response.status_code != 200:
                    self.report_nitter_failure(base_url)
                    continue

                soup = BeautifulSoup(response.text, "html.parser")

                # Проверка на заблокированную страницу
                error_msg = soup.select_one(".error-panel")
                if error_msg and "not found" in error_msg.get_text(strip=True).lower():
                    logger.warning(f"Nitter reports that account @{username} is not found")
                    return None, None

                # Проверка на существование контейнера с твитами
                timeline = soup.select_one(".timeline")
                if not timeline:
                    self.report_nitter_failure(base_url)
                    continue

                # Ищем все элементы твитов
                timeline_items = soup.select(".timeline-item")

                if not timeline_items:
                    self.report_nitter_failure(base_url)
                    continue

                for item in timeline_items:
                    # Проверяем, что это не закрепленный твит и не ретвит
                    pinned_icon = item.select_one(".pinned")
                    if pinned_icon:
                        continue

                    # Проверяем на ретвит
                    tweet_header = item.select_one(".tweet-header")
                    if tweet_header:
                        header_text = tweet_header.get_text(strip=True).lower()
                        if "retweeted" in header_text or "ретвитнул" in header_text:
                            continue

                    # Ищем ссылку на твит
                    link = item.select_one(".tweet-link")
                    if not link or "href" not in link.attrs:
                        continue

                    href = link["href"]
                    match = re.search(r'/status/(\d+)', href)
                    if not match:
                        continue

                    tweet_id = match.group(1)

                    # Проверяем валидность ID
                    if not self.validate_tweet_id(username, tweet_id):
                        continue

                    # Получаем текст твита
                    content = item.select_one(".tweet-content")
                    tweet_text = content.get_text(strip=True) if content else "[Новый твит]"
                    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

                    # Обновляем кеш
                    tweet_data = {"text": tweet_text, "url": tweet_url}
                    update_cache("tweets", f"nitter_{username.lower()}", {
                        "tweet_id": tweet_id,
                        "tweet_data": tweet_data
                    })

                    result = (tweet_id, {"text": tweet_text, "url": tweet_url})
                    self.set_cache(cache_key, result)
                    return result

                # Если прошли все твиты и не нашли подходящий (например, есть только ретвиты),
                # пробуем взять первый твит в ленте
                if timeline_items:
                    item = timeline_items[0]
                    link = item.select_one(".tweet-link")
                    if link and "href" in link.attrs:
                        href = link["href"]
                        match = re.search(r'/status/(\d+)', href)
                        if match:
                            tweet_id = match.group(1)

                            # Проверяем валидность ID
                            if not self.validate_tweet_id(username, tweet_id):
                                continue

                            content = item.select_one(".tweet-content")
                            tweet_text = content.get_text(strip=True) if content else "[Твит]"
                            tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

                            # Обновляем кеш
                            tweet_data = {"text": tweet_text, "url": tweet_url}
                            update_cache("tweets", f"nitter_{username.lower()}", {
                                "tweet_id": tweet_id,
                                "tweet_data": tweet_data
                            })

                            result = (tweet_id, {"text": tweet_text, "url": tweet_url})
                            self.set_cache(cache_key, result)
                            return result

            except Exception as e:
                logger.error(f"Ошибка при получении твита через Nitter ({base_url}) для {username}: {e}")
                self.report_nitter_failure(base_url)

        return None, None

    async def get_latest_tweet_multi(self, username, methods=None, use_proxies=False):
        """Пытается получить последний твит разными методами одновременно"""
        await self.init_async_session()

        if not methods:
            methods = ["apify", "nitter", "web"] if APIFY_API_TOKEN else ["nitter", "web"]

        tasks = []

        for method in methods:
            if method == "apify" and APIFY_API_TOKEN:
                tasks.append(asyncio.create_task(self.get_latest_tweet_apify(username, use_proxies)))
            elif method == "nitter":
                tasks.append(asyncio.create_task(self.get_latest_tweet_nitter_async(username, use_proxies)))
            elif method == "web":
                tasks.append(asyncio.create_task(self.get_latest_tweet_web_async(username, use_proxies)))

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

        result = None
        for task in done:
            try:
                method_result = task.result()
                if method_result and method_result[0]:
                    result = method_result
                    break
            except Exception as e:
                logger.error(f"Ошибка в асинхронной задаче для {username}: {e}")

        return result

    async def get_latest_tweet_web_async(self, username, use_proxies=False):
        return self.get_latest_tweet_web(username, use_proxies)

    async def get_latest_tweet_nitter_async(self, username, use_proxies=False):
        return self.get_latest_tweet_nitter(username, use_proxies)


# Многометодная проверка твитов
async def check_tweet_multi_method(username, methods=None, use_proxies=False, max_retries=2):
    """Проверяет твиты всеми доступными методами с улучшенной логикой повторов"""
    if not methods:
        settings = get_settings()
        methods = settings.get("scraper_methods",
                               ["api", "apify", "web", "nitter"] if APIFY_API_TOKEN else ["api", "web", "nitter"])

    twitter_api = TwitterClient(TWITTER_BEARER)
    scrapers = TwitterScrapers()

    # Очищаем кеш для конкретного пользователя
    scrapers.cache = {k: v for k, v in scrapers.cache.items() if not k.lower().endswith(username.lower())}

    user_id = None
    tweet_id = None
    tweet_data = None
    successful_method = None
    retry_count = 0

    # Первый проход - проверяем все методы по приоритету
    for method in methods:
        if tweet_id:
            break

        try:
            if method == "api" and TWITTER_BEARER and not twitter_api.rate_limited:
                user_id, tweet_id, tweet_data = twitter_api.get_latest_tweet(username, use_proxies)
                if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                    successful_method = "api"

            elif method == "apify" and APIFY_API_TOKEN:
                tweet_id, tweet_data = await scrapers.get_latest_tweet_apify(username, use_proxies)
                if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                    successful_method = "apify"

            elif method == "nitter":
                tweet_id, tweet_data = scrapers.get_latest_tweet_nitter(username, use_proxies)
                if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                    successful_method = "nitter"

            elif method == "web":
                tweet_id, tweet_data = scrapers.get_latest_tweet_web(username, use_proxies)
                if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                    successful_method = "web"

        except Exception as e:
            logger.error(f"Ошибка при проверке {username} методом {method}: {e}")

    # Если ни один из методов не сработал, повторяем попытки с большим ожиданием
    while not tweet_id and retry_count < max_retries:
        retry_count += 1
        logger.info(f"Повторная попытка {retry_count} для @{username}")

        # Небольшая задержка перед повторной попыткой
        await asyncio.sleep(2 * retry_count)

        # Меняем приоритеты методов для повторных попыток
        if "web" in methods and methods[0] != "web":
            methods = ["web"] + [m for m in methods if m != "web"]

        # Повторяем попытки
        for method in methods:
            try:
                if method == "api" and TWITTER_BEARER and not twitter_api.rate_limited:
                    user_id, tweet_id, tweet_data = twitter_api.get_latest_tweet(username, use_proxies)
                    if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                        successful_method = "api"
                        break

                elif method == "apify" and APIFY_API_TOKEN:
                    tweet_id, tweet_data = await scrapers.get_latest_tweet_apify(username, use_proxies)
                    if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                        successful_method = "apify"
                        break

                elif method == "nitter":
                    # При повторе используем другие инстансы Nitter
                    tweet_id, tweet_data = scrapers.get_latest_tweet_nitter(username, use_proxies)
                    if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                        successful_method = "nitter"
                        break

                elif method == "web":
                    # Для web-скрапинга обновляем user-agent
                    scrapers.session.headers.update({"User-Agent": scrapers.get_random_user_agent()})
                    tweet_id, tweet_data = scrapers.get_latest_tweet_web(username, use_proxies)
                    if tweet_id and scrapers.validate_tweet_id(username, tweet_id):
                        successful_method = "web"
                        break

            except Exception as e:
                logger.error(f"Ошибка при повторной проверке {username} методом {method}: {e}")

    await scrapers.close_async_session()

    if tweet_id and len(str(tweet_id)) < 15:
        logger.warning(f"Получен подозрительно короткий ID твита для @{username}: {tweet_id}. Игнорируем.")
        return user_id, None, None, None

    return user_id, tweet_id, tweet_data, successful_method


# [Остальные функции бота (cmd_start, cmd_add, cmd_remove, cmd_list и т.д.) остаются без изменений]
# [Фоновые задачи и обработчики также остаются без изменений]
async def on_startup(app):
    """Вызывается при запуске бота"""
    global background_task

    # Инициализируем команды бота
    await app.bot.set_my_commands([
        BotCommand("start", "Начало работы"),
        BotCommand("add", "Добавить аккаунт"),
        BotCommand("remove", "Удалить аккаунт"),
        BotCommand("list", "Список аккаунтов"),
        BotCommand("check", "Проверить твиты"),
        BotCommand("interval", "Интервал проверки"),
        BotCommand("settings", "Настройки бота"),
        BotCommand("proxy", "Управление прокси"),
        BotCommand("stats", "Статистика мониторинга"),
        BotCommand("update_nitter", "Обновить Nitter-инстансы")
    ])

    # Инициализируем данные
    init_accounts()

    # Создаем файл прокси, если не существует
    if not os.path.exists(PROXIES_FILE):
        save_json(PROXIES_FILE, {"proxies": []})

    # Создаем файл кеша, если не существует
    if not os.path.exists(CACHE_FILE):
        save_json(CACHE_FILE, {"tweets": {}, "users": {}, "timestamp": int(time.time())})

    # Обновляем список Nitter-инстансов
    try:
        logger.info("Обновление списка Nitter-инстансов...")
        await update_nitter_instances()
    except Exception as e:
        logger.error(f"Ошибка при обновлении Nitter-инстансов: {e}")

    # Запускаем фоновую задачу
    background_task = asyncio.create_task(background_check(app))
    logger.info("Бот запущен, фоновая задача активирована")


async def on_shutdown(app):
    """Вызывается при остановке бота"""
    global background_task
    if background_task and not background_task.cancelled():
        logger.info("Останавливаем фоновую задачу...")
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
        logger.info("Фоновая задача остановлена")

    # Закрываем все асинхронные сессии
    scrapers = TwitterScrapers()
    await scrapers.close_async_session()


# Глобальная переменная для фоновой задачи
background_task = None


async def background_check(app):
    """Фоновая проверка аккаунтов с улучшенной логикой приоритетов"""
    global background_task
    background_task = asyncio.current_task()

    await asyncio.sleep(10)  # Начальная задержка

    while True:
        try:
            # Проверка на отмену задачи
            if asyncio.current_task().cancelled():
                logger.info("Фоновая задача отменена")
                break

            settings = get_settings()
            if not settings.get("enabled", True):
                logger.info("Мониторинг отключен, пропускаем проверку")
                await asyncio.sleep(settings["check_interval"])
                continue

            logger.info("Фоновая проверка аккаунтов")
            subs = load_json(SUBSCRIBERS_FILE, [])
            accounts = init_accounts()

            # Пропускаем проверку, если нет подписчиков или аккаунтов
            if not subs or not accounts:
                logger.info("Нет подписчиков или аккаунтов, пропускаем проверку")
                await asyncio.sleep(settings["check_interval"])
                continue

            # Получаем настройки
            use_proxies = settings.get("use_proxies", False)
            methods = settings.get("scraper_methods", ["web", "nitter", "api"])
            parallel_checks = settings.get("parallel_checks", 3)
            randomize = settings.get("randomize_intervals", True)
            accounts_updated = False

            # Улучшенная сортировка аккаунтов с учетом приоритета и времени
            # Формула: приоритет = базовый_приоритет + бонус_за_неудачи - штраф_за_недавнюю_проверку
            now = datetime.now()
            sorted_accounts = []

            for username, account in accounts.items():
                # Базовый приоритет
                priority = account.get("priority", 1.0)

                # Увеличиваем приоритет для аккаунтов с высоким процентом неудач
                fail_count = account.get("fail_count", 0)
                if fail_count > 0:
                    priority += min(0.5, fail_count * 0.1)  # Максимум +0.5 за неудачи

                # Уменьшаем приоритет для недавно проверенных аккаунтов
                last_check = account.get("last_check", "2000-01-01T00:00:00")
                try:
                    last_check_dt = datetime.fromisoformat(last_check)
                    hours_since_check = (now - last_check_dt).total_seconds() / 3600

                    # Если проверяли менее 1 часа назад, уменьшаем приоритет
                    if hours_since_check < 1:
                        priority -= 0.5 * (1 - hours_since_check)  # От -0 до -0.5
                except:
                    pass

                sorted_accounts.append((username, account, priority))

            # Сортируем по уменьшению приоритета
            sorted_accounts.sort(key=lambda x: x[2], reverse=True)

            # Проверяем аккаунты группами для параллельной обработки
            for i in range(0, len(sorted_accounts), parallel_checks):
                # Если задача отменена, выходим
                if asyncio.current_task().cancelled():
                    logger.info("Фоновая задача отменена")
                    return

                # Берем очередную группу аккаунтов
                batch = sorted_accounts[i:i + parallel_checks]
                tasks = []

                # Создаем задачи для параллельной проверки аккаунтов
                for username, account, _ in batch:
                    if asyncio.current_task().cancelled():
                        break

                    display_name = account.get('username', username)
                    tasks.append(process_account(app, subs, accounts, display_name, account, methods, use_proxies))

                # Запускаем все задачи параллельно
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error(f"Ошибка в параллельной проверке: {result}")
                        elif result:  # Если был обновлен аккаунт
                            accounts_updated = True

                # Небольшая задержка между группами
                await asyncio.sleep(3)

            # Сохраняем обновленные данные
            if accounts_updated:
                save_accounts(accounts)

            # Определяем время до следующей проверки
            if randomize:
                # Случайное время в пределах диапазона
                min_factor = settings.get("min_interval_factor", 0.8)
                max_factor = settings.get("max_interval_factor", 1.2)
                factor = random.uniform(min_factor, max_factor)
                wait_time = int(settings["check_interval"] * factor)
                logger.info(f"Случайное время ожидания: {wait_time} секунд (x{factor:.2f})")
            else:
                wait_time = settings["check_interval"]
                logger.info(f"Следующая проверка через {wait_time} секунд")

            await asyncio.sleep(wait_time)

        except asyncio.CancelledError:
            logger.info("Фоновая задача отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в фоновой проверке: {e}")
            traceback.print_exc()
            # Не останавливаем задачу при ошибках
            await asyncio.sleep(60)

# Если это первая проверка, снимаем флаг и не считаем твит новым
async def process_account(app, subs, accounts, username, account, methods, use_proxies):
            """Обрабатывает один аккаунт и отправляет уведомления при новых твитах"""
            try:
                # Обновляем время проверки
                account['last_check'] = datetime.now().isoformat()
                account['check_count'] = account.get('check_count', 0) + 1

                # Получаем последний известный твит и проверяем флаг первой проверки
                last_id = account.get('last_tweet_id')
                first_check = account.get('first_check', False)

                # Используем мультиметодную проверку
                user_id, tweet_id, tweet_data, method = await check_tweet_multi_method(
                    username, methods, use_proxies
                )

                # Обновляем ID пользователя, если получили новый
                if user_id and not account.get('user_id'):
                    account['user_id'] = user_id

                # Если не нашли твит
                if not tweet_id:
                    # Увеличиваем счетчик неудач
                    account['fail_count'] = account.get('fail_count', 0) + 1

                    # Обновляем процент успеха
                    total_checks = account.get('check_count', 1)
                    fail_count = account.get('fail_count', 0)
                    account['success_rate'] = 100 * (total_checks - fail_count) / total_checks

                    # Уменьшаем приоритет проблемных аккаунтов
                    if account.get('fail_count', 0) > 3:
                        account['priority'] = max(0.1, account.get('priority', 1.0) * 0.9)

                    logger.info(f"Аккаунт @{username}: твиты не найдены (методы: {methods})")
                    return True

                # Сбрасываем счетчик неудач при успехе и восстанавливаем приоритет
                if account.get('fail_count', 0) > 0:
                    account['fail_count'] = max(0, account.get('fail_count', 0) - 1)

                if account.get('priority', 1.0) < 1.0:
                    account['priority'] = min(1.0, account.get('priority', 1.0) * 1.1)

                # Обновляем процент успеха
                total_checks = account.get('check_count', 1)
                fail_count = account.get('fail_count', 0)
                account['success_rate'] = 100 * (total_checks - fail_count) / total_checks

                # Обновляем метод проверки
                account['check_method'] = method

                # Сохраняем текст и URL последнего твита
                if tweet_data:
                    account['last_tweet_text'] = tweet_data.get('text', '')
                    account['last_tweet_url'] = tweet_data.get('url', '')

                # Если это первая проверка, снимаем флаг и не считаем твит новым
                if first_check:
                    account['first_check'] = False
                    account['last_tweet_id'] = tweet_id
                    logger.info(f"Аккаунт @{username}: первая проверка, сохранен ID {tweet_id}")
                    return True

                # Если нашли новый твит (ID изменился)
                elif tweet_id != last_id:
                    try:
                        # Проверяем, что это действительно более новый твит по ID
                        is_newer = int(tweet_id) > int(last_id)
                    except (ValueError, TypeError):
                        # Если не удалось сравнить как числа, считаем что новый
                        is_newer = True

                    if is_newer:
                        # Обнаружен новый твит!
                        account['last_tweet_id'] = tweet_id
                        logger.info(f"Аккаунт @{username}: новый твит {tweet_id}, отправляем уведомления")

                        # Отправляем уведомления всем подписчикам
                        if tweet_data:
                            tweet_text = tweet_data.get('text', '[Новый твит]')
                            tweet_url = tweet_data.get('url', f"https://twitter.com/{username}/status/{tweet_id}")
                            tweet_msg = f"🐦 @{username}:\n\n{tweet_text}\n\n{tweet_url}"

                            for chat_id in subs:
                                try:
                                    await app.bot.send_message(chat_id=chat_id, text=tweet_msg,
                                                               disable_web_page_preview=False)
                                    await asyncio.sleep(0.5)  # Небольшая задержка
                                except Exception as e:
                                    logger.error(f"Ошибка отправки сообщения в чат {chat_id}: {e}")
                        return True
                    else:
                        # ID изменился, но твит старее - просто обновляем ID
                        account['last_tweet_id'] = tweet_id
                        logger.info(f"Аккаунт @{username}: обновлен ID твита на {tweet_id}")
                        return True
                else:
                    logger.info(f"Аккаунт @{username}: нет новых твитов (метод: {method})")
                    return False

            except Exception as e:
                logger.error(f"Ошибка при обработке аккаунта @{username}: {e}")
                traceback.print_exc()

                # Увеличиваем счетчик неудач
                account['fail_count'] = account.get('fail_count', 0) + 1

                # Обновляем процент успеха
                total_checks = account.get('check_count', 1)
                fail_count = account.get('fail_count', 0)
                account['success_rate'] = 100 * (total_checks - fail_count) / total_checks

                # Уменьшаем приоритет проблемных аккаунтов
                if account.get('fail_count', 0) > 3:
                    account['priority'] = max(0.1, account.get('priority', 1.0) * 0.9)

            return True

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    chat_id = update.effective_chat.id
    subs = load_json(SUBSCRIBERS_FILE, [])
    if chat_id not in subs:
        subs.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")],
        [InlineKeyboardButton("🔍 Проверить твиты", callback_data="check")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

    await update.message.reply_text(
        "👋 Бот мониторинга Twitter!\n\n"
        "Используйте команды:\n"
        "/add <username> - добавить аккаунт\n"
        "/remove <username> - удалить аккаунт\n"
        "/list - список аккаунтов\n"
        "/check - проверить твиты\n"
        "/interval <минуты> - интервал проверки\n"
        "/settings - настройки\n"
        "/proxy - управление прокси\n"
        "/update_nitter - обновить список Nitter-инстансов",
        reply_markup=keyboard
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет новый аккаунт для отслеживания"""
    if not context.args:
        return await update.message.reply_text("Использование: /add <username>")

    username = context.args[0].lstrip("@")
    accounts = init_accounts()

    if username.lower() in accounts:
        return await update.message.reply_text(f"@{username} уже добавлен.")

    message = await update.message.reply_text(f"Проверяем @{username}...")

    settings = get_settings()
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["web", "nitter", "api"])

    user_id, tweet_id, tweet_data, method = await check_tweet_multi_method(
        username, methods, use_proxies
    )

    if not tweet_id:
        return await message.edit_text(f"❌ Не удалось найти аккаунт @{username} или получить его твиты.")

    accounts[username.lower()] = {
        "username": username,
        "user_id": user_id,
        "added_at": datetime.now().isoformat(),
        "last_check": datetime.now().isoformat(),
        "last_tweet_id": tweet_id,
        "check_count": 1,
        "success_rate": 100.0,
        "fail_count": 0,
        "check_method": method,
        "priority": 1.0,
        "first_check": True,
        "last_tweet_text": tweet_data.get('text', '[Текст недоступен]') if tweet_data else '[Текст недоступен]',
        "last_tweet_url": tweet_data.get('url',
                                         f"https://twitter.com/{username}/status/{tweet_id}") if tweet_data else f"https://twitter.com/{username}/status/{tweet_id}"
    }
    save_accounts(accounts)

    tweet_text = tweet_data.get('text', '[Текст недоступен]') if tweet_data else '[Текст недоступен]'
    tweet_url = tweet_data.get('url',
                               f"https://twitter.com/{username}/status/{tweet_id}") if tweet_data else f"https://twitter.com/{username}/status/{tweet_id}"

    result = (f"✅ Добавлен @{username}\n\n"
              f"📝 Последний твит:\n{tweet_text}\n\n"
              f"🆔 ID твита: {tweet_id}\n"
              f"🔍 Метод проверки: {method}\n"
              f"🔗 {tweet_url}\n\n"
              f"Бот будет отправлять уведомления о новых твитах.")

    await message.edit_text(result)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет аккаунт из отслеживания"""
    if not context.args:
        return await update.message.reply_text("Использование: /remove <username>")

    username = context.args[0].lstrip("@")
    accounts = init_accounts()

    if username.lower() not in accounts:
        return await update.message.reply_text(f"@{username} не найден в списке.")

    del accounts[username.lower()]
    save_accounts(accounts)
    await update.message.reply_text(f"✅ Удалён @{username}.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список отслеживаемых аккаунтов"""
    accounts = init_accounts()

    if not accounts:
        if hasattr(update, 'callback_query') and update.callback_query:
            return await update.callback_query.edit_message_text(
                "Список пуст. Добавьте аккаунты с помощью команды /add <username>"
            )
        else:
            return await update.message.reply_text(
                "Список пуст. Добавьте аккаунты с помощью команды /add <username>"
            )

    settings = get_settings()
    interval_mins = settings["check_interval"] // 60
    enabled = settings.get("enabled", True)
    status = "✅" if enabled else "❌"

    msg = f"⚙️ Настройки:\n• Интервал проверки: {interval_mins} мин.\n• Мониторинг: {status}\n\n"
    msg += f"📋 Аккаунты ({len(accounts)}):\n"

    for username, data in sorted(accounts.items(), key=lambda x: x[1].get("priority", 1.0), reverse=True):
        display_name = data.get('username', username)
        last_check = data.get("last_check", "никогда")
        tweet_id = data.get("last_tweet_id", "нет")
        method = data.get("check_method", "unknown")
        success_rate = data.get("success_rate", 100.0)
        tweet_text = data.get("last_tweet_text", "")

        if last_check != "никогда":
            try:
                check_dt = datetime.fromisoformat(last_check)
                last_check = check_dt.strftime("%Y-%m-%d %H:%M")
            except:
                last_check = "недавно"

        msg += f"• @{display_name} (ID: {tweet_id}, {success_rate:.0f}%, метод: {method}, проверка: {last_check})"

        if tweet_text:
            short_text = tweet_text[:50] + "..." if len(tweet_text) > 50 else tweet_text
            msg += f"\n  ➡️ {short_text}"

        msg += "\n\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Проверить твиты", callback_data="check")],
        [InlineKeyboardButton("🔄 Принудительно обновить", callback_data="check_force")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, reply_markup=keyboard)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверяет новые твиты"""
    force_update = False

    if context.args and (context.args[0].lower() in ['force', 'update', 'обновить']):
        force_update = True

    if hasattr(update, 'callback_query') and update.callback_query:
        if update.callback_query.data == "check_force":
            force_update = True
        message = await update.callback_query.edit_message_text(
            "Проверяем твиты..." if force_update else "Загружаем последние найденные твиты..."
        )
    else:
        message = await update.message.reply_text(
            "Проверяем твиты..." if force_update else "Загружаем последние найденные твиты..."
        )

    accounts = init_accounts()

    if not accounts:
        return await message.edit_text(
            "Список пуст. Добавьте аккаунты с помощью команды /add <username>"
        )

    if not force_update:
        results = []

        for username, account in accounts.items():
            display_name = account.get('username', username)
            last_id = account.get('last_tweet_id')
            last_check = account.get('last_check', 'никогда')
            method = account.get('check_method', 'unknown')

            if last_check != 'никогда':
                try:
                    check_dt = datetime.fromisoformat(last_check)
                    last_check = check_dt.strftime("%Y-%m-%d %H:%M")
                except:
                    last_check = "недавно"

            if last_id:
                tweet_text = account.get('last_tweet_text', '[Текст недоступен]')
                tweet_url = account.get('last_tweet_url', f"https://twitter.com/{display_name}/status/{last_id}")

                results.append(f"📱 @{display_name} (ID: {last_id}, метод: {method}, проверка: {last_check})\n" +
                               f"➡️ {tweet_text}\n➡️ {tweet_url}")
            else:
                results.append(f"❓ @{display_name}: твиты не найдены")

        result_text = "📊 Последние найденные твиты:\n\n" + "\n\n".join(results)

        if len(result_text) > 4000:
            result_text = result_text[:3997] + "..."

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Принудительно обновить", callback_data="check_force")],
            [InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")]
        ])

        await message.edit_text(result_text, reply_markup=keyboard)
        return

    settings = get_settings()
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["web", "nitter", "api"])

    results = []
    new_tweets = []
    found_tweets = []
    accounts_updated = False

    for username, account in accounts.items():
        display_name = account.get('username', username)
        last_id = account.get('last_tweet_id')
        first_check = account.get('first_check', False)

        account['last_check'] = datetime.now().isoformat()
        account['check_count'] = account.get('check_count', 0) + 1
        accounts_updated = True

        try:
            user_id, tweet_id, tweet_data, method = await check_tweet_multi_method(
                display_name, methods, use_proxies
            )

            if user_id and not account.get('user_id'):
                account['user_id'] = user_id
                accounts_updated = True

            if not tweet_id:
                account['fail_count'] = account.get('fail_count', 0) + 1
                total_checks = account.get('check_count', 1)
                fail_count = account.get('fail_count', 0)
                account['success_rate'] = 100 * (total_checks - fail_count) / total_checks

                if last_id:
                    results.append(f"❓ @{display_name}: твиты не найдены, последний известный ID: {last_id}")
                else:
                    results.append(f"❓ @{display_name}: твиты не найдены")
                continue

            if account.get('fail_count', 0) > 0:
                account['fail_count'] = max(0, account.get('fail_count', 0) - 1)

            total_checks = account.get('check_count', 1)
            fail_count = account.get('fail_count', 0)
            account['success_rate'] = 100 * (total_checks - fail_count) / total_checks

            account['check_method'] = method

            if tweet_data:
                found_tweets.append({
                    'username': display_name,
                    'tweet_id': tweet_id,
                    'data': tweet_data
                })

                tweet_text = tweet_data.get('text', '[Текст недоступен]')
                tweet_url = tweet_data.get('url', f"https://twitter.com/{display_name}/status/{tweet_id}")
                account['last_tweet_text'] = tweet_text
                account['last_tweet_url'] = tweet_url

            if first_check:
                account['first_check'] = False
                account['last_tweet_id'] = tweet_id
                accounts_updated = True
                tweet_text = tweet_data.get('text', '[Текст недоступен]') if tweet_data else '[Текст недоступен]'
                tweet_url = tweet_data.get('url',
                                           f"https://twitter.com/{display_name}/status/{tweet_id}") if tweet_data else f"https://twitter.com/{display_name}/status/{tweet_id}"
                results.append(
                    f"📝 @{display_name}: первая проверка, сохранен ID твита {tweet_id}\n➡️ Текст: {tweet_text}\n➡️ Ссылка: {tweet_url}")
            elif tweet_id != last_id:
                try:
                    is_newer = int(tweet_id) > int(last_id)
                except (ValueError, TypeError):
                    is_newer = True

                if is_newer:
                    account['last_tweet_id'] = tweet_id
                    accounts_updated = True

                    tweet_text = tweet_data.get('text', 'Текст недоступен')
                    tweet_url = tweet_data.get('url', f"https://twitter.com/{display_name}/status/{tweet_id}")

                    new_tweet_msg = f"🔥 Новый твит от @{display_name}:\n\n{tweet_text}\n\n🔗 {tweet_url}"
                    new_tweets.append(new_tweet_msg)
                    results.append(f"✅ @{display_name}: новый твит {tweet_id} (метод: {method})")
                else:
                    account['last_tweet_id'] = tweet_id
                    accounts_updated = True
                    results.append(f"🔄 @{display_name}: обновлен ID твита на {tweet_id} (метод: {method})")
            else:
                results.append(f"🔄 @{display_name}: нет новых твитов (метод: {method})")

        except Exception as e:
            logger.error(f"Ошибка при проверке @{display_name}: {e}")
            traceback.print_exc()
            results.append(f"❌ @{display_name}: ошибка - {str(e)[:50]}")
            account['fail_count'] = account.get('fail_count', 0) + 1

    if accounts_updated:
        save_accounts(accounts)

    if new_tweets:
        await message.edit_text(f"✅ Найдено {len(new_tweets)} новых твитов!")

        for tweet_msg in new_tweets:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=tweet_msg,
                                           disable_web_page_preview=False)
    else:
        result_text = "🔍 Новых твитов не найдено.\n\n📊 Результаты проверки:\n" + "\n".join(results)

        if len(result_text) > 4000:
            result_text = result_text[:3997] + "..."

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Проверить снова", callback_data="check_force"),
            InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")
        ]])

        await message.edit_text(result_text, reply_markup=keyboard)


async def cmd_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает интервал проверки"""
    if not context.args:
        settings = get_settings()
        current_mins = settings["check_interval"] // 60
        return await update.message.reply_text(
            f"Текущий интервал проверки: {current_mins} мин.\n"
            f"Для изменения: /interval <минуты>"
        )

    try:
        mins = int(context.args[0])
        if mins < 1:
            return await update.message.reply_text("Интервал должен быть не менее 1 минуты.")
        if mins > 1440:
            return await update.message.reply_text("Интервал должен быть не более 1440 минут (24 часа).")

        settings = update_setting("check_interval", mins * 60)
        await update.message.reply_text(f"✅ Интервал проверки установлен на {mins} мин.")
    except ValueError:
        await update.message.reply_text("Использование: /interval <минуты>")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает настройки бота"""
    settings = get_settings()

    interval_mins = settings.get("check_interval", DEFAULT_CHECK_INTERVAL) // 60
    enabled = settings.get("enabled", True)
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["web", "nitter", "api"])
    parallel_checks = settings.get("parallel_checks", 3)
    randomize = settings.get("randomize_intervals", True)

    enabled_status = "✅ включен" if enabled else "❌ выключен"
    proxies_status = "✅ включено" if use_proxies else "❌ выключено"
    randomize_status = "✅ включено" if randomize else "❌ выключено"

    proxies = get_proxies()
    proxy_count = len(proxies.get("proxies", []))

    nitter_instances = settings.get("nitter_instances", [])
    nitter_count = len(nitter_instances)

    msg = (
        "⚙️ **Настройки мониторинга**\n\n"
        f"• Мониторинг: {enabled_status}\n"
        f"• Интервал проверки: {interval_mins} мин.\n"
        f"• Случайные интервалы: {randomize_status}\n"
        f"• Одновременные проверки: {parallel_checks}\n"
        f"• Использование прокси: {proxies_status} (доступно: {proxy_count})\n"
        f"• Nitter-инстансы: {nitter_count}\n\n"
        f"• Приоритет методов: {', '.join(methods)}\n\n"
    )

    keyboard = []

    keyboard.append([
        InlineKeyboardButton("🔄 Вкл/выкл мониторинг", callback_data="toggle_monitoring"),
        InlineKeyboardButton("🔌 Вкл/выкл прокси", callback_data="toggle_proxies")
    ])

    keyboard.append([
        InlineKeyboardButton("API", callback_data="method_priority:api"),
        InlineKeyboardButton("Nitter", callback_data="method_priority:nitter"),
        InlineKeyboardButton("Web", callback_data="method_priority:web")
    ])

    if APIFY_API_TOKEN:
        keyboard.append([InlineKeyboardButton("Apify", callback_data="method_priority:apify")])

    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="list")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def cmd_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление прокси-серверами"""
    if not context.args:
        proxies = get_proxies()
        proxy_list = proxies.get("proxies", [])

        if not proxy_list:
            await update.message.reply_text(
                "⚠️ Список прокси пуст.\n\n"
                "Добавьте прокси командой:\n"
                "/proxy add <ip:port> или <ip:port:user:pass>\n\n"
                "Другие команды:\n"
                "/proxy list - показать список прокси\n"
                "/proxy clear - очистить список прокси"
            )
            return

        msg = f"🔌 Всего прокси: {len(proxy_list)}\n\n"
        for i, proxy in enumerate(proxy_list[:20], 1):
            msg += f"{i}. `{proxy}`\n"

        if len(proxy_list) > 20:
            msg += f"\n... и еще {len(proxy_list) - 20} прокси."

        msg += "\n\nДля добавления используйте:\n/proxy add <ip:port> или <ip:port:user:pass>"

        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    action = context.args[0].lower()

    if action == "add" and len(context.args) > 1:
        proxy = context.args[1]
        proxies = get_proxies()
        proxy_list = proxies.get("proxies", [])

        if ":" not in proxy:
            await update.message.reply_text("❌ Неверный формат прокси. Используйте ip:port или ip:port:user:pass")
            return

        if proxy not in proxy_list:
            proxy_list.append(proxy)
            proxies["proxies"] = proxy_list
            save_json(PROXIES_FILE, proxies)
            await update.message.reply_text(f"✅ Прокси `{proxy}` добавлен. Всего: {len(proxy_list)}",
                                            parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Этот прокси уже добавлен")

    elif action == "list":
        proxies = get_proxies()
        proxy_list = proxies.get("proxies", [])

        if not proxy_list:
            await update.message.reply_text("Список прокси пуст.")
            return

        msg = f"🔌 Всего прокси: {len(proxy_list)}\n\n"
        for i, proxy in enumerate(proxy_list[:20], 1):
            msg += f"{i}. `{proxy}`\n"

        if len(proxy_list) > 20:
            msg += f"\n... и еще {len(proxy_list) - 20} прокси."

        await update.message.reply_text(msg, parse_mode="Markdown")

    elif action == "clear":
        save_json(PROXIES_FILE, {"proxies": []})
        await update.message.reply_text("✅ Список прокси очищен")

    else:
        await update.message.reply_text(
            "❓ Неизвестная команда. Используйте:\n"
            "/proxy add <ip:port> - добавить прокси\n"
            "/proxy list - показать список прокси\n"
            "/proxy clear - очистить список прокси"
        )


async def cmd_update_nitter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновляет список Nitter-инстансов"""
    message = await update.message.reply_text("🔍 Проверка доступных Nitter-инстансов...")

    try:
        instances = await update_nitter_instances()

        if instances:
            await message.edit_text(
                f"✅ Найдено {len(instances)} рабочих Nitter-инстансов:\n\n" +
                "\n".join(f"• {instance}" for instance in instances)
            )
        else:
            await message.edit_text(
                "❌ Не найдено работающих Nitter-инстансов. Будет использоваться прямой скрапинг Twitter."
            )
    except Exception as e:
        await message.edit_text(f"❌ Ошибка при обновлении: {str(e)}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику работы бота"""
    accounts = init_accounts()

    if not accounts:
        return await update.message.reply_text("Аккаунты не добавлены")

    total_checks = sum(acct.get("check_count", 0) for acct in accounts.values())
    total_fails = sum(acct.get("fail_count", 0) for acct in accounts.values())
    success_rate = 100.0 * (total_checks - total_fails) / max(1, total_checks)

    methods = {}
    for account in accounts.values():
        method = account.get("check_method")
        if method:
            methods[method] = methods.get(method, 0) + 1

    most_reliable = sorted(
        [(username, data.get("success_rate", 0)) for username, data in accounts.items()],
        key=lambda x: x[1],
        reverse=True
    )[:5]

    least_reliable = sorted(
        [(username, data.get("success_rate", 0)) for username, data in accounts.items()],
        key=lambda x: x[1]
    )[:5]

    msg = (
        "📊 **Статистика мониторинга**\n\n"
        f"• Всего аккаунтов: {len(accounts)}\n"
        f"• Всего проверок: {total_checks}\n"
        f"• Успешных проверок: {total_checks - total_fails} ({success_rate:.1f}%)\n\n"

        "**Методы проверки:**\n"
    )

    for method, count in methods.items():
        percent = 100.0 * count / len(accounts)
        msg += f"• {method}: {count} ({percent:.1f}%)\n"

    msg += "\n**Самые надежные аккаунты:**\n"
    for username, rate in most_reliable:
        msg += f"• @{accounts[username].get('username', username)}: {rate:.1f}%\n"

    msg += "\n**Проблемные аккаунты:**\n"
    for username, rate in least_reliable:
        msg += f"• @{accounts[username].get('username', username)}: {rate:.1f}%\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    if query.data == "list":
        await cmd_list(update, context)
    elif query.data == "check":
        await cmd_check(update, context)
    elif query.data == "check_force":
        await cmd_check(update, context)  # callback_data уже установлен как check_force
    elif query.data == "settings":
        await cmd_settings(update, context)
    elif query.data == "toggle_proxies":
        await toggle_proxies(update, context)
    elif query.data == "toggle_monitoring":
        await toggle_monitoring(update, context)
    elif query.data.startswith("method_priority:"):
        method = query.data.split(":", 1)[1]
        await change_method_priority(update, context, method)


async def toggle_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает/выключает использование прокси"""
    settings = get_settings()
    current = settings.get("use_proxies", False)
    settings["use_proxies"] = not current
    save_json(SETTINGS_FILE, settings)

    status = "✅ включено" if settings["use_proxies"] else "❌ выключено"
    proxies = get_proxies()
    proxy_count = len(proxies.get("proxies", []))

    await update.callback_query.edit_message_text(
        f"Использование прокси: {status}\n\n"
        f"Количество прокси: {proxy_count}\n\n"
        "Вернитесь в настройки с помощью /settings",
    )


async def toggle_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает/выключает мониторинг"""
    settings = get_settings()
    current = settings.get("enabled", True)
    settings["enabled"] = not current
    save_json(SETTINGS_FILE, settings)

    status = "✅ включен" if settings["enabled"] else "❌ выключен"

    await update.callback_query.edit_message_text(
        f"Мониторинг: {status}\n\n"
        "Вернитесь в настройки с помощью /settings",
    )


async def change_method_priority(update: Update, context: ContextTypes.DEFAULT_TYPE, method):
    """Изменяет приоритет методов проверки"""
    settings = get_settings()
    methods = settings.get("scraper_methods", ["web", "nitter", "api"])

    if method in methods:
        methods.remove(method)
    methods.insert(0, method)

    settings["scraper_methods"] = methods
    save_json(SETTINGS_FILE, settings)

    await cmd_settings(update, context)


def main():
    if not TG_TOKEN:
        logger.error("TG_TOKEN не указан в .env файле")
        return

    for path, default in [
        (SUBSCRIBERS_FILE, []),
        (SETTINGS_FILE, {
            "check_interval": DEFAULT_CHECK_INTERVAL,
            "enabled": True,
            "use_proxies": False,
            "scraper_methods": ["api", "apify", "web", "nitter"] if APIFY_API_TOKEN else ["api", "web", "nitter"],
            "max_retries": 3,
            "cache_expiry": 3600,
            "randomize_intervals": True,
            "min_interval_factor": 0.8,
            "max_interval_factor": 1.2,
            "parallel_checks": 3,
            "nitter_instances": NITTER_INSTANCES,
            "health_check_interval": 3600,
            "last_health_check": 0
        })
    ]:
        if not os.path.exists(path):
            save_json(path, default)

    app = ApplicationBuilder().token(TG_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("proxy", cmd_proxy))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("update_nitter", cmd_update_nitter))

    app.add_handler(CallbackQueryHandler(button_handler))

    settings = get_settings()
    interval_mins = settings["check_interval"] // 60
    logger.info(f"🚀 Бот запущен, интервал проверки: {interval_mins} мин.")
    app.run_polling()


if __name__ == "__main__":
    main()
