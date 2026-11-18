
import asyncio
import logging

from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TOKEN
from app.handlers import router
from app.database.models import async_main, async_close
from app.budget_scheduler import budget_warning_scheduler


async def main():
    await async_main()

    bot = Bot(token=TOKEN)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler_task = asyncio.create_task(
        budget_warning_scheduler(bot)
    )

    try:
        await dp.start_polling(bot)

    finally:
        scheduler_task.cancel()

        with suppress(asyncio.CancelledError):
            await scheduler_task

        await bot.session.close()
        await async_close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("Bot stopped")
