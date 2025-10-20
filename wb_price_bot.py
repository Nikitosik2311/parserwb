
import requests
import time
import json
import os
from urllib.parse import quote_plus
from datetime import datetime

# Настройки (можно переопределить через переменные окружения)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '1013871325')  # numeric chat id
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL_SECONDS', 300))  # повтор проверок в секундах (по умолчанию 5 минут)

# Список запросов и порог (рубли). По задаче — два запроса и порог 50000
QUERIES = [
    {"q": "Iphone 16", "threshold": 50000},
    {"q": "Айфон 16", "threshold": 50000},
]

# Файл для хранения уже отправленных уведомлений (чтобы не спамить)
NOTIFIED_FILE = 'notified.json'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
}

SEARCH_URL = 'https://search.wb.ru/exactmatch/ru/common/v4/search?query={query}&limit=30'


def load_notified():
    try:
        with open(NOTIFIED_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_notified(s):
    try:
        with open(NOTIFIED_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('Error saving notified file:', e)


def send_telegram_message(text):
    if TELEGRAM_TOKEN.startswith('PUT_YOUR'):
        print('Telegram token not set. Set TELEGRAM_TOKEN environment variable.')
        return False
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': False,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code == 200:
            return True
        else:
            print('Telegram send error', r.status_code, r.text)
            return False
    except Exception as e:
        print('Telegram request failed:', e)
        return False


def parse_search_results(data):
    """Извлекает список товаров с ценами (в рублях) из ответа WB (если возможно).
    Возвращает список словарей: {id, name, price_rub, url}
    """
    items = []
    try:
        # Попробуем пройти по стандартной структуре
        products = []
        if isinstance(data, dict):
            # В разных версиях API могут быть разные поля
            # чаще встречается data['data']['products'] или data['data']['subjects']
            for key in ('data', 'items', 'products', 'subjects'):
                if key in data and isinstance(data[key], list):
                    products = data[key]
                    break
            # иначе проверим вложенное
            if not products and 'data' in data and isinstance(data['data'], dict):
                for k in ('products', 'subjects', 'items'):
                    if k in data['data'] and isinstance(data['data'][k], list):
                        products = data['data'][k]
                        break

        # Фоллбек — искать все объекты, у которых есть 'id' и 'salePriceU'
        if not products:
            def collect(d):
                if isinstance(d, dict):
                    if 'id' in d and ('salePriceU' in d or 'priceU' in d or 'price' in d):
                        products.append(d)
                    for v in d.values():
                        collect(v)
                elif isinstance(d, list):
                    for it in d:
                        collect(it)
            collect(data)

        for p in products:
            try:
                pid = p.get('id') or p.get('nm_id') or p.get('sku')
                name = p.get('name') or p.get('title') or p.get('brand') or ''
                # цена в некоторых API приходит как salePriceU (в копейках)
                price_u = p.get('salePriceU') or p.get('priceU') or p.get('price')
                if price_u is None:
                    # попробуем из nested
                    if 'price' in p and isinstance(p['price'], dict):
                        price_u = p['price'].get('sale') or p['price'].get('now')
                # перевод в рубли: предположим, что значение в копейках -> делим на 100
                price_rub = None
                if isinstance(price_u, (int, float)):
                    price_rub = int(price_u) / 100.0
                elif isinstance(price_u, str) and price_u.isdigit():
                    price_rub = int(price_u) / 100.0

                link = None
                if pid:
                    link = f'https://www.wildberries.ru/catalog/{pid}/detail.aspx'

                if price_rub is not None:
                    items.append({'id': str(pid), 'name': name, 'price': price_rub, 'url': link})
            except Exception:
                continue
    except Exception as e:
        print('Error parsing results:', e)
    return items


def search_wb(query):
    url = SEARCH_URL.format(query=quote_plus(query))
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print('WB search HTTP', r.status_code)
            return []
        try:
            data = r.json()
        except ValueError:
            # иногда WB возвращает JS wrapper — попробуем извлечь JSON вручную
            text = r.text
            start = text.find('{')
            if start != -1:
                try:
                    data = json.loads(text[start:])
                except Exception:
                    return []
            else:
                return []
        return parse_search_results(data)
    except Exception as e:
        print('WB request failed:', e)
        return []


def main_loop():
    print('Starting Wildberries watcher. Queries:', [q['q'] for q in QUERIES])
    notified = load_notified()
    while True:
        for q in QUERIES:
            query = q['q']
            threshold = q['threshold']
            print(f'[{datetime.now()}] Searching for: {query} (threshold {threshold} RUB)')
            try:
                results = search_wb(query)
                for item in results:
                    try:
                        if item['price'] <= threshold:
                            uid = f"{query}__{item.get('id') or item.get('url') or item.get('name')}__{int(item['price'])}"
                            if uid in notified:
                                # уже уведомляли
                                continue
                            text = (
                                f"🔔 <b>Найдено на Wildberries</b>\n"
                                f"<b>{item.get('name') or query}</b> — <b>{item['price']:.0f} ₽</b>\n"
                            )
                            if item.get('url'):
                                text += f"Ссылка: {item['url']}\n"
                            text += f"Запрос: {query}"
                            ok = send_telegram_message(text)
                            if ok:
                                notified.add(uid)
                                save_notified(notified)
                                print('Notified:', uid)
                            else:
                                print('Failed to send notification for', uid)
                    except Exception as e:
                        print('Error processing item:', e)
            except Exception as e:
                print('Search loop error:', e)
            # Небольшая пауза между запросами, чтобы не нагружать API
            time.sleep(1)
        print('Iteration finished. Sleeping for', CHECK_INTERVAL, 'seconds')
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        print('Stopped by user')


