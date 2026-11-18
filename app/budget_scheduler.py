
import asyncio
import logging

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.database import requests as rq


logger = logging.getLogger(__name__)

# Московский часовой пояс
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Ежедневное время проверки
CHECK_HOUR = 9
CHECK_MINUTE = 0

# Не более трёх пользователей одновременно
BATCH_SIZE = 3

# Пауза между группами пользователей
BATCH_DELAY = 0.5

SEVERITY_LEVELS = {
    "moderate": 1,
    "high": 2,
}

BUDGET_THRESHOLDS = {
    70: "🟡",
    90: "🟠",
    100: "🔴",
}


def get_moscow_now() -> datetime:
    """Возвращает московское время без tzinfo для SQLite."""
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def get_next_run_time() -> datetime:
    """Возвращает время следующей проверки — 09:00 МСК."""

    now = datetime.now(MOSCOW_TZ)

    next_run = now.replace(
        hour=CHECK_HOUR,
        minute=CHECK_MINUTE,
        second=0,
        microsecond=0,
    )

    if next_run <= now:
        next_run += timedelta(days=1)

    return next_run


def format_budget_warning(warnings: list[dict]) -> str:
    """Формирует сообщение об ускорении расходов."""

    lines = [
        "📊 ВНИМАНИЕ: ИЗМЕНИЛСЯ ТЕМП РАСХОДОВ",
        "",
        "Обнаружено ускорение расходов относительно предыдущих 30 дней.",
        "",
    ]

    for warning in warnings:
        severity = warning["severity"]
        icon = "🔴" if severity == "high" else "🟡"

        growth = warning["growth_pct"]
        current = warning["current_spending"]
        historical = warning["historical_30d_spending"]
        projected = warning["projected_monthly_spending"]

        currency = warning["currency"]
        category = warning["category"]

        lines.extend([
            f"{icon} {category}",
            f"Расходы с начала месяца: {current:.2f} {currency}",
            f"За предыдущие 30 дней: {historical:.2f} {currency}",
            f"Ускорение расходов: +{growth:.1f}%",
            f"Прогноз на месяц: {projected:.2f} {currency}",
            "",
        ])

    lines.extend([
        "⚠️ Прогноз основан на текущем темпе расходов.",
        "Это не гарантированный результат и не установленный лимит бюджета.",
    ])

    return "\n".join(lines)


def format_monthly_budget_notification(
    notifications: list[dict],
) -> str:
    """Формирует сообщение о достижении порогов месячных бюджетов."""

    lines = [
        "🎯 КОНТРОЛЬ МЕСЯЧНЫХ БЮДЖЕТОВ",
        "",
    ]

    for notification in notifications:
        threshold = notification["threshold"]
        icon = BUDGET_THRESHOLDS[threshold]

        category = notification["category"]
        spent = notification["spent"]
        limit = notification["limit"]
        remaining = notification["remaining"]
        currency = notification["currency"]

        if threshold == 70:
            title = "Достигнуто 70% бюджета"
        elif threshold == 90:
            title = "Достигнуто 90% бюджета"
        else:
            title = "Достигнут лимит бюджета"

        lines.extend([
            f"{icon} {category} — {title}",
            f"Потрачено: {spent:.2f} / {limit:.2f} {currency}",
            f"Использовано: {notification['percentage']:.1f}%",
            f"Остаток: {remaining:.2f} {currency}",
            "",
        ])

    lines.append(
        "💡 Следи за расходами, чтобы не выйти за установленный лимит."
    )

    return "\n".join(lines)

async def send_monthly_budget_notifications(
    bot: Bot,
    tg_id: int,
    now: datetime | None = None,
):
    now = now or get_moscow_now()

    notifications = await rq.get_budget_notifications_to_send(
        tg_id=tg_id,
        now=now,
    )

    if not notifications:
        return

    reserved = []

    try:
        # Сначала резервируем каждый достигнутый порог в БД.
        for notification in notifications:
            saved = await rq.save_budget_limit_notification(
                tg_id=tg_id,
                budget_id=notification["budget_id"],
                threshold=notification["threshold"],
                now=now,
            )

            if saved:
                reserved.append(notification)

        if not reserved:
            return

        # Отправляем только те уведомления, которые удалось зарезервировать.
        message_text = format_monthly_budget_notification(reserved)

        await bot.send_message(
            chat_id=tg_id,
            text=message_text,
        )

        logger.info(
            "Monthly budget notifications sent: user=%s count=%s",
            tg_id,
            len(reserved),
        )

    except (Exception, asyncio.CancelledError):
        # Если отправка или резервирование завершились ошибкой,
        # освобождаем уже созданные резервирования.
        for notification in reserved:
            try:
                await rq.delete_budget_limit_notification(
                    tg_id=tg_id,
                    budget_id=notification["budget_id"],
                    threshold=notification["threshold"],
                    now=now,
                )
            except Exception:
                logger.exception(
                    "Failed to release budget notification reservation: "
                    "user=%s budget=%s threshold=%s",
                    tg_id,
                    notification["budget_id"],
                    notification["threshold"],
                )

        raise

async def check_monthly_budgets(bot: Bot, tg_id: int):
    try:
        await send_monthly_budget_notifications(
            bot=bot,
            tg_id=tg_id,
            now=get_moscow_now(),
        )

    except Exception:
        logger.exception(
            "Failed to check monthly budgets for user %s",
            tg_id,
        )

async def check_user_budget(bot: Bot, tg_id: int):
    """Проверяет оба типа бюджетных предупреждений пользователя."""

    # 1. Проверка ускорения расходов.
    try:
        now = get_moscow_now()
        period_key = now.strftime("%Y-%m")

        data = await rq.get_user_budget_warnings(tg_id, now)

        
        
        if data["status"] == "available":
            reserved_warnings = []

            # Резервируем отправку каждого предупреждения.
            for warning in data["warnings"]:
                reservation = await rq.save_budget_warning_notification(
                    tg_id=tg_id,
                    category_name=warning["category"],
                    currency=warning["currency"],
                    period_key=period_key,
                    severity=warning["severity"],
                )

                if reservation is not None:
                    previous_severity, previous_sent_at = reservation

                    reserved_warnings.append(
                        (warning, previous_severity, previous_sent_at)
                    )

            if reserved_warnings:
                warnings_to_send = [
                    item[0] for item in reserved_warnings
                ]

                message_text = format_budget_warning(warnings_to_send)

                try:
                    await bot.send_message(
                        chat_id=tg_id,
                        text=message_text,
                    )

                except (Exception, asyncio.CancelledError):
                    # При ошибке восстанавливаем предыдущее состояние записей.
                    for warning, previous_severity, previous_sent_at in reserved_warnings:
                        try:
                            await rq.rollback_budget_warning_notification(
                                tg_id=tg_id,
                                category_name=warning["category"],
                                currency=warning["currency"],
                                period_key=period_key,
                                severity=warning["severity"],
                                previous_severity=previous_severity,
                                previous_sent_at=previous_sent_at,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to rollback budget warning reservation "
                                "for user %s, category %s",
                                tg_id,
                                warning["category"],
                            )

                    raise

                logger.info(
                    "Budget warnings sent to user %s: %s",
                    tg_id,
                    len(reserved_warnings),
                )



    except Exception:
        logger.exception(
            "Failed to check spending pace for user %s",
            tg_id,
        )

    # 2. Независимая проверка месячных лимитов.
    await check_monthly_budgets(bot, tg_id)


async def check_all_users(bot: Bot):
    """Проверяет всех пользователей небольшими группами."""

    user_ids = await rq.get_all_user_tg_ids()

    logger.info(
        "Starting budget check for %s users",
        len(user_ids),
    )

    for start in range(0, len(user_ids), BATCH_SIZE):
        batch = user_ids[start:start + BATCH_SIZE]

        await asyncio.gather(
            *(
                check_user_budget(bot, tg_id)
                for tg_id in batch
            )
        )

        logger.info(
            "Processed %s of %s users",
            min(start + BATCH_SIZE, len(user_ids)),
            len(user_ids),
        )

        if start + BATCH_SIZE < len(user_ids):
            await asyncio.sleep(BATCH_DELAY)


async def budget_warning_scheduler(bot: Bot):
    """Запускает ежедневную проверку бюджетов в 09:00 МСК."""

    logger.info(
        "Budget warning scheduler started. Timezone: Europe/Moscow"
    )

    while True:
        try:
            next_run = get_next_run_time()
            now = datetime.now(MOSCOW_TZ)

            delay = (next_run - now).total_seconds()

            logger.info(
                "Next budget check at %s Moscow time",
                next_run.strftime("%Y-%m-%d %H:%M:%S"),
            )

            await asyncio.sleep(delay)

            logger.info("Daily budget check started")

            await check_all_users(bot)

            logger.info("Daily budget check completed")

        except asyncio.CancelledError:
            logger.info("Budget warning scheduler stopped")
            raise

        except Exception:
            logger.exception("Budget scheduler error")

            # При общей ошибке повторяем попытку через 5 минут.
            await asyncio.sleep(300)
