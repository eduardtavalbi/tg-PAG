import logging
import uuid

from app.budget_scheduler import send_monthly_budget_notifications
from datetime import datetime
from decimal import Decimal, InvalidOperation

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from app.database import requests as rq
from app.ai.gemini import parse_transactions, generate_financial_advice
from app.keyboards import (
    main_menu_keyboard,
    transaction_confirmation_keyboard,
    stats_navigation_keyboard,
)

router = Router()
logger = logging.getLogger(__name__)

CURRENCIES = {
    "CNY": "¥",
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
}


# ---------------------------------------------------------------------
# FSM
# ---------------------------------------------------------------------

class PendingTransactions(StatesGroup):
    waiting_confirmation = State()


class PendingGoal(StatesGroup):
    waiting_confirmation = State()


class PendingGoalEdit(StatesGroup):
    waiting_value = State()


class BudgetStates(StatesGroup):
    waiting_amount = State()
    waiting_edit_amount = State()


# ---------------------------------------------------------------------
# COMMON HELPERS
# ---------------------------------------------------------------------

def to_decimal(value) -> Decimal:
    result = Decimal(str(value).replace(",", "."))
    if not result.is_finite():
        raise ValueError("Сумма должна быть конечным числом.")
    return result


def format_amount(value) -> str:
    amount = to_decimal(value)
    if amount == amount.to_integral_value():
        return f"{amount:,.0f}".replace(",", " ")
    return f"{amount:,.2f}".replace(",", " ")


def format_money(amount, currency="CNY") -> str:
    currency = (currency or "CNY").upper()
    symbol = CURRENCIES.get(currency, currency)
    return f"{format_amount(amount)} {symbol}"



def format_transaction(transaction) -> str:
    currency = transaction.get("currency", "CNY")

    if transaction["type"] == "income":
        emoji, sign = "🟢", "+"
    else:
        emoji, sign = "🔴", "−"

    amount = format_money(transaction["amount"], currency)
    description = transaction.get("description", "Операция")
    category = transaction.get("category", "Другое")

    return (
        f"{emoji} {sign}{amount}"
        f" — {description}\n"
        f"   📁 {category}"
    )

def currency_keyboard(prefix: str, currencies=None):
    currencies = currencies or list(CURRENCIES)
    rows = []
    row = []
    for currency in currencies:
        row.append(
            InlineKeyboardButton(
                text=f"{CURRENCIES.get(currency, currency)} {currency}",
                callback_data=f"{prefix}:{currency}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="budget:cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def goal_confirmation_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="goal:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="goal:cancel"),
    ]])


def goals_list_keyboard(goals):
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🎯 {goal.name}",
                callback_data=f"goal:view:{goal.id}",
            )
        ]
        for goal in goals
    ]

    buttons.append([
        InlineKeyboardButton(
            text="🏆 Завершённые цели",
            callback_data="goal:completed:list",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def goal_details_keyboard(goal_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✏️ Редактировать",
            callback_data=f"goal:edit:{goal_id}",
        )],
        [InlineKeyboardButton(
            text="✅ Выполнить цель",
            callback_data=f"goal:complete:ask:{goal_id}",
        )],
        [InlineKeyboardButton(
            text="🗑 Удалить цель",
            callback_data=f"goal:delete:ask:{goal_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ К списку целей",
            callback_data="goal:list",
        )],
    ])


def completed_goals_keyboard(goals):
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🏆 {goal.name}",
                callback_data=f"goal:completed:view:{goal.id}",
            )
        ]
        for goal in goals
    ]

    buttons.append([
        InlineKeyboardButton(
            text="⬅️ К активным целям",
            callback_data="goal:list",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def completed_goal_details_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⬅️ К завершённым целям",
                callback_data="goal:completed:list",
            )
        ],
        [
            InlineKeyboardButton(
                text="🎯 К активным целям",
                callback_data="goal:list",
            )
        ],
    ])

def goal_edit_keyboard(goal_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Название", callback_data=f"goal:editfield:{goal_id}:name")],
        [InlineKeyboardButton(text="💰 Целевая сумма", callback_data=f"goal:editfield:{goal_id}:amount")],
        [InlineKeyboardButton(text="📅 Дедлайн", callback_data=f"goal:editfield:{goal_id}:deadline")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"goal:view:{goal_id}")],
    ])


def goal_complete_keyboard(goal_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выполнить", callback_data=f"goal:complete:confirm:{goal_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"goal:view:{goal_id}"),
    ]])


def goal_delete_keyboard(goal_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"goal:delete:confirm:{goal_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"goal:delete:cancel:{goal_id}"),
    ]])


def format_goal_progress(progress):
    goal = progress["goal"]
    currency = getattr(goal, "currency", "CNY")
    return (
        f"🎯 {goal.name}\n\n"
        f"💰 Накоплено: {format_money(progress['saved'], currency)}\n"
        f"🎯 Цель: {format_money(progress['target'], currency)}\n"
        f"📊 Прогресс: {progress['progress']:.1f}%\n"
        f"Осталось: {format_money(progress['remaining'], currency)}"
    )


def budgets_list_keyboard(budgets):
    buttons = [[InlineKeyboardButton(
        text=f"💳 {item['category']} · {item['currency']}",
        callback_data=f"budget:view:{item['budget_id']}",
    )] for item in budgets]
    buttons.append([InlineKeyboardButton(text="➕ Создать бюджет", callback_data="budget:create")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def budget_categories_keyboard(categories):
    buttons = [[InlineKeyboardButton(
        text=category.name,
        callback_data=f"budget:category:{category.id}",
    )] for category in categories]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="budget:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def budget_details_keyboard(budget_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить лимит", callback_data=f"budget:edit:{budget_id}")],
        [InlineKeyboardButton(text="🗑 Удалить бюджет", callback_data=f"budget:delete:ask:{budget_id}")],
        [InlineKeyboardButton(text="⬅️ К бюджетам", callback_data="budget:list")],
    ])


def budget_delete_keyboard(budget_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"budget:delete:confirm:{budget_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"budget:view:{budget_id}"),
    ]])


def format_budget_progress(progress):
    percentage = progress["percentage"]
    if percentage >= 100:
        status = "🔴 Лимит превышен"
    elif percentage >= 90:
        status = "🟠 Лимит почти исчерпан"
    elif percentage >= 70:
        status = "🟡 Расходы приближаются к лимиту"
    else:
        status = "🟢 Всё в пределах бюджета"

    currency = progress["currency"]
    return (
        f"💳 БЮДЖЕТ: {progress['category']}\n\n"
        f"📅 Месяц: {progress['month']}\n\n"
        f"💰 Лимит: {format_money(progress['limit'], currency)}\n"
        f"🔴 Потрачено: {format_money(progress['spent'], currency)}\n"
        f"🟢 Осталось: {format_money(progress['remaining'], currency)}\n\n"
        f"📊 Использовано: {percentage:.1f}%\n{status}"
    )


def find_matching_goal(goals, requested_name):
    requested_name = requested_name.casefold().strip()
    matches = [
        goal for goal in goals
        if requested_name == goal.name.casefold()
        or requested_name in goal.name.casefold()
        or goal.name.casefold() in requested_name
    ]
    return matches[0] if len(matches) == 1 else None


async def run_financial_advisor(message: Message, tg_id: int):
    status = await message.answer(
        "🤖 Анализирую твои финансы за последние 30 дней..."
    )
    try:
        data = await rq.get_advisor_data(tg_id)
        advice = await generate_financial_advice(data)
        await status.edit_text(advice)
    except Exception:
        logger.exception("Ошибка AI-советника для пользователя %s", tg_id)
        await status.edit_text(
            "❌ Не удалось подготовить финансовый анализ. Попробуй позже."
        )


# ---------------------------------------------------------------------
# START, MENU, CANCEL, ADVISOR
# ---------------------------------------------------------------------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await rq.set_user(message.from_user.id)
    await rq.create_default_categories(message.from_user.id)
    await message.answer(
        "👋 Привет! Я твой AI-финансовый помощник.\n\n"
        "Пиши о доходах и расходах обычным текстом.\n"
        "Например: «Получил 8000 CNY зарплаты, потратил 500 CNY на продукты».\n\n"
        "Я распознаю операции и предложу сохранить их.\n\n"
        "Также можно вести финансовые цели:\n"
        "«Хочу накопить 50000 CNY на MacBook»\n"
        "«Отложил 3000 CNY на MacBook»\n\n"
        "Команды: /balance, /stats, /history, /goals, /advice.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())


@router.message(Command("advice"))
async def advice_command(message: Message, state: FSMContext):
    if await state.get_state():
        await message.answer("Сначала заверши текущее действие или используй /cancel.")
        return
    await run_financial_advisor(message, message.from_user.id)


@router.callback_query(F.data == "menu:advice")
async def advice_callback(callback: CallbackQuery, state: FSMContext):
    if await state.get_state():
        await callback.answer("Сначала заверши текущее действие или используй /cancel.", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await run_financial_advisor(callback.message, callback.from_user.id)


# ---------------------------------------------------------------------
# BALANCE AND STATISTICS
# ---------------------------------------------------------------------

async def build_balance_text(tg_id: int):
    lines = ["💰 БАЛАНС ПО ВАЛЮТАМ\n"]
    found = False
    for currency in CURRENCIES:
        value = await rq.get_balance(tg_id, currency=currency)
        if value != 0:
            lines.append(format_money(value, currency))
            found = True
    if not found:
        lines.append("Операций пока нет.")
    return "\n".join(lines)


@router.message(Command("balance"))
async def balance_command(message: Message):
    await message.answer(
        await build_balance_text(message.from_user.id),
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:balance")
async def balance_callback(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            await build_balance_text(callback.from_user.id),
            reply_markup=main_menu_keyboard(),
        )


MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


async def build_stats_text(tg_id: int, year: int, month: int):
    if month == 1:
        py, pm = year - 1, 12
    else:
        py, pm = year, month - 1

    sections = [f"📊 СТАТИСТИКА ЗА {MONTH_NAMES[month - 1].upper()} {year}"]
    has_any = False

    for currency in CURRENCIES:
        current = await rq.get_monthly_statistics(
            tg_id, year=year, month=month, currency=currency
        )
        previous = await rq.get_monthly_statistics(
            tg_id, year=py, month=pm, currency=currency
        )
        if not current["income"] and not current["expense"] and not previous["income"] and not previous["expense"]:
            continue

        has_any = True

        def compare(now_value, before_value):
            if before_value == 0:
                return "➡️ 0%" if now_value == 0 else "🆕 появились операции"
            change = (now_value - before_value) / abs(before_value) * 100
            if change > 0:
                return f"🔺 +{change:.1f}%"
            if change < 0:
                return f"🔻 {change:.1f}%"
            return "➡️ 0%"

        sections.append(
            f"\n💱 {currency}\n"
            f"🟢 Доходы: {format_money(current['income'], currency)}\n"
            f"🔴 Расходы: {format_money(current['expense'], currency)}\n"
            f"💰 Разница: {format_money(current['balance'], currency)}"
        )

        if current["expense_categories"]:
            sections.append("\n📉 Расходы по категориям:")
            for item in sorted(current["expense_categories"], key=lambda x: x["amount"], reverse=True):
                sections.append(f"• {item['name']}: {format_money(item['amount'], currency)}")

        if current["income_categories"]:
            sections.append("\n📈 Доходы по категориям:")
            for item in sorted(current["income_categories"], key=lambda x: x["amount"], reverse=True):
                sections.append(f"• {item['name']}: {format_money(item['amount'], currency)}")

        sections.append(
            "\n📅 Сравнение с предыдущим месяцем:\n"
            f"Доходы: {compare(current['income'], previous['income'])}\n"
            f"Расходы: {compare(current['expense'], previous['expense'])}\n"
            f"Баланс: {compare(current['balance'], previous['balance'])}"
        )

    if not has_any:
        sections.append("\nОпераций за этот и предыдущий месяцы нет.")
    return "\n".join(sections)


@router.message(Command("stats"))
async def stats_command(message: Message):
    now = datetime.now()
    await message.answer(
        await build_stats_text(message.from_user.id, now.year, now.month),
        reply_markup=stats_navigation_keyboard(now.year, now.month),
    )


@router.callback_query(F.data.startswith("stats:"))
async def stats_navigation(callback: CallbackQuery):
    parts = callback.data.split(":")
    now = datetime.now()
    if parts[1] == "current":
        year, month = now.year, now.month
    else:
        year, month = int(parts[1]), int(parts[2])

    if not 1 <= month <= 12 or (year, month) > (now.year, now.month):
        await callback.answer("Эта дата недоступна.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(
            await build_stats_text(callback.from_user.id, year, month),
            reply_markup=stats_navigation_keyboard(year, month),
        )
    await callback.answer()


@router.callback_query(F.data == "menu:stats")
async def stats_callback(callback: CallbackQuery):
    now = datetime.now()
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            await build_stats_text(callback.from_user.id, now.year, now.month),
            reply_markup=stats_navigation_keyboard(now.year, now.month),
        )


# ---------------------------------------------------------------------
# GOALS
# ---------------------------------------------------------------------

@router.message(Command("goals"))
async def goals_command(message: Message):
    goals = await rq.get_goals(message.from_user.id)

    text = (
        "🎯 Выбери активную финансовую цель:"
        if goals
        else "🎯 Активных финансовых целей пока нет."
    )

    await message.answer(
        text,
        reply_markup=goals_list_keyboard(goals),
    )

@router.callback_query(F.data == "menu:goals")
async def menu_goals_callback(callback: CallbackQuery):
    goals = await rq.get_goals(callback.from_user.id)
    await callback.answer()

    if not callback.message:
        return

    text = (
        "🎯 Выбери активную финансовую цель:"
        if goals
        else "🎯 Активных финансовых целей пока нет."
    )

    await callback.message.answer(
        text,
        reply_markup=goals_list_keyboard(goals),
    )

@router.callback_query(F.data == "goal:list")
async def goals_list_callback(callback: CallbackQuery):
    goals = await rq.get_goals(callback.from_user.id)
    if not callback.message:
        await callback.answer()
        return
    if not goals:
        await callback.message.edit_text("🎯 Активных целей пока нет.")
    else:
        await callback.message.edit_text(
            "🎯 ТВОИ ФИНАНСОВЫЕ ЦЕЛИ\n\nВыбери цель:",
            reply_markup=goals_list_keyboard(goals),
        )
    await callback.answer()

@router.callback_query(F.data == "goal:completed:list")
async def completed_goals_list_callback(callback: CallbackQuery):
    goals = await rq.get_completed_goals(callback.from_user.id)

    if not callback.message:
        await callback.answer()
        return

    if not goals:
        text = "🏆 Завершённых целей пока нет."
    else:
        text = "🏆 Твои завершённые финансовые цели:"

    await callback.message.edit_text(
        text,
        reply_markup=completed_goals_keyboard(goals),
    )

    await callback.answer()



@router.callback_query(F.data.startswith("goal:completed:view:"))
async def completed_goal_view_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[3])

    progress = await rq.get_goal_progress(
        callback.from_user.id,
        goal_id,
    )

    if not progress or progress["goal"].status != "completed":
        await callback.answer(
            "Завершённая цель не найдена.",
            show_alert=True,
        )
        return

    if callback.message:
        await callback.message.edit_text(
            format_goal_progress(progress),
            reply_markup=completed_goal_details_keyboard(),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("goal:view:"))
async def goal_view_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[2])
    progress = await rq.get_goal_progress(callback.from_user.id, goal_id)
    if not progress:
        await callback.answer("Цель не найдена.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            format_goal_progress(progress),
            reply_markup=goal_details_keyboard(goal_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("goal:edit:"))
async def goal_edit_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[2])
    progress = await rq.get_goal_progress(callback.from_user.id, goal_id)
    if not progress:
        await callback.answer("Цель не найдена.", show_alert=True)
        return
    
    if progress["goal"].status != "active":
        await callback.answer(
            "Завершённую цель нельзя редактировать.",
            show_alert=True,
        )
        return
    
    if callback.message:
        await callback.message.edit_text(
            f"✏️ Что хочешь изменить в цели «{progress['goal'].name}»?",
            reply_markup=goal_edit_keyboard(goal_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("goal:editfield:"))
async def goal_edit_field_callback(callback: CallbackQuery, state: FSMContext):
    _, _, goal_id, field = callback.data.split(":")
    goal_id = int(goal_id)
    progress = await rq.get_goal_progress(
        callback.from_user.id,
        goal_id,
    )

    if not progress or progress["goal"].status != "active":
        await callback.answer(
            "Эту цель нельзя редактировать.",
            show_alert=True,
        )
        return
    if field not in {"name", "amount", "deadline"}:
        await callback.answer("Неизвестное поле.", show_alert=True)
        return
    await state.clear()
    await state.update_data(edit_goal_id=goal_id, edit_goal_field=field)
    await state.set_state(PendingGoalEdit.waiting_value)
    prompts = {
        "name": "Введи новое название цели:",
        "amount": "Введи новую целевую сумму:",
        "deadline": "Введи новый дедлайн в формате ГГГГ-ММ-ДД или «нет»:",
    }
    if callback.message:
        await callback.message.answer(prompts[field])
    await callback.answer()


@router.message(PendingGoalEdit.waiting_value, F.text)
async def goal_edit_value_message(message: Message, state: FSMContext):
    data = await state.get_data()
    goal_id = data.get("edit_goal_id")
    field = data.get("edit_goal_field")
    raw = message.text.strip()
    try:
        if field == "name":
            if not raw or len(raw) > 100:
                raise ValueError("Название должно содержать от 1 до 100 символов.")
            value = raw
        elif field == "amount":
            value = to_decimal(raw)
            if value <= 0:
                raise ValueError("Сумма должна быть больше нуля.")
        else:
            value = None if raw.casefold() in {"нет", "none", "null", "-"} else datetime.fromisoformat(raw)
        updated = await rq.update_goal(
            message.from_user.id,
            goal_id,
            "target_amount" if field == "amount" else field,
            value,
        )
        if not updated:
            await message.answer("Цель не найдена.")
            await state.clear()
            return
        await state.clear()
        progress = await rq.get_goal_progress(message.from_user.id, goal_id)
        await message.answer(
            "✅ Цель обновлена.\n\n" + format_goal_progress(progress),
            reply_markup=goal_details_keyboard(goal_id),
        )
    except (ValueError, InvalidOperation) as error:
        await message.answer(f"❌ {error}\nПопробуй ещё раз.")
    except Exception:
        logger.exception("Ошибка редактирования цели")
        await state.clear()
        await message.answer("❌ Не удалось изменить цель.")


@router.callback_query(F.data.startswith("goal:complete:ask:"))
async def goal_complete_ask_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[3])

    progress = await rq.get_goal_progress(
        callback.from_user.id,
        goal_id,
    )

    if not progress or progress["goal"].status != "active":
        await callback.answer(
            "Цель уже завершена или не найдена.",
            show_alert=True,
        )
        return
    
    if callback.message:
        await callback.message.edit_text(
            "Отметить эту цель выполненной?",
            reply_markup=goal_complete_keyboard(goal_id),
        )
    await callback.answer()




@router.callback_query(F.data.startswith("goal:delete:ask:"))
async def goal_delete_ask_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[3])

    progress = await rq.get_goal_progress(
        callback.from_user.id,
        goal_id,
    )

    if not progress or progress["goal"].status != "active":
        await callback.answer(
            "Цель уже завершена или не найдена.",
            show_alert=True,
        )
        return

    if callback.message:
        await callback.message.edit_text(
            "⚠️ Ты действительно хочешь удалить эту цель?",
            reply_markup=goal_delete_keyboard(goal_id),
        )
    await callback.answer()



@router.callback_query(F.data.startswith("goal:complete:confirm:"))
async def goal_complete_confirm_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[3])

    result = await rq.complete_goal(
        callback.from_user.id,
        goal_id,
    )

    if not result:
        await callback.answer(
            "Цель не найдена или уже не активна.",
            show_alert=True,
        )
        return

    if callback.message:
        await callback.message.edit_text(
            "✅ Цель отмечена выполненной."
        )

    await callback.answer("Готово!")


@router.callback_query(F.data.startswith("goal:delete:confirm:"))
async def goal_delete_confirm_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[3])

    deleted = await rq.delete_goal(
        callback.from_user.id,
        goal_id,
    )

    if not deleted:
        await callback.answer(
            "Цель не найдена или уже не активна.",
            show_alert=True,
        )
        return

    if callback.message:
        await callback.message.edit_text(
            "🗑 Цель удалена."
        )

    await callback.answer("Удалено!")



@router.callback_query(F.data.startswith("goal:delete:cancel:"))
async def goal_delete_cancel_callback(callback: CallbackQuery):
    goal_id = int(callback.data.split(":")[3])

    progress = await rq.get_goal_progress(
        callback.from_user.id,
        goal_id,
    )

    if not progress:
        await callback.answer(
            "Цель не найдена.",
            show_alert=True,
        )
        return

    if callback.message:
        if progress["goal"].status == "completed":
            await callback.message.edit_text(
                format_goal_progress(progress),
                reply_markup=completed_goal_details_keyboard(),
            )

        elif progress["goal"].status == "active":
            await callback.message.edit_text(
                format_goal_progress(progress),
                reply_markup=goal_details_keyboard(goal_id),
            )

        else:
            await callback.message.edit_text(
                "Эта цель больше недоступна.",
                reply_markup=goals_list_keyboard(
                    await rq.get_goals(callback.from_user.id)
                ),
            )

    await callback.answer("Отменено.")


# ---------------------------------------------------------------------
# TRANSACTION HISTORY
# ---------------------------------------------------------------------

async def send_history(message: Message, tg_id: int):
    transactions = await rq.get_transactions(tg_id, limit=10)
    categories = await rq.get_categories(tg_id)
    category_map = {category.id: category.name for category in categories}
    if not transactions:
        await message.answer("История операций пока пуста.")
        return

    lines = []
    for transaction in transactions:
        lines.append(format_transaction({
            "type": transaction.type,
            "amount": transaction.amount,
            "currency": getattr(transaction, "currency", "CNY"),
            "description": transaction.description,
            "category": category_map.get(transaction.category_id, "Другое"),
        }))
    await message.answer(
        "📜 Последние операции:\n\n" + "\n\n".join(lines),
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("history"))
async def history_command(message: Message):
    await send_history(message, message.from_user.id)


@router.callback_query(F.data == "menu:history")
async def history_callback(callback: CallbackQuery):
    await callback.answer()
    if callback.message:
        await send_history(callback.message, callback.from_user.id)


# ---------------------------------------------------------------------
# MONTHLY BUDGETS
# ---------------------------------------------------------------------

@router.callback_query(F.data == "budget:list")
async def budget_list_callback(callback: CallbackQuery):
    budgets = await rq.get_budgets(callback.from_user.id)
    progress_items = []
    for budget in budgets:
        progress = await rq.get_budget_progress(callback.from_user.id, budget.id)
        if progress:
            progress_items.append(progress)

    if not callback.message:
        await callback.answer()
        return

    if not progress_items:
        await callback.message.edit_text(
            "💳 Месячных бюджетов пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Создать бюджет", callback_data="budget:create")
            ]]),
        )
    else:
        lines = ["💳 ТВОИ МЕСЯЧНЫЕ БЮДЖЕТЫ\n"]
        for item in progress_items:
            lines.append(
                f"📁 {item['category']} ({item['currency']})\n"
                f"💰 {format_money(item['spent'], item['currency'])} / "
                f"{format_money(item['limit'], item['currency'])}\n"
                f"📊 Использовано: {item['percentage']:.1f}%"
            )
        await callback.message.edit_text(
            "\n\n".join(lines),
            reply_markup=budgets_list_keyboard(progress_items),
        )
    await callback.answer()


@router.callback_query(F.data == "budget:create")
async def budget_create_callback(callback: CallbackQuery, state: FSMContext):
    categories = await rq.get_categories(callback.from_user.id, category_type="expense")
    if not categories:
        await callback.answer("Сначала создай расходную категорию.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "📁 Выбери категорию для нового бюджета:",
            reply_markup=budget_categories_keyboard(categories),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("budget:category:"))
async def budget_category_callback(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":")[2])
    categories = await rq.get_categories(callback.from_user.id, category_type="expense")
    category = next((item for item in categories if item.id == category_id), None)
    if category is None:
        await callback.answer("Категория не найдена.", show_alert=True)
        return
    await state.clear()
    await state.update_data(budget_category_id=category_id)
    if callback.message:
        await callback.message.edit_text(
            f"📁 Категория: {category.name}\n\nВыбери валюту бюджета:",
            reply_markup=currency_keyboard("budgetcurrency"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("budgetcurrency:"))
async def budget_currency_callback(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split(":")[1].upper()
    if currency not in CURRENCIES:
        await callback.answer("Неизвестная валюта.", show_alert=True)
        return
    await state.update_data(budget_currency=currency)
    await state.set_state(BudgetStates.waiting_amount)
    if callback.message:
        await callback.message.edit_text(
            f"💱 Валюта: {currency}\n\nВведи месячный лимит, например 3000:"
        )
    await callback.answer()


@router.message(BudgetStates.waiting_amount, F.text)
async def budget_amount_message(message: Message, state: FSMContext):
    try:
        amount = to_decimal(message.text.strip())
        if amount <= 0:
            raise ValueError
    except (ValueError, InvalidOperation):
        await message.answer("❌ Введи корректную положительную сумму. Например: 3000")
        return

    data = await state.get_data()
    try:
        budget = await rq.create_budget(
            tg_id=message.from_user.id,
            category_id=data.get("budget_category_id"),
            amount=amount,
            currency=data.get("budget_currency", "CNY"),
        )
    except ValueError as error:
        await state.clear()
        await message.answer(f"❌ {error}", reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Ошибка создания бюджета")
        await state.clear()
        await message.answer("❌ Не удалось создать бюджет.")
        return

    await state.clear()
    progress = await rq.get_budget_progress(message.from_user.id, budget.id)
    await message.answer(
        "✅ Месячный бюджет создан!\n\n" + format_budget_progress(progress),
        reply_markup=budget_details_keyboard(budget.id),
    )


@router.callback_query(F.data.startswith("budget:view:"))
async def budget_view_callback(callback: CallbackQuery):
    budget_id = int(callback.data.split(":")[2])
    progress = await rq.get_budget_progress(callback.from_user.id, budget_id)
    if not progress:
        await callback.answer("Бюджет не найден.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            format_budget_progress(progress),
            reply_markup=budget_details_keyboard(budget_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("budget:edit:"))
async def budget_edit_callback(callback: CallbackQuery, state: FSMContext):
    budget_id = int(callback.data.split(":")[2])
    progress = await rq.get_budget_progress(callback.from_user.id, budget_id)
    if not progress:
        await callback.answer("Бюджет не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(editing_budget_id=budget_id)
    await state.set_state(BudgetStates.waiting_edit_amount)
    if callback.message:
        await callback.message.answer(
            f"✏️ Новый лимит для «{progress['category']}» "
            f"({progress['currency']}).\n"
            f"Текущий: {format_money(progress['limit'], progress['currency'])}\n\n"
            "Введи новый лимит:"
        )
    await callback.answer()


@router.message(BudgetStates.waiting_edit_amount, F.text)
async def budget_edit_amount_message(message: Message, state: FSMContext):
    try:
        amount = to_decimal(message.text.strip())
        if amount <= 0:
            raise ValueError
    except (ValueError, InvalidOperation):
        await message.answer("❌ Введи корректную положительную сумму.")
        return

    data = await state.get_data()
    budget_id = data.get("editing_budget_id")
    try:
        updated = await rq.update_budget(message.from_user.id, budget_id, amount)
        if not updated:
            await state.clear()
            await message.answer("Бюджет не найден.")
            return
        await state.clear()
        progress = await rq.get_budget_progress(message.from_user.id, budget_id)
        await message.answer(
            "✅ Лимит изменён.\n\n" + format_budget_progress(progress),
            reply_markup=budget_details_keyboard(budget_id),
        )
    except Exception:
        logger.exception("Ошибка изменения бюджета")
        await state.clear()
        await message.answer("❌ Не удалось изменить бюджет.")


@router.callback_query(F.data.startswith("budget:delete:ask:"))
async def budget_delete_ask_callback(callback: CallbackQuery):
    budget_id = int(callback.data.split(":")[3])
    if callback.message:
        await callback.message.edit_text(
            "⚠️ Действительно удалить этот бюджет?",
            reply_markup=budget_delete_keyboard(budget_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("budget:delete:confirm:"))
async def budget_delete_confirm_callback(callback: CallbackQuery):
    budget_id = int(callback.data.split(":")[3])
    deleted = await rq.delete_budget(callback.from_user.id, budget_id)
    if not deleted:
        await callback.answer("Бюджет не найден.", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("🗑 Бюджет удалён.")
    await callback.answer("Удалено!")


@router.callback_query(F.data == "budget:cancel")
async def budget_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.edit_text("Создание бюджета отменено.")
    await callback.answer("Отменено!")



async def send_budget_limit_notifications(bot, tg_id: int):
    await send_monthly_budget_notifications(
        bot=bot,
        tg_id=tg_id,
    )

# ---------------------------------------------------------------------
# AI PARSER AND ATOMIC CONFIRMATION
# ---------------------------------------------------------------------

@router.message(PendingGoal.waiting_confirmation, F.text)
async def waiting_for_goal_confirmation(message: Message):
    await message.answer("Сначала подтверди или отмени действие с целью.")


@router.callback_query(PendingGoal.waiting_confirmation, F.data == "goal:confirm")
async def confirm_goal(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    action = data.get("pending_goal_action")
    if not action:
        await state.clear()
        await callback.answer("Данные цели не найдены.", show_alert=True)
        return
    try:
        if action["type"] == "create":
            deadline = datetime.fromisoformat(action["deadline"]) if action.get("deadline") else None
            goal = await rq.create_goal(
                tg_id=callback.from_user.id,
                name=action["name"],
                target_amount=to_decimal(action["amount"]),
                deadline=deadline,
                currency=action.get("currency", "CNY"),
            )
            goal_id = goal.id
            result_text = "✅ Финансовая цель создана!"
        elif action["type"] == "contribute":
            goal_id = action["goal_id"]
            await rq.add_goal_contribution(
                callback.from_user.id, goal_id, to_decimal(action["amount"])
            )
            result_text = "✅ Цель успешно пополнена!"
        else:
            raise ValueError("Неизвестное действие")
        progress = await rq.get_goal_progress(callback.from_user.id, goal_id)
        await state.clear()
        if callback.message:
            await callback.message.edit_text(result_text + "\n\n" + format_goal_progress(progress))
        await callback.answer("Сохранено!")
    except Exception:
        logger.exception("Ошибка сохранения цели")
        await callback.answer("Не удалось сохранить цель.", show_alert=True)


@router.callback_query(PendingGoal.waiting_confirmation, F.data == "goal:cancel")
async def cancel_goal(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.edit_text("❌ Действие с целью отменено.")
    await callback.answer("Отменено!")


@router.message(PendingTransactions.waiting_confirmation, F.text)
async def waiting_for_confirmation(message: Message):
    await message.answer("Сначала подтверди или отмени предыдущие операции. Используй /cancel для отмены.")


@router.message(F.text, F.text.startswith("/"))
async def unknown_command_handler(message: Message):
    await message.answer("Неизвестная команда. Используй меню бота.")


@router.message(F.text)
async def ai_message_handler(message: Message, state: FSMContext):
    if await state.get_state():
        await message.answer("Сначала заверши текущее действие или используй /cancel.")
        return

    user_text = message.text.strip()
    if not user_text:
        return

    status = await message.answer("🤖 Анализирую сообщение...")
    try:
        result = await parse_transactions(user_text)
    except Exception:
        logger.exception("Ошибка обработки финансового сообщения")
        await status.edit_text("❌ Не удалось обработать сообщение. Попробуй ещё раз.")
        return

    if result.needs_clarification:
        await status.edit_text(result.clarification_question or "Уточни детали операции.")
        return

    if not any([result.transactions, result.goal_creation, result.goal_contribution, result.goal_allocation]):
        await status.edit_text("Я не нашёл финансовых действий. Попробуй сформулировать иначе.")
        return

    tg_id = message.from_user.id
    categories = await rq.get_categories(tg_id)
    category_map = {(c.type, c.name.casefold()): c.id for c in categories}

    transactions = []

    for item in result.transactions:
        category_id = category_map.get(
            (item.type, item.category.casefold())
        )

        if category_id is None:
            await status.edit_text(
                f"Не удалось определить категорию «{item.category}» "
                f"для операции «{item.description}».\n\n"
                "Проверь категории и повтори сообщение."
            )
            return

        transactions.append({
            "type": item.type,
            "amount": str(item.amount),
            "description": item.description,
            "category_id": category_id,
            "category": item.category,
            "currency": item.currency,
        })

    pending_goal_creation = None
    if result.goal_creation:
        goal = result.goal_creation
        if not goal.name.strip() or goal.target_amount <= 0:
            await status.edit_text("Не удалось определить название или сумму цели.")
            return
        if goal.deadline:
            try:
                datetime.fromisoformat(goal.deadline)
            except ValueError:
                await status.edit_text("Укажи дату цели в формате ГГГГ-ММ-ДД.")
                return
        pending_goal_creation = {
            "name": goal.name.strip(),
            "amount": str(goal.target_amount),
            "deadline": goal.deadline,
            "currency": goal.currency,
        }

    goals = await rq.get_goals(tg_id)
    pending_contributions = []
    preview = []

    if result.goal_contribution:
        contribution = result.goal_contribution
        if contribution.amount <= 0:
            await status.edit_text("Сумма пополнения должна быть больше нуля.")
            return

        if pending_goal_creation and contribution.goal_name.casefold().strip() == pending_goal_creation["name"].casefold():
            target_currency = pending_goal_creation["currency"]
            pending_contributions.append({
                "goal_name": pending_goal_creation["name"],
                "amount": str(contribution.amount),
                "new_goal": True,
                "currency": target_currency,
            })
        else:
            matched = find_matching_goal(goals, contribution.goal_name)
            if not matched:
                names = "\n".join(f"• {g.name} ({g.currency})" for g in goals) or "Активных целей нет."
                await status.edit_text(f"Не удалось определить цель.\n\nДоступные цели:\n{names}")
                return
            pending_contributions.append({
                "goal_id": matched.id,
                "goal_name": matched.name,
                "amount": str(contribution.amount),
                "new_goal": False,
                "currency": matched.currency,
            })

    if result.goal_allocation:
        allocation = result.goal_allocation
        percentage = to_decimal(allocation.percentage)
        if percentage <= 0 or percentage > 100:
            await status.edit_text("Процент должен быть больше 0 и не больше 100.")
            return

        # Распределяем доходы отдельно по каждой валюте. Валюты никогда не складываются.
        income_by_currency = {}
        for item in result.transactions:
            if item.type == "income":
                income_by_currency[item.currency] = (
                    income_by_currency.get(item.currency, Decimal("0")) + to_decimal(item.amount)
                )

        if not income_by_currency:
            await status.edit_text("Не нашёл доход, от которого можно рассчитать процент.")
            return

        if pending_goal_creation and allocation.goal_name.casefold().strip() == pending_goal_creation["name"].casefold():
            target_goal_id = None
            target_name = pending_goal_creation["name"]
            target_currency = pending_goal_creation["currency"]
            is_new = True
        else:
            matched = find_matching_goal(goals, allocation.goal_name)
            if not matched:
                names = "\n".join(f"• {g.name} ({g.currency})" for g in goals) or "Активных целей нет."
                await status.edit_text(f"Не удалось определить цель для отчисления.\n\nДоступные цели:\n{names}")
                return
            target_goal_id = matched.id
            target_name = matched.name
            target_currency = matched.currency
            is_new = False

        eligible_income = income_by_currency.get(target_currency, Decimal("0"))
        if eligible_income <= 0:
            await status.edit_text(
                f"В сообщении нет дохода в валюте {target_currency}, "
                "поэтому нельзя рассчитать пополнение этой цели."
            )
            return

        allocation_amount = (eligible_income * percentage / Decimal("100")).quantize(Decimal("0.01"))
        pending_contributions.append({
            "goal_id": target_goal_id,
            "goal_name": target_name,
            "amount": str(allocation_amount),
            "new_goal": is_new,
            "currency": target_currency,
            "percentage": str(percentage),
            "total_income": str(eligible_income),
        })

        # Обычные доходы и расходы
    if transactions:
        preview.append("📊 ОПЕРАЦИИ")
        preview.extend(
            format_transaction(item)
            for item in transactions
        )

    # Создание новой цели
    if pending_goal_creation:
        goal_currency = pending_goal_creation["currency"]

        preview.append(
            f"🎯 СОЗДАНИЕ ЦЕЛИ\n"
            f"Название: {pending_goal_creation['name']}\n"
            f"Сумма: {format_money(pending_goal_creation['amount'], goal_currency)}"
            + (
                f"\n📅 Срок: {pending_goal_creation['deadline']}"
                if pending_goal_creation["deadline"]
                else ""
            )
        )

    # Пополнение целей
    if pending_contributions:
        contribution_lines = ["🎯 ПОПОЛНЕНИЕ ЦЕЛЕЙ"]

        for item in pending_contributions:
            contribution_lines.append(
                f"🎯 {item['goal_name']}\n"
                f"➕ Пополнение: {format_money(item['amount'], item['currency'])}"
            )

            if "percentage" in item:
                contribution_lines.append(
                    f"📊 Расчёт: "
                    f"{format_money(item['total_income'], item['currency'])}"
                    f" × {item['percentage']}% = "
                    f"{format_money(item['amount'], item['currency'])}"
                )

        preview.append("\n\n".join(contribution_lines))

    await state.update_data(
        pending_transactions=transactions,
        pending_goal_creation=pending_goal_creation,
        pending_contributions=pending_contributions,
        pending_confirmation_id=uuid.uuid4().hex,
    )
    await state.set_state(PendingTransactions.waiting_confirmation)
    await status.edit_text(
        "🤖 Я распознал следующие действия:\n\n"
        + "\n\n".join(preview)
        + "\n\nВсё правильно? Сохранить?",
        reply_markup=transaction_confirmation_keyboard(),
    )


@router.callback_query(PendingTransactions.waiting_confirmation, F.data == "ai:confirm")
async def confirm_transactions(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    transactions = data.get("pending_transactions", [])
    goal_creation = data.get("pending_goal_creation")
    contributions = data.get("pending_contributions", [])

    if not transactions and not goal_creation and not contributions:
        await state.clear()
        await callback.answer("Действия не найдены.", show_alert=True)
        return

    try:
        goal_payload = dict(goal_creation) if goal_creation else None
        if goal_payload and goal_payload.get("deadline"):
            goal_payload["deadline"] = datetime.fromisoformat(goal_payload["deadline"])

        confirmation_id = data.get("pending_confirmation_id")

        if not confirmation_id:
            await state.clear()
            await callback.answer(
                "Не найден ID подтверждения. Попробуй повторить операцию.",
                show_alert=True,
            )
            return

        saved = await rq.save_financial_actions(
            tg_id=callback.from_user.id,
            transactions=transactions,
            goal_creation=goal_payload,
            contributions=contributions,
            confirmation_id=confirmation_id,
        )

        if not saved:
            await state.clear()
            await callback.answer(
                "Эти действия уже были сохранены.",
                show_alert=True,
            )
            return
        
        await state.clear()

    except Exception:
        logger.exception("Ошибка атомарного сохранения финансовых действий")
        await callback.answer(
            "Не удалось сохранить действия. Ничего не сохранено.",
            show_alert=True,
        )
        return

    if any(item["type"] == "expense" for item in transactions):
        try:
            await send_budget_limit_notifications(
                callback.bot,
                callback.from_user.id,
            )
        except Exception:
            logger.exception(
                "Финансовые действия сохранены, но возникла ошибка "
                "при проверке бюджетных уведомлений. Пользователь: %s",
                callback.from_user.id,
            )
    lines = ["✅ Все действия успешно сохранены!"]
    if transactions:
        lines.append("\n📊 Операции:")
        lines.extend(format_transaction(item) for item in transactions)
    if goal_creation:
        lines.append(f"\n🎯 Создана цель: {goal_creation['name']}")
    if contributions:
        lines.append("\n🎯 Пополнения целей:")
        for item in contributions:
            lines.append(
                f"➕ {item['goal_name']}: "
                f"{format_money(item['amount'], item.get('currency', 'CNY'))}"
            )

    if callback.message:
        await callback.message.edit_text("\n".join(lines))
    await callback.answer("Сохранено!")


@router.callback_query(PendingTransactions.waiting_confirmation, F.data == "ai:cancel")
async def cancel_transactions(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.edit_text("❌ Все действия отменены. Ничего не сохранено.")
    await callback.answer("Отменено!")


@router.message()
async def unknown_message(message: Message):
    await message.answer(
        "Я понимаю текстовые сообщения. Напиши о доходе, расходе или финансовой цели."
    )
