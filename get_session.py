import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
# --- ВСТАВЬ СВОИ ДАННЫЕ СЮДА ---
API_ID = 30040785             # Твой API ID (цифрами, без кавычек)
API_HASH = '91cc645efceb331a96eb3f2d3703b6a3'        # Твой API HASH (в кавычках)
PHONE = '+918233471364'       # Твой номер телефона с +
PASSWORD = 'mafanya_2009'             # Твой пароль 2FA (если есть, в кавычках, иначе None)
# ------------------------------
async def main():
    # Создаем пустую сессию в памяти
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    print(f"🚀 Начинаем авторизацию для: {PHONE}")
    # Запуск процесса входа
    try:
        await client.start(phone=PHONE, password=PASSWORD)
        if await client.is_user_authorized():
            print("\n✅ УСПЕХ! СТРОКА СЕССИИ НИЖЕ:")
            print("-" * 50)
            print(client.session.save())  # Вот это нужно скопировать
            print("-" * 50)
            print("\nСкопируй её и вставь в таблицу watcher.readers (session_string)")
        else:
            print("❌ Ошибка: не удалось авторизоваться.")
    except Exception as e:
        print(f"❌ Произошла ошибка: {e}")
    finally:
        await client.disconnect()
if __name__ == "__main__":
    asyncio.run(main())