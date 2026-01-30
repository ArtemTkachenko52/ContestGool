from telethon import TelegramClient
from decouple import config


api_id = config('API_ID')
api_hash = config('API_HASH')
phone = config('PHONE')
login = config('LOGIN')
password = config('PASSWORD', default=None)

client = TelegramClient(login, api_id, api_hash)

async def main():
	await client.start(phone=phone, password=password)
	print("Клиент Telethon успешно запущен!")
	await client.run_until_disconnected()

if __name__ == "__main__":
	import asyncio
	asyncio.run(main())