# -*- coding: utf-8 -*-
"""
current_api.py — модуль работы с API курсов валют exchangerate.host.

Endpoints:
    /live    — курсы в реальном времени
    /convert — конвертация суммы из одной валюты в другую

API-ключ берётся из переменных окружения (файл .env), не хранится в коде.
Для экономии бесплатного лимита (100 запросов/мес) ответы кешируются.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.exchangerate.host"
API_KEY = os.getenv("CURRENCY_API_KEY", "")

CACHE_TTL = 600  # секунды, 10 минут
_cache: dict[str, tuple[float, object]] = {}


class CurrencyAPIError(Exception):
    """Ошибка при обращении к API курсов валют."""


def _api_get(endpoint: str, params: dict) -> dict:
    """Выполняет GET-запрос к API и проверяет ответ."""
    if not API_KEY:
        raise CurrencyAPIError(
            "API-ключ не задан. Пропишите CURRENCY_API_KEY в файле .env"
        )
    params["access_key"] = API_KEY
    try:
        response = requests.get(f"{API_URL}/{endpoint}", params=params, timeout=10)
        data = response.json()
    except requests.RequestException as exc:
        raise CurrencyAPIError(f"Ошибка соединения с API: {exc}")
    except ValueError:
        raise CurrencyAPIError("API вернул некорректный ответ (не JSON)")

    # На тарифе free поля success может не быть — считаем успехом, если есть данные
    if data.get("success") is False:
        err = data.get("error", {})
        raise CurrencyAPIError(
            f"Ошибка API ({err.get('code', '?')}): {err.get('info', 'нет данных')}"
        )
    return data


def _cache_get(key: str):
    record = _cache.get(key)
    if record and time.time() - record[0] < CACHE_TTL:
        return record[1]
    return None


def _cache_put(key: str, value) -> None:
    _cache[key] = (time.time(), value)


def get_current_rate(base: str = "USD", currencies: list[str] | None = None) -> dict[str, float]:
    """
    Возвращает словарь курсов: сколько единиц валюты назначения дают за 1 базовую валюту.

    Пример: get_current_rate("RUB", ["CNY"]) -> {"CNY": 0.0095}
    """
    currencies = currencies or []
    base = base.upper()
    currencies = [c.upper() for c in currencies if c.upper() != base]
    if not currencies:
        return {}

    cache_key = f"live:{base}:{','.join(currencies)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = _api_get("live", {"source": base, "currencies": ",".join(currencies)})
    quotes = data.get("quotes") or {}
    result: dict[str, float] = {}
    for quote, rate in quotes.items():
        # /live возвращает ключи вида "USDRUB" (source + symbol) — обрезаем префикс
        code = quote[len(base):] if len(quote) == len(base) + 3 else quote
        if rate:
            result[code] = float(rate)
    if not result:
        raise CurrencyAPIError("API не вернул курсы для заданных валют")
    _cache_put(cache_key, result)
    return result


def convert_currency(amount: float, from_currency: str, to_currency: str) -> float:
    """
    Конвертирует amount из валюты from_currency в валюту to_currency через API.
    Возвращает получившуюся сумму.
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return float(amount)

    cache_key = f"rate:{from_currency}:{to_currency}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return float(amount) * cached

    data = _api_get(
        "convert",
        {"from": from_currency, "to": to_currency, "amount": 1},
    )
    result = data.get("result")
    info = data.get("info") or {}
    rate = float(result if result is not None else info.get("rate", 0))
    if not rate:
        raise CurrencyAPIError(
            f"API не смог сконвертировать {from_currency} -> {to_currency}"
        )
    _cache_put(cache_key, rate)
    return float(amount) * rate


if __name__ == "__main__":
    # Быстрая проверка модуля: python current_api.py
    print(get_current_rate("RUB", ["USD", "EUR", "CNY"]))
    print(convert_currency(100, "RUB", "CNY"))
