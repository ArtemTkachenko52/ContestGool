BOT_USERNAME = 'contest_gool_bot'
CHANNELS = [
    'contestgo_test'
]

KEYWORDS = [
    'конкурс'
]

# Ключ для отправки вместе с пересылаемым сообщением
FORWARD_KEY = 'KEY: 12345'
from telethon import TelegramClient
from decouple import config


api_id = config('API_ID')
api_hash = config('API_HASH')
phone = config('PHONE')
login = config('LOGIN')
password = config('PASSWORD', default=None)

client = TelegramClient(login, api_id, api_hash)

from telethon import events

async def main():
	await client.start(phone=phone, password=password)
	print("Клиент Telethon успешно запущен!")

	@client.on(events.NewMessage(chats=CHANNELS))
	async def handler(event):
		text = event.message.message or ""
		if any(keyword.lower() in text.lower() for keyword in KEYWORDS):
			# Пересылка в бота
			await event.message.forward_to(BOT_USERNAME)
			await client.send_message(BOT_USERNAME, FORWARD_KEY)
			print(f"Переслано боту: {text[:50]}... и отправлен ключ {FORWARD_KEY}")

	await client.run_until_disconnected()

if __name__ == "__main__":
	import asyncio
	asyncio.run(main())