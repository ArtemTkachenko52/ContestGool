BOT_USERNAME = 'contest_gool_bot'
CHANNELS = [
	'contestgo_test'
]

KEYWORDS = [
	'конкурс'
]
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
			# Пересылка в "Избранное" (Saved Messages)
			await event.message.forward_to('me')
			# Пересылка в бота
			await event.message.forward_to(BOT_USERNAME)
			print(f"Переслано в избранное и боту: {text[:50]}...")

	await client.run_until_disconnected()

if __name__ == "__main__":
	import asyncio
	asyncio.run(main())