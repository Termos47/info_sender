import os
import re
import time
import threading
import feedparser
from dotenv import load_dotenv
import telebot
import logging
from datetime import datetime
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, BotCommand
import requests
import json
from typing import Any, Union, List, Optional, Type, Callable, Tuple, Dict
from PIL import Image, ImageDraw, ImageFont
import textwrap
import random
import traceback
import math

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("rss_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('RSSBot')

# Загрузка переменных окружения
load_dotenv()

# Безопасное чтение переменных окружения с проверкой типов
def get_env_var(
    name: str, 
    default: Any = None, 
    required: bool = False, 
    var_type: Union[Type, Callable] = str
) -> Any:
    value = os.getenv(name, default)
    if required and value is None:
        logger.critical(f"Environment variable {name} is required but not set")
        exit(1)
    
    if value is None:
        return default
        
    try:
        if var_type == int:
            return int(value)
        elif var_type == list:
            value = value.strip("[]")
            return [url.strip().strip("'\"") for url in value.split(',') if url.strip()]
        elif var_type == bool:
            return value.lower() in ['true', '1', 'yes', 'y']
        elif var_type == tuple:
            return tuple(map(int, value.split(',')))
        elif var_type == float:
            return float(value)
        return value
    except (TypeError, ValueError) as e:
        logger.error(f"Error converting {name} to {var_type.__name__}: {str(e)}")
        return default

# Чтение конфигурации
TOKEN: str = get_env_var('TELEGRAM_TOKEN', required=True)
CHANNEL_ID: str = get_env_var('CHANNEL_ID', required=True)
OWNER_ID: int = get_env_var('OWNER_ID', required=True, var_type=int)
RSS_URLS: List[str] = get_env_var(
    'RSS_URLS', 
    default="https://www.interfax.ru/rss.asp", 
    var_type=list
)
CHECK_INTERVAL: int = get_env_var('CHECK_INTERVAL', default=300, var_type=int)

# YandexGPT settings
YANDEX_API_KEY: Optional[str] = get_env_var('YANDEX_API_KEY')
YANDEX_FOLDER_ID: str = get_env_var('YANDEX_FOLDER_ID', default='')
DISABLE_YAGPT: bool = get_env_var('DISABLE_YAGPT', default=False, var_type=bool)

# Настройки для генерации изображений
FONTS_DIR: str = get_env_var('FONTS_DIR', default='fonts')
TEMPLATES_DIR: str = get_env_var('TEMPLATES_DIR', default='templates')
OUTPUT_DIR: str = get_env_var('OUTPUT_DIR', default='temp_images')
DEFAULT_FONT: str = get_env_var('DEFAULT_FONT', default='Montserrat-Bold.ttf')

# Расширенные настройки генерации изображений
TEXT_COLOR: Tuple[int, int, int] = get_env_var('TEXT_COLOR', default='255,255,255', var_type=tuple)
STROKE_COLOR: Tuple[int, int, int] = get_env_var('STROKE_COLOR', default='0,0,0', var_type=tuple)
STROKE_WIDTH: int = get_env_var('STROKE_WIDTH', default=2, var_type=int)
MAX_LINES: int = get_env_var('MAX_LINES', default=3, var_type=int)
TEXT_AREA_WIDTH: float = get_env_var('TEXT_AREA_WIDTH', default=0.8, var_type=float)
TEXT_POSITION_X: str = get_env_var('TEXT_POSITION_X', default='center')
TEXT_POSITION_Y: str = get_env_var('TEXT_POSITION_Y', default='center')
TEXT_OFFSET_X: int = get_env_var('TEXT_OFFSET_X', default=0, var_type=int)
TEXT_OFFSET_Y: int = get_env_var('TEXT_OFFSET_Y', default=0, var_type=int)
FONT_SIZE_RATIO: float = get_env_var('FONT_SIZE_RATIO', default=0.08, var_type=float)
LINE_HEIGHT_RATIO: float = get_env_var('LINE_HEIGHT_RATIO', default=1.2, var_type=float)
DEBUG_GRID: bool = get_env_var('DEBUG_GRID', default=False, var_type=bool)
BACKGROUND_COLOR: Tuple[int, int, int] = get_env_var('BACKGROUND_COLOR', default='40,40,40', var_type=tuple)

bot = telebot.TeleBot(TOKEN)
sent_entries = set()

# Статистика работы бота
stats = {
    'start_time': None,
    'posts_sent': 0,
    'last_check': None,
    'errors': 0,
    'last_post': None,
    'yagpt_used': 0,
    'yagpt_errors': 0,
    'images_generated': 0
}

# Класс для генерации изображений с заголовками
class ImageGenerator:
    def __init__(self, templates_dir: str, fonts_dir: str, output_dir: str):
        self.templates_dir = templates_dir
        self.fonts_dir = fonts_dir
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.templates_dir, exist_ok=True)
        os.makedirs(self.fonts_dir, exist_ok=True)
        
        # Загрузка конфигурации шаблонов
        self.templates_config = self.load_templates_config()
        
    def load_templates_config(self) -> Dict[str, Dict]:
        """Загружает конфигурацию шаблонов из JSON файла"""
        config_path = os.path.join(self.templates_dir, 'templates_config.json')
        if not os.path.exists(config_path):
            return {}
            
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading templates config: {str(e)}")
            return {}
        
    def generate_image(self, title: str) -> Optional[str]:
        """Генерирует изображение с заголовком новости"""
        try:
            # Получаем список доступных шаблонов
            templates = [f for f in os.listdir(self.templates_dir) 
                        if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
            
            template_config = None
            template_file = None
            
            if templates:
                # Выбираем случайный шаблон
                template_file = random.choice(templates)
                template_path = os.path.join(self.templates_dir, template_file)
                img = Image.open(template_path).convert('RGB')
                
                # Проверяем, есть ли конфигурация для этого шаблона
                if template_file in self.templates_config:
                    template_config = self.templates_config[template_file]
                    logger.info(f"Using template config for: {template_file}")
            else:
                logger.warning("No templates found. Using default background")
                # Создаем простой фон, если шаблонов нет
                img = Image.new('RGB', (1200, 630), color=BACKGROUND_COLOR)
            
            # Создаем объект для рисования
            draw = ImageDraw.Draw(img)
            
            # Функция для конвертации цвета в кортеж целых чисел
            def convert_color(color_val) -> Tuple[int, int, int]:
                """Конвертирует цвет в кортеж целых чисел (r, g, b)"""
                # Обработка кортежей и списков
                if isinstance(color_val, (tuple, list)) and len(color_val) == 3:
                    return (int(color_val[0]), int(color_val[1]), int(color_val[2]))
                
                # Обработка строк
                if isinstance(color_val, str):
                    try:
                        parts = color_val.split(',')
                        if len(parts) == 3:
                            return (int(parts[0]), int(parts[1]), int(parts[2]))
                    except:
                        pass
                
                # Возвращаем белый цвет по умолчанию
                return (255, 255, 255)
            
            # Применяем конфигурацию шаблона или глобальные настройки
            text_color = template_config.get('text_color', TEXT_COLOR) if template_config else TEXT_COLOR
            stroke_color = template_config.get('stroke_color', STROKE_COLOR) if template_config else STROKE_COLOR
            
            # Конвертируем цвета в правильный формат
            text_color_converted = convert_color(text_color)
            stroke_color_converted = convert_color(stroke_color)
            
            stroke_width = template_config.get('stroke_width', STROKE_WIDTH) if template_config else STROKE_WIDTH
            max_lines = template_config.get('max_lines', MAX_LINES) if template_config else MAX_LINES
            text_area_width = template_config.get('text_area_width', TEXT_AREA_WIDTH) if template_config else TEXT_AREA_WIDTH
            text_position_x = template_config.get('text_position_x', TEXT_POSITION_X) if template_config else TEXT_POSITION_X
            text_position_y = template_config.get('text_position_y', TEXT_POSITION_Y) if template_config else TEXT_POSITION_Y
            text_offset_x = template_config.get('text_offset_x', TEXT_OFFSET_X) if template_config else TEXT_OFFSET_X
            text_offset_y = template_config.get('text_offset_y', TEXT_OFFSET_Y) if template_config else TEXT_OFFSET_Y
            font_size_ratio = template_config.get('font_size_ratio', FONT_SIZE_RATIO) if template_config else FONT_SIZE_RATIO
            line_height_ratio = template_config.get('line_height_ratio', LINE_HEIGHT_RATIO) if template_config else LINE_HEIGHT_RATIO
            
            # Загружаем шрифт
            font_name = template_config.get('font', DEFAULT_FONT) if template_config else DEFAULT_FONT
            font_path = os.path.join(self.fonts_dir, font_name)
            
            # Рассчитываем размер шрифта
            base_font_size = max(10, int(img.height * font_size_ratio))
            
            try:
                font = ImageFont.truetype(font_path, base_font_size)
            except IOError:
                logger.warning(f"Font {font_name} not found. Using default font")
                font = ImageFont.load_default()
                base_font_size = 20
            
            # Настройки текста
            max_width = int(img.width * text_area_width)  # Ширина текстовой области
            
            # Разбиваем заголовок на строки
            wrapper = textwrap.TextWrapper(
                width=int(max_width / (base_font_size * 0.6)),  # Эмпирическая формула
                break_long_words=True,
                replace_whitespace=False
            )
            
            # Пытаемся разбить на строки с учетом максимального количества
            lines = []
            words = title.split()
            current_line = ""
            
            for word in words:
                test_line = current_line + word + " "
                # Оцениваем ширину текста через bbox
                text_bbox = draw.textbbox((0, 0), test_line, font=font)
                text_width = int(text_bbox[2] - text_bbox[0])
                if text_width <= max_width:
                    current_line = test_line
                else:
                    if current_line:
                        lines.append(current_line.strip())
                    current_line = word + " "
            
            if current_line:
                lines.append(current_line.strip())
            
            # Ограничиваем количество строк
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                if len(lines[-1]) > 15:
                    lines[-1] = lines[-1][:-3] + "..."
            
            # Рассчитываем высоту строки
            test_bbox = draw.textbbox((0, 0), "Test", font=font)
            line_height = int((test_bbox[3] - test_bbox[1]) * line_height_ratio)
            total_height = len(lines) * line_height
            
            # Позиционирование текста
            y_position = self.calculate_y_position(
                img.height, total_height, text_position_y, text_offset_y
            )
            
            # Рисуем каждую строку текста
            for line in lines:
                # Рассчитываем ширину текста для выравнивания
                text_bbox = draw.textbbox((0, 0), line, font=font)
                text_width = int(text_bbox[2] - text_bbox[0])
                
                x_position = self.calculate_x_position(
                    img.width, text_width, text_position_x, text_offset_x
                )
                
                # Рисуем текст с контуром для лучшей читаемости
                draw.text(
                    (x_position, y_position),
                    line,
                    font=font,
                    fill=text_color_converted,
                    stroke_fill=stroke_color_converted,
                    stroke_width=stroke_width
                )
                y_position += line_height
            
            # Отладочная сетка
            if DEBUG_GRID:
                self.draw_debug_grid(draw, img.width, img.height)
            
            # Сохраняем изображение
            output_path = os.path.join(self.output_dir, f"post_{int(time.time())}.jpg")
            img.save(output_path)
            
            stats['images_generated'] += 1
            return output_path
        
        except Exception as e:
            logger.error(f"Image generation failed: {str(e)}")
            logger.error(traceback.format_exc())
            return None
            
    def calculate_x_position(self, 
                            image_width: int, 
                            text_width: int, 
                            position: str, 
                            offset: int) -> int:
        """Рассчитывает позицию текста по горизонтали"""
        if position == 'left':
            return offset
        elif position == 'right':
            return image_width - text_width + offset
        else:  # center
            return (image_width - text_width) // 2 + offset
            
    def calculate_y_position(self, 
                            image_height: int, 
                            total_height: int, 
                            position: str, 
                            offset: int) -> int:
        """Рассчитывает позицию текста по вертикали"""
        if position == 'top':
            return offset
        elif position == 'bottom':
            return image_height - total_height + offset
        else:  # center
            return (image_height - total_height) // 2 + offset
            
    def draw_debug_grid(self, draw, width: int, height: int) -> None:
        """Рисует отладочную сетку на изображении"""
        # Вертикальные линии
        for x in range(0, width, 50):
            draw.line([(x, 0), (x, height)], fill=(255, 0, 0, 128), width=1)
        
        # Горизонтальные линии
        for y in range(0, height, 50):
            draw.line([(0, y), (width, y)], fill=(0, 0, 255, 128), width=1)
        
        # Центральные оси
        draw.line([(width//2, 0), (width//2, height)], fill=(0, 255, 0, 200), width=2)
        draw.line([(0, height//2), (width, height//2)], fill=(0, 255, 0, 200), width=2)

# Инициализация генератора изображений
image_generator = ImageGenerator(
    templates_dir=TEMPLATES_DIR,
    fonts_dir=FONTS_DIR,
    output_dir=OUTPUT_DIR
)

def enhance_with_yagpt(title: str, description: str) -> Optional[dict]:
    """Улучшает текст поста с помощью YandexGPT через REST API"""
    if DISABLE_YAGPT or not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return None

    # Ограничение длины входных данных
    MAX_INPUT_LENGTH = 3000
    if len(description) > MAX_INPUT_LENGTH:
        description = description[:MAX_INPUT_LENGTH] + "..."

    prompt = f"""
Ты — профессиональный редактор новостей. Перепиши заголовок и описание новостного поста для Telegram-канала, чтобы они были:
1. Более привлекательными и цепляющими
2. Легко читаемыми
3. Сохраняли суть оригинала
4. Оптимизированными под соцсети (используй эмодзи, абзацы)
5. Добавь релевантные эмодзи в заголовок
6. Сделай текст более живым и интересным
7. Убери лишние детали, оставив суть
8. Максимальная длина заголовка: 100 символов
9. Максимальная длина описания: 400 символов

Ответ в формате JSON: {{"title": "новый заголовок", "description": "новое описание"}}

Оригинальный заголовок: {title}
Оригинальное описание: {description}
    """

    try:
        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {YANDEX_API_KEY}"
        }
        
        data = {
            "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt-lite",
            "completionOptions": {
                "temperature": 0.4,
                "maxTokens": 1500
            },
            "messages": [
                {
                    "role": "user",
                    "text": prompt
                }
            ]
        }

        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()

        # Извлечение текста ответа
        result_text = result['result']['alternatives'][0]['message']['text']

        try:
            # Извлекаем JSON из текста ответа
            json_start = result_text.find('{')
            json_end = result_text.rfind('}') + 1
            if json_start == -1 or json_end == 0:
                logger.error(f"YandexGPT response format error: {result_text}")
                return None
                
            json_str = result_text[json_start:json_end]
            data = json.loads(json_str)
            return {
                'title': data.get('title', title),
                'description': data.get('description', description)
            }
        except json.JSONDecodeError as e:
            logger.error(f"YandexGPT JSON error: {e}\nResponse: {result_text}")
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"YandexGPT request error: {str(e)}")
        stats['yagpt_errors'] += 1
    except Exception as e:
        logger.error(f"YandexGPT processing error: {str(e)}")
        stats['yagpt_errors'] += 1
    
    return None

# Надёжный механизм управления потоком
class BotController:
    def __init__(self):
        self.is_running = False
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.last_check = datetime.now()
        
    def start(self) -> bool:
        if self.is_running:
            return False
            
        self.is_running = True
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self.rss_loop, daemon=True)
        self.worker_thread.start()
        
        # Запись статистики
        stats['start_time'] = datetime.now()
        stats['last_check'] = None
        stats['posts_sent'] = 0
        stats['errors'] = 0
        stats['yagpt_used'] = 0
        stats['yagpt_errors'] = 0
        stats['images_generated'] = 0
        
        return True
        
    def stop(self) -> bool:
        if not self.is_running:
            return False
            
        self.is_running = False
        self.stop_event.set()
        
        # Ожидаем завершение потока (максимум 5 секунд)
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)
            
        return True
        
    def status(self) -> bool:
        return self.is_running
        
    def rss_loop(self) -> None:
        logger.info("===== RSS LOOP STARTED =====")
        while self.is_running and not self.stop_event.is_set():
            try:
                self.last_check = datetime.now()
                stats['last_check'] = self.last_check
                logger.info(f"Checking {len(RSS_URLS)} RSS feeds")
                
                for url in RSS_URLS:
                    if self.stop_event.is_set():
                        break
                        
                    try:
                        feed = feedparser.parse(url)
                        if not feed.entries:
                            logger.warning(f"Empty feed: {url}")
                            continue
                            
                        # Обработка новых записей
                        for entry in reversed(feed.entries[:10]):
                            if self.stop_event.is_set():
                                break
                                
                            if not hasattr(entry, 'link') or entry.link in sent_entries:
                                continue
                                
                            try:
                                message, image_path = self.format_message(entry)
                                
                                # Отправка с изображением, если доступно
                                if image_path and os.path.exists(image_path):
                                    try:
                                        with open(image_path, 'rb') as photo:
                                            bot.send_photo(
                                                chat_id=CHANNEL_ID,
                                                photo=photo,
                                                caption=message,
                                                parse_mode='HTML'
                                            )
                                        # Удаляем временный файл после отправки
                                        os.remove(image_path)
                                        logger.info(f"Image sent and removed: {image_path}")
                                    except Exception as e:
                                        logger.error(f"Error sending photo: {str(e)}")
                                        # Пробуем отправить без изображения
                                        bot.send_message(
                                            chat_id=CHANNEL_ID,
                                            text=message,
                                            parse_mode='HTML'
                                        )
                                else:
                                    bot.send_message(
                                        chat_id=CHANNEL_ID,
                                        text=message,
                                        parse_mode='HTML'
                                    )
                                
                                sent_entries.add(entry.link)
                                stats['posts_sent'] += 1
                                stats['last_post'] = datetime.now()
                                logger.info(f"Posted: {entry.link}")
                                
                                # Пауза между постами
                                time.sleep(3)
                                
                            except Exception as e:
                                logger.error(f"Send error: {str(e)}")
                                stats['errors'] += 1
                                
                    except Exception as e:
                        logger.error(f"Feed error ({url}): {str(e)}")
                        stats['errors'] += 1
                
                # Ожидание следующей проверки с возможностью прерывания
                logger.info(f"Cycle complete. Next check in {CHECK_INTERVAL} sec")
                self.stop_event.wait(CHECK_INTERVAL)
                
            except Exception as e:
                logger.critical(f"Loop error: {str(e)}")
                stats['errors'] += 1
                time.sleep(30)
                
        logger.info("===== RSS LOOP STOPPED =====")
    
    @staticmethod
    def format_message(entry: Any) -> tuple:
        title = entry.title if hasattr(entry, 'title') else "No title"
        description = entry.description if hasattr(entry, 'description') else ""
        link = entry.link if hasattr(entry, 'link') else ""
        
        # Очистка HTML
        clean: Callable[[str], str] = lambda text: re.sub(r'<[^>]+>', '', text) if text else ""
        title = clean(title)
        description = clean(description)
        
        original_title = title
        original_description = description
        
        # Улучшение текста с помощью YandexGPT
        if not DISABLE_YAGPT and YANDEX_API_KEY and YANDEX_FOLDER_ID:
            try:
                enhanced = enhance_with_yagpt(title, description)
                if enhanced:
                    new_title = enhanced.get('title')
                    new_description = enhanced.get('description')
                    
                    # Проверяем качество улучшения
                    if (new_title and len(new_title) > 10 and 
                        new_description and len(new_description) > 30 and
                        len(new_title) < 120 and len(new_description) < 600):
                        title = new_title
                        description = new_description
                        stats['yagpt_used'] += 1
                        logger.info("YandexGPT enhancement applied")
                    else:
                        logger.warning("YandexGPT output validation failed")
            except Exception as e:
                logger.error(f"YandexGPT integration error: {str(e)}")
                stats['yagpt_errors'] += 1

        # Сокращение описания
        if len(description) > 500:
            description = description[:500] + "..."
            
        # Генерация изображения с заголовком
        image_path = None
        try:
            # Используем оригинальный или улучшенный заголовок для изображения
            image_title = title if title else original_title
            if image_title:
                image_path = image_generator.generate_image(image_title)
                if image_path:
                    logger.info(f"Image generated: {image_path}")
                else:
                    logger.warning("Image generation returned no path")
        except Exception as e:
            logger.error(f"Image generation error: {str(e)}")
        
        # Форматирование сообщения
        message = f"<b>{title}</b>\n\n{description}\n\n<a href='{link}'>🔗 Читать полностью</a>"
        return message, image_path

# Инициализация контроллера
controller = BotController()

# Создаем клавиатуру с кнопками
def create_reply_keyboard() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    
    # Первый ряд - управление
    if controller.status():
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
    BotCommand("sources", "Список источников"),
    BotCommand("yagpt_status", "Статус YandexGPT"),
    BotCommand("test_image", "Тест генерации изображения")
])

# Информация о боте
INFO_MESSAGE = """
ℹ️ <b>Информация о боте</b>

🤖 <b>Автопостинг новостей в Telegram-канал</b>

Этот бот автоматически публикует новости из RSS-лент в ваш канал. 
Просто настройте источники, и бот будет регулярно проверять их 
на наличие новых материалов.

<b>Основные функции:</b>
• Автоматическая публикация новостей
• Генерация изображений с заголовками
• Улучшение текстов с помощью ИИ (YandexGPT)
• Гибкая настройка источников
• Подробная статистика работы

<b>Технические характеристики:</b>
• Поддержка множества RSS-источников
• Автоматическая обработка изображений
• Интеллектуальное форматирование текста
• Управление через Telegram-интерфейс

<b>Версия:</b> 6.0 (Image Generation Pro+) (Июль 2025)
"""

# Краткое описание бота
BOT_DESCRIPTION = """
🤖 <b>Автопостинг новостей в Telegram-канал</b>

Этот бот автоматически публикует новости из RSS-лент в ваш канал. 
Просто настройте источники, и бот будет регулярно проверять их 
на наличие новых материалов.

<b>Основные функции:</b>
• Автоматическая публикация новостей
• Генерация изображений с заголовками
• Фильтрация свежих новостей
• Удобное управление
• Улучшение текстов с помощью YandexGPT
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
/yagpt_status - Статус интеграции с YandexGPT
/test_image - Тест генерации изображения

<b>Используйте кнопки внису для быстрого доступа к командам 👇</b>
"""

# Функции для генерации отчетов
def generate_status_report() -> str:
    """Генерация отчёта о состоянии бота"""
    if not stats['start_time']:
        return "❓ Бот в настоящее время остановлен"
    
    uptime = datetime.now() - stats['start_time']
    hours, remainder = divmod(uptime.total_seconds(), 3600)
    minutes, seconds = divmod(remainder, 60)
    
    last_check = stats['last_check'].strftime("%H:%M:%S") if stats['last_check'] else "никогда"
    last_post = stats['last_post'].strftime("%H:%M:%S") if stats['last_post'] else "никогда"
    
    report = (
        f"🤖 <b>Статус бота</b>\n"
        f"⏱ Время работы: {int(hours)}ч {int(minutes)}м\n"
        f"📊 Отправлено новостей: {stats['posts_sent']}\n"
        f"🖼 Сгенерировано изображений: {stats['images_generated']}\n"
        f"❌ Ошибки: {stats['errors']}\n"
        f"🔄 Последняя проверка: {last_check}\n"
        f"📬 Последняя публикация: {last_post}\n"
        f"🔗 Источников: {len(RSS_URLS)}\n"
        f"📝 Состояние: {'работает ▶️' if controller.status() else 'остановлен 🛑'}"
    )
    return report

def generate_stats_report() -> str:
    """Генерация статистического отчёта"""
    if not stats['start_time']:
        return "📊 Статистика недоступна: бот не запущен"
    
    uptime = datetime.now() - stats['start_time']
    hours = uptime.total_seconds() / 3600
    posts_per_hour = stats['posts_sent'] / hours if hours > 0 else 0
    
    report = (
        f"📈 <b>Статистика бота</b>\n"
        f"⏱ Время работы: {str(uptime).split('.')[0]}\n"
        f"📊 Всего отправлено новостей: {stats['posts_sent']}\n"
        f"🖼 Сгенерировано изображений: {stats['images_generated']}\n"
        f"📮 Средняя скорость: {posts_per_hour:.1f} новостей/час\n"
        f"❌ Всего ошибок: {stats['errors']}\n"
        f"🔗 Источников: {len(RSS_URLS)}\n"
        f"🆔 Канал: {CHANNEL_ID}\n"
        f"🕒 Последняя активность: {stats['last_check'].strftime('%Y-%m-%d %H:%M') if stats['last_check'] else 'N/A'}"
    )
    
    # Добавляем информацию о YandexGPT
    report += (
        f"\n\n🧠 <b>YandexGPT</b>\n"
        f"Статус: {'включен ✅' if not DISABLE_YAGPT else 'выключен ⚠️'}\n"
        f"API ключ: {'установлен' if YANDEX_API_KEY else 'отсутствует'}\n"
        f"Каталог: {'указан' if YANDEX_FOLDER_ID else 'не указан'}\n"
        f"Использовано: {stats['yagpt_used']} раз\n"
        f"Ошибки: {stats['yagpt_errors']}"
    )
    return report

def generate_combined_report() -> str:
    """Объединенный отчет: статус + статистика"""
    status = generate_status_report()
    stats_report = generate_stats_report()
    return f"{status}\n\n{stats_report}"

def list_sources() -> str:
    """Форматированный список источников"""
    sources = "\n".join([f"• {i+1}. {url}" for i, url in enumerate(RSS_URLS)])
    return f"📚 <b>Источники новостей</b> ({len(RSS_URLS)}):\n{sources}"

def get_yagpt_status() -> str:
    """Статус интеграции с YandexGPT"""
    status = "🟢 Активна" if not DISABLE_YAGPT else "🔴 Отключена"
    key_status = "🟢 Установлен" if YANDEX_API_KEY else "🔴 Отсутствует"
    folder_status = "🟢 Указан" if YANDEX_FOLDER_ID else "⚠️ Не указан"
    
    report = (
        f"🧠 <b>Статус YandexGPT</b>\n\n"
        f"• Интеграция: {status}\n"
        f"• API ключ: {key_status}\n"
        f"• Каталог: {folder_status}\n"
        f"• Использовано: {stats['yagpt_used']} раз\n"
        f"• Ошибки: {stats['yagpt_errors']}"
    )
    
    if DISABLE_YAGPT or not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        report += "\n\nℹ️ Для активации установите переменные окружения:\n" \
                    "YANDEX_API_KEY и YANDEX_FOLDER_ID"
                
    return report

# Обработчики команд
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    bot.reply_to(message, 
        f"{BOT_DESCRIPTION}\n\n{COMMANDS_LIST}",
        parse_mode="HTML",
        reply_markup=create_reply_keyboard()
    )

@bot.message_handler(commands=['status'])
def send_status(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    bot.reply_to(message, generate_status_report(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['stats'])
def send_stats(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    bot.reply_to(message, generate_combined_report(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['start_bot'])
def start_command(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    if controller.start():
        bot.reply_to(message, "✅ Публикация начата! 🚀", 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())
    else:
        bot.reply_to(message, "⚠️ Бот уже запущен!", 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['pause', 'stop'])
def stop_command(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    if controller.stop():
        bot.reply_to(message, "🛑 Публикация остановлена! ⏸️", 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())
    else:
        bot.reply_to(message, "⚠️ Бот уже остановлен!", 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['restart'])
def restart_command(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    controller.stop()
    time.sleep(1)
    if controller.start():
        bot.reply_to(message, "🔄 Бот успешно перезапущен! 🔄", 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())
    else:
        bot.reply_to(message, "⚠️ Ошибка при перезапуске!", 
                    parse_mode="HTML",
                    reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['sources'])
def sources_command(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    bot.reply_to(message, list_sources(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['yagpt_status'])
def yagpt_status_command(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    bot.reply_to(message, get_yagpt_status(), 
                parse_mode="HTML",
                reply_markup=create_reply_keyboard())

@bot.message_handler(commands=['test_image'])
def test_image_command(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
        
    test_text = "Тест генерации изображения: Проверка работы системы"
    if message.text is not None:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            test_text = parts[1]
    
    try:
        logger.info(f"Starting image generation with text: {test_text}")
        logger.info(f"Fonts directory: {FONTS_DIR}")
        logger.info(f"Templates directory: {TEMPLATES_DIR}")
        logger.info(f"Default font: {DEFAULT_FONT}")
        
        # Проверка существования шрифта
        font_path = os.path.join(FONTS_DIR, DEFAULT_FONT)
        if not os.path.exists(font_path):
            logger.error(f"Font not found: {font_path}")
            bot.reply_to(message, f"❌ Шрифт не найден: {DEFAULT_FONT}")
            return
            
        # Проверка шаблонов
        templates = [f for f in os.listdir(TEMPLATES_DIR) 
                   if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        if not templates:
            logger.warning("No templates found, using solid background")
        
        image_path = image_generator.generate_image(test_text)
        
        if image_path and os.path.exists(image_path):
            with open(image_path, 'rb') as photo:
                bot.send_photo(
                    message.chat.id,
                    photo,
                    caption=f"✅ Тест генерации изображения\nТекст: {test_text}",
                    parse_mode='HTML'
                )
            os.remove(image_path)
            logger.info(f"Test image sent and removed: {image_path}")
        else:
            logger.error("Image generation returned None or path does not exist")
            bot.reply_to(message, "❌ Ошибка генерации. Проверьте логи для деталей")
            
    except Exception as e:
        error_msg = f"⚠️ Ошибка: {str(e)}"
        logger.error(f"Test image error: {traceback.format_exc()}")
        bot.reply_to(message, error_msg)

# Обработка текстовых сообщений (кнопок)
@bot.message_handler(content_types=['text'])
def handle_text_messages(message: telebot.types.Message) -> None:
    if message.from_user is None or message.from_user.id != OWNER_ID:
        return
    
    if message.text is None:
        return
    
    text = message.text.strip()
    
    # Обработка кнопок
    if text == "▶️ Запустить":
        start_command(message)
    elif text == "⏸️ Приостановить" or text == "🛑 Остановить":
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

# Проверка доступа при запуске
def initial_check() -> Optional[str]:
    try:
        me = bot.get_me()
        logger.info(f"Bot started: @{me.username}")
        
        # Проверка канала
        bot.send_chat_action(CHANNEL_ID, 'typing')
        logger.info(f"Channel access OK: {CHANNEL_ID}")
        
        # Проверка RSS
        for url in RSS_URLS:
            feed = feedparser.parse(url)
            status = "OK" if feed.entries else "ERROR"
            logger.info(f"RSS check: {url} - {status}")
            
        # Проверка YandexGPT
        if not DISABLE_YAGPT and YANDEX_API_KEY and YANDEX_FOLDER_ID:
            logger.info("YandexGPT integration: ACTIVE")
        else:
            logger.info("YandexGPT integration: DISABLED")
            
        # Проверка генератора изображений
        logger.info("Image generator setup:")
        logger.info(f"  Fonts directory: {FONTS_DIR}")
        
        # Проверка наличия шрифта
        font_path = os.path.join(FONTS_DIR, DEFAULT_FONT)
        if os.path.exists(font_path):
            logger.info(f"  Main font: {DEFAULT_FONT} - FOUND")
        else:
            logger.warning(f"  Main font: {DEFAULT_FONT} - NOT FOUND! Using system default")
            
        # Проверка шаблонов
        logger.info(f"  Templates directory: {TEMPLATES_DIR}")
        templates = os.listdir(TEMPLATES_DIR) if os.path.exists(TEMPLATES_DIR) else []
        if templates:
            logger.info(f"  Found {len(templates)} templates")
        else:
            logger.warning("  No templates found! Using solid color backgrounds")
        
        # Тестовая генерация изображения
        test_image_path = image_generator.generate_image("Тест генерации изображения: Запуск бота")
        if test_image_path and os.path.exists(test_image_path):
            logger.info(f"Test image generated: {test_image_path}")
            # Отправляем тестовое изображение владельцу
            try:
                with open(test_image_path, 'rb') as photo:
                    bot.send_photo(OWNER_ID, photo, caption="✅ Тест генерации изображений пройден успешно!")
                os.remove(test_image_path)
            except Exception as e:
                logger.warning(f"Failed to send test image: {str(e)}")
        else:
            logger.warning("Test image generation failed")
            
        # Логирование конфигурации
        logger.info(f"Configuration:")
        logger.info(f"  TOKEN: {TOKEN[:5]}...{TOKEN[-5:]}")
        logger.info(f"  CHANNEL_ID: {CHANNEL_ID}")
        logger.info(f"  OWNER_ID: {OWNER_ID}")
        logger.info(f"  RSS_URLS: {RSS_URLS}")
        logger.info(f"  CHECK_INTERVAL: {CHECK_INTERVAL}")
        logger.info(f"  YANDEX_API_KEY: {'Set' if YANDEX_API_KEY else 'Not set'}")
        logger.info(f"  YANDEX_FOLDER_ID: {YANDEX_FOLDER_ID}")
        logger.info(f"  DISABLE_YAGPT: {DISABLE_YAGPT}")
        
        # Настройки изображений
        logger.info("Image Settings:")
        logger.info(f"  TEXT_COLOR: {TEXT_COLOR}")
        logger.info(f"  STROKE_COLOR: {STROKE_COLOR}")
        logger.info(f"  STROKE_WIDTH: {STROKE_WIDTH}")
        logger.info(f"  MAX_LINES: {MAX_LINES}")
        logger.info(f"  TEXT_AREA_WIDTH: {TEXT_AREA_WIDTH}")
        logger.info(f"  TEXT_POSITION: {TEXT_POSITION_X}/{TEXT_POSITION_Y}")
        logger.info(f"  TEXT_OFFSET: {TEXT_OFFSET_X}/{TEXT_OFFSET_Y}")
        logger.info(f"  FONT_SIZE_RATIO: {FONT_SIZE_RATIO}")
        logger.info(f"  LINE_HEIGHT_RATIO: {LINE_HEIGHT_RATIO}")
        logger.info(f"  BACKGROUND_COLOR: {BACKGROUND_COLOR}")
        logger.info(f"  DEBUG_GRID: {DEBUG_GRID}")
            
    except Exception as e:
        logger.critical(f"STARTUP ERROR: {str(e)}")
        logger.error(traceback.format_exc())
        return f"⚠️ Ошибка при запуске: {str(e)}"
    return None

if __name__ == '__main__':
    logger.info("===== BOT STARTING (Image Generation Pro+) =====")
    error = initial_check()
    
    if error:
        bot.send_message(OWNER_ID, error, parse_mode="HTML")
    
    logger.info("===== READY FOR COMMANDS =====")
    bot.infinity_polling()