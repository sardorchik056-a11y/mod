from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BusinessConnection, Message, User

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)   # не спамить «Update handled» в лог
log = logging.getLogger(__name__)

# Токен бота прописан прямо в коде. Вставьте свой токен от @BotFather между кавычками.
TOKEN = "8693034024:AAEjOqhChUGq8IvZHYIOw2-RcfJLSyK7ZBI"

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

TG_LIMIT = 4096            # максимум символов в одном сообщении Telegram
MSG_CACHE_LIMIT = 5000     # сколько последних сообщений помним (для «Было / Стало»)
NAME_HISTORY_LIMIT = 20    # сколько записей истории имён хранить на человека
SPAM_DELAY = 1.0           # пауза между сообщениями спама (Telegram режет чаще ~1/сек)

muted: dict[int, set[int]] = {}
owners: dict[str, int] = {}
msg_cache: OrderedDict[tuple, str] = OrderedDict()
spam_stop: dict[int, asyncio.Event] = {}
name_history: dict[int, list[dict]] = {}
chat_mode: dict[int, str] = {}   # chat_id -> ключ активного режима ("pm", "inl", "emo", "gt")

background_tasks: set[asyncio.Task] = set()   # чтобы фоновые задачи не собрал сборщик мусора
_http: aiohttp.ClientSession | None = None


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
    msg_cache[key] = text
    msg_cache.move_to_end(key)
    while len(msg_cache) > MSG_CACHE_LIMIT:
        msg_cache.popitem(last=False)


def http() -> aiohttp.ClientSession:
    """Общая HTTP-сессия для внешних сервисов (курс, погода, перевод)."""
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _http


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def content_of(message: Message) -> str:
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


async def tg_call(label: str, make_call):
    """
    Вызов Bot API без исключений. Возвращает (результат, None) или (None, описание_ошибки).
    Не пишет токен в логи. При лимите запросов (429) ждёт и повторяет попытку.
    """
    error = "нет ответа от Telegram"
    for _ in range(3):
        try:
            return await make_call(), None
        except TelegramRetryAfter as e:
            wait = min(int(e.retry_after), 30)
            log.warning(f"{label}: лимит Telegram, жду {wait} с")
            error = "лимит запросов Telegram"
            await asyncio.sleep(wait + 0.5)
        except TelegramNetworkError as e:
            log.warning(f"{label}: сетевая ошибка ({type(e).__name__})")
            return None, "сетевая ошибка"
        except TelegramAPIError as e:
            return None, str(e.message)
    return None, error


async def get_owner(biz_id):
    if not biz_id:
        return None
    if biz_id in owners:
        return owners[biz_id]
    try:
        bc = await bot.get_business_connection(biz_id)
    except Exception as e:
        log.warning(f"Не удалось определить владельца: {type(e).__name__}")
        return None
    owners[biz_id] = bc.user.id
    log.info(f"Владелец определён: {bc.user.id}")
    return bc.user.id


def track_name(user: User):
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


async def delete_business_msg(chat_id: int, msg_id: int, biz_id: str) -> bool:
    _, err = await tg_call(
        "deleteBusinessMessages",
        lambda: bot.delete_business_messages(business_connection_id=biz_id, message_ids=[msg_id]),
    )
    if err is not None:
        log.warning(f"❌ Ошибка удаления: {err}")
        return False
    return True


async def edit_business_msg(chat_id: int, msg_id: int, biz_id: str, text: str) -> bool:
    if len(text) > TG_LIMIT:
        log.warning("❌ Ошибка редактирования: текст длиннее лимита Telegram")
        return False
    _, err = await tg_call(
        "editMessageText",
        lambda: bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=msg_id, business_connection_id=biz_id
        ),
    )
    if err is None:
        return True
    if "not modified" in err:   # текст уже такой же — это не ошибка
        return True
    log.warning(f"❌ Ошибка редактирования: {err}")
    return False


async def send_business_msg(chat_id: int, biz_id: str, text: str) -> int | None:
    if len(text) > TG_LIMIT:
        log.warning("❌ Ошибка отправки: текст длиннее лимита Telegram")
        return None
    sent, err = await tg_call(
        "sendMessage",
        lambda: bot.send_message(chat_id=chat_id, text=text, business_connection_id=biz_id),
    )
    if err is None:
        return sent.message_id
    log.warning(f"❌ Ошибка отправки: {err}")
    return None


def format_user(from_user: User):
    username = f"@{from_user.username}" if from_user.username else "нет username"
    full_name = from_user.first_name or ""
    if from_user.last_name:
        full_name += f" {from_user.last_name}"
    return full_name.strip(), username


async def notify_owner(owner_id: int, from_user: User, text: str, event: str, old_text: str = None):
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
    _, err = await tg_call("sendMessage", lambda: bot.send_message(chat_id=owner_id, text=body))
    if err is not None:
        log.warning(f"Не удалось уведомить владельца: {err}")


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


async def get_rate(raw: str) -> str:
    """Получает актуальный курс ЦБ РФ."""
    amount, currency = parse_rate_input(raw)
    if not currency:
        return "❌ Не удалось распознать валюту.\n" + RATE_USAGE
    if currency == "RUB":
        return "ℹ️ Рубль — базовая валюта, курс считается относительно него.\nУкажите другую валюту: USD, EUR, CNY..."

    try:
        async with http().get(
            "https://www.cbr.ru/scripts/XML_daily.asp",
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            r.raise_for_status()
            content = await r.read()
        root = ET.fromstring(content)

        rate_rub = None
        for valute in root.findall("Valute"):
            if valute.find("CharCode").text == currency:
                nominal = int(valute.find("Nominal").text)
                rate_rub = float(valute.find("Value").text.replace(",", ".")) / nominal
                break
        date = root.attrib.get("Date", "")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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


async def get_weather(city: str) -> str:
    """Получает погоду через бесплатный wttr.in."""
    not_found = "❌ Город не найден. Проверьте название и попробуйте ещё раз.\nПример: .weth Москва"
    try:
        async with http().get(
            "https://wttr.in/" + quote(city) + "?format=j1&lang=ru",
            headers={"User-Agent": "curl/7.0"},
        ) as r:
            if r.status == 404:
                return not_found
            r.raise_for_status()
            data = await r.json(content_type=None)
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
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
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

async def _delete_later(chat_id: int, msg_id: int, biz_id: str, delay: int):
    await asyncio.sleep(delay)
    await delete_business_msg(chat_id, msg_id, biz_id)


def schedule_delete(chat_id: int, msg_id: int, biz_id: str, delay: int):
    """Удаляет сообщение через delay секунд."""
    spawn(_delete_later(chat_id, msg_id, biz_id, delay))


# ── Перевод ───────────────────────────────────────────────────────

TRANSLATE_LIMIT = 3000


async def translate_to_russian(text: str) -> str:
    """Переводит текст на русский язык через бесплатный Google Translate."""
    try:
        async with http().get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text[:TRANSLATE_LIMIT]},
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        result = "".join(part[0] for part in data[0] if part and part[0])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning(f"Перевод: ошибка соединения ({type(e).__name__})")
        return "❌ Сервис перевода сейчас недоступен. Попробуйте чуть позже."
    except (ValueError, KeyError, IndexError, TypeError) as e:
        log.warning(f"Перевод: неожиданный ответ ({type(e).__name__})")
        return "❌ Не удалось получить перевод."
    return fit_escaped(result, 3800) if result else "❌ Не удалось получить перевод."


# ── Спам ──────────────────────────────────────────────────────────

async def run_spam(chat_id: int, biz_id: str, text: str, count: int, stop_event: asyncio.Event):
    sent = 0
    failures = 0
    try:
        for _ in range(count):
            if stop_event.is_set():
                break
            if await send_business_msg(chat_id, biz_id, text):
                sent += 1
                failures = 0
            else:
                failures += 1
                if failures >= 5:
                    log.warning("⚠️ Спам остановлен: 5 неудачных отправок подряд")
                    break
            try:
                # ждём паузу, но команда .stop прерывает ожидание сразу
                await asyncio.wait_for(stop_event.wait(), timeout=SPAM_DELAY)
            except asyncio.TimeoutError:
                pass
    finally:
        if spam_stop.get(chat_id) is stop_event:
            spam_stop.pop(chat_id, None)
        log.info(f"✅ Спам завершён: {sent}/{count}")


# ── .info ─────────────────────────────────────────────────────────

async def get_user_info_text(target_id: int) -> str:
    chat, err = await tg_call("getChat", lambda: bot.get_chat(target_id))

    if err is not None:
        return f"❌ Не удалось получить информацию о пользователе.\n<i>{esc(err)}</i>"

    first_name = chat.first_name or "—"
    last_name = chat.last_name or ""
    username = f"@{chat.username}" if chat.username else "нет username"
    uid = chat.id
    bio = getattr(chat, "bio", None) or "не указана"
    full_name = f"{first_name} {last_name}".strip()
    profile_link = f"https://t.me/{chat.username}" if chat.username else f"tg://user?id={uid}"

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
    @wraps(handler)
    async def wrapper(message: Message):
        try:
            await handler(message)
        except Exception:
            log.exception(f"Ошибка в обработчике {handler.__name__}")
    return wrapper


async def handle_owner_command(message: Message, cmd: str, args: str):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    owner_id = message.from_user.id if message.from_user else 0
    replied = message.reply_to_message
    replied_user = replied.from_user if replied and replied.from_user else None

    async def reply(text: str):
        await edit_business_msg(chat_id, msg_id, biz_id, text)

    if cmd == ".mute":
        if replied_user:
            target = replied_user.id
            if target == owner_id:
                await reply("❌ Нельзя заглушить самого себя.")
                return
            name = esc(replied_user.first_name or target)
            do_mute(chat_id, target)
            await reply(
                f'<tg-emoji emoji-id="5357069174512303778">🔇</tg-emoji> <b>{name}</b> заглушён.\n'
                "Его сообщения будут удаляться. Чтобы снять мут, отправьте .unmute в ответ на его сообщение."
            )
        else:
            do_mute(chat_id, -1)
            await reply(
                '<tg-emoji emoji-id="5357069174512303778">🔇</tg-emoji> Все сообщения собеседника в этом чате заглушены.\n'
                "Чтобы снять мут, отправьте .unmute."
            )

    elif cmd == ".unmute":
        if replied_user:
            target = replied_user.id
            name = esc(replied_user.first_name or target)
            if do_unmute(chat_id, target):
                await reply(
                    f'<tg-emoji emoji-id="5357069174512303778">🔊</tg-emoji> Мут с <b>{name}</b> снят. '
                    "Сообщения снова доступны."
                )
            else:
                await reply(f"ℹ️ <b>{name}</b> не был заглушён.")
        else:
            if do_unmute(chat_id, -1):
                await reply(
                    '<tg-emoji emoji-id="5357069174512303778">🔊</tg-emoji> Мут снят. '
                    "Сообщения снова доступны."
                )
            else:
                await reply(
                    "ℹ️ Общий мут в этом чате не включён.\n"
                    "Чтобы снять мут с конкретного человека, отправьте .unmute в ответ на его сообщение."
                )

    elif cmd == ".stop":
        event = spam_stop.get(chat_id)
        if event:
            event.set()
            await reply("🛑 Спам остановлен.")
        else:
            await reply("ℹ️ В этом чате нет активного спама.")

    elif cmd == ".spam":
        usage = "Формат: .spam <текст> <количество>\nПример: .spam gg 10"
        pair = args.rsplit(None, 1)
        if len(pair) < 2:
            await reply("❌ Не хватает данных.\n" + usage)
            return
        spam_text, count_raw = pair[0].strip(), pair[1]
        try:
            count = int(count_raw)
        except ValueError:
            await reply("❌ Количество должно быть целым числом.\n" + usage)
            return
        if count < 1 or count > 1000:
            await reply("❌ Количество должно быть от 1 до 1000.")
            return
        if len(spam_text) > 4000:
            await reply("❌ Текст слишком длинный (максимум 4000 символов).")
            return
        old = spam_stop.get(chat_id)
        if old:
            old.set()
        stop_event = asyncio.Event()
        spam_stop[chat_id] = stop_event
        await reply(
            f"📨 <b>Спам запущен</b>: «{esc(shorten(spam_text, 200))}» × {count}\n"
            "Чтобы остановить, отправьте .stop"
        )
        spawn(run_spam(chat_id, biz_id, spam_text, count, stop_event))

    elif cmd in (".pm", ".inl", ".emo", ".gt"):
        await reply(toggle_mode(chat_id, cmd[1:]))

    elif cmd == ".tr":
        if not replied:
            await reply("❌ Отправьте .tr в ответ на сообщение, которое нужно перевести.")
            return
        original_text = content_of(replied)
        if not original_text:
            await reply("❌ В этом сообщении нет текста для перевода.")
            return
        await reply("🌐 Перевожу...")
        translated = await translate_to_russian(original_text)
        if translated.startswith("❌"):
            await reply(translated)
        else:
            await reply(f"🌐 <b>Перевод на русский:</b>\n\n{translated}")

    elif cmd == ".rate":
        if not args:
            await reply("❌ Укажите валюту.\n" + RATE_USAGE)
            return
        await reply(await get_rate(args))

    elif cmd == ".weth":
        if not args:
            await reply("❌ Укажите город.\nПример: .weth Москва")
            return
        await reply("🌤 Получаю погоду...")
        await reply(await get_weather(args))

    elif cmd == ".sec":
        if not args:
            await reply("❌ Укажите текст.\nПример: .sec Это секрет!")
            return
        sent_id = await send_business_msg(
            chat_id, biz_id,
            f"🤫 <b>Секретное сообщение</b>\n\n{fit_escaped(args, 3800)}\n\n<i>Исчезнет через 10 секунд...</i>",
        )
        if sent_id:
            await delete_business_msg(chat_id, msg_id, biz_id)
            schedule_delete(chat_id, sent_id, biz_id, delay=10)
        else:
            await reply("❌ Не удалось отправить секретное сообщение. Попробуйте ещё раз.")

    elif cmd == ".info":
        target_id = replied_user.id if replied_user else chat_id
        await reply(await get_user_info_text(target_id))


async def apply_active_mode(message: Message, text: str):
    """Если в чате включён режим, переписывает обычное сообщение владельца."""
    key = chat_mode.get(message.chat.id)
    if not key:
        return
    mode = MODES[key]
    new_text = esc(mode["transform"](text))   # экранируем уже готовый текст
    if len(new_text) > TG_LIMIT:
        log.info(f"{mode['icon']} {mode['title']}: результат слишком длинный, сообщение оставлено как есть")
        return
    await edit_business_msg(message.chat.id, message.message_id, message.business_connection_id, new_text)
    log.info(f"{mode['icon']} {mode['title']}: {shorten(text, 60)!r} → {shorten(new_text, 60)!r}")


# ── Подключение бизнес-аккаунта ───────────────────────────────────

@dp.business_connection()
async def handle_connection(connection: BusinessConnection):
    if connection.is_enabled:
        owners[connection.id] = connection.user.id
        log.info(f"Бизнес-аккаунт подключён, владелец: {connection.user.id}")
    else:
        owners.pop(connection.id, None)
        log.info("Бизнес-аккаунт отключён")


# ── Новые сообщения ───────────────────────────────────────────────

@dp.business_message()
@safe
async def handle_new(message: Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    user_id = message.from_user.id if message.from_user else 0
    text = (message.text or "").strip()

    owner_id = await get_owner(biz_id)
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
            await handle_owner_command(message, cmd, args)
        else:
            await apply_active_mode(message, text)
        return

    # ── Мут для собеседника ───────────────────────────────────────
    # Без известного владельца ничего не удаляем, чтобы случайно не задеть его сообщения.
    if owner_id is None or not biz_id:
        return
    if is_muted(chat_id, user_id) or is_muted(chat_id, -1):
        if await delete_business_msg(chat_id, msg_id, biz_id) and message.from_user:
            await notify_owner(owner_id, message.from_user, stored or "[медиа без текста]", "deleted")


# ── Изменённые сообщения ──────────────────────────────────────────

@dp.edited_business_message()
@safe
async def handle_edited(message: Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    biz_id = message.business_connection_id
    user_id = message.from_user.id if message.from_user else 0

    owner_id = await get_owner(biz_id)
    if owner_id is None or user_id == owner_id:
        return

    if message.from_user:
        track_name(message.from_user)

    new_text = content_of(message) or "[медиа без текста]"
    old_text = msg_cache.get((chat_id, msg_id))
    log.info(f"✏️ Изменено от {user_id}: {shorten(old_text or 'неизвестно', 60)!r} → {shorten(new_text, 60)!r}")
    cache_put(chat_id, msg_id, new_text)

    if message.from_user:
        await notify_owner(owner_id, message.from_user, new_text, "edited", old_text)


async def main():
    try:
        await bot.delete_webhook()   # если когда-то был вебхук, polling вернёт ошибку 409
    except Exception as e:
        log.warning(f"Не удалось сбросить вебхук: {type(e).__name__}")
    log.info("Бот запущен...")
    try:
        await dp.start_polling(
            bot,
            skip_updates=True,   # не выполнять старые команды, накопившиеся пока бот был выключен
            allowed_updates=["business_message", "edited_business_message", "business_connection"],
            polling_timeout=30,
        )
    finally:
        if _http is not None and not _http.closed:
            await _http.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен.")
