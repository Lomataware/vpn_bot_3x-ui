import asyncio
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
    broadcast_command, diag_command, sync_hy_command, error_handler
)
from xui_api import xui

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def _post_start_sync(application: Application):
    """При старте бота синхронизируем существующих клиентов с Hysteria2-инбаундом,
    чтобы старые подписки тоже начали отдавать vless+hysteria2."""
    if not Config.XUI_ENABLE_HYSTERIA:
        return
    try:
        res = await xui.sync_all_hysteria_clients()
        if res.get("error"):
            logger.warning(f"Стартовая синхронизация Hysteria2: {res['error']}")
        else:
            logger.info(
                f"Стартовая синхронизация Hysteria2: инбаунд ID={res['hysteria_inbound']}, "
                f"проверено={res['checked']}, добавлено={res['added']}, "
                f"обновлено={res['updated']}, ошибок={res['failed']}"
            )
    except Exception as e:
        logger.warning(f"Стартовая синхронизация Hysteria2 не удалась: {e}")


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
    app.add_handler(CommandHandler("diag", diag_command))
    app.add_handler(CommandHandler("synchy", sync_hy_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    app.post_init = lambda application: asyncio.ensure_future(_post_start_sync(application))

    logger.info("VPN-бот (3x-ui) успешно запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
