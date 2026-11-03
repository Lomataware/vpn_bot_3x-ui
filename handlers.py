import asyncio
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import Config
from database import (
    save_user, get_user_info, set_user_xui, has_used_trial, mark_trial_used,
    get_db_connection, save_payment, update_payment_status, get_payment,
    get_payment_by_external_id
)
from keyboards import get_main_menu, get_back_button, get_plans, get_payment_methods
from xui_api import xui
from crypto_bot import crypto_bot

logger = logging.getLogger(__name__)


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _menu_for_user(user_id: int):
    return get_main_menu(show_trial=not has_used_trial(user_id))


def _format_links_block(result: dict) -> str:
    """Форматирует блок с прямым VLESS-ключом и ссылкой подписки (если задана)."""
    main_url = result.get("subscription_url", "")
    sub_page = result.get("sub_page_url", "")
    lines = f"🔗 <b>Ключ подключения:</b>\n<code>{main_url}</code>\n"
    if sub_page and sub_page != main_url:
        lines += f"\n🌐 <b>Ссылка на подписку (Sub URL):</b>\n<code>{sub_page}</code>\n"
    return lines


async def _activate_user_sub(user_id: int, days: int) -> dict:
    """Создает или продлевает подписку клиента в 3x-ui."""
    info = get_user_info(user_id)
    existing_email = _row_get(info, "xui_email")
    result = await xui.create_or_extend_subscription(
        user_id=user_id,
        days=days,
        existing_username=existing_email
    )
    if result["success"]:
        set_user_xui(
            user_id,
            result["username"],
            result.get("uuid"),
            result.get("sub_id")
        )
    return result


# ===== /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    user = update.effective_user
    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        f"Добро пожаловать в скоростной VPN-сервис на базе 3x-ui.\n"
        f"Выберите нужное действие в меню ниже:"
    )
    await update.message.reply_text(text, reply_markup=_menu_for_user(user.id))


# ===== Выдача подписки админом =====
async def issue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not Config.is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /issue <user_id> <days>\nПример: /issue 123456789 30"
        )
        return
    try:
        user_id = int(context.args[0])
        days = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ user_id и days должны быть целыми числами.")
        return

    result = await _activate_user_sub(user_id, days)
    if result["success"]:
        action_word = "продлена" if result.get("extended") else "активирована"
        expire_str = datetime.fromtimestamp(result["expire"]).strftime("%d.%m.%Y %H:%M")
        links_block = _format_links_block(result)
        try:
            msg = (
                f"🎉 <b>Ваша подписка {action_word}!</b>\n\n"
                f"⏳ Срок: <b>{days} дн.</b> (до <b>{expire_str}</b>)\n"
                f"{links_block}\n"
                f"Скопируйте ключ и импортируйте его в Hiddify / v2rayNG / Streisand / Throne."
            )
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
            await update.message.reply_text(
                f"✅ Подписка {action_word} для {user_id} на {days} дн. ({result['username']})"
            )
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Подписка {action_word}, но не удалось отправить сообщение пользователю: {e}\n"
                f"Клиент: {result['username']}\nСсылка: {result['subscription_url']}"
            )
    else:
        await update.message.reply_text(f"❌ Ошибка 3x-ui: {result.get('error')}")


# ===== Одобрение ручного платежа =====
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not Config.is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /approve <payment_id>")
        return
    try:
        payment_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ payment_id должен быть числом.")
        return

    payment = get_payment(payment_id)
    if not payment:
        await update.message.reply_text("❌ Платеж не найден.")
        return
    if payment["status"] != "pending":
        await update.message.reply_text(f"ℹ️ Статус платежа уже: {payment['status']}")
        return

    user_id = payment["user_id"]
    result = await _activate_user_sub(user_id, 30)
    if result["success"]:
        if not update_payment_status(payment_id, "approved", result["username"]):
            await update.message.reply_text("⚠️ Платеж уже был обработан.")
            return
        mark_trial_used(user_id)
        expire_str = datetime.fromtimestamp(result["expire"]).strftime("%d.%m.%Y")
        links_block = _format_links_block(result)
        try:
            msg = (
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"📅 Действует до: <b>{expire_str}</b>\n"
                f"{links_block}\n"
                f"Импортируйте ключ в Hiddify / v2rayNG / Streisand."
            )
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
            await update.message.reply_text(f"✅ Платеж #{payment_id} подтвержден для пользователя {user_id}")
        except Exception as e:
            await update.message.reply_text(
                f"⚠️ Подписка выдана, но отправка ЛС не удалась: {e}\nСсылка: {result['subscription_url']}"
            )
    else:
        await update.message.reply_text(f"❌ Ошибка 3x-ui: {result.get('error')}")


# ===== Список ожидающих платежей =====
async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not Config.is_admin(update.effective_user.id):
        return
    with get_db_connection() as conn:
        payments = conn.execute('''
            SELECT p.id, p.user_id, p.plan, p.amount, p.method, p.created_at,
                   u.username, u.first_name
            FROM payments p LEFT JOIN users u ON p.user_id = u.id
            WHERE p.status = 'pending' ORDER BY p.created_at DESC
        ''').fetchall()
    if not payments:
        await update.message.reply_text("✅ Нет ожидающих платежей.")
        return
    text = f"⏳ <b>Ожидающие платежи ({len(payments)}):</b>\n\n"
    for p in payments:
        user_info = f"@{p['username']}" if p["username"] else (p["first_name"] or p["user_id"])
        text += f"#{p['id']} | {user_info} | {p['amount']} ₽ | {p['method']}\n👉 /approve {p['id']}\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ===== Рассылка с защитой от FloodWait =====
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not Config.is_admin(update.effective_user.id):
        return
    message = " ".join(context.args) if context.args else ""
    if not message:
        await update.message.reply_text("Использование: /broadcast <текст сообщения>")
        return
    with get_db_connection() as conn:
        users = conn.execute("SELECT id FROM users WHERE is_blocked = FALSE").fetchall()
    total = len(users)
    await update.message.reply_text(f"📢 Начинаю рассылку для {total} пользователей...")
    sent, failed = 0, 0
    for row in users:
        try:
            await context.bot.send_message(chat_id=row[0], text=message)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            if "blocked" in str(e).lower() or "deleted" in str(e).lower() or "403" in str(e):
                with get_db_connection() as conn:
                    conn.execute("UPDATE users SET is_blocked = TRUE WHERE id = ?", (row[0],))
            logger.error(f"Broadcast error {row[0]}: {e}")
    await update.message.reply_text(f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}")


# ===== Обработчик inline-кнопок =====
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = update.effective_user
    save_user(user)

    try:
        if data == "main_menu":
            await query.answer()
            await query.edit_message_text("Выберите действие:", reply_markup=_menu_for_user(user.id))

        elif data == "free_trial":
            if has_used_trial(user.id):
                await query.answer("Вы уже использовали пробный период!", show_alert=True)
                await query.edit_message_text("Выберите действие:", reply_markup=_menu_for_user(user.id))
                return
            await query.answer("Создаем тестовый доступ...")
            result = await _activate_user_sub(user.id, Config.TRIAL_DAYS)
            if result["success"]:
                mark_trial_used(user.id)
                expire_str = datetime.fromtimestamp(result["expire"]).strftime("%d.%m.%Y %H:%M")
                links_block = _format_links_block(result)
                text = (
                    f"🎁 <b>Пробный период на {Config.TRIAL_DAYS} дн. активирован!</b>\n\n"
                    f"📅 Действует до: <b>{expire_str}</b>\n"
                    f"{links_block}\n"
                    f"Скопируйте ключ и добавьте в приложение Hiddify / v2rayNG / Streisand."
                )
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_back_button("main_menu")
                )
            else:
                logger.error(f"Free trial creation failed for {user.id}: {result.get('error')}")
                await query.edit_message_text(
                    f"❌ Не удалось создать тестовую подписку ({result.get('error')}). "
                    f"Попробуйте позже или напишите в поддержку @{Config.SUPPORT_USERNAME}.",
                    reply_markup=get_back_button("main_menu")
                )

        elif data == "buy_subscription":
            await query.answer()
            text = (
                "💳 <b>Покупка подписки</b>\n\n"
                "🚀 Высокая скорость и минимальный пинг.\n"
                "🛡 Современные протоколы обхода блокировок на базе 3x-ui.\n"
                "📱 Поддержка всех устройств (Android, iOS, Windows, macOS, Linux).\n\n"
                "Выберите тарифный план:"
            )
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_plans())

        elif data == "plan_1":
            await query.answer()
            context.user_data["selected_plan"] = {
                "name": "1 месяц",
                "price": Config.PRICE_1_MONTH,
                "days": 30
            }
            text = (
                f"📦 Тариф: <b>1 месяц</b>\n"
                f"💰 Стоимость: <b>{Config.PRICE_1_MONTH} ₽</b>\n\n"
                f"Выберите удобный способ оплаты:"
            )
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_payment_methods())

        elif data == "pay_manual":
            await query.answer()
            plan = context.user_data.get(
                "selected_plan", {"name": "1 месяц", "price": Config.PRICE_1_MONTH}
            )
            payment_id = save_payment(user.id, plan.get("name"), plan.get("price"), "card")
            text = (
                f"💳 <b>Оплата переводом</b>\n\n"
                f"📦 Тариф: <b>{plan.get('name')}</b>\n"
                f"💰 К оплате: <b>{plan.get('price')} ₽</b>\n"
                f"🆔 Номер заказа: <code>#{payment_id}</code>\n\n"
                f"🏦 <b>Реквизиты для перевода:</b>\n"
                f"• <b>Банк:</b> {Config.BANK_NAME}\n"
                f"• <b>Номер / СБП:</b> <code>{Config.SUPPORT_PHONE}</code>\n\n"
                f"После оплаты отправьте чек в поддержку @{Config.SUPPORT_USERNAME} "
                f"и укажите номер заказа: <code>#{payment_id}</code>\n\n"
                f"Подписка будет активирована сразу после проверки."
            )
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=get_back_button("main_menu")
            )

        elif data == "pay_crypto":
            await query.answer()
            plan = context.user_data.get(
                "selected_plan", {"name": "1 месяц", "price": Config.PRICE_1_MONTH}
            )
            result = await crypto_bot.create_invoice(
                amount=plan.get("price"),
                description=f"VPN {plan.get('name')}",
                user_id=user.id
            )
            if result["success"]:
                payment_id = save_payment(
                    user.id, plan.get("name"), plan.get("price"),
                    "crypto", "pending", str(result["invoice_id"])
                )
                context.user_data["active_invoice"] = {
                    "invoice_id": result["invoice_id"],
                    "payment_id": payment_id
                }
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Оплатить в CryptoBot", url=result["pay_url"])],
                    [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_{result['invoice_id']}")],
                    [InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]
                ])
                text = (
                    f"💎 <b>Оплата криптовалютой</b>\n\n"
                    f"📦 Тариф: <b>{plan.get('name')}</b>\n"
                    f"💰 Сумма: <b>{result.get('amount')} {result.get('currency', 'RUB')}</b>\n"
                    f"🆔 Заказ: <code>#{payment_id}</code>\n\n"
                    f"Нажмите кнопку ниже для оплаты, затем нажмите «Проверить оплату»."
                )
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                logger.error(f"CryptoBot create_invoice failed: {result.get('error')}")
                text = (
                    "⚠️ <b>Ошибка создания счета CryptoBot</b>\n\n"
                    "<i>Пожалуйста, выберите другой способ оплаты или обратитесь в поддержку.</i>"
                )
                await query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=get_back_button("main_menu")
                )

        elif data.startswith("check_"):
            try:
                invoice_id = int(data.split("_")[1])
            except (ValueError, IndexError):
                await query.answer("Некорректный счет", show_alert=True)
                return

            payment_record = get_payment_by_external_id(str(invoice_id), user.id)
            if not payment_record:
                invoice = context.user_data.get("active_invoice", {})
                if invoice and invoice.get("invoice_id") == invoice_id:
                    payment_record = get_payment(invoice["payment_id"])

            if not payment_record:
                await query.answer("Счет не найден в базе данных", show_alert=True)
                return

            payment_id = payment_record["id"]
            if payment_record["status"] in ("paid", "approved"):
                await query.answer("Этот счет уже оплачен и активирован!", show_alert=True)
                return

            result = await crypto_bot.get_invoice(invoice_id)
            if not result["success"]:
                await query.answer("Ошибка проверки счета в CryptoBot", show_alert=True)
                return

            status = result["status"]
            if status == "paid":
                await query.answer("Оплата получена! Активируем подписку...")
                mr = await _activate_user_sub(user.id, 30)
                if mr["success"]:
                    update_payment_status(payment_id, "paid", mr["username"])
                    mark_trial_used(user.id)
                    expire_str = datetime.fromtimestamp(mr["expire"]).strftime("%d.%m.%Y")
                    links_block = _format_links_block(mr)
                    text = (
                        f"✅ <b>Оплата прошла успешно!</b>\n\n"
                        f"📅 Подписка до: <b>{expire_str}</b>\n"
                        f"{links_block}\n"
                        f"Скопируйте ключ и добавьте в Hiddify / v2rayNG / Streisand."
                    )
                    await query.edit_message_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_back_button("main_menu")
                    )
                else:
                    logger.error(f"3x-ui activation failed after CryptoBot payment #{payment_id}: {mr.get('error')}")
                    await query.answer(
                        f"Оплата получена, но 3x-ui вернул ошибку: {mr.get('error')}. Попробуйте нажать кнопку еще раз.",
                        show_alert=True
                    )
            elif status == "active":
                await query.answer("Счет еще не оплачен.", show_alert=True)
            elif status == "expired":
                await query.answer("Срок действия счета истек.", show_alert=True)
            else:
                await query.answer(f"Статус счета: {status}", show_alert=True)

        elif data == "my_account":
            await query.answer()
            info = get_user_info(user.id)
            created = _row_get(info, "created_at", "—")
            email = _row_get(info, "xui_email")
            username_disp = f"@{user.username}" if user.username else "не указан"
            text = (
                f"👤 <b>Личный кабинет</b>\n\n"
                f"🆔 ID: <code>{user.id}</code>\n"
                f"🙍‍♂️ Имя: {user.full_name}\n"
                f"📧 Username: {username_disp}\n"
                f"📅 Регистрация: {str(created)[:10]}\n\n"
            )
            keyboard_rows = []
            if email:
                ud = await xui.get_user(email)
                if ud["success"]:
                    d = ud["data"]
                    status = d.get("status", "unknown")
                    expire = d.get("expire")
                    status_emoji = "🟢" if status == "active" else "🔴"
                    expire_str = datetime.fromtimestamp(expire).strftime("%d.%m.%Y") if expire else "безлимит"
                    text += f"{status_emoji} <b>Статус подписки:</b> {status} (до {expire_str})\n"
                    keyboard_rows.append([InlineKeyboardButton("📊 Детали трафика", callback_data="sub_info")])
                    keyboard_rows.append([InlineKeyboardButton("🔗 Получить ключ подключения", callback_data="resend_link")])
                else:
                    text += "⚪ <b>Статус подписки:</b> не найдена в панели\n"
            else:
                text += "⚪ <b>Статус подписки:</b> нет активной подписки\n"

            keyboard_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard_rows)
            )

        elif data == "sub_info":
            info = get_user_info(user.id)
            email = _row_get(info, "xui_email")
            if not email:
                await query.answer("У вас нет подписки", show_alert=True)
                return
            ud = await xui.get_user(email)
            if not ud["success"]:
                await query.answer("Не удалось получить данные из 3x-ui", show_alert=True)
                return
            await query.answer()
            d = ud["data"]
            expire = d.get("expire")
            expire_str = datetime.fromtimestamp(expire).strftime("%d.%m.%Y %H:%M") if expire else "безлимит"
            used = (d.get("used_traffic") or 0) / (1024 ** 3)
            limit = d.get("data_limit")
            limit_str = f"{limit / (1024 ** 3):.0f} ГБ" if limit else "Безлимит"
            status = d.get("status", "unknown")
            status_emoji = "🟢" if status == "active" else "🔴"
            text = (
                f"📊 <b>Информация о подписке</b>\n\n"
                f"{status_emoji} <b>Статус:</b> {status}\n"
                f"⏳ <b>Истекает:</b> {expire_str}\n"
                f"📈 <b>Трафик:</b> {used:.2f} ГБ / {limit_str}\n"
            )
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=get_back_button("my_account")
            )

        elif data == "resend_link":
            info = get_user_info(user.id)
            email = _row_get(info, "xui_email")
            if not email:
                await query.answer("Подписка не найдена", show_alert=True)
                return
            ud = await xui.get_user(email)
            if ud["success"] and ud.get("subscription_url"):
                await query.answer()
                links_block = _format_links_block(ud)
                text = (
                    f"{links_block}\n"
                    f"Скопируйте ключ и импортируйте в Hiddify / v2rayNG / Streisand / v2rayN."
                )
                await query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=get_back_button("my_account")
                )
            else:
                await query.answer("Не удалось получить ссылку", show_alert=True)

        elif data == "terms":
            await query.answer()
            text = (
                "📜 <b>Правила использования сервиса</b>\n\n"
                "1. Запрещено использование торрентов (BitTorrent заблокирован на уровне сервера).\n"
                "2. Запрещена рассылка спама, DDoS-атаки и любая противоправная активность.\n"
                "3. Ключ предназначен для личного использования.\n"
                "4. Поддержка отвечает в течение 24 часов.\n"
                "5. При нарушении правил доступ аннулируется без возврата средств.\n"
            )
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())

        elif data == "help":
            await query.answer()
            text = (
                "🆘 <b>Помощь и подключение</b>\n\n"
                f"👨‍💻 <b>Поддержка:</b> @{Config.SUPPORT_USERNAME}\n\n"
                "<b>Как подключиться:</b>\n"
                "1. Получите пробный доступ или оплатите подписку в боте.\n"
                "2. Скопируйте выданный ключ (нажмите на него).\n"
                "3. Установите клиент для вашей ОС и добавьте ключ из буфера обмена:\n"
                "   • <b>Hiddify</b> (Android / iOS / Windows / macOS / Linux)\n"
                "   • <b>v2rayNG</b> (Android)\n"
                "   • <b>Streisand / V2Box</b> (iOS)\n"
                "   • <b>v2rayN / Throne</b> (Windows / Linux)\n"
            )
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_back_button())

    except Exception as e:
        logger.error(f"handle_button error ({data}): {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "⚠️ Произошла ошибка. Вернитесь в главное меню.",
                reply_markup=get_back_button("main_menu")
            )
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    user = update.effective_user
    text = update.message.text.lower()
    if "привет" in text or "hi" in text or "меню" in text:
        await update.message.reply_text("Главное меню:", reply_markup=_menu_for_user(user.id))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not Config.is_admin(update.effective_user.id):
        return
    with get_db_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE date(last_active) = date('now')").fetchone()[0]
        trials = conn.execute("SELECT COUNT(*) FROM users WHERE trial_used = TRUE").fetchone()[0]
        pend = conn.execute("SELECT COUNT(*) FROM payments WHERE status = 'pending'").fetchone()[0]
        paid = conn.execute("SELECT COUNT(*) FROM payments WHERE status IN ('approved','paid')").fetchone()[0]
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"🟢 Активны сегодня: {active}\n"
        f"🎁 Использовали триал: {trials}\n\n"
        f"⏳ Ожидают подтверждения: {pend}\n"
        f"✅ Успешных оплат: {paid}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}", exc_info=context.error)
