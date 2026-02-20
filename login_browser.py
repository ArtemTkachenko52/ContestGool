import asyncio
from playwright.async_api import async_playwright
import os

PHONE = "918088396263" # Номер без плюса для папки

async def login():
    # Создаем папку сессии прямо в корне проекта
    user_data_dir = os.path.join(os.getcwd(), "sessions_storage", f"session_{PHONE}")


    async with async_playwright() as p:
        # headless=False — ОТКРОЕТ ОКНО БРАУЗЕРА
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False, 
            args=['--no-sandbox']
        )
        page = await context.new_page()
        # ЗАХОДИМ СТРОГО В ВЕРСИЮ /A/
        await page.goto('https://web.telegram.org')

        print("🔓 ВНИМАНИЕ: Залогинься в Телеграм через QR или СМС.")
        print("🔓 Дождись появления списка чатов.")
        print("❌ Как только увидишь свои чаты — ЗАКРОЙ ОКНО БРАУЗЕРА.")

        # Даем 5 минут на ввод кода
        await page.wait_for_timeout(300000) 
        await context.close()

if __name__ == "__main__":
    asyncio.run(login())
