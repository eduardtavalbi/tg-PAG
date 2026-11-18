from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from datetime import datetime

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def stats_navigation_keyboard(year: int, month: int):
    if month == 1:
        previous_year = year - 1
        previous_month = 12
    else:
        previous_year = year
        previous_month = month - 1

    if month == 12:
        next_year = year + 1
        next_month = 1
    else:
        next_year = year
        next_month = month + 1

    now = datetime.now()
    current_month = (year == now.year and month == now.month)

    buttons = [
        [
            InlineKeyboardButton(
                text="◀️ Предыдущий",
                callback_data=f"stats:{previous_year}:{previous_month}",
            ),
            InlineKeyboardButton(
                text="Следующий ▶️",
                callback_data=f"stats:{next_year}:{next_month}",
            ),
        ]
    ]

    if not current_month:
        buttons.append([
            InlineKeyboardButton(
                text="📅 Текущий месяц",
                callback_data="stats:current",
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)



def main_menu_keyboard():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="💰 Баланс",
        callback_data="menu:balance",
    )

    builder.button(
        text="📊 Статистика",
        callback_data="menu:stats",
    )

    builder.button(
        text="📜 История операций",
        callback_data="menu:history",
    )

    builder.button(
        text="🎯 Финансовые цели",
        callback_data="menu:goals",
    )

    builder.button(
        text="💳 Мои бюджеты",
        callback_data="budget:list",
    )

    builder.button(
        text="🤖 AI-советник",
        callback_data="menu:advice",
    )

    builder.adjust(2, 2, 2)

    return builder.as_markup()

def transaction_confirmation_keyboard():
    builder = InlineKeyboardBuilder()

    builder.button(
        text="✅ Подтвердить",
        callback_data="ai:confirm",
    )

    builder.button(
        text="❌ Отменить",
        callback_data="ai:cancel",
    )

    builder.adjust(2)

    return builder.as_markup()
