#!/usr/bin/env python3
"""Telegram-бот погоди для Хмельницького. Запускається за розкладом у GitHub Actions.

Надсилає поточну погоду + прогноз на найближчий наступний слот (08:00, 12:00, 16:00, 20:00 за Києвом).
Без зовнішніх залежностей (тільки стандартна бібліотека Python 3.9+).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

TZ_NAME = "Europe/Kiev"


class _KyivFallback(tzinfo):
    """Запасний варіант, якщо в системі немає бази часових поясів:
    UTC+2, а з останньої неділі березня до останньої неділі жовтня (01:00 UTC) UTC+3."""

    @staticmethod
    def _last_sunday(year, month):
        d = datetime(year, month + 1, 1, 1) - timedelta(days=1) if month < 12 else datetime(year, 12, 31, 1)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def _is_dst_utc(self, utc_naive):
        y = utc_naive.year
        return self._last_sunday(y, 3) <= utc_naive < self._last_sunday(y, 10)

    def utcoffset(self, dt):
        # dt зараз місцевий; наближено визначаємо DST через UTC+2
        utc_guess = dt.replace(tzinfo=None) - timedelta(hours=2)
        return timedelta(hours=3 if self._is_dst_utc(utc_guess) else 2)

    def dst(self, dt):
        return self.utcoffset(dt) - timedelta(hours=2)

    def tzname(self, dt):
        return "EEST" if self.dst(dt) else "EET"


try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # noqa: BLE001
    TZ = _KyivFallback()
CITY = "Хмельницький"
LAT, LON = 49.4230, 26.9871
SLOTS = (8, 12, 16, 20)

# Скрипт запускається за ~15 хв до слоту (cron у UTC, є два запуски на слот
# для зимового/літнього часу). Приймаємо запуск, лише якщо до слоту
# не більше EARLY_MIN хв або після слоту не більше LATE_MIN хв.
EARLY_MIN = 30
LATE_MIN = 40

WMO = {
    0: ("☀️", "Ясно"),
    1: ("🌤", "Переважно ясно"),
    2: ("⛅", "Мінлива хмарність"),
    3: ("☁️", "Хмарно"),
    45: ("🌫", "Туман"),
    48: ("🌫", "Паморозний туман"),
    51: ("🌦", "Слабка мряка"),
    53: ("🌦", "Мряка"),
    55: ("🌧", "Сильна мряка"),
    56: ("🌧", "Крижана мряка"),
    57: ("🌧", "Сильна крижана мряка"),
    61: ("🌧", "Слабкий дощ"),
    63: ("🌧", "Дощ"),
    65: ("🌧", "Сильний дощ"),
    66: ("🌧", "Крижаний дощ"),
    67: ("🌧", "Сильний крижаний дощ"),
    71: ("🌨", "Слабкий сніг"),
    73: ("🌨", "Сніг"),
    75: ("❄️", "Сильний сніг"),
    77: ("🌨", "Снігові зерна"),
    80: ("🌦", "Слабка злива"),
    81: ("🌧", "Злива"),
    82: ("⛈", "Сильна злива"),
    85: ("🌨", "Снігопад"),
    86: ("❄️", "Сильний снігопад"),
    95: ("⛈", "Гроза"),
    96: ("⛈", "Гроза з градом"),
    99: ("⛈", "Сильна гроза з градом"),
}

WIND_DIRS = ("північний", "північно-східний", "східний", "південно-східний",
             "південний", "південно-західний", "західний", "північно-західний")

CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
)
HOURLY_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability,"
    "precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
)


def retry(fn, attempts=4, delay=5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"Спроба {i + 1}/{attempts} не вдалась: {exc}", file=sys.stderr)
            time.sleep(delay * (i + 1))
    raise last


def find_slot(now):
    """Повертає слот (aware datetime), якому відповідає поточний запуск, або None."""
    now_utc = now.astimezone(timezone.utc)
    for day in (-1, 0, 1):
        base = (now + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        for h in SLOTS:
            slot = base.replace(hour=h)
            delta_min = (slot.astimezone(timezone.utc) - now_utc).total_seconds() / 60
            if -LATE_MIN <= delta_min <= EARLY_MIN:
                return slot
    return None


def next_slot(slot):
    for h in SLOTS:
        if h > slot.hour:
            return slot.replace(hour=h, minute=0)
    return (slot + timedelta(days=1)).replace(hour=SLOTS[0], minute=0)


def fetch_weather():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "current": CURRENT_VARS,
        "hourly": HOURLY_VARS,
        "wind_speed_unit": "ms",
        "timezone": TZ_NAME,
        "forecast_days": 2,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)

    def go():
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)

    return retry(go)


def temp(v):
    r = int(round(v))
    return f"{r:+d}°C" if r else "0°C"


def wind_dir(deg):
    return WIND_DIRS[int(round(deg / 45)) % 8]


def mmhg(hpa):
    return int(round(hpa * 0.750062))


def build_message(data, slot, nxt):
    c = data["current"]
    emoji, desc = WMO.get(c["weather_code"], ("🌡", "Невідомі умови"))
    lines = [
        f"{emoji} <b>{CITY}</b> — зараз ({slot:%H:%M})",
        f"<b>{desc}</b>",
        f"🌡 Температура: {temp(c['temperature_2m'])}, відчувається як {temp(c['apparent_temperature'])}",
        f"💧 Вологість: {c['relative_humidity_2m']}%",
        f"💨 Вітер: {c['wind_speed_10m']:.0f} м/с, пориви до {c['wind_gusts_10m']:.0f} м/с, {wind_dir(c['wind_direction_10m'])}",
        f"🧭 Тиск: {mmhg(c['pressure_msl'])} мм рт. ст.",
        f"☁️ Хмарність: {c['cloud_cover']}%",
        f"🌧 Опади (за останню годину): {c['precipitation']:.1f} мм",
    ]

    h = data["hourly"]
    key = nxt.strftime("%Y-%m-%dT%H:00")
    if key in h["time"]:
        i = h["time"].index(key)
        e2, d2 = WMO.get(h["weather_code"][i], ("🌡", "Невідомі умови"))
        when = f"{nxt:%H:%M}" + (" завтра" if nxt.date() != slot.date() else "")
        lines += [
            "",
            f"⏭ <b>Прогноз на {when}</b>",
            f"{e2} {d2}",
            f"🌡 {temp(h['temperature_2m'][i])}, відчуватиметься як {temp(h['apparent_temperature'][i])}",
            f"💧 Вологість: {h['relative_humidity_2m'][i]}%",
            f"💨 Вітер: {h['wind_speed_10m'][i]:.0f} м/с, пориви до {h['wind_gusts_10m'][i]:.0f} м/с, {wind_dir(h['wind_direction_10m'][i])}",
            f"☔ Ймовірність опадів: {h['precipitation_probability'][i]}% ({h['precipitation'][i]:.1f} мм)",
        ]
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TG_TOKEN"]
    chat_id = os.environ["TG_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()

    def go():
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    resp = retry(go)
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram повернув помилку: {resp}")


def main():
    now = datetime.now(TZ)
    force = os.environ.get("FORCE", "").lower() == "true"

    slot = find_slot(now)
    if slot is None:
        if not force:
            print(f"{now:%Y-%m-%d %H:%M} за Києвом — не час для відправки, пропускаю.")
            return 0
        slot = now.replace(second=0, microsecond=0)  # тестовий запуск
    else:
        wait = (slot.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            print(f"Чекаю {wait:.0f} с до {slot:%H:%M}")
            time.sleep(wait)

    data = fetch_weather()
    send_telegram(build_message(data, slot, next_slot(slot)))
    print("Надіслано.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
