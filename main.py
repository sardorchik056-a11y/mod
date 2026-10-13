import logging
import time
import threading
import requests
import telebot
from telebot import types
from datetime import datetime
import random

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = "8693034024:AAEjOqhChUGq8IvZHYIOw2-RcfJLSyK7ZBI"
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

muted: dict[int, set[int]] = {}
owners: dict[str, int] = {}
msg_cache: dict[tuple, str] = {}
spam_stop: dict[int, threading.Event] = {}
name_history: dict[int, list[dict]] = {}
pikmi_mode: dict[int, bool] = {}
italian_mode: dict[int, bool] = {}
emo_mode: dict[int, bool] = {}
gentleman_mode: dict[int, bool] = {}  # Джентльменский режим


# ── Утилиты ───────────────────────────────────────────────────────

def is_muted(chat_id, user_id):
    return user_id in muted.get(chat_id, set())

def do_mute(chat_id, user_id):
    muted.setdefault(chat_id, set()).add(user_id)

def do_unmute(chat_id, user_id):
    muted.get(chat_id, set()).discard(user_id)

def get_owner(biz_id):
    if biz_id in owners:
        return owners[biz_id]
    try:
        bc = bot.get_business_connection(biz_id)
        owners[biz_id] = bc.user.id
        log.info(f"Owner={bc.user.id}")
        return bc.user.id
    except Exception as e:
        log.warning(f"get_business_connection error: {e}")
        return None


def track_name(user: types.User):
    uid = user.id
    full_name = (user.first_name or "").strip()
    if user.last_name:
        full_name += f" {user.last_name}"
    username = user.username or ""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    history = name_history.setdefault(uid, [])
    if history:
        last = history[-1]
        if last["name"] == full_name and last["username"] == username:
            return
    history.append({"name": full_name, "username": username, "seen": now})
    log.info(f"📝 История имён: {uid} → {full_name} / @{username}")


def delete_business_msg(chat_id: int, msg_id: int, biz_id: str) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/deleteBusinessMessages"
    r = requests.post(url, json={"business_connection_id": biz_id, "chat_id": chat_id, "message_ids": [msg_id]}, timeout=10)
    data = r.json()
    if not data.get("ok"):
        log.warning(f"❌ Ошибка удаления: {data.get('description')}")
    return data.get("ok", False)


def edit_business_msg(chat_id: int, msg_id: int, biz_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    r = requests.post(url, json={
        "business_connection_id": biz_id,
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML"
    }, timeout=10)
    data = r.json()
    if not data.get("ok"):
        log.warning(f"❌ Ошибка редактирования: {data.get('description')}")
    return data.get("ok", False)


def send_business_msg(chat_id: int, biz_id: str, text: str) -> int | None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(url, json={
        "business_connection_id": biz_id,
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }, timeout=10)
    data = r.json()
    if data.get("ok"):
        return data["result"]["message_id"]
    log.warning(f"❌ Ошибка отправки: {data.get('description')}")
    return None


def format_user(from_user):
    username = f"@{from_user.username}" if from_user.username else "нет username"
    full_name = from_user.first_name or ""
    if from_user.last_name:
        full_name += f" {from_user.last_name}"
    return full_name, username


def notify_owner(owner_id: int, from_user, text: str, event: str, old_text: str = None):
    full_name, username = format_user(from_user)
    if event == "deleted":
        body = (
            f"🗑 <b>Удалённое сообщение</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f'<tg-emoji emoji-id="5260399854500191689">🚽</tg-emoji> От: {full_name} ({username})\n'
            f"🆔 ID: {from_user.id}\n"
            f"💬 Текст:\n{text}"
        )
    else:
        body = (
            f"✏️ <b>Изменённое сообщение</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f'<tg-emoji emoji-id="5260399854500191689">🚽</tg-emoji> От: {full_name} ({username})\n'
            f"🆔 ID: {from_user.id}\n"
            f'<tg-emoji emoji-id="6030776052345737530">🚽</tg-emoji> Было:\n{old_text or "неизвестно"}\n\n'
            f'<tg-emoji emoji-id="5258215846450305872">🚽</tg-emoji> Стало:\n{text}'
        )
    try:
        bot.send_message(owner_id, body, parse_mode="HTML")
    except Exception as e:
        log.warning(f"Не удалось уведомить владельца: {e}")


# ── Пикми-режим ───────────────────────────────────────────────────

PIKMI_ADDONS = [
    " ~ня", " ~пупсик", " ~кис-кис", " ~зайка", " ~солнышко", " ~котёнок",
    " ✨", " 🌸", " 💕", " 💖", " 💗", " 💫", " 🎀", " ♡", 
    " (милота)", " (няшно)", " (пупсик)", " (кись-кись)",
    " 〜", " ⋆｡°✩", " ˚₊· ͟͟͞͞➳", " ・゜゜・．",
]

PIKMI_PREFIXES = [
    "ня~ ", "пупс~ ", "кис~ ", "пикми~ ", "♡ ", "🌸 ", "✨ ", "💕 ", "💖 ",
]

PIKMI_HEARTS = ["❤️", "🧡", "💛", "💚", "💙", "💜", "🩷", "🩵", "💕", "💖", "💗", "💓", "💝", "💞", "💟"]

PIKMI_EMOJI_MIX = ["🥺", "👉👈", "uwu", "owo", ">_<", "^_^", "~_~", "☆", "★", "♪", "♫"]

def pikmi_add_to_text(original_text: str) -> str:
    result = original_text
    words = result.split()
    
    if random.random() < 0.4 and words:
        idx = random.randint(0, len(words)-1)
        words[idx] = words[idx] + random.choice(PIKMI_HEARTS)
        result = " ".join(words)
    
    if random.random() < 0.3 and words:
        idx = random.randint(0, len(words)-1)
        words[idx] = random.choice(PIKMI_HEARTS) + words[idx]
        result = " ".join(words)
    
    if random.random() < 0.5:
        result = random.choice(PIKMI_PREFIXES) + result
    
    if random.random() < 0.7:
        result += random.choice(PIKMI_ADDONS)
    
    if random.random() < 0.6:
        heart_count = random.randint(1, 4)
        result += " " + " ".join(random.choices(PIKMI_HEARTS, k=heart_count))
    
    if random.random() < 0.4:
        result += " " + random.choice(PIKMI_EMOJI_MIX)
    
    if random.random() < 0.2:
        result = f"({result})"
    
    if random.random() < 0.3 and result:
        result = result[0].upper() + result[1:] if result else result
    
    if random.random() < 0.25:
        vowels = ['а', 'о', 'у', 'ы', 'э', 'я', 'ё', 'ю', 'и', 'е']
        for vowel in vowels:
            if vowel in result.lower():
                result = result.replace(vowel, vowel * 2, 1)
                break
    
    return result


# ── Итальянский романтик-режим ────────────────────────────────────

ITALIAN_PREFIXES = [
    "Amore mio~ ", "Tesoro~ ", "Bella~ ", "Carissima~ ", "Dolcezza~ ", 
    "Cuore mio~ ", "Stella~ ", "Angelo~ ", "Principessa~ ", "Principe~ ",
    "🇮🇹 ", "🍕 ", "🍝 ", "☕ ", "🌹 ", "💋 ",
]

ITALIAN_SUFFIXES = [
    " ~ti amo", " ~cuore mio", " ~la mia vita", " ~dolcezza", 
    " ~bellissima", " ~tesoro", " ~amore", " ~per sempre",
    " ~con affetto", " ~con passione", " ~mwah", " ~ciao bella",
]

ITALIAN_PHRASES = [
    " Ti amo! 💕", " Sei la mia vita! 🌹", " Bellissima! ✨", 
    " Che bello! 💋", " Perfetto! 🇮🇹", " Dolcezza! 🍕",
    " Amore eterno! 💕", " La mia anima gemella! 🌟", " Sogno d'amore! 💫",
    " Cuore impazzito! 💗", " Passione infinita! 🔥", " Tesoro mio! 💖",
    " Sei speciale! 🎀", " Con tutto il cuore! 💝", " Mamma mia che amore! 🍝",
]

ITALIAN_GESTURES = [
    " 🤌", " 🇮🇹", " 💋", " 🤌🇮🇹", " 👉🇮🇹", " 💕🤌", 
    " 🍕🤌", " 🍝💋", " 🌹🤌", " 💕🇮🇹", " 🤌✨", " 💋🇮🇹",
]

ITALIAN_WORDS_REPLACE = {
    "привет": "Ciao bella! 🇮🇹",
    "пока": "Ciao ciao amore! 💕",
    "спасибо": "Grazie mille tesoro! 🌹",
    "люблю": "Ti amo con tutto il cuore! 💋",
    "хорошо": "Perfetto amore mio! ✨",
    "красиво": "Bellissimo! 🤌🇮🇹",
    "да": "Si amore! 💕",
    "нет": "No dolcezza... 💔",
}

def italian_add_to_text(original_text: str) -> str:
    text_lower = original_text.lower()
    result = original_text
    
    for word, replacement in ITALIAN_WORDS_REPLACE.items():
        if word in text_lower:
            result = replacement
            break
    
    if result == original_text:
        if random.random() < 0.6:
            result = random.choice(ITALIAN_PREFIXES) + result
        
        if random.random() < 0.5:
            result += random.choice(ITALIAN_PHRASES)
        
        if random.random() < 0.4:
            result += random.choice(ITALIAN_SUFFIXES)
        
        if random.random() < 0.7:
            result += random.choice(ITALIAN_GESTURES)
        
        if random.random() < 0.5:
            hearts = ["❤️", "🧡", "💛", "💚", "💙", "💜", "🩷", "💕", "💖", "💗", "💓", "💝"]
            result += " " + "".join(random.choices(hearts, k=random.randint(1, 3)))
        
        if random.random() < 0.3:
            exclamations = ["!", "!!", "!!!", "~", "~~", "~~~"]
            result += random.choice(exclamations)
    
    return result


# ── Эмо-режим ─────────────────────────────────────────────────────

EMO_PREFIXES = [
    "Никто не поймет... ", "В этой тьме... ", "Моя душа плачет... ", 
    "Снова эта боль... ", "Зачем я здесь... ", "Пустота внутри... ",
    "Тьма окутывает... ", "Слезы на ветру... ", "Разбитое сердце... ",
    "Одиночество... ", "Мрак... ", "Безысходность... ", "Депрессия... ",
]

EMO_SUFFIXES = [
    " ...никто не поймет", " ...боль внутри", " ...слезы текут", 
    " ...тьма сжирает", " ...пустота", " ...безысходность",
    " ...зачем всё это", " ...нет смысла", " ...одиночество убивает",
    " ...разбито навсегда", " ...эмоции прочь", " ...ничего не важно",
]

EMO_PHRASES = [
    " 🖤", " 💔", " 🥀", " 🌑", " 🌙", " 🕷️", " 🕸️", " ⚰️", 
    " вЕчНыЙ мРаК...", " дУшА бОлИт...", " сЕрДцЕ рВаНоЕ...",
    " никто не поймет этой боли", " в мире нет счастья", " все против меня",
]

EMO_WORDS_REPLACE = {
    "привет": "привет... если тебе есть до меня дело... 🖤",
    "пока": "прощай... в пустоту... 💔",
    "спасибо": "какое спасибо... мне ничего не нужно... 🥀",
    "люблю": "разве можно любить такое чудовище... 💔",
    "хорошо": "хорошо? что такое хорошо? пустота... 🖤",
    "плохо": "плохо? всегда плохо... всегда больно... 💔",
    "круто": "круто? ничто не круто в этом мире... 🥀",
    "смешно": "смех сквозь слезы... никто не понимает... 💧",
    "да": "да... наверное... хотя какая разница... 🖤",
    "нет": "нет... никогда... все бессмысленно... 💔",
}

def emo_add_to_text(original_text: str) -> str:
    text_lower = original_text.lower()
    result = original_text
    
    for word, replacement in EMO_WORDS_REPLACE.items():
        if word in text_lower:
            result = replacement
            break
    
    if result == original_text:
        if random.random() < 0.4:
            chars = []
            for c in result:
                if random.random() < 0.3:
                    chars.append(c.upper() if c.islower() else c.lower())
                else:
                    chars.append(c)
            result = "".join(chars)
        
        if random.random() < 0.5:
            result = random.choice(EMO_PREFIXES) + result.lower()
        
        if random.random() < 0.6:
            result += random.choice(EMO_SUFFIXES)
        
        if random.random() < 0.7:
            result += random.choice(EMO_PHRASES)
        
        if random.random() < 0.5:
            dots = random.choice(["..", "...", "....", "....."])
            result += dots
    
    if random.random() < 0.8:
        sad_endings = [" 🖤", " 💔", " 🥀", " 🌑", " 🌙", " 🕷️", " 🕸️", " ⚰️", " 💧", " 😢", " 😭"]
        result += random.choice(sad_endings)
    
    return result


# ── Джентльменский режим (aristocrat) ─────────────────────────────

GENTLEMAN_PREFIXES = [
    "Сударь, ", "Сударыня, ", "Милостивый государь, ", "Дражайшая, ",
    "Честь имею заметить, ", "Позвольте обратиться, ", "С глубочайшим почтением, ",
    "Осмелюсь доложить, ", "Ваш покорный слуга, ", "Имею честь, ",
    "Всенижайше прошу, ", "Благоволите выслушать, ", "С премногим уважением, ",
    "🎩 ", "🧐 ", "🏛️ ", "📜 ", "⚜️ ",
]

GENTLEMAN_SUFFIXES = [
    " ...с позволения сказать", " ...ежели позволите", " ...покорнейше благодарю",
    " ...нижайший поклон", " ...с чувством глубокого уважения", " ...честь по чести",
    " ...по совести и чести", " ...истинно так", " ...сударь мой", " ...милостивый государь",
]

GENTLEMAN_PHRASES = [
    " Честь имею! 🎩", " Премного благодарен! 🧐", " Весьма признателен! 📜",
    " Изывольно! ⚜️", " Браво! 🏛️", " Истинно так! 🎩", " Глубокоуважаемо! 🧐",
    " С премногим удовольствием! 📜", " Покорнейше благодарствую! ⚜️",
]

GENTLEMAN_WORDS_REPLACE = {
    "привет": "Честь имею приветствовать вас, сударь! 🎩",
    "здравствуй": "Нижайший поклон вам, милостивый государь! 🧐",
    "пока": "Позвольте откланяться, честь имею! 📜",
    "до свидания": "С чувством глубокого уважения прощаюсь с вами! ⚜️",
    "спасибо": "Покорнейше благодарю, сударь! Весьма признателен! 🎩",
    "пожалуйста": "Помилуйте, не стоит благодарности! 🧐",
    "да": "Истинно так, милостивый государь! 📜",
    "нет": "Увы, но вынужден отказать, сударь... 🎩",
    "хорошо": "Весьма изывольно, премного одобряю! ⚜️",
    "отлично": "Браво! Пальму первенства вам вручаю! 🏛️",
    "извини": "Помилуйте, вину свою признаю и прощения прошу! 🧐",
}

def gentleman_add_to_text(original_text: str) -> str:
    """Добавляет джентльменские/аристократические эффекты к тексту"""
    text_lower = original_text.lower()
    result = original_text
    
    for word, replacement in GENTLEMAN_WORDS_REPLACE.items():
        if word in text_lower:
            result = replacement
            break
    
    if result == original_text:
        if random.random() < 0.6:
            result = random.choice(GENTLEMAN_PREFIXES) + result
        
        if random.random() < 0.5:
            result += random.choice(GENTLEMAN_SUFFIXES)
        
        if random.random() < 0.5:
            result += random.choice(GENTLEMAN_PHRASES)
        
        if random.random() < 0.3:
            result = result.replace("я", "покорный слуга")
            result = result.replace("Я", "Покорный слуга")
        
        if random.random() < 0.3:
            result = result.replace("хочу", "желаю")
            result = result.replace("говорю", "изволю говорить")
            result = result.replace("иду", "шествую")
        
        if random.random() < 0.4:
            result += "..."
    
    if random.random() < 0.6:
        gentleman_endings = [" 🎩", " 🧐", " 📜", " ⚜️", " 🏛️", " 🎻", " 🕰️", " 💼"]
        result += random.choice(gentleman_endings)
    
    return result



# ── Курс валют ────────────────────────────────────────────────────

def parse_rate_input(raw: str):
    """Парсит строку вида '100usd', '36 eur', 'usd' → (amount, currency)."""
    import re
    raw = raw.strip()
    m = re.match(r"^([0-9]+(?:[.,][0-9]+)?)\s*([a-zA-Z]+)$", raw)
    if m:
        return float(m.group(1).replace(",", ".")), m.group(2).upper()
    m = re.match(r"^([a-zA-Z]+)\s*([0-9]+(?:[.,][0-9]+)?)$", raw)
    if m:
        return float(m.group(2).replace(",", ".")), m.group(1).upper()
    m = re.match(r"^([a-zA-Z]+)$", raw)
    if m:
        return None, m.group(1).upper()
    return None, None


def get_rate(raw: str) -> str:
    """Получает актуальный курс ЦБ РФ."""
    try:
        import xml.etree.ElementTree as ET

        amount, currency = parse_rate_input(raw)
        if not currency:
            return "❌ Не удалось распознать валюту.\nПримеры: .rate USD / .rate 100usd / .rate 36EUR"

        r = requests.get(
            "https://www.cbr.ru/scripts/XML_daily.asp",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        r.encoding = "windows-1251"
        root = ET.fromstring(r.text.encode("utf-8"))

        rate_rub = None
        nominal = 1
        for valute in root.findall("Valute"):
            if valute.find("CharCode").text == currency:
                nominal = int(valute.find("Nominal").text)
                value_str = valute.find("Value").text.replace(",", ".")
                rate_rub = float(value_str) / nominal
                break

        if rate_rub is None:
            return "❌ Валюта <b>" + currency + "</b> не найдена в базе ЦБ РФ.\nПримеры: USD, EUR, GBP, CNY, UAH, KZT"

        date = root.attrib.get("Date", "")

        if amount is not None:
            result_rub = amount * rate_rub
            lines = [
                "💱 <b>Конвертация (ЦБ РФ)</b>",
                "━━━━━━━━━━━━━━━",
                f"{amount:g} {currency} = <b>{result_rub:,.2f} ₽</b>",
                "",
                f"<i>Курс ЦБ на {date}: 1 {currency} = {rate_rub:.4f} ₽</i>",
            ]
        else:
            lines = [
                "💱 <b>Курс " + currency + " (ЦБ РФ на " + date + ")</b>",
                "━━━━━━━━━━━━━━━",
                f"1 {currency} = <b>{rate_rub:.4f} ₽</b>",
                f"10 {currency} = <b>{rate_rub*10:.2f} ₽</b>",
                f"100 {currency} = <b>{rate_rub*100:.2f} ₽</b>",
                f"1000 {currency} = <b>{rate_rub*1000:.2f} ₽</b>",
            ]
        return "\n".join(lines)
    except Exception as e:
        log.warning("Ошибка курса валют: " + str(e))
        return "❌ Ошибка при получении курса."


# ── Погода ────────────────────────────────────────────────────────

def get_weather(city: str) -> str:
    """Получает погоду через бесплатный wttr.in."""
    try:
        r = requests.get(
            "https://wttr.in/" + requests.utils.quote(city) + "?format=j1&lang=ru",
            headers={"User-Agent": "curl/7.0"},
            timeout=10
        )
        data = r.json()
        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        city_name = area["areaName"][0]["value"]
        country = area["country"][0]["value"]

        temp = current["temp_C"]
        feels = current["FeelsLikeC"]
        desc = current["lang_ru"][0]["value"] if current.get("lang_ru") else current["weatherDesc"][0]["value"]
        humidity = current["humidity"]
        wind = current["windspeedKmph"]
        wind_dir = current["winddir16Point"]

        emoji = "☀️"
        desc_lower = desc.lower()
        if any(w in desc_lower for w in ["дождь", "ливень", "морось"]):
            emoji = "🌧"
        elif any(w in desc_lower for w in ["снег", "метель", "пурга"]):
            emoji = "❄️"
        elif any(w in desc_lower for w in ["облач", "пасмурно", "туман"]):
            emoji = "☁️"
        elif "гроза" in desc_lower:
            emoji = "⛈"
        elif "частично" in desc_lower:
            emoji = "⛅️"

        lines = [
            emoji + " <b>Погода: " + city_name + ", " + country + "</b>",
            "━━━━━━━━━━━━━━━",
            "🌡 Температура: <b>" + temp + "°C</b> (ощущается " + feels + "°C)",
            "🌤 Состояние: " + desc,
            "💧 Влажность: " + humidity + "%",
            "💨 Ветер: " + wind + " км/ч " + wind_dir,
        ]
        return "\n".join(lines)
    except Exception as e:
        log.warning("Ошибка погоды: " + str(e))
        return "❌ Не удалось получить погоду. Проверьте название города."


# ── Самоуничтожение ───────────────────────────────────────────────

def schedule_delete(chat_id: int, msg_id: int, biz_id: str, delay: int):
    """Удаляет сообщение через delay секунд."""
    def _delete():
        time.sleep(delay)
        delete_business_msg(chat_id, msg_id, biz_id)
    threading.Thread(target=_delete, daemon=True).start()

# ── Перевод ───────────────────────────────────────────────────────

def translate_to_russian(text: str) -> str:
    """Переводит текст на русский язык через бесплатный Google Translate."""
    try:
        import urllib.parse
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": "ru",
            "dt": "t",
            "q": text,
        }
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        result = "".join(part[0] for part in data[0] if part[0])
        return result if result else "❌ Не удалось получить перевод."
    except Exception as e:
        log.warning(f"Ошибка перевода: {e}")
        return "❌ Ошибка при переводе."


# ── Спам ──────────────────────────────────────────────────────────

def run_spam(chat_id: int, biz_id: str, text: str, count: int, stop_event: threading.Event):
    sent = 0
    for i in range(count):
        if stop_event.is_set():
            break
        if send_business_msg(chat_id, biz_id, text):
            sent += 1
        time.sleep(0.5)
    log.info(f"✅ Спам завершён: {sent}/{count}")
    spam_stop.pop(chat_id, None)


# ── .info ─────────────────────────────────────────────────────────

def get_user_info_text(target_id: int) -> str:
    url = f"https://api.telegram.org/bot{TOKEN}/getChat"
    r = requests.post(url, json={"chat_id": target_id}, timeout=10)
    data = r.json()

    if not data.get("ok"):
        return f"❌ Не удалось получить информацию: {data.get('description', 'неизвестная ошибка')}"

    chat = data["result"]
    first_name = chat.get("first_name", "—")
    last_name = chat.get("last_name", "")
    username = f"@{chat['username']}" if chat.get("username") else "нет username"
    uid = chat.get("id", target_id)
    bio = chat.get("bio") or "нет биографии"
    full_name = f"{first_name} {last_name}".strip()
    profile_link = f"https://t.me/{chat['username']}" if chat.get("username") else f"tg://user?id={uid}"

    return (
        f"👤 <b>Информация о пользователе</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📛 <b>Имя:</b> {full_name}\n"
        f"🔖 <b>Юзернейм:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"📝 <b>Биография:</b> {bio}\n"
        f"🔗 <b>Профиль:</b> <a href='{profile_link}'>открыть</a>"
    )


# ── Новые сообщения ───────────────────────────────────────────────

@bot.business_message_handler(func=lambda m: True)
def handle_new(message: types.Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    user_id = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip()
    text_lower = text.lower()

    owner_id = get_owner(biz_id)
    is_owner = (user_id == owner_id)

    log.info(f"[{'ВЛАДЕЛЕЦ' if is_owner else 'СОБЕСЕДНИК'}] user={user_id} msg_id={msg_id} text={text!r}")

    if text:
        msg_cache[(chat_id, msg_id)] = text
    if message.from_user and not is_owner:
        track_name(message.from_user)

    if is_owner:
        # Обработка команд
        if text_lower == ".mute":
            if message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
                target_name = message.reply_to_message.from_user.first_name or str(target)
                edit_business_msg(chat_id, msg_id, biz_id,
                    f'<tg-emoji emoji-id="5357069174512303778">🔇</tg-emoji> {target_name} замучен.\nИспользуйте .unmute (в ответ на сообщение) для снятия мута.')
            else:
                target = -1
                edit_business_msg(chat_id, msg_id, biz_id,
                    f'<tg-emoji emoji-id="5357069174512303778">🔇</tg-emoji> Все сообщения в этом чате заглушены.\nИспользуйте .unmute для снятия мута.')
            do_mute(chat_id, target)

        elif text_lower == ".unmute":
            if message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
                target_name = message.reply_to_message.from_user.first_name or str(target)
                edit_business_msg(chat_id, msg_id, biz_id,
                    f'<tg-emoji emoji-id="5357069174512303778">🔊</tg-emoji> Мут снят с {target_name}. Сообщения снова доступны.')
            else:
                target = -1
                edit_business_msg(chat_id, msg_id, biz_id,
                    f'<tg-emoji emoji-id="5357069174512303778">🔊</tg-emoji> Мут снят. Сообщения снова доступны.')
            do_unmute(chat_id, target)

        elif text_lower == ".stop":
            if chat_id in spam_stop:
                spam_stop[chat_id].set()
                edit_business_msg(chat_id, msg_id, biz_id, "🛑 Спам остановлен.")
            else:
                edit_business_msg(chat_id, msg_id, biz_id, "ℹ️ Нет активного спама.")

        elif text_lower.startswith(".spam "):
            parts = text.split()
            if len(parts) < 3:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ Формат: .spam <текст> <количество>\nПример: .spam gg 10")
                return
            try:
                count = int(parts[-1])
            except ValueError:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ Количество должно быть числом.\nПример: .spam gg 10")
                return
            if count < 1 or count > 1000:
                edit_business_msg(chat_id, msg_id, biz_id, "❌ Количество должно быть от 1 до 1000.")
                return
            spam_text = " ".join(parts[1:-1])
            if chat_id in spam_stop:
                spam_stop[chat_id].set()
            stop_event = threading.Event()
            spam_stop[chat_id] = stop_event
            edit_business_msg(chat_id, msg_id, biz_id,
                f"📨 Запускаю спам: «{spam_text}» × {count}\nНапишите .stop для остановки.")
            threading.Thread(target=run_spam, args=(chat_id, biz_id, spam_text, count, stop_event), daemon=True).start()

        elif text_lower == ".pm":
            if italian_mode.get(chat_id, False):
                italian_mode[chat_id] = False
            if emo_mode.get(chat_id, False):
                emo_mode[chat_id] = False
            if gentleman_mode.get(chat_id, False):
                gentleman_mode[chat_id] = False
            if pikmi_mode.get(chat_id, False):
                pikmi_mode[chat_id] = False
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🌸 <b>Пикми-режим выключен</b> 🌸\nТвои сообщения снова обычные~")
            else:
                pikmi_mode[chat_id] = True
                edit_business_msg(chat_id, msg_id, biz_id,
                    "💕 <b>Пикми-режим включен!</b> 💕\n"
                    "Теперь к твоим сообщениям будут добавляться милые штучки!\n"
                    "Пиши .pm снова чтобы выключить 🎀")

        elif text_lower == ".inl":
            if pikmi_mode.get(chat_id, False):
                pikmi_mode[chat_id] = False
            if emo_mode.get(chat_id, False):
                emo_mode[chat_id] = False
            if gentleman_mode.get(chat_id, False):
                gentleman_mode[chat_id] = False
            if italian_mode.get(chat_id, False):
                italian_mode[chat_id] = False
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🇮🇹 <b>Итальянский режим выключен</b> 🇮🇹\nТвои сообщения снова обычные~")
            else:
                italian_mode[chat_id] = True
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🇮🇹 <b>Итальянский романтик-режим включен!</b> 💕\n"
                    "Теперь твои сообщения станут страстными и романтичными как в Италии!\n"
                    "Пиши .inl снова чтобы выключить 🤌")

        elif text_lower == ".emo":
            if pikmi_mode.get(chat_id, False):
                pikmi_mode[chat_id] = False
            if italian_mode.get(chat_id, False):
                italian_mode[chat_id] = False
            if gentleman_mode.get(chat_id, False):
                gentleman_mode[chat_id] = False
            if emo_mode.get(chat_id, False):
                emo_mode[chat_id] = False
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🖤 <b>Эмо-режим выключен</b> 🖤\nТвои сообщения снова обычные~")
            else:
                emo_mode[chat_id] = True
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🖤 <b>Эмо-режим включен!</b> 💔\n"
                    "Теперь твои сообщения будут наполнены глубокой печалью и философией...\n"
                    "Пиши .emo снова чтобы выключить 🥀")

        elif text_lower == ".gt":
            if pikmi_mode.get(chat_id, False):
                pikmi_mode[chat_id] = False
            if italian_mode.get(chat_id, False):
                italian_mode[chat_id] = False
            if emo_mode.get(chat_id, False):
                emo_mode[chat_id] = False
            if gentleman_mode.get(chat_id, False):
                gentleman_mode[chat_id] = False
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🎩 <b>Джентльменский режим выключен</b> 🎩\nТвои сообщения снова обычные~")
            else:
                gentleman_mode[chat_id] = True
                edit_business_msg(chat_id, msg_id, biz_id,
                    "🎩 <b>Джентльменский режим включен!</b> 🧐\n"
                    "Теперь твои сообщения будут исполнены аристократического изящества и благородства!\n"
                    "Пиши .gt снова чтобы выключить 📜")

        elif text_lower == ".tr":
            if not message.reply_to_message:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ Используйте .tr в ответ на сообщение, которое нужно перевести.")
                return
            original_text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
            if not original_text:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ В сообщении нет текста для перевода.")
                return
            edit_business_msg(chat_id, msg_id, biz_id, "🌐 Перевожу...")
            translated = translate_to_russian(original_text)
            edit_business_msg(chat_id, msg_id, biz_id,
                f"🌐 <b>Перевод на русский:</b>\n\n{translated}")

        elif text_lower.startswith(".rate"):
            arg = text[5:].strip()
            if not arg:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ Укажите валюту.\nПримеры: .rate USD / .rate 100usd / .rate 36EUR")
                return
            edit_business_msg(chat_id, msg_id, biz_id, get_rate(arg))

        elif text_lower.startswith(".weth"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ Укажите город.\nПример: .weth Москва")
                return
            edit_business_msg(chat_id, msg_id, biz_id, "🌤 Получаю погоду...")
            edit_business_msg(chat_id, msg_id, biz_id, get_weather(parts[1]))

        elif text_lower.startswith(".sec "):
            sec_text = text[5:].strip()
            if not sec_text:
                edit_business_msg(chat_id, msg_id, biz_id,
                    "❌ Укажите текст.\nПример: .sec Это секрет!")
                return
            sent_id = send_business_msg(chat_id, biz_id, "🤫 <b>Секретное сообщение</b>\n\n" + sec_text + "\n\n<i>Исчезнет через 10 секунд...</i>")
            delete_business_msg(chat_id, msg_id, biz_id)
            if sent_id:
                schedule_delete(chat_id, sent_id, biz_id, delay=10)

        elif text_lower == ".info":
            target_id = (
                message.reply_to_message.from_user.id
                if message.reply_to_message and message.reply_to_message.from_user
                else chat_id
            )
            edit_business_msg(chat_id, msg_id, biz_id, get_user_info_text(target_id))

        # ── Применяем активный режим ──────────────────────────────
        if text and not text_lower.startswith("."):
            if pikmi_mode.get(chat_id, False):
                pikmi_text = pikmi_add_to_text(text)
                edit_business_msg(chat_id, msg_id, biz_id, pikmi_text)
                log.info(f"🌸 Пикми-режим: {text!r} → {pikmi_text!r}")
            elif italian_mode.get(chat_id, False):
                italian_text = italian_add_to_text(text)
                edit_business_msg(chat_id, msg_id, biz_id, italian_text)
                log.info(f"🇮🇹 Итальянский режим: {text!r} → {italian_text!r}")
            elif emo_mode.get(chat_id, False):
                emo_text = emo_add_to_text(text)
                edit_business_msg(chat_id, msg_id, biz_id, emo_text)
                log.info(f"🖤 Эмо-режим: {text!r} → {emo_text!r}")
            elif gentleman_mode.get(chat_id, False):
                gentleman_text = gentleman_add_to_text(text)
                edit_business_msg(chat_id, msg_id, biz_id, gentleman_text)
                log.info(f"🎩 Джентльменский режим: {text!r} → {gentleman_text!r}")
        
        return

    # ── Мут для собеседника ───────────────────────────────────────
    if biz_id and (is_muted(chat_id, user_id) or is_muted(chat_id, -1)):
        msg_text = message.text or "[медиа / без текста]"
        if owner_id and message.from_user:
            notify_owner(owner_id, message.from_user, msg_text, "deleted")
        delete_business_msg(chat_id, msg_id, biz_id)


# ── Изменённые сообщения ──────────────────────────────────────────

@bot.edited_business_message_handler(func=lambda m: True)
def handle_edited(message: types.Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    user_id = message.from_user.id if message.from_user else 0

    owner_id = get_owner(biz_id)
    if user_id == owner_id:
        return

    if message.from_user:
        track_name(message.from_user)

    new_text = message.text or "[медиа / без текста]"
    old_text = msg_cache.get((chat_id, msg_id), "неизвестно")
    log.info(f"✏️ Изменено от {user_id}: {old_text!r} → {new_text!r}")
    msg_cache[(chat_id, msg_id)] = new_text

    if owner_id and message.from_user:
        notify_owner(owner_id, message.from_user, new_text, "edited", old_text)


if __name__ == "__main__":
    log.info("Бот запущен...")
    bot.infinity_polling(
        allowed_updates=["business_message", "edited_business_message", "business_connection"],
        timeout=30, long_polling_timeout=30,
    )
