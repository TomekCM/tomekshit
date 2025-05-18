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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Добавлен ID администратора

# Обновленный список Nitter-инстансов с рабочими серверами
# Обновленный список Nitter-инстансов (проверены на работоспособность)
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
    def __init__(self, use_safari=False):
        if use_safari and platform.system() == "Darwin":  # Проверка на macOS для Safari
            logger.info("Инициализация Safari WebDriver")
            try:
                options = SafariOptions()
                # Safari не поддерживает много опций, доступных в Chrome
                self.driver = webdriver.Safari(options=options)
                logger.info("Safari WebDriver успешно инициализирован")
            except Exception as e:
                logger.error(f"Не удалось инициализировать Safari WebDriver: {e}")
                raise
        else:
            logger.info("Инициализация Chrome WebDriver")
            options = ChromeOptions()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-notifications")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--start-maximized")

            # Используем разные user-agent для обхода блокировок
            user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 110)}.0.{random.randint(1000, 9999)}.{random.randint(10, 99)} Safari/537.36"
            options.add_argument(f"user-agent={user_agent}")

            self.driver = webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install()),
                options=options
            )

        self.driver.implicitly_wait(10)
        logger.info(f"WebDriver инициализирован: {type(self.driver).__name__}")

    def get(self, url, proxies=None, timeout=30):
        try:
            # Добавляем параметр для обхода кеширования
            if '?' not in url:
                url += f"?_cb={int(time.time())}"
            else:
                url += f"&_cb={int(time.time())}"

            logger.info(f"Загружаю страницу: {url}")
            self.driver.get(url)

            # Ждем загрузку контента
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Даем время для загрузки всего динамического контента
            time.sleep(5)

            # Проверяем, нет ли капчи или блокировки
            page_source = self.driver.page_source.lower()
            if "captcha" in page_source or "blocked" in page_source or "rate limit" in page_source:
                logger.warning(f"Обнаружена капча или блокировка на странице {url}")

            return self
        except Exception as e:
            logger.error(f"Ошибка при загрузке страницы {url}: {e}")
            return self

    @property
    def html(self):
        return self.driver

    def close(self):
        try:
            self.driver.quit()
            logger.info("WebDriver закрыт")
        except Exception as e:
            logger.error(f"Ошибка при закрытии WebDriver: {e}")

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


def is_admin(user_id):
    """Проверяет, является ли пользователь администратором бота"""
    settings = get_settings()
    admin_ids = settings.get('admin_ids', [])
    return user_id in admin_ids or user_id == ADMIN_ID


def init_accounts():
    """Загружает и инициализирует данные всех отслеживаемых аккаунтов"""
    accounts = load_json(ACCOUNTS_FILE, {})

    # Обновляем структуру для всех аккаунтов
    for username, account in accounts.items():
        if "priority" not in account:
            account["priority"] = 1.0

        # Добавляем настройки методов скрапинга для каждого аккаунта
        if "scraper_methods" not in account:
            # По умолчанию используем общие настройки
            account["scraper_methods"] = None

    save_json(ACCOUNTS_FILE, accounts)
    return accounts


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при сохранении файла {path}: {e}")


def save_accounts(accounts_data):
    """Сохраняет данные аккаунтов в JSON файл"""
    save_json(ACCOUNTS_FILE, accounts_data)


def get_cache():
    """Загружает кеш из файла с проверкой на устаревшие данные"""
    cache = load_json(CACHE_FILE, {"tweets": {}, "users": {}, "timestamp": int(time.time())})

    # Очистка устаревших данных кеша (старше 6 часов)
    current_time = int(time.time())
    hours_ago = current_time - 21600  # 6 часов

    # Очистка кеша твитов
    tweets_cache = cache.get("tweets", {})
    for username, data in list(tweets_cache.items()):
        if data.get("timestamp", 0) < hours_ago:
            del tweets_cache[username]

    # Очистка кеша пользователей
    users_cache = cache.get("users", {})
    for username, data in list(users_cache.items()):
        if data.get("timestamp", 0) < hours_ago:
            del users_cache[username]

    cache["timestamp"] = current_time
    return cache


def update_cache(category, key, data, force=False):
    """Обновляет кеш с возможностью принудительного обновления"""
    cache = get_cache()

    if category not in cache:
        cache[category] = {}

    # Принудительное удаление старого значения
    if force and key in cache[category]:
        logger.info(f"Принудительное обновление кеша для {category}:{key}")
        del cache[category][key]

    # Добавляем новые данные с текущим временем
    if data is not None:  # Проверка на None, чтобы не сохранять пустые данные
        cache[category][key] = {
            "data": data,
            "timestamp": int(time.time())
        }

    save_json(CACHE_FILE, cache)


def get_from_cache(category, key, max_age=300):  # Максимальное время жизни кеша по умолчанию - 5 минут
    """Получает данные из кеша, если они не устарели"""
    cache = get_cache()

    if category in cache and key in cache[category]:
        item = cache[category][key]
        if int(time.time()) - item.get("timestamp", 0) < max_age:
            return item.get("data")

    return None


def delete_from_cache(category=None, key=None):
    """Удаляет данные из кеша (конкретную запись или весь раздел)"""
    cache = get_cache()

    if category is None:
        # Очищаем весь кеш, но сохраняем структуру
        cache = {"tweets": {}, "users": {}, "timestamp": int(time.time())}
        logger.info("Полная очистка кеша")
    elif key is None and category in cache:
        # Очищаем конкретный раздел кеша
        cache[category] = {}
        logger.info(f"Очищен кеш раздела {category}")
    elif category in cache and key in cache[category]:
        # Удаляем конкретную запись
        del cache[category][key]
        logger.info(f"Удалена запись {key} из кеша {category}")

    save_json(CACHE_FILE, cache)


# Управление настройками
def get_settings():
    settings = load_json(SETTINGS_FILE, {
        "check_interval": DEFAULT_CHECK_INTERVAL,
        "enabled": True,
        "use_proxies": False,
        "scraper_methods": ["nitter", "api", "web"],  # Nitter в первом приоритете
        "max_retries": 3,
        "cache_expiry": 1800,  # 30 минут для кеша
        "randomize_intervals": True,
        "min_interval_factor": 0.8,
        "max_interval_factor": 1.2,
        "parallel_checks": 3,
        "api_request_limit": 20,  # Увеличенный лимит запросов к API
        "nitter_instances": NITTER_INSTANCES,
        "health_check_interval": 3600,  # Интервал проверки здоровья инстансов (1 час)
        "last_health_check": 0  # Время последней проверки инстансов
    })

    # Проверка целостности данных
    if "api_request_limit" not in settings or not isinstance(settings["api_request_limit"], int):
        settings["api_request_limit"] = 20
        save_json(SETTINGS_FILE, settings)
        logger.warning("API лимит был сброшен на значение по умолчанию (20)")

    return settings


def update_setting(key, value):
    """Обновляет настройку и возвращает новые настройки"""
    settings = get_settings()
    settings[key] = value
    if key == "check_interval":  # При изменении интервала заодно оптимизируем методы
        settings["scraper_methods"] = ["nitter", "web", "api"]  # API в последнюю очередь

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


# Полная очистка данных по аккаунту
def clean_account_data(username):
    """Полностью очищает данные по указанному аккаунту"""
    logger.info(f"Очистка всех данных для аккаунта @{username}")

    # Очищаем кеш для всех методов
    delete_from_cache("tweets", f"api_{username.lower()}")
    delete_from_cache("tweets", f"web_{username.lower()}")
    delete_from_cache("tweets", f"nitter_{username.lower()}")
    delete_from_cache("users", username.lower())

    # Очищаем данные из accounts.json
    accounts = init_accounts()
    if username.lower() in accounts:
        # Сохраняем только базовую информацию
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
            "tweet_data": {}
        }
        save_accounts(accounts)

    logger.info(f"Данные для аккаунта @{username} очищены")


def login_to_twitter(driver, username=None, password=None):
    """Выполняет вход в Twitter через WebDriver"""
    if not username:
        username = os.getenv("TWITTER_USERNAME", "")
    if not password:
        password = os.getenv("TWITTER_PASSWORD", "")

    if not username or not password:
        logger.warning("Не удалось найти учетные данные Twitter в переменных окружения")
        return False

    try:
        logger.info("Пытаемся войти в Twitter...")

        # Переходим на страницу логина
        driver.get("https://twitter.com/login")
        time.sleep(5)  # Даем время для загрузки

        # Находим поле ввода имени пользователя
        try:
            username_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "text"))
            )
            username_field.send_keys(username)

            # Нажимаем "Далее"
            next_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Next')]"))
            )
            next_button.click()
            time.sleep(3)

            # Находим поле ввода пароля
            password_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "password"))
            )
            password_field.send_keys(password)

            # Нажимаем "Войти"
            login_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Log in')]"))
            )
            login_button.click()

            # Ждем загрузки главной страницы
            WebDriverWait(driver, 15).until(
                lambda d: "login" not in d.current_url.lower() or "home" in d.current_url
            )

            logger.info("Успешно вошли в Twitter")
            return True
        except Exception as e:
            logger.error(f"Ошибка при вводе данных для входа: {e}")

            # Попробуем альтернативный способ входа (на случай, если интерфейс отличается)
            try:
                # Альтернативная форма входа
                username_field = driver.find_element(By.CSS_SELECTOR, "input[autocomplete='username']")
                username_field.send_keys(username)
                driver.find_element(By.CSS_SELECTOR, "div[role='button']").click()
                time.sleep(3)

                password_field = driver.find_element(By.CSS_SELECTOR, "input[name='password']")
                password_field.send_keys(password)
                driver.find_element(By.CSS_SELECTOR, "div[data-testid='LoginButton']").click()
                time.sleep(5)

                logger.info("Успешно вошли альтернативным способом")
                return True
            except Exception as e2:
                logger.error(f"Альтернативный вход тоже не удался: {e2}")
                return False

    except Exception as e:
        logger.error(f"Ошибка при входе в Twitter: {e}")
        return False


def get_browser_session(use_existing=False, user_data_dir=None, remote_debugging_port=9222):
    """Получает сессию Safari браузера"""
    logger.info("Инициализация Safari WebDriver")

    try:
        # Проверяем, что мы на macOS
        if platform.system() != "Darwin":
            logger.error("Safari WebDriver доступен только на macOS")
            raise RuntimeError("Safari WebDriver доступен только на macOS")

        # Инициализируем Safari WebDriver
        options = SafariOptions()
        driver = webdriver.Safari(options=options)
        logger.info("Safari WebDriver создан")

        # Создаем экземпляр HTMLSession и связываем с драйвером
        session = HTMLSession(use_safari=True)
        session.driver = driver

        # Пытаемся загрузить сохраненные куки
        cookies_file = os.path.join(DATA_DIR, "twitter_cookies.json")
        if os.path.exists(cookies_file):
            try:
                # Сначала переходим на twitter.com, чтобы установить домен для cookies
                driver.get("https://twitter.com")
                time.sleep(2)

                with open(cookies_file, "r") as f:
                    cookies = json.load(f)
                    for cookie in cookies:
                        try:
                            driver.add_cookie(cookie)
                        except Exception as e:
                            logger.debug(f"Не удалось добавить куки: {e}")

                # Перезагружаем страницу с cookie
                driver.get("https://twitter.com/home")
                time.sleep(3)

                if "login" not in driver.current_url.lower():
                    logger.info("Успешно вошли в Twitter с сохраненными куки")
                else:
                    # Если куки не сработали, пробуем форму входа
                    logger.info("Куки устарели, пробуем обычный вход")
                    login_to_twitter(driver)
            except Exception as e:
                logger.error(f"Ошибка при загрузке куки: {e}")
                # Пробуем обычный вход
                login_to_twitter(driver)
        else:
            # Проверяем авторизацию в Twitter
            logger.info("Проверяем авторизацию в Twitter")
            driver.get("https://twitter.com/home")
            time.sleep(10)  # Safari требует больше времени для загрузки

            # Если не авторизованы, пробуем войти
            if "login" in driver.current_url.lower():
                logger.warning("Не залогинены в Twitter, пробуем автоматический вход")
                login_success = login_to_twitter(driver)

                if not login_success:
                    logger.warning("Автоматический вход не удался. Запрашиваем ручной вход.")
                    # Запрашиваем ручной вход
                    driver.get("https://twitter.com/login")
                    print(
                        "\n\nПожалуйста, войдите в аккаунт Twitter в открывшемся окне Safari и нажмите Enter здесь...")
                    input("Нажмите Enter после входа в Twitter...")
            else:
                logger.info("Обнаружена активная сессия Twitter в Safari")

        # После авторизации (автоматической или ручной)
        # Сохраняем куки для будущих сессий
        try:
            cookies = driver.get_cookies()
            with open(cookies_file, "w") as f:
                json.dump(cookies, f)
            logger.info("Куки Twitter сохранены для будущих сессий")
        except Exception as e:
            logger.error(f"Не удалось сохранить куки: {e}")

        # Добавляем скролл для загрузки контента
        logger.info("Выполняем скролл для загрузки контента")
        for i in range(3):
            driver.execute_script("window.scrollBy(0, 1000);")
            time.sleep(2)
            driver.execute_script("window.scrollBy(0, 1000);")
            time.sleep(2)

        # Проверим наличие данных аккаунта
        try:
            username_element = driver.find_element(By.CSS_SELECTOR, '[data-testid="AppTabBar_Profile_Link"]')
            if username_element:
                logger.info("Найден элемент профиля, аутентификация подтверждена")
        except Exception as e:
            logger.warning(f"Не удалось найти элемент профиля: {e}")

        return session
    except Exception as e:
        logger.error(f"Ошибка при инициализации Safari: {e}")
        traceback.print_exc()
        raise

def launch_safari_for_scraping():
    """Настраивает Safari для скрапинга и открывает Twitter"""
    import subprocess
    import platform

    if platform.system() != "Darwin":  # Проверка что это macOS
        logger.error("Safari доступен только на macOS")
        return False

    try:
        # Разрешаем Safari WebDriver
        logger.info("Активация Safari WebDriver...")
        subprocess.run(['safaridriver', '--enable'], check=True)
        logger.info("Safari WebDriver включен")

        # Открываем Twitter в Safari для ручной авторизации
        logger.info("Открываем Twitter в Safari для авторизации...")
        subprocess.Popen(['open', '-a', 'Safari', 'https://twitter.com/login'])
        logger.info("Twitter открыт в Safari для авторизации")

        return True
    except Exception as e:
        logger.error(f"Ошибка при настройке Safari: {e}")
        traceback.print_exc()
        return False


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
            if "tweet_data" not in account:
                account["tweet_data"] = {}
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

        settings = get_settings()
        # Получаем значение из настроек
        api_request_limit = settings.get("api_request_limit", 20)
        logger.info(f"Запрос твитов для user_id={user_id}, лимит API: {api_request_limit}")

        url = f"https://api.twitter.com/2/users/{user_id}/tweets"
        params = {
            "max_results": api_request_limit,  # Используем значение из настроек
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
                # Остальной код остается без изменений
                data = response.json()
                tweets = data.get("data", [])
                includes = data.get("includes", {})

                # Добавляем информацию о медиа в твиты
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

    def get_latest_tweet_web(self, username, use_proxies=False):
        """Веб-скрапинг Twitter с принудительной загрузкой новых твитов"""
        logger.info(f"Запрос твитов для @{username} через веб-скрапинг...")

        try:
            # Определяем, нужно ли использовать авторизованную сессию
            is_private = False
            accounts = init_accounts()
            if username.lower() in accounts:
                is_private = accounts[username.lower()].get("is_private", False)

            # Выбираем, какую сессию использовать
            if is_private:
                logger.info(f"Используем авторизованную сессию для @{username} (приватный аккаунт)")
                try:
                    session = get_browser_session(use_existing=True)
                    logger.info("Успешно подключились к браузерной сессии")
                except Exception as e:
                    logger.error(f"Ошибка подключения к браузеру: {e}, используем обычную сессию")
                    session = HTMLSession()
            else:
                # Для обычных аккаунтов используем стандартную сессию
                session = HTMLSession()

            # Используем ?f=tweets для хронологического отображения
            url = f"https://twitter.com/{username}/with_replies"

            # Добавляем случайное число для обхода кеша
            url = f"{url}?_={int(time.time())}"

            logger.info(f"Загрузка страницы {url}")
            response = session.get(url)

            # Увеличиваем время загрузки
            time.sleep(3)

            # Прокрутка для загрузки всех возможных твитов
            session.driver.execute_script("window.scrollTo(0, 400)")
            time.sleep(1.5)
            session.driver.execute_script("window.scrollTo(0, 800)")
            time.sleep(1.5)
            session.driver.execute_script("window.scrollTo(0, 1200)")
            time.sleep(2)
            session.driver.execute_script("window.scrollTo(0, 1600)")
            time.sleep(2)

            # Вывод текущего времени
            current_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            current_user = os.getlogin()
            logger.info(f"Current Date and Time (UTC): {current_time}")
            logger.info(f"Current User's Login: {current_user}")

            # JavaScript для сбора ВСЕХ твитов на странице
            tweets_data = session.driver.execute_script(r"""
                function extractTweets() {
                    // Список для хранения данных твитов
                    const tweets = [];

                    try {
                        // Получаем ВСЕ твиты на странице
                        const tweetElements = document.querySelectorAll('article[data-testid="tweet"]');
                        console.log(`Найдено ${tweetElements.length} твитов на странице`);

                        for (const article of tweetElements) {
                            try {
                                // Проверка на закрепленный твит
                                const socialContext = article.querySelector('[data-testid="socialContext"]');
                                const isPinned = socialContext && 
                                    (socialContext.textContent.includes('Pinned') || 
                                     socialContext.textContent.includes('Закрепленный') ||
                                     socialContext.textContent.includes('закреплен'));

                                // Получаем ID твита из ссылки на статус
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

                                // Получаем текст твита
                                const textElement = article.querySelector('[data-testid="tweetText"]');
                                const tweetText = textElement ? textElement.innerText : '';

                                // Получаем дату публикации
                                let timestamp = '';
                                let displayDate = '';
                                const timeElement = article.querySelector('time');
                                if (timeElement) {
                                    timestamp = timeElement.getAttribute('datetime');
                                    displayDate = timeElement.innerText;
                                }

                                // Проверяем наличие фото
                                const photoElements = article.querySelectorAll('[data-testid="tweetPhoto"]');
                                const mediaUrls = [];

                                for (const photoEl of photoElements) {
                                    const img = photoEl.querySelector('img');
                                    if (img && img.src) {
                                        let imgUrl = img.src;
                                        // Пытаемся получить фото лучшего качества
                                        imgUrl = imgUrl.replace('&name=small', '&name=large');
                                        imgUrl = imgUrl.replace('&name=thumb', '&name=large');
                                        mediaUrls.push({
                                            type: 'photo',
                                            url: imgUrl
                                        });
                                    }
                                }

                                // Добавляем твит в список
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

            # Если твиты найдены
            if tweets_data and len(tweets_data) > 0:
                # Отображаем информацию о первых твитах
                for i, tweet in enumerate(tweets_data[:3]):
                    logger.info(f"Твит #{i + 1}: ID={tweet.get('id')}, " +
                                f"Время={tweet.get('timestamp', 'нет')}, " +
                                f"Закреплен={tweet.get('isPinned')}")

                # Получаем последний известный ID
                last_known_id = None
                accounts = init_accounts()
                if username.lower() in accounts:
                    last_known_id = accounts[username.lower()].get('last_tweet_id')

                # Сортируем твиты по ID (самые новые в начале)
                try:
                    tweets_data.sort(key=lambda x: int(x.get('id', '0')), reverse=True)
                    logger.info(f"Твиты отсортированы по ID")
                except Exception as e:
                    logger.warning(f"Ошибка сортировки твитов: {e}")

                # КРИТИЧЕСКИ ВАЖНАЯ ПРОВЕРКА: сравниваем найденный ID с последним известным
                if last_known_id:
                    for tweet in tweets_data:
                        try:
                            if int(tweet.get('id')) > int(last_known_id):
                                # Нашли более новый твит!
                                tweet_id = tweet.get('id')
                                tweet_data = {
                                    "text": tweet.get('text', '[Текст недоступен]'),
                                    "url": f"https://twitter.com/{username}/status/{tweet_id}",
                                    "created_at": tweet.get('timestamp', ''),
                                    "formatted_date": tweet.get('displayDate', ''),
                                    "is_pinned": tweet.get('isPinned', False),
                                    "has_media": tweet.get('hasMedia', False),
                                    "media": tweet.get('media', [])
                                }

                                logger.info(f"Найден БОЛЕЕ НОВЫЙ твит: {tweet_id}")
                                return tweet_id, tweet_data
                        except (ValueError, TypeError):
                            pass

                # Если не нашли твит новее известного, просто возвращаем самый новый
                tweet_id = tweets_data[0].get('id')  # Самый новый по нашей сортировке
                tweet_data = {
                    "text": tweets_data[0].get('text', '[Текст недоступен]'),
                    "url": f"https://twitter.com/{username}/status/{tweet_id}",
                    "created_at": tweets_data[0].get('timestamp', ''),
                    "formatted_date": tweets_data[0].get('displayDate', ''),
                    "is_pinned": tweets_data[0].get('isPinned', False),
                    "has_media": tweets_data[0].get('hasMedia', False),
                    "media": tweets_data[0].get('media', [])
                }

                return tweet_id, tweet_data

            logger.warning(f"Не найдены твиты для @{username}")
            return None, None

        except Exception as e:
            logger.error(f"Ошибка при получении твитов через веб: {e}")
            traceback.print_exc()

        return None, None

    def get_latest_tweet(self, username, use_proxies=False):
        """Получает последний твит пользователя через API Twitter"""
        logger.info(f"Запрос твитов для @{username} через API...")

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

        # Обрабатываем полученные данные
        try:
            # Проверяем, что у нас есть данные
            if not isinstance(tweets, list) or len(tweets) == 0:
                logger.warning(f"Получен пустой или неправильный список твитов для @{username}")
                return user_id, None, None

            # Выбираем первый (самый новый) твит
            tweet = tweets[0]
            tweet_id = tweet["id"]
            tweet_text = tweet["text"]
            tweet_created_at = tweet.get("created_at", "")

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
            if "attachments" in tweet and "media_keys" in tweet["attachments"] and "includes" in tweets:
                media_keys = tweet["attachments"]["media_keys"]
                media_data = tweets.get("includes", {}).get("media", [])

                media = []
                for item in media_data:
                    if item["media_key"] in media_keys:
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
                "tweet_id": tweet_id,
                "tweet_data": tweet_data
            })

            logger.info(f"API нашел твит: {tweet_id}")
            return user_id, tweet_id, tweet_data

        except Exception as e:
            logger.error(f"Ошибка при обработке твитов для @{username}: {e}")
            traceback.print_exc()
            return user_id, None, None

    # Добавьте эти методы в класс TwitterClient
    def get_user_id(self, username, use_proxies=False):
        """Получает Twitter ID пользователя по имени аккаунта"""
        logger.info(f"Запрос ID пользователя для @{username}...")

        # Проверяем лимиты API
        if not self.bearer_token or not self.check_rate_limit():
            return None

        # Строим URL запроса
        url = f"https://api.twitter.com/2/users/by/username/{username}"
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": self.user_agent
        }

        try:
            # Используем прокси, если указано
            proxies = get_random_proxy() if use_proxies else None
            response = self.session.get(url, headers=headers, proxies=proxies, timeout=10)

            # Если превышен лимит запросов
            if response.status_code == 429:
                reset_time = int(response.headers.get("x-rate-limit-reset", time.time() + 900))
                self.set_rate_limit(reset_time)
                remaining = int(response.headers.get("x-rate-limit-remaining", 0))
                limit = int(response.headers.get("x-rate-limit-limit", 0))
                logger.warning(
                    f"API лимит запросов: {remaining}/{limit}. Сброс в {reset_time}"
                )
                return None

            # При успешном ответе
            if response.status_code == 200:
                data = response.json()
                if "data" in data and "id" in data["data"]:
                    user_id = data["data"]["id"]
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

    def format_tweet_date(self, date_string):
        """Преобразует ISO дату твита в читабельный формат"""
        if not date_string:
            return "неизвестная дата"

        try:
            dt = datetime.fromisoformat(date_string.replace("Z", "+00:00"))
            # Преобразуем в локальное время (UTC+3)
            local_dt = dt + timedelta(hours=3)
            return local_dt.strftime("%d.%m.%Y %H:%M")
        except:
            return date_string


# Класс для работы с Nitter
class NitterScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml",
            "Cache-Control": "no-cache"
        })
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

        # Если прошло больше часа с последней проверки, обновляем инстансы
        current_time = int(time.time())
        last_check = settings.get("last_health_check", 0)
        health_check_interval = settings.get("health_check_interval", 3600)

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

    def get_latest_tweet_nitter(self, username, use_proxies=False):
        """Получает последний твит через Nitter с проверкой инстансов"""
        logger.info(f"Запрос твитов для @{username} через Nitter...")

        try:
            # Получаем список здоровых инстансов Nitter (из кеша или настроек)
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

            # Остальной код остается тем же...

            newest_tweet_id = None
            newest_tweet_data = None
            newest_timestamp = None

            # Пробуем разные инстансы Nitter
            for nitter in nitter_instances[:3]:  # Используем только первые 3 инстанса
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

                        # Пропускаем закрепленные твиты и ретвиты
                        if is_pinned or is_retweet:
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
                                logger.warning(f"Не удалось распарсить дату: {date_str}")
                                continue

                            tweet_timestamp = tweet_datetime.timestamp()
                        except Exception as e:
                            logger.error(f"Ошибка при разборе даты твита: {date_str}, ошибка: {e}")
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
                                "retweets": retweets
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
                    "updated_at": time.time()  # Добавляем время обновления для контроля устаревания
                }, force=True)  # Принудительно обновляем кеш

                return newest_tweet_id, newest_tweet_data

            logger.warning(f"Не удалось найти твиты для @{username} через все доступные серверы Nitter")

        except Exception as e:
            logger.error(f"Общая ошибка при получении твитов для @{username} через Nitter: {e}")
            traceback.print_exc()

        return None, None


# Класс для веб-скрапинга
class WebScraper:
    def __init__(self):
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:96.0) Gecko/20100101 Firefox/96.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36"
        ]

    def get_random_user_agent(self):
        return random.choice(self.user_agents)

    def validate_tweet_id(self, username, tweet_id):
        if not tweet_id:
            return False
        if len(str(tweet_id)) < 15:
            logger.warning(f"Слишком короткий ID твита для @{username}: {tweet_id}")
            return False
        return True

    def get_latest_tweet_web(self, username, use_proxies=False):
        """Улучшенный веб-скрапинг Twitter для получения твитов"""
        logger.info(f"Запрос твитов для @{username} через веб-скрапинг...")

        try:
            # Используем Selenium для полной загрузки страницы с JavaScript
            with HTMLSession(use_safari=platform.system() == "Darwin") as session:  # Используем Safari на macOS
                # URL для страницы со свежими твитами, добавляем s=20 для принудительной сортировки
                url = f"https://twitter.com/{username}?s=20"

                # Добавляем параметр для обхода кеширования
                url = f"{url}&_={int(time.time())}"

                logger.info(f"Загрузка страницы {url}")
                session.get(url)

                # Функция проверки статуса авторизации
                def check_login_status(driver):
                    try:
                        # Проверяем наличие элементов, видимых только при авторизации
                        driver.find_element(By.CSS_SELECTOR, "a[data-testid='AppTabBar_Profile_Link']")
                        return True
                    except Exception:
                        return False

                # Дополнительные действия для убеждения Twitter в том, что это человек
                try:
                    # Прокрутка страницы для загрузки контента
                    session.driver.execute_script("window.scrollTo(0, 400)")
                    time.sleep(1.5)
                    session.driver.execute_script("window.scrollTo(0, 800)")
                    time.sleep(1.5)
                    session.driver.execute_script("window.scrollTo(0, 1200)")
                    time.sleep(2)

                    # Проверка статуса авторизации
                    is_logged_in = check_login_status(session.driver)
                    logger.info(f"Статус авторизации в Twitter: {'Авторизован' if is_logged_in else 'НЕ авторизован'}")

                    if not is_logged_in:
                        logger.warning("Twitter авторизация отсутствует! Приватные твиты не будут доступны.")
                        # Попробуем авторизоваться (если добавишь эту логику)
                        # login_to_twitter(session.driver, twitter_username, twitter_password)

                    # Попытка нажать на вкладку "Твиты"
                    session.driver.execute_script("""
                        // Проверяем, находимся ли мы на вкладке "Твиты"
                        var tweetsTab = document.querySelector('nav[role="navigation"] a[role="tab"][aria-selected="true"]');
                        if (!tweetsTab || tweetsTab.innerText.indexOf("твит") === -1 && tweetsTab.innerText.indexOf("Tweet") === -1) {
                            // Попытка нажать на вкладку "Твиты"
                            var allTabs = document.querySelectorAll('nav[role="navigation"] a[role="tab"]');
                            for (var i = 0; i < allTabs.length; i++) {
                                if (allTabs[i].innerText.indexOf("твит") !== -1 || allTabs[i].innerText.indexOf("Tweet") !== -1) {
                                    allTabs[i].click();
                                    break;
                                }
                            }
                        }
                    """)
                    time.sleep(1)

                except Exception as e:
                    logger.warning(f"Не удалось выполнить дополнительные действия в браузере: {e}")

                # Проверяем наличие твитов
                tweet_selectors = [
                    "article[data-testid='tweet']",
                    "div[data-testid='tweetText']",
                    "article[role='article']",
                    "div.css-1dbjc4n.r-18u37iz",
                    "div[data-testid='cellInnerDiv']"
                ]

                tweets = []
                for selector in tweet_selectors:
                    logger.info(f"Пробуем найти твиты через селектор: {selector}")
                    try:
                        found_tweets = session.driver.find_elements(By.CSS_SELECTOR, selector)
                        if found_tweets and len(found_tweets) > 0:
                            tweets = found_tweets
                            logger.info(f"Найдено {len(tweets)} твитов с селектором {selector}")
                            break
                    except Exception as e:
                        logger.info(f"Ошибка с селектором {selector}: {e}")

                # Добавь вывод в лог для отладки
                logger.info(f"Найдено {len(tweets)} элементов твитов")

                # Собираем данные о твитах с помощью JavaScript
                tweets_data = session.driver.execute_script(r"""
                    // Функция извлечения твитов с информацией о времени
                    function extractTweets() {
                        const tweets = [];

                        // Ищем все твиты на странице
                        const tweetElements = document.querySelectorAll('article[data-testid="tweet"]');
                        console.log("Найдено твитов:", tweetElements.length);

                        for(const article of tweetElements) {
                            try {
                                // Проверяем, не закрепленный ли это твит
                                const socialContext = article.querySelector('[data-testid="socialContext"]');
                                const isPinned = socialContext && 
                                    (socialContext.textContent.includes('Pinned') || 
                                     socialContext.textContent.includes('Закрепленный') ||
                                     socialContext.textContent.includes('закреплен'));

                                // Получаем ID твита из ссылки
                                let tweetId = null;
                                const links = article.querySelectorAll('a[href*="/status/"]');
                                for(const link of links) {
                                    const match = link.href.match(/\/status\/(\d+)/);
                                    if(match && match[1]) {
                                        tweetId = match[1];
                                        break;
                                    }
                                }

                                // Проверяем наличие ID
                                if(!tweetId) continue;

                                // Получаем текст твита
                                const textElement = article.querySelector('[data-testid="tweetText"]');
                                const tweetText = textElement ? textElement.innerText : '';

                                // Получаем дату
                                let timestamp = '';
                                let displayDate = '';
                                const timeElement = article.querySelector('time');
                                if(timeElement) {
                                    timestamp = timeElement.getAttribute('datetime');
                                    displayDate = timeElement.innerText;
                                }

                                // Получаем метрики твита (лайки, ретвиты)
                                const metrics = {};
                                const likeButton = article.querySelector('[data-testid="like"]');
                                if(likeButton) {
                                    const likeText = likeButton.textContent;
                                    metrics.likes = likeText.match(/\d+/) ? parseInt(likeText.match(/\d+/)[0]) : 0;
                                }

                                const retweetButton = article.querySelector('[data-testid="retweet"]');
                                if(retweetButton) {
                                    const retweetText = retweetButton.textContent;
                                    metrics.retweets = retweetText.match(/\d+/) ? parseInt(retweetText.match(/\d+/)[0]) : 0;
                                }

                                // Проверяем наличие медиа
                                const hasPhotos = article.querySelector('[data-testid="tweetPhoto"]');
                                const hasVideo = article.querySelector('[data-testid="videoPlayer"]');

                                // Собираем информацию о медиафайлах
                                const media = [];
                                if(hasPhotos || hasVideo) {
                                    const mediaElements = article.querySelectorAll('[data-testid="tweetPhoto"] img, [data-testid="videoPlayer"] video');
                                    for(const mediaEl of mediaElements) {
                                        const src = mediaEl.src || mediaEl.currentSrc;
                                        if(src) {
                                            media.push({
                                                type: mediaEl.tagName === 'VIDEO' ? 'video' : 'photo',
                                                url: src
                                            });
                                        }
                                    }
                                }

                                // Добавляем информацию о твите
                                tweets.push({
                                    id: tweetId,
                                    text: tweetText,
                                    timestamp: timestamp,
                                    displayDate: displayDate,
                                    isPinned: !!isPinned,
                                    hasMedia: !!(hasPhotos || hasVideo),
                                    metrics: metrics,
                                    media: media
                                });
                            } catch(e) {
                                console.error("Ошибка при обработке твита:", e);
                            }
                        }

                        return tweets;
                    }

                    // Запускаем функцию извлечения твитов
                    return extractTweets();
                """)

                logger.info(f"Извлечено {len(tweets_data) if tweets_data else 0} твитов для @{username}")

                # Вывод текущего времени для отладки
                current_time = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"Current Date and Time (UTC - YYYY-MM-DD HH:MM:SS formatted): {current_time}")

                # Вывод текущего пользователя для отладки
                try:
                    current_user = os.getlogin()
                    logger.info(f"Current User's Login: {current_user}")
                except:
                    pass

                # Инициализируем переменные заранее!
                tweet_id = None
                tweet_data = None

                # Если твиты найдены, выбираем самый подходящий
                if tweets_data and len(tweets_data) > 0:
                    # Отфильтровываем закрепленные твиты
                    regular_tweets = [t for t in tweets_data if not t.get('isPinned')]

                    # Если есть обычные твиты, берем их, иначе берем закрепленный
                    target_tweets = regular_tweets if regular_tweets else tweets_data

                    # Сортируем по timestamp (если доступно)
                    target_tweets.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

                    # Дополнительная отладка
                    for i, tweet in enumerate(target_tweets[:3]):
                        logger.info(f"Твит #{i + 1}: ID={tweet.get('id')}, Время={tweet.get('timestamp', 'нет')}, " +
                                    f"Закреплен={tweet.get('isPinned')}")

                    if target_tweets:
                        selected_tweet = target_tweets[0]
                        tweet_id = selected_tweet.get('id')

                        # Получаем последний известный ID из аккаунтов
                        last_known_id = None
                        accounts = init_accounts()
                        if username.lower() in accounts:
                            last_known_id = accounts[username.lower()].get('last_tweet_id')

                        # КРИТИЧЕСКАЯ ПРОВЕРКА: сравниваем с последним известным ID
                        if last_known_id and tweet_id:
                            try:
                                is_newer = int(tweet_id) > int(last_known_id)
                                if not is_newer:
                                    logger.warning(f"Веб-скрапинг нашел старый твит ID={tweet_id} для @{username}, " +
                                                  f"(текущий ID={last_known_id}), ищем другой твит")

                                    # Ищем более новый твит
                                    for tweet in target_tweets:
                                        current_id = tweet.get('id')
                                        try:
                                            if int(current_id) > int(last_known_id):
                                                tweet_id = current_id
                                                selected_tweet = tweet
                                                logger.info(f"Найден более новый твит ID={tweet_id} для @{username}")
                                                break
                                        except:
                                            pass
                            except (ValueError, TypeError):
                                logger.warning(f"Не удалось сравнить ID твитов: {tweet_id} vs {last_known_id}")

                        if not self.validate_tweet_id(username, tweet_id):
                            logger.warning(f"Некорректный ID твита: {tweet_id}")
                            return None, None

                        # Формируем данные о твите
                        tweet_data = {
                            "text": selected_tweet.get('text', '[Текст недоступен]'),
                            "url": f"https://twitter.com/{username}/status/{tweet_id}",
                            "created_at": selected_tweet.get('timestamp', ''),
                            "formatted_date": selected_tweet.get('displayDate', 'неизвестная дата'),
                            "is_pinned": selected_tweet.get('isPinned', False),
                            "has_media": selected_tweet.get('hasMedia', False),
                            "likes": selected_tweet.get('metrics', {}).get('likes', 0),
                            "retweets": selected_tweet.get('metrics', {}).get('retweets', 0)
                        }

                        # Добавляем информацию о медиа, если она есть
                        if 'media' in selected_tweet and selected_tweet['media']:
                            tweet_data['media'] = selected_tweet['media']

                        # Обновляем кеш принудительно для получения свежих данных
                        update_cache("tweets", f"web_{username.lower()}", {
                            "tweet_id": tweet_id,
                            "tweet_data": tweet_data
                        }, force=True)

                        logger.info(f"Найден твит ID {tweet_id} для @{username} через веб-скрапинг")
                        return tweet_id, tweet_data

                    logger.warning(f"Твиты найдены, но ни один не подходит для @{username}")
                    return None, None

                logger.warning(f"Не найдены твиты для @{username} через веб-скрапинг")
                return None, None

        except Exception as e:
            logger.error(f"Ошибка при получении твитов для @{username} через веб: {e}")
            traceback.print_exc()

        return None, None


async def send_tweet_with_media(app, subs, username, tweet_id, tweet_data):
    """Отправляет сообщение о твите с фото/видео, если они есть"""
    # Формируем сообщение
    tweet_text = tweet_data.get('text', '[Новый твит]')
    tweet_url = tweet_data.get('url', f"https://twitter.com/{username}/status/{tweet_id}")
    formatted_date = tweet_data.get('formatted_date', '')

    likes = tweet_data.get('likes', 0)
    retweets = tweet_data.get('retweets', 0)

    # Формируем сообщение с метриками
    metrics_text = f"👍 {likes} · 🔄 {retweets}" if likes or retweets else ""

    # Основное сообщение
    tweet_msg = f"🐦 @{username}"

    # Добавляем дату, если она есть
    if formatted_date:
        tweet_msg += f" · {formatted_date}"

    # Добавляем текст
    tweet_msg += f"\n\n{tweet_text}"

    # URL и метрики добавляем в любом случае
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


# Многометодная проверка твитов
async def check_tweet_multi_method(username, account_methods=None, use_proxies=False, max_retries=2):
    """Проверяет твиты всеми доступными методами с учетом индивидуальных настроек аккаунта"""
    # Получаем настройки методов
    settings = get_settings()

    # Если не указаны методы конкретно для этого аккаунта, используем глобальные настройки
    if not account_methods:
        # Загружаем аккаунты и проверяем, есть ли у аккаунта свои настройки методов
        accounts = init_accounts()
        if username.lower() in accounts and accounts[username.lower()].get("scraper_methods"):
            methods = accounts[username.lower()]["scraper_methods"]
            logger.info(f"Используем индивидуальные методы для @{username}: {methods}")
        else:
            # Используем общие настройки
            methods = settings.get("scraper_methods", ["nitter", "api", "web"])
            logger.info(f"Используем общие методы скрапинга: {methods}")
    else:
        methods = account_methods

    twitter_client = TwitterClient(TWITTER_BEARER)
    nitter_scraper = NitterScraper()
    web_scraper = WebScraper()

    results = {
        "api": {"user_id": None, "tweet_id": None, "tweet_data": None},
        "nitter": {"tweet_id": None, "tweet_data": None},
        "web": {"tweet_id": None, "tweet_data": None}
    }

    # Проверяем методы в указанном порядке
    for method in methods:
        try:
            if method == "nitter":
                tweet_id, tweet_data = nitter_scraper.get_latest_tweet_nitter(username, use_proxies)
                if tweet_id:
                    results["nitter"]["tweet_id"] = tweet_id
                    results["nitter"]["tweet_data"] = tweet_data
                    logger.info(f"Nitter нашел твит: {tweet_id}")

            elif method == "api" and TWITTER_BEARER and not twitter_client.rate_limited:
                # Предполагая, что метод get_user_id и get_latest_tweet реализованы
                try:
                    user_id, tweet_id, tweet_data = twitter_client.get_latest_tweet(username, use_proxies)
                    if tweet_id:
                        results["api"]["user_id"] = user_id
                        results["api"]["tweet_id"] = tweet_id
                        results["api"]["tweet_data"] = tweet_data
                        logger.info(f"API нашел твит: {tweet_id}")
                except AttributeError as e:
                    logger.error(f"Не реализован метод API: {e}")

            elif method == "web":
                tweet_id, tweet_data = web_scraper.get_latest_tweet_web(username, use_proxies)
                if tweet_id:
                    results["web"]["tweet_id"] = tweet_id
                    results["web"]["tweet_data"] = tweet_data
                    logger.info(f"Web нашел твит: {tweet_id}")

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
            # Увеличиваем счетчик неудач и т.д.
            account['fail_count'] = account.get('fail_count', 0) + 1
            # ... [остальной код для неудачи]
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

        # КРИТИЧЕСКИ ВАЖНАЯ ПРОВЕРКА: сравниваем найденный ID с последним известным
        if last_id and not first_check:
            try:
                # Только числовое сравнение ID
                if int(tweet_id) <= int(last_id):
                    logger.warning(f"⚠️ Аккаунт @{username}: найден более старый твит {tweet_id} " +
                                  f"(текущий {last_id}), игнорируем!")
                    return True  # Выходим БЕЗ обновления данных
            except (ValueError, TypeError):
                logger.warning(f"Не удалось сравнить ID твитов для @{username}")
                # В случае ошибки сравнения предполагаем, что ID разные

        # Если это первая проверка или найден более новый твит - обновляем данные
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
        # ... [остальной код для ошибок]

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
        BotCommand("check", "Показать последние твиты"),
        BotCommand("interval", "Интервал проверки"),
        BotCommand("settings", "Настройки бота"),
        BotCommand("proxy", "Управление прокси"),
        BotCommand("stats", "Статистика мониторинга"),
        BotCommand("methods", "Настройка методов мониторинга"),
        BotCommand("clearcache", "Очистка кеша"),
        BotCommand("reset", "Полный сброс данных аккаунта"),
        BotCommand("update_nitter", "Обновить Nitter-инстансы"),
        BotCommand("auth", "Запустить Safari для авторизации"),
        BotCommand("privateon", "Пометить аккаунт как приватный"),
        BotCommand("privateoff", "Отключить приватный режим для аккаунта"),
    ])

    # Инициализируем данные
    init_accounts()

    # Создаем файл прокси, если не существует
    if not os.path.exists(PROXIES_FILE):
        save_json(PROXIES_FILE, {"proxies": []})

    # Создаем файл кеша, если не существует
    if not os.path.exists(CACHE_FILE):
        save_json(CACHE_FILE, {"tweets": {}, "users": {}, "timestamp": int(time.time())})

    # Обновляем список Nitter-инстансов (в фоне)
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
    if background_task and not background_task.cancelled():
        logger.info("Останавливаем фоновую задачу...")
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
        logger.info("Фоновая задача остановлена")


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
            methods = settings.get("scraper_methods", ["nitter", "api", "web"])
            parallel_checks = settings.get("parallel_checks", 3)
            randomize = settings.get("randomize_intervals", True)
            accounts_updated = False

            # Проверяем, нужно ли обновить инстансы Nitter
            if "nitter" in methods:
                current_time = int(time.time())
                last_check = settings.get("last_health_check", 0)
                health_check_interval = settings.get("health_check_interval", 3600)

                if current_time - last_check > health_check_interval:
                    logger.info("Обновление списка Nitter-инстансов...")
                    try:
                        await update_nitter_instances()
                    except Exception as e:
                        logger.error(f"Ошибка при обновлении Nitter-инстансов: {e}")

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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    chat_id = update.effective_chat.id
    subs = load_json(SUBSCRIBERS_FILE, [])
    if chat_id not in subs:
        subs.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subs)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список аккаунтов", callback_data="list")],
        [InlineKeyboardButton("🔍 Показать твиты", callback_data="check")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ])

    await update.message.reply_text(
        "👋 Бот мониторинга Twitter!\n\n"
        "Используйте команды:\n"
        "/add <username> - добавить аккаунт\n"
        "/remove <username> - удалить аккаунт\n"
        "/list - список аккаунтов\n"
        "/check - показать последние твиты\n"
        "/interval <минуты> - интервал проверки\n"
        "/settings - настройки\n"
        "/proxy - управление прокси\n"
        "/clearcache - очистка кеша\n"
        "/reset <username> - сброс данных аккаунта",
        reply_markup=keyboard
    )


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
            "Пример: `/methods elonmusk api,web,nitter`\n"
            "Для сброса: `/methods elonmusk reset`"
        )
        return

    username = args[0].replace("@", "")
    methods_str = args[1].lower()

    # Загружаем аккаунты
    accounts = init_accounts()

    if username.lower() not in accounts:
        await message.reply_text(f"❌ Аккаунт @{username} не найден.")
        return

    # Если это сброс настроек
    if methods_str == "reset":
        accounts[username.lower()]["scraper_methods"] = None
        save_json(ACCOUNTS_FILE, accounts)
        await message.reply_text(f"✅ Настройки скрапинга для @{username} сброшены до общих.")
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
    save_json(ACCOUNTS_FILE, accounts)

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
        "🔄 Запускаю Safari для авторизации в Twitter...\n\n"
        "1. Войдите в свой аккаунт Twitter\n"
        "2. После входа НЕ закрывайте браузер\n"
        "3. Бот будет использовать эту сессию для доступа к закрытым аккаунтам\n\n"
        "⚠️ Safari должен оставаться открытым для работы авторизованного скрапинга"
    )

    success = launch_safari_for_scraping()

    if success:
        await message.reply_text(
            "✅ Safari запущен.\n\n"
            "После авторизации используйте команду `/privateon username`, чтобы отметить аккаунт как приватный."
        )
    else:
        await message.reply_text(
            "❌ Не удалось запустить Safari.\n"
            "Проверьте наличие Safari на компьютере."
        )


async def cmd_privateon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмечает аккаунт как приватный, требующий авторизации"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    # Получаем имя аккаунта из аргументов команды
    args = context.args
    if not args:
        await message.reply_text("❌ Укажите имя аккаунта: /privateon username")
        return

    username = args[0].replace("@", "")

    # Загружаем данные аккаунтов
    accounts = init_accounts()

    if username.lower() not in accounts:
        await message.reply_text(f"❌ Аккаунт @{username} не найден.")
        return

    # Отмечаем аккаунт как приватный
    accounts[username.lower()]["is_private"] = True

    # Настраиваем методы скрапинга для приватного аккаунта
    accounts[username.lower()]["scraper_methods"] = ["web", "api"]  # Приоритет на веб с авторизацией

    # Сбрасываем текущие данные для принудительной проверки
    accounts[username.lower()]["first_check"] = True

    # Сохраняем обновленные данные
    save_json(ACCOUNTS_FILE, accounts)

    # Отправляем подтверждение
    await message.reply_text(
        f"✅ Аккаунт @{username} отмечен как приватный.\n"
        f"При следующей проверке будет использована авторизованная сессия Safari."
    )


async def cmd_privateoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отключает приватный режим для аккаунта"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    # Получаем имя аккаунта из аргументов команды
    args = context.args
    if not args:
        await message.reply_text("❌ Укажите имя аккаунта: /privateoff username")
        return

    username = args[0].replace("@", "")

    # Загружаем данные аккаунтов
    accounts = init_accounts()

    if username.lower() not in accounts:
        await message.reply_text(f"❌ Аккаунт @{username} не найден.")
        return

    # Снимаем отметку приватного аккаунта
    accounts[username.lower()]["is_private"] = False

    # Возвращаем стандартные методы скрапинга
    accounts[username.lower()]["scraper_methods"] = None

    # Сбрасываем текущие данные для принудительной проверки
    accounts[username.lower()]["first_check"] = True

    # Сохраняем обновленные данные
    save_json(ACCOUNTS_FILE, accounts)

    # Отправляем подтверждение
    await message.reply_text(
        f"✅ Приватный режим для аккаунта @{username} отключен.\n"
        f"При следующей проверке будут использованы обычные методы скрапинга."
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет новый аккаунт для отслеживания"""
    if not context.args:
        return await update.message.reply_text("Использование: /add <username>")

    username = context.args[0].lstrip("@")
    accounts = init_accounts()

    if username.lower() in accounts:
        return await update.message.reply_text(
            f"@{username} уже добавлен.\nИспользуйте /reset {username} для сброса данных аккаунта.")

    message = await update.message.reply_text(f"Проверяем @{username}...")

    settings = get_settings()
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["nitter", "api", "web"])

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
        "tweet_data": tweet_data or {}
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

    msg = f"⚙️ Настройки:\n• Интервал проверки: {interval_mins} мин.\n• Мониторинг: {status}\n\n"
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
        methods_info = "общие" if scraper_methods is None else ", ".join(scraper_methods)

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
        # Добавляем строку с методами скрапинга
        account_line += f"\n  🛠 Методы: {methods_info}"
        msg += account_line

        if tweet_text:
            short_text = tweet_text[:50] + "..." if len(tweet_text) > 50 else tweet_text
            msg += f"\n  ➡️ {short_text}"

        msg += "\n\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Показать твиты", callback_data="check")],
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
            tweet_info += f"\n🔍 Метод: {method}, проверка: {last_check}"

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


async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает кеш для обновления данных"""
    chat_id = update.effective_chat.id

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
                "\n".join(f"• {instance}" for instance in instances)
            )
        else:
            await message.edit_text(
                "❌ Не найдено работающих Nitter-инстансов. Будет использоваться прямой скрапинг Twitter."
            )
    except Exception as e:
        await message.edit_text(f"❌ Ошибка при обновлении: {str(e)}")


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


async def cmd_set_methods(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает приоритетные методы скрапинга для конкретного аккаунта"""
    message = update.effective_message
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    # Получаем аргументы: имя аккаунта и список методов
    args = context.args
    if len(args) < 2:
        await message.reply_text(
            "❌ Укажите имя аккаунта и методы скрапинга:\n"
            "/methods username api,web,nitter\n\n"
            "Методы будут использованы в указанном порядке. "
            "Для сброса на общие настройки используйте /methods username reset"
        )
        return

    username = args[0].replace("@", "")
    methods_arg = args[1].lower()

    # Загружаем аккаунты
    accounts = init_accounts()

    if username.lower() not in accounts:
        await message.reply_text(f"❌ Аккаунт @{username} не найден.")
        return

    account = accounts[username.lower()]

    # Проверяем, не сброс ли это настроек
    if methods_arg == "reset":
        account["scraper_methods"] = None
        save_json(ACCOUNTS_FILE, accounts)
        await message.reply_text(f"✅ Настройки методов для @{username} сброшены на общие.")
        return

    # Парсим список методов
    methods = [m.strip() for m in methods_arg.split(',')]
    valid_methods = []

    # Проверяем валидность методов
    for method in methods:
        if method in ["api", "web", "nitter"]:
            valid_methods.append(method)

    if not valid_methods:
        await message.reply_text("❌ Не указано ни одного допустимого метода (api, web, nitter).")
        return

    # Сохраняем настройки для аккаунта
    account["scraper_methods"] = valid_methods
    save_json(ACCOUNTS_FILE, accounts)

    await message.reply_text(
        f"✅ Для аккаунта @{username} установлены методы: {', '.join(valid_methods)}\n"
        f"Методы будут использованы в указанном порядке приоритета."
    )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает настройки бота"""
    settings = get_settings()

    interval_mins = settings.get("check_interval", DEFAULT_CHECK_INTERVAL) // 60
    enabled = settings.get("enabled", True)
    use_proxies = settings.get("use_proxies", False)
    methods = settings.get("scraper_methods", ["nitter", "api", "web"])
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
        InlineKeyboardButton("API", callback_data="method_priority:api"),
        InlineKeyboardButton("Web", callback_data="method_priority:web")
    ])

    keyboard.append([
        InlineKeyboardButton("+1 API лимит", callback_data="increase_api_limit_1"),
        InlineKeyboardButton("+5 API лимит", callback_data="increase_api_limit_5"),
        InlineKeyboardButton("-5 API лимит", callback_data="decrease_api_limit")
    ])

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
        await cmd_clearcache(update, context)
        await asyncio.sleep(2)
        await check_all_accounts(update, context)
    elif query.data == "settings":
        await cmd_settings(update, context)
    elif query.data == "toggle_proxies":
        await toggle_proxies(update, context)
    elif query.data == "toggle_monitoring":
        await toggle_monitoring(update, context)
    elif query.data == "clearcache":
        await cmd_clearcache(update, context)
    elif query.data == "increase_api_limit_1":
        await change_api_limit(update, context, increase=1)
    elif query.data == "increase_api_limit_5":
        await change_api_limit(update, context, increase=5)
    elif query.data == "decrease_api_limit":
        await change_api_limit(update, context, increase=-5)
    elif query.data.startswith("method_priority:"):
        method = query.data.split(":", 1)[1]
        await change_method_priority(update, context, method)

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
    methods = settings.get("scraper_methods", ["nitter", "api", "web"])

    results = []
    new_tweets = []
    accounts_updated = False

    # Для каждого аккаунта выполняем проверку
    for username, account in accounts.items():
        display_name = account.get('username', username)
        last_id = account.get('last_tweet_id')
        first_check = account.get('first_check', False)

        account['last_check'] = datetime.now().isoformat()
        account['check_count'] = account.get('check_count', 0) + 1
        accounts_updated = True

        try:
            # Очищаем кеш для гарантии получения свежих данных
            update_cache("tweets", f"web_{username.lower()}", None, force=True)
            update_cache("tweets", f"nitter_{username.lower()}", None, force=True)
            update_cache("tweets", f"api_{username.lower()}", None, force=True)

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
                    f"📝 @{display_name}: первая проверка, сохранен ID твита {tweet_id}\n➡️ Текст: {tweet_text}\n➡️ Ссылка: {tweet_url}")
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

                    tweet_msg += f":\n\n{tweet_text}\n\n🔗 {tweet_url}"

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
            traceback.print_exc()
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


async def change_api_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, increase=1):
    """Изменяет лимит запросов к API"""
    settings = get_settings()
    current = settings.get("api_request_limit", 20)

    # Новый лимит с проверкой на границы
    new_limit = min(100, current + increase) if increase > 0 else max(1, current + increase)

    settings["api_request_limit"] = new_limit
    save_json(SETTINGS_FILE, settings)

    # Выводим информацию о новом лимите в лог для проверки
    logger.info(f"API лимит изменен с {current} на {new_limit}")

    await cmd_settings(update, context)


async def change_method_priority(update: Update, context: ContextTypes.DEFAULT_TYPE, method):
    """Изменяет приоритет методов проверки"""
    settings = get_settings()
    methods = settings.get("scraper_methods", ["nitter", "api", "web"])

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
            "scraper_methods": ["nitter", "api", "web"],  # Nitter в первом приоритете
            "max_retries": 3,
            "cache_expiry": 1800,
            "randomize_intervals": True,
            "min_interval_factor": 0.8,
            "max_interval_factor": 1.2,
            "parallel_checks": 3,
            "api_request_limit": 20,  # Увеличенный лимит API
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
    app.add_handler(CommandHandler("clearcache", cmd_clearcache))
    app.add_handler(CommandHandler("interval", cmd_interval))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("proxy", cmd_proxy))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("update_nitter", cmd_update_nitter))
    app.add_handler(CommandHandler("methods", cmd_methods))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("privateon", cmd_privateon))
    app.add_handler(CommandHandler("privateoff", cmd_privateoff))
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