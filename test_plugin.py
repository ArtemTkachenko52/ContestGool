# -*- coding: utf-8 -*-
NAME = "TestPlugin"
VERSION = "1.0.0"
DESCRIPTION = "Проверка запуска"
CREDITS = "@ArtemTk29"
UUID = "550e8400-e29b-41d4-a716-446655440000"
SETTINGS_PAGE = False
BIND_TO_DELETE = False
# Ошибка была здесь: Cardinal ожидает список функций, а не False
BIND_TO_PRE_INIT = [] 
BIND_TO_MESSAGES = []
BIND_TO_POST_INIT = []
def setup(cardinal):
    cardinal.logger.info(f"--- [!!!] ПОБЕДА: {NAME} ЗАПУЩЕН [!!!] ---")