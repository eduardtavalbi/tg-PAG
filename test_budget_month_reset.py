
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    User,
    Category,
    Budget,
    Transaction,
)
from app.database import requests as rq


class TestBudgetMonthReset(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.session_patch = patch.object(
            rq,
            "async_session",
            self.session_factory,
        )
        self.session_patch.start()

        async with self.session_factory() as session:
            user = User(tg_id=999999999)
            session.add(user)
            await session.flush()

            category = Category(
                user_id=user.id,
                name="Test food",
                type="expense",
            )
            session.add(category)
            await session.flush()

            budget = Budget(
                user_id=user.id,
                category_id=category.id,
                amount=Decimal("1000"),
                currency="CNY",
            )
            session.add(budget)
            await session.flush()

            transaction = Transaction(
                user_id=user.id,
                category_id=category.id,
                amount=Decimal("750"),
                currency="CNY",
                description="Test expense",
                type="expense",
                created_at=datetime(2026, 11, 17, 12, 0),
            )
            session.add(transaction)
            await session.commit()

            self.tg_id = user.tg_id
            self.budget_id = budget.id

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.engine.dispose()

    async def test_monthly_expenses_reset(self):
        november = await rq.get_budget_progress(
            self.tg_id,
            self.budget_id,
            now=datetime(2026, 11, 18, 12, 0),
        )

        self.assertIsNotNone(november)
        self.assertEqual(november["spent"], Decimal("750.00"))
        self.assertEqual(november["percentage"], Decimal("75.00"))

        december = await rq.get_budget_progress(
            self.tg_id,
            self.budget_id,
            now=datetime(2026, 12, 1, 12, 0),
        )

        self.assertIsNotNone(december)
        self.assertEqual(december["spent"], Decimal("0.00"))
        self.assertEqual(december["percentage"], Decimal("0.00"))

    async def test_notifications_are_separated_by_month(self):
        november = datetime(2026, 11, 18, 12, 0)
        december = datetime(2026, 12, 1, 12, 0)

        first_save = await rq.save_budget_limit_notification(
            self.tg_id,
            self.budget_id,
            70,
            now=november,
        )
        self.assertTrue(first_save)

        duplicate_save = await rq.save_budget_limit_notification(
            self.tg_id,
            self.budget_id,
            70,
            now=november,
        )
        self.assertFalse(duplicate_save)

        december_save = await rq.save_budget_limit_notification(
            self.tg_id,
            self.budget_id,
            70,
            now=december,
        )
        self.assertTrue(december_save)


if __name__ == "__main__":
    unittest.main()
