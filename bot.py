"""
Архивариус — Telegram-бот: сохраняет скинутые фото и видео по папкам.
Кнопки: добавить файл · удалить файл · папки · добавить папку · поиск по названию.
Везде есть «Назад» и «Главное меню».
Стек: aiogram 3.x · хранение: JSON + файлы на диске (папка data/).
Запуск: положи рядом .env с BOT_TOKEN и выполни  python bot.py

v1.1 — исправлено:
  • скачивание файла (download_file теперь получает file_path, а не file_id —
    именно из-за этого был краш 404 Not Found и бот молчал);
  • файлы больше 20 МБ больше не валят бота — приходит понятное сообщение;
  • фото/видео, присланное без кнопки «Добавить файл», получает подсказку,
    а не тишину.
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "data" / "files"
DB_PATH = BASE_DIR / "data" / "db.json"
FILES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_FOLDERS = ["Упражнения", "Рецепты", "Семья", "Мусор"]

# ---------------- хранилище ----------------

def load_db() -> dict:
    if DB_PATH.exists():
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    db = {"folders": list(DEFAULT_FOLDERS), "files": []}
    save_db(db)
    return db

def save_db(db: dict) -> None:
    DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#]', "", name).strip()
    return name[:64] or "Без названия"

def file_path(f: dict) -> Path:
    return FILES_DIR / (f["uid"] + f["ext"])

# ---------------- клавиатуры ----------------

def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)

def kb(rows, *, back: str = None) -> InlineKeyboardMarkup:
    """Любая клавиатура: свои кнопки + «Назад» + «Главное меню»."""
    rows = [list(r) for r in rows]
    if back:
        rows.append([btn("⬅️ Назад", back)])
    rows.append([btn("🏠 Главное меню", "go:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

MAIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [btn("➕ Добавить файл", "go:add_file")],
    [btn("🗑 Удалить файл", "go:del_file")],
    [btn("📁 Папки", "go:folders")],
    [btn("➕ Добавить папку", "go:add_folder")],
    [btn("🔍 Поиск по названию", "go:search")],
])

def folders_kb(db: dict, prefix: str, back: str) -> InlineKeyboardMarkup:
    rows = [[btn("📁 " + name, prefix + ":" + str(i))]
            for i, name in enumerate(db["folders"])]
    return kb(rows, back=back)

def files_kb(files: list, prefix: str, back: str) -> InlineKeyboardMarkup:
    rows = [[btn(("📷 " if f["kind"] == "photo" else "🎬 ") + f["name"],
                 prefix + ":" + f["uid"])]
            for f in files]
    return kb(rows, back=back)

# ---------------- состояния ----------------

class Form(StatesGroup):
    wait_media = State()        # ждём фото/видео
    wait_folder = State()       # выбор папки для нового файла
    wait_file_name = State()    # название нового файла
    wait_folder_name = State()  # название новой папки
    wait_del_name = State()     # название файла для удаления
    wait_search = State()       # запрос поиска

router = Router()

async def switch(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()

# ---------------- главное меню ----------------

@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я Архивариус 🗂\n"
        "Скинь мне фото или видео — сохраню по папкам.\n\n"
        "Выбери действие:",
        reply_markup=MAIN_KB,
    )

@router.callback_query(F.data == "go:main")
async def go_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await switch(call, "Главное меню 🏠 Выбери действие:", MAIN_KB)

# ---------------- добавить файл ----------------

@router.callback_query(F.data == "go:add_file")
async def add_file(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.wait_media)
    await switch(call, "Кидай фото или видео 👇",
                 kb([[btn("❌ Отмена", "go:main")]], back=None))

@router.message(Form.wait_media)
async def got_media(message: Message, state: FSMContext):
    kind = file_id = None
    if message.photo:
        kind, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        kind, file_id = "video", message.video.file_id
    elif message.document and message.document.mime_type:
        mime = message.document.mime_type
        if mime.startswith("image/"):
            kind, file_id = "photo", message.document.file_id
        elif mime.startswith("video/"):
            kind, file_id = "video", message.document.file_id
    if kind is None:
        await message.answer("Жду именно фото или видео 🙏",
                             reply_markup=kb([[btn("❌ Отмена", "go:main")]]))
        return

    uid = uuid.uuid4().hex
    try:
        # ВАЖНО: download_file принимает file_path из get_file, а НЕ file_id!
        # (file_id в этом месте даёт 404 Not Found от api.telegram.org)
        tg_file = await message.bot.get_file(file_id)
        ext = Path(tg_file.file_path).suffix or (".jpg" if kind == "photo" else ".mp4")
        await message.bot.download_file(tg_file.file_path,
                                        destination=FILES_DIR / (uid + ext))
    except TelegramAPIError:
        # Файлы больше 20 МБ Telegram ботам не отдаёт — говорим об этом честно.
        await state.clear()
        await message.answer(
            "Не смог скачать файл 😔\n"
            "Если он больше 20 МБ — Telegram не отдаёт такие файлы боту. "
            "Сожми и отправь ещё раз.",
            reply_markup=MAIN_KB,
        )
        return

    await state.update_data(kind=kind, ext=ext, uid=uid)
    await state.set_state(Form.wait_folder)
    db = load_db()
    await message.answer("Получил! 📥 В какую папку сохраним?",
                         reply_markup=folders_kb(db, "folder:pick", back="go:main"))

@router.callback_query(Form.wait_folder, F.data.startswith("folder:pick:"))
async def pick_folder(call: CallbackQuery, state: FSMContext):
    db = load_db()
    folder = db["folders"][int(call.data.rsplit(":", 1)[-1])]
    await state.update_data(folder=folder)
    await state.set_state(Form.wait_file_name)
    await switch(call, "Принял! Папка: «" + folder + "» 📁\nКак назовём файл?",
                 kb([], back="go:main"))

@router.message(Form.wait_file_name)
async def file_name(message: Message, state: FSMContext):
    data = await state.get_data()
    name = sanitize(message.text or "")
    db = load_db()
    db["files"].append({
        "uid": data["uid"], "name": name, "folder": data["folder"],
        "kind": data["kind"], "ext": data["ext"],
        "added": datetime.now().strftime("%d.%m.%Y %H:%M"),
    })
    save_db(db)
    await state.clear()
    await message.answer("✅ Сохранено!\n«" + name + "» → 📁 " + data["folder"] +
                         "\n\nЧто дальше?", reply_markup=MAIN_KB)

# ---------------- удалить файл ----------------

@router.callback_query(F.data == "go:del_file")
async def del_file(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await switch(call, "Как найдём файл, который нужно удалить?",
                 kb([[btn("✍️ По названию", "del:by_name")],
                     [btn("📁 Выбрать папку, затем файл", "del:by_folder")]],
                    back="go:main"))

@router.callback_query(F.data == "del:by_name")
async def del_by_name(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.wait_del_name)
    await switch(call, "Напиши точное название файла ✍️", kb([], back="go:del_file"))

@router.message(Form.wait_del_name)
async def del_name(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()
    db = load_db()
    found = [f for f in db["files"] if f["name"].lower() == query]
    if not found:
        await state.clear()
        await message.answer("Не нашёл файл «" + message.text.strip() + "» 🤷",
                             reply_markup=kb([[btn("🔁 Попробовать ещё раз", "del:by_name")]],
                                             back="go:main"))
        return
    await state.clear()
    await message.answer("Нашёл! Нажми, что удаляем:",
                         reply_markup=files_kb(found, "del:ask", back="go:main"))

@router.callback_query(F.data == "del:by_folder")
async def del_by_folder(call: CallbackQuery, state: FSMContext):
    await switch(call, "Выбери папку:", folders_kb(load_db(), "del:folder", back="go:del_file"))

@router.callback_query(F.data.startswith("del:folder:"))
async def del_folder_files(call: CallbackQuery, state: FSMContext):
    db = load_db()
    folder = db["folders"][int(call.data.rsplit(":", 1)[-1])]
    files = [f for f in db["files"] if f["folder"] == folder]
    if not files:
        await switch(call, "В папке «" + folder + "» пусто 🕸",
                     kb([[btn("📁 Выбрать другую", "del:by_folder")]], back="go:main"))
    else:
        await switch(call, "Папка «" + folder + "» — выбери файл:",
                     files_kb(files, "del:ask", back="del:by_folder"))

@router.callback_query(F.data.startswith("del:ask:"))
async def del_ask(call: CallbackQuery, state: FSMContext):
    uid = call.data.rsplit(":", 1)[-1]
    f = next(x for x in load_db()["files"] if x["uid"] == uid)
    await switch(call, "Точно удаляем «" + f["name"] + "» из папки «" + f["folder"] + "»?",
                 kb([[btn("✅ Да, удалить", "del:yes:" + uid)],
                     [btn("❌ Отмена", "go:main")]], back="go:del_file"))

@router.callback_query(F.data.startswith("del:yes:"))
async def del_yes(call: CallbackQuery, state: FSMContext):
    uid = call.data.rsplit(":", 1)[-1]
    db = load_db()
    f = next((x for x in db["files"] if x["uid"] == uid), None)
    if f:
        file_path(f).unlink(missing_ok=True)
        db["files"] = [x for x in db["files"] if x["uid"] != uid]
        save_db(db)
    await state.clear()
    await switch(call, "🗑 Готово! Файл удалён.\n\nЧто дальше?", MAIN_KB)

# ---------------- папки ----------------

@router.callback_query(F.data == "go:folders")
async def folders(call: CallbackQuery, state: FSMContext):
    db = load_db()
    lines = []
    for name in db["folders"]:
        count = len([f for f in db["files"] if f["folder"] == name])
        lines.append("📁 " + name + " — " + str(count) + " шт.")
    rows = [[btn("📁 " + n, "open:" + str(i))] for i, n in enumerate(db["folders"])]
    rows.append([btn("➕ Добавить папку", "go:add_folder")])
    await switch(call, "Все папки:\n" + "\n".join(lines), kb(rows, back="go:main"))

@router.callback_query(F.data.startswith("open:"))
async def folder_open(call: CallbackQuery, state: FSMContext):
    db = load_db()
    folder = db["folders"][int(call.data.rsplit(":", 1)[-1])]
    files = [f for f in db["files"] if f["folder"] == folder]
    if not files:
        await switch(call, "В папке «" + folder + "» пусто 🕸",
                     kb([[btn("➕ Добавить файл", "go:add_file")]], back="go:folders"))
    else:
        await switch(call, "Папка «" + folder + "». Нажми на файл — отправлю его:",
                     files_kb(files, "send", back="go:folders"))

@router.callback_query(F.data.startswith("send:"))
async def send_file(call: CallbackQuery, state: FSMContext):
    uid = call.data.rsplit(":", 1)[-1]
    f = next((x for x in load_db()["files"] if x["uid"] == uid), None)
    if not f:
        await switch(call, "Файл уже удалён 🤷", MAIN_KB)
        return
    if not file_path(f).exists():
        await switch(call, "Файл «" + f["name"] + "» пропал с диска 🤷 Запись удалена.",
                     MAIN_KB)
        return
    media = FSInputFile(file_path(f))
    caption = "«" + f["name"] + "» · 📁 " + f["folder"]
    if f["kind"] == "photo":
        await call.message.answer_photo(media, caption=caption)
    else:
        await call.message.answer_video(media, caption=caption)
    await call.answer("Отправил 📎")

# ---------------- добавить папку ----------------

@router.callback_query(F.data == "go:add_folder")
async def add_folder(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.wait_folder_name)
    await switch(call, "Как назовём новую папку? ✍️", kb([], back="go:main"))

@router.message(Form.wait_folder_name)
async def folder_name(message: Message, state: FSMContext):
    name = sanitize(message.text or "")
    db = load_db()
    if any(n.lower() == name.lower() for n in db["folders"]):
        await state.clear()
        await message.answer("Папка «" + name + "» уже есть 🤔",
                             reply_markup=kb([[btn("🔁 Придумать другое", "go:add_folder")]],
                                             back="go:main"))
        return
    db["folders"].append(name)
    save_db(db)
    await state.clear()
    await message.answer("📁 Папка «" + name + "» создана!\n\nЧто дальше?",
                         reply_markup=MAIN_KB)

# ---------------- поиск по названию ----------------

@router.callback_query(F.data == "go:search")
async def search(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.wait_search)
    await switch(call, "Напиши название файла, который нужно открыть 🔍",
                 kb([], back="go:main"))

@router.message(Form.wait_search)
async def search_query(message: Message, state: FSMContext):
    query = (message.text or "").strip().lower()
    db = load_db()
    found = [f for f in db["files"] if query in f["name"].lower()]
    await state.clear()
    if not found:
        await message.answer("По запросу «" + message.text.strip() + "» ничего нет 🤷",
                             reply_markup=kb([[btn("🔁 Поиск", "go:search")]],
                                             back="go:main"))
        return
    await message.answer("Нашёл: " + str(len(found)) + " шт. Нажми — отправлю файл:",
                         reply_markup=files_kb(found, "send", back="go:main"))

# ---------------- запасной выход ----------------

@router.message()
async def fallback(message: Message, state: FSMContext):
    """Сюда долетает всё, что не поймали сценарии: текст и файлы вне состояний.
    Раньше бот в таких случаях просто молчал."""
    is_media = bool(
        message.photo
        or message.video
        or message.animation
        or (message.document and message.document.mime_type
            and message.document.mime_type.startswith(("image/", "video/")))
    )
    await state.clear()
    if is_media:
        await message.answer(
            "Вижу файл 👀 Но сохраняю я через меню:\n"
            "нажми «➕ Добавить файл» и кидай снова.",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer("Я понимаю только кнопки 🙂 Выбери действие:",
                             reply_markup=MAIN_KB)

# ---------------- запуск ----------------

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в файле .env (получи у @BotFather)")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("Архивариус на связи 🗂  (Ctrl+C — остановить)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
