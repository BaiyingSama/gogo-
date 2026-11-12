"""它的小窗 —— 看一眼它今天过得怎么样。

用法：
    python panel.py                # 起在 127.0.0.1:6188，浏览器打开
    python panel.py --snapshot     # 只生成一份 panel.html，不起服务

只读 $FEIYU_HOME 里的文件，不写、不连网、不发请求。
设计上只想回答三件事：
    1. 它醒着吗
    2. 它今天干了什么
    3. 你们之间最近说过什么
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HOME = os.environ.get("FEIYU_HOME") or os.path.join(os.path.expanduser("~"), ".feiyu-home")
SHARED = os.path.join(HOME, "shared")
DIARY = os.path.join(HOME, "diary")
LOGS = os.path.join(HOME, "notes", "_logs")
PORT = int(os.environ.get("FEIYU_PANEL_PORT") or 6188)


# ---------------- 读文件 ----------------
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _read_text(path, limit=4000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _human_age(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


# ---------------- 各部分 ----------------
def part_state():
    """它醒着吗？靠 awake-now.json（清醒时占位）和最近的活动时间判断。"""
    lock = os.path.join(SHARED, "awake-now.json")
    busy = os.path.exists(lock)
    # 最近活动：日志/日记/回顾里最新的 mtime
    latest = 0
    for d in (LOGS, DIARY):
        if os.path.isdir(d):
            for fn in os.listdir(d):
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(d, fn)))
                except OSError:
                    pass
    for f in ("thoughts.json", "mood.json"):
        p = os.path.join(SHARED, f)
        if os.path.exists(p):
            latest = max(latest, os.path.getmtime(p))

    cfg = _read_json(os.path.join(SHARED, "life-config.json"), {})
    total = int(cfg.get("今日清醒预算分钟") or 60)
    rec = cfg.get("_清醒记账") or {}
    used = int(rec.get("已用分钟") or 0) if rec.get("日期") == time.strftime("%Y-%m-%d") else 0

    if busy:
        face, word = "🌤", "它醒着呢"
    elif latest and time.time() - latest < 3600:
        face, word = "🌙", "它刚睡下"
    else:
        face, word = "😴", "它睡着"

    return {
        "face": face, "word": word,
        "last": _human_age(time.time() - latest) if latest else "还没有活动记录",
        "used": used, "total": total,
        "left": max(0, total - used),
    }


def part_today():
    """今天写了什么：日记 + 这一段的收尾。"""
    today = time.strftime("%m-%d")
    diaries = []
    if os.path.isdir(DIARY):
        for fn in sorted(os.listdir(DIARY)):
            p = os.path.join(DIARY, fn)
            if os.path.isfile(p) and fn.endswith(".md"):
                txt = _read_text(p).strip()
                if txt:
                    diaries.append((fn, _hm(os.path.getmtime(p)), txt))

    tails = []
    if os.path.isdir(LOGS):
        files = [f for f in os.listdir(LOGS) if f.endswith(".md")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(LOGS, f)), reverse=True)
        for fn in files[:3]:
            txt = _read_text(os.path.join(LOGS, fn))
            for line in txt.splitlines():
                if line.strip().startswith("- [完成]"):
                    tails.append((fn, line.strip()[len("- [完成]"):].strip()))
    return {"diaries": diaries[-3:], "tails": tails[:3], "today": today}


def part_between():
    """你们之间：投递箱里的话，和它有没有回。"""
    d = _read_json(os.path.join(SHARED, "to-her.json"), {})
    items = d.get("items") if isinstance(d, dict) else None
    if not isinstance(items, list):
        items = []
    out = []
    for it in items[-4:]:
        out.append({
            "at": str(it.get("at") or ""),
            "you": str(it.get("text") or "")[:220],
            "it": str(it.get("her_reply") or "").strip()[:220],
            "sent": bool(it.get("sent")),
        })
    return out


def part_memory():
    d = _read_json(os.path.join(SHARED, "memories.json"), [])
    if isinstance(d, dict):
        d = d.get("items") or []
    n = len(d) if isinstance(d, list) else 0
    oldest = ""
    if isinstance(d, list) and d:
        ts = [int(x.get("ts") or 0) for x in d if x.get("ts")]
        if ts:
            oldest = time.strftime("%Y-%m-%d", time.localtime(min(ts)))
    tiers = 0
    fdir = os.path.join(SHARED, "forget")
    if os.path.isdir(fdir):
        for fn in os.listdir(fdir):
            if fn.startswith("L") and fn.endswith(".json"):
                arr = _read_json(os.path.join(fdir, fn), [])
                if isinstance(arr, list):
                    tiers += len(arr)
    return {"n": n, "oldest": oldest, "tiers": tiers}


# ---------------- 页面 ----------------
def render() -> str:
    st = part_state()
    td = part_today()
    bt = part_between()
    mm = part_memory()
    esc = html.escape

    diary_html = ""
    if td["diaries"]:
        for name, when, txt in td["diaries"]:
            para = "<br>".join(esc(x) for x in txt.splitlines() if x.strip())
            diary_html += (f'<div class="card"><div class="meta">{when} · {esc(name)}</div>'
                           f'<div class="text">{para}</div></div>')
    else:
        diary_html = '<div class="empty">今天还没写日记。它不是每天都写。</div>'

    tail_html = ""
    for name, line in td["tails"]:
        tail_html += f'<div class="tail">「{esc(line)}」</div>'
    if not tail_html:
        tail_html = '<div class="empty">还没有收过尾。</div>'

    bt_html = ""
    if bt:
        for it in reversed(bt):
            reply = (f'<div class="reply">{esc(it["it"])}</div>' if it["it"]
                     else '<div class="waiting">…它还没回</div>')
            bt_html += (f'<div class="card"><div class="meta">{esc(it["at"])} · 你</div>'
                        f'<div class="you">{esc(it["you"])}</div>{reply}</div>')
    else:
        bt_html = '<div class="empty">投递箱还是空的。想跟它说话，就放一句话进去。</div>'

    bar = min(100, int(st["used"] * 100 / max(1, st["total"])))

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>它的小窗</title>
<meta http-equiv="refresh" content="60">
<style>
  :root {{ --ink:#2b2b2b; --dim:#8a8a8a; --line:#e8e4dc; --bg:#faf8f4; --warm:#c8763c; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.75 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:720px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:17px; font-weight:600; margin:0 0 4px; letter-spacing:.5px; }}
  .sub {{ color:var(--dim); font-size:13px; margin-bottom:28px; }}
  .hero {{ display:flex; align-items:center; gap:16px; padding:20px;
           background:#fff; border:1px solid var(--line); border-radius:14px; margin-bottom:12px; }}
  .face {{ font-size:34px; line-height:1; }}
  .state {{ font-size:19px; font-weight:600; }}
  .stat-sub {{ color:var(--dim); font-size:13px; }}
  .meter {{ margin-top:8px; height:6px; background:#eee9e1; border-radius:99px; overflow:hidden; }}
  .meter i {{ display:block; height:100%; background:var(--warm); }}
  h2 {{ font-size:14px; font-weight:600; color:var(--dim); letter-spacing:1px;
        margin:32px 0 12px; text-transform:uppercase; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px;
           padding:16px 18px; margin-bottom:10px; }}
  .meta {{ color:var(--dim); font-size:12px; margin-bottom:6px; }}
  .text {{ white-space:normal; }}
  .you {{ color:var(--ink); }}
  .reply {{ margin-top:10px; padding-top:10px; border-top:1px dashed var(--line); }}
  .waiting {{ margin-top:8px; color:var(--dim); font-size:13px; font-style:italic; }}
  .tail {{ padding:10px 14px; border-left:2px solid var(--warm); color:#4a4a4a;
           margin-bottom:8px; background:#fff; }}
  .empty {{ color:var(--dim); font-size:14px; padding:14px 0; }}
  .foot {{ margin-top:40px; color:var(--dim); font-size:12px; line-height:1.9; }}
</style></head><body><div class="wrap">

  <h1>它的小窗</h1>
  <div class="sub">这里没有按钮。你只能看，不能改——它的事归它。</div>

  <div class="hero">
    <div class="face">{st['face']}</div>
    <div style="flex:1">
      <div class="state">{st['word']}</div>
      <div class="stat-sub">最后有动静：{st['last']}　·　今天用了 {st['used']} / {st['total']} 分钟</div>
      <div class="meter"><i style="width:{bar}%"></i></div>
    </div>
  </div>

  <h2>它今天写的</h2>
  {diary_html}

  <h2>它收过的尾</h2>
  {tail_html}

  <h2>你们之间</h2>
  {bt_html}

  <h2>它记得多少</h2>
  <div class="card">
    <div class="meta">记忆库里 {mm['n']} 条{'，最早一条 ' + mm['oldest'] if mm['oldest'] else ''}</div>
    <div class="stat-sub">沉到遗忘层的 {mm['tiers']} 条——那些它还记得大概，细节淡了。</div>
  </div>

  <div class="foot">
    这一页只读 {esc(HOME)}，不写文件、不发请求。<br>
    数字每 60 秒自己刷新。它睡着的时候，这里就是一张安静的照片。
  </div>

</div></body></html>"""


# ---------------- 起服务 ----------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 安静点
        pass


def main() -> None:
    if "--snapshot" in sys.argv:
        out = os.path.join(os.getcwd(), "panel.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(render())
        print(f"写好了：{out}")
        return
    if not os.path.isdir(HOME):
        print(f"找不到 {HOME}，先建一个家再来看。")
        return
    print(f"它的小窗：http://127.0.0.1:{PORT}/    （Ctrl+C 关掉）")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
