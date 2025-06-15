import feedparser
import time
import logging
import re
import datetime
import requests
import telebot
import os
import threading
from dotenv import load_dotenv
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, BotCommand

# Загрузка переменных окружения из .env файла
load_dotenv()

# Конфигурация из .env
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
ADMIN_ID = os.getenv('ADMIN_ID')
RSS_URLS = os.getenv('RSS_URLS', '').split(',')

# Параметры с значениями по умолчанию
try:
    POST_DELAY = int(os.getenv('POST_DELAY', '10'))
    CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '300'))
    MAX_HISTORY = int(os.getenv('MAX_HISTORY', '100'))
except ValueError:
    POST_DELAY = 10
    CHECK_INTERVAL = 300
    MAX_HISTORY = 100

# Проверка обязательных переменных
if not BOT_TOKEN or not CHANNEL_ID or not RSS_URLS or not ADMIN_ID:
    raise ValueError("Не заданы обязательные параметры в .env файле!")

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("news_bot.log")
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Глобальные переменные для управления состоянием
is_running = False
bot_thread = None
sent_news = set()
stats = {
    'start_time': None,
    'posts_sent': 0,
    'last_check': None,
    'errors': 0
}

# Загрузка истории отправленных новостей
def load_history():
    global sent_news
    try:
        if os.path.exists("sent_news.txt"):
            with open("sent_news.txt", "r") as f:
                sent_news = set(f.read().splitlines())
            logger.info(f"Загружено {len(sent_news)} отправленных новостей из истории")
            return True
    except Exception as e:
        logger.error(f"Ошибка загрузки истории: {str(e)}")
    return False

# Сохранение истории
def save_history():
    try:
        with open("sent_news.txt", "w") as f:
            f.write("\n".join(sent_news))
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения истории: {str(e)}")
        return False

def send_admin_message(message):
    """Отправка сообщения администратору"""
    try:
        bot.send_message(ADMIN_ID, message, parse_mode="HTML")
        logger.info(f"Уведомление отправлено админу: {message}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу: {str(e)}")
        return False

def clean_html(raw_html):
    """Удаление HTML-тегов из текста"""
    clean = re.compile('<.*?>|&([a-z0-9]+|#[0-9]{1,6}|#x[0-9a-f]{1,6});')
    return re.sub(clean, '', raw_html)

def is_image_url(url):
    """Проверка, является ли URL изображением по расширению"""
    if not url:
        return False
        
    image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
    return any(url.lower().endswith(ext) for ext in image_extensions)

def get_news():
    """Парсинг RSS-лент и фильтрация новостей"""
    all_news = []
    logger.info(f"Проверка {len(RSS_URLS)} источников...")
    stats['last_check'] = datetime.datetime.now()
    
    for url in RSS_URLS:
        if not url.strip():
            continue
            
        try:
            feed = feedparser.parse(url.strip())
            logger.info(f"Получено {len(feed.entries)} новостей из {url}")
            
            for entry in feed.entries:
                # Пропускаем записи без даты публикации
                if not hasattr(entry, 'published_parsed'):
                    continue
                    
                # Фильтр по времени (только свежие новости)
                try:
                    entry_time = datetime.datetime(*entry.published_parsed[:6])
                    if (datetime.datetime.now() - entry_time).days > 1:
                        continue
                except Exception as time_error:
                    logger.warning(f"Ошибка времени: {str(time_error)}")
                    continue
                
                # Форматирование новости
                title = clean_html(entry.title) if hasattr(entry, 'title') else "Без названия"
                link = entry.link if hasattr(entry, 'link') else ""
                summary = clean_html(entry.description)[:200] + "..." if hasattr(entry, 'description') else ""
                
                news_item = {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "image": None,
                    "source": url
                }
                
                # Поиск изображения (если есть)
                if hasattr(entry, 'media_content'):
                    for media in entry.media_content:
                        if media.get('type', '').startswith('image'):
                            news_item["image"] = media['url']
                            break
                
                # Если не нашли в media_content, пробуем найти в enclosure
                if not news_item["image"] and hasattr(entry, 'enclosures'):
                    for enclosure in entry.enclosures:
                        if enclosure.get('type', '').startswith('image'):
                            news_item["image"] = enclosure['href']
                            break
                
                # Проверяем, что это действительно изображение
                if news_item["image"] and not is_image_url(news_item["image"]):
                    news_item["image"] = None
                
                all_news.append(news_item)
        except Exception as e:
            error_msg = f"Ошибка парсинга {url}: {str(e)}"
            logger.error(error_msg)
            stats['errors'] += 1
            send_admin_message(f"⚠️ Ошибка RSS: {url}\n{str(e)[:200]}...")
    
    return all_news

def send_news(news_item):
    """Отправка новости в канал"""
    try:
        # Форматируем сообщение с защитой от слишком длинных текстов
        title = news_item['title'][:200] + "..." if len(news_item['title']) > 200 else news_item['title']
        summary = news_item['summary'][:300] if news_item['summary'] else ""
        
        message = f"<b>{title}</b>\n\n{summary}\n\n🔗 {news_item['link']}"
        
        if news_item["image"]:
            try:
                # Проверяем доступность изображения
                response = requests.head(news_item["image"], timeout=5)
                if response.status_code == 200:
                    bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=news_item["image"],
                        caption=message
                    )
                    stats['posts_sent'] += 1
                    return True
            except Exception as img_error:
                logger.warning(f"Ошибка изображения: {str(img_error)}")
        
        # Если изображение недоступно или отсутствует
        bot.send_message(
            chat_id=CHANNEL_ID,
            text=message,
            disable_web_page_preview=False
        )
        stats['posts_sent'] += 1
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки: {str(e)}")
        stats['errors'] += 1
        send_admin_message(f"❌ Ошибка отправки новости:\n{news_item['title'][:50]}...\nОшибка: {str(e)[:200]}")
        return False

def generate_status_report():
    """Генерация отчёта о состоянии бота"""
    if not stats['start_time']:
        return "❓ Бот в настоящее время остановлен"
    
    uptime = datetime.datetime.now() - stats['start_time']
    hours, remainder = divmod(uptime.total_seconds(), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    last_check = stats['last_check'].strftime("%H:%M:%S") if stats['last_check'] else "никогда"
    
    report = (
        f"🤖 <b>Статус бота</b>\n"
        f"⏱ Время работы: {int(hours)}ч {int(minutes)}м\n"
        f"📊 Отправлено новостей: {stats['posts_sent']}\n"
        f"❌ Ошибки: {stats['errors']}\n"
        f"🔄 Последняя проверка: {last_check}\n"
        f"🔗 Источников: {len(RSS_URLS)}\n"
        f"📝 Состояние: {'работает ▶️' if is_running else 'остановлен 🛑'}"
    )
    return report

def generate_stats_report():
    """Генерация статистического отчёта"""
    if not stats['start_time']:
        return "📊 Статистика недоступна: бот не запущен"
    
    uptime = datetime.datetime.now() - stats['start_time']
    hours = uptime.total_seconds() / 3600
    posts_per_hour = stats['posts_sent'] / hours if hours > 0 else 0
    
    report = (
        f"📈 <b>Статистика бота</b>\n"
        f"⏱ Время работы: {str(uptime).split('.')[0]}\n"
        f"📊 Всего отправлено новостей: {stats['posts_sent']}\n"
        f"📮 Средняя скорость: {posts_per_hour:.1f} новостей/час\n"
        f"❌ Всего ошибок: {stats['errors']}\n"
        f"🔗 Источников: {len(RSS_URLS)}\n"
        f"🆔 Канал: {CHANNEL_ID}\n"
        f"🕒 Последняя активность: {stats['last_check'].strftime('%Y-%m-%d %H:%M') if stats['last_check'] else 'N/A'}"
    )
    return report

def generate_combined_report():
    """Объединенный отчет: статус + статистика"""
    status = generate_status_report()
    stats_report = generate_stats_report()
    return f"{status}\n\n{stats_report}"

def list_sources():
    """Форматированный список источников"""
    sources = "\n".join([f"• {i+1}. {url}" for i, url in enumerate(RSS_URLS)])
    return f"📚 <b>Источники новостей</b> ({len(RSS_URLS)}):\n{sources}"

def bot_worker():
    """Рабочий процесс бота"""
    global sent_news
    
    logger.info("=== Рабочий процесс запущен ===")
    send_admin_message("🚀 Рабочий процесс бота запущен!")
    
    # Загрузка истории
    load_history()
    
    # Основной цикл
    while is_running:
        try:
            # Получаем новости
            news = get_news()
            new_count = sum(1 for item in news if item["link"] and item["link"] not in sent_news)
            logger.info(f"Найдено {len(news)} новостей, новых: {new_count}")
            
            if new_count:
                send_admin_message(f"🔍 Найдено {new_count} новых новостей из {len(news)}")
            
            # Отправляем новые новости
            sent_in_cycle = 0
            for item in news:
                if not is_running:  # Проверка флага остановки
                    break
                    
                if item["link"] and item["link"] not in sent_news:
                    if send_news(item):
                        sent_news.add(item["link"])
                        sent_in_cycle += 1
                        logger.info(f"Отправлено: {item['title'][:30]}...")
                        time.sleep(POST_DELAY)
            
            # Отправляем отчёт об отправке
            if sent_in_cycle:
                send_admin_message(f"📬 Отправлено {sent_in_cycle} новостей в канал")
            
            # Сохраняем историю и очищаем старые записи
            if len(sent_news) > MAX_HISTORY:
                sent_news = set(list(sent_news)[-MAX_HISTORY//2:])
                
            save_history()
            
            # Ожидание следующей проверки
            logger.info(f"Следующая проверка через {CHECK_INTERVAL//60} минут...")
            wait_time = CHECK_INTERVAL
            while wait_time > 0 and is_running:
                time.sleep(1)
                wait_time -= 1
            
        except Exception as e:
            error_msg = f"Критическая ошибка в рабочем процессе: {str(e)}"
            logger.critical(error_msg)
            stats['errors'] += 1
            send_admin_message(f"🔥 КРИТИЧЕСКАЯ ОШИБКА:\n{str(e)[:300]}")
            time.sleep(60)
    
    logger.info("Рабочий процесс остановлен")
    send_admin_message("🛑 Рабочий процесс бота остановлен!")

def start_bot():
    """Запуск бота"""
    global is_running, bot_thread, stats
    
    if is_running:
        return "Бот уже запущен! ✅"
    
    is_running = True
    stats['start_time'] = datetime.datetime.now()
    stats['last_check'] = None
    stats['posts_sent'] = 0
    stats['errors'] = 0
    
    bot_thread = threading.Thread(target=bot_worker)
    bot_thread.daemon = True
    bot_thread.start()
    
    logger.info("Бот запущен")
    return "Бот успешно запущен! 🚀"

def pause_bot():
    """Пауза бота"""
    global is_running
    
    if not is_running:
        return "Бот уже остановлен! ⏸️"
    
    is_running = False
    logger.info("Бот приостановлен")
    return "Бот приостановлен. ⏸️ Новости не будут отправляться до возобновления."

def stop_bot():
    """Полная остановка бота"""
    global is_running
    
    if not is_running:
        return "Бот уже остановлен! 🛑"
    
    is_running = False
    save_history()
    logger.info("Бот остановлен")
    return "Бот полностью остановлен! 🛑"

def restart_bot():
    """Перезапуск бота"""
    stop_bot()
    time.sleep(2)
    return start_bot()

# Создаем клавиатуру с кнопками
def create_reply_keyboard():
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    
    # Первый ряд - управление
    if is_running:
        markup.add(
            KeyboardButton("⏸️ Приостановить"),
            KeyboardButton("🛑 Остановить"),
        )
    else:
        markup.add(
            KeyboardButton("▶️ Запустить"),
            KeyboardButton("🔄 Перезапустить"),
        )
    
    # Второй ряд - информация
    markup.add(
        KeyboardButton("📊 Статистика"),
        KeyboardButton("📝 Источники"),
    )
    
    # Третий ряд - помощь и информация
    markup.add(
        KeyboardButton("❓ Помощь"),
        KeyboardButton("ℹ️ Инфо")
    )
    
    return markup

# Регистрируем команды для бокового меню
bot.set_my_commands([
    BotCommand("start", "Запустить бота"),
    BotCommand("help", "Помощь и команды"),
    BotCommand("status", "Текущий статус"),
    BotCommand("stats", "Статистика работы"),
    BotCommand("start_bot", "Запустить публикацию"),
    BotCommand("pause", "Приостановить"),
    BotCommand("stop", "Остановить бота"),
    BotCommand("restart", "Перезапустить"),
    BotCommand("sources", "Список источников")
])

# Информация о боте (можете редактировать)
INFO_MESSAGE = """
ℹ️ <b>Информация о боте</b>

🤖 <b>Автопостинг новостей в Telegram-канал</b>

Этот бот автоматически публикует новости из RSS-лент в ваш канал. 
Просто настройте источники, и бот будет регулярно проверять их 
на наличие новых материалов.

<b>Основные возможности:</b>
• Автоматический парсинг RSS-лент
• Фильтрация свежих новостей
• Публикация с изображениями
• Удобное управление через кнопки
• Подробная статистика работы

<b>Технические детали:</b>
• Разработано на Python с использованием pyTelegramBotAPI
• Поддержка всех популярных RSS-форматов
• Автономная работа 24/7

<b>Разработчик:</b>
Ваше имя или компания

<b>Контакты:</b>
@ваш_username
email@example.com

<b>Версия:</b> 2.0 (Июнь 2025)
"""

# Краткое описание бота
BOT_DESCRIPTION = """
🤖 <b>Автопостинг новостей в Telegram-канал</b>

Этот бот автоматически публикует новости из RSS-лент в ваш канал. 
Просто настройте источники, и бот будет регулярно проверять их 
на наличие новых материалов.

<b>Основные функции:</b>
• Автоматическая публикация новостей
• Поддержка изображений
• Фильтрация свежих новостей
• Удобное управление
"""

# Список всех команд
COMMANDS_LIST = """
<b>Доступные команды:</b>

/start - Запустить бота и показать это сообщение
/help - Показать список команд
/status - Текущий статус бота
/stats - Статистика работы
/start_bot - Запустить публикацию новостей
/pause - Приостановить публикацию
/stop - Полностью остановить бота
/restart - Перезапустить бота
/sources - Список источников новостей

<b>Используйте кнопки внизу для быстрого доступа к командам 👇</b>
"""

# Приветственное сообщение для новых чатов
WELCOME_MESSAGE = f"""
{BOT_DESCRIPTION}

Чтобы начать работу, используйте команду /start или выберите действие из меню.
"""

# Обработчики команд
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logger.info(f"Получена команда /start от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        logger.warning(f"Попытка доступа неавторизованного пользователя: {message.from_user.id}")
        return
        
    logger.info("Отправка приветственного сообщения с командами")
    bot.reply_to(message, 
        f"{BOT_DESCRIPTION}\n\n{COMMANDS_LIST}",
        parse_mode="HTML",
        reply_markup=create_reply_keyboard()
    )

@bot.message_handler(commands=['status'])
def send_status(message):
    logger.info(f"Получена команда /status от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    bot.reply_to(message, generate_status_report(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['stats'])
def send_stats(message):
    logger.info(f"Получена команда /stats от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    bot.reply_to(message, generate_combined_report(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['start_bot'])
def start_command(message):
    logger.info(f"Получена команда /start_bot от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    result = start_bot()
    bot.reply_to(message, result, 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['pause'])
def pause_command(message):
    logger.info(f"Получена команда /pause от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    result = pause_bot()
    bot.reply_to(message, result, 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['stop'])
def stop_command(message):
    logger.info(f"Получена команда /stop от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    result = stop_bot()
    bot.reply_to(message, result, 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['restart'])
def restart_command(message):
    logger.info(f"Получена команда /restart от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    result = restart_bot()
    bot.reply_to(message, result, 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['sources'])
def sources_command(message):
    logger.info(f"Получена команда /sources от {message.from_user.id}")
    if str(message.from_user.id) != ADMIN_ID:
        return
        
    bot.reply_to(message, list_sources(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

# Обработка текстовых сообщений (кнопок)
@bot.message_handler(func=lambda message: True)
def handle_text_messages(message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    logger.info(f"Получено текстовое сообщение: '{text}' от {user_id}")
    
    if str(user_id) != ADMIN_ID:
        bot.reply_to(message, "⛔ Доступ запрещен!", reply_markup=create_reply_keyboard())
        return
    
    # Обработка кнопок
    if text == "▶️ Запустить":
        start_command(message)
    elif text == "⏸️ Приостановить":
        pause_command(message)
    elif text == "🛑 Остановить":
        stop_command(message)
    elif text == "🔄 Перезапустить":
        restart_command(message)
    elif text == "📊 Статистика":
        report = generate_combined_report()
        bot.reply_to(message, report, 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())
    elif text == "📝 Источники":
        sources_command(message)
    elif text == "❓ Помощь":
        send_welcome(message)
    elif text == "ℹ️ Инфо":
        bot.reply_to(message, INFO_MESSAGE, 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())
    else:
        bot.reply_to(message, "⚠️ Неизвестная команда. Используйте /help для списка команд",
                    reply_markup=create_reply_keyboard())

# Обработчик для пустых чатов
@bot.message_handler(content_types=['text'], func=lambda message: message.text not in ['/start', '/help'])
def handle_empty_chat(message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    
    # Если это первое сообщение в чате
    if message.text and message.text.startswith('/') is False:
        logger.info(f"Отправка приветственного сообщения для нового чата")
        bot.reply_to(message, WELCOME_MESSAGE, 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())

# Запуск бота
if __name__ == "__main__":
    # Загружаем историю
    load_history()
    
    # Отправляем уведомление о запуске
    logger.info("Запуск бота...")
    send_admin_message("🤖 Бот управления запущен! Используйте /help для списка команд")
    
    # Запускаем обработку сообщений
    logger.info("Бот запущен и ожидает команд...")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.critical(f"Критическая ошибка в основном потоке: {str(e)}")
        send_admin_message(f"💥 КРИТИЧЕСКАЯ ОШИБКА: {str(e)[:300]}")
