import asyncio
from playwright.async_api import async_playwright
import os

# ВСТАВЬ СВОЙ НОМЕР СЮДА (для названия папки)
PHONE = "+918088396263" 

async def login():
    # Папка создастся прямо там, где запустишь скрипт
    user_data_dir = os.path.join(os.getcwd(), f"session_{PHONE}")
    
    async with async_playwright() as p:
        # headless=False — ОТКРОЕТ ОКНО БРАУЗЕРА
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False, 
            args=['--no-sandbox']
        )
        page = await context.new_page()
        await page.goto('https://web.telegram.org')
        
        print("🔓 Залогинься в Телеграм и подожди, пока появится список чатов.")
        print("❌ Как только залогинишься — просто закрой это окно браузера.")
        
        # Ждем, пока ты сам закроешь браузер
        await page.wait_for_timeout(300000) # Даем тебе 5 минут на ввод кода
        await context.close()

if __name__ == "__main__":
    asyncio.run(login())
