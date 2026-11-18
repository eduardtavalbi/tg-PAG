
import asyncio
from decimal import Decimal

from app.database import requests as rq
from app.database.models import async_close


# Укажи свой Telegram ID
TG_ID = 899134543


async def main():
    try:
        user = await rq.get_user(TG_ID)

        if user is None:
            print("Сначала запусти бота и отправь ему /start")
            return

        categories = await rq.get_categories(
            TG_ID,
            category_type="expense",
        )

        if not categories:
            print("Расходные категории не найдены")
            return

        category = categories[0]

        print("Категория:", category.name)

        # Проверяем создание
        budget = await rq.create_budget(
            tg_id=TG_ID,
            category_id=category.id,
            amount=Decimal("3000"),
            currency="CNY",
        )

        print("Бюджет создан:", budget.id)

        # Проверяем прогресс
        progress = await rq.get_budget_progress(
            TG_ID,
            budget.id,
        )

        print("\nПрогресс:")
        print(progress)

        assert progress["limit"] == Decimal("3000.00")

        # Проверяем изменение
        updated = await rq.update_budget(
            TG_ID,
            budget.id,
            Decimal("4000"),
        )

        assert updated is True

        progress = await rq.get_budget_progress(
            TG_ID,
            budget.id,
        )

        assert progress["limit"] == Decimal("4000.00")

        print("\nЛимит успешно изменён")

        # Проверяем удаление
        print("\nSUCCESS: все тесты пройдены!")

    finally:
        await async_close()


if __name__ == "__main__":
    asyncio.run(main())
