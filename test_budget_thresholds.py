
import unittest
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
)
from app.database import requests as rq


class TestBudgetThresholds(unittest.IsolatedAsyncioTestCase):

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
            user = User(tg_id=999999998)
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
            await session.commit()

            self.tg_id = user.tg_id
            self.budget_id = budget.id
            self.category_id = category.id

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.engine.dispose()

    async def test_thresholds_and_duplicate_protection(self):
        test_cases = [
            (Decimal("700"), 70),
            (Decimal("200"), 90),
            (Decimal("100"), 100),
        ]

        for amount, expected_threshold in test_cases:
            transaction = await rq.add_transaction(
                tg_id=self.tg_id,
                transaction_type="expense",
                amount=amount,
                description=f"Test threshold {expected_threshold}%",
                category_id=self.category_id,
                currency="CNY",
            )

            self.assertIsNotNone(transaction)

            notifications = await rq.get_budget_notifications_to_send(
                self.tg_id
            )

            budget_notifications = [
                item
                for item in notifications
                if item["budget_id"] == self.budget_id
            ]

            thresholds = sorted(
                item["threshold"]
                for item in budget_notifications
            )

            self.assertEqual(
                thresholds,
                [expected_threshold],
                f"Ожидался порог {expected_threshold}%, "
                f"получены {thresholds}",
            )

            saved = await rq.save_budget_limit_notification(
                self.tg_id,
                self.budget_id,
                expected_threshold,
            )

            self.assertTrue(saved)

            # После сохранения уведомление больше не должно появляться.
            notifications = await rq.get_budget_notifications_to_send(
                self.tg_id
            )

            budget_notifications = [
                item
                for item in notifications
                if item["budget_id"] == self.budget_id
            ]

            self.assertEqual(budget_notifications, [])

        # Повторное сохранение порога 100% должно блокироваться.
        duplicate = await rq.save_budget_limit_notification(
            self.tg_id,
            self.budget_id,
            100,
        )

        self.assertFalse(duplicate)

    async def test_final_budget_progress(self):
        for amount in (
            Decimal("700"),
            Decimal("200"),
            Decimal("100"),
        ):
            await rq.add_transaction(
                tg_id=self.tg_id,
                transaction_type="expense",
                amount=amount,
                description="Test expense",
                category_id=self.category_id,
                currency="CNY",
            )

        progress = await rq.get_budget_progress(
            self.tg_id,
            self.budget_id,
        )

        self.assertIsNotNone(progress)
        self.assertEqual(progress["spent"], Decimal("1000.00"))
        self.assertEqual(progress["percentage"], Decimal("100.00"))
        self.assertEqual(progress["remaining"], Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
