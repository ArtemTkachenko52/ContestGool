# -*- coding: utf-8 -*-
from sqlalchemy import select, update, desc
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import asyncio
import math
# Настройки плагина для Cardinal
NAME = "StarsOrderAutomation"
VERSION = "1.1.0"
DESCRIPTION = "Автоматическое распределение заказов звезд/подарков"
CREDITS = "@ArtemTk29"
UUID = "550e8400-e29b-41d4-a716-446655440000"
SETTINGS_PAGE = False
BIND_TO_PRE_INIT = []
BIND_TO_MESSAGES = []
BIND_TO_POST_INIT = []
BIND_TO_DELETE = []
# Конфигурация БД (должна совпадать с docker-compose)
DATABASE_URL = "postgresql+asyncpg://admin:password123@db:5432/contest_monitor"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# Справочник цен: Цена на FP -> Техническая стоимость (с учетом 15% и запаса)
PRICE_MAP = {
    21.0: 25,  # Роза
    43.0: 50,  # Торт/Букет/Ракета/Шампанское
    85.0: 100  # Кубок/Кольцо/Алмаз
}
def setup(cardinal):
    @cardinal.event("new_order") # Событие появления нового заказа
    async def handle_new_order(order_obj):
        """
        Логика: Проверка возможности исполнения и нарезка задач в БД.
        """
        price_per_item = float(order_obj.price) / int(order_obj.amount)
        needed_quantity = int(order_obj.amount)       
        # 1. Проверяем, наш ли это товар по цене
        if price_per_item not in PRICE_MAP:
            cardinal.logger.warning(f"⚠️ Заказ #{order_obj.id}: Цена {price_per_item} не в реестре. Пропуск.")
            return
        tech_cost = PRICE_MAP[price_per_item]
        total_needed_stars = tech_cost * needed_quantity
        cardinal.logger.info(f"📦 Новый заказ #{order_obj.id}: {needed_quantity} шт. по {tech_cost} зв. (Всего: {total_needed_stars})")
        async with AsyncSessionLocal() as session:
            # 2. Получаем "богатых" воркеров (Баланс > 25) сверху вниз
            from database.models import WorkerAccount, StarReport, SalesOrder
            res = await session.execute(
                select(WorkerAccount)
                .where(WorkerAccount.stars_balance > 25, WorkerAccount.is_alive == True)
                .order_by(desc(WorkerAccount.stars_balance))
            )
            workers = res.scalars().all()
            assignments = []
            remaining_to_fulfill = needed_quantity
            # 3. АЛГОРИТМ РАСЧЕТА (Твой ТЗ)
            for w in workers:
                if remaining_to_fulfill <= 0:
                    break
                effective_balance = w.stars_balance - 25
                # Сколько ПОЛНЫХ подарков может отправить этот аккаунт
                can_send = math.floor(effective_balance / tech_cost)
                if can_send > 0:
                    take = min(can_send, remaining_to_fulfill)
                    assignments.append({
                        "worker_id": w.tg_id,
                        "count": take,
                        "worker_obj": w
                    })
                    remaining_to_fulfill -= take
            # 4. ВЕРДИКТ
            if remaining_to_fulfill > 0:
                cardinal.logger.error(f"❌ НЕДОСТАТОЧНО СРЕДСТВ для заказа #{order_obj.id}. Нужно еще {remaining_to_fulfill} шт.")
                # Опционально: можно отправить сообщение покупателю через cardinal.send_message
                return
            # 5. ИСПОЛНЕНИЕ: Пишем в БД задачи для function1
            try:
                # Создаем запись о продаже для статистики
                new_sale = SalesOrder(
                    customer_username=order_obj.username,
                    gift_type=order_obj.title, # Название товара
                    total_quantity=needed_quantity,
                    status="processing"
                )
                session.add(new_sale)
                await session.flush() # Получаем ID продажи
                for assign in assignments:
                    for _ in range(assign['count']):
                        session.add(StarReport(
                            passport_id=0, # Метка внешнего заказа
                            target_user=order_obj.username,
                            method=order_obj.title,
                            executor_id=assign['worker_id'],
                            status="approved" # Сразу в работу воркеру
                        ))
                    # Обновляем баланс в БД превентивно (чтобы следующий заказ видел актуальные цифры)
                    assign['worker_obj'].stars_balance -= (assign['count'] * tech_cost)
                await session.commit()
                cardinal.logger.info(f"✅ Заказ #{order_obj.id} распределен между {len(assignments)} воркерами.")
                # Подтверждаем заказ в Cardinal (опционально)
                # order_obj.send_message("Ваш заказ принят и обрабатывается автоматически!")
            except Exception as e:
                await session.rollback()
                cardinal.logger.error(f"🔥 Ошибка БД при обработке заказа: {e}")