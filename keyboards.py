from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import Config


def get_main_menu(show_trial: bool = False):
    buttons = []
    if show_trial:
        buttons.append([
            InlineKeyboardButton(
                f"🎁 Бесплатный тест ({Config.TRIAL_DAYS} дн.)",
                callback_data="free_trial"
            )
        ])
    buttons.extend([
        [InlineKeyboardButton("💳 Купить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton("👤 Мой аккаунт", callback_data="my_account")],
        [InlineKeyboardButton("📜 Правила использования", callback_data="terms")],
        [InlineKeyboardButton("🆘 Помощь и инструкция", callback_data="help")],
    ])
    return InlineKeyboardMarkup(buttons)


def get_back_button(target="main_menu"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data=target)]
    ])


def get_plans():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"1 месяц — {Config.PRICE_1_MONTH} ₽", callback_data="plan_1")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ])


def get_payment_methods():
    buttons = [
        [InlineKeyboardButton("💳 Перевод по СБП / Карте", callback_data="pay_manual")],
    ]
    if Config.CRYPTOBOT_ENABLED:
        buttons.insert(0, [InlineKeyboardButton("💎 Криптовалюта (CryptoBot)", callback_data="pay_crypto")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="buy_subscription")])
    return InlineKeyboardMarkup(buttons)


def get_admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]
    ])
