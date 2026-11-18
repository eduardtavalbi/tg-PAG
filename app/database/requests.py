from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from typing import Any

from sqlalchemy import (
    case,
    func,
    select,
)

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from sqlalchemy.exc import IntegrityError

from app.database.models import (
    async_session,
    User,
    ProcessedConfirmation,
    Category,
    Transaction,
    Goal,
    GoalContribution,
    BudgetWarningNotification,
    Budget,
    BudgetLimitNotification,
)



# =========================================================
# COMMON VALIDATION / HELPERS
# =========================================================

SUPPORTED_TRANSACTION_TYPES = {"income", "expense"}
BUDGET_THRESHOLDS = (70, 90, 100)
CURRENCY_LENGTH = 3
MONEY_QUANTUM = Decimal("0.01")
CURRENCIES = {
    "CNY": "¥",
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
}


def _decimal(value: Any, field_name: str = "Сумма") -> Decimal:
    """Безопасно преобразует число в Decimal и отклоняет NaN/Infinity."""
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field_name}: некорректное число") from exc

    if not result.is_finite():
        raise ValueError(f"{field_name}: значение должно быть конечным числом")

    return result


def _positive_amount(value: Any, field_name: str = "Сумма") -> Decimal:
    amount = _decimal(value, field_name)

    if amount <= 0:
        raise ValueError(f"{field_name} должна быть больше нуля")

    return amount.quantize(MONEY_QUANTUM)


def _currency(value: str | None = "CNY") -> str:
    currency = (value or "CNY").strip().upper()

    if currency not in CURRENCIES:
        raise ValueError("Неподдерживаемая валюта")

    return currency

def _description(value: str, max_length: int = 100) -> str:
    result = str(value).strip()

    if not result or len(result) > max_length:
        raise ValueError(
            f"Текст должен содержать от 1 до {max_length} символов"
        )

    return result


def _next_month_start(now: datetime) -> datetime:
    if now.month == 12:
        return datetime(now.year + 1, 1, 1)

    return datetime(now.year, now.month + 1, 1)


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1)


async def _get_user_id(session, tg_id: int) -> int:
    user_id = await session.scalar(
        select(User.id).where(User.tg_id == tg_id)
    )

    if user_id is None:
        raise ValueError("Пользователь не найден")

    return user_id


async def _get_owned_goal(
    session,
    user_id: int,
    goal_id: int,
    active_only: bool = True,
):
    query = select(Goal).where(
        Goal.id == goal_id,
        Goal.user_id == user_id,
    )

    if active_only:
        query = query.where(Goal.status == "active")

    return await session.scalar(query)


# =========================================================
# USERS
# =========================================================

async def set_user(tg_id: int) -> User:
    async with async_session() as session:
        user = await session.scalar(
            select(User).where(User.tg_id == tg_id)
        )

        if user is not None:
            return user

        user = User(tg_id=tg_id)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def get_user(tg_id: int):
    async with async_session() as session:
        return await session.scalar(
            select(User).where(User.tg_id == tg_id)
        )


# =========================================================
# CATEGORIES
# =========================================================

DEFAULT_CATEGORIES = {
    "expense": [
        "Еда",
        "Транспорт",
        "Покупки",
        "Развлечения",
        "Жильё",
        "Здоровье",
        "Образование",
        "Подписки",
        "Другое",
    ],
    "income": [
        "Зарплата",
        "Подработка",
        "Подарки",
        "Инвестиции",
        "Другое",
    ],
}


async def create_default_categories(tg_id: int):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        existing = set(
            (
                await session.execute(
                    select(Category.name, Category.type).where(
                        Category.user_id == user_id
                    )
                )
            ).all()
        )

        for category_type, names in DEFAULT_CATEGORIES.items():
            for name in names:
                if (name, category_type) not in existing:
                    session.add(
                        Category(
                            name=name,
                            type=category_type,
                            user_id=user_id,
                        )
                    )

        await session.commit()


async def get_categories(
    tg_id: int,
    category_type: str | None = None,
):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        query = select(Category).where(Category.user_id == user_id)

        if category_type is not None:
            if category_type not in SUPPORTED_TRANSACTION_TYPES:
                raise ValueError("Некорректный тип категории")
            query = query.where(Category.type == category_type)

        result = await session.scalars(query.order_by(Category.name))
        return list(result.all())


async def add_category(
    tg_id: int,
    name: str,
    category_type: str,
):
    if category_type not in SUPPORTED_TRANSACTION_TYPES:
        raise ValueError("Некорректный тип категории")

    name = _description(name, 50)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        category = Category(
            name=name,
            type=category_type,
            user_id=user_id,
        )
        session.add(category)

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("Такая категория уже существует") from exc

        await session.refresh(category)
        return category


# =========================================================
# TRANSACTIONS
# =========================================================

async def _validate_category(
    session,
    user_id: int,
    category_id: int | None,
    transaction_type: str,
):
    if category_id is None:
        return

    category = await session.scalar(
        select(Category).where(
            Category.id == category_id,
            Category.user_id == user_id,
            Category.type == transaction_type,
        )
    )

    if category is None:
        raise ValueError("Категория не найдена или не принадлежит пользователю")


async def add_transaction(
    tg_id: int,
    transaction_type: str,
    amount: Decimal | int | float,
    description: str,
    category_id: int | None = None,
    currency: str = "CNY",
):
    if transaction_type not in SUPPORTED_TRANSACTION_TYPES:
        raise ValueError("Тип операции должен быть income или expense")

    amount = _positive_amount(amount)
    description = _description(description)
    currency = _currency(currency)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)
        await _validate_category(
            session, user_id, category_id, transaction_type
        )

        transaction = Transaction(
            type=transaction_type,
            amount=amount,
            description=description,
            category_id=category_id,
            currency=currency,
            user_id=user_id,
        )

        session.add(transaction)
        await session.commit()
        await session.refresh(transaction)
        return transaction


async def add_transactions_batch(
    tg_id: int,
    transactions: list[dict],
):
    """Сохраняет список операций одним commit или не сохраняет ничего."""
    async with async_session() as session:
        try:
            user_id = await _get_user_id(session, tg_id)
            objects = []

            for item in transactions:
                transaction_type = item.get("type")

                if transaction_type not in SUPPORTED_TRANSACTION_TYPES:
                    raise ValueError("Некорректный тип операции")

                amount = _positive_amount(item.get("amount"))
                description = _description(item.get("description", ""))
                currency = _currency(item.get("currency", "CNY"))
                category_id = item.get("category_id")

                await _validate_category(
                    session, user_id, category_id, transaction_type
                )

                objects.append(
                    Transaction(
                        type=transaction_type,
                        amount=amount,
                        description=description,
                        currency=currency,
                        category_id=category_id,
                        user_id=user_id,
                    )
                )

            session.add_all(objects)
            await session.commit()
            return objects

        except Exception:
            await session.rollback()
            raise


async def get_transactions(
    tg_id: int,
    limit: int = 20,
    transaction_type: str | None = None,
):
    if limit < 1:
        raise ValueError("Лимит должен быть положительным")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        query = select(Transaction).where(
            Transaction.user_id == user_id
        )

        if transaction_type is not None:
            if transaction_type not in SUPPORTED_TRANSACTION_TYPES:
                raise ValueError("Некорректный тип операции")
            query = query.where(Transaction.type == transaction_type)

        result = await session.scalars(
            query.order_by(
                Transaction.created_at.desc(),
                Transaction.id.desc(),
            ).limit(limit)
        )
        return list(result.all())


async def delete_transaction(tg_id: int, transaction_id: int) -> bool:
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        transaction = await session.scalar(
            select(Transaction).where(
                Transaction.id == transaction_id,
                Transaction.user_id == user_id,
            )
        )

        if transaction is None:
            return False

        await session.delete(transaction)
        await session.commit()
        return True


# =========================================================
# BALANCE AND STATISTICS
# =========================================================

async def get_balance(tg_id: int, currency: str = "CNY") -> Decimal:
    currency = _currency(currency)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        rows = await session.execute(
            select(
                Transaction.type,
                func.coalesce(func.sum(Transaction.amount), 0),
            )
            .where(
                Transaction.user_id == user_id,
                Transaction.currency == currency,
            )
            .group_by(Transaction.type)
        )

        totals = {
            transaction_type: Decimal(str(total or 0))
            for transaction_type, total in rows.all()
        }

        income = totals.get("income", Decimal("0"))
        expense = totals.get("expense", Decimal("0"))
        return income - expense


async def get_balances_by_currency(tg_id: int) -> dict[str, Decimal]:
    """Возвращает баланс отдельно по каждой валюте, не смешивая курсы."""
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        rows = await session.execute(
            select(
                Transaction.currency,
                Transaction.type,
                func.coalesce(func.sum(Transaction.amount), 0),
            )
            .where(Transaction.user_id == user_id)
            .group_by(Transaction.currency, Transaction.type)
        )

        totals: dict[str, dict[str, Decimal]] = {}

        for currency, transaction_type, total in rows.all():
            totals.setdefault(
                currency,
                {"income": Decimal("0"), "expense": Decimal("0")},
            )
            totals[currency][transaction_type] = Decimal(str(total or 0))

        return {
            currency: values["income"] - values["expense"]
            for currency, values in totals.items()
        }


async def get_statistics(
    tg_id: int,
    start_date: datetime,
    end_date: datetime,
    currency: str = "CNY",
):
    currency = _currency(currency)

    if end_date <= start_date:
        raise ValueError("Дата окончания должна быть позже даты начала")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        conditions = (
            Transaction.user_id == user_id,
            Transaction.created_at >= start_date,
            Transaction.created_at < end_date,
            Transaction.currency == currency,
        )

        totals_result = await session.execute(
            select(
                Transaction.type,
                func.coalesce(func.sum(Transaction.amount), 0),
            )
            .where(*conditions)
            .group_by(Transaction.type)
        )

        totals = {
            transaction_type: Decimal(str(total or 0))
            for transaction_type, total in totals_result.all()
        }

        income = totals.get("income", Decimal("0"))
        expense = totals.get("expense", Decimal("0"))

        category_rows = await session.execute(
            select(
                Transaction.type,
                Category.id,
                func.coalesce(Category.name, "Без категории"),
                func.sum(Transaction.amount),
            )
            .outerjoin(
                Category,
                (Transaction.category_id == Category.id)
                & (Category.user_id == Transaction.user_id),
            )
            .where(*conditions)
            .group_by(
                Transaction.type,
                Category.id,
                Category.name,
            )
            .order_by(
                Transaction.type,
                Category.name,
            )
        )
        income_categories = []
        expense_categories = []

        for transaction_type, category_id, category_name, amount in category_rows.all():
            item = {
                "name": category_name,
                "amount": Decimal(str(amount or 0)),
            }

            if transaction_type == "income":
                income_categories.append(item)
            else:
                expense_categories.append(item)

        return {
            "currency": currency,
            "income": income,
            "expense": expense,
            "balance": income - expense,
            "income_categories": income_categories,
            "expense_categories": expense_categories,
        }


async def get_monthly_statistics(
    tg_id: int,
    year: int | None = None,
    month: int | None = None,
    currency: str = "CNY",
):
    now = datetime.now()
    year = now.year if year is None else year
    month = now.month if month is None else month

    if not 1 <= month <= 12:
        raise ValueError("Месяц должен быть от 1 до 12")

    start_date = datetime(year, month, 1)
    end_date = (
        datetime(year + 1, 1, 1)
        if month == 12
        else datetime(year, month + 1, 1)
    )

    return await get_statistics(
        tg_id,
        start_date,
        end_date,
        currency,
    )


# =========================================================
# GOALS
# =========================================================

async def create_goal(
    tg_id: int,
    name: str,
    target_amount: Decimal | int | float,
    deadline: datetime | None = None,
    currency: str = "CNY",
):
    name = _description(name)
    target_amount = _positive_amount(target_amount, "Целевая сумма")
    currency = _currency(currency)

    if deadline is not None and not isinstance(deadline, datetime):
        raise ValueError("Некорректная дата завершения цели")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        goal = Goal(
            name=name,
            target_amount=target_amount,
            deadline=deadline,
            currency=currency,
            user_id=user_id,
        )

        session.add(goal)
        await session.commit()
        await session.refresh(goal)
        return goal


async def get_goals(tg_id: int):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        result = await session.scalars(
            select(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.status == "active",
            )
            .order_by(Goal.created_at.desc())
        )
        return list(result.all())


async def get_completed_goals(tg_id: int):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        result = await session.scalars(
            select(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.status == "completed",
            )
            .order_by(Goal.created_at.desc())
        )
        return list(result.all())



async def add_goal_contribution(
    tg_id: int,
    goal_id: int,
    amount: Decimal | int | float,
):
    amount = _positive_amount(amount, "Сумма пополнения")

    async with async_session() as session:
        try:
            user_id = await _get_user_id(session, tg_id)

            goal = await _get_owned_goal(
                session,
                user_id,
                goal_id,
            )

            if goal is None:
                raise ValueError("Активная цель не найдена")

            contribution = GoalContribution(
                amount=amount,
                goal_id=goal.id,
                user_id=user_id,
            )

            session.add(contribution)
            await session.flush()

            total = await session.scalar(
                select(
                    func.coalesce(
                        func.sum(GoalContribution.amount),
                        0,
                    )
                ).where(
                    GoalContribution.goal_id == goal.id,
                    GoalContribution.user_id == user_id,
                )
            )

            if Decimal(str(total or 0)) >= Decimal(
                str(goal.target_amount)
            ):
                goal.status = "completed"

            await session.commit()
            await session.refresh(contribution)

            return contribution

        except Exception:
            await session.rollback()
            raise


async def get_goal_progress(tg_id: int, goal_id: int):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        goal = await session.scalar(
            select(Goal).where(
                Goal.id == goal_id,
                Goal.user_id == user_id,
            )
        )

        if goal is None:
            return None

        total = await session.scalar(
            select(
                func.coalesce(
                    func.sum(GoalContribution.amount),
                    0,
                )
            ).where(
                GoalContribution.goal_id == goal.id,
                GoalContribution.user_id == user_id,
            )
        )

        saved = Decimal(str(total or 0))
        target = Decimal(str(goal.target_amount))

        progress = (
            min(100, float(saved / target * 100))
            if target > 0
            else 0
        )

        return {
            "goal": goal,
            "saved": saved,
            "target": target,
            "remaining": max(
                Decimal("0"),
                target - saved,
            ),
            "progress": progress,
        }

async def get_goal_contributions(
    tg_id: int,
    goal_id: int,
    limit: int = 10,
):
    if limit < 1:
        raise ValueError("Лимит должен быть положительным")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        goal_exists = await session.scalar(
            select(Goal.id).where(
                Goal.id == goal_id,
                Goal.user_id == user_id,
            )
        )

        if goal_exists is None:
            return []

        result = await session.scalars(
            select(GoalContribution)
            .where(
                GoalContribution.goal_id == goal_id,
                GoalContribution.user_id == user_id,
            )
            .order_by(
                GoalContribution.created_at.desc(),
                GoalContribution.id.desc(),
            )
            .limit(limit)
        )
        return list(result.all())



async def delete_goal(tg_id: int, goal_id: int) -> bool:
    """Мягкое удаление только активной цели."""
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)
        goal = await _get_owned_goal(session, user_id, goal_id)

        if goal is None:
            return False

        goal.status = "deleted"
        await session.commit()
        return True


async def update_goal(
    tg_id: int,
    goal_id: int,
    field: str,
    value,
) -> bool:
    async with async_session() as session:
        try:
            user_id = await _get_user_id(session, tg_id)

            goal = await _get_owned_goal(
                session,
                user_id,
                goal_id,
            )

            if goal is None:
                return False

            if field == "name":
                goal.name = _description(value)

            elif field == "target_amount":
                goal.target_amount = _positive_amount(
                    value,
                    "Целевая сумма",
                )

            elif field == "deadline":
                if value is not None and not isinstance(value, datetime):
                    raise ValueError("Некорректная дата")

                goal.deadline = value

            elif field == "currency":
                new_currency = _currency(value)

                contribution_count = await session.scalar(
                    select(func.count(GoalContribution.id)).where(
                        GoalContribution.goal_id == goal.id,
                        GoalContribution.user_id == user_id,
                    )
                )

                if contribution_count:
                    raise ValueError(
                        "Нельзя менять валюту цели после её первого пополнения"
                    )

                goal.currency = new_currency

            else:
                raise ValueError("Неизвестное поле")

            if field == "target_amount":
                total = await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(GoalContribution.amount),
                            0,
                        )
                    ).where(
                        GoalContribution.goal_id == goal.id,
                        GoalContribution.user_id == user_id,
                    )
                )

                if Decimal(str(total or 0)) >= goal.target_amount:
                    goal.status = "completed"
                else:
                    goal.status = "active"

            await session.commit()
            return True

        except Exception:
            await session.rollback()
            raise


async def complete_goal(tg_id: int, goal_id: int) -> bool:
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)
        goal = await _get_owned_goal(session, user_id, goal_id)

        if goal is None or goal.status != "active":
            return False

        goal.status = "completed"
        await session.commit()
        return True


# =========================================================
# ATOMIC FINANCIAL ACTIONS
# =========================================================

async def save_financial_actions(
    tg_id: int,
    transactions: list[dict],
    goal_creation: dict | None,
    contributions: list[dict],
    confirmation_id: str
):
    """
    Атомарно сохраняет транзакции, создание цели и её пополнения.
    При любой ошибке выполняется rollback всей операции.
    Валюта пополнения наследуется от валюты цели.
    """
    async with async_session() as session:
        try:
            user_id = await _get_user_id(session, tg_id)
            new_goal = None
            if not confirmation_id or len(confirmation_id) != 32:
                raise ValueError("Некорректный ID подтверждения")

            result = await session.execute(
                sqlite_insert(ProcessedConfirmation)
                .values(
                    user_id=user_id,
                    confirmation_id=confirmation_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "confirmation_id"]
                )
            )

            if result.rowcount == 0:
                await session.rollback()
                return False

            # 1. Создаём цель внутри той же транзакции БД.
            if goal_creation:
                name = _description(goal_creation.get("name", ""))
                target_amount = _positive_amount(
                    goal_creation.get("amount"),
                    "Целевая сумма",
                )
                currency = _currency(
                    goal_creation.get("currency", "CNY")
                )
                deadline = goal_creation.get("deadline")

                if deadline is not None and not isinstance(deadline, datetime):
                    raise ValueError("Некорректная дата цели")

                new_goal = Goal(
                    name=name,
                    target_amount=target_amount,
                    deadline=deadline,
                    currency=currency,
                    user_id=user_id,
                )
                session.add(new_goal)
                await session.flush()

            # 2. Проверяем и добавляем транзакции.
            transaction_objects = []

            for item in transactions:
                transaction_type = item.get("type")

                if transaction_type not in SUPPORTED_TRANSACTION_TYPES:
                    raise ValueError("Некорректный тип операции")

                amount = _positive_amount(item.get("amount"))
                description = _description(item.get("description", ""))
                currency = _currency(item.get("currency", "CNY"))
                category_id = item.get("category_id")

                await _validate_category(
                    session,
                    user_id,
                    category_id,
                    transaction_type,
                )

                transaction_objects.append(
                    Transaction(
                        type=transaction_type,
                        amount=amount,
                        description=description,
                        currency=currency,
                        category_id=category_id,
                        user_id=user_id,
                    )
                )

            session.add_all(transaction_objects)

            
            # 3. Проверяем и добавляем пополнения.
            touched_goals: dict[int, Goal] = {}

            for item in contributions:
                amount = _positive_amount(
                    item.get("amount"),
                    "Сумма пополнения",
                )

                if item.get("new_goal"):
                    goal = new_goal

                    if goal is None:
                        raise ValueError(
                            "Новая цель для пополнения не найдена"
                        )
                else:
                    goal_id = item.get("goal_id")

                    if goal_id is None:
                        raise ValueError("Не указан ID цели")

                    goal = await _get_owned_goal(
                        session,
                        user_id,
                        int(goal_id),
                    )

                if goal is None or goal.status != "active":
                    raise ValueError(
                        "Цель не найдена или уже не активна"
                    )

                contribution_currency = item.get("currency")

                if contribution_currency is not None:
                    if _currency(contribution_currency) != goal.currency:
                        raise ValueError(
                            "Валюта пополнения должна совпадать с валютой цели"
                        )

                session.add(
                    GoalContribution(
                        amount=amount,
                        goal_id=goal.id,
                        user_id=user_id,
                    )
                )

                touched_goals[goal.id] = goal

            # 4. Проверяем прогресс целей до единственного commit.
            await session.flush()

            for goal_id, goal in touched_goals.items():
                total = await session.scalar(
                    select(func.coalesce(func.sum(GoalContribution.amount), 0))
                    .where(
                        GoalContribution.goal_id == goal_id,
                        GoalContribution.user_id == user_id,
                    )
                )

                if Decimal(str(total or 0)) >= Decimal(
                    str(goal.target_amount)
                ):
                    goal.status = "completed"

            await session.commit()
            return True

        except Exception:
            await session.rollback()
            raise


# =========================================================
# AI FINANCIAL ADVISOR DATA
# =========================================================

async def get_advisor_data(tg_id: int):
    """
    Собирает финансовые показатели за 30 дней, историю пополнений
    целей за 90 дней и прогнозы. Все суммы разделены по валютам.
    """
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)
        now = datetime.now()

        expense_comparison = await get_expense_comparison(
            session, user_id, now
        )
        expense_anomalies = await get_expense_anomalies(
            session, user_id, now
        )
        budget_warnings = await get_budget_warnings(
            session, user_id, now
        )

        start_date = now - timedelta(days=30)
        contribution_start = now - timedelta(days=90)

        transaction_result = await session.scalars(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.created_at >= start_date,
                Transaction.created_at <= now,
            ).order_by(Transaction.created_at.desc())
        )
        transactions = list(transaction_result.all())

        currencies: dict[str, dict[str, Decimal]] = {}

        for transaction in transactions:
            currency = transaction.currency
            data = currencies.setdefault(
                currency,
                {
                    "income": Decimal("0"),
                    "expense": Decimal("0"),
                },
            )
            amount = Decimal(str(transaction.amount))

            if transaction.type == "income":
                data["income"] += amount
            else:
                data["expense"] += amount

        for data in currencies.values():
            income = data["income"]
            expense = data["expense"]
            balance = income - expense

            data["balance"] = balance
            data["average_daily_income"] = (
                income / Decimal("30")
            ).quantize(MONEY_QUANTUM)
            data["average_daily_expense"] = (
                expense / Decimal("30")
            ).quantize(MONEY_QUANTUM)
            data["average_daily_balance"] = (
                balance / Decimal("30")
            ).quantize(MONEY_QUANTUM)
            data["savings_rate"] = (
                (balance / income * Decimal("100")).quantize(MONEY_QUANTUM)
                if income > 0
                else None
            )

        category_result = await session.execute(
            select(
                Category.id,
                func.coalesce(Category.name, "Без категории"),
                func.sum(Transaction.amount),
                Transaction.currency,
            )
            .select_from(Transaction)
            .outerjoin(
                Category,
                (Transaction.category_id == Category.id)
                & (Category.user_id == Transaction.user_id),
            )
            .where(
                Transaction.user_id == user_id,
                Transaction.type == "expense",
                Transaction.created_at >= start_date,
                Transaction.created_at <= now,
            )
            .group_by(
                Category.id,
                Category.name,
                Transaction.currency,
            )
            .order_by(func.sum(Transaction.amount).desc())
        )

        expenses_by_category = []

        for category_id, name, amount, currency in category_result.all():
            amount = Decimal(str(amount or 0))

            total_expense = currencies.get(
                currency, {}
            ).get("expense", Decimal("0"))

            percentage = (
                (amount / total_expense * Decimal("100")).quantize(MONEY_QUANTUM)
                if total_expense > 0
                else None
            )

            expenses_by_category.append({
                "category": name,
                "amount": amount,
                "currency": currency,
                "percentage": percentage,
            })

        goals_result = await session.scalars(
            select(Goal)
            .where(
                Goal.user_id == user_id,
                Goal.status == "active",
            )
            .order_by(Goal.created_at.desc())
        )
        goals = list(goals_result.all())

        goals_data = []

        for goal in goals:
            saved_result = await session.scalar(
                select(func.coalesce(func.sum(GoalContribution.amount), 0))
                .where(
                    GoalContribution.goal_id == goal.id,
                    GoalContribution.user_id == user_id,
                )
            )

            saved = Decimal(str(saved_result or 0))
            target = Decimal(str(goal.target_amount))
            remaining = max(Decimal("0"), target - saved)
            progress = (
                min(Decimal("100"), saved / target * Decimal("100"))
                if target > 0
                else Decimal("0")
            )

            contribution_result = await session.execute(
                select(
                    GoalContribution.amount,
                    GoalContribution.created_at,
                ).where(
                    GoalContribution.goal_id == goal.id,
                    GoalContribution.user_id == user_id,
                    GoalContribution.created_at >= contribution_start,
                    GoalContribution.created_at <= now,
                )
            )
            contributions = contribution_result.all()
            contribution_count = len(contributions)

            contribution_total = sum(
                (Decimal(str(amount)) for amount, _ in contributions),
                Decimal("0"),
            )

            observation_days = 0
            average_monthly_contribution = None
            forecast_days = None
            forecast_date = None

            if contributions:
                first_date = min(
                    created_at for _, created_at in contributions
                )
                observation_days = max(
                    0, (now.date() - first_date.date()).days
                )

            if (
                remaining > 0
                and contribution_count >= 2
                and contribution_total > 0
                and observation_days >= 14
            ):
                average_daily = (
                    contribution_total / Decimal(str(observation_days))
                )
                average_monthly_contribution = (
                    average_daily * Decimal("30.4375")
                ).quantize(MONEY_QUANTUM)

                if average_daily > 0:
                    forecast_days = int(
                        (remaining / average_daily).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    )
                    forecast_date = (
                        now + timedelta(days=forecast_days)
                    ).date().isoformat()

            if remaining == 0:
                forecast_status = "target_reached"
            elif contribution_count == 0:
                forecast_status = "no_contributions"
            elif observation_days < 14:
                forecast_status = "insufficient_history"
            elif contribution_count < 2:
                forecast_status = "insufficient_contributions"
            elif forecast_date is not None:
                forecast_status = "available"
            else:
                forecast_status = "insufficient_data"

            deadline_days_remaining = None
            required_monthly_contribution = None
            deadline_status = "not_set"

            if goal.deadline:
                deadline_days_remaining = (
                    goal.deadline.date() - now.date()
                ).days

                if remaining == 0:
                    deadline_status = "completed_by_amount"
                elif deadline_days_remaining < 0:
                    deadline_status = "overdue"
                elif deadline_days_remaining == 0:
                    deadline_status = "due_today"
                else:
                    deadline_status = "upcoming"
                    required_monthly_contribution = (
                        remaining
                        * Decimal("30.4375")
                        / Decimal(str(deadline_days_remaining))
                    ).quantize(
                        MONEY_QUANTUM,
                        rounding=ROUND_CEILING,
                    )

            currency_data = currencies.get(goal.currency, {})
            period_balance = currency_data.get(
                "balance", Decimal("0")
            )

            required_share = (
                (
                    required_monthly_contribution
                    / period_balance
                    * Decimal("100")
                ).quantize(MONEY_QUANTUM)
                if (
                    required_monthly_contribution is not None
                    and period_balance > 0
                )
                else None
            )

            goals_data.append({
                "name": goal.name,
                "target": target,
                "saved": saved,
                "remaining": remaining,
                "progress": progress,
                "currency": goal.currency,
                "deadline": (
                    goal.deadline.isoformat() if goal.deadline else None
                ),
                "deadline_days_remaining": deadline_days_remaining,
                "deadline_status": deadline_status,
                "contributions_last_90_days": contribution_total,
                "contribution_count_last_90_days": contribution_count,
                "observation_days": observation_days,
                "average_monthly_contribution": average_monthly_contribution,
                "forecast_days": forecast_days,
                "forecast_date": forecast_date,
                "forecast_status": forecast_status,
                "required_monthly_contribution": required_monthly_contribution,
                "period_balance": period_balance,
                "required_contribution_share_of_period_balance_pct": required_share,
            })

        return {
            "period_days": 30,
            "period_start": start_date.isoformat(),
            "period_end": now.isoformat(),
            "currencies": currencies,
            "expenses_by_category": expenses_by_category,
            "goals": goals_data,
            "expense_comparison": expense_comparison,
            "budget_warnings": budget_warnings,
            "expense_anomalies": expense_anomalies,
        }



async def get_expense_comparison(
    session,
    user_id: int,
    now: datetime,
):
    """Сравнивает последние 30 дней с предыдущими 30 днями."""
    current_start = now - timedelta(days=30)
    previous_start = now - timedelta(days=60)

    period_column = case(
        (Transaction.created_at >= current_start, "current"),
        else_="previous",
    ).label("period")

    result = await session.execute(
        select(
            period_column,
            Category.id,
            func.coalesce(Category.name, "Без категории"),
            Transaction.currency,
            func.sum(Transaction.amount),
        )
        .select_from(Transaction)
        .outerjoin(
            Category,
            (Transaction.category_id == Category.id)
            & (Category.user_id == Transaction.user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.created_at >= previous_start,
            Transaction.created_at <= now,
        )
        .group_by(
            period_column,
            Category.id,
            Category.name,
            Transaction.currency,
        )
    )

    comparison = {}

    for period, category_id, category, currency, amount in result.all():
        key = (category_id, currency)

        item = comparison.setdefault(
            key,
            {
                "category": category,
                "currency": currency,
                "current_amount": Decimal("0"),
                "previous_amount": Decimal("0"),
            },
        )

        if period == "current":
            item["current_amount"] = Decimal(str(amount or 0))
        else:
            item["previous_amount"] = Decimal(str(amount or 0))

    result_data = []

    for item in comparison.values():
        current = item["current_amount"]
        previous = item["previous_amount"]
        change_amount = current - previous

        change_pct = (
            (change_amount / previous * Decimal("100")).quantize(MONEY_QUANTUM)
            if previous > 0
            else None
        )

        if previous == 0 and current > 0:
            trend = "new"
        elif current == 0 and previous > 0:
            trend = "disappeared"
        elif change_amount > 0:
            trend = "increased"
        elif change_amount < 0:
            trend = "decreased"
        else:
            trend = "unchanged"

        result_data.append({
            **item,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "trend": trend,
        })

    result_data.sort(
        key=lambda item: abs(item["change_amount"]),
        reverse=True,
    )

    return result_data



async def get_expense_anomalies(
    session,
    user_id: int,
    now: datetime,
):
    """
    Ищет необычно крупные расходы за последние 30 дней.
    Сравнение выполняется по той же категории и валюте,
    используя медиану и MAD предыдущих 90 дней.
    """
    current_start = now - timedelta(days=30)
    history_start = now - timedelta(days=120)

    min_history_count = 5
    mad_multiplier = Decimal("3")
    mad_normalization = Decimal("1.4826")
    minimum_multiplier = Decimal("2")

    history_result = await session.execute(
        select(
            Category.id,
            func.coalesce(Category.name, "Без категории"),
            Transaction.currency,
            Transaction.amount,
        )
        .select_from(Transaction)
        .outerjoin(
            Category,
            (Transaction.category_id == Category.id)
            & (Category.user_id == Transaction.user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.created_at >= history_start,
            Transaction.created_at < current_start,
        )
    )

    historical_expenses = {}

    for category_id, category, currency, amount in history_result.all():
        historical_expenses.setdefault(
            (category_id, currency), []
        ).append(Decimal(str(amount)))

    current_result = await session.execute(
        select(
            Transaction.id,
            Transaction.description,
            Transaction.amount,
            Transaction.currency,
            Transaction.created_at,
            Category.id,
            func.coalesce(Category.name, "Без категории"),
        )
        .select_from(Transaction)
        .outerjoin(
            Category,
            (Transaction.category_id == Category.id)
            & (Category.user_id == Transaction.user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.created_at >= current_start,
            Transaction.created_at <= now,
        )
        .order_by(Transaction.created_at.desc())
    )

    current_expenses = current_result.all()

    def median(values: list[Decimal]) -> Decimal:
        ordered = sorted(values)
        count = len(ordered)
        middle = count // 2

        if count % 2:
            return ordered[middle]

        return (ordered[middle - 1] + ordered[middle]) / Decimal("2")

    anomalies = []
    insufficient_history_count = 0
    checked_count = 0

    for (
        transaction_id,
        description,
        amount,
        currency,
        created_at,
        category_id,
        category,
    ) in current_expenses:
        amount = Decimal(str(amount))

        historical = historical_expenses.get(
            (category_id, currency),
            [],
        )

        if len(historical) < min_history_count:
            insufficient_history_count += 1
            continue

        checked_count += 1
        median_amount = median(historical)

        if median_amount <= 0:
            continue

        deviations = [
            abs(value - median_amount)
            for value in historical
        ]
        mad = median(deviations)

        if mad == 0:
            threshold = median_amount * minimum_multiplier
        else:
            threshold = median_amount + (
                mad_multiplier * mad_normalization * mad
            )
            threshold = max(
                threshold,
                median_amount * minimum_multiplier,
            )

        if amount <= threshold:
            continue

        increase_pct = (
            (amount - median_amount)
            / median_amount
            * Decimal("100")
        ).quantize(MONEY_QUANTUM)

        anomalies.append({
            "transaction_id": transaction_id,
            "description": description,
            "category": category,
            "amount": amount,
            "currency": currency,
            "created_at": created_at.isoformat(),
            "historical_count": len(historical),
            "historical_median": median_amount,
            "historical_mad": mad,
            "anomaly_threshold": threshold,
            "increase_over_median_pct": increase_pct,
        })

    return {
        "anomalies": anomalies,
        "checked_count": checked_count,
        "insufficient_history_count": insufficient_history_count,
        "total_current_expenses": len(current_expenses),
    }


async def get_budget_warnings(
    session,
    user_id: int,
    now: datetime,
):
    """
    Сравнивает дневной темп расходов с начала текущего месяца
    со средним дневным расходом за 30 дней до начала месяца.
    Анализ ведётся отдельно по категории и валюте.
    """
    current_start = _month_start(now)
    history_start = current_start - timedelta(days=30)

    elapsed_days = (
        Decimal(str((now - current_start).total_seconds()))
        / Decimal("86400")
    )
    days_in_month = monthrange(now.year, now.month)[1]

    history_result = await session.execute(
        select(
            Category.id,
            func.coalesce(Category.name, "Без категории"),
            Transaction.currency,
            func.sum(Transaction.amount),
            func.count(Transaction.id),
        )
        .select_from(Transaction)
        .outerjoin(
            Category,
            (
                (Transaction.category_id == Category.id)
                & (Transaction.user_id == Category.user_id)
            ),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.created_at >= history_start,
            Transaction.created_at < current_start,
        )
        .group_by(
            Category.id,
            Category.name,
            Transaction.currency,
        )
    )

    historical = {
        (category_id, currency): {
            "category": category,
            "total": Decimal(str(total or 0)),
            "count": count,
        }
        for category_id, category, currency, total, count
        in history_result.all()
    }

    current_result = await session.execute(
        select(
            Category.id,
            func.coalesce(Category.name, "Без категории"),
            Transaction.currency,
            func.sum(Transaction.amount),
            func.count(Transaction.id),
        )
        .select_from(Transaction)
        .outerjoin(
            Category,
            (Transaction.category_id == Category.id)
            & (Transaction.user_id == Category.user_id),
        )   
        .where(
            Transaction.user_id == user_id,
            Transaction.type == "expense",
            Transaction.created_at >= current_start,
            Transaction.created_at <= now,
        )
        .group_by(
            Category.id,
            Category.name,
            Transaction.currency,
        )
    )

    current_expenses = current_result.all()

    if elapsed_days < Decimal("5"):
        return {
            "warnings": [],
            "checked_count": 0,
            "insufficient_history_count": len(current_expenses),
            "elapsed_days": float(elapsed_days),
            "days_in_month": days_in_month,
            "status": "insufficient_period",
        }

    warnings = []
    checked_count = 0
    insufficient_history_count = 0

    for category_id, category, currency, total, count in current_expenses:
        current_total = Decimal(str(total or 0))
        previous = historical.get((category_id, currency))

        if previous is None or previous["count"] < 3:
            insufficient_history_count += 1
            continue

        historical_total = previous["total"]

        if historical_total <= 0:
            insufficient_history_count += 1
            continue

        checked_count += 1

        historical_daily = historical_total / Decimal("30")
        current_daily = current_total / elapsed_days

        projected_monthly = current_daily * Decimal(days_in_month)
        historical_monthly = historical_daily * Decimal(days_in_month)

        growth_pct = (
            current_daily / historical_daily - Decimal("1")
        ) * Decimal("100")

        if growth_pct < Decimal("30"):
            continue

        severity = "high" if growth_pct >= Decimal("60") else "moderate"

        warnings.append({
            "category": category,
            "currency": currency,
            "current_spending": current_total,
            "historical_30d_spending": historical_total,
            "current_daily_average": current_daily,
            "historical_daily_average": historical_daily,
            "projected_monthly_spending": projected_monthly,
            "historical_monthly_baseline": historical_monthly,
            "growth_pct": growth_pct,
            "severity": severity,
            "historical_transaction_count": previous["count"],
        })

    warnings.sort(
        key=lambda item: item["growth_pct"],
        reverse=True,
    )

    return {
        "warnings": warnings,
        "checked_count": checked_count,
        "insufficient_history_count": insufficient_history_count,
        "elapsed_days": float(elapsed_days),
        "days_in_month": days_in_month,
        "status": "available",
    }


async def get_all_user_tg_ids():
    async with async_session() as session:
        result = await session.scalars(select(User.tg_id))
        return list(result.all())


async def get_budget_warning_severity(
    tg_id: int,
    category_name: str,
    currency: str,
    period_key: str,
):
    currency = _currency(currency)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        notification = await session.scalar(
            select(BudgetWarningNotification).where(
                BudgetWarningNotification.user_id == user_id,
                BudgetWarningNotification.category_name == category_name,
                BudgetWarningNotification.currency == currency,
                BudgetWarningNotification.period_key == period_key,
            )
        )
        return notification.severity if notification else None




async def save_budget_warning_notification(
    tg_id: int,
    category_name: str,
    currency: str,
    period_key: str,
    severity: str,
):
    currency = _currency(currency)

    if severity not in {"moderate", "high"}:
        raise ValueError("Некорректный уровень предупреждения")

    if not isinstance(category_name, str) or not category_name or len(category_name) > 50:
        raise ValueError("Некорректное название категории")

    if not isinstance(period_key, str):
        raise ValueError("Некорректный период")

    try:
        datetime.strptime(period_key, "%Y-%m")
    except ValueError:
        raise ValueError("Некорректный период")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        existing = await session.scalar(
            select(BudgetWarningNotification).where(
                BudgetWarningNotification.user_id == user_id,
                BudgetWarningNotification.category_name == category_name,
                BudgetWarningNotification.currency == currency,
                BudgetWarningNotification.period_key == period_key,
            )
        )

        previous_severity = existing.severity if existing else None
        previous_sent_at = existing.sent_at if existing else None

        stmt = sqlite_insert(
            BudgetWarningNotification
        ).values(
            user_id=user_id,
            category_name=category_name,
            currency=currency,
            period_key=period_key,
            severity=severity,
            sent_at=datetime.now(),
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "user_id",
                "category_name",
                "currency",
                "period_key",
            ],
            set_={
                "severity": stmt.excluded.severity,
                "sent_at": datetime.now(),
            },
            where=(
                (BudgetWarningNotification.severity == "moderate")
                & (stmt.excluded.severity == "high")
            ),
        ).returning(BudgetWarningNotification.id)

        result = await session.execute(stmt)
        reserved_id = result.scalar_one_or_none()

        await session.commit()

        if reserved_id is None:
            return None

        return previous_severity, previous_sent_at


async def rollback_budget_warning_notification(
    tg_id: int,
    category_name: str,
    currency: str,
    period_key: str,
    severity: str,
    previous_severity: str | None,
    previous_sent_at: datetime | None,
):
    currency = _currency(currency)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        notification = await session.scalar(
            select(BudgetWarningNotification).where(
                BudgetWarningNotification.user_id == user_id,
                BudgetWarningNotification.category_name == category_name,
                BudgetWarningNotification.currency == currency,
                BudgetWarningNotification.period_key == period_key,
                BudgetWarningNotification.severity == severity,
            )
        )

        if notification is None:
            return False

        if previous_severity is None:
            await session.delete(notification)
        else:
            notification.severity = previous_severity
            notification.sent_at = previous_sent_at

        await session.commit()

        return True


async def get_user_budget_warnings(
    tg_id: int,
    now: datetime | None = None,
):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        return await get_budget_warnings(
            session,
            user_id,
            now or datetime.now(),
        )


# =========================================================
# MONTHLY BUDGETS
# =========================================================

async def create_budget(
    tg_id: int,
    category_id: int,
    amount: Decimal | int | float,
    currency: str = "CNY",
):
    amount = _positive_amount(amount, "Лимит бюджета")
    currency = _currency(currency)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        category = await session.scalar(
            select(Category).where(
                Category.id == category_id,
                Category.user_id == user_id,
                Category.type == "expense",
            )
        )

        if category is None:
            raise ValueError("Расходная категория не найдена")

        budget = Budget(
            user_id=user_id,
            category_id=category_id,
            amount=amount,
            currency=currency,
        )
        session.add(budget)

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError(
                "Бюджет для этой категории и валюты уже существует"
            ) from exc

        await session.refresh(budget)
        return budget


async def get_budgets(tg_id: int):
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        result = await session.scalars(
            select(Budget)
            .where(Budget.user_id == user_id)
            .order_by(Budget.created_at.desc())
        )
        return list(result.all())



async def update_budget(
    tg_id: int,
    budget_id: int,
    amount: Decimal | int | float,
) -> bool:
    amount = _positive_amount(amount, "Лимит бюджета")

    async with async_session() as session:
        try:
            user_id = await _get_user_id(session, tg_id)

            budget = await session.scalar(
                select(Budget).where(
                    Budget.id == budget_id,
                    Budget.user_id == user_id,
                )
            )

            if budget is None:
                return False

            budget.amount = amount

            await session.commit()
            return True

        except Exception:
            await session.rollback()
            raise


async def delete_budget(
    tg_id: int,
    budget_id: int,
) -> bool:
    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        budget = await session.scalar(
            select(Budget).where(
                Budget.id == budget_id,
                Budget.user_id == user_id,
            )
        )

        if budget is None:
            return False

        await session.delete(budget)
        await session.commit()
        return True



async def get_budget_progress(
    tg_id: int,
    budget_id: int,
    now: datetime | None = None,
):
    now = now or datetime.now()

    month_start = _month_start(now)
    next_month = _next_month_start(now)

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        budget = await session.scalar(
            select(Budget).where(
                Budget.id == budget_id,
                Budget.user_id == user_id,
            )
        )

        if budget is None:
            return None

        category = await session.scalar(
            select(Category).where(
                Category.id == budget.category_id,
                Category.user_id == user_id,
                Category.type == "expense",
            )
        )

        if category is None:
            return None

        spent_result = await session.scalar(
            select(func.coalesce(func.sum(Transaction.amount), 0))
            .select_from(Transaction)
            .join(
                Category,
                (Transaction.category_id == Category.id)
                & (Transaction.user_id == Category.user_id),
            )
            .where(
                Transaction.user_id == user_id,
                Transaction.category_id == budget.category_id,
                Category.user_id == user_id,
                Category.type == "expense",
                Transaction.type == "expense",
                Transaction.currency == budget.currency,
                Transaction.created_at >= month_start,
                Transaction.created_at < next_month,
            )
        )

        spent = Decimal(str(spent_result or 0))
        limit = Decimal(str(budget.amount))

        if limit <= 0:
            raise ValueError("Лимит бюджета должен быть больше нуля")

        remaining = max(
            Decimal("0"),
            limit - spent,
        )

        percentage = (
            spent / limit * Decimal("100")
        ).quantize(MONEY_QUANTUM)

        return {
            "budget_id": budget.id,
            "category_id": budget.category_id,
            "category": category.name,
            "currency": budget.currency,
            "limit": limit,
            "spent": spent,
            "remaining": remaining,
            "percentage": percentage,
            "is_exceeded": spent >= limit,
            "month": now.strftime("%Y-%m"),
        }


# =========================================================
# BUDGET LIMIT NOTIFICATIONS
# =========================================================

async def get_budget_notifications_to_send(
    tg_id: int,
    now: datetime | None = None,
):
    """
    Возвращает достигнутые пороги 70/90/100%, для которых ещё
    нет записи об отправке в текущем месяце.
    """
    now = now or datetime.now()
    period_key = now.strftime("%Y-%m")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        # Один запрос получает все бюджеты и уже отправленные пороги.
        rows = await session.execute(
            select(
                Budget,
                Category.name,
                BudgetLimitNotification.threshold_percent,
            )
            .join(
                Category,
                (
                    (Category.id == Budget.category_id)
                    & (Category.user_id == Budget.user_id)
                ),
            )
            .outerjoin(
                BudgetLimitNotification,
                (
                    (BudgetLimitNotification.budget_id == Budget.id)
                    & (BudgetLimitNotification.user_id == user_id)
                    & (BudgetLimitNotification.period_key == period_key)
                ),
            )
            .where(Budget.user_id == user_id)
            .order_by(Budget.id)
        )

        budget_data: dict[int, dict[str, Any]] = {}
        sent_thresholds: dict[int, set[int]] = {}

        for budget, category_name, threshold in rows.all():
            budget_data[budget.id] = {
                "budget": budget,
                "category": category_name,
            }
            if threshold is not None:
                sent_thresholds.setdefault(budget.id, set()).add(threshold)

    notifications = []

    # Считаем прогресс уже вне открытой сессии.
    for budget_id, data in budget_data.items():
        budget = data["budget"]
        progress = await get_budget_progress(tg_id, budget_id, now)

        if progress is None:
            continue

        sent = sent_thresholds.get(budget_id, set())
        percentage = progress["percentage"]

        for threshold in BUDGET_THRESHOLDS:
            if percentage >= Decimal(str(threshold)) and threshold not in sent:
                notifications.append({
                    **progress,
                    "threshold": threshold,
                })

    return notifications


async def save_budget_limit_notification(
    tg_id: int,
    budget_id: int,
    threshold: int,
    now: datetime | None = None,
):
    if threshold not in BUDGET_THRESHOLDS:
        raise ValueError("Недопустимый порог бюджета")

    now = now or datetime.now()
    period_key = now.strftime("%Y-%m")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        # Проверяем принадлежность бюджета пользователю.
        budget_exists = await session.scalar(
            select(Budget.id).where(
                Budget.id == budget_id,
                Budget.user_id == user_id,
            )
        )

        if budget_exists is None:
            raise ValueError("Бюджет не найден")

        notification = BudgetLimitNotification(
            user_id=user_id,
            budget_id=budget_id,
            period_key=period_key,
            threshold_percent=threshold,
        )
        session.add(notification)

        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False

        return True


async def delete_budget_limit_notification(
    tg_id: int,
    budget_id: int,
    threshold: int,
    now: datetime | None = None,
):
    now = now or datetime.now()
    period_key = now.strftime("%Y-%m")

    async with async_session() as session:
        user_id = await _get_user_id(session, tg_id)

        notification = await session.scalar(
            select(BudgetLimitNotification).where(
                BudgetLimitNotification.user_id == user_id,
                BudgetLimitNotification.budget_id == budget_id,
                BudgetLimitNotification.period_key == period_key,
                BudgetLimitNotification.threshold_percent == threshold,
            )
        )

        if notification is None:
            return False

        await session.delete(notification)
        await session.commit()

        return True