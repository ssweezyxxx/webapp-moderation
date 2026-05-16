import os
import threading
import logging

from flask import Flask, request, jsonify, render_template
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Настройка логирования для отслеживания ошибок на сервере
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("Переменная окружения TELEGRAM_TOKEN не задана!")

BOT_USERNAME = "m0d3rati0n_bot" # Замените на username вашего бота

# --- Создаём Flask приложение ---
flask_app = Flask(__name__)

# --- Создаём приложение Telegram бота ---
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

# --- Простой обработчик команд (пример) ---
async def start_handler(update: Update, context):
    await update.message.reply_text('Привет! Я ваш бот-модератор.')

telegram_app.add_handler(CommandHandler("start", start_handler))

# --- API для веб-интерфейса ---
@flask_app.route("/my_groups")
def my_groups():
    """Эндпоинт для получения списка групп пользователя."""
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    # TODO: Здесь вам нужно реализовать логику получения списка групп,
    # где пользователь является администратором и бот добавлен.
    # Пока возвращаем тестовые данные для отладки.
    test_groups = [
        {
            "id": -1001234567890,
            "title": "Тестовая группа",
            "settings": {
                "automod_enabled": True,
                "check_links": True,
                "check_keywords": True,
                "check_flood": True,
                "action_on_link": "delete",
                "action_on_keyword": "warn",
                "action_on_flood": "mute",
                "flood_threshold": 5,
                "mute_duration": 10,
                "warn_limit": 3
            },
            "words": ["спам", "реклама"],
            "admins": [int(user_id)]
        }
    ]
    return jsonify({"groups": test_groups})

@flask_app.route("/settings", methods=["POST"])
def save_settings():
    """Эндпоинт для сохранения настроек."""
    data = request.get_json()
    chat_id = data.get("chat_id")
    action = data.get("action")
    
    # TODO: Реализуйте сохранение в базу данных.
    logger.info(f"Получен запрос на сохранение: chat_id={chat_id}, action={action}")
    return jsonify({"status": "ok"})

@flask_app.route("/")
def index():
    """Отдаёт главную HTML-страницу."""
    return render_template("index.html")

# --- Функция для запуска бота в отдельном потоке ---
def run_telegram_bot():
    """Запускает polling Telegram бота."""
    logger.info("Запуск Telegram бота...")
    telegram_app.run_polling()

# --- Точка входа для Render ---
if __name__ == "__main__":
    # Запускаем Telegram бота в фоне
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.start()
    logger.info("Бот запущен в фоновом потоке.")
    
    # Запускаем Flask сервер
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Запуск Flask сервера на порту {port}...")
    flask_app.run(host="0.0.0.0", port=port)
