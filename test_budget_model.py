
import asyncio

from sqlalchemy import inspect

from app.database.models import engine, async_main, async_close


async def main():
    try:
        await async_main()

        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda conn: inspect(conn).get_table_names()
            )

        print("Таблицы базы данных:")
        for table in tables:
            print("-", table)

        assert "budgets" in tables

        print("\nSUCCESS: таблица budgets создана!")

    finally:
        await async_close()


if __name__ == "__main__":
    asyncio.run(main())
