import asyncio
from app.database import requests as rq
from app.database.models import async_close

async def main():
    try:
        user = await rq.get_user(999999999)
        print("Пользователь найден:", user is not None)

        if user:
            budgets = await rq.get_budgets(999999999)
            print("Количество бюджетов:", len(budgets))

            for budget in budgets:
                print(
                    "Бюджет:",
                    budget.id,
                    budget.amount,
                    budget.currency,
                )
    finally:
        await async_close()

asyncio.run(main())
