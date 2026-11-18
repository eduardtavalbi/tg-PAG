
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from sqlalchemy import select, func, event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    User,
    Category,
    Transaction,
    Goal,
    GoalContribution,
)
from app.database import requests as rq
from app.ai.schemas import TransactionResult
from app.ai.gemini import parse_transactions


TG_ID = 987654321


class FinancialActionsTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Создаём изолированную тестовую SQLite-базу в памяти.
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        # Включаем внешние ключи для каждого подключения.
        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_test_foreign_keys(
            dbapi_connection,
            connection_record,
        ):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.test_session = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
        )

        # Создаём таблицы.
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        # Проверяем, что SQLite действительно включила внешние ключи.
        async with self.engine.connect() as connection:
            result = await connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            )
            self.assertEqual(result.scalar_one(), 1)

        # Подменяем сессии requests.py на тестовые.
        self.session_patch = patch.object(
            rq,
            "async_session",
            self.test_session,
        )
        self.session_patch.start()

        # Создаём тестового пользователя и категорию.
        async with self.test_session() as session:
            user = User(tg_id=TG_ID)
            session.add(user)
            await session.flush()

            self.user_id = user.id

            category = Category(
                name="Еда",
                type="expense",
                user_id=user.id,
            )
            session.add(category)

            await session.commit()
            await session.refresh(category)

            self.category_id = category.id

    async def asyncTearDown(self):
        self.session_patch.stop()
        await self.engine.dispose()

    async def count_rows(self, model):
        async with self.test_session() as session:
            result = await session.scalar(
                select(func.count()).select_from(model)
            )
            return result

    # ---------------------------------
    # 1. УСПЕШНОЕ СОХРАНЕНИЕ
    # ---------------------------------

    async def test_successful_atomic_save(self):
        result = await rq.save_financial_actions(
            tg_id=TG_ID,
            confirmation_id="a" * 32,
            transactions=[
                {
                    "type": "expense",
                    "amount": "50.00",
                    "description": "Покупка продуктов",
                    "currency": "USD",
                    "category_id": self.category_id,
                }
            ],
            goal_creation={
                "name": "Ноутбук",
                "amount": "100",
                "deadline": None,
            },
            contributions=[
                {
                    "amount": "100",
                    "new_goal": True,
                }
            ],
        )

        self.assertTrue(result)

        self.assertEqual(
            await self.count_rows(Transaction),
            1,
        )

        self.assertEqual(
            await self.count_rows(Goal),
            1,
        )

        self.assertEqual(
            await self.count_rows(GoalContribution),
            1,
        )

        async with self.test_session() as session:
            transaction = await session.scalar(
                select(Transaction)
            )

            goal = await session.scalar(
                select(Goal)
            )

            contribution = await session.scalar(
                select(GoalContribution)
            )

            self.assertEqual(
                transaction.currency,
                "USD",
            )

            self.assertEqual(
                transaction.amount,
                Decimal("50.00"),
            )

            self.assertEqual(
                goal.name,
                "Ноутбук",
            )

            self.assertEqual(
                goal.status,
                "completed",
            )

            self.assertEqual(
                contribution.amount,
                Decimal("100"),
            )

            self.assertEqual(
                contribution.goal_id,
                goal.id,
            )

            self.assertEqual(
                contribution.user_id,
                self.user_id,
            )

    # ---------------------------------
    # 2. ROLLBACK ПРИ НЕКОРРЕКТНОЙ КАТЕГОРИИ
    # ---------------------------------

    async def test_rollback_on_invalid_category(self):
        with self.assertRaisesRegex(
            ValueError,
            "(?i)категор",
        ):
            await rq.save_financial_actions(
                tg_id=TG_ID,
                confirmation_id="b" * 32,
                transactions=[
                    {
                        "type": "expense",
                        "amount": "50",
                        "description": "Корректная операция",
                        "category_id": self.category_id,
                    },
                    {
                        "type": "expense",
                        "amount": "100",
                        "description": "Некорректная операция",
                        "category_id": 999999,
                    },
                ],
                goal_creation={
                    "name": "Новая цель",
                    "amount": "1000",
                    "deadline": None,
                },
                contributions=[
                    {
                        "amount": "100",
                        "new_goal": True,
                    }
                ],
            )

        self.assertEqual(
            await self.count_rows(Transaction),
            0,
        )

        self.assertEqual(
            await self.count_rows(Goal),
            0,
        )

        self.assertEqual(
            await self.count_rows(GoalContribution),
            0,
        )

    # ---------------------------------
    # 3. ROLLBACK ПРИ НЕКОРРЕКТНОМ ПОПОЛНЕНИИ
    # ---------------------------------

    async def test_rollback_on_invalid_contribution(self):
        with self.assertRaises(ValueError):
            await rq.save_financial_actions(
                tg_id=TG_ID,
                confirmation_id="c" * 32,
                transactions=[
                    {
                        "type": "expense",
                        "amount": "50",
                        "description": "Продукты",
                        "category_id": self.category_id,
                    }
                ],
                goal_creation={
                    "name": "Новая цель",
                    "amount": "1000",
                    "deadline": None,
                },
                contributions=[
                    {
                        "amount": "-10",
                        "new_goal": True,
                    }
                ],
            )

        self.assertEqual(
            await self.count_rows(Transaction),
            0,
        )

        self.assertEqual(
            await self.count_rows(Goal),
            0,
        )

        self.assertEqual(
            await self.count_rows(GoalContribution),
            0,
        )


class AIParserTest(unittest.IsolatedAsyncioTestCase):

    @patch(
        "app.ai.gemini.client.aio.models.generate_content",
        new_callable=AsyncMock,
    )
    async def test_parser_accepts_parsed_response(self, mock_generate):
        expected = TransactionResult(
            transactions=[
                {
                    "type": "expense",
                    "amount": "120",
                    "description": "Продукты",
                    "category": "Еда",
                }
            ]
        )

        mock_generate.return_value = SimpleNamespace(
            parsed=expected,
            text=None,
        )

        result = await parse_transactions(
            "Потратил 120 юаней на продукты"
        )

        self.assertEqual(
            len(result.transactions),
            1,
        )

        self.assertEqual(
            result.transactions[0].amount,
            Decimal("120"),
        )

        mock_generate.assert_awaited_once()

    @patch(
        "app.ai.gemini.client.aio.models.generate_content",
        new_callable=AsyncMock,
    )
    async def test_parser_fallback_to_json_text(self, mock_generate):
        mock_generate.return_value = SimpleNamespace(
            parsed=None,
            text=(
                '{"transactions": ['
                '{"type": "income",'
                '"amount": "5000",'
                '"description": "Зарплата",'
                '"category": "Зарплата"}'
                ']}'
            ),
        )

        result = await parse_transactions(
            "Получил зарплату 5000"
        )

        self.assertEqual(
            result.transactions[0].type,
            "income",
        )

        self.assertEqual(
            result.transactions[0].amount,
            Decimal("5000"),
        )

    @patch(
        "app.ai.gemini.client.aio.models.generate_content",
        new_callable=AsyncMock,
    )
    async def test_parser_rejects_empty_response(self, mock_generate):
        mock_generate.return_value = SimpleNamespace(
            parsed=None,
            text=None,
        )

        with self.assertRaises(ValueError):
            await parse_transactions("Потратил деньги")

    @patch(
        "app.ai.gemini.client.aio.models.generate_content",
        new_callable=AsyncMock,
    )
    async def test_parser_rejects_invalid_json(self, mock_generate):
        mock_generate.return_value = SimpleNamespace(
            parsed=None,
            text="Это не JSON",
        )

        with self.assertRaises(ValidationError):
            await parse_transactions("Потратил 100")

    @patch(
        "app.ai.gemini.client.aio.models.generate_content",
        new_callable=AsyncMock,
    )
    async def test_parser_rejects_zero_amount(self, mock_generate):
        mock_generate.return_value = SimpleNamespace(
            parsed=None,
            text=(
                '{"transactions": ['
                '{"type": "expense",'
                '"amount": "0",'
                '"description": "Продукты",'
                '"category": "Еда"}'
                ']}'
            ),
        )

        with self.assertRaises(ValidationError):
            await parse_transactions("Потратил 0")

    @patch(
        "app.ai.gemini.client.aio.models.generate_content",
        new_callable=AsyncMock,
    )
    async def test_allocation_without_income_requires_clarification(
        self,
        mock_generate,
    ):
        expected = TransactionResult(
            goal_allocation={
                "goal_name": "Ноутбук",
                "percentage": "20",
            }
        )

        mock_generate.return_value = SimpleNamespace(
            parsed=expected,
            text=None,
        )

        result = await parse_transactions(
            "Отложи 20 процентов на ноутбук"
        )

        self.assertTrue(result.needs_clarification)
        self.assertIsNotNone(result.clarification_question)


if __name__ == "__main__":
    unittest.main(verbosity=2)
