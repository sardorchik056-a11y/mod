from __future__ import annotations

import functools
import html
import logging
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime

import requests
import telebot
from telebot import types

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Токен берём из переменной окружения BOT_TOKEN (в коде его хранить нельзя).
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("❌ Не задан токен бота. Укажите его в переменной окружения BOT_TOKEN.")

# num_threads=8: пока одни обработчики ждут ответ от внешних сервисов,
# остальные сообщения не зависают в очереди.
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=8)

TG_LIMIT = 4096            # максимум символов в одном сообщении Telegram
MSG_CACHE_LIMIT = 5000     # сколько последних сообщений помним (для «Было / Стало»)
NAME_HISTORY_LIMIT = 20    # сколько записей истории имён хранить на человека
SPAM_DELAY = 1.0           # пауза между сообщениями спама (Telegram режет чаще ~1/сек)

muted: dict[int, set[int]] = {}
owners: dict[str, int] = {}
msg_cache: OrderedDict[tuple, str] = OrderedDict()
spam_stop: dict[int, threading.Event] = {}
name_history: dict[int, list[dict]] = {}
chat_mode: dict[int, str] = {}   # chat_id -> ключ активного режима ("pm", "inl", "emo", "gt")

cache_lock = threading.Lock()
spam_lock = threading.Lock()
_session = requests.Session()


# ── Утилиты ───────────────────────────────────────────────────────

def esc(value) -> str:
    """Экранирует текст для HTML-режима Telegram."""
    return html.escape(str(value), quote=False)


def shorten(value, limit: int) -> str:
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def fit_escaped(raw, limit: int) -> str:
    """Экранирует текст и обрезает так, чтобы результат не превысил limit символов."""
    raw = str(raw)
    out = esc(raw)
    while len(out) > limit and raw:
        raw = raw[: max(0, int(len(raw) * limit / len(out)) - 2)]
        out = esc(raw) + "…"
    return out


def fmt_num(value: float, digits: int = 2) -> str:
    """12345.6 -> '12 345,60'."""
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def fmt_amount(value: float) -> str:
    return fmt_num(value, 0) if float(value).is_integer() else fmt_num(value, 2)


def cache_put(chat_id: int, msg_id: int, text: str):
    key = (chat_id, msg_id)
    with cache_lock:
        msg_cache[key] = text
        msg_cache.move_to_end(key)
        while len(msg_cache) > MSG_CACHE_LIMIT:
            msg_cache.popitem(last=False)


def content_of(message: types.Message) -> str:
    return (message.text or message.caption or "").strip()


def is_muted(chat_id, user_id):
    return user_id in muted.get(chat_id, set())


def do_mute(chat_id, user_id):
    muted.setdefault(chat_id, set()).add(user_id)


def do_unmute(chat_id, user_id) -> bool:
    """Снимает мут. Возвращает True, если мут действительно был."""
    users = muted.get(chat_id)
    if not users or user_id not in users:
        return False
    users.discard(user_id)
    if not users:
        muted.pop(chat_id, None)
    return True


def tg_api(method: str, payload: dict) -> dict:
    """
    Вызов Bot API. Никогда не бросает исключений и не пишет токен в логи.
    При лимите запросов (429) ждёт и повторяет попытку.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    result = {"ok": False, "description": "нет ответа от Telegram"}
    for _ in range(3):
        try:
            data = _session.post(url, json=payload, timeout=15).json()
        except (requests.RequestException, ValueError) as e:
            log.warning(f"{method}: сетевая ошибка ({type(e).__name__})")
            return {"ok": False, "description": "сетевая ошибка"}
        if data.get("ok"):
            return data
        result = data
        if data.get("error_code") == 429:
            wait = data.get("parameters", {}).get("retry_after", 1)
            log.warning(f"{method}: лимит Telegram, жду {wait} с")
            time.sleep(min(int(wait), 30) + 0.5)
            continue
        break
    return result


def get_owner(biz_id):
    if not biz_id:
        return None
    if biz_id in owners:
        return owners[biz_id]
    try:
        bc = bot.get_business_connection(biz_id)
        owners[biz_id] = bc.user.id
        log.info(f"Владелец определён: {bc.user.id}")
        return bc.user.id
    except Exception as e:
        log.warning(f"Не удалось определить владельца: {type(e).__name__}")
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
    del history[:-NAME_HISTORY_LIMIT]
    log.info(f"📝 История имён: {uid} → {full_name} / @{username}")


def delete_business_msg(chat_id: int, msg_id: int, biz_id: str) -> bool:
    data = tg_api("deleteBusinessMessages", {
        "business_connection_id": biz_id,
        "chat_id": chat_id,
        "message_ids": [msg_id],
    })
    if not data.get("ok"):
        log.warning(f"❌ Ошибка удаления: {data.get('description')}")
    return bool(data.get("ok"))


def edit_business_msg(chat_id: int, msg_id: int, biz_id: str, text: str) -> bool:
    if len(text) > TG_LIMIT:
        log.warning("❌ Ошибка редактирования: текст длиннее лимита Telegram")
        return False
    data = tg_api("editMessageText", {
        "business_connection_id": biz_id,
        "chat_id": chat_id,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
    })
    if data.get("ok"):
        return True
    desc = str(data.get("description", ""))
    if "not modified" in desc:   # текст уже такой же — это не ошибка
        return True
    log.warning(f"❌ Ошибка редактирования: {desc}")
    return False


def send_business_msg(chat_id: int, biz_id: str, text: str) -> int | None:
    if len(text) > TG_LIMIT:
        log.warning("❌ Ошибка отправки: текст длиннее лимита Telegram")
        return None
    data = tg_api("sendMessage", {
        "business_connection_id": biz_id,
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    })
    if data.get("ok"):
        return data["result"]["message_id"]
    log.warning(f"❌ Ошибка отправки: {data.get('description')}")
    return None


def format_user(from_user):
    username = f"@{from_user.username}" if from_user.username else "нет username"
    full_name = from_user.first_name or ""
    if from_user.last_name:
        full_name += f" {from_user.last_name}"
    return full_name.strip(), username


def notify_owner(owner_id: int, from_user, text: str, event: str, old_text: str = None):
    full_name, username = format_user(from_user)
    who = f"{esc(full_name) or 'Без имени'} ({esc(username)})"
    if event == "deleted":
        body = (
            "🗑 <b>Сообщение удалено (мут)</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f'<tg-emoji emoji-id="5260399854500191689">👤</tg-emoji> От: {who}\n'
            f"🆔 ID: <code>{from_user.id}</code>\n"
            f"💬 Текст:\n{fit_escaped(text, 3400)}"
        )
    else:
        old = old_text or "неизвестно (сообщение пришло до запуска бота)"
        body = (
            "✏️ <b>Сообщение изменено</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f'<tg-emoji emoji-id="5260399854500191689">👤</tg-emoji> От: {who}\n'
            f"🆔 ID: <code>{from_user.id}</code>\n"
            f'<tg-emoji emoji-id="6030776052345737530">📄</tg-emoji> Было:\n{fit_escaped(old, 1600)}\n\n'
            f'<tg-emoji emoji-id="5258215846450305872">✏️</tg-emoji> Стало:\n{fit_escaped(text, 1600)}'
        )
    try:
        bot.send_message(owner_id, body, parse_mode="HTML")
    except Exception as e:
        log.warning(f"Не удалось уведомить владельца: {type(e).__name__}")


def normalize(text: str) -> str:
    """Для сравнения с готовыми фразами: без регистра, знаков препинания и лишних пробелов."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def decorate_random_word(text: str, decorate) -> str:
    """Применяет decorate к случайному слову, не трогая пробелы и переносы строк."""
    parts = re.split(r"(\s+)", text)
    idxs = [i for i, p in enumerate(parts) if p and not p.isspace()]
    if not idxs:
        return text
    i = random.choice(idxs)
    parts[i] = decorate(parts[i])
    return "".join(parts)


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

CYR_VOWELS = re.compile(r"[аоуыэяёюиеАОУЫЭЯЁЮИЕ]")


def pikmi_add_to_text(original_text: str) -> str:
    result = original_text

    if random.random() < 0.25:
        matches = list(CYR_VOWELS.finditer(result))
        if matches:
            m = random.choice(matches)
            result = result[: m.end()] + m.group(0) + result[m.end():]

    if random.random() < 0.4:
        result = decorate_random_word(result, lambda w: w + random.choice(PIKMI_HEARTS))

    if random.random() < 0.3:
        result = decorate_random_word(result, lambda w: random.choice(PIKMI_HEARTS) + w)

    if random.random() < 0.5:
        result = random.choice(PIKMI_PREFIXES) + result

    if random.random() < 0.7:
        result += random.choice(PIKMI_ADDONS)

    if random.random() < 0.6:
        result += " " + " ".join(random.choices(PIKMI_HEARTS, k=random.randint(1, 4)))

    if random.random() < 0.4:
        result += " " + random.choice(PIKMI_EMOJI_MIX)

    if random.random() < 0.2:
        result = f"({result})"

    if random.random() < 0.3 and result:
        result = result[0].upper() + result[1:]

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
    " Sei speciale! 🎀", " Con tutto il cuore! 💝", " Mamma mia, che amore! 🍝",
]

ITALIAN_GESTURES = [
    " 🤌", " 🇮🇹", " 💋", " 🤌🇮🇹", " 👉🇮🇹", " 💕🤌",
    " 🍕🤌", " 🍝💋", " 🌹🤌", " 💕🇮🇹", " 🤌✨", " 💋🇮🇹",
]

# Готовые ответы подставляются, только если сообщение целиком состоит из этого слова.
ITALIAN_WORDS_REPLACE = {
    "привет": "Ciao bella! 🇮🇹",
    "пока": "Ciao ciao, amore! 💕",
    "спасибо": "Grazie mille, tesoro! 🌹",
    "люблю": "Ti amo con tutto il cuore! 💋",
    "хорошо": "Perfetto, amore mio! ✨",
    "красиво": "Bellissimo! 🤌🇮🇹",
    "да": "Sì, amore! 💕",
    "нет": "No, dolcezza... 💔",
}


def italian_add_to_text(original_text: str) -> str:
    canned = ITALIAN_WORDS_REPLACE.get(normalize(original_text))
    if canned:
        return canned

    result = original_text

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
        result += random.choice(["!", "!!", "!!!", "~", "~~", "~~~"])

    return result


# ── Эмо-режим ─────────────────────────────────────────────────────

EMO_PREFIXES = [
    "Никто не поймёт... ", "В этой тьме... ", "Моя душа плачет... ",
    "Снова эта боль... ", "Зачем я здесь... ", "Пустота внутри... ",
    "Тьма окутывает... ", "Слёзы на ветру... ", "Разбитое сердце... ",
    "Одиночество... ", "Мрак... ", "Безысходность... ", "Депрессия... ",
]

EMO_SUFFIXES = [
    " ...никто не поймёт", " ...боль внутри", " ...слёзы текут",
    " ...тьма поглощает", " ...пустота", " ...безысходность",
    " ...зачем всё это", " ...нет смысла", " ...одиночество давит",
    " ...разбито навсегда", " ...эмоции прочь", " ...ничего не важно",
]

EMO_PHRASES = [
    " 🖤", " 💔", " 🥀", " 🌑", " 🌙", " 🕷️", " 🕸️", " ⚰️",
    " вЕчНыЙ мРаК...", " дУшА бОлИт...", " сЕрДцЕ рВаНоЕ...",
    " никто не поймёт этой боли", " в мире нет счастья", " все против меня",
]

EMO_SAD_ENDINGS = [" 🖤", " 💔", " 🥀", " 🌑", " 🌙", " 🕷️", " 🕸️", " ⚰️", " 💧", " 😢", " 😭"]

EMO_WORDS_REPLACE = {
    "привет": "привет... если тебе есть до меня дело... 🖤",
    "пока": "прощай... в пустоту... 💔",
    "спасибо": "какое спасибо... мне ничего не нужно... 🥀",
    "люблю": "разве можно любить такое чудовище... 💔",
    "хорошо": "хорошо? что такое хорошо? пустота... 🖤",
    "плохо": "плохо? всегда плохо... всегда больно... 💔",
    "круто": "круто? ничто не круто в этом мире... 🥀",
    "смешно": "смех сквозь слёзы... никто не понимает... 💧",
    "да": "да... наверное... хотя какая разница... 🖤",
    "нет": "нет... никогда... всё бессмысленно... 💔",
}


def emo_add_to_text(original_text: str) -> str:
    canned = EMO_WORDS_REPLACE.get(normalize(original_text))
    if canned:
        return canned

    result = original_text

    if random.random() < 0.5:
        result = random.choice(EMO_PREFIXES) + result.lower()
    elif random.random() < 0.4:
        result = "".join(
            c.upper() if c.islower() else c.lower() if random.random() < 0.3 else c
            for c in result
        ) if False else "".join(
            (c.swapcase() if random.random() < 0.3 else c) for c in result
        )

    if random.random() < 0.6:
        result += random.choice(EMO_SUFFIXES)

    if random.random() < 0.7:
        result += random.choice(EMO_PHRASES)

    if random.random() < 0.5:
        result += random.choice(["..", "...", "....", "....."])

    if random.random() < 0.8:
        result += random.choice(EMO_SAD_ENDINGS)

    return result


# ── Джентльменский режим (аристократ) ─────────────────────────────

GENTLEMAN_PREFIXES = [
    "Сударь, ", "Сударыня, ", "Милостивый государь, ", "Дражайший, ", "Дражайшая, ",
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
    " Извольте! ⚜️", " Браво! 🏛️", " Истинно так! 🎩", " Глубочайшее почтение! 🧐",
    " С премногим удовольствием! 📜", " Покорнейше благодарствую! ⚜️",
]

GENTLEMAN_ENDINGS = [" 🎩", " 🧐", " 📜", " ⚜️", " 🏛️", " 🎻", " 🕰️", " 💼"]

GENTLEMAN_WORDS_REPLACE = {
    "привет": "Честь имею приветствовать вас, сударь! 🎩",
    "здравствуй": "Нижайший поклон вам, милостивый государь! 🧐",
    "здравствуйте": "Нижайший поклон вам, милостивый государь! 🧐",
    "пока": "Позвольте откланяться, честь имею! 📜",
    "до свидания": "С чувством глубокого уважения прощаюсь с вами! ⚜️",
    "спасибо": "Покорнейше благодарю, сударь! Весьма признателен! 🎩",
    "пожалуйста": "Помилуйте, не стоит благодарности! 🧐",
    "да": "Истинно так, милостивый государь! 📜",
    "нет": "Увы, вынужден отказать, сударь... 🎩",
    "хорошо": "Весьма недурно, премного одобряю! ⚜️",
    "отлично": "Браво! Пальму первенства вам вручаю! 🏛️",
    "извини": "Помилуйте, вину свою признаю и прощения прошу! 🧐",
    "извините": "Помилуйте, вину свою признаю и прощения прошу! 🧐",
}

# Замены только целых слов (раньше «я» заменялось даже внутри слов).
GENTLEMAN_REPLACEMENTS = [
    (r"\bя\b", "покорный слуга"),
    (r"\bхочу\b", "желаю"),
    (r"\bговорю\b", "изволю говорить"),
    (r"\bиду\b", "шествую"),
]


def _replace_word(text: str, pattern: str, replacement: str) -> str:
    def _sub(m):
        word = m.group(0)
        return replacement.capitalize() if word[:1].isupper() else replacement
    return re.sub(pattern, _sub, text, flags=re.IGNORECASE)


def gentleman_add_to_text(original_text: str) -> str:
    """Добавляет джентльменские/аристократические эффекты к тексту."""
    canned = GENTLEMAN_WORDS_REPLACE.get(normalize(original_text))
    if canned:
        return canned

    result = original_text

    if random.random() < 0.6:
        result = random.choice(GENTLEMAN_PREFIXES) + result

    if random.random() < 0.5:
        result += random.choice(GENTLEMAN_SUFFIXES)

    if random.random() < 0.5:
        result += random.choice(GENTLEMAN_PHRASES)

    if random.random() < 0.3:
        for pattern, replacement in GENTLEMAN_REPLACEMENTS:
            result = _replace_word(result, pattern, replacement)

    if random.random() < 0.4:
        result += "..."

    if random.random() < 0.6:
        result += random.choice(GENTLEMAN_ENDINGS)

    return result


# ── Режимы: описание и переключение ───────────────────────────────

MODES = {
    "pm": {
        "title": "Пикми-режим",
        "icon": "🌸",
        "transform": pikmi_add_to_text,
        "on": (
            "💕 <b>Пикми-режим включён</b> 💕\n"
            "К вашим сообщениям будут добавляться милые штучки.\n"
            "Остальные режимы отключены. Чтобы выключить, отправьте .pm ещё раз 🎀"
        ),
        "off": "🌸 <b>Пикми-режим выключен</b> 🌸\nСообщения снова отправляются без изменений.",
    },
    "inl": {
        "title": "Итальянский режим",
        "icon": "🇮🇹",
        "transform": italian_add_to_text,
        "on": (
            "🇮🇹 <b>Итальянский романтик-режим включён</b> 💕\n"
            "Ваши сообщения станут страстными и романтичными, как в Италии.\n"
            "Остальные режимы отключены. Чтобы выключить, отправьте .inl ещё раз 🤌"
        ),
        "off": "🇮🇹 <b>Итальянский режим выключен</b> 🇮🇹\nСообщения снова отправляются без изменений.",
    },
    "emo": {
        "title": "Эмо-режим",
        "icon": "🖤",
        "transform": emo_add_to_text,
        "on": (
            "🖤 <b>Эмо-режим включён</b> 💔\n"
            "Ваши сообщения наполнятся глубокой печалью и философией...\n"
            "Остальные режимы отключены. Чтобы выключить, отправьте .emo ещё раз 🥀"
        ),
        "off": "🖤 <b>Эмо-режим выключен</b> 🖤\nСообщения снова отправляются без изменений.",
    },
    "gt": {
        "title": "Джентльменский режим",
        "icon": "🎩",
        "transform": gentleman_add_to_text,
        "on": (
            "🎩 <b>Джентльменский режим включён</b> 🧐\n"
            "Ваши сообщения обретут аристократическое изящество и благородство.\n"
            "Остальные режимы отключены. Чтобы выключить, отправьте .gt ещё раз 📜"
        ),
        "off": "🎩 <b>Джентльменский режим выключен</b> 🎩\nСообщения снова отправляются без изменений.",
    },
}


def toggle_mode(chat_id: int, key: str) -> str:
    """Включает режим (выключая остальные) или выключает его, если он уже активен."""
    if chat_mode.get(chat_id) == key:
        chat_mode.pop(chat_id, None)
        return MODES[key]["off"]
    chat_mode[chat_id] = key
    return MODES[key]["on"]


# ── Курс валют ────────────────────────────────────────────────────

RATE_USAGE = "Примеры: .rate USD / .rate 100usd / .rate 36EUR"


def parse_rate_input(raw: str):
    """Парсит строку вида '100usd', '36 eur', 'usd' → (amount, currency)."""
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
    amount, currency = parse_rate_input(raw)
    if not currency:
        return "❌ Не удалось распознать валюту.\n" + RATE_USAGE
    if currency == "RUB":
        return "ℹ️ Рубль — базовая валюта, курс считается относительно него.\nУкажите другую валюту: USD, EUR, CNY..."

    try:
        r = _session.get(
            "https://www.cbr.ru/scripts/XML_daily.asp",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)

        rate_rub = None
        for valute in root.findall("Valute"):
            if valute.find("CharCode").text == currency:
                nominal = int(valute.find("Nominal").text)
                rate_rub = float(valute.find("Value").text.replace(",", ".")) / nominal
                break
        date = root.attrib.get("Date", "")
    except requests.RequestException as e:
        log.warning(f"Курс валют: ошибка соединения ({type(e).__name__})")
        return "❌ Не удалось связаться с сервером ЦБ РФ. Попробуйте чуть позже."
    except (ET.ParseError, ValueError, AttributeError) as e:
        log.warning(f"Курс валют: неожиданный ответ ЦБ ({type(e).__name__})")
        return "❌ Не удалось обработать ответ ЦБ РФ. Попробуйте чуть позже."

    if rate_rub is None:
        return (
            f"❌ Валюта <b>{esc(currency)}</b> не найдена в базе ЦБ РФ.\n"
            "Доступны, например: USD, EUR, GBP, CNY, UAH, KZT"
        )

    if amount is not None:
        lines = [
            "💱 <b>Конвертация (ЦБ РФ)</b>",
            "━━━━━━━━━━━━━━━",
            f"{fmt_amount(amount)} {currency} = <b>{fmt_num(amount * rate_rub)} ₽</b>",
            "",
            f"<i>Курс ЦБ на {esc(date)}: 1 {currency} = {fmt_num(rate_rub, 4)} ₽</i>",
        ]
    else:
        lines = [
            f"💱 <b>Курс {currency} (ЦБ РФ на {esc(date)})</b>",
            "━━━━━━━━━━━━━━━",
            f"1 {currency} = <b>{fmt_num(rate_rub, 4)} ₽</b>",
            f"10 {currency} = <b>{fmt_num(rate_rub * 10)} ₽</b>",
            f"100 {currency} = <b>{fmt_num(rate_rub * 100)} ₽</b>",
            f"1000 {currency} = <b>{fmt_num(rate_rub * 1000)} ₽</b>",
        ]
    return "\n".join(lines)


# ── Погода ────────────────────────────────────────────────────────

WIND_DIRS = {
    "N": "С", "NNE": "ССВ", "NE": "СВ", "ENE": "ВСВ",
    "E": "В", "ESE": "ВЮВ", "SE": "ЮВ", "SSE": "ЮЮВ",
    "S": "Ю", "SSW": "ЮЮЗ", "SW": "ЮЗ", "WSW": "ЗЮЗ",
    "W": "З", "WNW": "ЗСЗ", "NW": "СЗ", "NNW": "ССЗ",
}


def weather_emoji(desc: str) -> str:
    d = desc.lower()
    if "гроза" in d:
        return "⛈"
    if any(w in d for w in ("снег", "метель", "пурга")):
        return "❄️"
    if any(w in d for w in ("дождь", "ливень", "морось")):
        return "🌧"
    if "частично" in d or "переменн" in d:
        return "⛅️"
    if any(w in d for w in ("облач", "пасмурно", "туман", "дымка")):
        return "☁️"
    if any(w in d for w in ("ясно", "солнечно")):
        return "☀️"
    return "🌤"


def get_weather(city: str) -> str:
    """Получает погоду через бесплатный wttr.in."""
    not_found = "❌ Город не найден. Проверьте название и попробуйте ещё раз.\nПример: .weth Москва"
    try:
        r = _session.get(
            "https://wttr.in/" + requests.utils.quote(city) + "?format=j1&lang=ru",
            headers={"User-Agent": "curl/7.0"},
            timeout=10,
        )
        if r.status_code == 404:
            return not_found
        r.raise_for_status()
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
        wind_dir = WIND_DIRS.get(current["winddir16Point"], current["winddir16Point"])
    except requests.RequestException as e:
        log.warning(f"Погода: ошибка соединения ({type(e).__name__})")
        return "❌ Сервис погоды сейчас недоступен. Попробуйте чуть позже."
    except (ValueError, KeyError, IndexError, TypeError) as e:
        log.warning(f"Погода: неожиданный ответ ({type(e).__name__})")
        return not_found

    lines = [
        f"{weather_emoji(desc)} <b>Погода: {esc(city_name)}, {esc(country)}</b>",
        "━━━━━━━━━━━━━━━",
        f"🌡 Температура: <b>{esc(temp)}°C</b> (ощущается как {esc(feels)}°C)",
        f"🌤 Состояние: {esc(desc)}",
        f"💧 Влажность: {esc(humidity)}%",
        f"💨 Ветер: {esc(wind)} км/ч, {esc(wind_dir)}",
    ]
    return "\n".join(lines)


# ── Самоуничтожение ───────────────────────────────────────────────

def schedule_delete(chat_id: int, msg_id: int, biz_id: str, delay: int):
    """Удаляет сообщение через delay секунд."""
    timer = threading.Timer(delay, delete_business_msg, args=(chat_id, msg_id, biz_id))
    timer.daemon = True
    timer.start()


# ── Перевод ───────────────────────────────────────────────────────

TRANSLATE_LIMIT = 3000


def translate_to_russian(text: str) -> str:
    """Переводит текст на русский язык через бесплатный Google Translate."""
    try:
        r = _session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text[:TRANSLATE_LIMIT]},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        result = "".join(part[0] for part in data[0] if part and part[0])
    except requests.RequestException as e:
        log.warning(f"Перевод: ошибка соединения ({type(e).__name__})")
        return "❌ Сервис перевода сейчас недоступен. Попробуйте чуть позже."
    except (ValueError, KeyError, IndexError, TypeError) as e:
        log.warning(f"Перевод: неожиданный ответ ({type(e).__name__})")
        return "❌ Не удалось получить перевод."
    return fit_escaped(result, 3800) if result else "❌ Не удалось получить перевод."


# ── Спам ──────────────────────────────────────────────────────────

def run_spam(chat_id: int, biz_id: str, text: str, count: int, stop_event: threading.Event):
    sent = 0
    failures = 0
    try:
        for _ in range(count):
            if stop_event.is_set():
                break
            if send_business_msg(chat_id, biz_id, text):
                sent += 1
                failures = 0
            else:
                failures += 1
                if failures >= 5:
                    log.warning("⚠️ Спам остановлен: 5 неудачных отправок подряд")
                    break
            stop_event.wait(SPAM_DELAY)   # в отличие от sleep, прерывается командой .stop сразу
    finally:
        with spam_lock:
            if spam_stop.get(chat_id) is stop_event:
                spam_stop.pop(chat_id, None)
        log.info(f"✅ Спам завершён: {sent}/{count}")


# ── .info ─────────────────────────────────────────────────────────

def get_user_info_text(target_id: int) -> str:
    data = tg_api("getChat", {"chat_id": target_id})

    if not data.get("ok"):
        reason = esc(data.get("description", "неизвестная ошибка"))
        return f"❌ Не удалось получить информацию о пользователе.\n<i>{reason}</i>"

    chat = data["result"]
    first_name = chat.get("first_name", "—")
    last_name = chat.get("last_name", "")
    username = f"@{chat['username']}" if chat.get("username") else "нет username"
    uid = chat.get("id", target_id)
    bio = chat.get("bio") or "не указана"
    full_name = f"{first_name} {last_name}".strip()
    profile_link = f"https://t.me/{chat['username']}" if chat.get("username") else f"tg://user?id={uid}"

    return (
        "👤 <b>Информация о пользователе</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"📛 <b>Имя:</b> {esc(full_name)}\n"
        f"🔖 <b>Юзернейм:</b> {esc(username)}\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"📝 <b>О себе:</b> {esc(bio)}\n"
        f'🔗 <b>Профиль:</b> <a href="{profile_link}">открыть</a>'
    )


# ── Команды владельца ─────────────────────────────────────────────

# Команды без аргументов: если после них есть текст, это обычное сообщение.
SIMPLE_COMMANDS = {".mute", ".unmute", ".stop", ".pm", ".inl", ".emo", ".gt", ".tr", ".info"}
# Команды с аргументами.
ARG_COMMANDS = {".spam", ".rate", ".weth", ".sec"}


def parse_command(text: str):
    """Возвращает (команда, аргументы) или (None, '') для обычного текста."""
    if not text.startswith("."):
        return None, ""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ARG_COMMANDS:
        return cmd, args
    if cmd in SIMPLE_COMMANDS and not args:
        return cmd, ""
    return None, ""


def safe(handler):
    """Ошибка в одном обработчике не должна ломать работу бота."""
    @functools.wraps(handler)
    def wrapper(message):
        try:
            handler(message)
        except Exception:
            log.exception(f"Ошибка в обработчике {handler.__name__}")
    return wrapper


def handle_owner_command(message: types.Message, cmd: str, args: str):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    owner_id = message.from_user.id if message.from_user else 0
    replied = message.reply_to_message
    replied_user = replied.from_user if replied and replied.from_user else None

    def reply(text: str):
        edit_business_msg(chat_id, msg_id, biz_id, text)

    if cmd == ".mute":
        if replied_user:
            target = replied_user.id
            if target == owner_id:
                reply("❌ Нельзя заглушить самого себя.")
                return
            name = esc(replied_user.first_name or target)
            do_mute(chat_id, target)
            reply(
                f'<tg-emoji emoji-id="5357069174512303778">🔇</tg-emoji> <b>{name}</b> заглушён.\n'
                "Его сообщения будут удаляться. Чтобы снять мут, отправьте .unmute в ответ на его сообщение."
            )
        else:
            do_mute(chat_id, -1)
            reply(
                '<tg-emoji emoji-id="5357069174512303778">🔇</tg-emoji> Все сообщения собеседника в этом чате заглушены.\n'
                "Чтобы снять мут, отправьте .unmute."
            )

    elif cmd == ".unmute":
        if replied_user:
            target = replied_user.id
            name = esc(replied_user.first_name or target)
            if do_unmute(chat_id, target):
                reply(
                    f'<tg-emoji emoji-id="5357069174512303778">🔊</tg-emoji> Мут с <b>{name}</b> снят. '
                    "Сообщения снова доступны."
                )
            else:
                reply(f"ℹ️ <b>{name}</b> не был заглушён.")
        else:
            if do_unmute(chat_id, -1):
                reply(
                    '<tg-emoji emoji-id="5357069174512303778">🔊</tg-emoji> Мут снят. '
                    "Сообщения снова доступны."
                )
            else:
                reply(
                    "ℹ️ Общий мут в этом чате не включён.\n"
                    "Чтобы снять мут с конкретного человека, отправьте .unmute в ответ на его сообщение."
                )

    elif cmd == ".stop":
        with spam_lock:
            event = spam_stop.get(chat_id)
        if event:
            event.set()
            reply("🛑 Спам остановлен.")
        else:
            reply("ℹ️ В этом чате нет активного спама.")

    elif cmd == ".spam":
        usage = "Формат: .spam <текст> <количество>\nПример: .spam gg 10"
        pair = args.rsplit(None, 1)
        if len(pair) < 2:
            reply("❌ Не хватает данных.\n" + usage)
            return
        spam_text, count_raw = pair[0].strip(), pair[1]
        try:
            count = int(count_raw)
        except ValueError:
            reply("❌ Количество должно быть целым числом.\n" + usage)
            return
        if count < 1 or count > 1000:
            reply("❌ Количество должно быть от 1 до 1000.")
            return
        if len(spam_text) > 4000:
            reply("❌ Текст слишком длинный (максимум 4000 символов).")
            return
        with spam_lock:
            old = spam_stop.get(chat_id)
            if old:
                old.set()
            stop_event = threading.Event()
            spam_stop[chat_id] = stop_event
        reply(
            f"📨 <b>Спам запущен</b>: «{esc(shorten(spam_text, 200))}» × {count}\n"
            "Чтобы остановить, отправьте .stop"
        )
        threading.Thread(
            target=run_spam, args=(chat_id, biz_id, spam_text, count, stop_event), daemon=True
        ).start()

    elif cmd in (".pm", ".inl", ".emo", ".gt"):
        reply(toggle_mode(chat_id, cmd[1:]))

    elif cmd == ".tr":
        if not replied:
            reply("❌ Отправьте .tr в ответ на сообщение, которое нужно перевести.")
            return
        original_text = content_of(replied)
        if not original_text:
            reply("❌ В этом сообщении нет текста для перевода.")
            return
        reply("🌐 Перевожу...")
        translated = translate_to_russian(original_text)
        if translated.startswith("❌"):
            reply(translated)
        else:
            reply(f"🌐 <b>Перевод на русский:</b>\n\n{translated}")

    elif cmd == ".rate":
        if not args:
            reply("❌ Укажите валюту.\n" + RATE_USAGE)
            return
        reply(get_rate(args))

    elif cmd == ".weth":
        if not args:
            reply("❌ Укажите город.\nПример: .weth Москва")
            return
        reply("🌤 Получаю погоду...")
        reply(get_weather(args))

    elif cmd == ".sec":
        if not args:
            reply("❌ Укажите текст.\nПример: .sec Это секрет!")
            return
        sent_id = send_business_msg(
            chat_id, biz_id,
            f"🤫 <b>Секретное сообщение</b>\n\n{fit_escaped(args, 3800)}\n\n<i>Исчезнет через 10 секунд...</i>",
        )
        if sent_id:
            delete_business_msg(chat_id, msg_id, biz_id)
            schedule_delete(chat_id, sent_id, biz_id, delay=10)
        else:
            reply("❌ Не удалось отправить секретное сообщение. Попробуйте ещё раз.")

    elif cmd == ".info":
        target_id = replied_user.id if replied_user else chat_id
        reply(get_user_info_text(target_id))


def apply_active_mode(message: types.Message, text: str):
    """Если в чате включён режим, переписывает обычное сообщение владельца."""
    key = chat_mode.get(message.chat.id)
    if not key:
        return
    mode = MODES[key]
    new_text = esc(mode["transform"](text))   # экранируем уже готовый текст
    if len(new_text) > TG_LIMIT:
        log.info(f"{mode['icon']} {mode['title']}: результат слишком длинный, сообщение оставлено как есть")
        return
    edit_business_msg(message.chat.id, message.message_id, message.business_connection_id, new_text)
    log.info(f"{mode['icon']} {mode['title']}: {shorten(text, 60)!r} → {shorten(new_text, 60)!r}")


# ── Новые сообщения ───────────────────────────────────────────────

@bot.business_message_handler(func=lambda m: True)
@safe
def handle_new(message: types.Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    user_id = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip()

    owner_id = get_owner(biz_id)
    is_owner = owner_id is not None and user_id == owner_id

    log.info(f"[{'ВЛАДЕЛЕЦ' if is_owner else 'СОБЕСЕДНИК'}] user={user_id} msg_id={msg_id} text={shorten(text, 60)!r}")

    stored = content_of(message)
    if stored:
        cache_put(chat_id, msg_id, stored)
    if message.from_user and not is_owner:
        track_name(message.from_user)

    if is_owner:
        if not text:
            return
        cmd, args = parse_command(text)
        if cmd:
            handle_owner_command(message, cmd, args)
        else:
            apply_active_mode(message, text)
        return

    # ── Мут для собеседника ───────────────────────────────────────
    # Без известного владельца ничего не удаляем, чтобы случайно не задеть его сообщения.
    if owner_id is None or not biz_id:
        return
    if is_muted(chat_id, user_id) or is_muted(chat_id, -1):
        if delete_business_msg(chat_id, msg_id, biz_id) and message.from_user:
            notify_owner(owner_id, message.from_user, stored or "[медиа без текста]", "deleted")


# ── Изменённые сообщения ──────────────────────────────────────────

@bot.edited_business_message_handler(func=lambda m: True)
@safe
def handle_edited(message: types.Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    user_id = message.from_user.id if message.from_user else 0

    owner_id = get_owner(biz_id)
    if owner_id is None or user_id == owner_id:
        return

    if message.from_user:
        track_name(message.from_user)

    new_text = content_of(message) or "[медиа без текста]"
    old_text = msg_cache.get((chat_id, msg_id))
    log.info(f"✏️ Изменено от {user_id}: {shorten(old_text or 'неизвестно', 60)!r} → {shorten(new_text, 60)!r}")
    cache_put(chat_id, msg_id, new_text)

    if message.from_user:
        notify_owner(owner_id, message.from_user, new_text, "edited", old_text)


if __name__ == "__main__":
    try:
        bot.remove_webhook()   # если когда-то был вебхук, polling вернёт ошибку 409
    except Exception as e:
        log.warning(f"Не удалось сбросить вебхук: {type(e).__name__}")
    log.info("Бот запущен...")
    try:
        bot.infinity_polling(
            skip_pending=True,   # не выполнять старые команды, накопившиеся пока бот был выключен
            allowed_updates=["business_message", "edited_business_message", "business_connection"],
            timeout=30, long_polling_timeout=30,
        )
    except KeyboardInterrupt:
        log.info("Бот остановлен.")
