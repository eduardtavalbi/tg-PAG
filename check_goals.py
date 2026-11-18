import asyncio

from app.database import requests as rq


async def main():
    tg_id = 999999999


    active = await rq.get_goals(tg_id)
    completed = await rq.get_completed_goals(tg_id)

    print("АКТИВНЫЕ ЦЕЛИ:")
    for goal in active:
        print(goal.id, goal.name, goal.status)

    print("\nЗАВЕРШЁННЫЕ ЦЕЛИ:")
    for goal in completed:
        progress = await rq.get_goal_progress(tg_id, goal.id)

        print(
            goal.id,
            goal.name,
            goal.status,
            progress["saved"],
            progress["target"],
            progress["progress"],
        )


asyncio.run(main())