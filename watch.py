"""Klook 優惠提醒（2026-10-16）

Klook 台灣的活動頁（tetris/promo/klook137 等）會把之後幾週的搶購事先排好：
每張優惠券都有開搶時間，每個商品卡片寫著「週五限量買1送1」這種字。
這支每隔一陣子讀一次這些頁面，挑出
  ① 五折（含）以下、買一送一（買1送2）、滿額折到一半以上
  ② 台灣的商品（或全站／全球都能用的折扣碼）
發現新的就推到手機，開搶前再提醒一次。

指令：
  python watch.py scan    馬上讀一次 Klook，印出挑到的優惠
  python watch.py list    印出目前記著的優惠（不上網）
  python watch.py tick    排程用：資料舊了才讀 Klook，該提醒的推出去
  python watch.py test    推一則測試通知到手機
"""
import datetime as dt
import json
import os
import re
import secrets
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
EVENTS = DATA / "events.json"
SENT = DATA / "sent.json"
PRODUCTS = DATA / "products.json"
LOG = DATA / "log.txt"

TZ = dt.timezone(dt.timedelta(hours=8))
BASE = "https://www.klook.com"
SEED_SLUGS = ["klook137"]          # 「Klook 137 最便宜」活動頁，從它連出去的活動頁也一起讀
MAX_SLUGS = 15
PAGE_API = "/v1/tetrisrendersrv/public/publish/getPageInfo?slug={slug}&urlPrefix=promo&lang=zh-TW&version=1"
COUPON_API = "/v1/promosrv/program/tetris/coupon/cards?program_ids={ids}"

DEFAULT_CONFIG = {
    "ntfy_topic": "",              # 手機裝 ntfy App 訂閱這個名字就收得到
    "line_channel_id": "",         # LINE 官方帳號（Messaging API）的頻道 ID＋密鑰，程式自己換金鑰
    "line_channel_secret": "",
    "line_channel_token": "",      # 有長期金鑰也可以直接填這個
    "line_user_id": "",            # 空白＝推給官方帳號的全部好友
    "remind_minutes": [120, 10],   # 開搶前幾分鐘提醒（錯過大的只補最小的那一則）
    # 一天讀一次 Klook（使用者 2026-10-16 指定）：優惠都提前一週以上排好，活動頁上次更新是 10/15 18:08，
    # 選 19:00 讀＝當天傍晚加的也抓得到，又趕得上晚上 22:00 那場的提前 2 小時提醒
    "scan_at": "19:00",
    "exclude_ids": [231819],       # 不要的商品編號（231819＝SIF 首爾插畫展台北站，使用者 2026-10-16 說不要）
    "exclude_words": [],           # 名稱含這些字的不要
}

WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
TW_WORDS = ("台灣", "臺灣", "國旅", "國內", "Taiwan", "台北", "臺北", "新北", "桃園", "台中", "臺中", "台南",
            "臺南", "高雄", "基隆", "新竹", "苗栗", "南投", "雲林", "嘉義", "屏東", "宜蘭", "花蓮", "台東",
            "臺東", "澎湖", "金門", "馬祖", "墾丁", "日月潭", "阿里山", "小琉球", "綠島", "蘭嶼")


# ───────────── 共用 ─────────────

def now():
    return dt.datetime.now(TZ)


def log(msg):
    DATA.mkdir(exist_ok=True)
    line = f"{now():%Y-%m-%d %H:%M:%S} {msg}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        pass


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    DATA.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


ENV_KEYS = {"ntfy_topic": "KLOOK_NTFY_TOPIC", "line_channel_id": "LINE_CHANNEL_ID",
            "line_channel_secret": "LINE_CHANNEL_SECRET", "line_user_id": "LINE_USER_ID"}


def config():
    """本機讀 config.json；GitHub 雲端沒有這個檔，密鑰從加密設定（環境變數）來。"""
    cfg = {**DEFAULT_CONFIG, **load(CONFIG, {})}
    for k, env in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    if os.environ.get("GITHUB_ACTIONS"):
        return cfg
    if not cfg["ntfy_topic"]:
        cfg["ntfy_topic"] = "klook-" + secrets.token_hex(6)   # 隨機名字，別人猜不到
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


# ───────────── 判斷折扣 ─────────────

def deep_discount(text):
    """五折（含）以下、買一送一、滿額折一半以上 → 回傳那段字；不夠深回 None。"""
    t = (text or "").replace("買一送一", "買1送1").replace("買一送二", "買1送2")
    m = re.search(r"買\s*1\s*送\s*[1-9]|買1日送第2日", t)
    if m:
        return m.group(0).replace(" ", "")
    m = re.search(r"(?<![\d.])([1-5](?:\.\d)?)\s*折(起)?", t)          # 55折、85折不算；5折、1折、5折起算
    if m:
        return m.group(0).replace(" ", "")
    m = re.search(r"(?<![\d.])([5-9]\d|100)\s*%(?!\s*回饋)", t)
    if m and re.search(r"off|折扣|promo", t, re.I):
        return f"{m.group(1)}% off"
    m = re.search(r"滿\s*\$?\s*([\d,]+)\s*折\s*\$?\s*([\d,]+)", t)
    if m:
        full, off = (int(x.replace(",", "")) for x in m.groups())
        if full and off / full >= 0.5:
            return f"滿{full:,}折{off:,}"
    return None


def coupon_is_taiwan(name):
    """優惠券沒有對到商品，只能看名字：要寫台灣地名。全站／全球通用的不要（使用者 2026-10-16 指定）。"""
    t = re.sub(r"台北富邦|\S{0,4}銀行\S{0,4}", "", name)        # 彰化銀行、台北富邦是銀行名不是地點
    if re.search(r"全站|全球", t):
        return False
    return any(w in t for w in TW_WORDS)


def clean_coupon_name(ref):
    """內部名稱「[TW_OP_cp][CP][Hook][Klook137]1016｜台灣5折｜APP_xxx」→「台灣5折」"""
    parts = [p.strip() for p in (ref or "").split("｜")]
    if len(parts) >= 2:
        hit = [p for p in parts[1:] if deep_discount(p)] or [parts[1]]
        return hit[0]
    t = re.sub(r"\[[^\]]*\]", "", ref or "").strip()
    return re.sub(r"_(One Time|monthly|Weekly|Oct|\d{6,}).*$", "", t, flags=re.I).strip("_ ") or ref


# ───────────── 讀 Klook ─────────────

JS_FETCH = """async (u) => {
  const r = await fetch(u, {headers: {currency: 'TWD', 'accept-language': 'zh-TW'}});
  return [r.status, await r.text(), r.url];
}"""


class Klook:
    """Klook 直接用程式連會被擋（403），跟旅遊地圖那支爬蟲一樣開無頭瀏覽器、在頁面裡呼叫。"""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._b = self._pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        self.page = self._b.new_page(locale="zh-TW")
        self.page.goto(BASE + "/zh-TW/", wait_until="domcontentloaded", timeout=60000)
        return self

    def __exit__(self, *a):
        self._b.close()
        self._pw.stop()

    def get(self, url):
        status, text, final = self.page.evaluate(JS_FETCH, url)
        if status != 200:
            raise RuntimeError(f"{url} 回 {status}")
        return text, final


def page_components(text):
    """getPageInfo 回的是一段 JS：window.$comps = [...]，把那個陣列拆出來。"""
    i = text.find("window.$comps = ")
    if i < 0:
        return []
    comps, _ = json.JSONDecoder().raw_decode(text, i + len("window.$comps = "))
    return comps if isinstance(comps, list) else list(comps.values())


def schedules(comp):
    """元件可能排了好幾段（每段從 dateTime 起生效），沒排就是一直有效。回傳 [(生效, 失效, 內容)]。"""
    d = comp.get("data") or {}
    if not d.get("_useSchedule"):
        return [(None, None, d)]
    rows = []
    for s in d.get("_scheduleData") or []:
        try:
            start = dt.datetime.strptime(s["dateTime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except (KeyError, ValueError):
            continue
        rows.append([start, None, s.get("schemaData") or {}])
    rows.sort(key=lambda r: r[0])
    for a, b in zip(rows, rows[1:]):
        a[1] = b[0]
    return [tuple(r) for r in rows]


def parse_time(*texts):
    for t in texts:
        m = re.search(r"(上午|中午|下午|晚上)?\s*(\d{1,2})[:：](\d{2})", t or "")
        if m:
            h = int(m.group(2))
            if m.group(1) in ("下午", "晚上") and h < 12:
                h += 12
            return h, int(m.group(3))
    return None


def parse_date(text, ref):
    m = re.search(r"(?<!\d)(1[0-2]|0?[1-9])/([0-3]?\d)(?!\d)", text or "")
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))
    year = ref.year + (1 if mo < ref.month - 6 else 0) - (1 if mo > ref.month + 6 else 0)
    try:
        return dt.date(year, mo, d)
    except ValueError:
        return None


def parse_weekdays(text):
    t = (text or "").replace("週末", "週六週日").replace("周", "週")
    days = set()
    for m in re.finditer(r"週([一二三四五六日天](?:\s*[、,／/]?\s*週?[一二三四五六日天])*)", t):
        days |= {WEEKDAY[c] for c in m.group(1) if c in WEEKDAY}
    return sorted(days)


def collect(kl):
    """讀活動頁，回傳 (優惠券清單, 商品清單, 讀了哪些頁)。"""
    todo, done = list(SEED_SLUGS), []
    coupons, items = {}, []
    while todo and len(done) < MAX_SLUGS:
        slug = todo.pop(0)
        if slug in done:
            continue
        done.append(slug)
        try:
            text, _ = kl.get(PAGE_API.format(slug=slug))
        except RuntimeError as e:
            log(f"活動頁 {slug} 讀不到：{e}")
            continue
        if slug in SEED_SLUGS:
            todo += [s for s in dict.fromkeys(re.findall(r"tetris/promo/([\w-]+)", text)) if s not in done]
        promo_url = f"{BASE}/zh-TW/tetris/promo/{slug}/"
        for comp in page_components(text):
            for start, end, sd in schedules(comp):
                section = sd.get("sectionTitle") or ""
                # 優惠券
                for tab in (sd.get("infoCouponData") or {}).get("tabs") or []:
                    for cp in tab.get("couponList") or []:
                        pid = cp.get("program_id")
                        if pid and pid not in coupons:
                            coupons[pid] = {"program_id": pid, "ref": cp.get("reference_name", ""),
                                            "start": cp.get("start_time_unix") or 0,
                                            "end": cp.get("end_time_unix") or 0,
                                            "section": section, "promo_url": promo_url}
                # 商品卡片
                groups = (sd.get("tabGroup") or {}).get("groups")
                if groups is None:
                    groups = [{"tabs": (sd.get("data") or {}).get("tabs") or []}]
                for g in groups:
                    for tab in g.get("tabs") or []:
                        label = tab.get("displayName") or tab.get("label") or ""
                        for a in tab.get("activities") or []:
                            desc = (a.get("description_text") or "").strip()
                            if not desc or not deep_discount(desc):
                                continue
                            items.append({"vertical_type": str(a.get("vertical_type")),
                                          "id": a.get("vertical_id") or a.get("activity_id"),
                                          "desc": desc, "label": label, "section": section,
                                          "section_desc": sd.get("description") or "",
                                          "sched_start": start, "sched_end": end, "promo_url": promo_url})
    return coupons, items, done


def coupon_details(kl, pids):
    out = {}
    pids = list(pids)
    for i in range(0, len(pids), 20):
        try:
            text, _ = kl.get(COUPON_API.format(ids=",".join(map(str, pids[i:i + 20]))))
            for it in (json.loads(text).get("result") or {}).get("items") or []:
                out[it["program_id"]] = it
        except (RuntimeError, ValueError) as e:
            log(f"優惠券說明讀不到：{e}")
    return out


def local_titles():
    """旅遊地圖那支爬蟲存過的 Klook 商品（1.3 萬筆）：商品編號 → 名稱、城市。"""
    out = {}
    for f in (ROOT.parent / "travel-map" / "data" / "klook_raw").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for c in d.get("cards") or []:
            x = c.get("data") or {}
            if x.get("vertical_id") and x.get("title"):
                out[int(x["vertical_id"])] = (x["title"], x.get("city_name") or "")
    return out


def product_infos(kl, ids, cache):
    """商品是哪一國：用 Klook 的 basic API 一次問一批（商品頁本身會擋程式，2026-10-16 一口氣問 155 頁整個被封）。
    名稱：先查旅遊地圖存過的；查不到才開商品頁，一次最多 3 頁、隔 3 秒、被擋就停。"""
    need = [i for i in dict.fromkeys(ids) if str(i) not in cache]
    for k in range(0, len(need), 30):
        chunk = need[k:k + 30]
        try:
            text, _ = kl.get(f"/v1/usrcsrv/activities/{','.join(map(str, chunk))}/basic?k_lang=zh_TW")
            for r in (json.loads(text).get("result") or []):
                cache[str(r["id"])] = {"country": r.get("country_code") or "", "title": None, "city": "",
                                       "url": f"{BASE}/zh-TW/activity/{r['id']}/"}
        except (RuntimeError, ValueError) as e:
            log(f"商品國別讀不到：{e}")
    titles = None
    tries = 0
    for i in ids:
        info = cache.get(str(i))
        if not info or info["country"] != "TW" or info["title"]:
            continue
        if titles is None:
            titles = local_titles()
        if int(i) in titles:
            info["title"], info["city"] = titles[int(i)]
            continue
        if tries >= 3:
            continue
        tries += 1
        time.sleep(3)
        try:
            html, final = kl.get(f"/zh-TW/activity/{i}/")
        except RuntimeError as e:
            log(f"商品頁 {i} 讀不到（只會少名字）：{e}")
            tries = 99
            continue
        t = re.search(r"<title[^>]*>([^<]*)", html)
        c = re.search(r'"city_name":"([^"]+)"', html)
        if t and "klook.com" != t.group(1).strip():
            info["title"] = re.sub(r"\s*[-|｜]\s*Klook.*$", "", t.group(1)).strip()
            info["city"] = c.group(1) if c else ""
            info["url"] = final.split("?")[0]
    return cache


def build_events(kl, cfg):
    """讀 Klook 整理成提醒用的清單：每一筆有開搶時間（沒有就是「現在就有」）。"""
    t0 = now()
    coupons, items, slugs = collect(kl)
    events = {}

    # ① 優惠券：名字看折扣深不深、是不是台灣能用
    live = {pid: c for pid, c in coupons.items() if c["end"] and c["end"] > t0.timestamp()}
    details = coupon_details(kl, live)
    for pid, c in live.items():
        d = details.get(pid) or {}
        name = clean_coupon_name(d.get("description") or c["ref"])
        text = f"{name} {clean_coupon_name(c['ref'])} {d.get('program_tag', '')}"
        disc = deep_discount(text)
        if not disc or not coupon_is_taiwan(f"{name} {clean_coupon_name(c['ref'])}"):
            continue
        terms = ((d.get("usage") or {}).get("terms_and_conditions") or {}).get("text") or ""
        start = dt.datetime.fromtimestamp(c["start"], TZ) if c["start"] else None
        key = f"coupon:{pid}"
        events[key] = {"key": key, "kind": "優惠券", "title": name, "discount": disc,
                       "start": start.isoformat() if start else None,
                       "end": dt.datetime.fromtimestamp(c["end"], TZ).isoformat(),
                       "where": "全站／全球" if re.search(r"全站|全球", name) else "台灣",
                       "app_only": "APP" in c["ref"].upper() or "App" in terms,
                       "url": c["promo_url"], "section": c["section"]}

    # ② 商品卡片：要是台灣的商品
    cache = load(PRODUCTS, {})
    skip_ids = {int(x) for x in cfg.get("exclude_ids") or []}
    items = [it for it in items if it["vertical_type"] in ("100", "106") and int(it["id"]) not in skip_ids
             and not (it["sched_end"] and it["sched_end"] <= t0)]
    product_infos(kl, [it["id"] for it in items], cache)
    for it in items:
        info = cache.get(str(it["id"]))
        if not info or info["country"] != "TW":
            continue
        info = {**info, "title": info["title"] or f"Klook 商品 #{it['id']}（名稱沒抓到，點連結看）"}
        ref = it["sched_start"] or t0
        hm = parse_time(it["desc"], it["label"], it["section_desc"])
        day = parse_date(it["desc"], ref) or parse_date(it["label"], ref)
        starts = []
        if day:
            starts = [dt.datetime(day.year, day.month, day.day, *(hm or (0, 0)), tzinfo=TZ)]
        else:
            wds = parse_weekdays(it["desc"])
            if wds:
                lo = max(ref, t0 - dt.timedelta(days=1)).date()
                hi = (it["sched_end"] or (max(ref, t0) + dt.timedelta(days=7))).date()
                d = lo
                while d < hi:
                    if d.weekday() in wds:
                        starts.append(dt.datetime(d.year, d.month, d.day, *(hm or (0, 0)), tzinfo=TZ))
                    d += dt.timedelta(days=1)
        base = {"kind": "商品", "title": info["title"], "city": info["city"], "discount": deep_discount(it["desc"]),
                "desc": it["desc"], "url": info["url"], "section": it["section"], "where": "台灣",
                "has_time": bool(hm)}
        if not starts:   # 沒寫哪一天：現在就有
            key = f"item:{it['vertical_type']}:{it['id']}:now"
            events.setdefault(key, {**base, "key": key, "start": None,
                                    "end": it["sched_end"].isoformat() if it["sched_end"] else None})
        for s in starts:
            if s < t0 - dt.timedelta(hours=12):
                continue
            key = f"item:{it['vertical_type']}:{it['id']}:{s:%Y%m%d%H%M}"
            events.setdefault(key, {**base, "key": key, "start": s.isoformat(), "end": None})
    save(PRODUCTS, cache)
    words = [w for w in cfg.get("exclude_words") or [] if w]
    events = {k: e for k, e in events.items() if not any(w in e["title"] for w in words)}
    log(f"讀了 {len(slugs)} 個活動頁（{', '.join(slugs)}）：優惠券 {len(live)} 張、"
        f"深折扣商品卡 {len(items)} 張 → 挑到 {len(events)} 筆")
    return events


# ───────────── 推通知 ─────────────

def push(cfg, title, body, url=None):
    ok = False
    if cfg.get("ntfy_topic"):
        msg = {"topic": cfg["ntfy_topic"], "title": title, "message": body, "tags": ["tada"], "priority": 4}
        if url:
            msg["click"] = url
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            ok = True
        except Exception as e:  # noqa: BLE001 網路錯誤種類很多，記下來就好
            log(f"ntfy 推送失敗：{e}")
    if cfg.get("line_channel_token") or cfg.get("line_channel_secret"):
        try:
            token = cfg.get("line_channel_token") or line_token(cfg)
            text = f"{title}\n{body}" + (f"\n{url}" if url else "")
            msg = {"messages": [{"type": "text", "text": text[:4900]}]}
            if cfg.get("line_user_id"):
                api, msg["to"] = "push", cfg["line_user_id"]
            else:
                api = "broadcast"   # 沒填自己的 userId 就推給官方帳號全部好友（只有自己一個）
            req = urllib.request.Request(f"https://api.line.me/v2/bot/message/{api}",
                                         data=json.dumps(msg).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            urllib.request.urlopen(req, timeout=20).read()
            ok = True
        except Exception as e:  # noqa: BLE001
            log(f"LINE 推送失敗：{e}")
    return ok


def line_token(cfg):
    """用頻道 ID＋密鑰換一把 15 分鐘的金鑰，每次推之前現換，不用管過期。"""
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cfg["line_channel_id"],
                                   "client_secret": cfg["line_channel_secret"]}).encode()
    r = urllib.request.urlopen(urllib.request.Request("https://api.line.me/oauth2/v3/token", data=data), timeout=20)
    return json.loads(r.read())["access_token"]


def when_text(ev):
    if not ev.get("start"):
        return "現在就有"
    s = dt.datetime.fromisoformat(ev["start"])
    wd = "一二三四五六日"[s.weekday()]
    t = f"{s:%H:%M}" if (ev.get("kind") == "優惠券" or ev.get("has_time")) else "（時間以商品頁為準）"
    return f"{s.month}/{s.day}（{wd}）{t}"


def line_of(ev):
    head = f"{when_text(ev)}｜{ev['title']}"
    if ev["kind"] == "優惠券":
        tail = f"{ev['discount']}優惠碼" + ("，要用 App 領" if ev.get("app_only") else "")
    else:
        tail = ev.get("desc") or ev["discount"]
        if ev.get("city"):
            head += f"（{ev['city']}）"
    return f"• {head}\n  {tail}"


def notify_new(cfg, events, sent):
    t = now()
    new = [e for k, e in events.items() if k not in sent["announced"]
           and (not e.get("start") or dt.datetime.fromisoformat(e["start"]) > t)]
    if not new:
        return
    new.sort(key=lambda e: e["start"] or "")
    body = "\n".join(line_of(e) for e in new[:15])
    if len(new) > 15:
        body += f"\n…另外還有 {len(new) - 15} 筆"
    if push(cfg, f"Klook 新優惠 {len(new)} 筆", body, new[0]["url"]):
        sent["announced"] += [e["key"] for e in new]
        log(f"推了新優惠 {len(new)} 筆")


def left_text(mins):
    return (f"{mins // 60} 小時" + (f" {mins % 60} 分" if mins % 60 else "")) if mins >= 60 else f"{mins} 分鐘"


def remind_title(evs, mins):
    names = [e["title"] if e["kind"] == "優惠券" else f"{e['title'][:12]} {e['discount']}" for e in evs]
    return f"Klook 再 {left_text(mins)}開搶：" + "、".join(names)[:60]


def notify_due(cfg, events, sent):
    t = now()
    rem = sorted(cfg["remind_minutes"])
    due = {}
    for e in events.values():
        if not e.get("start"):
            continue
        s = dt.datetime.fromisoformat(e["start"])
        ms = [m for m in rem if s - dt.timedelta(minutes=m) <= t < s]
        if not ms:
            continue
        tag = f"{e['key']}@{min(ms)}"
        if tag in sent["reminded"]:
            continue
        due.setdefault((e["start"], min(ms)), []).append(e)
    for (start, m), evs in sorted(due.items()):
        mins = int((dt.datetime.fromisoformat(start) - t).total_seconds() // 60) + 1
        if push(cfg, remind_title(evs, mins), "\n".join(line_of(e) for e in evs), evs[0]["url"]):
            for e in evs:
                s = dt.datetime.fromisoformat(e["start"])
                sent["reminded"] += [f"{e['key']}@{x}" for x in rem if s - dt.timedelta(minutes=x) <= t]
            log(f"提醒 {start} 開搶 {len(evs)} 筆")


def next_reminder(cfg, events, sent):
    """還沒發過的提醒裡，最早該發的時間。"""
    t, best = now(), None
    for e in events.values():
        if not e.get("start"):
            continue
        s = dt.datetime.fromisoformat(e["start"])
        if s <= t:
            continue
        for m in cfg["remind_minutes"]:
            at = s - dt.timedelta(minutes=m)
            if f"{e['key']}@{m}" not in sent["reminded"] and (best is None or at < best):
                best = at
    return best


def next_scan_time(cfg):
    """下一次該讀 Klook 的時間（今天的 scan_at 還沒讀就是今天，否則明天）。"""
    t = now()
    hh, mm = map(int, cfg["scan_at"].split(":"))
    at = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return at if at > t else at + dt.timedelta(days=1)


def need_scan(cfg):
    """每天 scan_at 之後讀一次；超過 26 小時沒讀（例如前一次失敗）也讀。"""
    last = load(EVENTS, {}).get("scanned_at")
    if not last or os.environ.get("KLOOK_FORCE_SCAN") == "true":
        return True
    last, t = dt.datetime.fromisoformat(last), now()
    hh, mm = map(int, cfg["scan_at"].split(":"))
    today_at = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (t >= today_at > last) or t - last > dt.timedelta(hours=26)


# ───────────── 指令 ─────────────

def do_scan(cfg):
    with Klook() as kl:
        events = build_events(kl, cfg)
    # 記每一筆第一次看到的時間：之後看得出 Klook 通常提前幾天、幾點排上去
    old = load(EVENTS, {}).get("events", {})
    for k, e in events.items():
        e["first_seen"] = (old.get(k) or {}).get("first_seen") or now().isoformat(timespec="minutes")
    save(EVENTS, {"scanned_at": now().isoformat(), "events": events, "fail": 0})
    return events


def show(events):
    for e in sorted(events.values(), key=lambda e: e["start"] or ""):
        print(line_of(e))
        print(f"  {e['url']}")


def tick():
    cfg = config()
    state = load(EVENTS, {})
    sent = {"announced": [], "reminded": [], **load(SENT, {})}
    events = state.get("events", {})
    if need_scan(cfg):
        try:
            events = do_scan(cfg)
        except Exception:  # noqa: BLE001 讀不到就用舊資料繼續提醒，連續失敗才通知
            log("讀 Klook 失敗：" + traceback.format_exc(limit=3))
            state["fail"] = state.get("fail", 0) + 1
            state["scanned_at"] = now().isoformat()    # 下一次照間隔再試，不要每 5 分鐘狂試
            save(EVENTS, state)
            if state["fail"] == 3:
                push(cfg, "Klook 優惠提醒讀不到 Klook", "連續三次讀不到活動頁，提醒可能會漏，請看電腦上的 log.txt。")
    notify_new(cfg, events, sent)
    notify_due(cfg, events, sent)
    save(SENT, sent)
    # GitHub 的定時工作不準（10/16 八小時才跑一次），LINE 又不能預約：
    # 雲端這一次就醒著等，有提醒時間到就發；KLOOK_CHAIN＝等到時限才結束，由工作流程再開下一次接力。
    # 時限最晚到下一次該讀 Klook 的時間，讓下一棒去讀。
    wait = int(os.environ.get("KLOOK_WAIT_MINUTES") or 0)
    deadline = min(now() + dt.timedelta(minutes=wait), next_scan_time(cfg))
    while True:
        nxt = next_reminder(cfg, events, sent)
        if not nxt or nxt > deadline:
            if os.environ.get("KLOOK_CHAIN") == "true" and deadline > now():
                log(f"沒有要發的，等到 {deadline:%m/%d %H:%M} 交給下一棒")
                time.sleep((deadline - now()).total_seconds())
            break
        secs = (nxt - now()).total_seconds()
        if secs > 0:
            log(f"等 {secs / 60:.0f} 分鐘到 {nxt:%H:%M} 發提醒")
            time.sleep(secs + 1)
        notify_due(cfg, events, sent)
        save(SENT, sent)     # 發一則存一次，工作被中途砍掉也不會重發
    sent["announced"] = [k for k in sent["announced"] if k in events]
    sent["reminded"] = [k for k in sent["reminded"] if k.split("@")[0] in events]
    save(SENT, sent)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    cfg = config()
    if cmd == "scan":
        show(do_scan(cfg))
    elif cmd == "list":
        show(load(EVENTS, {}).get("events", {}))
    elif cmd == "test":
        ok = push(cfg, "Klook 優惠提醒：測試", "收到這則就設定好了（GitHub 雲端送出）。之後五折以上、買一送一的台灣優惠會推到這裡。",
                  BASE + "/zh-TW/tetris/promo/klook137/")
        print("已送出" if ok else "送不出去，看 data/log.txt")
    elif cmd == "tick":
        tick()
    elif cmd == "needscan":           # 雲端用：要讀 Klook 才裝瀏覽器
        print("yes" if need_scan(cfg) else "no")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
