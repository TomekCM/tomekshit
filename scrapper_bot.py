import os
import json
import time
import logging
import random
import requests
import re
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
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.safari.options import Options as SafariOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.safari.service import Service as SafariService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from typing import Dict, List, Tuple, Any, Optional, Union
import platform

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL = 600  # секунд (10 минут)
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
TWITTER_BEARER = os.getenv("TWITTER_BEARER", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.lacontrevoie.fr",
    "https://nitter.unixfox.eu",
    "https://nitter.fdn.fr",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.mint.lgbt",
    "https://nitter.privacy.com.de",
    "https://nitter.projectsegfau.lt",
    "https://nitter.privacydev.net",
    "https://tweet.lambda.dance",
    "https://tweet.namejeff.xyz"
]

DATA_DIR = "data"
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
API_LIMITS_FILE = os.path.join(DATA_DIR, "api_limits.json")
PROXIES_FILE = os.path.join(DATA_DIR, "proxies.json")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
BROWSER_STATS_FILE = os.path.join(DATA_DIR, "browser_stats.json")

os.makedirs(DATA_DIR, exist_ok=True)


def update_browser_stats(browser_name, action_type, success):
    """Обновляет статистику использования браузеров"""
    try:
        if os.path.exists(BROWSER_STATS_FILE):
            with open(BROWSER_STATS_FILE, 'r') as f:
                stats = json.load(f)
        else:
            stats = {"browsers": {}, "last_update": int(time.time())}

        # Инициализируем данные для браузера, если их нет
        if browser_name not in stats["browsers"]:
            stats["browsers"][browser_name] = {
                "total_attempts": 0,
                "successful_attempts": 0,
                "captchas": 0,
                "errors": 0,
                "last_success": None
            }

        # Обновляем статистику
        stats["browsers"][browser_name]["total_attempts"] += 1

        if action_type == "captcha":
            stats["browsers"][browser_name]["captchas"] += 1

        if action_type == "error":
            stats["browsers"][browser_name]["errors"] += 1

        if success:
            stats["browsers"][browser_name]["successful_attempts"] += 1
            stats["browsers"][browser_name]["last_success"] = int(time.time())

        stats["last_update"] = int(time.time())

        with open(BROWSER_STATS_FILE, 'w') as f:
            json.dump(stats, f)

    except Exception as e:
        logger.error(f"Ошибка при обновлении статистики браузеров: {e}")


class HTMLSession:
    def __init__(self, use_safari=True, incognito=False, driver=None, browser_name=None):
        self.use_safari = use_safari
        self.incognito = incognito
        self.retry_count = 0
        self.max_retries = 2
        self.browser_name = browser_name or ("Safari" if use_safari else "Chrome")

        # Статистика для этой сессии
        self.stats = {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "captchas": 0,
            "timeouts": 0
        }

        # Если драйвер был передан снаружи
        if driver:
            self.driver = driver
        # Инициализация Safari
        elif use_safari and platform.system() == "Darwin":
            logger.info(f"Инициализация Safari WebDriver")
            try:
                options = SafariOptions()
                self.driver = webdriver.Safari(options=options)
                self.driver.set_page_load_timeout(25)  # Уменьшаем таймаут

                # ВАЖНО: Проверка авторизации Twitter в Safari
                cookies_file = os.path.join(DATA_DIR, f"twitter_cookies_safari.json")
                if os.path.exists(cookies_file):
                    self.driver.get("https://twitter.com")
                    try:
                        with open(cookies_file, "r") as f:
                            cookies = json.load(f)
                            for cookie in cookies:
                                try:
                                    self.driver.add_cookie(cookie)
                                except:
                                    pass
                    except Exception as e:
                        logger.warning(f"Не удалось загрузить куки для Safari: {e}")

                logger.info("Safari WebDriver успешно инициализирован")
            except Exception as e:
                logger.error(f"Не удалось инициализировать Safari WebDriver: {e}")
                raise
        # Инициализация Chrome
        else:
            logger.info(f"Инициализация Chrome WebDriver")
            options = ChromeOptions()

            # НЕ используем headless режим на Mac
            if platform.system() != "Darwin":
                options.add_argument("--headless=new")

            settings = get_settings()
            chrome_profile = settings.get("chrome_profile_path",
                                          os.path.join(os.path.expanduser('~'), 'TwitterBotProfile'))
            os.makedirs(chrome_profile, exist_ok=True)
            options.add_argument(f"--user-data-dir={chrome_profile}")
            options.add_argument("--disable-extensions")  # Отключаем расширения
            options.add_argument("--disable-plugins")  # Отключаем плагины
            options.add_argument("--disable-application-cache")  # Отключаем кеш
            options.add_argument("--log-level=3")  # Только критические ошибки
            options.add_argument("--silent")  # Минимизация вывода

            # Дополнительные опции для стабильности на macOS
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-notifications")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-logging")
            os.environ['PYTHONIOENCODING'] = 'utf-8'
            os.environ['WDM_LOG_LEVEL'] = '0'  # Уменьшить логи WebDriverManager

            # Предотвращаем закрытие Chrome после завершения сессии
            options.add_experimental_option("detach", True)

            try:
                self.driver = webdriver.Chrome(
                    service=ChromeService(ChromeDriverManager().install()),
                    options=options
                )
                self.driver.set_page_load_timeout(25)
            except Exception as e:
                logger.error(f"Не удалось инициализировать Chrome WebDriver: {e}")
                raise

        self.driver.implicitly_wait(10)
        logger.info(f"WebDriver {self.browser_name} инициализирован: {type(self.driver).__name__}")

        # Глобальная статистика
        update_browser_stats(self.browser_name, "init", True)

        # Сохраняем куки Twitter после инициализации
        self.save_twitter_cookies()

    def save_browser_state(driver, browser_name):
        """Сохраняет состояние браузера (куки) для повторного использования"""
        try:
            current_url = driver.current_url
            if "twitter.com" in current_url:
                # Сохраняем куки Twitter
                cookies_file = os.path.join(DATA_DIR, f"twitter_cookies_{browser_name.lower().replace(' ', '_')}.json")
                cookies = driver.get_cookies()
                with open(cookies_file, "w") as f:
                    json.dump(cookies, f)
                logger.info(f"Twitter куки сохранены для {browser_name}: {len(cookies)} куки")

                # Сохраняем скриншот для отладки
                screenshot_path = os.path.join(DATA_DIR, f"{browser_name.lower()}_auth_state.png")
                driver.save_screenshot(screenshot_path)
                logger.info(f"Скриншот текущего состояния сохранен: {screenshot_path}")

                # Проверяем авторизацию
                logger.info(f"URL после авторизации: {current_url}")
                if "login" not in current_url:
                    logger.info(f"✅ {browser_name} успешно авторизован в Twitter!")
                    return True
        except Exception as e:
            logger.error(f"Ошибка при сохранении состояния {browser_name}: {e}")

        return False

    def save_twitter_cookies(self):
        """Сохраняет куки Twitter если находимся на twitter.com"""
        try:
            current_url = self.driver.current_url
            if "twitter.com" in current_url and "login" not in current_url:
                cookies_file = os.path.join(DATA_DIR,
                                            f"twitter_cookies_{self.browser_name.lower().replace(' ', '_')}.json")
                cookies = self.driver.get_cookies()
                with open(cookies_file, "w") as f:
                    json.dump(cookies, f)
                logger.info(f"Куки Twitter сохранены для {self.browser_name}")
        except Exception as e:
            logger.error(f"Не удалось сохранить куки Twitter: {e}")

    def bypass_captcha(self):
        """Пытается обнаружить и преодолеть капчу"""
        try:
            # Проверка наличия кнопки или фрейма с капчей
            captcha_elements = self.driver.find_elements(By.XPATH,
                                                         "//*[contains(@src, 'captcha') or contains(@title, 'captcha') or contains(@id, 'captcha')]")

            if captcha_elements:
                logger.info("Обнаружена капча, пытаемся преодолеть...")

                # Имитация задержек и движения мыши
                self.driver.execute_script("window.scrollBy(0, 100);")
                time.sleep(random.uniform(1, 2))
                self.driver.execute_script("window.scrollBy(0, -50);")

                # Возможно здесь можно добавить взаимодействие с API разрешения капчи
                return False
        except:
            pass

        return False

    def get(self, url, proxies=None, timeout=25):
        try:
            # Добавляем параметры для обхода кеширования
            if '?' not in url:
                url += f"?_={int(time.time())}"
            else:
                url += f"&_={int(time.time())}"

            # Добавляем случайный параметр для дальнейшего обхода кеширования
            url += f"&_cb={int(time.time())}"

            self.stats["attempts"] += 1
            logger.info(f"Загружаю страницу через {self.browser_name}: {url}")

            # Проверяем текущий URL
            current_url = self.driver.current_url
            # Если текущий URL уже содержит twitter и username, просто обновляем страницу
            if "twitter.com" in current_url and url.split("?")[0].split("/")[-1] in current_url:
                logger.info(f"Уже на странице Twitter, просто обновляем с новыми параметрами")
                self.driver.get(url)
            else:
                # Иначе загружаем новый URL
                self.driver.get(url)

            # Добавляем имитацию человеческого поведения
            self._human_like_behavior()

            # Ждем загрузку контента
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Даем время для загрузки динамического контента
            time.sleep(random.uniform(2, 3.5))

            # Проверяем наличие капчи или блокировки
            page_source = self.driver.page_source.lower()
            if "captcha" in page_source or "blocked" in page_source or "rate limit" in page_source:
                self.stats["captchas"] += 1
                logger.warning(f"Обнаружена капча или блокировка на странице {url} в {self.browser_name}")
                update_browser_stats(self.browser_name, "captcha", False)

                # Пробуем сохранить снимок экрана для диагностики
                try:
                    screenshot_path = os.path.join(DATA_DIR, f"captcha_{int(time.time())}.png")
                    self.driver.save_screenshot(screenshot_path)
                    logger.info(f"Сохранен снимок капчи: {screenshot_path}")
                except:
                    pass

                return self

            # Успешная загрузка
            self.stats["successes"] += 1
            update_browser_stats(self.browser_name, "success", True)
            return self

        except Exception as e:
            self.stats["failures"] += 1
            if "timeout" in str(e).lower():
                self.stats["timeouts"] += 1

            logger.error(f"Ошибка при загрузке страницы {url} через {self.browser_name}: {e}")
            update_browser_stats(self.browser_name, "error", False)
            return self

    def _human_like_behavior(self):
        """Имитирует поведение человека в браузере"""
        try:
            # Случайная пауза перед действиями
            time.sleep(random.uniform(0.5, 1.5))

            # Выполняем случайный скроллинг
            scroll_amount = random.randint(100, 300)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
            time.sleep(random.uniform(0.3, 0.8))

            # Еще один скролл
            scroll_amount = random.randint(200, 500)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
            time.sleep(random.uniform(0.5, 1))

            # Иногда скроллим назад немного
            if random.random() > 0.7:
                self.driver.execute_script(f"window.scrollBy(0, -{random.randint(50, 150)});")
                time.sleep(random.uniform(0.2, 0.5))
        except:
            # Игнорируем ошибки в имитации поведения
            pass

    @property
    def html(self):
        return self.driver

    def close(self):
        try:
            self.driver.quit()
            logger.info(f"WebDriver {self.browser_name} закрыт")
        except Exception as e:
            logger.error(f"Ошибка при закрытии WebDriver {self.browser_name}: {e}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def is_admin(user_id):
    settings = get_settings()
    admin_ids = settings.get('admin_ids', [])
    return user_id in admin_ids or user_id == ADMIN_ID


def init_accounts():
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
        if "tweet_data" not in account:
            account["tweet_data"] = {}
            updated = True
        if "scraper_methods" not in account:
            account["scraper_methods"] = None
            updated = True

    if updated:
        save_json(ACCOUNTS_FILE, accounts)

    return accounts


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при сохранении файла {path}: {e}")


def save_accounts(accounts_data):
    save_json(ACCOUNTS_FILE, accounts_data)


def get_cache():
    cache = load_json(CACHE_FILE, {"tweets": {}, "users": {}, "timestamp": int(time.time())})

    current_time = int(time.time())
    hours_ago = current_time - 21600  # 6 часов

    tweets_cache = cache.get("tweets", {})
    for username, data in list(tweets_cache.items()):
        if data.get("timestamp", 0) < hours_ago:
            del tweets_cache[username]

    users_cache = cache.get("users", {})
    for username, data in list(users_cache.items()):
        if data.get("timestamp", 0) < hours_ago:
            del users_cache[username]

    cache["timestamp"] = current_time
    return cache


def update_cache(category, key, data, force=False):
    cache = get_cache()

    if category not in cache:
        cache[category] = {}

    # Если нужно сохранить историю
    if category == "tweets" and key in cache[category] and not force:
        # Получаем текущие данные
        current_data = cache[category][key].get("data", {})
        current_tweet_id = current_data.get("tweet_id")

        # Если новые данные содержат новый ID твита, сохраняем старые в историю
        if data and "tweet_id" in data and current_tweet_id and data["tweet_id"] != current_tweet_id:
            # Создаем или обновляем историю
            if "history" not in cache[category][key]:
                cache[category][key]["history"] = []

            # Добавляем текущие данные в историю (ограничиваем до 10 записей)
            history_entry = {
                "tweet_id": current_tweet_id,
                "tweet_data": current_data.get("tweet_data", {}),
                "timestamp": cache[category][key].get("timestamp", int(time.time()))
            }

            history = cache[category][key]["history"]
            history.append(history_entry)

            # Ограничиваем размер истории
            if len(history) > 10:
                history = history[-10:]

            cache[category][key]["history"] = history

    # Принудительное удаление старого значения
    if force and key in cache[category]:
        del cache[category][key]

    # Добавляем новые данные с текущим временем
    if data is not None:
        cache[category][key] = {
            "data": data,
            "timestamp": int(time.time())
        }

    save_json(CACHE_FILE, cache)


def get_from_cache(category, key, max_age=300):
    cache = get_cache()

    if category in cache and key in cache[category]:
        item = cache[category][key]
        if int(time.time()) - item.get("timestamp", 0) < max_age:
            return item.get("data")

    return None


def delete_from_cache(category=None, key=None):
    cache = get_cache()

    if category is None:
        cache = {"tweets": {}, "users": {}, "timestamp": int(time.time())}
        logger.info("Полная очистка кеша")
    elif key is None and category in cache:
        cache[category] = {}
        logger.info(f"Очищен кеш раздела {category}")
    elif category in cache and key in cache[category]:
        del cache[category][key]
        logger.info(f"Удалена запись {key} из кеша {category}")

    save_json(CACHE_FILE, cache)


def get_settings():
    settings = load_json(SETTINGS_FILE, {
        "check_interval": DEFAULT_CHECK_INTERVAL,
        "enabled": True,
        "use_proxies": False,
        "scraper_methods": ["nitter", "web", "api"],
        "max_retries": 3,
        "cache_expiry": 1800,
        "randomize_intervals": True,
        "min_interval_factor": 0.8,
        "max_interval_factor": 1.2,
        "parallel_checks": 3,
        "api_request_limit": 20,
        "nitter_instances": NITTER_INSTANCES,
        "health_check_interval": 3600,
        "last_health_check": 0
    })

    if "api_request_limit" not in settings or not isinstance(settings["api_request_limit"], int):
        settings["api_request_limit"] = 20
        save_json(SETTINGS_FILE, settings)

    return settings


def update_setting(key, value):
    settings = get_settings()
    settings[key] = value
    save_json(SETTINGS_FILE, settings)
    return settings


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


def clean_account_data(username):
    logger.info(f"Очистка всех данных для аккаунта @{username}")

    delete_from_cache("tweets", f"api_{username.lower()}")
    delete_from_cache("tweets", f"web_{username.lower()}")
    delete_from_cache("tweets", f"nitter_{username.lower()}")
    delete_from_cache("users", username.lower())

    accounts = init_accounts()
    if username.lower() in accounts:
        accounts[username.lower()] = {
            "username": accounts[username.lower()].get("username", username),
            "added_at": datetime.now().isoformat(),
            "last_check": None,
            "last_tweet_id": None,
            "check_count": 0,
            "success_rate": 100.0,
            "fail_count": 0,
            "check_method": None,
            "priority": 1.0,
            "first_check": True,
            "last_tweet_text": "",
            "last_tweet_url": "",
            "tweet_data": {},
            "scraper_methods": accounts[username.lower()].get("scraper_methods", None)  # Сохраняем настройки методов
        }
        save_accounts(accounts)

    logger.info(f"Данные для аккаунта @{username} очищены")


def login_to_twitter(driver, platform_name="Safari"):
    """Вход в Twitter с сохранением куки после успеха"""
    # Выбор учетных данных в зависимости от браузера
    if platform_name.lower() == "chrome":
        username = os.getenv("TWITTER_USERNAME_CHROME", os.getenv("TWITTER_USERNAME", ""))
        password = os.getenv("TWITTER_PASSWORD_CHROME", os.getenv("TWITTER_PASSWORD", ""))
    else:  # Safari или любой другой
        username = os.getenv("TWITTER_USERNAME", "")
        password = os.getenv("TWITTER_PASSWORD", "")

    if not username or not password:
        logger.warning(f"Не удалось найти учетные данные Twitter для {platform_name} в переменных окружения")
        return False

    # Проверяем текущий URL, возможно мы уже авторизованы
    current_url = driver.current_url
    if "twitter.com/home" in current_url or "twitter.com/explore" in current_url:
        logger.info(f"Уже авторизованы в Twitter через {platform_name}")

        # Сохраняем куки для будущих сессий
        try:
            cookies = driver.get_cookies()
            cookies_file = os.path.join(DATA_DIR, f"twitter_cookies_{platform_name.lower()}.json")
            with open(cookies_file, "w") as f:
                json.dump(cookies, f)
            logger.info(f"Куки Twitter сохранены для будущих сессий {platform_name}")
        except Exception as e:
            logger.error(f"Не удалось сохранить куки для {platform_name}: {e}")

        return True

    try:
        logger.info(f"Пытаемся войти в Twitter через {platform_name}...")
        driver.get("https://twitter.com/login")
        time.sleep(5)

        # Сделаем скриншот для диагностики
        try:
            screenshot_path = os.path.join(DATA_DIR, f"twitter_login_{platform_name.lower()}_{int(time.time())}.png")
            driver.save_screenshot(screenshot_path)
            logger.info(f"Сохранен скриншот страницы входа: {screenshot_path}")
        except:
            pass

        try:
            username_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "text"))
            )
            username_field.send_keys(username)

            next_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Next')]"))
            )
            next_button.click()
            time.sleep(3)

            password_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "password"))
            )
            password_field.send_keys(password)

            login_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Log in')]"))
            )
            login_button.click()

            # Ожидаем или редирект на домашнюю страницу, или ошибку
            WebDriverWait(driver, 15).until(
                lambda d: "login" not in d.current_url.lower() or "error" in d.current_url.lower()
            )

            # Проверяем успешность входа
            if "login" not in driver.current_url.lower() and "error" not in driver.current_url.lower():
                logger.info(f"✅ Успешно вошли в Twitter через {platform_name}")

                # Сохраняем куки для будущих сессий
                try:
                    cookies = driver.get_cookies()
                    cookies_file = os.path.join(DATA_DIR, f"twitter_cookies_{platform_name.lower()}.json")
                    with open(cookies_file, "w") as f:
                        json.dump(cookies, f)
                    logger.info(f"Куки Twitter сохранены для будущих сессий {platform_name}")
                except Exception as e:
                    logger.error(f"Не удалось сохранить куки для {platform_name}: {e}")

                return True
            else:
                logger.warning(f"❌ Не удалось войти через {platform_name} - проверьте правильность учетных данных")
                return False

        except Exception as e:
            logger.error(f"Ошибка при вводе данных для входа в {platform_name}: {e}")

            # Альтернативный способ входа
            try:
                username_field = driver.find_element(By.CSS_SELECTOR, "input[autocomplete='username']")
                username_field.send_keys(username)
                driver.find_element(By.CSS_SELECTOR, "div[role='button']").click()
                time.sleep(3)

                password_field = driver.find_element(By.CSS_SELECTOR, "input[name='password']")
                password_field.send_keys(password)
                driver.find_element(By.CSS_SELECTOR, "div[data-testid='LoginButton']").click()
                time.sleep(5)

                # Проверяем успешность входа
                if "login" not in driver.current_url.lower():
                    logger.info(f"✅ Успешно вошли альтернативным способом через {platform_name}")

                    # Сохраняем куки для будущих сессий
                    try:
                        cookies = driver.get_cookies()
                        cookies_file = os.path.join(DATA_DIR, f"twitter_cookies_{platform_name.lower()}.json")
                        with open(cookies_file, "w") as f:
                            json.dump(cookies, f)
                        logger.info(f"Куки Twitter сохранены для будущих сессий {platform_name}")
                    except Exception as e:
                        logger.error(f"Не удалось сохранить куки для {platform_name}: {e}")

                    return True
                return False
            except Exception as e2:
                logger.error(f"Альтернативный вход через {platform_name} тоже не удался: {e2}")
                return False

    except Exception as e:
        logger.error(f"Ошибка при входе в Twitter через {platform_name}: {e}")
        return False

def get_browser_session(use_safari=True, incognito=False):
    """Получает сессию браузера с возможностью выбора между Safari и Chrome"""
    browser_name = "Safari" if use_safari else "Chrome"
    if incognito:
        browser_name += " (инкогнито)"

    logger.info(f"Инициализация {browser_name} WebDriver")

    try:
        # Для Chrome в macOS нужны особые настройки
        if not use_safari and platform.system() == "Darwin":
            options = ChromeOptions()
            chrome_profile = os.path.join(os.path.expanduser('~'), 'ChromeDriverProfile')
            os.makedirs(chrome_profile, exist_ok=True)

            # Важные опции для стабильности Chrome на macOS
            options.add_argument(f"--user-data-dir={chrome_profile}")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")

            # Эти опции помогут избежать ошибки DevToolsActivePort
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-background-timer-throttling")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-default-apps")

            # Важно: НЕ используем headless для Chrome на macOS
            options.add_experimental_option("detach", True)  # Не закрывать Chrome после завершения сессии

            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            session = HTMLSession(driver=driver, browser_name=browser_name)
            return session
        else:
            # Создаем экземпляр HTMLSession с нужными параметрами
            session = HTMLSession(use_safari=use_safari, incognito=incognito)
            return session

    except Exception as e:
        logger.error(f"Ошибка при инициализации {browser_name}: {e}")
        traceback.print_exc()
        raise

def launch_safari_for_scraping():
    """Открывает Twitter в Safari для авторизации"""
    import subprocess
    import platform

    if platform.system() != "Darwin":  # Проверка что это macOS
        logger.error("Safari доступен только на macOS")
        return False

    try:
        # Выполним дополнительные подготовительные команды
        try:
            subprocess.run(['sudo', 'safaridriver', '--enable'], check=True)
            logger.info("SafariDriver включен с повышенными привилегиями")
        except Exception as e:
            logger.warning(f"Не удалось включить SafariDriver: {e}")

        try:
            subprocess.run(['sudo', 'mkdir', '-p', '/var/db/webkit/WebKitDriverStorage/'], check=False)
            subprocess.run(['sudo', 'chown', '-R', '_webkitagent', '/var/db/webkit/WebKitDriverStorage/'], check=False)
            logger.info("Настроены права для WebKitDriverStorage")
        except Exception as e:
            logger.warning(f"Не удалось настроить права для WebKitDriverStorage: {e}")

        # Сначала проверяем, не открыт ли уже Safari
        try:
            process = subprocess.Popen(['ps', '-A'], stdout=subprocess.PIPE)
            output, _ = process.communicate()
            if b'Safari.app' in output:
                logger.info("Safari уже запущен, отправляем команду на открытие Twitter")
                subprocess.run(['open', '-a', 'Safari', 'https://twitter.com/login'], check=True)
            else:
                # Открываем Twitter в Safari для авторизации
                logger.info("Открываем Twitter в Safari для авторизации...")
                subprocess.Popen(['open', '-a', 'Safari', 'https://twitter.com/login'])
        except:
            # На всякий случай, если не сработает проверка процессов
            subprocess.Popen(['open', '-a', 'Safari', 'https://twitter.com/login'])

        logger.info("Twitter открыт в Safari для авторизации")

        return True
    except Exception as e:
        logger.error(f"Ошибка при открытии Safari: {e}")
        traceback.print_exc()
        return False


def launch_chrome_for_scraping():
    """Открывает Twitter в Chrome для авторизации"""
    import subprocess
    import platform
    import os
    import signal
    import time

    try:
        logger.info("Открываем Twitter в Chrome для авторизации...")

        # Создаем директорию для профиля, если нужно
        chrome_profile = os.path.join(os.path.expanduser('~'), 'ChromeDriverProfile')
        os.makedirs(chrome_profile, exist_ok=True)
        logger.info(f"Создана директория профиля: {chrome_profile}")

        # Проверяем, не запущен ли уже Chrome с этим профилем
        if platform.system() == "Darwin":  # macOS
            try:
                # Проверяем процессы Chrome
                process = subprocess.Popen(['ps', 'aux'], stdout=subprocess.PIPE)
                output, _ = process.communicate()
                if chrome_profile.encode() in output:
                    logger.info("Chrome уже запущен с этим профилем, убиваем процесс")
                    # Находим и убиваем процессы Chrome с нашим профилем
                    grep_proc = subprocess.Popen(['grep', 'Chrome'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                    grep_proc.stdin.write(output)
                    grep_proc.stdin.close()
                    chrome_processes = grep_proc.stdout.read().decode('utf-8').strip().split('\n')

                    for proc in chrome_processes:
                        if chrome_profile in proc:
                            parts = proc.split()
                            if len(parts) > 1:
                                try:
                                    pid = int(parts[1])
                                    os.kill(pid, signal.SIGTERM)
                                    logger.info(f"Убит процесс Chrome с PID {pid}")
                                except:
                                    pass
                    # Даем время на закрытие
                    time.sleep(1)
            except Exception as e:
                logger.warning(f"Ошибка при проверке процессов Chrome: {e}")

            # Открываем Chrome с профилем
            chrome_cmd = [
                'open',
                '-a', 'Google Chrome',
                '--args',
                f'--user-data-dir={chrome_profile}',
                '--no-first-run',
                '--no-default-browser-check',
                'https://twitter.com/login'
            ]

            subprocess.Popen(chrome_cmd)
            logger.info("Chrome запущен с командой: " + " ".join(chrome_cmd))

        elif platform.system() == "Windows":
            chrome_path = os.path.join(os.environ.get('PROGRAMFILES', 'C:\\Program Files'),
                                       'Google\\Chrome\\Application\\chrome.exe')
            if not os.path.exists(chrome_path):
                chrome_path = os.path.join(os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)'),
                                           'Google\\Chrome\\Application\\chrome.exe')

            if os.path.exists(chrome_path):
                subprocess.Popen([
                    chrome_path,
                    f'--user-data-dir={chrome_profile}',
                    '--no-first-run',
                    '--no-default-browser-check',
                    'https://twitter.com/login'
                ])
            else:
                subprocess.Popen(['start', 'chrome',
                                  f'--user-data-dir={chrome_profile}',
                                  'https://twitter.com/login'], shell=True)
        else:  # Linux или другие системы
            subprocess.Popen([
                'google-chrome',
                f'--user-data-dir={chrome_profile}',
                '--no-first-run',
                '--no-default-browser-check',
                'https://twitter.com/login'
            ])

        logger.info("Twitter открыт в Chrome для авторизации")
        return True
    except Exception as e:
        logger.error(f"Ошибка при открытии Chrome: {e}")
        traceback.print_exc()
        return False

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


async def check_nitter_instance_status(instance):
    """Проверяет работоспособность Nitter-инстанса"""
    try:
        timeout = aiohttp.ClientTimeout(total=5)  # Короткий таймаут
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{instance}/", headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status == 200:
                    try:
                        text = await response.text()
                        if "nitter" in text.lower() or "twitter" in text.lower():
                            return True
                    except:
                        pass
        return False
    except:
        return False


async def get_working_nitter_instances():
    """Возвращает список работающих Nitter-инстансов"""
    working_instances = []
    tasks = [check_nitter_instance_status(instance) for instance in NITTER_INSTANCES]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, is_working in enumerate(results):
        if is_working and not isinstance(is_working, Exception):
            working_instances.append(NITTER_INSTANCES[i])
            logger.info(f"Nitter инстанс доступен: {NITTER_INSTANCES[i]}")

    if working_instances:
        return working_instances
    else:
        logger.warning("Нет доступных Nitter-инстансов. Используем список по умолчанию.")
        # Возвращаем первые 3 инстанса из списка, даже если они не работают
        return NITTER_INSTANCES[:3]


async def update_nitter_instances():
    """Проверяет и обновляет список рабочих Nitter-инстансов"""
    # Проверяем, что цикл событий запущен
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.error("Невозможно обновить Nitter-инстансы: цикл событий не запущен")
        return []

    working_instances = []

    try:
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
    except Exception as e:
        logger.error(f"Ошибка при обновлении Nitter-инстансов: {e}")
        return NITTER_INSTANCES[:3]  # В случае ошибки возвращаем первые 3 инстанса


class TwitterClient:
    def __init__(self, bearer_token):
        self.bearer_token = bearer_token
        self.rate_limited = False
        self.rate_limit_reset = 0
        self.user_agent = UserAgent().random
        self.cache = {}
        self.session = requests.Session()
        # Отключаем проверку SSL для решения проблем с сертификатами
        self.session.verify = False
        # Подавляем предупреждения о небезопасных запросах
        import urllib3
        urllib3.disable_warnings()

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

        limits = load_json(API_LIMITS_FILE, {})
        limits["twitter_api"] = {
            "rate_limited": True,
            "reset_time": reset_time,
            "updated_at": int(time.time())
        }
        save_json(API_LIMITS_FILE, limits)

    def get_user_by_username(self, username):
        if not self.bearer_token or not self.check_rate_limit():
            return None

        cached_user = get_from_cache("users", username.lower(), 86400)
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
                    update_cache("users", username.lower(), data["data"])
                    return data["data"]
            else:
                logger.error(f"Ошибка при получении пользователя: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"Ошибка запроса к API: {e}")

        return None

    def get_user_id(self, username, use_proxies=False):
        """Получает Twitter ID пользователя по имени аккаунта"""
        logger.info(f"Запрос ID пользователя для @{username}...")

        # Проверяем кеш пользователя
        cached_user_data = get_from_cache("users", username.lower(), 86400)  # Кеш на 24 часа
        if cached_user_data and "id" in cached_user_data:
            logger.info(f"ID пользователя @{username} найден в кеше: {cached_user_data['id']}")
            return cached_user_data["id"]

        # Проверяем лимиты API
        if not self.bearer_token or not self.check_rate_limit():
            return None

        url = f"https://api.twitter.com/2/users/by/username/{username}"
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": self.user_agent
        }

        try:
            proxies = get_random_proxy() if use_proxies else None
            response = self.session.get(url, headers=headers, proxies=proxies, timeout=10)

            if response.status_code == 429:
                reset_time = int(response.headers.get("x-rate-limit-reset", time.time() + 900))
                self.set_rate_limit(reset_time)
                remaining = int(response.headers.get("x-rate-limit-remaining", 0))
                limit = int(response.headers.get("x-rate-limit-limit", 0))
                logger.warning(
                    f"API лимит запросов: {remaining}/{limit}. Сброс в {reset_time}"
                )
                return None

            if response.status_code == 200:
                data = response.json()
                if "data" in data and "id" in data["data"]:
                    user_id = data["data"]["id"]
                    # Сохраняем в кеш с данными пользователя
                    update_cache("users", username.lower(), data["data"])
                    logger.info(f"Получен ID пользователя @{username}: {user_id}")
                    return user_id
                else:
                    logger.warning(f"ID пользователя @{username} не найден в ответе API")
                    return None
            else:
                logger.warning(f"Ошибка API {response.status_code} при запросе ID @{username}")
                return None

        except Exception as e:
            logger.error(f"Ошибка при получении ID пользователя @{username}: {e}")
            return None

    def get_user_tweets(self, user_id, use_proxies=False):
        # Проверяем нужно ли вообще делать запрос к API
        if not self.bearer_token or not self.check_rate_limit():
            return None

        settings = get_settings()
        api_request_limit = settings.get("api_request_limit", 20)
        logger.info(f"Запрос твитов для user_id={user_id}, лимит API: {api_request_limit}")

        url = f"https://api.twitter.com/2/users/{user_id}/tweets"
        params = {
            "max_results": api_request_limit,
            "tweet.fields": "created_at,text,attachments,public_metrics",
            "exclude": "retweets,replies",
            "expansions": "attachments.media_keys",
            "media.fields": "type,url,preview_image_url"
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
                includes = data.get("includes", {})

                if tweets and "media" in includes:
                    media_map = {m["media_key"]: m for m in includes["media"]}

                    for tweet in tweets:
                        if "attachments" in tweet and "media_keys" in tweet["attachments"]:
                            media_keys = tweet["attachments"]["media_keys"]
                            tweet["media"] = []

                            for key in media_keys:
                                if key in media_map:
                                    tweet["media"].append(media_map[key])

                return tweets
            else:
                logger.error(f"Ошибка при получении твитов: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"Ошибка запроса к API: {e}")

        return None

    def get_latest_tweet(self, username, last_known_id=None, use_proxies=False):
        """Получает последний твит пользователя через API Twitter"""
        logger.info(f"Запрос твитов для @{username} через API...")

        # Если передан последний известный ID, проверяем нужно ли запрашивать API
        # Используем API только если другие методы нашли более новый твит, но данные о нем неполные
        # Или если другие методы не нашли твит вообще
        if last_known_id:
            # Проверяем кеш API твитов
            cached_data = get_from_cache("tweets", f"api_{username.lower()}", 3600)  # Кеш на 1 час
            if cached_data and "tweet_id" in cached_data:
                cached_id = cached_data["tweet_id"]
                # Если в кеше уже есть этот ID, используем его
                if cached_id == last_known_id:
                    logger.info(f"Найден твит {cached_id} в кеше API для @{username}")
                    return cached_data.get("user_id"), cached_id, cached_data.get("tweet_data")

        # Проверяем, нужно ли вообще обращаться к API
        # (если у нас лимиты исчерпаны или ключа нет - не запрашиваем)
        if not self.bearer_token or not self.check_rate_limit():
            logger.info("API недоступен из-за лимитов или отсутствия ключа")
            return None, None, None

        # Получаем ID пользователя
        user_id = self.get_user_id(username, use_proxies)
        if not user_id:
            logger.warning(f"Не удалось получить ID пользователя @{username}")
            return None, None, None

        # Получаем твиты пользователя
        tweets = self.get_user_tweets(user_id, use_proxies)
        if not tweets:
            logger.warning(f"Не удалось получить твиты для @{username}")
            return user_id, None, None

        try:
            if not isinstance(tweets, list) or len(tweets) == 0:
                logger.warning(f"Получен пустой или неправильный список твитов для @{username}")
                return user_id, None, None

            # Выбираем первый (самый новый) твит
            tweet = tweets[0]
            tweet_id = tweet["id"]
            tweet_text = tweet["text"]
            tweet_created_at = tweet.get("created_at", "")

            # Если нам передан известный ID, проверяем не старше ли полученный твит
            if last_known_id:
                try:
                    # Сравниваем ID
                    if int(tweet_id) <= int(last_known_id):
                        logger.info(f"API вернул более старый или тот же твит ({tweet_id}) для @{username}")
                        # Вернем ID пользователя и известный ID твита, но без данных
                        return user_id, last_known_id, None
                except (ValueError, TypeError):
                    pass

            # Формируем дату в читаемом формате
            formatted_date = ""
            if tweet_created_at:
                try:
                    dt = datetime.fromisoformat(tweet_created_at.replace("Z", "+00:00"))
                    formatted_date = dt.strftime("%d %b %Y, %H:%M")
                except:
                    formatted_date = tweet_created_at

            # Собираем данные о твите
            tweet_data = {
                "text": tweet_text,
                "url": f"https://twitter.com/{username}/status/{tweet_id}",
                "created_at": tweet_created_at,
                "formatted_date": formatted_date,
                "is_pinned": False,
                "has_media": "attachments" in tweet,
                "likes": tweet.get("public_metrics", {}).get("like_count", 0),
                "retweets": tweet.get("public_metrics", {}).get("retweet_count", 0)
            }

            # Обработка медиа-вложений
            if "attachments" in tweet and "media_keys" in tweet["attachments"] and "media" in tweet:
                media = []
                for item in tweet["media"]:
                    media_url = item.get("url", "") or item.get("preview_image_url", "")
                    if media_url:
                        media.append({
                            "type": item.get("type", "photo"),
                            "url": media_url
                        })

                if media:
                    tweet_data["media"] = media

            # Добавляем в кэш
            update_cache("tweets", f"api_{username.lower()}", {
                "user_id": user_id,
                "tweet_id": tweet_id,
                "tweet_data": tweet_data
            })

            logger.info(f"API нашел твит: {tweet_id}")
            return user_id, tweet_id, tweet_data

        except Exception as e:
            logger.error(f"Ошибка при обработке твитов для @{username}: {e}")
            traceback.print_exc()
            return user_id, None, None


class NitterScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml",
            "Cache-Control": "no-cache"
        })
        # Отключаем проверку SSL для работы со всеми инстансами
        self.session.verify = False
        # Подавляем предупреждения о небезопасных запросах
        import urllib3
        urllib3.disable_warnings()
        self.nitter_failures = {}

    def get_random_user_agent(self):
        agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36 Edg/92.0.902.55"
        ]
        return random.choice(agents)

    def report_nitter_failure(self, instance):
        if instance not in self.nitter_failures:
            self.nitter_failures[instance] = 0
        self.nitter_failures[instance] += 1

    def get_healthy_nitter_instances(self, max_failures=3):
        settings = get_settings()
        nitter_instances = settings.get("nitter_instances", NITTER_INSTANCES)

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

    def validate_tweet_id(self, username, tweet_id):
        if not tweet_id:
            return False
        if len(str(tweet_id)) < 15:
            logger.warning(f"Слишком короткий ID твита для @{username}: {tweet_id}")
            return False
        return True

    def get_latest_tweet_nitter(self, username, last_known_id=None, use_proxies=False):
        """Получает последний твит через Nitter с проверкой инстансов"""
        logger.info(f"Запрос твитов для @{username} через Nitter...")

        try:
            # Получаем список здоровых инстансов Nitter
            settings = get_settings()
            nitter_instances = settings.get("nitter_instances", NITTER_INSTANCES)

            if not nitter_instances:
                logger.error("Нет доступных Nitter-инстансов")
                return None, None

            headers = {
                'User-Agent': self.get_random_user_agent(),
                'Accept-Language': 'en-US,en;q=0.9',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            }

            # Перебираем инстансы в случайном порядке
            random.shuffle(nitter_instances)

            newest_tweet_id = None
            newest_tweet_data = None
            newest_timestamp = None

            # Пробуем разные инстансы Nitter
            for nitter in nitter_instances[:3]:
                try:
                    # Добавляем случайное число для обхода кеширования
                    cache_buster = f"?r={int(time.time())}"
                    full_url = f"{nitter}/{username}{cache_buster}"

                    logger.info(f"Попытка получения твитов через {nitter}...")

                    proxies = get_random_proxy() if use_proxies else None
                    nitter_response = self.session.get(full_url, headers=headers, proxies=proxies, timeout=15)

                    if nitter_response.status_code != 200:
                        logger.warning(f"Nitter {nitter} вернул код {nitter_response.status_code}")
                        self.report_nitter_failure(nitter)
                        continue

                    soup = BeautifulSoup(nitter_response.text, 'html.parser')

                    # Поиск всех твитов
                    tweet_divs = soup.select('.timeline-item')

                    if not tweet_divs:
                        logger.warning(f"Не найдены твиты на {nitter} для @{username}")
                        self.report_nitter_failure(nitter)
                        continue

                    logger.info(f"Найдено {len(tweet_divs)} твитов на {nitter}")

                    # Проходим по всем найденным твитам
                    for tweet_div in tweet_divs:
                        # Проверяем на закрепленный твит
                        is_pinned = bool(tweet_div.select_one('.pinned'))

                        # Проверяем на ретвит
                        is_retweet = bool(tweet_div.select_one('.retweet-header'))

                        # Пропускаем закрепленные твиты и ретвиты если есть последний известный ID
                        if last_known_id and (is_pinned or is_retweet):
                            continue

                        # Извлекаем дату твита
                        tweet_date = tweet_div.select_one('.tweet-date a')
                        if not tweet_date or not tweet_date.get('title'):
                            continue

                        # Формат даты в Nitter: "Mar 28, 2025 · 10:50 PM UTC"
                        date_str = tweet_date.get('title')
                        display_date = date_str

                        try:
                            # Пробуем разные форматы дат
                            date_formats = [
                                '%b %d, %Y · %I:%M %p UTC',  # Mar 28, 2025 · 10:50 PM UTC
                                '%d %b %Y · %H:%M:%S UTC',  # 28 Mar 2025 · 22:50:00 UTC
                                '%B %d, %Y · %I:%M %p UTC',  # March 28, 2025 · 10:50 PM UTC
                                '%Y-%m-%d %H:%M:%S'  # 2025-03-28 22:50:09
                            ]

                            tweet_datetime = None
                            for fmt in date_formats:
                                try:
                                    tweet_datetime = datetime.strptime(date_str, fmt)
                                    break
                                except:
                                    continue

                            if not tweet_datetime:
                                # Если не удалось распознать дату, пропускаем твит
                                continue

                            tweet_timestamp = tweet_datetime.timestamp()
                        except Exception as e:
                            continue

                        # Ссылка на твит и извлечение ID
                        tweet_link = tweet_div.select_one('.tweet-link')
                        if not tweet_link or not tweet_link.get('href'):
                            continue

                        # Путь к твиту типа /username/status/12345678
                        href = tweet_link.get('href')
                        # Извлекаем ID
                        match = re.search(r'/status/(\d+)', href)
                        if not match:
                            continue

                        tweet_id = match.group(1)

                        # Если передан последний известный ID, проверяем, новее ли текущий
                        if last_known_id:
                            try:
                                if int(tweet_id) <= int(last_known_id):
                                    logger.info(
                                        f"Nitter: твит {tweet_id} не новее последнего известного {last_known_id}")
                                    continue  # Пропускаем этот твит, ищем более новые
                            except (ValueError, TypeError):
                                # При ошибке сравнения проверяем по времени
                                pass

                        # Проверяем, является ли этот твит новее найденного ранее
                        if newest_timestamp is None or tweet_timestamp > newest_timestamp:
                            newest_timestamp = tweet_timestamp
                            newest_tweet_id = tweet_id

                            # Текст твита
                            tweet_content = tweet_div.select_one('.tweet-content')
                            tweet_text = tweet_content.get_text() if tweet_content else "[Текст недоступен]"

                            # URL твита
                            tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

                            # Проверяем наличие медиа
                            has_images = bool(tweet_div.select('.attachments .attachment-image'))
                            has_video = bool(tweet_div.select('.attachments .attachment-video'))

                            # Получаем метрики, если доступны
                            stats = tweet_div.select('.tweet-stats .icon-container')
                            likes = 0
                            retweets = 0

                            for stat in stats:
                                stat_text = stat.get_text(strip=True)
                                if "retweet" in stat.get('class', []):
                                    try:
                                        retweets = int(stat_text)
                                    except:
                                        pass
                                elif "heart" in stat.get('class', []):
                                    try:
                                        likes = int(stat_text)
                                    except:
                                        pass

                            # Собираем медиа ссылки
                            media = []
                            if has_images:
                                for img in tweet_div.select('.attachments .attachment-image img'):
                                    if img.get('src'):
                                        media.append({
                                            "type": "photo",
                                            "url": img['src']
                                        })

                            if has_video:
                                for video in tweet_div.select('.attachments .attachment-video source'):
                                    if video.get('src'):
                                        media.append({
                                            "type": "video",
                                            "url": video['src']
                                        })

                            # Данные о твите
                            newest_tweet_data = {
                                "text": tweet_text,
                                "url": tweet_url,
                                "is_pinned": is_pinned,
                                "is_retweet": is_retweet,
                                "created_at": str(tweet_datetime) if tweet_datetime else "",
                                "formatted_date": display_date,
                                "timestamp": tweet_timestamp,
                                "has_media": has_images or has_video,
                                "likes": likes,
                                "retweets": retweets,
                                "media": media if (has_images or has_video) else []
                            }

                            logger.info(f"Найден твит от {display_date}, ID: {tweet_id}")

                    # Если нашли хотя бы один твит, останавливаемся
                    if newest_tweet_id:
                        break

                except Exception as e:
                    logger.error(f"Ошибка при обращении к {nitter}: {e}")
                    self.report_nitter_failure(nitter)
                    continue

            # Если нашли хотя бы один твит
            if newest_tweet_id and self.validate_tweet_id(username, newest_tweet_id):
                logger.info(f"Самый новый твит (ID: {newest_tweet_id}) от {newest_tweet_data.get('formatted_date')}")

                # Сохраняем в кеш с принудительной очисткой старых данных
                update_cache("tweets", f"nitter_{username.lower()}", {
                    "tweet_id": newest_tweet_id,
                    "tweet_data": newest_tweet_data,
                    "updated_at": time.time()
                }, force=True)

                return newest_tweet_id, newest_tweet_data

            logger.warning(f"Не удалось найти твиты для @{username} через все доступные серверы Nitter")

        except Exception as e:
            logger.error(f"Общая ошибка при получении твитов для @{username} через Nitter: {e}")
            traceback.print_exc()

        return None, None


class WebScraper:
    def __init__(self):
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:96.0) Gecko/20100101 Firefox/96.0"
        ]
        # Для ротации используем только Safari и Chrome (без разделения на safari/chrome)
        self.browser_rotation = ["safari", "chrome"]
        self.current_browser_index = 0

    def get_random_user_agent(self):
        return random.choice(self.user_agents)

    def validate_tweet_id(self, username, tweet_id):
        if not tweet_id:
            return False
        if len(str(tweet_id)) < 15:
            logger.warning(f"Слишком короткий ID твита для @{username}: {tweet_id}")
            return False
        return True

    def _get_next_browser_config(self):
        """Возвращает следующий браузер в ротации"""
        browser = self.browser_rotation[self.current_browser_index]
        self.current_browser_index = (self.current_browser_index + 1) % len(self.browser_rotation)

        if browser == "safari":
            return True, False  # Safari, не инкогнито
        else:  # chrome
            return False, False  # Chrome, не инкогнито

    def get_latest_tweet_web(self, username, last_known_id=None, use_proxies=False, max_retries=2, browser_override=None):
        """Улучшенный веб-скрапинг Twitter с использованием разных браузеров при неудачах"""
        logger.info(f"Запрос твитов для @{username} через веб-скрапинг...")

        # Проверяем, нужно ли делать запрос через веб, если есть последний известный ID
        # Если у нас есть данные в кеше и это не первая проверка, то проверяем кеш
        if last_known_id:
            cached_data = get_from_cache("tweets", f"web_{username.lower()}", 3600)
            if cached_data and cached_data.get("tweet_id") == last_known_id:
                logger.info(f"Найден твит {last_known_id} в кеше для @{username}, пропускаем веб-скрапинг")
                return last_known_id, cached_data.get("tweet_data")

        retry_count = 0
        while retry_count < max_retries:
            # Выбираем браузер для этой попытки
            if browser_override:
                # Используем явно указанный браузер
                if browser_override == "safari":
                    use_safari, incognito = True, False
                elif browser_override == "chrome":
                    use_safari, incognito = False, False
                else:
                    # Если указан неизвестный браузер, используем следующий по ротации
                    use_safari, incognito = self._get_next_browser_config()
            else:
                # Используем следующий браузер в ротации
                use_safari, incognito = self._get_next_browser_config()

            browser_name = "Safari" if use_safari else "Chrome"
            logger.info(f"Попытка {retry_count + 1}/{max_retries} через {browser_name}")

            try:
                with HTMLSession(use_safari=use_safari, incognito=incognito) as session:
                    # URL для страницы со свежими твитами
                    url = f"https://twitter.com/{username}?s=20"

                    logger.info(f"Загрузка страницы {url} через {browser_name}")
                    session.get(url)

                    # Дополнительные действия для имитации человека
                    # Прокрутка страницы для загрузки контента
                    session.driver.execute_script("window.scrollTo(0, 400)")
                    time.sleep(random.uniform(0.7, 1.3))

                    # Случайный скроллинг
                    session.driver.execute_script(f"window.scrollBy(0, {random.randint(200, 400)});")
                    time.sleep(random.uniform(0.5, 1))

                    # Собираем данные о твитах с помощью JavaScript
                    tweets_data = session.driver.execute_script(r"""
                        function extractTweets() {
                            const tweets = [];
                            try {
                                const tweetElements = document.querySelectorAll('article[data-testid="tweet"]');
                                console.log(`Найдено ${tweetElements.length} твитов на странице`);

                                for (const article of tweetElements) {
                                    try {
                                        const socialContext = article.querySelector('[data-testid="socialContext"]');
                                        const isPinned = socialContext && 
                                            (socialContext.textContent.includes('Pinned') || 
                                             socialContext.textContent.includes('Закрепленный') ||
                                             socialContext.textContent.includes('закреплен'));

                                        let tweetId = null;
                                        const links = article.querySelectorAll('a[href*="/status/"]');
                                        for (const link of links) {
                                            const match = link.href.match(/\/status\/(\d+)/);
                                            if (match && match[1]) {
                                                tweetId = match[1];
                                                break;
                                            }
                                        }

                                        if (!tweetId) continue;

                                        const textElement = article.querySelector('[data-testid="tweetText"]');
                                        const tweetText = textElement ? textElement.innerText : '';

                                        let timestamp = '';
                                        let displayDate = '';
                                        const timeElement = article.querySelector('time');
                                        if (timeElement) {
                                            timestamp = timeElement.getAttribute('datetime');
                                            displayDate = timeElement.innerText;
                                        }

                                        const photoElements = article.querySelectorAll('[data-testid="tweetPhoto"]');
                                        const mediaUrls = [];

                                        for (const photoEl of photoElements) {
                                            const img = photoEl.querySelector('img');
                                            if (img && img.src) {
                                                let imgUrl = img.src;
                                                imgUrl = imgUrl.replace('&name=small', '&name=large');
                                                imgUrl = imgUrl.replace('&name=thumb', '&name=large');
                                                mediaUrls.push({
                                                    type: 'photo',
                                                    url: imgUrl
                                                });
                                            }
                                        }

                                        tweets.push({
                                            id: tweetId,
                                            text: tweetText,
                                            timestamp: timestamp,
                                            displayDate: displayDate,
                                            isPinned: isPinned,
                                            hasMedia: photoElements.length > 0,
                                            media: mediaUrls
                                        });
                                    } catch(e) {
                                        console.error("Ошибка обработки твита:", e);
                                    }
                                }
                            } catch(e) {
                                console.error("Ошибка сбора твитов:", e);
                            }

                            return tweets;
                        }
                        return extractTweets();
                    """)

                    logger.info(f"Извлечено {len(tweets_data) if tweets_data else 0} твитов для @{username}")

                    if tweets_data and len(tweets_data) > 0:
                        # Отфильтровываем закрепленные твиты если ищем обновления
                        if last_known_id:
                            regular_tweets = [t for t in tweets_data if not t.get('isPinned')]
                            target_tweets = regular_tweets or tweets_data
                        else:
                            target_tweets = tweets_data

                        # Сортируем по ID (самые новые в начале)
                        try:
                            target_tweets.sort(key=lambda x: int(x.get('id', '0')), reverse=True)
                        except:
                            pass

                        if target_tweets:
                            selected_tweet = target_tweets[0]
                            tweet_id = selected_tweet.get('id')

                            # Проверяем, новее ли найденный твит последнего известного
                            if last_known_id:
                                try:
                                    is_newer = int(tweet_id) > int(last_known_id)
                                    if not is_newer:
                                        # Если не нашли более новый твит, сразу возвращаем результат
                                        # ИЗМЕНЕНИЕ: Не продолжаем проверку следующими браузерами, если нашли твит
                                        logger.warning(
                                            f"Web через {browser_name} не нашел новый твит для @{username} (текущий: {last_known_id})")
                                        return last_known_id, cached_data.get("tweet_data") if cached_data else None
                                except (ValueError, TypeError):
                                    pass

                            if not self.validate_tweet_id(username, tweet_id):
                                logger.warning(f"Некорректный ID твита: {tweet_id}")
                                retry_count += 1
                                continue

                            # Формируем данные о твите
                            tweet_data = {
                                "text": selected_tweet.get('text', '[Текст недоступен]'),
                                "url": f"https://twitter.com/{username}/status/{tweet_id}",
                                "created_at": selected_tweet.get('timestamp', ''),
                                "formatted_date": selected_tweet.get('displayDate', 'неизвестная дата'),
                                "is_pinned": selected_tweet.get('isPinned', False),
                                "has_media": selected_tweet.get('hasMedia', False),
                                "media": selected_tweet.get('media', []),
                                "browser_used": browser_name
                            }

                            # Обновляем кеш
                            update_cache("tweets", f"web_{username.lower()}", {
                                "tweet_id": tweet_id,
                                "tweet_data": tweet_data
                            })

                            logger.info(f"Найден твит ID {tweet_id} для @{username} через {browser_name}")
                            return tweet_id, tweet_data

                    # Если не нашли твиты, переходим к следующему браузеру
                    retry_count += 1

            except Exception as e:
                retry_count += 1
                logger.error(f"Ошибка при получении твитов для @{username} через {browser_name}: {e}")
                if retry_count < max_retries:
                    logger.info(f"Повторная попытка {retry_count}/{max_retries} для @{username}")
                time.sleep(random.uniform(1.5, 3))  # Случайная пауза перед повторной попыткой

        return None, None


async def send_tweet_with_media(app, subs, username, tweet_id, tweet_data):
    # Формируем сообщение
    tweet_text = tweet_data.get('text', '[Новый твит]')
    tweet_url = tweet_data.get('url', f"https://twitter.com/{username}/status/{tweet_id}")
    formatted_date = tweet_data.get('formatted_date', '')

    likes = tweet_data.get('likes', 0)
    retweets = tweet_data.get('retweets', 0)
    browser_used = tweet_data.get('browser_used', '')

    # Формируем метрики
    metrics_text = f"👍 {likes} · 🔄 {retweets}" if likes or retweets else ""
    if browser_used:
        metrics_text += f" · 🌐 {browser_used}" if metrics_text else f"🌐 {browser_used}"

    # Основное сообщение
    tweet_msg = f"🐦 @{username}"

    # Добавляем дату
    if formatted_date:
        tweet_msg += f" · {formatted_date}"

    # Добавляем текст
    tweet_msg += f"\n\n{tweet_text}"

    # URL и метрики
    footer = f"\n\n{tweet_url}"
    if metrics_text:
        footer += f"\n\n{metrics_text}"

    # Проверяем наличие медиа
    media = tweet_data.get('media', [])
    has_media = tweet_data.get('has_media', False) or len(media) > 0

    # Если subs - это просто ID чата (не список), преобразуем в список
    if not isinstance(subs, list):
        subs = [subs]

    for chat_id in subs:
        try:
            # Если нет медиа, отправляем обычное сообщение
            if not has_media:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=tweet_msg + footer,
                    disable_web_page_preview=False
                )
                continue

            # Ищем URL фотографий
            photo_urls = []
            for item in media:
                if isinstance(item, dict) and 'type' in item and item.get('type',
                                                                          '').lower() == 'photo' and 'url' in item:
                    photo_urls.append(item['url'])

            # Если нашли фото
            if photo_urls:
                # Ограничение длины подписи в Telegram
                caption = (tweet_msg + footer)[:1024]

                # Отправляем первое фото с подписью
                await app.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_urls[0],
                    caption=caption
                )

                # Если есть дополнительные фото, отправляем их отдельно
                for url in photo_urls[1:]:
                    try:
                        await app.bot.send_photo(
                            chat_id=chat_id,
                            photo=url
                        )
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.error(f"Ошибка при отправке дополнительного фото: {e}")
            else:
                # Если фото не нашли, отправляем обычное сообщение с превью
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=tweet_msg + footer,
                    disable_web_page_preview=False
                )

            await asyncio.sleep(0.5)  # Небольшая задержка между сообщениями

        except Exception as e:
            logger.error(f"Ошибка отправки сообщения в чат {chat_id}: {e}")
            # В случае ошибки отправляем текстовое сообщение
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=tweet_msg + footer,
                    disable_web_page_preview=False
                )
            except:
                pass


async def check_tweet_multi_method(username, account_methods=None, use_proxies=False, max_retries=2):
    """Проверяет твиты всеми доступными методами с учетом индивидуальных настроек аккаунта и порядком приоритета"""
    # Получаем настройки методов
    settings = get_settings()
    accounts = init_accounts()
    account = accounts.get(username.lower(), {})
    last_known_id = account.get('last_tweet_id')

    # Определяем какие методы использовать
    if account_methods:
        methods = account_methods
    elif account.get("scraper_methods"):
        methods = account["scraper_methods"]
        logger.info(f"Используем индивидуальные методы для @{username}: {methods}")
    else:
        methods = settings.get("scraper_methods", ["nitter", "web", "api"])
        logger.info(f"Используем общие методы скрапинга: {methods}")

    twitter_client = TwitterClient(TWITTER_BEARER)
    nitter_scraper = NitterScraper()
    web_scraper = WebScraper()

    results = {
        "api": {"user_id": None, "tweet_id": None, "tweet_data": None},
        "nitter": {"tweet_id": None, "tweet_data": None},
        "web": {"tweet_id": None, "tweet_data": None}
    }

    found_new_id = False  # Флаг, определяющий нашли ли мы новый ID

    # Проверяем методы в указанном порядке
    for method in methods:
        try:
            # Если мы уже нашли новый твит, прерываем дальнейший поиск
            if found_new_id:
                logger.info(f"Уже нашли новый твит методом {next(k for k, v in results.items() if v.get('tweet_id'))}, пропускаем остальные методы")
                break

            if method == "nitter":
                tweet_id, tweet_data = nitter_scraper.get_latest_tweet_nitter(username, last_known_id, use_proxies)
                if tweet_id:
                    results["nitter"]["tweet_id"] = tweet_id
                    results["nitter"]["tweet_data"] = tweet_data
                    logger.info(f"Nitter нашел твит: {tweet_id}")
                    if last_known_id and int(tweet_id) > int(last_known_id):
                        found_new_id = True

            elif method == "api" and TWITTER_BEARER and not twitter_client.rate_limited:
                user_id, tweet_id, tweet_data = twitter_client.get_latest_tweet(username, last_known_id, use_proxies)
                if user_id:
                    results["api"]["user_id"] = user_id
                if tweet_id:
                    results["api"]["tweet_id"] = tweet_id
                    results["api"]["tweet_data"] = tweet_data
                    logger.info(f"API нашел твит: {tweet_id}")
                    if last_known_id and int(tweet_id) > int(last_known_id):
                        found_new_id = True

            elif method == "web":
                # Используем только обычный веб-скрапинг без разделения на Chrome/Safari
                tweet_id, tweet_data = web_scraper.get_latest_tweet_web(username, last_known_id, use_proxies, max_retries)
                if tweet_id:
                    results["web"]["tweet_id"] = tweet_id
                    results["web"]["tweet_data"] = tweet_data
                    logger.info(f"Web нашел твит: {tweet_id}")
                    if last_known_id and int(tweet_id) > int(last_known_id):
                        found_new_id = True

        except Exception as e:
            logger.error(f"Ошибка при проверке {username} методом {method}: {e}")
            traceback.print_exc()

    # Собираем все найденные ID твитов
    tweet_ids = {}
    for method, data in results.items():
        if data["tweet_id"]:
            tweet_ids[method] = data["tweet_id"]

    logger.info(f"Найденные ID для @{username}: {tweet_ids}")

    # Если ничего не нашли
    if not tweet_ids:
        return None, None, None, None

    # Выбираем самый большой ID (самый новый твит)
    try:
        newest_method, newest_id = max(tweet_ids.items(), key=lambda x: int(x[1]))
        logger.info(f"Выбран самый новый твит: {newest_id} (метод: {newest_method})")
    except (ValueError, TypeError):
        # При ошибке берем первый найденный
        newest_method = next(iter(tweet_ids))
        newest_id = tweet_ids[newest_method]
        logger.warning(f"Не удалось сравнить ID твитов, выбран первый: {newest_id}")

    # Получаем user_id из API (если был найден)
    user_id = results["api"]["user_id"]
    # Получаем данные твита от выбранного метода
    tweet_data = results[newest_method]["tweet_data"]

    # Если данных нет, но есть твит - попробуем данные из другого метода
    if newest_id and not tweet_data:
        for method, data in results.items():
            if data["tweet_id"] == newest_id and data["tweet_data"]:
                tweet_data = data["tweet_data"]
                break

    return user_id, newest_id, tweet_data, newest_method

async def process_account(app, subs, accounts, username, account, methods, use_proxies):
    """Обрабатывает один аккаунт и отправляет уведомления при новых твитах"""
    try:
        # Обновляем время проверки
        account['last_check'] = datetime.now().isoformat()
        account['check_count'] = account.get('check_count', 0) + 1

        # Получаем последний известный твит и проверяем флаг первой проверки
        last_id = account.get('last_tweet_id')
        first_check = account.get('first_check', False)

        # Проверяем флаг приватности аккаунта
        is_private = account.get('is_private', False)

        logger.info(f"Проверка аккаунта @{username}, последний ID: {last_id}" +
                    (", приватный: да" if is_private else ""))

        # Используем мультиметодную проверку с учетом приватности
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
            total_checks = account.get('check_count', 1)
            fail_count = account.get('fail_count', 0)
            account['success_rate'] = 100 * (total_checks - fail_count) / total_checks
            # Уменьшаем приоритет проблемных аккаунтов
            if account.get('fail_count', 0) > 3:
                account['priority'] = max(0.1, account.get('priority', 1.0) * 0.9)
            return True

        # Сбрасываем счетчик неудач при успехе
        if account.get('fail_count', 0) > 0:
            account['fail_count'] = max(0, account.get('fail_count', 0) - 1)

        # Обновляем процент успеха
        total_checks = account.get('check_count', 1)
        fail_count = account.get('fail_count', 0)
        account['success_rate'] = 100 * (total_checks - fail_count) / total_checks

        # Обновляем метод проверки
        account['check_method'] = method

        # Сравниваем найденный ID с последним известным
        if last_id and not first_check:
            try:
                if int(tweet_id) <= int(last_id):
                    logger.warning(f"⚠️ Аккаунт @{username}: найден более старый твит {tweet_id} " +
                                   f"(текущий {last_id}), игнорируем!")
                    return True
            except (ValueError, TypeError):
                logger.warning(f"Не удалось сравнить ID твитов для @{username}")

        # Если это первая проверка или найден более новый твит
        if first_check or tweet_id != last_id:
            # Обновляем данные твита
            account['check_method'] = method
            if tweet_data:
                account['last_tweet_text'] = tweet_data.get('text', '')
                account['last_tweet_url'] = tweet_data.get('url', '')
                account['tweet_data'] = tweet_data

            if first_check:
                account['first_check'] = False
                account['last_tweet_id'] = tweet_id
                logger.info(f"Аккаунт @{username}: первая проверка, сохранен ID {tweet_id}")
                return True
            else:
                # Нашли новый твит
                account['last_tweet_id'] = tweet_id
                logger.info(f"Аккаунт @{username}: новый твит {tweet_id}, отправляем уведомления")

                # Отправляем уведомления
                if tweet_data:
                    await send_tweet_with_media(app, subs, username, tweet_id, tweet_data)
                return True
        else:
            # ID совпадает, нет новых твитов
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


async def on_startup(app):
    """Вызывается при запуске бота"""
    logger.info("Бот запущен, инициализация...")

    # Инициализируем команды бота
    await app.bot.set_my_commands([
        BotCommand("start", "Начало работы"),
        BotCommand("add", "Добавить аккаунт"),
        BotCommand("remove", "Удалить аккаунт"),
        BotCommand("list", "Список аккаунтов"),
        BotCommand("check", "Проверить аккаунты"),
        BotCommand("settings", "Настройки бота"),
        BotCommand("methods", "Настройка методов скрапинга"),
        BotCommand("update_nitter", "Обновить Nitter-инстансы"),
        BotCommand("auth", "Запустить Safari для авторизации"),
        BotCommand("auth_chrome", "Запустить Chrome для авторизации"),
        BotCommand("stats", "Статистика веб-скрапинга"),
        BotCommand("reset", "Сброс данных аккаунта"),
    ])

    # Инициализируем данные
    init_accounts()

    # Создаем файл прокси, если не существует
    if not os.path.exists(PROXIES_FILE):
        save_json(PROXIES_FILE, {"proxies": []})

    # Создаем файл кеша, если не существует
    if not os.path.exists(CACHE_FILE):
        save_json(CACHE_FILE, {"tweets": {}, "users": {}, "timestamp": int(time.time())})

    # Создаем файл статистики браузеров, если не существует
    if not os.path.exists(BROWSER_STATS_FILE):
        save_json(BROWSER_STATS_FILE, {"browsers": {}, "last_update": int(time.time())})

    # Обновляем список Nitter-инстансов
    try:
        logger.info("Обновление списка Nitter-инстансов...")
        asyncio.create_task(update_nitter_instances())
    except Exception as e:
        logger.error(f"Ошибка при обновлении Nitter-инстансов: {e}")

    # Запускаем фоновую задачу проверки твитов
    global background_task
    background_task = asyncio.create_task(background_check(app))
    logger.info("Фоновая задача активирована")


async def on_shutdown(app):
    """Вызывается при остановке бота"""
    global background_task
    if background_task and not background_task.done() and not background_task.cancelled():
        logger.info("Останавливаем фоновую задачу...")
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ошибка при остановке фоновой задачи: {e}")
        logger.info("Фоновая задача остановлена")


# Глобальная переменная для фоновой задачи
background_task = None


async def background_check(app):
    """Фоновая проверка аккаунтов с улучшенной логикой приоритетов"""
    global background_task
    background_task = asyncio.current_task()

    # При запуске не проверяем сразу, ждем интервал
    settings = get_settings()
    wait_time = settings.get("check_interval", DEFAULT_CHECK_INTERVAL)
    logger.info(f"Фоновая задача запущена, проверка через {wait_time} секунд")
    await asyncio.sleep(wait_time)

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
            methods = settings.get("scraper_methods", ["nitter", "web", "api"])
            parallel_checks = settings.get("parallel_checks", 3)
            randomize = settings.get("randomize_intervals", True)
            accounts_updated = False

            # Проверяем, нужно ли обновить инстансы Nitter
            if "nitter" in methods:
                current_time = int(time.time())
                last_check = settings.get("last_health_check", 0)
                health_check_interval = settings.get("health_check_interval", 1800)  # 30 минут

                if current_time - last_check > health_check_interval:
                    logger.info("Обновление списка Nitter-инстансов...")
                    try:
                        await update_nitter_instances()
                    except Exception as e:
                        logger.error(f"Ошибка при обновлении Nitter-инстансов: {e}")

            # Улучшенная сортировка аккаунтов с учетом приоритета и времени
            now = datetime.now()
            sorted_accounts = []

            for username, account in accounts.items():
                # Пропускаем аккаунты с отключенными методами
                if account.get("scraper_methods") == []:
                    logger.info(f"Пропускаем аккаунт @{username} с пустым списком методов")
                    continue

                # Базовый приоритет
                priority = account.get("priority", 1.0)

                # Увеличиваем приоритет для аккаунтов с высоким процентом неудач
                fail_count = account.get("fail_count", 0)
                if fail_count > 0:
                    priority += min(0.5, fail_count * 0.1)

                # Уменьшаем приоритет для недавно проверенных аккаунтов
                last_check = account.get("last_check", "2000-01-01T00:00:00")
                try:
                    last_check_dt = datetime.fromisoformat(last_check)
                    hours_since_check = (now - last_check_dt).total_seconds() / 3600

                    # Если проверяли менее 1 часа назад, уменьшаем приоритет
                    if hours_since_check < 1:
                        priority -= 0.5 * (1 - hours_since_check)  # От -0 до -0.5
                except Exception:
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
                    account_methods = account.get('scraper_methods', methods)
                    tasks.append(
                        process_account(app, subs, accounts, display_name, account, account_methods, use_proxies))

                # Запускаем все задачи параллельно
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error(f"Ошибка в параллельной проверке: {result}")
                        elif result:  # Если был обновлен аккаунт
                            accounts_updated = True

                # Небольшая задержка между группами
                await asyncio.sleep(2)

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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    chat_id = update.effective_chat.id
    subs = load_json(SUBSCRIBERS_FILE, [])
    if chat_id not in subs:
        subs.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")],
        [InlineKeyboardButton("🔍 Проверить аккаунты", callback_data="check")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

    await update.message.reply_text(
        "👋 Бот мониторинга Twitter!\n\n"
        "Используйте команды:\n"
        "/add <username> - добавить аккаунт\n"
        "/remove <username> - удалить аккаунт\n"
        "/list - список аккаунтов\n"
        "/check - показать последние твиты\n"
        "/settings - настройки\n"
        "/methods <username> <method1,method2> - приоритет проверок\n"
        "/reset <username> - сброс данных аккаунта\n"
        "/stats - статистика браузеров\n"
        "/update_nitter - обновляет список Nitter-инстансы\n"
        "/auth - запустить Safari для авторизации\n"
        "/auth_chrome - запустить Chrome для авторизации\n\n"
        "Бот автоматически проверяет новые твиты и отправляет уведомления.",
        reply_markup=keyboard
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет новый аккаунт для отслеживания"""
    if not context.args:
        return await update.message.reply_text("Использование: /add <username>")

    username = context.args[0].lstrip("@")
    accounts = init_accounts()

    if username.lower() in accounts:
        return await update.message.reply_text(
            f"@{username} уже добавлен.\nИспользуйте /settings для управления аккаунтом.")

    message = await update.message.reply_text(f"Проверяем @{username}...")

    settings = get_settings()
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["nitter", "web", "api"])

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
                                         f"https://twitter.com/{username}/status/{tweet_id}") if tweet_data else f"https://twitter.com/{username}/status/{tweet_id}",
        "tweet_data": tweet_data or {},
        "scraper_methods": None
    }
    save_accounts(accounts)

    # Создаем подробное сообщение с информацией о твите
    if tweet_data:
        tweet_text = tweet_data.get('text', '[Текст недоступен]')
        tweet_url = tweet_data.get('url', f"https://twitter.com/{username}/status/{tweet_id}")
        formatted_date = tweet_data.get('formatted_date', '')

        likes = tweet_data.get('likes', 0)
        retweets = tweet_data.get('retweets', 0)

        result = f"✅ Добавлен @{username}\n\n"

        if formatted_date:
            result += f"📅 Дата: {formatted_date}\n"

        result += f"📝 Последний твит:\n{tweet_text}\n\n"
        result += f"🆔 ID твита: {tweet_id}\n"
        result += f"🔍 Метод проверки: {method}\n"

        if likes or retweets:
            result += f"👍 Лайки: {likes}, 🔄 Ретвиты: {retweets}\n"

        result += f"🔗 {tweet_url}\n\n"
        result += "Бот будет отправлять уведомления о новых твитах."

        # Проверяем наличие медиа для включения превью
        disable_preview = not tweet_data.get('has_media', False) and not tweet_data.get('media')

        await message.edit_text(result, disable_web_page_preview=disable_preview)
    else:
        # Упрощенная версия, если полные данные не доступны
        result = (f"✅ Добавлен @{username}\n\n"
                  f"🆔 ID последнего твита: {tweet_id}\n"
                  f"🔍 Метод проверки: {method}\n"
                  f"🔗 https://twitter.com/{username}/status/{tweet_id}\n\n"
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

    # Очищаем кеш для удаленного аккаунта
    delete_from_cache("tweets", f"web_{username.lower()}")
    delete_from_cache("tweets", f"nitter_{username.lower()}")
    delete_from_cache("tweets", f"api_{username.lower()}")
    delete_from_cache("users", username.lower())

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
    methods = settings.get("scraper_methods", ["nitter", "web", "api"])

    msg = f"⚙️ Настройки:\n• Интервал проверки: {interval_mins} мин.\n• Мониторинг: {status}\n• Методы по умолчанию: {', '.join(methods)}\n\n"
    msg += f"📋 Аккаунты ({len(accounts)}):\n"

    for username, data in sorted(accounts.items(), key=lambda x: x[1].get("priority", 1.0), reverse=True):
        display_name = data.get('username', username)
        last_check = data.get("last_check", "никогда")
        tweet_id = data.get("last_tweet_id", "нет")
        method = data.get("check_method", "unknown")
        success_rate = data.get("success_rate", 100.0)
        tweet_text = data.get("last_tweet_text", "")
        formatted_date = data.get("tweet_data", {}).get("formatted_date", "")

        # Добавляем информацию о методах скрапинга
        scraper_methods = data.get("scraper_methods")
        methods_info = f"общие ({', '.join(settings.get('scraper_methods', ['nitter', 'web', 'api']))})" if scraper_methods is None else ', '.join(scraper_methods)

        # Если методы полностью отключены
        if scraper_methods == []:
            methods_info = "❌ отключен"

        if last_check != "никогда":
            try:
                check_dt = datetime.fromisoformat(last_check)
                last_check = check_dt.strftime("%Y-%m-%d %H:%M")
            except:
                last_check = "недавно"

        account_line = f"• @{display_name}"
        if formatted_date:
            account_line += f" ({formatted_date})"

        account_line += f"\n  ID: {tweet_id}, {success_rate:.0f}%, метод: {method}, проверка: {last_check}"
        account_line += f"\n  🛠 Методы: {methods_info}"
        msg += account_line

        if tweet_text:
            short_text = tweet_text[:50] + "..." if len(tweet_text) > 50 else tweet_text
            msg += f"\n  ➡️ {short_text}"

        msg += "\n\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Проверить аккаунты", callback_data="check")],
        [InlineKeyboardButton("🧹 Очистить кеш", callback_data="clearcache")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=keyboard)
    else:
        await update.message.reply_text(msg, reply_markup=keyboard)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последние найденные твиты без проверки"""
    if hasattr(update, 'callback_query') and update.callback_query:
        message = await update.callback_query.edit_message_text(
            "Загружаем последние найденные твиты..."
        )
    else:
        message = await update.message.reply_text(
            "Загружаем последние найденные твиты..."
        )

    accounts = init_accounts()

    if not accounts:
        return await message.edit_text(
            "Список пуст. Добавьте аккаунты с помощью команды /add <username>"
        )

    results = []

    for username, account in accounts.items():
        display_name = account.get('username', username)
        last_id = account.get('last_tweet_id')
        last_check = account.get('last_check', 'никогда')
        method = account.get('check_method', 'unknown')
        tweet_data = account.get('tweet_data', {})

        if last_check != 'никогда':
            try:
                check_dt = datetime.fromisoformat(last_check)
                last_check = check_dt.strftime("%Y-%m-%d %H:%M")
            except:
                last_check = "недавно"

        if last_id:
            # Формируем подробное представление твита из сохраненных данных
            tweet_text = tweet_data.get('text', account.get('last_tweet_text', '[Текст недоступен]'))
            tweet_url = tweet_data.get('url', account.get('last_tweet_url',
                                                          f"https://twitter.com/{display_name}/status/{last_id}"))
            formatted_date = tweet_data.get('formatted_date', '')
            browser_used = tweet_data.get('browser_used', '')

            tweet_info = f"📱 @{display_name}"

            # Добавляем дату, если она есть
            if formatted_date:
                tweet_info += f" ({formatted_date})"

            tweet_info += f"\n➡️ {tweet_text}"

            # Добавляем метрики, если они есть
            likes = tweet_data.get('likes', 0)
            retweets = tweet_data.get('retweets', 0)

            if likes or retweets:
                tweet_info += f"\n👍 {likes} · 🔄 {retweets}"

            # Добавляем метод и время проверки
            method_info = method
            if browser_used:
                method_info += f" · {browser_used}"

            tweet_info += f"\n🔍 Метод: {method_info}, проверка: {last_check}"

            # Добавляем URL в конце
            tweet_info += f"\n🔗 {tweet_url}"

            results.append(tweet_info)
        else:
            results.append(f"❓ @{display_name}: твиты не найдены")

    result_text = "📊 Последние найденные твиты:\n\n" + "\n\n".join(results)

    if len(result_text) > 4000:
        result_text = result_text[:3997] + "..."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Проверить принудительно", callback_data="check_force")],
        [InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")]
    ])

    await message.edit_text(result_text, reply_markup=keyboard, disable_web_page_preview=True)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает настройки бота"""
    settings = get_settings()

    interval_mins = settings.get("check_interval", DEFAULT_CHECK_INTERVAL) // 60
    enabled = settings.get("enabled", True)
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["nitter", "web", "api"])
    parallel_checks = settings.get("parallel_checks", 3)
    api_request_limit = settings.get("api_request_limit", 20)
    randomize = settings.get("randomize_intervals", True)

    enabled_status = "✅ включен" if enabled else "❌ выключен"
    proxies_status = "✅ включено" if use_proxies else "❌ выключено"
    randomize_status = "✅ включено" if randomize else "❌ выключено"

    proxies = get_proxies()
    proxy_count = len(proxies.get("proxies", []))

    nitter_instances = settings.get("nitter_instances", NITTER_INSTANCES)
    nitter_count = len(nitter_instances)

    msg = (
        "⚙️ **Настройки мониторинга**\n\n"
        f"• Мониторинг: {enabled_status}\n"
        f"• Интервал проверки: {interval_mins} мин.\n"
        f"• Случайные интервалы: {randomize_status}\n"
        f"• Одновременные проверки: {parallel_checks}\n"
        f"• Лимит API запросов: {api_request_limit}\n"
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
        InlineKeyboardButton("Nitter", callback_data="method_priority:nitter"),
        InlineKeyboardButton("Web", callback_data="method_priority:web"),
        InlineKeyboardButton("API", callback_data="method_priority:api")
    ])

    keyboard.append([
        InlineKeyboardButton("⏱ Интервал", callback_data="set_interval"),
        InlineKeyboardButton("🧹 Очистить кеш", callback_data="clearcache"),
        InlineKeyboardButton("🔄 Обновить Nitter", callback_data="update_nitter")
    ])

    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="list")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode="Markdown")


async def cmd_methods(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает методы скрапинга для аккаунта"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    # Получаем аргументы команды
    args = context.args
    if not args or len(args) < 2:
        await message.reply_text(
            "📝 Использование: `/methods username method1,method2`\n\n"
            "Доступные методы: `api`, `web`, `nitter`\n"
            "Пример: `/methods elonmusk nitter,web,api`\n"
            "Для сброса к общим настройкам: `/methods elonmusk reset`\n"
            "Для полного отключения аккаунта: `/methods elonmusk clear`",
            parse_mode="Markdown"
        )
        return

    username = args[0].replace("@", "")
    methods_str = args[1].lower()

    # Загружаем аккаунты
    accounts = init_accounts()

    if username.lower() not in accounts:
        await message.reply_text(f"❌ Аккаунт @{username} не найден.")
        return

    # Если это сброс настроек к общим
    if methods_str == "reset":
        accounts[username.lower()]["scraper_methods"] = None
        save_accounts(accounts)

        # Получаем общие методы для отображения
        settings = get_settings()
        common_methods = settings.get("scraper_methods", ["nitter", "web", "api"])

        await message.reply_text(
            f"✅ Настройки скрапинга для @{username} сброшены до общих.\n"
            f"Будут использоваться методы: {', '.join(common_methods)}"
        )
        return

    # Если это полная очистка методов (отключение аккаунта)
    if methods_str == "clear":
        accounts[username.lower()]["scraper_methods"] = []
        save_accounts(accounts)
        await message.reply_text(f"✅ Методы скрапинга для @{username} полностью очищены. Аккаунт отключен.")
        return

    # Разбираем список методов
    methods = [m.strip() for m in methods_str.split(',')]
    valid_methods = []

    for m in methods:
        if m in ["api", "web", "nitter"]:
            valid_methods.append(m)

    if not valid_methods:
        await message.reply_text("❌ Не указаны допустимые методы (`api`, `web`, `nitter`)")
        return

    # Сохраняем настройки
    accounts[username.lower()]["scraper_methods"] = valid_methods
    save_accounts(accounts)

    await message.reply_text(
        f"✅ Для @{username} установлены методы: {', '.join(valid_methods)}\n"
        f"Порядок определяет приоритет использования."
    )

async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает Safari для авторизации в Twitter"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    await message.reply_text(
        "🔄 Подготовка Safari для авторизации в Twitter...\n\n"
        "⚠️ ВАЖНО: Для работы WebDriver в Safari:\n\n"
        "1. Откройте Terminal и введите команду:\n"
        "   `sudo safaridriver --enable`\n\n"
        "2. Введите пароль администратора\n"
        "3. В Safari включите меню Разработка:\n"
        "   Safari → Настройки → Дополнения → ✓ Показывать меню «Разработка»\n\n"
        "4. В меню «Разработка» включите «Разрешить удаленную автоматизацию»\n\n"
        "После этих действий нажмите кнопку для открытия Twitter"
    )

    # Создаем кнопку для продолжения после настройки
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть Twitter в Safari", callback_data="open_safari")
    ]])

    await message.reply_text("Нажмите кнопку после выполнения настройки:", reply_markup=keyboard)


async def cmd_auth_chrome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает Chrome для авторизации в Twitter"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    await message.reply_text(
        "🔄 Подготовка Chrome для авторизации в Twitter...\n\n"
        "1. Открывается Chrome с новым профилем\n"
        "2. Войдите в свой аккаунт Twitter\n"
        "3. После авторизации НЕ закрывайте браузер\n"
        "4. Вернитесь в бот и нажмите кнопку 'Проверить авторизацию'"
    )

    # Очистим директорию профиля если были проблемы
    try:
        chrome_profile = os.path.join(os.path.expanduser('~'), 'ChromeDriverProfile')
        if os.path.exists(chrome_profile):
            import shutil
            shutil.rmtree(chrome_profile)
            os.makedirs(chrome_profile)
            logger.info(f"Профиль Chrome очищен и пересоздан: {chrome_profile}")
    except Exception as e:
        logger.warning(f"Не удалось очистить профиль Chrome: {e}")

    # Пытаемся запустить Chrome с чистым профилем
    success = launch_chrome_for_scraping()

    if success:
        # Создаем кнопку для проверки авторизации
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Проверить авторизацию", callback_data="check_chrome_auth")
        ]])

        await message.reply_text(
            "✅ Chrome запущен с Twitter.\n"
            "Войдите в аккаунт и нажмите кнопку ниже для проверки авторизации.",
            reply_markup=keyboard
        )
    else:
        await message.reply_text(
            "❌ Не удалось запустить Chrome.\n"
            "Возможные причины:\n"
            "1. Chrome не установлен\n"
            "2. Проблемы с правами доступа\n"
            "3. Конфликт с уже запущенным Chrome\n\n"
            "Попробуйте закрыть все окна Chrome и повторить попытку."
        )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику веб-скрапинга"""
    if not os.path.exists(BROWSER_STATS_FILE):
        return await update.message.reply_text("Статистика браузеров еще не собрана.")

    try:
        with open(BROWSER_STATS_FILE, 'r') as f:
            stats = json.load(f)

        msg = "📊 **Статистика браузеров**\n\n"

        for browser, data in stats["browsers"].items():
            total = data.get("total_attempts", 0)
            successful = data.get("successful_attempts", 0)
            success_rate = (successful / total * 100) if total > 0 else 0
            captchas = data.get("captchas", 0)
            errors = data.get("errors", 0)

            # Форматируем время последнего успеха
            last_success = data.get("last_success")
            if last_success:
                last_success_str = datetime.fromtimestamp(last_success).strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_success_str = "нет данных"

            msg += f"**{browser}**\n"
            msg += f"- Всего попыток: {total}\n"
            msg += f"- Успешных: {successful} ({success_rate:.1f}%)\n"
            msg += f"- Капчи: {captchas}\n"
            msg += f"- Ошибки: {errors}\n"
            msg += f"- Последний успех: {last_success_str}\n\n"

        last_update = stats.get("last_update", 0)
        last_update_str = datetime.fromtimestamp(last_update).strftime("%Y-%m-%d %H:%M:%S")
        msg += f"Последнее обновление: {last_update_str}"

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка при получении статистики браузеров: {e}")
        await update.message.reply_text(f"Ошибка при получении статистики: {str(e)}")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полностью сбрасывает данные аккаунта"""
    if not context.args:
        return await update.message.reply_text("Использование: /reset <username>")

    username = context.args[0].lstrip("@")
    accounts = init_accounts()

    if username.lower() not in accounts:
        return await update.message.reply_text(f"@{username} не найден в списке.")

    message = await update.message.reply_text(f"Сброс данных для аккаунта @{username}...")

    # Полная очистка данных по аккаунту
    clean_account_data(username)

    # Повторная инициализация
    await message.edit_text(
        f"✅ Данные для аккаунта @{username} полностью сброшены.\n"
        "Будет выполнена повторная проверка при следующем обновлении."
    )


async def cmd_update_nitter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновляет список Nitter-инстансов"""
    message = await update.message.reply_text("🔍 Проверка доступных Nitter-инстансов...")

    try:
        instances = await update_nitter_instances()

        if instances:
            await message.edit_text(
                f"✅ Найдено {len(instances)} рабочих Nitter-инстансов:\n\n" +
                "\n".join(f"• {instance}" for instance in instances[:10]) +
                ("\n\n...и ещё больше" if len(instances) > 10 else "")
            )
        else:
            await message.edit_text(
                "❌ Не найдено работающих Nitter-инстансов. Будет использоваться прямой скрапинг Twitter."
            )
    except Exception as e:
        await message.edit_text(f"❌ Ошибка при обновлении: {str(e)}")


async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает кеш для обновления данных"""
    # Если вызвано из меню
    if hasattr(update, 'callback_query') and update.callback_query:
        message = await update.callback_query.edit_message_text("Очистка кеша...")
    else:
        message = await update.message.reply_text("Очистка кеша...")

    accounts = init_accounts()

    if not accounts:
        await message.edit_text("Нет отслеживаемых аккаунтов.")
        return

    # Очищаем кеш для всех аккаунтов
    for username in accounts:
        delete_from_cache("tweets", f"web_{username.lower()}")
        delete_from_cache("tweets", f"nitter_{username.lower()}")
        delete_from_cache("tweets", f"api_{username.lower()}")

    await message.edit_text(
        f"✅ Кеш очищен для {len(accounts)} аккаунтов.\n\n"
        "При следующей проверке будут получены свежие данные."
    )


async def set_interval_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню для установки интервала проверки"""
    settings = get_settings()
    current_mins = settings["check_interval"] // 60

    msg = f"⏱ Текущий интервал проверки: {current_mins} минут\n\nВыберите новый интервал:"

    keyboard = []
    # Добавляем кнопки для различных интервалов
    intervals = [5, 10, 15, 30, 60, 120]
    row = []

    for interval in intervals:
        btn_text = f"{interval} мин" + (" ✓" if current_mins == interval else "")
        row.append(InlineKeyboardButton(btn_text, callback_data=f"interval:{interval}"))
        if len(row) == 3:  # По 3 кнопки в ряд
            keyboard.append(row)
            row = []

    if row:  # Добавляем оставшиеся кнопки
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(msg, reply_markup=reply_markup)


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, interval_str):
    """Устанавливает интервал проверки"""
    try:
        interval = int(interval_str)
        if interval < 1:
            interval = 1
        if interval > 1440:
            interval = 1440

        update_setting("check_interval", interval * 60)
        await cmd_settings(update, context)

    except ValueError:
        await update.callback_query.edit_message_text(
            "⚠️ Ошибка при установке интервала. Пожалуйста, выберите другое значение.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="set_interval")
            ]])
        )


async def update_nitter_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновляет список Nitter-инстансов"""
    message = await update.callback_query.edit_message_text("🔍 Проверка доступных Nitter-инстансов...")

    try:
        instances = await update_nitter_instances()

        if instances:
            # Ограничиваем вывод до 5 инстансов для краткости
            instances_display = instances[:5]
            more_count = len(instances) - len(instances_display)

            text = f"✅ Найдено {len(instances)} рабочих Nitter-инстансов:\n\n" + \
                   "\n".join(f"• {instance}" for instance in instances_display)

            if more_count > 0:
                text += f"\n\n...и ещё {more_count} инстансов."

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="settings")
            ]])

            await message.edit_text(text, reply_markup=keyboard)
        else:
            await message.edit_text(
                "❌ Не найдено работающих Nitter-инстансов. Будет использоваться прямой скрапинг Twitter.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data="settings")
                ]])
            )
    except Exception as e:
        await message.edit_text(
            f"❌ Ошибка при обновлении: {str(e)}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="settings")
            ]])
        )


async def toggle_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает/выключает использование прокси"""
    settings = get_settings()
    current = settings.get("use_proxies", False)
    settings["use_proxies"] = not current
    save_json(SETTINGS_FILE, settings)

    # Переходим обратно в настройки
    await cmd_settings(update, context)


async def toggle_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает/выключает мониторинг"""
    settings = get_settings()
    current = settings.get("enabled", True)
    settings["enabled"] = not current
    save_json(SETTINGS_FILE, settings)

    # Переходим обратно в настройки
    await cmd_settings(update, context)


async def change_method_priority(update: Update, context: ContextTypes.DEFAULT_TYPE, method):
    """Изменяет приоритет методов проверки"""
    settings = get_settings()
    methods = settings.get("scraper_methods", ["nitter", "web", "api"])

    # Перемещаем выбранный метод в начало списка
    if method in methods:
        methods.remove(method)
    methods.insert(0, method)

    # Сохраняем обновленное значение
    settings["scraper_methods"] = methods
    save_json(SETTINGS_FILE, settings)

    # Возвращаемся в настройки
    await cmd_settings(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    if query.data == "list":
        await cmd_list(update, context)
    elif query.data == "check":
        await cmd_check(update, context)
    elif query.data == "check_force":
        await cmd_clearcache(update, context)
        await asyncio.sleep(1)
        await check_all_accounts(update, context)
    elif query.data == "settings":
        await cmd_settings(update, context)
    elif query.data == "toggle_proxies":
        await toggle_proxies(update, context)
    elif query.data == "toggle_monitoring":
        await toggle_monitoring(update, context)
    elif query.data == "clearcache":
        await cmd_clearcache(update, context)
    elif query.data == "set_interval":
        await set_interval_menu(update, context)
    elif query.data == "update_nitter":
        await update_nitter_menu(update, context)
    elif query.data.startswith("interval:"):
        await set_interval(update, context, query.data.split(":", 1)[1])
    elif query.data.startswith("method_priority:"):
        method = query.data.split(":", 1)[1]
        await change_method_priority(update, context, method)
    elif query.data == "open_safari":
        success = launch_safari_for_scraping()
        if success:
            await query.edit_message_text(
                "✅ Safari запущен с Twitter.\n\n"
                "1. Войдите в свой аккаунт Twitter\n"
                "2. После входа НЕ закрывайте браузер\n"
                "3. Теперь вы можете использовать методы скрапинга с помощью `/methods username web`"
            )
        else:
            await query.edit_message_text(
                "❌ Не удалось запустить Safari.\n"
                "Проверьте, что Safari установлен и доступен."
            )


async def check_all_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительно проверяет все аккаунты"""
    if hasattr(update, 'callback_query') and update.callback_query:
        message = await update.callback_query.edit_message_text("Проверяем твиты...")
    else:
        message = await update.message.reply_text("Проверяем твиты...")

    accounts = init_accounts()

    if not accounts:
        return await message.edit_text("Список пуст. Добавьте аккаунты с помощью команды /add <username>")

    settings = get_settings()
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["nitter", "web", "api"])

    results = []
    new_tweets = []
    accounts_updated = False

    # Для каждого аккаунта выполняем проверку
    for username, account in accounts.items():
        display_name = account.get('username', username)
        last_id = account.get('last_tweet_id')
        first_check = account.get('first_check', False)
        account_methods = account.get('scraper_methods', methods)

        # Пропускаем аккаунты с пустым списком методов
        if account_methods == []:
            results.append(f"⏭️ @{display_name}: пропущен (методы отключены)")
            continue

        account['last_check'] = datetime.now().isoformat()
        account['check_count'] = account.get('check_count', 0) + 1
        accounts_updated = True

        try:
            # Очищаем кеш для гарантии получения свежих данных
            delete_from_cache("tweets", f"web_{username.lower()}")
            delete_from_cache("tweets", f"nitter_{username.lower()}")
            delete_from_cache("tweets", f"api_{username.lower()}")

            user_id, tweet_id, tweet_data, method = await check_tweet_multi_method(
                display_name, account_methods, use_proxies
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
                tweet_text = tweet_data.get('text', '[Текст недоступен]')
                tweet_url = tweet_data.get('url', f"https://twitter.com/{display_name}/status/{tweet_id}")
                account['last_tweet_text'] = tweet_text
                account['last_tweet_url'] = tweet_url
                account['tweet_data'] = tweet_data

            if first_check:
                account['first_check'] = False
                account['last_tweet_id'] = tweet_id
                accounts_updated = True

                tweet_text = tweet_data.get('text', '[Текст недоступен]') if tweet_data else '[Текст недоступен]'
                tweet_url = tweet_data.get('url',
                                           f"https://twitter.com/{display_name}/status/{tweet_id}") if tweet_data else f"https://twitter.com/{display_name}/status/{tweet_id}"
                results.append(
                    f"📝 @{display_name}: первая проверка, сохранен ID твита {tweet_id}\n➡️ Текст: {tweet_text[:50]}...")
            elif tweet_id != last_id:
                try:
                    is_newer = int(tweet_id) > int(last_id)
                except (ValueError, TypeError):
                    is_newer = True

                if is_newer:
                    account['last_tweet_id'] = tweet_id
                    accounts_updated = True

                    # Формируем подробное сообщение с данными о твите
                    tweet_text = tweet_data.get('text', '[Текст недоступен]')
                    tweet_url = tweet_data.get('url', f"https://twitter.com/{display_name}/status/{tweet_id}")
                    formatted_date = tweet_data.get('formatted_date', '')

                    tweet_msg = f"🔥 Новый твит от @{display_name}"
                    if formatted_date:
                        tweet_msg += f" ({formatted_date})"

                    tweet_msg += f":\n\n{tweet_text[:50]}..."

                    # Добавляем метрики, если они есть
                    likes = tweet_data.get('likes', 0)
                    retweets = tweet_data.get('retweets', 0)
                    if likes or retweets:
                        tweet_msg += f"\n\n👍 {likes} · 🔄 {retweets}"

                    new_tweets.append((display_name, tweet_id, tweet_data))
                    results.append(f"✅ @{display_name}: новый твит {tweet_id} (метод: {method})")
                else:
                    account['last_tweet_id'] = tweet_id
                    accounts_updated = True
                    results.append(f"🔄 @{display_name}: обновлен ID твита на {tweet_id} (метод: {method})")
            else:
                results.append(f"🔄 @{display_name}: нет новых твитов (метод: {method})")

        except Exception as e:
            logger.error(f"Ошибка при проверке @{display_name}: {e}")
            results.append(f"❌ @{display_name}: ошибка - {str(e)[:50]}")
            account['fail_count'] = account.get('fail_count', 0) + 1

    if accounts_updated:
        save_accounts(accounts)

    if new_tweets:
        await message.edit_text(f"✅ Найдено {len(new_tweets)} новых твитов!")

        # Отправляем уведомления о новых твитах
        subs = [update.effective_chat.id]  # Отправляем только текущему пользователю
        for username, tweet_id, tweet_data in new_tweets:
            await send_tweet_with_media(context.application, subs, username, tweet_id, tweet_data)
    else:
        result_text = "🔍 Новых твитов не найдено.\n\n📊 Результаты проверки:\n" + "\n".join(results)

        if len(result_text) > 4000:
            result_text = result_text[:3997] + "..."

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Проверить снова", callback_data="check_force"),
            InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")
        ]])

        await message.edit_text(result_text, reply_markup=keyboard)


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
            "scraper_methods": ["nitter", "web", "api"],
            "max_retries": 3,
            "cache_expiry": 1800,
            "randomize_intervals": True,
            "min_interval_factor": 0.8,
            "max_interval_factor": 1.2,
            "parallel_checks": 3,
            "api_request_limit": 20,
            "nitter_instances": NITTER_INSTANCES,
            "health_check_interval": 1800,  # 30 минут
            "last_health_check": 0
        })
    ]:
        if not os.path.exists(path):
            save_json(path, default)

    # Проверяем наличие файла статистики браузеров
    if not os.path.exists(BROWSER_STATS_FILE):
        save_json(BROWSER_STATS_FILE, {"browsers": {}, "last_update": int(time.time())})

    app = ApplicationBuilder().token(TG_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()

    # Регистрируем обработчики команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("clearcache", cmd_clearcache))
    app.add_handler(CommandHandler("interval", set_interval_menu))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("methods", cmd_methods))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("auth_chrome", cmd_auth_chrome))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("update_nitter", cmd_update_nitter))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(button_handler))

    settings = get_settings()
    interval_mins = settings["check_interval"] // 60
    logger.info(f"🚀 Бот запущен, интервал проверки: {interval_mins} мин.")
    try:
        app.run_polling(close_loop=False)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()