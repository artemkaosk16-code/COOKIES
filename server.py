"""
Бэкенд «Печеньки и Молока»: оплата Telegram Stars, рейтинг, бот с кнопкой «Играть».

Переменные окружения:
  BOT_TOKEN       токен от @BotFather                         (обязательно)
  WEBAPP_URL      адрес игры, например https://логин.github.io/репозиторий/  (обязательно)
  WEBHOOK_SECRET  любая длинная случайная строка               (рекомендуется)
  SUPPORT_CONTACT как связаться по вопросам оплаты, например @ваш_ник
  DB_PATH         путь к базе SQLite (кладите на постоянный диск!), по умолчанию game.db
"""
import os, re, time, hmac, hashlib, json, sqlite3
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP = os.environ["WEBAPP_URL"]
SECRET = os.environ.get("WEBHOOK_SECRET", "")
SUPPORT = os.environ.get("SUPPORT_CONTACT", "владельцу бота")
TG = f"https://api.telegram.org/bot{TOKEN}"

db = sqlite3.connect(os.environ.get("DB_PATH", "game.db"), check_same_thread=False)
db.executescript("""
create table if not exists top(uid integer primary key, name text, score real);
create table if not exists purchases(charge text primary key, uid integer, item text, claimed integer default 0);
""")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# id -> (название, описание, цена в Stars). Должно совпадать с SHOP в index.html
ITEMS = {
    "spin": ("Ещё одно вращение", "Дополнительная попытка колеса сегодня", 10),
    "frenzy": ("Фарт ×7 на 30 минут", "Вся выпечка ×7 на полчаса", 20),
    "box": ("Ящик печенек", "3 часа выпечки и 10 000 печенек", 25),
    "mag": ("Золотой магнит", "Золотые печенья появляются втрое чаще", 39),
    "shakes": ("8 молочных коктейлей", "+40% к выпечке навсегда", 49),
    "auto": ("Ночной пекарь", "Офлайн-доход 100% и до 24 часов", 59),
    "box2": ("Сундук печенек", "12 часов выпечки и 50 000 печенек", 79),
    "tap": ("Платиновая скалка", "Клики ×3 навсегда", 79),
    "vip": ("Вечная посыпка", "Вся выпечка ×2 навсегда", 99),
}


def check_user(init: str) -> dict:
    """Проверяет подпись initData от Telegram и возвращает пользователя."""
    d = dict(parse_qsl(init))
    got = d.pop("hash", "")
    data = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, data.encode(), hashlib.sha256).hexdigest(), got):
        raise HTTPException(401, "bad signature")
    if time.time() - int(d.get("auth_date", 0)) > 3 * 86400:
        raise HTTPException(401, "expired")
    return json.loads(d["user"])


async def tg(method: str, **params):
    async with httpx.AsyncClient(timeout=10) as c:
        return (await c.post(f"{TG}/{method}", json=params)).json()


@app.get("/")
def health():
    return {"ok": True}


@app.post("/invoice")
async def invoice(r: Request):
    b = await r.json()
    check_user(b["init"])
    if b["item"] not in ITEMS:
        raise HTTPException(400, "unknown item")
    title, desc, price = ITEMS[b["item"]]
    res = await tg("createInvoiceLink", title=title, description=desc, payload=b["item"],
                   provider_token="", currency="XTR", prices=[{"label": title, "amount": price}])
    return {"link": res["result"]}


@app.post("/claim")
async def claim(r: Request):
    """Выдаёт оплаченные, но ещё не полученные товары. Фальшивую оплату с клиента подделать нельзя."""
    u = check_user((await r.json())["init"])
    rows = db.execute("select charge,item from purchases where uid=? and claimed=0", (u["id"],)).fetchall()
    for charge, _ in rows:
        db.execute("update purchases set claimed=1 where charge=?", (charge,))
    db.commit()
    return {"items": [item for _, item in rows]}


@app.post("/score")
async def score(r: Request):
    b = await r.json()
    u = check_user(b["init"])
    name = re.sub(r'[<>&"\x00-\x1f]', "", str(b.get("name", ""))).strip()[:16] or u.get("first_name", "Пекарь")
    db.execute(
        "insert into top values(?,?,?) on conflict(uid) do update "
        "set score=max(score,excluded.score), name=excluded.name",
        (u["id"], name, float(b["score"])))
    db.commit()
    return {"ok": True}


@app.get("/top")
def top():
    rows = db.execute("select uid,name,score from top order by score desc limit 20")
    return [{"id": i, "name": n, "score": s} for i, n, s in rows]


@app.post("/webhook")
async def webhook(r: Request):
    if SECRET and r.headers.get("x-telegram-bot-api-secret-token") != SECRET:
        raise HTTPException(403)
    u = await r.json()

    if "pre_checkout_query" in u:  # без ответа за 10 секунд оплата сорвётся
        q = u["pre_checkout_query"]
        ok = q["invoice_payload"] in ITEMS
        extra = {} if ok else {"error_message": "Товар недоступен"}
        await tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok, **extra)

    m = u.get("message", {})
    pay = m.get("successful_payment")
    if pay:
        db.execute("insert or ignore into purchases(charge,uid,item) values(?,?,?)",
                   (pay["telegram_payment_charge_id"], m["from"]["id"], pay["invoice_payload"]))
        db.commit()

    text = m.get("text", "")
    if text.startswith("/start"):
        await tg("sendMessage", chat_id=m["chat"]["id"], text="🍪🥛 Печенька и Молоко",
                 reply_markup={"inline_keyboard": [[{"text": "Играть", "web_app": {"url": WEBAPP}}]]})
    elif text.startswith("/paysupport"):
        await tg("sendMessage", chat_id=m["chat"]["id"],
                 text=f"По вопросам оплаты и возвратов напишите {SUPPORT}.")
    return {"ok": True}
