
import asyncio
import unittest

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app import budget_scheduler as scheduler


class FakeDateTime(datetime):
    fixed_now = datetime(2026, 11, 17, 8, 59)

    @classmethod
    def now(cls, tz=None):
        if tz:
            return cls.fixed_now.replace(
                tzinfo=scheduler.MOSCOW_TZ
            )

        return cls.fixed_now


class BudgetSchedulerTests(unittest.IsolatedAsyncioTestCase):

    # ---------------------------------
    # 1. ПРОВЕРКА ВРЕМЕНИ ЗАПУСКА
    # ---------------------------------

    async def test_next_run_time(self):
        with patch.object(
            scheduler,
            "datetime",
            FakeDateTime,
        ):
            # До 09:00 — запуск сегодня
            FakeDateTime.fixed_now = datetime(
                2026, 11, 17, 8, 59
            )

            result = scheduler.get_next_run_time()

            self.assertEqual(
                result.hour,
                9,
            )
            self.assertEqual(
                result.day,
                17,
            )

            # После 09:00 — запуск завтра
            FakeDateTime.fixed_now = datetime(
                2026, 11, 17, 9, 1
            )

            result = scheduler.get_next_run_time()

            self.assertEqual(
                result.hour,
                9,
            )
            self.assertEqual(
                result.day,
                18,
            )

            # Ровно 09:00 — следующий запуск завтра
            FakeDateTime.fixed_now = datetime(
                2026, 11, 17, 9, 0
            )

            result = scheduler.get_next_run_time()

            self.assertEqual(
                result.day,
                18,
            )

        print("SUCCESS: время запуска 09:00 проверено")

    # ---------------------------------
    # 2. ОБРАБОТКА НЕСКОЛЬКИХ ПОЛЬЗОВАТЕЛЕЙ
    # ---------------------------------

    async def test_multiple_users(self):
        user_ids = [101, 102, 103, 104, 105, 106, 107]

        with (
            patch.object(
                scheduler.rq,
                "get_all_user_tg_ids",
                new=AsyncMock(return_value=user_ids),
            ),
            patch.object(
                scheduler,
                "check_user_budget",
                new=AsyncMock(),
            ) as check_mock,
            patch.object(
                scheduler.asyncio,
                "sleep",
                new=AsyncMock(),
            ) as sleep_mock,
            patch.object(
                scheduler,
                "BATCH_SIZE",
                3,
            ),
            patch.object(
                scheduler,
                "BATCH_DELAY",
                0.5,
            ),
        ):
            await scheduler.check_all_users(AsyncMock())

            self.assertEqual(
                check_mock.await_count,
                7,
            )

            # Между тремя группами две паузы
            self.assertEqual(
                sleep_mock.await_count,
                2,
            )

            sleep_mock.assert_any_await(0.5)

        print("SUCCESS: обработка 7 пользователей проверена")

    # ---------------------------------
    # 3. УСПЕШНАЯ ОТПРАВКА УВЕДОМЛЕНИЯ
    # ---------------------------------

    async def test_monthly_notification_success(self):
        notifications = [
            {
                "budget_id": 1,
                "threshold": 70,
                "category": "Еда",
                "spent": 750,
                "limit": 1000,
                "remaining": 250,
                "percentage": 75,
                "currency": "CNY",
            }
        ]

        bot = AsyncMock()

        with (
            patch.object(
                scheduler,
                "get_moscow_now",
                return_value=datetime(2026, 11, 17, 9, 0),
            ),
            patch.object(
                scheduler.rq,
                "get_budget_notifications_to_send",
                new=AsyncMock(return_value=notifications),
            ),
            patch.object(
                scheduler.rq,
                "save_budget_limit_notification",
                new=AsyncMock(return_value=True),
            ) as save_mock,
        ):
            await scheduler.check_monthly_budgets(bot, 123)

            bot.send_message.assert_awaited_once()

            save_mock.assert_awaited_once_with(
                tg_id=123,
                budget_id=1,
                threshold=70,
                now=datetime(2026, 11, 17, 9, 0),
            )

        print("SUCCESS: уведомление отправлено и сохранено")

    # ---------------------------------
    # 4. ОШИБКА TELEGRAM API
    # ---------------------------------

    
    async def test_telegram_send_failure(self):
        notifications = [
            {
                "budget_id": 1,
                "threshold": 70,
                "category": "Еда",
                "spent": 750,
                "limit": 1000,
                "remaining": 250,
                "percentage": 75,
                "currency": "CNY",
            }
        ]

        bot = AsyncMock()
        bot.send_message.side_effect = RuntimeError(
            "Telegram API unavailable"
        )

        with (
            patch.object(
                scheduler,
                "get_moscow_now",
                return_value=datetime(2026, 11, 17, 9, 0),
            ),
            patch.object(
                scheduler.rq,
                "get_budget_notifications_to_send",
                new=AsyncMock(return_value=notifications),
            ),
            patch.object(
                scheduler.rq,
                "save_budget_limit_notification",
                new=AsyncMock(return_value=True),
            ) as save_mock,
            patch.object(
                scheduler.rq,
                "delete_budget_limit_notification",
                new=AsyncMock(return_value=True),
            ) as delete_mock,
        ):
            # Ошибка обрабатывается внутри функции.
            await scheduler.check_monthly_budgets(bot, 123)

            bot.send_message.assert_awaited_once()

            # Резерв сначала создаётся.
            save_mock.assert_awaited_once()

            # После ошибки отправки резерв освобождается.
            delete_mock.assert_awaited_once()

        print("SUCCESS: при ошибке Telegram резерв освобождается")

    # ---------------------------------
    # 5. ПОВТОРНАЯ ПОПЫТКА ПЛАНИРОВЩИКА
    # ---------------------------------

    async def test_scheduler_retry(self):
        bot = AsyncMock()

        fake_now = datetime(
            2026, 11, 17, 8, 0,
            tzinfo=scheduler.MOSCOW_TZ,
        )

        class RetryDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

            # Первый sleep — ожидание 09:00.
            # Второй — повторная попытка через 5 минут.
            # Третий — останавливаем тест.
            if len(sleep_calls) == 3:
                raise asyncio.CancelledError

        with (
            patch.object(
                scheduler,
                "datetime",
                RetryDateTime,
            ),
            patch.object(
                scheduler,
                "get_next_run_time",
                return_value=datetime(
                    2026, 11, 17, 9, 0,
                    tzinfo=scheduler.MOSCOW_TZ,
                ),
            ),
            patch.object(
                scheduler.asyncio,
                "sleep",
                side_effect=fake_sleep,
            ),
            patch.object(
                scheduler,
                "check_all_users",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "Database unavailable"
                    )
                ),
            ) as check_mock,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.budget_warning_scheduler(bot)

            check_mock.assert_awaited_once()

            self.assertEqual(
                sleep_calls[1],
                300,
            )

        print("SUCCESS: повторная попытка через 5 минут проверена")


if __name__ == "__main__":
    unittest.main(verbosity=2)
