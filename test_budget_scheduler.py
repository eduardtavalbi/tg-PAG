
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, User
from app.database import requests as rq
from app import budget_scheduler as scheduler


TEST_TG_ID = 899134543
TEST_NOW = datetime(2026, 11, 18, 9, 0)
TEST_PERIOD = "2026-11"
TEST_CATEGORY = "Еда"


def make_warning(severity: str) -> dict:
    return {
        "category": TEST_CATEGORY,
        "currency": "CNY",
        "severity": severity,
        "growth_pct": 45 if severity == "moderate" else 75,
        "current_spending": 1500,
        "historical_30d_spending": 1000,
        "projected_monthly_spending": 2700,
    }


class BudgetSchedulerTest(IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test_budget.sqlite3"

        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}"
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.test_session = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.original_session = rq.async_session
        rq.async_session = self.test_session

        async with self.test_session() as session:
            session.add(User(tg_id=TEST_TG_ID))
            await session.commit()

        self.bot = AsyncMock(spec=Bot)
        self.warning = make_warning("moderate")

        self.warning_patch = patch.object(
            rq,
            "get_user_budget_warnings",
            new_callable=AsyncMock,
        )
        self.get_warnings = self.warning_patch.start()
        self.get_warnings.return_value = {
            "status": "available",
            "warnings": [self.warning],
        }

        self.time_patch = patch.object(
            scheduler,
            "get_moscow_now",
            return_value=TEST_NOW,
        )
        self.time_patch.start()

        self.monthly_patch = patch.object(
            scheduler,
            "check_monthly_budgets",
            new_callable=AsyncMock,
        )
        self.monthly_mock = self.monthly_patch.start()

    async def asyncTearDown(self):
        self.monthly_patch.stop()
        self.time_patch.stop()
        self.warning_patch.stop()

        rq.async_session = self.original_session

        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def get_saved_severity(self):
        return await rq.get_budget_warning_severity(
            tg_id=TEST_TG_ID,
            category_name=TEST_CATEGORY,
            currency="CNY",
            period_key=TEST_PERIOD,
        )

    async def test_success_and_duplicate_protection(self):
        # Первое moderate успешно отправляется.
        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(await self.get_saved_severity(), "moderate")

        # Повторное moderate не отправляется.
        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(self.bot.send_message.await_count, 1)

        # Повышение до high отправляется.
        self.warning = make_warning("high")
        self.get_warnings.return_value = {
            "status": "available",
            "warnings": [self.warning],
        }

        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(await self.get_saved_severity(), "high")

        # Повторное high не отправляется.
        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_new_notification_is_removed_after_send_failure(self):
        self.bot.send_message.side_effect = RuntimeError(
            "Telegram API unavailable"
        )

        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.bot.send_message.assert_awaited_once()
        self.assertIsNone(await self.get_saved_severity())

    async def test_moderate_is_restored_after_high_send_failure(self):
        # Сначала успешно отправляем moderate.
        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(await self.get_saved_severity(), "moderate")

        # Затем имитируем ошибку при отправке high.
        self.warning = make_warning("high")
        self.get_warnings.return_value = {
            "status": "available",
            "warnings": [self.warning],
        }
        self.bot.send_message.side_effect = RuntimeError(
            "Telegram API unavailable"
        )

        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(self.bot.send_message.await_count, 2)

        # Старый moderate должен восстановиться.
        self.assertEqual(await self.get_saved_severity(), "moderate")

        # После восстановления high можно попробовать отправить снова.
        self.bot.send_message.side_effect = None

        await scheduler.check_user_budget(self.bot, TEST_TG_ID)

        self.assertEqual(self.bot.send_message.await_count, 3)
        self.assertEqual(await self.get_saved_severity(), "high")


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
