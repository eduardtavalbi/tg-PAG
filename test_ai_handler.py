
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import handlers
from app.ai.schemas import (
    TransactionResult,
    Transaction,
    GoalCreation,
    GoalContribution,
    GoalPercentageAllocation,
)


TG_ID = 987654321


class FakeState:
    def __init__(self, data=None, current_state=None):
        self.data = data or {}
        self.current_state = current_state
        self.update_data = AsyncMock(side_effect=self._update_data)
        self.set_state = AsyncMock(side_effect=self._set_state)
        self.get_data = AsyncMock(return_value=self.data)
        self.get_state = AsyncMock(return_value=current_state)
        self.clear = AsyncMock(side_effect=self._clear)

    async def _update_data(self, **kwargs):
        self.data.update(kwargs)

    async def _set_state(self, state):
        self.current_state = state

    async def _clear(self):
        self.data.clear()
        self.current_state = None


class AIHandlerTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.state = FakeState()

        self.status = SimpleNamespace(
            edit_text=AsyncMock()
        )

        self.message = SimpleNamespace(
            text="Тестовое сообщение",
            from_user=SimpleNamespace(id=TG_ID),
            answer=AsyncMock(return_value=self.status),
        )

        self.category = SimpleNamespace(
            id=10,
            name="Еда",
            type="expense",
        )

        self.income_category = SimpleNamespace(
            id=11,
            name="Зарплата",
            type="income",
        )

        self.goal = SimpleNamespace(
            id=20,
            name="Ноутбук",
            currency="CNY",
        )

    def make_result(self, **kwargs):
        return TransactionResult(**kwargs)

    async def prepare_handler(self, result, categories=None, goals=None):
        categories = categories or []
        goals = goals or []

        patches = [
            patch.object(
                handlers,
                "parse_transactions",
                new_callable=AsyncMock,
                return_value=result,
            ),
            patch.object(
                handlers.rq,
                "get_categories",
                new_callable=AsyncMock,
                return_value=categories,
            ),
            patch.object(
                handlers.rq,
                "get_goals",
                new_callable=AsyncMock,
                return_value=goals,
            ),
            patch.object(
                handlers,
                "transaction_confirmation_keyboard",
                return_value="TEST_KEYBOARD",
            ),
        ]

        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        await handlers.ai_message_handler(
            self.message,
            self.state,
        )

    async def test_regular_expense(self):
        result = self.make_result(
            transactions=[
                Transaction(
                    type="expense",
                    amount=Decimal("50"),
                    description="Обед",
                    category="Еда",
                    currency="CNY",
                )
            ]
        )

        await self.prepare_handler(
            result,
            categories=[self.category],
        )

        data = self.state.data

        self.assertEqual(len(data["pending_transactions"]), 1)
        self.assertEqual(
            data["pending_transactions"][0]["category_id"],
            10,
        )
        self.assertEqual(
            data["pending_transactions"][0]["amount"],
            "50",
        )

        self.assertIsNone(data["pending_goal_creation"])
        self.assertEqual(data["pending_contributions"], [])
        self.assertTrue(data["pending_confirmation_id"])

        self.state.set_state.assert_awaited_once_with(
            handlers.PendingTransactions.waiting_confirmation
        )

        preview = self.status.edit_text.await_args.args[0]
        self.assertIn("50", preview)
        self.assertIn("Обед", preview)

    async def test_unknown_category_stops_processing(self):
        result = self.make_result(
            transactions=[
                Transaction(
                    type="expense",
                    amount=Decimal("50"),
                    description="Обед",
                    category="Несуществующая категория",
                )
            ]
        )

        await self.prepare_handler(
            result,
            categories=[self.category],
        )

        self.state.update_data.assert_not_awaited()
        self.state.set_state.assert_not_awaited()

        error_text = self.status.edit_text.await_args.args[0]
        self.assertIn("Несуществующая категория", error_text)

    async def test_goal_creation(self):
        result = self.make_result(
            goal_creation=GoalCreation(
                name="Новый ноутбук",
                target_amount=Decimal("5000"),
                currency="CNY",
            )
        )

        await self.prepare_handler(result)

        data = self.state.data

        self.assertEqual(
            data["pending_goal_creation"]["name"],
            "Новый ноутбук",
        )
        self.assertEqual(
            data["pending_goal_creation"]["amount"],
            "5000",
        )
        self.assertEqual(data["pending_transactions"], [])

        preview = self.status.edit_text.await_args.args[0]
        self.assertIn("Новый ноутбук", preview)
        self.assertIn("5 000", preview)

        self.state.set_state.assert_awaited_once()

    async def test_contribution_to_existing_goal(self):
        result = self.make_result(
            goal_contribution=GoalContribution(
                goal_name="Ноутбук",
                amount=Decimal("300"),
            )
        )

        await self.prepare_handler(
            result,
            goals=[self.goal],
        )

        contributions = self.state.data["pending_contributions"]

        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0]["goal_id"], 20)
        self.assertEqual(contributions[0]["goal_name"], "Ноутбук")
        self.assertEqual(contributions[0]["amount"], "300")
        self.assertFalse(contributions[0]["new_goal"])

        preview = self.status.edit_text.await_args.args[0]
        self.assertIn("Ноутбук", preview)
        self.assertIn("300", preview)

    async def test_percentage_allocation(self):
        result = self.make_result(
            transactions=[
                Transaction(
                    type="income",
                    amount=Decimal("1000"),
                    description="Зарплата",
                    category="Зарплата",
                    currency="CNY",
                )
            ],
            goal_allocation=GoalPercentageAllocation(
                goal_name="Ноутбук",
                percentage=Decimal("10"),
            ),
        )

        await self.prepare_handler(
            result,
            categories=[self.income_category],
            goals=[self.goal],
        )

        contributions = self.state.data["pending_contributions"]

        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0]["goal_id"], 20)
        self.assertEqual(contributions[0]["amount"], "100.00")
        self.assertEqual(contributions[0]["percentage"], "10")

        preview = self.status.edit_text.await_args.args[0]
        self.assertIn("100", preview)
        self.assertIn("10%", preview)

    async def test_gemini_error(self):
        with patch.object(
            handlers,
            "parse_transactions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Gemini unavailable"),
        ):
            await handlers.ai_message_handler(
                self.message,
                self.state,
            )

        self.state.update_data.assert_not_awaited()
        self.state.set_state.assert_not_awaited()
        self.status.edit_text.assert_awaited_once()

    async def test_no_financial_actions(self):
        result = self.make_result()

        await self.prepare_handler(result)

        self.state.update_data.assert_not_awaited()
        self.state.set_state.assert_not_awaited()

    async def test_confirmation_saves_actions(self):
        data = {
            "pending_transactions": [
                {
                    "type": "expense",
                    "amount": "50",
                    "description": "Обед",
                    "category_id": 10,
                    "category": "Еда",
                    "currency": "CNY",
                }
            ],
            "pending_goal_creation": None,
            "pending_contributions": [],
            "pending_confirmation_id": "test-confirmation-id",
        }

        state = FakeState(data=data)

        callback_message = SimpleNamespace(
            edit_text=AsyncMock()
        )

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=TG_ID),
            bot=SimpleNamespace(),
            message=callback_message,
            answer=AsyncMock(),
        )

        with (
            patch.object(
                handlers.rq,
                "save_financial_actions",
                new_callable=AsyncMock,
                return_value=True,
            ) as save_mock,
            patch.object(
                handlers,
                "send_budget_limit_notifications",
                new_callable=AsyncMock,
            ) as notification_mock,
        ):
            await handlers.confirm_transactions(callback, state)

        save_mock.assert_awaited_once()

        saved_data = save_mock.await_args.kwargs

        self.assertEqual(saved_data["tg_id"], TG_ID)
        self.assertEqual(
            saved_data["confirmation_id"],
            "test-confirmation-id",
        )

        notification_mock.assert_awaited_once()

        state.clear.assert_awaited_once()
        callback_message.edit_text.assert_awaited_once()
        callback.answer.assert_awaited_once()

    async def test_cancel_transactions(self):
        callback_message = SimpleNamespace(
            edit_text=AsyncMock()
        )

        callback = SimpleNamespace(
            message=callback_message,
            answer=AsyncMock(),
        )

        await handlers.cancel_transactions(
            callback,
            self.state,
        )

        self.state.clear.assert_awaited_once()
        callback_message.edit_text.assert_awaited_once()
        callback.answer.assert_awaited_once()

    
    async def test_message_while_waiting_confirmation(self):
        self.state.current_state = (
            handlers.PendingTransactions.waiting_confirmation
        )
        self.state.get_state.return_value = (
            handlers.PendingTransactions.waiting_confirmation
        )

        with patch.object(
            handlers,
            "parse_transactions",
            new_callable=AsyncMock,
        ) as parse_mock:
            await handlers.ai_message_handler(
                self.message,
                self.state,
            )

        parse_mock.assert_not_awaited()
        self.message.answer.assert_awaited_once()
        self.state.update_data.assert_not_awaited()

    async def test_gemini_requests_clarification(self):
        result = self.make_result(
            needs_clarification=True,
            clarification_question="Уточни валюту операции.",
        )

        await self.prepare_handler(result)

        self.assertEqual(
            self.status.edit_text.await_args.args[0],
            "Уточни валюту операции.",
        )
        self.state.update_data.assert_not_awaited()
        self.state.set_state.assert_not_awaited()

    async def test_contribution_to_unknown_goal(self):
        result = self.make_result(
            goal_contribution=GoalContribution(
                goal_name="Несуществующая цель",
                amount=Decimal("100"),
            )
        )

        await self.prepare_handler(
            result,
            goals=[self.goal],
        )

        self.state.update_data.assert_not_awaited()
        self.state.set_state.assert_not_awaited()

        error_text = self.status.edit_text.await_args.args[0]
        self.assertIn("Ноутбук", error_text)

    async def test_duplicate_confirmation(self):
        state = FakeState(data={
            "pending_transactions": [
                {
                    "type": "expense",
                    "amount": "50",
                    "description": "Обед",
                    "category_id": 10,
                    "category": "Еда",
                    "currency": "CNY",
                }
            ],
            "pending_goal_creation": None,
            "pending_contributions": [],
            "pending_confirmation_id": "duplicate-id",
        })

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=TG_ID),
            bot=SimpleNamespace(),
            message=SimpleNamespace(
                edit_text=AsyncMock()
            ),
            answer=AsyncMock(),
        )

        with patch.object(
            handlers.rq,
            "save_financial_actions",
            new_callable=AsyncMock,
            return_value=False,
        ) as save_mock:
            await handlers.confirm_transactions(callback, state)

        save_mock.assert_awaited_once()
        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once()

        self.assertTrue(
            callback.answer.await_args.kwargs.get("show_alert")
        )

    async def test_confirmation_without_id(self):
        state = FakeState(data={
            "pending_transactions": [
                {
                    "type": "expense",
                    "amount": "50",
                    "description": "Обед",
                    "category_id": 10,
                    "category": "Еда",
                    "currency": "CNY",
                }
            ],
            "pending_goal_creation": None,
            "pending_contributions": [],
        })

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=TG_ID),
            bot=SimpleNamespace(),
            message=SimpleNamespace(
                edit_text=AsyncMock()
            ),
            answer=AsyncMock(),
        )

        with patch.object(
            handlers.rq,
            "save_financial_actions",
            new_callable=AsyncMock,
        ) as save_mock:
            await handlers.confirm_transactions(callback, state)

        save_mock.assert_not_awaited()
        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once()

    async def test_database_save_error(self):
        state = FakeState(data={
            "pending_transactions": [
                {
                    "type": "expense",
                    "amount": "50",
                    "description": "Обед",
                    "category_id": 10,
                    "category": "Еда",
                    "currency": "CNY",
                }
            ],
            "pending_goal_creation": None,
            "pending_contributions": [],
            "pending_confirmation_id": "error-id",
        })

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=TG_ID),
            bot=SimpleNamespace(),
            message=SimpleNamespace(
                edit_text=AsyncMock()
            ),
            answer=AsyncMock(),
        )

        with patch.object(
            handlers.rq,
            "save_financial_actions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Database unavailable"),
        ) as save_mock:
            await handlers.confirm_transactions(callback, state)

        save_mock.assert_awaited_once()
        callback.answer.assert_awaited_once()

        # При ошибке сохранения FSM не очищается:
        # пользователь может повторить попытку.
        state.clear.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()



if __name__ == "__main__":
    unittest.main()
