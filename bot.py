import logging
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters
)
from config import Config
from database import init_db
from handlers import (
    start, handle_button, handle_message,
    stats_command, issue_command, approve_command, pending_command,
    broadcast_command, error_handler
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def main():
    if not Config.BOT_TOKEN:
        logger.error("Не задан BOT_TOKEN в .env файле!")
        return
    if not Config.XUI_URL:
        logger.warning("Не задан XUI_URL в .env файле!")

    init_db()

    app = Application.builder().token(Config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("issue", issue_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("VPN-бот (3x-ui) успешно запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
