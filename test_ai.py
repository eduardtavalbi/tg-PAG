import asyncio
from pprint import pprint

from app.database.requests import get_advisor_data
from app.ai.gemini import generate_financial_advice


async def main():
    tg_id = 899134543

    print("Получаю финансовые данные...")

    financial_data = await get_advisor_data(tg_id)

    pprint(financial_data)

    print("\nГенерирую финансовый совет...\n")

    advice = await generate_financial_advice(financial_data)

    print("=" * 40)
    print(advice)
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(main())