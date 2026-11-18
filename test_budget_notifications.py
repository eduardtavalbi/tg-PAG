
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


class TestBudgetNotifications(unittest.IsolatedAsyncioTestCase):

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

        self.now = datetime.now().replace(microsecond=0)

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
                created_at=self.now,
            )
            session.add(transaction)
            await session.commit()

            self.tg_id = user.tg_id
            self.budget_id = budget.id

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.engine.dispose()

    async def test_reached_thresholds_are_returned(self):
        notifications = await rq.get_budget_notifications_to_send(
            self.tg_id
        )

        self.assertTrue(
            notifications,
            "При расходах 75% бюджета должно появиться уведомление",
        )

    async def test_duplicate_notification_is_prevented(self):
        saved = await rq.save_budget_limit_notification(
            self.tg_id,
            self.budget_id,
            70,
            now=self.now,
        )

        self.assertTrue(saved)

        saved_again = await rq.save_budget_limit_notification(
            self.tg_id,
            self.budget_id,
            70,
            now=self.now,
        )

        self.assertFalse(saved_again)


if __name__ == "__main__":
    unittest.main()
