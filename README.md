# TRADE IS COOL — Nobitex Auto Executor

این پروژه لایه اجرای خودکار معامله است و **هیچ اتصال مستقیمی به تلگرام ندارد**.
معماری نهایی:

```text
Telegram Channel
      │
      ▼
GitHub Actions: telegram-github-bridge
      │
      ├── signals.jsonl ───────────────┐
      │                                ▼
      │                     Windows Executor (always on)
      │                                │
      │                                ▼
      │                           Nobitex Margin
      │
      └── outbox.jsonl ◀───────────────┘
              │
              ▼
       GitHub Actions → Telegram Admin
```

## قوانین معاملاتی قفل‌شده

این نسخه برای حساب واقعی نوشته شده و این موارد عمداً از `.env` قابل تغییر نیستند:

- ریسک اسمی هر معامله: **1 USDT** تا حد ضرر اولیه، قبل از کارمزد و لغزش قیمت
- اهرم: **5x**
- Target 1: **20%** در 1R
- Target 2: **30%** در 2R
- Target 3: **15%** در 4R
- Target 4: **10%** در 6R
- Runner: **25%**
- Runner trailing: **1.5R** پشت سقف/کف مطلوب

### نکته مهم درباره «ریسک 1 USDT»

حجم معامله از فاصله Entry تا Stop محاسبه می‌شود:

```text
position_amount = 1 USDT / abs(Entry - Stop)
notional = position_amount × Entry
collateral ≈ notional / 5
```

بنابراین 1 USDT «مارجین» نیست؛ **ریسک برنامه‌ریزی‌شده تا Stop است**. اهرم 5x فقط مارجین لازم برای همان ارزش پوزیشن را تعیین می‌کند.

## چرا ساختار سفارش‌ها تغییر کرده است؟

نسخه قبلی Executor برای تارگت‌ها از `POST /margin/orders/add` استفاده می‌کرد و سفارش خروج را مانند یک سفارش تعهدی جدید ارسال می‌کرد. این برای مدیریت یک پوزیشن باز، معماری درستی نیست.

در نسخه جدید، خروج‌ها با endpoint مربوط به همان position ثبت می‌شوند:

```text
POST /positions/{positionId}/close
```

و برای هر Target یک **OCO خروجی** ساخته می‌شود:

```text
TP Limit  +  Stop-Limit
```

به‌این‌ترتیب مجموع سفارش‌های خروج از تعهد پوزیشن بیشتر نمی‌شود. برای Runner که TP ثابت ندارد، یک `stop_market` مستقل استفاده می‌شود.

این مطابق مستندات فعلی نوبیتکس است: ثبت سفارش تعهدی در `/margin/orders/add` انجام می‌شود، اما تسویه/بستن موقعیت در `/positions/:positionId:/close` انجام می‌شود. نوبیتکس همچنین OCO را برای بستن موقعیت مستند کرده است.

## مدیریت معامله

در لحظه دریافت Entry، بدون منتظر ماندن برای پیام‌های بعدی:

1. پوزیشن Market باز می‌شود.
2. Position ID از نوبیتکس گرفته می‌شود.
3. چهار OCO خروجی برای T1 تا T4 ثبت می‌شوند.
4. Runner با Stop Market محافظت می‌شود.
5. Stop اولیه و تمام Targetها از همان ابتدا روی صرافی قرار می‌گیرند.

پس حتی اگر Executor برای مدتی خاموش شود، سفارش‌های ثابت Target/Stop که روی خود نوبیتکس ثبت شده‌اند همچنان وجود دارند.

### بعد از Targetها

- T1 → Stop باقی‌مانده به Entry منتقل می‌شود.
- T2 → Stop باقی‌مانده به T1 منتقل می‌شود.
- T3 → Stop باقی‌مانده به T2 منتقل می‌شود.
- T4 → 75% بانک شده و فقط Runner باقی می‌ماند.
- Runner → trailing stop با فاصله 1.5R؛ قیمت عمومی نوبیتکس هر 10 ثانیه بررسی و فقط در جهت مطلوب Stop بهبود داده می‌شود.

اگر پیام `BREAKEVEN`، `STOP AFTER TARGET 2` یا `STOP AFTER TARGET 3` برسد، Executor آن را **رویداد بستن** در نظر می‌گیرد و باقی‌مانده را در قیمت استراتژی تلاش می‌کند با Limit ببندد؛ اگر سریع Fill نشود، به Market fallback می‌کند.

## GitHub و گزارش تلگرام

Windows Executor فقط با GitHub و Nobitex ارتباط دارد.

- `signals.jsonl`: پیام‌های کانال
- `outbox.jsonl`: گزارش‌های Executor
- `state.json`: وضعیت محلی Executor

Bridge روی GitHub Actions گزارش‌های outbox را به Telegram می‌فرستد.

Bridge عمداً در صورت خطای Telegram، cursor پیام را جلو نمی‌برد؛ بنابراین گزارش ناموفق در اجرای بعدی دوباره تلاش می‌شود.

## امنیت API

API Key نوبیتکس را فقط با مجوزهای زیر بسازید:

```text
READ
TRADE
```

**WITHDRAW را فعال نکنید.**

IP Whitelist را روی IP ثابت کامپیوتری که Executor روی آن اجرا می‌شود قرار دهید.

کلید خصوصی فقط در `.env` محلی باشد و هرگز در GitHub commit نشود.

## نصب Windows Executor

```text
1. Python 3.11+ نصب کنید.
2. pip install -r requirements.txt
3. .env.example را به .env کپی کنید.
4. مقادیر GitHub و Nobitex را وارد کنید.
5. run_watchdog.bat را اجرا کنید.
```

قبل از اجرای واقعی، مطمئن شوید `NOBITEX_BASE_URL` دقیقاً این است:

```text
https://apiv2.nobitex.ir
```

Executor در صورت مشاهده `testnet` عمداً اجرا را متوقف می‌کند.

## خطاهای مهم

اگر یکی از این موارد رخ دهد، Executor معامله را بدون محافظت رها نمی‌کند و به ادمین هشدار می‌دهد:

- Position ID پیدا نشود
- Target/Stop سفارش خروج ثبت نشود
- اهرم 5x برای بازار مجاز نباشد
- API Key مجوز لازم نداشته باشد
- GitHub پیام سیگنال را ندهد
- signals.jsonl خراب یا بازنویسی شود

## تست‌های انجام‌شده روی این نسخه

- Python syntax compilation برای تمام فایل‌ها
- Parsing سیگنال استاندارد LONG/SHORT
- محاسبه ریسک 1 USDT
- محاسبه اهرم 5x
- تخصیص 20/30/15/10/25 درصدی حجم
- ساختار OCO Targetها + Runner Stop با مجموع 100%

**این تست‌ها شبیه‌سازی محلی هستند و به API واقعی نوبیتکس سفارش ارسال نمی‌کنند.**
