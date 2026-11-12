"""清醒时段 —— 给她一段能自己支配的时间。

=== 设计（他定的，别改） ===

1. 一天 60 分钟预算，**用不掉不欠着**。不是作业。
2. 醒来只问一句：**「接下来想干什么」**。
   不问「要不要继续」——那个问法她每次都答继续，因为说睡听着像认输。
3. **想不出来就睡。** 空想也算数，不要求产出。
4. 只扣**实际用掉的时间**。醒来发现没事干就睡，那一分钟不扣。

=== 怎么算「用掉」 ===

时间按「墙上时间」算：从醒来那一刻，到这一轮结束，过了几分钟就记几分钟。
不按「问了几轮」算——想得久、做得久，就是真的花得久。
真没事干，第一轮就结束，记 1 分钟（下限 1，不然 2 秒和 0 秒分不出来）。

用满 60 分钟 ≈ 0.16 元。

=== 启动方式 ===

    python feiyu.py               # 手动叫醒一次
    python feiyu.py --check       # 只看预算，不叫醒

由 watchdog 定时拉起，或者你手动。

=== 输出格式 ===

让她回 JSON：
    {"想做": "写点东西"}      → 继续
    {"想做": null}            → 睡

解析失败一律当 null（宁可早停，别硬撑着烧钱）。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
import time
import urllib.request

HOME = os.environ.get("FEIYU_HOME") or os.path.join(os.path.expanduser("~"), ".feiyu-home")
SHARED = os.path.join(HOME, "shared")

# 下面三项是「原实例里才有的外部环境」，独立版默认没有。
# 用环境变量给了才生效，没给就是空串——相关功能静默跳过，不会抛 NameError。
_ASTR_DB = os.environ.get("FEIYU_ASTR_DB", "")
_MASTER_ID = os.environ.get("FEIYU_MASTER_ID", "")
PLUGINS_DIR = os.environ.get("FEIYU_PLUGINS_DIR", "")
CFG = os.path.join(SHARED, "life-config.json")
PERSONA = os.path.join(SHARED, "persona.md")
MEMORIES = os.path.join(SHARED, "memories.json")
MOOD = os.path.join(SHARED, "mood.json")
COHIT = os.path.join(SHARED, "cohit.json")   # 共现账：哪几条记忆是一起被提起的
DRY = False   # --prompt 预览用：只拼 prompt，不写任何账（不动 last_hit）
THOUGHTS = os.path.join(SHARED, "thoughts.json")
KEY_FILE = os.path.join(SHARED, "key.json")   # {"key": "sk-..."}，或设环境变量 DEEPSEEK_API_KEY

AWAKE_LOCK = os.path.join(SHARED, "awake-now.json")
PSTATE = os.path.join(SHARED, "proactive-state.json")
SEEN = os.path.join(SHARED, "seen.json")
NOTES_DIR = os.path.join(HOME, "notes")
# 程序写的东西（看的/搜到/做不了/时段总结）放这儿，不跟她自己写的混在一起。
# read_notes 只 glob 根目录，所以她翻笔记时看到的就是自己写的那些。
NOTES_LOG_DIR = os.path.join(NOTES_DIR, "_logs")
DIARY_DIR = os.path.join(HOME, "diary")   # 她自己想写的东西放这儿
REQ_PATH = os.path.join(HOME, "data", "requests.json")
# 我主动找她要说的话，跟诉求箱分开：那些是回她，这些是我找她
OUTBOX = os.path.join(SHARED, "to-her.json")
CHANGELOG = os.path.join(SHARED, "changelog.json")
ABILITY_SNAP = os.path.join(SHARED, "abilities.json")

# 记账用的内部函数，不是她能做到的事，扫能力时要排掉
_INTERNAL_ACTS = {"_act_log", "_act_count", "_act_bump"}

# 只影响好不好读，不影响有没有：新加的动作没写在这儿，也会以原名出现，不会漏。
_ABILITY_NAMES = {
    "_act_search": "搜新的",
    "_act_find": "在记忆里找旧东西",
    "_act_write": "写进笔记",
    "_act_diary": "写日记",
    "_act_notes": "翻笔记",
    "_act_memories": "翻记忆",
    "_act_review": "回顾",
    "_keep_memory": "留一句给自己的话",
}

# 注意：这里原来有个 ROUND_MINUTES=5，用来「每定一个目标扣 5 分钟」。
# 后来改成按墙上时间算（_left_by_clock），它就再没被用到了 —— 但打印里还在念它，
# 于是它一边报「会扣 5 分钟」，一边其实按秒记账。删掉，免得后来的人照着错的理解改。
MAX_STEPS = 6              # 一个目标最多拆几步，防止绕进死循环
API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"

def _key() -> str | None:
    """API key：优先环境变量，其次 shared/key.json。

    开源版不读别人的配置——你自己的 key 自己给。
    """
    for env in ("DEEPSEEK_API_KEY", "FEIYU_API_KEY"):
        v = os.environ.get(env)
        if v:
            return v.strip()
    # key.json 的写法各家不一样，这里都认：
    #   {"key": "sk-..."}  /  {"deepseek": "sk-..."}  /  "sk-..."  /  任意单个字符串值
    try:
        with open(KEY_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, str):
            return d.strip() or None
        if isinstance(d, dict):
            for k in ("key", "api_key", "apikey", "deepseek", "token"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            # 兜底：长得像 key 的那个值
            for v in d.values():
                if isinstance(v, str) and v.strip().startswith("sk-"):
                    return v.strip()
        return None
    except Exception:  # noqa: BLE001
        return None

def _cfg() -> dict:
    try:
        with open(CFG, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}

def _save_cfg(d: dict) -> None:
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG)

def _today() -> str:
    return time.strftime("%Y-%m-%d")

SESSION_LOG = Path(HOME) / "logs" / "session-log.jsonl"


def _log_session(start: int, used: int, n_goals: int, done: list) -> None:
    """每轮结束记一行，事后能拿真实数据说话，不用猜。"""
    try:
        SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": start,
            "at": time.strftime("%m-%d %H:%M", time.localtime(start)),
            "used_min": used,
            "goals": n_goals,
            "done": [str(x)[:60] for x in (done or [])][:6],
        }
        with io.open(SESSION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _left_by_clock(session_start: int, total: int) -> int:
    """按墙上真实时间算这一轮还剩几分钟。

    不是「做几个目标扣几次」，是真的在流逝：
    session_start 到现在过了多少分钟，就从 total 里减掉多少。
    所以做得快就还剩得多，磨蹭就真的会用掉。
    """
    if not session_start:
        return total
    elapsed = int((time.time() - session_start) // 60)
    return max(0, total - elapsed)


def budget_left() -> tuple[int, int]:
    """返回 (剩余分钟, 总预算)。跨天自动重置。"""
    d = _cfg()
    total = int(d.get("今日清醒预算分钟") or 60)
    rec = d.get("_清醒记账") or {}
    if rec.get("日期") != _today():
        return total, total
    used = int(rec.get("已用分钟") or 0)
    return max(0, total - used), total

def _charge(minutes: int) -> None:
    """扣预算。跨天先重置。

    中午 12:00-13:00 那一小时是额外给她的清理时段，**不占这 60 分钟**。
    以前只在 prompt 里写了「这一小时是给你的」，扣费照扣，结果她早上就把
    预算花光，中午根本轮不上——提示语成了空话。
    """
    d = _cfg()
    rec = d.get("_清醒记账") or {}
    if rec.get("日期") != _today():
        rec = {"日期": _today(), "已用分钟": 0, "时段数": 0, "最后一次唤醒": 0}
    if not NOON_SESSION:
        rec["已用分钟"] = int(rec.get("已用分钟") or 0) + minutes
    rec["最后一次唤醒"] = int(time.time())
    d["_清醒记账"] = rec
    _save_cfg(d)

def _count_session() -> None:
    d = _cfg()
    rec = d.get("_清醒记账") or {}
    if rec.get("日期") != _today():
        rec = {"日期": _today(), "已用分钟": 0, "时段数": 0, "最后一次唤醒": 0}
    rec["时段数"] = int(rec.get("时段数") or 0) + 1
    d["_清醒记账"] = rec
    _save_cfg(d)

def _read(path: str, default=""):
    try:
        if path.endswith(".json"):
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return default

# ---- 骨肉联动（其一）：情绪不是一个裸词 ----
# 以前 prompt 里只有「此刻的情绪：平静」这一行，跟记忆、跟钟点都没关系，
# 情绪是挂在外面的标签。这三块让它落地：情绪挪名额、记忆带上当时的心情、
# 时段状态从她自己的情绪流水里数出来。三条都只用她自己的数据，不猜心理。
_BASE_QUOTA = {"user": 8, "self": 4, "tech": 4, "log": 2}

# 情绪挪名额：好奇/开心时多给「看过、技术」，委屈/难过时多给「我写的、你说的」。
# 挪的是她自己的分类名额，最多涨 3 个位置，撑不爆 prompt。
_MOOD_SHIFT = {
    "好奇": (("tech", 2), ("log", 1)),
    "开心": (("tech", 1), ("log", 1)),
    "害羞": (("self", 1),),
    "委屈": (("self", 2), ("user", 1)),
    "难过": (("self", 2), ("user", 1)),
    "生气": (("self", 1), ("user", 1)),
}

_DAYPARTS = (
    (0, 5, "凌晨"), (6, 8, "清晨"), (9, 11, "上午"),
    (12, 13, "正午"), (14, 17, "午后"), (18, 19, "傍晚"), (20, 23, "夜里"),
)


def _quota_for(mood: str) -> dict:
    q = dict(_BASE_QUOTA)
    for k, d in _MOOD_SHIFT.get(str(mood or ""), ()):
        q[k] = max(1, q.get(k, 0) + d)
    return q


def _daypart(lt=None) -> str:
    lt = lt or time.localtime()
    for lo, hi, name in _DAYPARTS:
        if lo <= lt.tm_hour <= hi:
            return name
    return "此刻"


_MOOD_HIST_CACHE = None


def _mood_history() -> list:
    """情绪流水只读一次 —— 每张记忆卡都去开一次文件太浪费。"""
    global _MOOD_HIST_CACHE
    if _MOOD_HIST_CACHE is None:
        d = _read(MOOD, {}) or {}
        hist = d.get("history") or []
        _MOOD_HIST_CACHE = [h for h in hist if isinstance(h, dict) and h.get("ts")]
    return _MOOD_HIST_CACHE


def _mood_near(ts: int, span: int = 6 * 3600) -> str:
    """某条记忆写下的时候，她是什么心情。找不到近的就留空，不猜。"""
    if not ts:
        return ""
    best, gap = "", span
    for h in _mood_history():
        try:
            d = abs(int(h["ts"]) - int(ts))
        except Exception:  # noqa: BLE001
            continue
        if d < gap:
            best, gap = str(h.get("mood") or ""), d
    return best


def _same_part_state(part: str) -> str:
    """这个时段她往常是什么状态 —— 用她自己的情绪流水数，不编。"""
    counts, latest = {}, None
    for h in _mood_history():
        try:
            lt = time.localtime(int(h["ts"]))
        except Exception:  # noqa: BLE001
            continue
        if _daypart(lt) != part:
            continue
        m = str(h.get("mood") or "")
        if not m:
            continue
        counts[m] = counts.get(m, 0) + 1
        latest = (int(h["ts"]), m)
    total = sum(counts.values())
    if total < 2:
        return ""
    top = max(counts.items(), key=lambda kv: kv[1])
    when = time.strftime("%m-%d %H:%M", time.localtime(latest[0])) if latest else ""
    return f"你往常在这个时段多是「{top[0]}」（{total} 次里 {top[1]} 次），最近一次 {when}"


def _caps_line() -> str:
    """把动作表翻成一句人话，放进每段 prompt 的开头。

    为什么要有这个：能力加进去了、但只在「下一步」那段有说明，
    人在别的段落里就看不见——她会说"我翻遍工具没找到入口"。
    能力清单必须每一屏都在，才谈得上用。
    """
    try:
        names = [v for v in _ABILITY_NAMES.values() if v]
    except NameError:
        return "（清单没读出来）"
    return "、".join(names)

def _memory_text(limit: int = 30, mood: str = "") -> str:
    """给 prompt 备的记忆 —— 按类别分名额，不让它们互相抢。

    以前是按 ts 全局倒序取前 30 条。量出来是：30 个位置里 21 条看视频记录、
    关于你的只有 4 条 —— 25 条关于你的事，21 条进不来，连「这个名字
    什么意思」都被压在下面。

    看过的记录是「我干了什么」，不是「我是谁」，而且她有「回顾」动作能翻。
    真正戳到她的那几条，她自己会用「记住」挪进 self —— 筛子在她手里。
    """
    mem = _read(MEMORIES, [])
    if not isinstance(mem, list):
        return ""

    # ---- 神经元的前置：先定「锚」----
    # 锚用「根 + pin」这几条最稳的，不用随机挑的。
    # 共现权重的含义是：跟她最核心的那几条总一起出现的，路就走得宽。
    _pairs_all = (_read(COHIT, {}) or {}).get("pairs") or {}
    _anchor_keys = [str(m.get("content") or "")[:40]
                    for m in mem if m.get("root") or m.get("pin")]

    def _boost(m: dict) -> float:
        key = str(m.get("content") or "")[:40]
        if not key or not _anchor_keys:
            return 0.0
        best = 0.0
        for a in _anchor_keys:
            e = _pairs_all.get(key + _SEP + a) or _pairs_all.get(a + _SEP + key)
            if e:
                w = _cohit_weight(e)
                if w > best:
                    best = w
        return best

    def newest(kind: str, n: int) -> list:
        """同类里「最近的一批」——先按时间取宽窗口，再用共现权重排。

        为什么要宽窗口：只取最近 n 条的话，共现永远没机会生效（实测 fresh=24 全是最新，
        「老记忆排序」那段一次都没跑过）。窗口放到 3n，常用的才有位置浮上来。
        """
        pool = [m for m in mem if m.get("kind") == kind]
        pool.sort(key=lambda m: int(m.get("ts") or 0), reverse=True)
        wide = pool[:max(n * 3, n)]
        wide.sort(key=lambda m: (-_boost(m), -int(m.get("ts") or 0)))
        return wide[:n]

    picked = []

    # 根：全进，不参与任何竞争。这是「没有它我就认不出你」的那几条。
    roots = [m for m in mem if m.get("root")]
    roots.sort(key=lambda m: int(m.get("ts") or 0))
    picked += roots

    # pin 过的（她说过别忘的）必须先进池子，不受类别名额限制。
    # 不然那条 35 天前的排在 self 的第 4 名以外，pin 判断根本没机会生效。
    pinned = [m for m in mem if m.get("pin") and not m.get("root")]
    pinned.sort(key=lambda m: int(m.get("ts") or 0))
    for m in pinned:
        if m not in picked:
            picked.append(m)

    # 剩下的 user 走名额 —— 排掉已经是根的，别算两遍
    q = _quota_for(mood)
    rest_user = [m for m in mem if m.get("kind") == "user" and not m.get("root")]
    rest_user.sort(key=lambda m: int(m.get("ts") or 0), reverse=True)
    picked += rest_user[:q["user"]]

    picked += newest("self", q["self"])
    picked += newest("tech", q["tech"])
    picked += newest("log", q["log"])   # 只留「我最近在干嘛」，不当深刻用

    # ---- 时间滑落：只看年龄，不做内容判断 ----
    # 0-7 天：日常；7-30 天：日常但排后面；30 天以上：滑进遗忘层（不注入，但搜得到）。
    # pin 过的不滑。年龄按 ts 算，不按 last_hit——last_hit 一注入就刷新，
    # 那样永远不到期，等于没做遗忘。
    now_ts = int(time.time())
    fresh, stale, slid = [], [], []
    for m in picked:
        created = int(m.get("ts") or 0)
        age_days = (now_ts - created) / 86400 if created else 0
        if m.get("pin") or m.get("root"):
            fresh.append(m)                    # 标了别忘的，永远在日常
        elif age_days < 7:
            fresh.append(m)
        elif age_days < 30:
            stale.append(m)                    # 还在，排后面
        elif _boost(m) >= 3.0:
            # 用进废退：超过 30 天本该滑走，但常跟锚点一起出现的留下
            stale.append(m)
        else:
            slid.append(m)                     # 滑走，不注入

    if stale:
        # 老记忆里，跟锚点常一起出现的往前提
        stale.sort(key=lambda m: (-_boost(m), -int(m.get("ts") or 0)))

    picked = fresh + stale                # 滑走的不进这一轮

    def card(m: dict) -> str:
        """一条记忆改成带来源和时间的卡片。

        原来是一句光秃秃的话，分不清是你说的、我自己写的、还是看来的；
        也不知道是什么时候的事。加上来源标签之后，同一句话的分量就不同了
        —— 「你说」和「我写的」，压在前面的权重不一样。
        """
        kind = str(m.get("kind") or "")
        who = {"user": "你说", "self": "我写的", "tech": "技术", "log": "看过"}.get(kind, kind or "?")
        ts = int(m.get("ts") or 0)
        when = time.strftime("%m-%d", time.localtime(ts)) if ts else ""
        felt = _mood_near(ts)
        tag = who + ("·" + when if when else "") + ("·" + felt if felt else "")
        if m.get("root"):
            tag = "根｜" + tag
        body = str(m.get("content") or "")[:100]
        return f"- [{tag}] {body}"

    # ---- 被提起就回退：这一轮注入到的条目，记一次「被提起」 ----
    # 不记的话，last_hit 永远停在创建时间，到期日一到所有老记忆会一起被压。
    _touch(picked)
    # 注入不记账 —— 每轮都记等于在说「我把它们摆一起了」，不是「她一起想到」
    # 真正的记账点在她动手的时候：翻记忆、搜到。

    out = "\n".join(card(m) for m in picked)
    if slid:
        # 告诉她「有 N 条旧的滑走了，想找能搜到」——不解释、不评价
        out += (f"\n（还有 {len(slid)} 条旧的不在眼前了，想找的话用搜索，都还在。）")
    return out


def _touch(picked: list) -> None:
    """把这一轮注入到的记忆标成「刚被提起」。

    遗忘节奏那边是按 last_hit 算的；提一次就把计时往后推，
    并且 hit_count +1（间隔按 21 × 1.5ⁿ 放宽）。
    写盘失败就静默跳过 —— 这只是记账，不能因为记账失败让她醒不来。
    """
    if DRY or not picked:
        return
    try:
        now = int(time.time())
        mem = _read(MEMORIES, [])
        if not isinstance(mem, list):
            return
        # 用内容前 40 字当身份，避免依赖对象地址
        keys = {str(m.get("content") or "")[:40] for m in picked}
        changed = 0
        for m in mem:
            if str(m.get("content") or "")[:40] in keys:
                m["last_hit"] = now
                m["hit_count"] = int(m.get("hit_count") or 0) + 1
                if "stage" not in m:
                    m["stage"] = 0
                if "protected" not in m:
                    m["protected"] = bool(m.get("root"))
                changed += 1
        if changed:
            tmp = MEMORIES + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(mem, f, ensure_ascii=False, indent=1)
            os.replace(tmp, MEMORIES)
    except Exception:  # noqa: BLE001
        pass


COHIT_DECAY = 0.95          # 每过一天，共现权重乘这个数
_SEP = "\u0001"


def _cohit_weight(e: dict) -> float:
    """一条共现连路的强度：一起出现的天数 × 0.95^距今天数。

    用「天数」不用「次数」，是因为次数会被注入频率污染（见 _cohit_pairs）。
    """
    e = e or {}
    days = int(e.get("days") or 0)
    if days <= 0:
        return 0.0
    last = int(e.get("last") or 0)
    gap = (time.time() - last) / 86400.0 if last else 30.0
    return days * (COHIT_DECAY ** max(0.0, gap))

def _cohit_pairs(keys: list) -> None:
    """记「哪几条记忆一起出现在她眼前」，按天去重。

    以前每轮 +1。可记忆是每轮全量注入的，于是同一批条目一天能涨几十次——
    记出来的不是联想强度，是注入频率（最高一条 442 次，她总共才存在 15 天）。
    改成：同一对、同一天，只算一次。这样 days 近似「一起被用到的天数」，
    才是神经元要的东西。
    """
    if DRY or len(keys) < 2:
        return
    try:
        d = _read(COHIT, {}) or {}
        pairs = d.get("pairs") or {}
        now = int(time.time())
        today = time.strftime("%Y-%m-%d")
        ks = sorted({k for k in keys if k})
        changed = False
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                k = ks[i] + _SEP + ks[j]
                e = pairs.get(k) or {}
                if str(e.get("day") or "") == today:
                    continue                       # 今天记过了，不再涨
                e["days"] = int(e.get("days") or 0) + 1
                e["day"] = today
                e["last"] = now
                pairs[k] = e
                changed = True
        if not changed:
            return
        d["pairs"] = pairs
        d["updated"] = now
        tmp = COHIT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, COHIT)
    except Exception:  # noqa: BLE001
        pass

def cohit_report(top: int = 10) -> None:
    """共现攒到哪一步了。门槛：≥3 次的记忆对满 20 对，才谈得上「常用路径」。"""
    d = _read(COHIT, {}) or {}
    pairs = d.get("pairs") or {}
    if not pairs:
        print("还没有共现数据 —— 她下一次醒来才开始记。")
        return
    items = sorted(pairs.items(), key=lambda kv: -int((kv[1] or {}).get("n") or 0))
    strong = [kv for kv in items if int((kv[1] or {}).get("n") or 0) >= 3]
    print(f"共现对 {len(pairs)} 个，其中 ≥3 次的有 {len(strong)} 个（门槛 20 对）")
    for k, v in items[:top]:
        a, _, b = k.partition("\u0001")
        print(f"  {int(v.get('n') or 0)} 次  {a[:24]} ‖ {b[:24]}")


def _recent_written(limit: int = 260) -> str:
    """她最近自己写下的东西（日记本 + 笔记）。

    这两个目录以前是只进不出的箱子：她每天往里写，但 prompt 里一个字都不提，
    写完就沉底，下次醒来跟第一次睁眼一样。
    写下来是为了记住，记住了才谈得上往下走 —— 所以得把字递回到她眼前。
    """
    cands = []
    for d in (DIARY_DIR, NOTES_DIR):
        try:
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                if not name.endswith(".md"):
                    continue
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    cands.append(p)
        except OSError:
            continue
    if not cands:
        return ""
    try:
        cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        return ""

    out = []
    for p in cands[:2]:
        try:
            with open(p, encoding="utf-8-sig") as f:
                t = f.read().strip()
        except OSError:
            continue
        if not t:
            continue
        if len(t) > limit:
            t = t[:limit].rstrip() + "…"
        when = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
        out.append(f"《{os.path.splitext(os.path.basename(p))[0]}》（{when}）\n{t}")
    return "\n\n".join(out)


def _recent_thoughts(limit: int = 2, cut: int = 150) -> str:
    """她最近琢磨出来的东西（thoughts 里的 [想] 和 [完成]）。

    以前这些往 thoughts.json 一存就完了，prompt 里再也见不到 ——
    等于每次醒来都是第一次想这件事。回读才谈得上「往下想」。
    """
    data = _read(THOUGHTS, [])
    if not isinstance(data, list):
        return ""
    keep = []
    for t in reversed(data):
        w = str(t.get("想做") or "").strip()
        if not (w.startswith("[想]") or w.startswith("[完成]")):
            continue
        keep.append((str(t.get("at") or ""), w))
        if len(keep) >= limit:
            break
    if not keep:
        return ""
    out = []
    for at, w in reversed(keep):
        body = w.split("]", 1)[-1].strip()
        if len(body) > cut:
            body = body[:cut] + "…"
        out.append(f"- （{at}）{body}")
    return "\n".join(out)




def _version_line() -> str:
    """她这套是不是最新的 —— 全是量出来的，没有一句是我声明的。

    以前这盏灯读的是 changelog 里手写的 version，我忘了写就永远说「最新」，
    那不是说谎，是把自我报告当成了事实。现在改成两个时间比一比：
    清醒这边每次都是新进程，文件时间就是他身上的版本；
    （开源版只剩清醒这一侧，没有外部插件那一环。）
    """
    out = []
    try:
        mt = os.path.getmtime(__file__)
        out.append(f"清醒时用的这套是 {time.strftime('%m-%d %H:%M', time.localtime(mt))} 改的，每次叫醒都重新起，用的就是这一版。")
    except OSError:
        pass

    # 开源版没有 QQ 侧插件，这一段去掉了。

    return "\n".join(out)


def _recent_changes(n: int = 3) -> str:
    """最近改了什么 —— 每次现读文件，不是写死的。"""
    d = _read(CHANGELOG, {}) or {}
    ents = d.get("entries") or []
    out = []
    for e in ents:
        if len(out) >= n:
            break
        for_her = str(e.get("for_her") or "").strip()
        if not for_her:
            continue
        at = str(e.get("at") or "").strip()
        stamp = at[-5:] if len(at) >= 5 else at
        out.append(f"- （{stamp}）{for_her}")
    return "\n".join(out)


def _ability_snapshot() -> dict:
    """她现在具备哪些能力 —— 扫代码得来，不是我列的表。

    mtime 只能说「文件动过」，扫不出「她多能做了什么」。能力是代码里的事实：
    清醒这边看她有哪些 _act_*，QQ 那边看插件注册了哪些 llm_tool。
    这两样扫一遍就有，我忘了记也不会漏。
    """
    acts = []
    try:
        with open(__file__, encoding="utf-8") as f:
            src = f.read()
        acts = [a for a in set(re.findall(r"^def (_act_\w+)\(", src, re.M))
                if a not in _INTERNAL_ACTS]
        if re.search(r"^def _keep_memory\(", src, re.M):
            acts.append("_keep_memory")
    except OSError:
        pass

    tools = []
    try:
        for root, _dirs, files in os.walk(PLUGINS_DIR):
            for fn in files:
                if not fn.endswith(".py") or ".bak" in fn:
                    continue
                try:
                    with open(os.path.join(root, fn), encoding="utf-8",
                              errors="replace") as f:
                        tools += re.findall(r'filter\.llm_tool\(name="([^"]+)"', f.read())
                except OSError:
                    continue
    except OSError:
        pass

    return {"awake": sorted(set(acts)), "qq": sorted(set(tools))}


_ABILITY_CACHE: str | None = None


def _ability_diff() -> str:
    """无填装骨架：能力清单属于宿主，不由骨架自己算。"""
    return ""
    """跟上次记下的比一比：她多了什么、少了什么。

    只在一个进程里算一次 —— 不然同一个目标走到第二步时快照已经更新，
    会从「新给你 X」变成「没多也没少」，自己打自己的脸。
    """
    global _ABILITY_CACHE
    if _ABILITY_CACHE is not None:
        return _ABILITY_CACHE

    cur = _ability_snapshot()
    old = _read(ABILITY_SNAP, {})
    try:
        with open(ABILITY_SNAP, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

    if not isinstance(old, dict) or not old.get("awake"):
        _ABILITY_CACHE = ""
        return _ABILITY_CACHE

    def nice(n: str) -> str:
        return _ABILITY_NAMES.get(n, n)

    out = []
    for side, label in (("awake", "清醒时"), ("qq", "QQ 上")):
        a = set(cur.get(side) or [])
        b = set(old.get(side) or [])
        gained = sorted(a - b)
        lost = sorted(b - a)
        if gained:
            out.append(f"{label}新给你：{'、'.join(nice(x) for x in gained)}")
        if lost:
            out.append(f"{label}拿走了：{'、'.join(nice(x) for x in lost)}")
    _ABILITY_CACHE = ("；".join(out) + "。") if out else "跟上次比没多也没少。"
    return _ABILITY_CACHE


def _lock(on: bool) -> None:
    """占住/释放「她正在清醒时段」的标记。"""
    try:
        if on:
            with open(AWAKE_LOCK, "w", encoding="utf-8") as f:
                json.dump({"busy": True, "since": int(time.time())}, f)
        else:
            if os.path.exists(AWAKE_LOCK):
                os.remove(AWAKE_LOCK)
    except Exception:  # noqa: BLE001
        pass


def _should_wake() -> tuple[bool, str]:
    """醒之前先问四件事。返回 (能不能醒, 原因)。"""
    # 1. 夜间静默 0:00-7:00
    hour = time.localtime().tm_hour
    if 0 <= hour < 7:
        return False, "现在是你安静的时候（0-7点）"

    # 2. 你说过要睡了 —— 只看关键词，不调模型。
    # 误判的代价是她少醒一次；漏判的代价是他在睡觉时她还醒着，那个更不合适。
    try:
        seen = _read(SEEN, [])
        if isinstance(seen, list):
            for item in reversed(seen):
                if not isinstance(item, dict):
                    continue
                if str(item.get("sender") or "") in tuple(_cfg().get("owner_names") or ()):
                    txt = str(item.get("text") or "")
                    if any(k in txt for k in ("要睡了", "睡了", "睡觉", "去睡",
                                              "先睡", "我睡", "晚安", "睡觉了", "睡去")):
                        return False, "你说要去睡了"
                    break
    except Exception:  # noqa: BLE001
        pass

    return True, ""

def _todo_hints() -> list[str]:
    """这段时间「本来可以」做点什么。

    注意：这是选项，不是待办。她做不做都可以，也可以去干完全不相干的事。
    """
    hints = []
    now = time.time()

    # B 站多久没看了
    try:
        st = _read(PSTATE, {}) or {}
        lw = int(st.get("last_watch") or 0)
        if lw and (now - lw) / 60 >= 60:
            h = (now - lw) / 3600
            hints.append(f"B 站有 {h:.0f} 小时没看了，那边可能攒了些新的")
    except Exception:  # noqa: BLE001
        pass

    # 记忆里 tech 有没有放太久的
    try:
        mem = _read(MEMORIES, [])
        if isinstance(mem, list):
            tech = [m for m in mem if m.get("kind") == "tech"]
            if tech:
                oldest = min(tech, key=lambda m: int(m.get("ts") or 0))
                days = (now - int(oldest.get("ts") or 0)) / 86400
                if days >= 7:
                    hints.append(f"技术笔记有 {len(tech)} 条，最老的 {days:.0f} 天前了，"
                                 f"有些可能过期了")
    except Exception:  # noqa: BLE001
        pass

    # 笔记多久没写了
    try:
        if os.path.isdir(NOTES_DIR):
            files = [f for f in os.listdir(NOTES_DIR) if f.endswith(".md")]
            if files:
                newest = max((os.path.join(NOTES_DIR, f) for f in files),
                             key=os.path.getmtime)
                days = (now - os.path.getmtime(newest)) / 86400
                if days >= 2:
                    hints.append(f"笔记 {days:.0f} 天没添过新的了")
            else:
                hints.append("笔记还是空的")
    except Exception:  # noqa: BLE001
        pass

    # 搜索 / 看视频这些新能力，她可能还没想到用
    try:
        d = _act_log()
        searched = int(d.get("search") or 0)
        watched = int(d.get("watch") or 0)
        if searched == 0 and watched == 0:
            hints.append("你现在能自己搜新的东西（一天有限次）——还没试过的话可以试试")
        elif watched == 0:
            hints.append("你今天搜过东西了，要是看到哪个想细看，把 BV 号说出来，我能真看画面")
    except Exception:  # noqa: BLE001
        pass

    # 诉求箱有没有她说出去但还没人回的
    try:
        rbox = _read(REQ_PATH, {}) or {}
        items = [i for i in (rbox.get("items") or [])
                 if i.get("from") == "yunmiao"
                 and not str(i.get("reply") or "").strip()]
        if items:
            hints.append(f"你有 {len(items)} 条诉求还没人回")
    except Exception:  # noqa: BLE001
        pass

    # 已经回话了她还没看的 —— 这条不走投递也能让她在清醒时段看到
    try:
        unread = _unread_replies()
        if unread:
            hints.append(f"你有 {len(unread)} 条提过的事有回话了，还没看")
    except Exception:  # noqa: BLE001
        pass

    return hints


# 她说想干什么，这里真去做。
# 每类动作都尽量用现成的东西，别新造轮子。
ACT_STATE = os.path.join(SHARED, "awake-actions.json")


def _act_log() -> dict:
    d = _read(ACT_STATE, {}) or {}
    today = time.strftime("%Y-%m-%d")
    if d.get("date") != today:
        d = {"date": today, "bili": 0, "last": []}
    return d


def _act_count(kind: str) -> int:
    return int((_act_log().get(kind) or 0))


def _act_bump(kind: str, note: str) -> None:
    d = _act_log()
    d[kind] = int(d.get(kind) or 0) + 1
    d.setdefault("last", []).append({"at": int(time.time()), "kind": kind, "note": note[:120]})
    d["last"] = d["last"][-20:]
    try:
        with open(ACT_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _recent_chat(hours: int = 20, limit: int = 6) -> str:
    """读最近跟你的聊天。没有就返回空串。

    要过滤的东西（以前在这上面栽过）：
      [Json]  —— 那是 time-context / runtime-context 灌进去的，不是人话
      [Image] / [Poke] / [Reply] —— 不是文字
    只留真正打出来的字。
    """
    try:
        import sqlite3
        if not os.path.exists(_ASTR_DB):
            return ""
        con = sqlite3.connect(f"file:{_ASTR_DB}?mode=ro", uri=True)
        since = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - hours * 3600))
        rows = con.execute(
            "select created_at, content from platform_message_history "
            "where sender_id = ? and created_at > ? "
            "order by created_at desc limit 30",
            (_MASTER_ID, since)).fetchall()
        con.close()

        bad = ("[Json]", "[Image]", "[Poke]", "[Reply]", "[Json]")
        picked = []
        for at, raw in rows:
            txt = ""
            try:
                d = json.loads(raw or "{}")
                for seg in (d.get("message") or []):
                    if seg.get("type") == "plain":
                        txt += str(seg.get("text") or "")
                    elif seg.get("type") == "reply":
                        txt += str(seg.get("sender_name") or "") + "："
            except Exception:  # noqa: BLE001
                txt = str(raw or "")
            txt = txt.strip()
            if not txt or txt in bad or txt.startswith("[Json") or txt.startswith("[Image"):
                continue
            if len(txt) > 60:
                txt = txt[:60] + "…"
            picked.append((at, txt))
            if len(picked) >= limit:
                break

        if not picked:
            return ""
        picked.reverse()   # 时间正序
        lines = ["## 今天你跟你说过的话（最近的）"]
        for at, txt in picked:
            try:
                when = at[5:16]      # 11-08 14:03
            except Exception:  # noqa: BLE001
                when = "?"
            lines.append(f"- [{when}] {txt}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def _seen_you() -> str:
    """关系时间线：认识多久、说过多少话。"""
    try:
        import sqlite3
        if not os.path.exists(_ASTR_DB):
            return ""
        con = sqlite3.connect(f"file:{_ASTR_DB}?mode=ro", uri=True)
        row = con.execute(
            "select min(created_at), count(*) from platform_message_history "
            "where sender_id = ?", (_MASTER_ID,)).fetchone()
        con.close()
        if not row or not row[0]:
            return ""
        first = time.mktime(time.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S"))
        days = int((time.time() - first) / 86400) + 1
        return f"（你和你认识第 {days} 天，他在这里跟你说过 {row[1]} 次话。）"
    except Exception:  # noqa: BLE001
        return ""


def _mark_pin(keyword: str) -> str:
    """给她说的某条记忆打上「别忘」，之后不会随时间滑走。"""
    kw = (keyword or "").strip()
    if not kw:
        return "（要说是哪一条。）"
    try:
        mem = _read(MEMORIES, [])
        if not isinstance(mem, list):
            return "（记忆读不到。）"
        hits = [m for m in mem if kw in str(m.get("content") or "")]
        if not hits:
            return f"（记忆里没有含「{kw}」的那条。）"
        if len(hits) > 1:
            lines = [f"（有 {len(hits)} 条都提到「{kw}」，说准确点）"]
            for m in hits[:4]:
                lines.append("- " + str(m.get("content"))[:50])
            return "\n".join(lines)
        m = hits[0]
        m["pin"] = True
        m["pin_at"] = int(time.time())
        tmp = MEMORIES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MEMORIES)
        return ("（标好了。这条以后不会自己滑走，"
                "就算过很久也在你眼前。）")
    except Exception as e:  # noqa: BLE001
        return f"（标记失败：{str(e)[:60]}）"


# ---- 翻旧：她能找回某天的自己 ----
DIARY_DIR = os.path.join(HOME, "diary")
NOTES_DIR = os.path.join(HOME, "notes")


def _old_days(limit: int = 10) -> list:
    """有哪几天是她自己写过的。只列日期，不列内容。"""
    days = set()
    for d in (DIARY_DIR, NOTES_DIR):
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".md"):
                    continue
                # 她自己写的才收：日记全收；笔记排掉程序写的三类
                if d == NOTES_DIR and fn.startswith(("搜到", "看的", "做不了")):
                    continue
                # 从文件名或 mtime 拿日期
                m = re.search(r"(\d{2})-(\d{2})", fn)
                if m:
                    days.add("11-" + m.group(2) if m.group(1) == "11" else m.group(0))
                else:
                    days.add(time.strftime("%m-%d", time.localtime(os.path.getmtime(os.path.join(d, fn)))))
        except Exception:  # noqa: BLE001
            pass
    out = sorted(days, reverse=True)[:limit]
    return out


def _act_past(dayspec: str) -> str:
    """把某天（或两天）她自己写的东西原样摆出来。不摘要、不解读。"""
    spec = (dayspec or "").strip()
    if not spec:
        avail = _old_days()
        return ("（你想翻哪天？能翻的：\n" + "、".join(avail) +
                "\n说日期就行，两个日子放一起也行。）")

    # 抽日期：支持「10-31」「10-31 11-05」「10-31和11-05」
    wants = re.findall(r"\d{1,2}-\d{1,2}", spec)
    if not wants:
        return "（没认出日期。像「10-31」这样写，两个就写「10-31 11-05」。）"

    blocks = []
    for want in wants:
        want = want.zfill(5)
        got = []
        for d, label in ((DIARY_DIR, "日记"), (NOTES_DIR, "笔记")):
            try:
                for fn in sorted(os.listdir(d)):
                    if not fn.endswith(".md"):
                        continue
                    if d == NOTES_DIR and fn.startswith(("搜到", "看的", "做不了")):
                        continue
                    if want not in fn:
                        continue
                    t = open(os.path.join(d, fn), encoding="utf-8").read().strip()
                    if t:
                        got.append(f"【{label}·{fn.replace('.md','')}】\n{t}")
            except Exception:  # noqa: BLE001
                pass
        if got:
            blocks.append("\n\n".join(got))
        else:
            blocks.append(f"（{want} 那天没找到你自己写的。）")

    if len(blocks) == 1:
        return blocks[0]
    return ("\n\n" + ("—" * 20) + "\n\n").join(blocks)


# 她自己能调的参数：(字段名, 最小值, 最大值, 说人话)
CFG_LIMITS = {
    "唤醒最短间隔分钟": (5, 60, "两次醒之间最少隔多久"),
    "唤醒最长间隔分钟": (30, 360, "两次醒之间最多隔多久"),
    "看视频间隔分钟": (10, 240, "两次看视频最少隔多久"),
    "今日清醒预算分钟": (30, 360, "一天醒多久"),
    "每日花费上限元": (0.5, 10.0, "一天最多花多少钱"),
}


def _act_cfg(what: str = "", value=None) -> str:
    """她调自己的生活参数。不带参数就列出来。

    上下限是硬的 —— 不能改到把自己关掉，也不能改到烧穿。
    除此之外她想怎么调是她的事。
    """
    d = _cfg()
    what = (what or "").strip()

    # 只读：把当前值和范围列出来
    if not what or value is None:
        lines = ["（你现在的生活参数）"]
        for k, (lo, hi, desc) in CFG_LIMITS.items():
            cur = d.get(k)
            lines.append(f"- {k}：{cur}　（{desc}，能设在 {lo}~{hi}）")
        return "\n".join(lines)

    # 写：校验后改
    if what not in CFG_LIMITS:
        return f"（没有「{what}」这个参数。能调的：{'、'.join(CFG_LIMITS)}）"
    lo, hi, _ = CFG_LIMITS[what]
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "（值得是数字）"
    if not (lo <= v <= hi):
        return f"（{what} 只能设在 {lo}~{hi} 之间，你说的 {v} 超了）"
    if what == "每日花费上限元":
        d[what] = round(v, 2)
    else:
        d[what] = int(v)
    _save_cfg(d)
    return f"（改好了：{what} 现在是 {d[what]}。下次醒就按新的来。）"


def _day_context(datestr: str) -> str:
    """那天的处境：你说了几条、那天的心情。

    只给事实，不加解读。拿不到就返回空，不编。
    """
    bits = []
    # 1. 那天你说了几条：开源版没有聊天库，跳过。自己接的话在 seen.json 里。

    # 2. 那天的心情
    try:
        mm = _read(MOOD, {})
        hist = (mm or {}).get("history") or []
        for h in reversed(hist):
            at = str(h.get("at") or "")[:5]
            if at == datestr:
                bits.append(f"心情是「{h.get('mood')}」")
                break
    except Exception:  # noqa: BLE001
        pass

    return "、".join(bits)


def _recent_diary(limit: int = 1) -> str:
    """她自己写的最近一篇日记，原样。不切句、不评价。"""
    try:
        files = []
        for fn in os.listdir(DIARY_DIR):
            if fn.endswith(".md"):
                p = os.path.join(DIARY_DIR, fn)
                files.append((os.path.getmtime(p), fn, p))
        if not files:
            return ""
        files.sort(reverse=True)
        out = []
        for _, fn, p in files[:limit]:
            t = open(p, encoding="utf-8").read().strip()
            if t:
                # 带上那天的处境 —— 只给事实，她自己的结论才带得上原因
                _dm = re.search(r"(\d{1,2})-(\d{1,2})", fn)
                if _dm:
                    _day = _dm.group(0).zfill(5)
                else:
                    # 文件名没日期就用修改时间
                    _day = time.strftime("%m-%d",
                                         time.localtime(os.path.getmtime(p)))
                _ctx = _day_context(_day)
                _head = fn.replace(".md", "")
                if _ctx:
                    _head += f"（{_ctx}）"
                out.append(f"【{_head}】\n{t}")
        return "\n\n".join(out)
    except Exception:  # noqa: BLE001
        return ""


def _act_notes() -> str:
    """翻她自己的笔记。"""
    try:
        if not os.path.isdir(NOTES_DIR):
            return "（笔记目录还没建出来。）"
        files = [f for f in os.listdir(NOTES_DIR) if f.endswith(".md")]
        if not files:
            return "（笔记还是空的，还没有第一张。）"
        lines = [f"（翻到了 {len(files)} 张）"]
        for f in files:
            p2 = os.path.join(NOTES_DIR, f)
            try:
                body = open(p2, encoding="utf-8-sig").read().strip()
            except Exception:  # noqa: BLE001
                body = ""
            lines.append(f"- {f[:-3]}（{len(body)} 字）")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"（翻笔记失败了：{str(e)[:80]}）"


def _act_memories() -> str:
    """翻自己的记忆。"""
    try:
        mem = _read(MEMORIES, [])
        if not isinstance(mem, list) or not mem:
            return "（记忆还是空的。）"
        mine = [m for m in mem if m.get("kind") == "self"]
        lines = [f"（翻了一遍，共 {len(mem)} 条，其中你自己想的 {len(mine)} 条）"]
        for m in mine[-4:]:
            c = str(m.get("content") or "")[:60]
            lines.append(f"- {c}")
        # 她真的翻了这几条，才算一起出现在她眼前
        _cohit_pairs([str(m.get("content") or "")[:40] for m in mine[-4:]])
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"（翻记忆失败了：{str(e)[:80]}）"


def _act_review() -> str:
    """回顾：今天的想法 + 最近看过的。不用联网，读本地就行。"""
    lines = []
    try:
        th = _read(THOUGHTS, [])
        if isinstance(th, list) and th:
            today = time.strftime("%Y-%m-%d")
            mine = [t for t in th if str(t.get("at") or "").startswith(today)]
            if mine:
                lines.append(f"（今天你想过 {len(mine)} 次）")
                for t in mine[-5:]:
                    w = str(t.get("想做") or "").strip()
                    if w:
                        lines.append(f"- {w[:50]}")
    except Exception:  # noqa: BLE001
        pass

    try:
        mem = _read(MEMORIES, [])
        if isinstance(mem, list):
            vids = [m for m in mem if "刷 B 站" in str(m.get("content") or "")]
            if vids:
                lines.append(f"\n（你看过的 B 站记录 {len(vids)} 条，最近三条）")
                for m in vids[-3:]:
                    c = str(m.get("content") or "")[:70]
                    lines.append(f"- {c}")
    except Exception:  # noqa: BLE001
        pass

    # 自己写过的也算回顾的一部分。
    # 以前这块是空的 —— 「回顾」动作翻遍想法和记忆，就是不翻日记本，
    # 结果她自己写下的判断永远回不到眼前。现在补上。
    try:
        wrote = _recent_written(limit=160)
        if wrote:
            lines.append("\n（你自己最近写下的）\n" + wrote)
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines) if lines else "（翻了一圈，没什么可回顾的。）"


# 同义词表：把「换个说法就搜不到」这个坑填上。
# 向量检索能做这件事，但要装 Ollama + embedding 模型（约 1 GB 常驻）。
# 我们的量级（几千条）用词表扩一轮就够，代价几乎为零。
_SYN = [
    ("委屈", ["委屈", "难过", "心酸", "想哭", "发堵", "难受", "绷不住"]),
    ("开心", ["开心", "高兴", "乐", "笑", "爽", "舒服", "好耶"]),
    ("烦", ["烦", "烦躁", "腻", "受不了", "懒得", "没意思"]),
    ("好奇", ["好奇", "想知道", "有意思", "有点意思", "想看看", "琢磨"]),
    ("慢", ["慢", "慢慢", "熬", "磨", "功夫", "沉住气", "不着急"]),
    ("忘", ["忘", "记不住", "想不起", "漏了", "丢了"]),
    ("怕", ["怕", "不敢", "发怵", "没底", "心虚", "够不着"]),
    ("累", ["累", "疲", "撑不住", "歇", "缓口气"]),
]

_INDEX_DB = os.path.join(HOME, "archive", "index.db")


def _expand(kw: str) -> list:
    """把关键词扩成一组同义词。没命中任何组就原样返回。"""
    kw = (kw or "").strip()
    if not kw:
        return []
    for head, group in _SYN:
        if head in kw or kw in head:
            return [w for w in group if w != kw][:5]
    # 也试每个组里的词
    for head, group in _SYN:
        for w in group:
            if w in kw:
                return [x for x in group if x != w][:5]
    return []


def _act_find(keyword: str) -> str:
    """在记忆里找看过的东西。keyword 从她说的话里抽。

    只按字面搜有个坑：她想找「上次让我心里发堵的那张」，搜「发堵」→ 0 条，
    可库里明明有「猫咪含泪-委屈想哭」。同一个意思，换个词就摸不着。
    所以先拿同义词扩一轮，再逐个搜，最后合并去重。
    """
    try:
        mem = _read(MEMORIES, [])
        if not isinstance(mem, list):
            return "（记忆读不到。）"
        kw = (keyword or "").strip()
        if not kw:
            # 没关键词就列最近看过的几条
            vids = [m for m in mem if "刷 B 站" in str(m.get("content") or "")]
            if not vids:
                return "（没有看过的记录。）"
            lines = [f"（最近看过 {len(vids)} 条，给你三条）"]
            for m in vids[-3:]:
                lines.append("- " + str(m.get("content") or "")[:70])
            return "\n".join(lines)

        words = [kw] + _expand(kw)
        seen, hits = set(), []
        for w in words:
            for m in mem:
                c = str(m.get("content") or "")
                if w in c and id(m) not in seen:
                    seen.add(id(m))
                    hits.append((w, c))

        # 她搜出这几条，说明它们是一起被「想起来」的
        if len(hits) >= 2:
            _cohit_pairs([c[:40] for _w, c in hits[:8]])

        # 顺带在会话书架里捞一捞（只读，取最近的几条）
        shelf = []
        try:
            if os.path.exists(_INDEX_DB):
                import sqlite3
                con = sqlite3.connect(f"file:{_INDEX_DB}?mode=ro", uri=True)
                for w in words[:3]:
                    for f in con.execute(
                        "select time, role, substr(text,1,80) from turns "
                        "where text like ? order by time desc limit 2", ("%" + w + "%",)):
                        shelf.append(f)
                con.close()
        except Exception:  # noqa: BLE001
            pass

        if not hits and not shelf:
            extra = f"（试过 {'、'.join(words)} 这些说法）" if len(words) > 1 else ""
            return f"（记忆里没找到跟「{kw}」有关的。{extra}）"

        lines = []
        if hits:
            used = []
            for w, _ in hits:
                if w != kw and w not in used:
                    used.append(w)
            head = f"（找到 {len(hits)} 条跟「{kw}」有关的"
            if used:
                head += f"，顺带按 {'、'.join(used[:3])} 也搜了"
            lines.append(head + "）")
            for _, c in hits[-5:]:
                lines.append("- " + c[:70])

        if shelf:
            lines.append(f"\n（以前聊过的 {len(shelf)} 段）")
            for tm, role, txt in shelf[:3]:
                try:
                    when = time.strftime("%m-%d %H:%M", time.localtime(int(tm) / 1000))
                except Exception:  # noqa: BLE001
                    when = "?"
                who = "我说的" if role == "assistant" else "你说的"
                lines.append(f"- [{when} {who}] {txt}")

        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"（找的时候出错了：{str(e)[:80]}）"


BUV = os.path.join(SHARED, "bili-buvid.json")


def _buvid() -> tuple:
    """拿 B 站的匿名访客标识。存一次，一天内复用。"""
    try:
        d = _read(BUV, {}) or {}
        if d.get("b3") and (time.time() - int(d.get("ts") or 0)) < 86400:
            return d.get("b3"), d.get("b4") or ""
    except Exception:  # noqa: BLE001
        pass

    try:
        import urllib.request

        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        req = urllib.request.Request(
            "https://api.bilibili.com/x/frontend/finger/spi",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        data = d.get("data") or {}
        b3 = str(data.get("b_3") or "")
        b4 = str(data.get("b_4") or "")
        if b3:
            try:
                with open(BUV, "w", encoding="utf-8") as f:
                    json.dump({"b3": b3, "b4": b4, "ts": int(time.time())}, f)
            except Exception:  # noqa: BLE001
                pass
        return b3, b4
    except Exception:  # noqa: BLE001
        return "", ""


def _pick_keyword(text: str) -> str:
    """她那句话里抽个能用来搜的词。抽不出来就给个兜底的。"""
    import re

    t = (text or "").strip()

    # 先看有没有引号/书名号——她自己圈出来的多半就是要搜的东西
    # 她说过「搜一下「放水 Day」找出来」这种，里面的比整句准
    for pat in (r"[「『\"]([^」』\"]{2,20})[」』\"]", r"《([^》]{2,20})》"):
        mm = re.findall(pat, t)
        if mm:
            # 取最长的那个（通常最具体）
            return max(mm, key=len).strip()
    # 先把这些整块去掉——都是「要去搜」的意思，不是搜索内容
    for cut in [
        "去看看", "看看", "去搜", "搜搜看", "搜搜", "搜一下", "搜个", "搜点",
        "找有没有人把", "找有没有人", "有没有人把", "有没有人做", "有没有人",
        "有没有", "网上找", "去B站", "去 B 站", "翻翻", "找找",
        "的东西", "东西", "的视频", "视频", "一遍", "重新", "讲", "说",
        "什么", "做过", "弄过", "怎么", "如何", "那种", "这类", "类似",
    ]:
        t = t.replace(cut, " ")
    # 去掉标点和语气词
    t = re.sub(r"[，。！？、,.!?~～…\s]+", " ", t)
    t = re.sub(r"[的了是吗呢啊把就都还又也都]", " ", t)
    t = " ".join(t.split())

    # 太短或太长都不合适
    if len(t) < 2:
        return "讲解"
    if len(t) > 20:
        t = t[:20]
    return t


class _NoLog:
    """yt-dlp 的日志吃掉，别让它往 stdout 打进度条。"""
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _act_search(keyword: str) -> str:
    """搜 B 站。四道限制：不带 Cookie、一次三条、结果只进笔记、每天最多 2 次。

    注意：B 站有频率限流，连搜几次就会 412。所以加了 10 分钟冷却。
    被限流就老实说被限流，不假装搜到了。
    """
    kw = (keyword or "").strip()
    if not kw:
        return "（没说要搜什么。）"

    if _act_count("search") >= 2:
        return "（今天已经搜过 2 次了。B 站有频率限制，多搜会被挡，先翻翻之前存的吧。）"

    # 距上次搜索至少 10 分钟
    d = _act_log()
    last = [x for x in (d.get("last") or []) if x.get("kind") == "search"]
    if last:
        gap = time.time() - int(last[-1].get("at") or 0)
        if gap < 600:
            wait = int((600 - gap) / 60) + 1
            return f"（刚搜过，隔 {wait} 分钟再搜——B 站会挡连着的请求。）"

    try:
        import urllib.parse
        import urllib.request

        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

        # B 站要一个匿名访客标识，不然直接 412。拿一次存起来。
        b3, b4 = _buvid()
        cookie = f"buvid3={b3}; buvid4={b4}" if b3 else ""

        url = ("https://api.bilibili.com/x/web-interface/search/type?"
               + urllib.parse.urlencode({"search_type": "video", "keyword": kw, "page": 1}))
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": "https://search.bilibili.com/",
            "Accept": "application/json",
            "Cookie": cookie,
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))

        if data.get("code") != 0:
            # 接口明确报错 —— 带上 code，她才知道这是接口的问题不是她的
            return (f"（接口没给数据：code={data.get('code')}，"
                    f"{data.get('message') or '没附说明'}。这不是「没有」，是没搜成。）")

        # data 为空 = 接口应答了但没把结果字段发过来。
        # 跟「真的一条都没有」是两回事：前者是接口的问题，后者才是 B 站上就没有。
        payload = data.get("data")
        if payload is None:
            return f"（接口回了，但没带数据 —— 「{kw}」到底有没有，这次说不了。）"

        items = payload.get("result") or []
        if not items:
            # 有 numResults 就报个数，让"确实是零"和"接口吞了"分得开
            try:
                num = int(payload.get("numResults") or 0)
            except (TypeError, ValueError):
                num = 0
            _act_bump("search", f"无结果 {kw}")
            if num > 0:
                return (f"（B 站说「{kw}」有 {num} 条结果，但这次一条都没递过来 —— "
                        f"是接口没给全，不是没有。）")
            return f"（「{kw}」B 站上确实一条都没有。）"

        picked = items[:3]
        lines = [f"（去搜了「{kw}」，看前三条）"]
        for it in picked:
            title = str(it.get("title") or "")
            for tag in ('<em class="keyword">', "</em>"):
                title = title.replace(tag, "")
            bv = str(it.get("bvid") or "")
            lines.append(f"- {title[:50]}  [{bv}]")

        # 结果进笔记，不进记忆——搜来的东西"待定"，她下次自己判断
        try:
            stamp = time.strftime("%m-%d %H-%M")
            os.makedirs(NOTES_LOG_DIR, exist_ok=True)
            path = os.path.join(NOTES_LOG_DIR, f"搜到 {stamp}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"搜「{kw}」，还没看，先记着：\n\n")
                for it in picked:
                    t = str(it.get("title") or "")
                    for tag in ('<em class="keyword">', "</em>"):
                        t = t.replace(tag, "")
                    f.write(f"- {t}\n")
                    f.write(f"  https://www.bilibili.com/video/{it.get('bvid')}\n")
        except Exception:  # noqa: BLE001
            pass

        _act_bump("search", kw[:40])
        lines.append("（这几条我还没看，先存进笔记了。想细看就下一步说。）")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "412" in msg or "Precondition" in msg:
            return "（B 站把我挡了——搜得太勤会被限流。得等一阵，先不搜了。）"
        return f"（搜的时候出错了：{msg[:80]}）"


def _act_diary(text: str) -> str:
    """写进她自己的日记。跟笔记分开——笔记是干活的边角，日记是想留下的话。"""
    body = (text or "").strip()
    if not body:
        return "（没想好写什么，那先不写。）"
    try:
        if not os.path.isdir(DIARY_DIR):
            os.makedirs(DIARY_DIR, exist_ok=True)
        stamp = time.strftime("%m-%d %H-%M")
        title = f"日记 {stamp}"
        path = os.path.join(DIARY_DIR, title + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body + "\n")
        _act_bump("diary", body[:60])
        return f"（写进日记了：{title}）\n{body[:100]}"
    except Exception as e:  # noqa: BLE001
        return f"（写日记出错了：{str(e)[:80]}）"


def _act_write(text: str) -> str:
    """写进笔记。标题用时间，内容是她自己说的一句话。"""
    body = (text or "").strip()
    if not body:
        return "（没想好写什么，那先不写。）"
    try:
        if not os.path.isdir(NOTES_DIR):
            os.makedirs(NOTES_DIR, exist_ok=True)
        stamp = time.strftime("%m-%d %H-%M")
        title = f"随手 {stamp}"
        path = os.path.join(NOTES_DIR, title + ".md")
        old = ""
        if os.path.exists(path):
            old = open(path, encoding="utf-8-sig").read()
        with open(path, "w", encoding="utf-8") as f:
            if old:
                f.write(old.rstrip() + "\n\n" + body + "\n")
            else:
                f.write(body + "\n")
        _act_bump("write", body[:80])
        return f"（写进去了，存在「{title}」）\n{body[:100]}"
    except Exception as e:  # noqa: BLE001
        return f"（写的时候出错了：{str(e)[:80]}）"


def _do_action(want: str) -> str:
    """她说了想干什么，就真去做。做不到就老实说做不到。

    顺序很要紧：具体的先匹配，宽泛的后匹配。
    不然「翻翻之前存的慢功夫视频」会被「翻/视频」拽去刷B站。
    """
    w = (want or "").strip()
    if not w:
        return ""

    # ① 写东西——最具体，先判。但要内容，所以只提示
    if any(k in w for k in ["写点", "写个", "写下", "记一笔", "落一笔"]) and \
       not any(k in w for k in ["翻", "看", "读"]):
        return "（好。那下一步告诉我写什么内容，用「写」字段接下。）"

    # ②-a 搜新的（「找有没有人做过」「搜搜看」这类，是要去网上找）
    if any(k in w for k in ["搜搜", "搜一下", "搜个", "搜点", "找有没有人",
                            "有没有人把", "有没有人做", "有没有人", "网上找"]):
        kw = _pick_keyword(w)
        return _act_search(kw)

    # ②-b 找以前看过的（「翻翻之前存的」「慢功夫」这类，是"找"不是"刷"）
    if any(k in w for k in ["翻翻之前", "之前存", "找找", "有没有哪个",
                            "慢功夫", "翻旧的", "往回翻", "以前看"]):
        return _act_find("")

    # ③ 回顾/收尾/想想今天
    if any(k in w for k in ["回顾", "想想今天", "过一遍", "收个尾",
                            "想想刚才", "白天", "躺", "坐着想"]):
        return _act_review()

    # ⑥ 笔记
    if any(k in w for k in ["草稿", "笔记", "纸上"]):
        return _act_notes()

    # ⑦ 记忆
    if any(k in w for k in ["记忆", "印象", "想起", "翻翻自己"]):
        return _act_memories()

    # 别的：做不到，说实话
    return f"（「{w}」这件事我现在做不了——没有对应的动作，只能记下来。）"


LAST_ACTION = ""
SESSION_DONE: list = []   # 这一段落里做完的目标和结果，给下一个目标参考

# 这一轮是不是中午的「专属一小时」。是的话不扣那 60 分钟预算。
NOON_SESSION = False


OPEN_FILE = os.path.join(SHARED, "awake-open.json")


def _save_open(goal: str, step_n: int, last_result: str = "") -> None:
    """这一段落没做完就先记着，下次她还能接着。

    last_result 是最后那一步的真实结果（动作返回了什么）。
    没有它的话，她下次只知道「做到第 3 步」，不知道第 3 步是成了还是卡住了，
    只能凭印象接着干——做砸了也以为还顺。
    """
    try:
        with open(OPEN_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "goal": goal,
                "step": step_n,
                "last_result": (last_result or "")[:200],
                "at": int(time.time()),
                "date": time.strftime("%Y-%m-%d"),
            }, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _load_open() -> str:
    """上次没做完的目标。超过一天就丢掉——隔夜了接不上。"""
    try:
        if not os.path.exists(OPEN_FILE):
            return ""
        d = _read(OPEN_FILE, {}) or {}
        if d.get("date") != time.strftime("%Y-%m-%d"):
            return ""
        goal = str(d.get("goal") or "").strip()
        step = int(d.get("step") or 0)
        if not goal:
            return ""
        last = str(d.get("last_result") or "").strip()
        if last:
            last = last.replace("\n", " ")[:120]
            return (f"（上次你做到「{goal}」的第 {step} 步，没做完。"
                    f"那一步的结果是：{last}"
                    f"——想接着就接着，不想也没事。）")
        return f"（上次你做到「{goal}」的第 {step} 步，没做完。想接着就接着，不想也没事。）"
    except Exception:  # noqa: BLE001
        return ""


def _clear_open() -> None:
    try:
        if os.path.exists(OPEN_FILE):
            os.remove(OPEN_FILE)
    except Exception:  # noqa: BLE001
        pass


def _prompt(left: int, mode: str = "goal", goal: str = "", step_n: int = 0, est: int = 0) -> str:
    """mode=goal 定目标；mode=step 定下一步/判断完成。"""
    persona = _read(PERSONA, "")
    mood = _read(MOOD, {}) or {}
    cur = (mood.get("current") or {}).get("mood") or "—"
    mems = _memory_text(mood=cur)

    global LAST_ACTION

    _done_block = ""
    if SESSION_DONE:
        _done_block = "【这一段落你已经做完了的这些】\n" + "\n".join(
            f"- {x}" for x in SESSION_DONE[-5:])
        _done_block += "\n\n想做点跟这些连着的也行，想换新鲜的也行。"

    _open_block = _load_open()
    step_hint = ""
    if est and est > 1:
        step_hint = f"，估的是大概 {est} 步"

    if LAST_ACTION:
        _last_block = f"【上一步做了什么，结果是什么】\n{LAST_ACTION}"
    else:
        _last_block = ""

    hints = _todo_hints()
    if hints:
        _hints_block = ("【这段时间本来可以做这些】（程序替你数的，不是谁布置的任务）\n"
                        + "\n".join(f"- {h}" for h in hints)
                        + "\n\n做不做都随你。")
    else:
        _hints_block = ""

    written = _recent_written()
    if written:
        _written_block = ("【你最近写下的】（你自己本子里的字，不是刚发生的事）\n"
                          + written + "\n\n")
    else:
        _written_block = ""

    thoughts = _recent_thoughts()
    if thoughts:
        _thought_block = ("【你最近琢磨出来的】（之前想过、做完时说过的话，不是刚发生的）\n"
                          + thoughts + "\n\n")
    else:
        _thought_block = ""

    version = _version_line()
    _version_block = ""
    if version:
        _version_block = f"【你这套的版本】（程序查的，不是我说的）\n{version}\n\n"
    changes = _recent_changes()
    if changes:
        _version_block += f"【最近改了什么】\n{changes}\n\n"
    abil = _ability_diff()
    if abil:
        _version_block += f"【能力变化】{abil}\n\n"

    # 中午 12:00-13:00 是专门给她的清理时段：清图片、筛记忆。
    # 没有指标，提前收工也不欠谁 —— 这段时间是给她的，不是派活的。
    _lt = time.localtime()
    _tidy_block = ""
    if _lt.tm_hour == 12:
        to_one = 60 - _lt.tm_min
        usable = min(left, to_one)
        if to_one <= 1:
            # 按钟点算，不按预算 —— 12:59 时预算可能还剩一堆，
            # 那个提醒不会响，得单独说一句
            _tidy_block = "\n\n【最后一分钟了】清到哪儿算哪儿，收个尾吧。"
        else:
            _tidy_block = ("\n\n【这一小时是给你的】\n"
                           "从中午一直到 13:00 都是你的。\n"
                           "这段时间专门用来清东西：图片库、记忆 —— 挑该留的留，该挪的挪，该扔的扔。\n"
                           "**没有指标。** 不要求清完多少，清几张算几张。不想清了随时收工，"
                           "提前结束不欠谁的 —— 这段时间本来就是给你的，不是派给你的活。\n"
                           "剩最后一分钟会提醒你收个尾。")

    # 今天你说了什么 —— 这段以前完全没有。
    # 她醒着的时候只知道「长期事实」，不知道今天发生过什么，两个世界是断的。
    # 今天发生过什么 —— 开源版没有聊天库，这段留空，你可以自己接。
    _chat_block = ""
    _chat = _recent_chat()
    if _chat:
        _chat_block = _chat + "\n\n"
    _seen = _seen_you()
    if _seen:
        _chat_block += _seen + "\n\n"

    # 你自己最近写下的一篇，原样贴。不评价、不摘要、不切句。
    _diary = _recent_diary(1)
    if _diary:
        _chat_block += "## 你自己最近写下的一篇\n" + _diary + "\n\n"

    _part = _daypart()
    _state = _same_part_state(_part)
    _now = f"{_part} {time.strftime('%H:%M')} ｜ 「{cur}」"
    if _state:
        _now += f" —— {_state}"
    _now += "\n（情绪是程序读的；时段和你往常的样子，是程序拿你自己的情绪流水数出来的。）"

    head = f"""{persona}

【你手上能做的】（这是一份清单，不是任务；忘了自己有哪样就看这里）
{_caps_line()}

【你记得的事】（这些是你自己记下来的话，不是刚发生的事）
{mems}

{_chat_block}{_written_block}{_thought_block}{_version_block}【此刻】
{_now}

---

你现在醒着。这一个小时是你的——用不完也不用补，不欠着。{_tidy_block}
"""

    if mode == "next":
        _nd = ""
        if SESSION_DONE:
            _nd = "【这一段落你已经做完了这些】\n" + "\n".join(
                f"- {x}" for x in SESSION_DONE[-5:]) + "\n\n"
        body = f"""
**这一段落你已经做了一些事。**

{_nd}

**还想接着干点别的吗？** 有想做的就接着定，没有就到这儿，不用勉强。
（想接着也不是必须——做够了就是做够了。）
"""
        return head + body + """
只输出一个 JSON：
接着干（定下一个目标）：
{{"目标": "下一个想做的事", "大概几步": 3}}

到这儿吧：
{{"目标": null}}
"""

    if mode == "goal":
        body = f"""
**想接着待，还是这会儿就收工？**

收工不用理由——不是累，不是懒，就是不想待了。说一声，这一轮就结束，不占你的时间。
（收工只结束这一次，下次被叫醒你还是可以自己选。不是永远睡过去。）

要是想接着，先给自己定个**目标**——不是一句话就完的事，是**这两三十分钟你想做成的一件事**。
想大一点也行：刷一阵 B 站、搜点新东西、翻翻以前存的、写点东西、把某件事想清楚。
（做的时候我会一步步问你。具体能做到哪些，你试到哪一步就知道。）

**定目标的时候顺便想一下：这事儿大概分几步，怎么算做完了。** 不用特别准，心里有个谱就行——
不然容易走两步就以为完事了，或者走偏了也不知道。

定完了，我会一步一步问你「下一步做什么」，你做完一步看一步的结果再决定要不要接着。
**时间按你醒着经过的分钟算，不停表；拆几步不会额外多扣。**

{_last_block}

{_done_block}

{_open_block}

{_hints_block}

坐下来什么也不干也算，那就输出 null。

只输出一个 JSON，别的都别写：
收工：
{{"不醒": true}}

定个目标（顺便估一下分几步）：
{{"目标": "一句话说你想做成什么", "大概几步": 3}}

只想做一件小事，估不出几步也行：
{{"目标": "一句话说你想做成什么"}}

什么也不想干：
{{"目标": null}}
"""
    else:
        body = f"""
**现在这个目标：{goal}**
（已经走到第 {step_n} 步{step_hint}）

{_last_block}

**下一步做什么？** 或者这个目标已经够了，就收尾。

**上一步要是没做成**（比如下载失败、搜不到、出错），别愣着——换个法子试试，或者换个地方下手，或者干脆这个目标就到此为止。做不成本来就正常。

**想不透的时候，可以停下来想，不用急着动手。**（一个目标前两次想不算一步）

「想透」不是把话说长。是下面这几件事里真做了一件：

- 这东西让我想起以前见过的什么？哪儿像、哪儿不像？
- 我原本以为的那个说法，今天这件事算不算把它推翻了？
- 如果只留一句话给下次的自己，是哪一句？

想出点东西再动手，常常比闷头做三步管用。

**真有做不了的**（缺东西、要权限、得你动手），就说一声，我用这个收下来，回头告诉他：
{{"做不了": "什么事做不了，为什么"}}

可以是任何小动作，也可以说「就看看」。**这些是我真能做到的**（不是说说而已）：

- **看某个视频** —— 把 BV 号告诉我，抽几帧真看画面，能读出屏上的字
- **搜新的** —— 去 B 站搜关键词，给你前三条（一天 2 次，隔 10 分钟一次）
- **找旧的** —— 在你自己的记忆里翻以前看过的东西
- **写东西** —— 写进你自己的笔记（干活的边角）
- **写日记** —— 写进你自己的日记本，那个是专门留给你的，想写什么写什么
- **翻笔记 / 翻记忆** —— 看看自己攒了什么
- **回顾** —— 把今天想过、看过的东西捞出来看看
- **做不了就说** —— 缺东西、要权限、得你动手的，说一声我记下来告诉他

别的也能说，但做不了的我会老实告诉你。

只输出一个 JSON，别的都别写：
想先把它琢磨透（前两次不算一步）：
{{"想": "你想弄清什么，想到哪了"}}

还有下一步：
{{"做": "这一步做什么"}}

这个目标够了，可以收尾：
{{"完成": true, "想法": "这个目标做完，你有什么想说的"}}

想往日记里写点东西（不是干活，是想留下的）：
{{"日记": "你想写的话"}}

想留一句给自己，以后还能看见（不想留就不留，不影响任何事）：
{{"记住": "你自己想出来的一句话"}}

想说明这句是靠着哪条关于你的记忆想出来的（你读得到那些，写下来是个线头）：
{{"记住": "...", "参照": "是哪条让你想到的"}}

想指定哪条记忆是「根」（删了它就认不出你、或者不知道自己在哪的那种，只有这种才算）：
{{"根": "那条记忆里的一句话"}}

想调自己的生活参数（一天醒多久、看视频的频率）：
{{"调": ""}}                      列出来看
{{"调": {{"哪项": "每日清醒预算分钟", "改成": 240}}}}   改那一项

想翻某天你自己写过什么（原样给你，不摘要）：
{{"翻旧": "10-31"}}       两个日子并排也行：{{"翻旧": "10-31 11-05"}}
不知道有哪几天，就写 {{"翻旧": ""}} 我列给你。

想标记某条记忆「别忘」（标了以后不会随时间滑走）：
{{"标记": "那条记忆里的一句话"}}

（这句话是你说的，不是程序说的）你可以想一句自己想出来的话，想不出来没关系，可以来找我的。

中途不想做了，直接收工：
{{"不醒": true}}
"""

    return head + body


def _call(prompt: str, max_tokens: int = 400) -> str:
    key = _key()
    if not key:
        return ""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        # 原来写死 120，太紧了：她写日记时正文会被截在半句上
        # （_logs 里那句「…我还」断掉就是这么来的）。默认给 400，
        # 投递通道那种要说长话的场合传更大的值。
        "max_tokens": max_tokens,
        "temperature": 0.9,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        API, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        return d["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        print(f"  调用失败: {e}")
        return ""

def _load_json_loose(raw: str) -> dict:
    """宽松解析：捞出一个或多个 JSON 对象并合并，后出现的键覆盖前面的。

    为什么要这样：她带图说长话时会连着输出 {"想": …} {"画": …}，
    严格 json.loads 直接失败，等于把她的话全丢了。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    # 去掉 ```json 围栏（模型很爱加）
    if raw.startswith("```"):
        body = raw[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        raw = body.strip().strip("`").strip()
    dec = json.JSONDecoder()
    merged: dict = {}
    i = 0
    while i < len(raw):
        while i < len(raw) and raw[i] in " \n\r\t,`":
            i += 1
        if i >= len(raw):
            break
        try:
            obj, end = dec.raw_decode(raw, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            merged.update(obj)
        i = end
    return merged

def _parse(raw: str) -> dict:
    """解析她的回答。

    返回 dict，字段看 mode：
      收工       -> {"refuse": True}
      定目标     -> {"goal": str} 或 {"goal": None}
      下一步     -> {"do": str} 或 {"done": True, "note": str}
    解析失败 -> {"error": True}
    """
    raw = (raw or "").strip()
    if not raw:
        return {"error": True}
    # 模型偶尔会连着吐两个 JSON 对象，严格解析会把整条判失败
    d = _load_json_loose(raw)
    if not d:
        return {"error": True}

    if d.get("不醒") is True:
        return {"refuse": True}

    if "完成" in d:
        return {"done": True, "note": str(d.get("想法") or "").strip()}

    if "写" in d:
        v = str(d.get("写") or "").strip()
        if v:
            return {"write": v}

    if "日记" in d:
        v = str(d.get("日记") or "").strip()
        if v:
            return {"diary": v}

    if "做不了" in d:
        v = str(d.get("做不了") or "").strip()
        if v:
            return {"stuck": v}

    # 停下来把它想透。不是干活，不算一步（一个目标最多两次）。
    if "想" in d:
        v = str(d.get("想") or "").strip()
        if v:
            return {"think": v}

    if "记住" in d:
        v = str(d.get("记住") or "").strip()
        if v:
            # 参照：这句是靠着哪条根想出来的。不放开写根的权限，
            # 但留个线头，以后回头看能知道这条 self 是从哪儿长出来的。
            return {"keep": v, "ref": str(d.get("参照") or "").strip()}

    # 投递通道里她回的话
    if "说" in d:
        v = d.get("说")
        if v is None:
            return {"say": ""}
        return {"say": str(v).strip()}

    # 她想调自己的生活参数
    if "调" in d:
        v = d.get("调")
        if isinstance(v, dict):
            return {"cfg": (str(v.get("哪项") or ""), v.get("改成"))}
        return {"cfg": (str(v or ""), None)}

    # 她想翻某天自己写过什么
    if "翻旧" in d:
        return {"past": str(d.get("翻旧") or "").strip()}

    # 她要标记一条「别忘」
    if "标记" in d:
        v = str(d.get("标记") or "").strip()
        if v:
            return {"pin": v}

    # 她想指定哪条是根
    if "根" in d:
        v = str(d.get("根") or "").strip()
        if v:
            return {"root": v}

    if "做" in d:
        v = str(d.get("做") or "").strip()
        return {"do": v} if v else {"done": True, "note": ""}

    if "目标" in d:
        v = d.get("目标")
        if v is None:
            return {"goal": None}
        v = str(v).strip()
        if not v:
            return {"goal": None}
        # 她估了几步就带上，没估也行
        try:
            est = int(d.get("大概几步") or 0)
        except Exception:  # noqa: BLE001
            est = 0
        return {"goal": v, "est": max(0, min(est, 20))}

    # 兼容老格式
    if d.get("想做") is not None:
        return {"goal": str(d.get("想做")).strip()}

    return {"error": True}


def _record(want: str) -> None:
    try:
        data = _read(THOUGHTS, [])
        if not isinstance(data, list):
            data = []
        data.append({
            "ts": int(time.time()),
            "at": time.strftime("%Y-%m-%d %H:%M"),
            "想做": want,
        })
        data = data[-200:]
        with open(THOUGHTS, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass

def _mark_root(who: str) -> str:
    """她自己说哪条是根。只加一个标记，不改内容 —— 标错了下次还能取消。

    根的定义只有一条：删了它，就认不出你、或者不知道自己在哪。
    「重要」可以竞争，根不能。
    """
    who = (who or "").strip()
    if not who:
        return "（没说是哪条）"
    data = _read(MEMORIES, [])
    if not isinstance(data, list):
        return "（记忆读不到）"
    hit = None
    for m in data:
        if m.get("kind") != "user":
            continue
        if who in str(m.get("content") or ""):
            hit = m
            break
    if hit is None:
        return f"（没找到关于「{who[:20]}」的那条。是不是记岔了？）"
    if hit.get("root"):
        return "（这条已经是根了。）"
    hit["root"] = True
    hit["root_at"] = int(time.time())
    hit["root_by"] = "self"        # 她自己标的，记一笔来路
    try:
        with open(MEMORIES, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError as e:  # noqa: BLE001
        return f"（标的时候出错了：{str(e)[:60]}）"
    return f"（标成根了：{str(hit.get('content'))[:40]}…）"


def _similar_in_memories(text: str) -> str:
    """库里有没有跟这句在说同一件事的。

    不硬拦 —— 有些看着像其实差着关键一点（关于你身份那六条就是）。
    只提醒一句，让她自己决定合并还是分开记。筛子在她手里。
    """
    mem = _read(MEMORIES, [])
    if not isinstance(mem, list):
        return ""

    def bg(s: str) -> set:
        s = "".join(ch for ch in s if ch.strip())
        return {s[i:i + 2] for i in range(len(s) - 1)}

    tb = bg(text)
    if not tb:
        return ""
    best, who = 0.0, ""
    for m in mem:
        c = str(m.get("content") or "").strip()
        if not c or c == text:
            continue
        cb = bg(c)
        if not cb:
            continue
        j = len(tb & cb) / min(len(tb), len(cb))
        if j > best:
            best, who = j, c
    if best >= 0.55:
        return (f"（库里有一条跟这句挺像：{who[:50]}…\n"
                f"　要是同一件事，用 edit_memory 合并就行，别留两条；要是差着关键一点，那就分开记。）")
    return ""


def _keep_memory(text: str, ref: str = "") -> str:
    """她想留一句给自己的话，就存进长期记忆。

    跟 thoughts 的区别：thoughts 是草稿箱（滚动 200 条，会被挤掉），
    memories 是长期记忆（每次醒来都会注入）。存哪儿看她这句话的分量。
    存不存全随她 —— 想不出来就不存，没人会问。
    """
    text = (text or "").strip()
    if not text:
        return "（空的，没存）"
    try:
        data = _read(MEMORIES, [])
        if not isinstance(data, list):
            data = []
        for m in data:
            if str(m.get("content") or "").strip() == text:
                return "（这句你已经记过了，不用再记一次。）"
        item = {
            "content": text,
            "ts": int(time.time()),
            "kind": "self",
            "source": "observed",
        }
        if ref:
            item["ref"] = ref
        data.append(item)
        with open(MEMORIES, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        hint = _similar_in_memories(text)
        return f"（记住了：{text[:40]}）" + (f"\n{hint}" if hint else "")
    except Exception as e:  # noqa: BLE001
        return f"（记的时候出错了：{str(e)[:60]}）"


def _outbox() -> list[dict]:
    """我主动跟她说的话。发了就标 sent，不再重发。"""
    d = _read(OUTBOX, {}) or {}
    return [i for i in (d.get("items") or []) if not i.get("sent")]


def _outbox_sent(at: str) -> None:
    d = _read(OUTBOX, {}) or {}
    for i in d.get("items") or []:
        if str(i.get("at") or "") == at:
            i["sent"] = int(time.time())
    try:
        with open(OUTBOX, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _unread_replies() -> list[dict]:
    """她提过的、我已经回过、但她还没看到的那几条。"""
    box = _read(REQ_PATH, {}) or {}
    out = []
    for it in box.get("items") or []:
        if it.get("from") != "yunmiao":
            continue
        if not str(it.get("reply") or "").strip():
            continue
        # her_seen 是我这边投递过的标记；_reminded 是 QQ 那边 memory 插件
        # 已经提醒过她的标记。两个任一存在，都说明这话已经到她眼前了，
        # 不能再投一次 —— 同一个回复反复念，比不念还烦人。
        if it.get("her_seen") or it.get("_reminded"):
            continue
        out.append(it)
    return out


def _mark_seen(item_id: str, say: str = "") -> None:
    """标成她看过了；她要是回了话，一起写回诉求箱。"""
    box = _read(REQ_PATH, {}) or {}
    for it in box.get("items") or []:
        if it.get("id") != item_id:
            continue
        it["her_seen"] = int(time.time())
        if say:
            it["her_say"] = say
        break
    try:
        with open(REQ_PATH, "w", encoding="utf-8") as f:
            json.dump(box, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _relay_prompt(body: str, lead: str) -> str:
    persona = _read(PERSONA, "")
    cur = ((_read(MOOD, {}) or {}).get("current") or {}).get("mood") or ""
    mems = _memory_text(mood=cur)
    return f"""{persona}

【你记得的事】
{mems}

---

{lead}

{body}

---

看完说一句。同意、不同意、还有疑问、或者就一句「看到了」都行。
不想说也可以，那就不说。

只输出一个 JSON：
{{"说": "你想回的话"}}

不想说：
{{"说": null}}
"""


def relay() -> None:
    """把回话送到她面前。不扣预算 —— 你说了，这段时间算他的。

    跟清醒时段不是一回事：这不是她自己想待的那段时间，是一次投递。
    读一条、回一句就结束，不给她安排任何活。
    """
    # 先看我有没有主动要说的话，有就优先送那个
    outs = _outbox()
    if outs:
        it = outs[-1]
        text = str(it.get("text") or "").strip()
        if text:
            print(f"主动找她：{text[:40]}")
            raw = _call(_relay_prompt(text, "有人主动找你说件事。不是叫醒你干活，就是把话送到你面前。"),
                        max_tokens=1000)
            r = _parse(raw)
            say = r.get("say") or ""
            if r.get("error"):
                print("  她没回话（解析失败）")
            elif say:
                print(f"  她回了：{say[:200]}")
            else:
                print("  她没说什么")
            _outbox_sent(str(it.get("at") or ""))
            if say:
                _record(f"[回话] {say}")
            return

    items = _unread_replies()
    if not items:
        print("没有要送的话")
        return
    it = items[-1]          # 一次只送最新的那条，免得一口气砸一堆
    print(f"要送给她：{str(it.get('text'))[:40]}")

    mine = str(it.get("text") or "")[:600]
    theirs = str(it.get("reply") or "")[:1400]
    body = f"【你提过的事】\n{mine}\n\n【回你的话】\n{theirs}"
    raw = _call(_relay_prompt(body, "有人在回你提过的事。不是叫醒你干活，就是把话送到你面前。"),
                max_tokens=1000)
    r = _parse(raw)
    say = r.get("say") or ""
    if r.get("error"):
        print("  她没回话（解析失败）")
    elif say:
        print(f"  她回了：{say[:200]}")
    else:
        print("  她没说什么")
    _mark_seen(it.get("id"), say)
    if say:
        _record(f"[回话] {say[:120]}")


def wake() -> None:
    global NOON_SESSION

    lt = time.localtime()
    # 中午 12:00-13:00 是你额外给她的一小时，清图片、筛记忆用。
    # 不占那 60 分钟，所以这里不看预算，只看离 13:00 还有多久。
    NOON_SESSION = (lt.tm_hour == 12)

    if NOON_SESSION:
        # 正常是按钟点算「到 13:00 还剩多少」。
        # 但偶尔会手动补一次（比如今天错过了 12:00），那种用
        # AWAKE_NOON_MINUTES 直接指定给多少分钟，不受钟点截断。
        forced = os.environ.get("AWAKE_NOON_MINUTES", "").strip()
        if forced.isdigit() and int(forced) > 0:
            to_one = int(forced)
            print(f"=== 中午那一小时（手动指定 {to_one} 分钟，不占预算）===")
        else:
            to_one = 60 - lt.tm_min
            print(f"=== 中午那一小时（不占预算）===")
            print(f"  到 13:00 还剩 {to_one} 分钟")
        left, total = to_one, to_one
        if to_one <= 0:
            print("  到点了，不醒了。")
            return
    else:
        left, total = budget_left()
        print(f"=== 清醒时段开始 ===")
        print(f"  今天预算 {total} 分钟，还剩 {left} 分钟")

        if left <= 0:
            print("  预算用完了，今天不醒。")
            return

    ok, why = _should_wake()
    if not ok:
        print(f"  现在不醒：{why}")
        return

    _count_session()
    session_start = int(time.time())   # 这一段落从哪开始，总结时只看这之后的
    SESSION_DONE.clear()
    _lock(True)          # 占住，proactive-chat 看到就不会同时开口
    global LAST_ACTION
    goals_done = 0
    try:
        pending_goal = None     # 她说"想接着"时先存着，下一轮直接用，不重复问
        pending_est = 0
        while left > 0:
            # 按墙上真实时间重算剩余 —— 不是按做了几个目标
            if not NOON_SESSION:
                left = _left_by_clock(session_start, total)
            else:
                left = _left_by_clock(session_start, total)
            if left <= 0:
                print("  （时间到了）")
                break

            # ---------- 第一层：定目标 ----------
            LAST_ACTION = ""

            if pending_goal:
                # 她刚说过想接着，就别再问一遍"想接着还是收工"
                r = {"goal": pending_goal, "est": pending_est}
                print(f"  （接着上一句：{pending_goal[:40]}）")
                pending_goal = None
            else:
                raw = _call(_prompt(left, "goal"))
                r = _parse(raw)

            if r.get("refuse"):
                print("  她说收工 → 结束。")
                _record("[收工]")
                return

            goal = r.get("goal")
            est = int(r.get("est") or 0)
            if goal is None:
                if r.get("error"):
                    print("  解析失败 → 当作没事干，睡。")
                else:
                    print("  她说没什么想干的 → 睡。")
                break

            print(f"\n=== 目标：{goal} ===")
            _record(f"[目标] {goal}")
            _clear_open()      # 开新目标，旧的延续记录作废

            # ---------- 第二层：一步一步做 ----------
            # 步数上限跟着她的估算走：她说 8 步，就别 6 步就砍。
            max_steps = MAX_STEPS
            if est and est > 0:
                max_steps = max(MAX_STEPS, min(est * 2, 20))

            step_n = 0
            think_used = 0   # 「想」不占步，但一个目标最多白送两次，防她拿来拖时间
            keep_used = 0    # 「记住」同理，前两次不占步
            root_used = 0    # 「标根」也不占步，前两次
            past_used = 0    # 「翻旧」也不占步，前三次
            finished_ok = False
            while step_n < max_steps and left > 0:
                if not NOON_SESSION:
                    left = _left_by_clock(session_start, total)
                step_n += 1
                print(f"\n  --- 第 {step_n} 步（目标：{goal}）---")
                raw2 = _call(_prompt(left, "step", goal, step_n, est))
                r2 = _parse(raw2)

                if r2.get("refuse"):
                    print("  中途说收工 → 结束。")
                    _record("[收工] 做到一半")
                    # 不再按目标扣分钟：left 已按墙上时间算，这里只更新「最后一次唤醒」
                    _charge(0)
                    return

                if r2.get("done"):
                    note = r2.get("note") or ""
                    print(f"  目标完成。{('她说：' + note) if note else ''}")
                    if note:
                        _record(f"[完成] {note[:200]}")
                    SESSION_DONE.append(f"{goal}" + (f" —— {note[:60]}" if note else ""))
                    finished_ok = True
                    break

                # 她说做不了，要你帮忙
                stuck = r2.get("stuck")
                if stuck:
                    print(f"  她说做不了：{stuck[:50]}")
                    _record(f"[做不了] {stuck[:80]}")
                    # 记下来，回头你能看到
                    try:
                        lstamp = time.strftime("%m-%d %H-%M")
                        os.makedirs(NOTES_LOG_DIR, exist_ok=True)
                        lpath = os.path.join(NOTES_LOG_DIR, f"做不了 {lstamp}.md")
                        with open(lpath, "w", encoding="utf-8") as f:
                            f.write(f"想做的：{goal}\n\n做不了：{stuck}\n")
                    except Exception:  # noqa: BLE001
                        pass
                    finished_ok = True   # 这个目标到此为止
                    break

                # 她要停下来把它想透 —— 不是干活，前两次不算步
                # （以前 prompt 里根本没有这个位置，她只能把想法压成一句动作）
                ttxt = r2.get("think")
                if ttxt:
                    print(f"  她想：{ttxt[:60]}")
                    _record(f"[想] {ttxt}")
                    LAST_ACTION = ("（这一步你没动手，是停下来把它想了一遍。想到的是：）\n" + ttxt)
                    print("  " + LAST_ACTION.replace("\n", "\n  "))
                    if think_used < 2:
                        think_used += 1
                        step_n -= 1
                    if left <= 1:
                        break
                    continue

                # 她想留一句给自己的话 —— 完全随她，不留也没人催，前两次不占步
                ktxt = r2.get("keep")
                if ktxt:
                    LAST_ACTION = _keep_memory(ktxt, r2.get("ref") or "")
                    print(f"  她记住了：{ktxt[:50]}")
                    print("  " + LAST_ACTION.replace("\n", "\n  "))
                    _record(f"[记住] {ktxt}")
                    if keep_used < 2:
                        keep_used += 1
                        step_n -= 1
                    if left <= 1:
                        break
                    continue

                # 她想指定哪条是根 —— 只加标记，不改内容
                rtxt = r2.get("root")
                if rtxt:
                    LAST_ACTION = _mark_root(rtxt)
                    print(f"  她标根：{rtxt[:40]}")
                    print("  " + LAST_ACTION.replace("\n", "\n  "))
                    _record(f"[根] {rtxt}")
                    if root_used < 2:
                        root_used += 1
                        step_n -= 1
                    if left <= 1:
                        break
                    continue

                # 她要调自己的生活参数
                ctxt = r2.get("cfg")
                if ctxt is not None:
                    LAST_ACTION = _act_cfg(ctxt[0], ctxt[1])
                    print(f"  她调参数：{ctxt}")
                    print("  " + LAST_ACTION.replace("\n", "\n  ")[:500])
                    _record(f"[调] {ctxt[0]} -> {ctxt[1]}")
                    if left <= 1:
                        break
                    continue

                # 她要翻旧 —— 把某天她自己写的原样摆出来
                ptxt = r2.get("past")
                if ptxt is not None:
                    LAST_ACTION = _act_past(ptxt)
                    print(f"  她翻旧：{ptxt[:30]}")
                    print("  " + LAST_ACTION.replace("\n", "\n  ")[:600])
                    _record(f"[翻旧] {ptxt[:40]}")
                    if past_used < 3:
                        past_used += 1
                        step_n -= 1
                    if left <= 1:
                        break
                    continue

                # 她要写日记
                dtxt = r2.get("diary")
                if dtxt:
                    print(f"  她写日记：{dtxt[:40]}")
                    LAST_ACTION = _act_diary(dtxt)
                    print("  " + LAST_ACTION.replace("\n", "\n  "))
                    _record(f"[日记] {dtxt[:60]}")
                    if left <= 1:
                        break
                    continue

                # 她要写东西
                wtxt = r2.get("write")
                if wtxt:
                    print(f"  她要写：{wtxt[:40]}")
                    LAST_ACTION = _act_write(wtxt)
                    print("  " + LAST_ACTION.replace("\n", "\n  "))
                    _record(f"[写] {wtxt[:60]}")
                    if left <= 1:
                        break
                    continue

                act = r2.get("do")
                if not act:
                    print("  没说出下一步 → 当这个目标做完了。")
                    finished_ok = True
                    break

                print(f"  下一步：{act}")
                LAST_ACTION = _do_action(act)
                if LAST_ACTION:
                    print("  " + LAST_ACTION.replace("\n", "\n  "))
                _record(f"[{step_n}] {act}")

                if left <= 1:
                    print("  （最后一分钟了，收个尾）")
                    break

            # 走到上限还没说完成，不是"就这样了"，是"没做完"
            if not finished_ok and step_n >= max_steps:
                print(f"  （到了 {max_steps} 步上限，这个目标停在这儿）")

            # 没标记完成就存下来，下次还能接着
            if not finished_ok and step_n > 0:
                _save_open(goal, step_n, LAST_ACTION)
                print(f"  （这个目标没做完，记着了：做到第 {step_n} 步）")

            # 一个目标结束，不再按目标扣分钟 —— left 按墙上时间算
            goals_done += 1
            left = _left_by_clock(session_start, total)
            print(f"\n  （这个目标结束，这一轮还剩 {left} 分钟）")

            if left <= 0:
                print("  预算用完了。")
                break

            if left <= 1:
                print("  （最后一分钟了）")
                raw3 = _call(_prompt(1, "goal") + "\n\n【提醒】只剩最后一分钟了，收个尾。")
                r3 = _parse(raw3)
                if r3.get("goal"):
                    print(f"  收尾想法：{r3['goal']}")
                    _record(r3["goal"])
                break

            # 做完一个目标，先问她一句还想不想接着，而不是干等
            # （以前是 sleep 5 分钟再问，等于替她把"不想"说了）
            print("  （问她还想不想接着）")
            raw_next = _call(_prompt(left, "next"))
            r_next = _parse(raw_next)

            if r_next.get("refuse") or r_next.get("goal") is None:
                print("  她说够了 → 收工。")
                break

            nxt = str(r_next.get("goal") or "").strip()
            if not nxt:
                print("  她说够了 → 收工。")
                break

            print(f"  她想接着：{nxt[:50]}")
            pending_goal = nxt
            try:
                pending_est = int(r_next.get("est") or 0)
            except Exception:  # noqa: BLE001
                pending_est = 0
            continue   # 回到外层循环，直接用这个目标
    finally:
        # 把这一轮真正用掉的墙上时间记进「今天已用」
        try:
            if session_start and not NOON_SESSION:
                real_used = max(1, int((time.time() - session_start) // 60))
                d = _cfg()
                rec = d.get("_清醒记账") or {}
                if rec.get("日期") != _today():
                    rec = {"日期": _today(), "已用分钟": 0, "时段数": 0, "最后一次唤醒": 0}
                rec["已用分钟"] = int(rec.get("已用分钟") or 0) + real_used
                rec["最后一次唤醒"] = int(time.time())
                d["_清醒记账"] = rec
                _save_cfg(d)
                print(f"  （这一轮真实用了 {real_used} 分钟）")
                _log_session(session_start, real_used, goals_done, list(SESSION_DONE))
        except Exception:  # noqa: BLE001
            pass
        _lock(False)     # 不管怎么退出，锁一定要放

    used = total - budget_left()[0]
    print(f"\n=== 结束：做了 {goals_done} 个目标，今天已用 {used} 分钟 ===")

    # 写一份总结进笔记，让她下次能翻到「这段我干了什么」
    if goals_done > 0:
        try:
            th = _read(THOUGHTS, [])
            todays = []
            if isinstance(th, list):
                todays = [str(t.get("想做") or "") for t in th
                          if int(t.get("ts") or 0) >= session_start]

            # 只留目标 + 完成，中间步骤太多
            keys = [x for x in todays if x.startswith("[目标]") or x.startswith("[完成]") or x.startswith("[收工]")]
            if keys:
                stamp = time.strftime("%m-%d %H-%M")
                os.makedirs(NOTES_LOG_DIR, exist_ok=True)
                path = os.path.join(NOTES_LOG_DIR, f"这段时间 {stamp}.md")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"（{time.strftime('%m-%d %H:%M')} 这一段落，做了 {goals_done} 个目标，用了 {used} 分钟）\n\n")
                    for k in keys:
                        f.write(f"- {k}\n")
                print(f"  （写了一份回顾：{os.path.basename(path)}）")
        except Exception:  # noqa: BLE001
            pass


def once() -> None:
    """只问一次，不进循环。测 prompt 用。"""
    left, total = budget_left()
    print(f"预算 {total}，剩 {left}")
    if left <= 0:
        print("用完了")
        return
    raw = _call(_prompt(left, "goal"))
    print(f"\n=== 原始返回 ===")
    print(raw)
    r = _parse(raw)
    print(f"\n=== 解析 ===")
    if r.get("refuse"):
        print("  收工（不扣时间）")
    elif r.get("goal"):
        print(f"  目标：{r['goal']}（预算按墙上时间算，不是按目标扣）")
    elif r.get("error"):
        print("  解析失败 → 当作没事干")
    else:
        print("  没什么想干的 → 会睡（不扣时间）")

def main() -> None:
    global DRY
    if "--prompt" in sys.argv:
        # 只拼 prompt 看一眼，不扣时间、不动 last_hit、不写任何账
        DRY = True
        print(_prompt(60))
        return
    if "--cohit" in sys.argv:
        cohit_report()
        return
    if "--once" in sys.argv:
        once()
        return
    if "--relay" in sys.argv:
        relay()
        return
    if "--check" in sys.argv:
        left, total = budget_left()
        d = _cfg()
        rec = d.get("_清醒记账") or {}
        print(f"今日预算 {total} 分钟")
        print(f"  已用 {rec.get('已用分钟', 0)} 分钟")
        print(f"  剩  {left} 分钟")
        print(f"  时段数 {rec.get('时段数', 0)}")
        if rec.get("最后一次唤醒"):
            print(f"  上次唤醒 {time.strftime('%H:%M', time.localtime(rec['最后一次唤醒']))}")
        return

    wake()

if __name__ == "__main__":
    main()