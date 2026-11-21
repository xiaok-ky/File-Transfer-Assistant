#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFiDrop 核心层 —— 局域网传输服务的全部后端逻辑（不含界面）。

对外提供：
  * Server        HTTP 服务（手机浏览器 + 电脑互传）
  * Store         按设备隔离的存储
  * Transfers     进行中传输的统一进度表（界面轮询它画进度条）
  * Discovery     UDP 广播，自动发现局域网内其它电脑
  * Clipboard     Windows 原生剪贴板

零第三方依赖（tkinter 界面在 gui.py，二维码用 qrcode 库，已随包）。
"""

import hashlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import socketserver
import sqlite3
import struct
import threading
import time
import uuid
import zlib
from http import server
from urllib.parse import urlparse, parse_qs, quote, unquote

APP_NAME = "WiFiDrop"          # 数据目录名（**不要改**，改了会丢历史数据）
APP_TITLE = "传输助手"          # 界面显示名
APP_VERSION = "1.0"

BASE_PORT = int(os.environ.get("WIFIDROP_PORT", "8765"))
PORT_TRIES = 40
DISCOVERY_PORT = 45876
MAX_UPLOAD_MB = int(os.environ.get("WIFIDROP_MAX_UPLOAD_MB", "4096"))
HISTORY_TEXT = 200
HISTORY_FILE_DAYS = 3
CHUNK = 1 << 18          # 256KB：大文件传输的读写块
STREAM_CHUNK = 1 << 20   # 1MB：并行分片每条连接的上报粒度
DEFAULT_STREAMS = 8      # 默认并行连接数（WiFi 下单流常填不满带宽）
MIN_PARALLEL = 2 * 1024 * 1024   # 小于 2MB 不走并行


def pick_streams(size: int) -> int:
    """按文件大小决定并行数：小文件少开，大文件多开。"""
    mb = size / 1048576.0
    if mb < 2:
        return 1
    if mb < 8:
        return 4
    if mb < 64:
        return 6
    if mb < 512:
        return 8
    return 8


def tune_socket(sock, rcv=4 * 1024 * 1024, snd=4 * 1024 * 1024):
    """放大 TCP 缓冲区：WiFi 延迟较高时，默认缓冲区会让单流跑不满。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    for opt, val in ((socket.SO_RCVBUF, rcv), (socket.SO_SNDBUF, snd)):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, val)
        except Exception:
            pass


# ============================================================ 路径
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) if (sys := __import__("sys")) else False


def app_dir() -> str:
    import sys as _s
    if getattr(_s, "frozen", False):
        return os.path.dirname(os.path.abspath(_s.executable))
    return os.path.dirname(os.path.abspath(__file__))


def current_user() -> str:
    u = ""
    for k in ("USERNAME", "USER", "LOGNAME"):
        u = os.environ.get(k) or ""
        if u:
            break
    if not u:
        try:
            import getpass
            u = getpass.getuser()
        except Exception:
            u = "user"
    return re.sub(r"[^\w\.\-]", "_", u) or "user"


# ============================================================ 设备编号（直观观测）
def _pc_id_path() -> str:
    return os.path.join(DATA_ROOT, "pcid.txt")


def load_or_create_pc_id() -> str:
    """本机编号：固定 4 位数字，如 #0731。同一台电脑永远不变。"""
    try:
        if os.path.exists(_pc_id_path()):
            s = open(_pc_id_path(), encoding="utf-8").read().strip()
            if re.match(r"^\d{4}$", s):
                return s
    except Exception:
        pass
    # 用计算机名 + 用户名散列，稳定且大致不冲突
    seed = (os.environ.get("COMPUTERNAME", "") + current_user()).encode("utf-8", "ignore")
    n = int(hashlib.sha1(seed).hexdigest()[:8], 16) % 10000
    pid = "%04d" % n
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        open(_pc_id_path(), "w", encoding="utf-8").write(pid)
    except Exception:
        pass
    return pid


# ============================================================ 数据根目录
def _try_write(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".__probe")
        open(p, "w").write("1")
        os.remove(p)
        return True
    except Exception:
        return False


def pick_data_root() -> str:
    cands = []
    if os.environ.get("WIFIDROP_DATA_DIR"):
        cands.append(os.environ["WIFIDROP_DATA_DIR"])
    cands.append(os.path.join(app_dir(), "data", current_user()))
    la = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    cands.append(os.path.join(la, APP_NAME, current_user()))
    cands.append(os.path.join(os.environ.get("TEMP", "."), APP_NAME, current_user()))
    for d in cands:
        if _try_write(d):
            return d
    return cands[-1]


DATA_ROOT = pick_data_root()
PC_ID = load_or_create_pc_id()
# 每个进程一个实例 ID：用来在同机多实例时区分「是我自己」还是「别人」
INSTANCE_ID = uuid.uuid4().hex[:12]
# 兜底设备 ID：没有归属设备时用它（保证永远有个合法 id）
LOCAL_STORE = hashlib.sha1(("local:" + PC_ID).encode()).hexdigest()[:16]


def load_or_create_token() -> str:
    env = (os.environ.get("WIFIDROP_TOKEN") or "").strip()
    if env:
        return env
    f = os.path.join(DATA_ROOT, "token.txt")
    try:
        if os.path.exists(f):
            t = open(f, encoding="utf-8").read().strip()
            if t:
                return t
    except Exception:
        pass
    t = uuid.uuid4().hex[:16]
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        open(f, "w", encoding="utf-8").write(t)
    except Exception:
        pass
    return t


TOKEN = load_or_create_token()


def cookie_name() -> str:
    return "wd_" + hashlib.sha1(TOKEN.encode()).hexdigest()[:10]


COOKIE_NAME = cookie_name()


# ============================================================ 剪贴板
CF_UNICODETEXT = 13
_HAS_WIN = (os.name == "nt")
if _HAS_WIN:
    import ctypes
    from ctypes import wintypes as _wt

    _u = ctypes.WinDLL("user32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _u.OpenClipboard.restype = _wt.BOOL
    _u.OpenClipboard.argtypes = [_wt.HWND]
    _u.CloseClipboard.restype = _wt.BOOL
    _u.GetClipboardData.restype = _wt.HANDLE
    _u.GetClipboardData.argtypes = [_wt.UINT]
    _u.SetClipboardData.restype = _wt.HANDLE
    _u.SetClipboardData.argtypes = [_wt.UINT, _wt.HANDLE]
    _u.EmptyClipboard.restype = _wt.BOOL
    _k.GlobalAlloc.restype = _wt.HGLOBAL
    _k.GlobalAlloc.argtypes = [_wt.UINT, ctypes.c_size_t]
    _k.GlobalLock.restype = ctypes.c_void_p
    _k.GlobalLock.argtypes = [_wt.HGLOBAL]
    _k.GlobalUnlock.restype = _wt.BOOL
    _k.GlobalUnlock.argtypes = [_wt.HGLOBAL]
    _k.GlobalSize.restype = ctypes.c_size_t
    _k.GlobalSize.argtypes = [_wt.HGLOBAL]


def clipboard_get() -> str:
    if not _HAS_WIN:
        return ""
    try:
        if not _u.OpenClipboard(None):
            return ""
        try:
            h = _u.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            size = _k.GlobalSize(h)
            if not size:
                return ""
            p = _k.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p, size // 2).rstrip("\x00")
            finally:
                _k.GlobalUnlock(h)
        finally:
            _u.CloseClipboard()
    except Exception:
        return ""


def clipboard_set(text: str) -> bool:
    if not _HAS_WIN:
        return False
    import ctypes
    try:
        if not _u.OpenClipboard(None):
            return False
        try:
            _u.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            n = ctypes.sizeof(buf)
            h = _k.GlobalAlloc(0x0002, n)
            if not h:
                return False
            p = _k.GlobalLock(h)
            if not p:
                return False
            ctypes.memmove(p, buf, n)
            _k.GlobalUnlock(h)
            return bool(_u.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            _u.CloseClipboard()
    except Exception:
        return False


def clipboard_get_timed(seconds=1.2) -> str:
    box = {}

    def _f():
        box["t"] = clipboard_get()

    t = threading.Thread(target=_f, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("t", "")


# ============================================================ 传输进度表
class _Cancelled(Exception):
    """用户点了「中止」。"""
    pass


class Transfers:
    """所有进行中/刚结束的传输。界面每 400ms 取一次快照画进度条。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def new(self, name, total, kind, peer, direction) -> str:
        tid = uuid.uuid4().hex[:10]
        now = time.time()
        with self._lock:
            self._items[tid] = {
                "id": tid, "name": name or "未命名", "total": int(total or 0),
                "done": 0, "kind": kind, "peer": peer or "", "dir": direction,
                "state": "running", "started": now, "ended": 0.0,
                "error": "", "cancel": False, "_samples": [(now, 0)],
            }
        return tid

    def update(self, tid, done):
        now = time.time()
        with self._lock:
            it = self._items.get(tid)
            if not it:
                return
            it["done"] = int(done)
            s = it["_samples"]
            s.append((now, int(done)))
            while len(s) > 2 and now - s[0][0] > 3.0:
                s.pop(0)

    def add(self, tid, delta):
        """多线程并行传输时按增量累加（并行分片各自上报）"""
        now = time.time()
        with self._lock:
            it = self._items.get(tid)
            if not it:
                return
            it["done"] = int(it["done"]) + int(delta)
            s = it["_samples"]
            s.append((now, it["done"]))
            while len(s) > 2 and now - s[0][0] > 3.0:
                s.pop(0)

    def finish(self, tid, state="done", error=""):
        with self._lock:
            it = self._items.get(tid)
            if not it:
                return
            it["state"] = state
            it["ended"] = time.time()
            it["error"] = error or ""
            if state == "done" and it["total"]:
                it["done"] = it["total"]

    def cancelled(self, tid) -> bool:
        with self._lock:
            it = self._items.get(tid)
            return bool(it and it.get("cancel") and it.get("cancel_mode") != "pause")

    def paused(self, tid) -> bool:
        with self._lock:
            it = self._items.get(tid)
            return bool(it and it.get("state") == "paused")

    def cancel(self, tid=None, mode="abort") -> int:
        """请求中止。

        mode:
          "abort"   中止（保留已传输的内容，但不再继续）
          "pause"   暂停（留待后续可恢复；目前实现为暂停发送循环）
          "delete"  中止并删除源/目标文件
        返回被标记的数量。
        """
        n = 0
        with self._lock:
            for k, v in self._items.items():
                if v["state"] != "running":
                    continue
                if tid and k != tid:
                    continue
                v["cancel"] = True
                v["cancel_mode"] = mode
                v["state"] = "paused" if mode == "pause" else "cancelled"
                v["ended"] = time.time()
                n += 1
        return n

    def resume(self, tid) -> bool:
        """从「暂停」恢复（仅限暂停状态）。"""
        with self._lock:
            it = self._items.get(tid)
            if not it or it.get("state") != "paused":
                return False
            it["cancel"] = False
            it["cancel_mode"] = ""
            it["state"] = "running"
            it["started"] = time.time() - 0.001
            it["_samples"] = [(time.time(), it["done"])]
            return True

    def restore(self, tid, name, total, done, state="paused"):
        """进程重启后，把之前未完成的任务登记回进度表。"""
        now = time.time()
        with self._lock:
            self._items[tid] = {
                "id": tid, "name": name or "文件", "total": int(total or 0),
                "done": int(done or 0), "kind": "pc", "peer": "", "dir": "up",
                "state": state, "started": now, "ended": now,
                "error": "", "cancel": False, "_samples": [(now, int(done or 0))],
            }

    def set_running(self, tid) -> bool:
        """把任务标记回运行中（续传前调用）。"""
        with self._lock:
            it = self._items.get(tid)
            if not it:
                return False
            it["cancel"] = False
            it["cancel_mode"] = ""
            it["state"] = "running"
            it["started"] = time.time()
            it["ended"] = 0.0
            it["_samples"] = [(time.time(), it["done"])]
            return True

    def _speed(self, it):
        """用最近采样算瞬时速度；样本不足时退化为平均速度。"""
        s = it.get("_samples") or []
        if len(s) >= 2:
            (t0, d0), (t1, d1) = s[0], s[-1]
            dt = t1 - t0
            if dt >= 0.25:
                return max(0.0, (d1 - d0) / dt)
        el = (it["ended"] or time.time()) - it["started"]
        if el > 0.3 and it["state"] != "running":
            return it["done"] / el
        return 0.0

    def snapshot(self):
        now = time.time()
        out = []
        with self._lock:
            for it in self._items.values():
                d = {k: v for k, v in it.items() if k != "_samples"}
                total, done = d["total"], d["done"]
                speed = self._speed(it)
                if d["state"] != "running":
                    el = max(0.001, (d["ended"] or now) - d["started"])
                    if done:
                        speed = done / el
                d["speed"] = speed
                d["percent"] = (done / total * 100.0) if total else (
                    100.0 if d["state"] == "done" else 0.0)
                if speed > 512 and total > done and d["state"] == "running":
                    d["eta"] = (total - done) / speed
                else:
                    d["eta"] = None
                out.append(d)
        out.sort(key=lambda x: (x["state"] != "running", -x["started"]))
        return out

    def clear_done(self):
        with self._lock:
            for k in [k for k, v in self._items.items() if v["state"] != "running"]:
                self._items.pop(k, None)

    def running_count(self):
        with self._lock:
            return sum(1 for v in self._items.values() if v["state"] == "running")


TRANSFERS = Transfers()


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def human_speed(n) -> str:
    return human_size(n) + "/s"


def human_eta(sec) -> str:
    if sec is None:
        return "--"
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 时 %d 分" % (sec // 3600, (sec % 3600) // 60)


# ============================================================ 存储
DB_LOCK = threading.Lock()
DEV_RE = re.compile(r"^[0-9a-f]{16}$")


def clean_dev(raw) -> str:
    s = str(raw or "").strip().lower()[:40]
    return s if DEV_RE.match(s) else ""


# 「外部文件」标记：stored 以这个前缀开头时，表示文件就在原位置，
# 没有复制进「文件」文件夹（省一份硬盘，也不拖慢发送）
EXT_PREFIX = "@ext:"


def ext_stored(path: str) -> str:
    """把外部绝对路径编码成 stored 字段。"""
    return EXT_PREFIX + os.path.abspath(path)


def resolve_stored(stored: str) -> str:
    """把 stored 解析成真实磁盘路径。"""
    if not stored:
        return ""
    if stored.startswith(EXT_PREFIX):
        return stored[len(EXT_PREFIX):]
    return os.path.join(files_dir(), stored)


def stored_is_ext(stored: str) -> bool:
    return bool(stored) and stored.startswith(EXT_PREFIX)


def new_dev() -> str:
    return uuid.uuid4().hex[:16]


# 文件目录：<数据根>/文件/  —— 只有一层，文件直接相邻
FILES_DIR = os.path.join(DATA_ROOT, "文件")


def files_dir() -> str:
    os.makedirs(FILES_DIR, exist_ok=True)
    return FILES_DIR


def devdir(dev: str) -> str:
    d = clean_dev(dev) or "0000000000000000"
    p = os.path.join(DATA_ROOT, "devices", d)
    os.makedirs(p, exist_ok=True)
    return p


def devdb(dev: str) -> str:
    return os.path.join(devdir(dev), "data.db")


def _conn(path):
    c = sqlite3.connect(path, timeout=10)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return c


def db_init(dev: str):
    with _conn(devdb(dev)) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS items(
            id TEXT PRIMARY KEY, ts REAL, kind TEXT, text TEXT,
            fname TEXT, stored TEXT, size INTEGER, devno TEXT, frompc TEXT)""")
        # 老库补列：clip = 这条文字是否已送进电脑剪贴板
        cols = {r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()}
        if "clip" not in cols:
            try:
                c.execute("ALTER TABLE items ADD COLUMN clip INTEGER DEFAULT 0")
            except Exception:
                pass


_INITED = set()
_INIT_LOCK = threading.Lock()


def ensure_db(dev: str):
    """懒建表：任何拿设备 ID 直接写库的地方都先过这一道，避免 no such table。"""
    d = clean_dev(dev) or LOCAL_STORE
    if d in _INITED:
        return d
    with _INIT_LOCK:
        if d not in _INITED:
            db_init(d)
            _INITED.add(d)
    return d


def device_for_pc(pcid: str) -> str:
    """另一台电脑发来的内容归到哪里。

    注意：如果 pcid 就是**本机**（自己发给自己、或回环测试），
    绝不能新建一个「电脑」设备——否则界面会凭空多出一台设备。
    统一返回 LOCAL_STORE。
    """
    pcid = str(pcid or "0")[:8]
    if pcid == str(PC_ID):
        return ensure_db(LOCAL_STORE)
    did = hashlib.sha1(("pc:" + pcid).encode()).hexdigest()[:16]
    reg = load_devices()
    if did not in reg:
        used = {v.get("no", 0) for v in reg.values()}
        no = 1
        while no in used:
            no += 1
        reg[did] = {"id": did, "label": "电脑 #" + pcid, "first": time.time(), "no": no}
        save_devices(reg)
    ensure_db(did)
    return did


def ensure_local_device():
    """确保「本机」在设备表里存在且有编号（编号为 0，列表里显示为「本机」）。"""
    reg = load_devices()
    me = clean_dev(LOCAL_STORE)
    if me not in reg:
        reg[me] = {"id": me, "label": "本机", "first": 0, "no": 0}
        save_devices(reg)
    ensure_db(me)
    return me


def _prune(c):
    cutoff = time.time() - HISTORY_FILE_DAYS * 86400
    for (iid, stored) in c.execute(
            "SELECT id, stored FROM items WHERE kind='file' AND ts < ?", (cutoff,)).fetchall():
        _rm_file(stored)
        c.execute("DELETE FROM items WHERE id=?", (iid,))
    for (iid,) in c.execute(
            "SELECT id FROM items WHERE kind='text' ORDER BY ts DESC LIMIT -1 OFFSET ?",
            (HISTORY_TEXT,)).fetchall():
        c.execute("DELETE FROM items WHERE id=?", (iid,))
    for (iid, stored) in c.execute(
            "SELECT id, stored FROM items ORDER BY ts DESC LIMIT -1 OFFSET 600").fetchall():
        _rm_file(stored)
        c.execute("DELETE FROM items WHERE id=?", (iid,))


def _rm_file(stored):
    if not stored:
        return
    try:
        p = os.path.join(files_dir(), stored)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def db_add(dev, kind, text="", fname="", stored="", size=0, devno="", frompc="",
           clip=0) -> dict:
    dev = ensure_db(dev)
    if not devno:
        reg = load_devices()
        devno = str(reg.get(dev, {}).get("no", ""))
    it = {"id": uuid.uuid4().hex[:12], "ts": time.time(), "kind": kind, "text": text,
          "fname": fname, "size": size, "devno": devno, "frompc": frompc, "clip": int(clip)}
    with DB_LOCK, _conn(devdb(dev)) as c:
        c.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (it["id"], it["ts"], kind, text, fname, stored, size, devno, frompc,
                   int(clip)))
        _prune(c)
    return it


def push_text(targets, text, to_clipboard=False):
    """电脑端发文字。

    **只写一条记录**（写进目标设备），不额外写「本机」，避免界面出现两行。
    如果没有任何真实设备，就记到本机（否则发了就丢了）。
    """
    text = (text or "").strip()
    if not text:
        return []
    reg = load_devices()
    me = clean_dev(LOCAL_STORE)
    real = _real_devices(reg, me)
    if not targets or "all" in targets:
        ids = real
    else:
        ids = [clean_dev(t) for t in targets if clean_dev(t) and clean_dev(t) != me]
    if not ids:
        # 没有设备可发：至少在本机留一条，方便回看
        ids = [ensure_local_device()]
    out = []
    for d in ids:
        try:
            out.append(db_add(d, "text", text=text, frompc="电脑"))
        except Exception:
            pass
    if to_clipboard:
        threading.Thread(target=clipboard_set, args=(text,), daemon=True).start()
    return out


def db_list(dev, limit=200):
    dev = ensure_db(dev)
    with DB_LOCK, _conn(devdb(dev)) as c:
        rows = c.execute("SELECT id,ts,kind,text,fname,stored,size,devno,frompc,clip "
                         "FROM items ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "text": r[3], "fname": r[4],
             "stored": r[5], "size": r[6], "devno": r[7], "frompc": r[8],
             "clip": r[9] or 0} for r in rows]


def db_since(dev, ts):
    dev = ensure_db(dev)
    with DB_LOCK, _conn(devdb(dev)) as c:
        rows = c.execute("SELECT id,ts,kind,text,fname,stored,size,devno,frompc,clip "
                         "FROM items WHERE ts > ? ORDER BY ts ASC LIMIT 100", (ts,)).fetchall()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "text": r[3], "fname": r[4],
             "stored": r[5], "size": r[6], "devno": r[7], "frompc": r[8],
             "clip": r[9] or 0} for r in rows]


def db_get(dev, iid):
    dev = ensure_db(dev)
    with DB_LOCK, _conn(devdb(dev)) as c:
        return c.execute("SELECT stored,fname FROM items WHERE id=?", (iid,)).fetchone()


def db_delete(dev, iid):
    """删除一条记录（文件本体由调用方处理）。"""
    dev = ensure_db(dev)
    with DB_LOCK, _conn(devdb(dev)) as c:
        c.execute("DELETE FROM items WHERE id=?", (iid,))


def clear_all_records() -> dict:
    """清除所有记录：清空每个设备的数据库 + 删除「文件」文件夹里的文件。

    **绝不碰外部源文件**（桌面等位置的 @ext: 记录只是记录本身被删）。
    返回 {"records": 删掉的记录数, "files": 删掉的文件数, "kept": 跳过数}
    """
    ddir = os.path.join(DATA_ROOT, "devices")
    n_rec = 0
    n_keep = 0
    try:
        for name in os.listdir(ddir):
            if not DEV_RE.match(name):
                continue
            try:
                with DB_LOCK, _conn(devdb(name)) as c:
                    rows = c.execute("SELECT stored FROM items").fetchall()
                    n_rec += len(rows)
                    for (s,) in rows:
                        if stored_is_ext(s):
                            n_keep += 1
                    c.execute("DELETE FROM items")
            except Exception:
                continue
    except Exception:
        pass

    # 删除「文件」文件夹里的所有文件（只删这一层，不动子目录/别处）
    n_file = 0
    fdir = os.path.abspath(files_dir())
    try:
        for fn in os.listdir(fdir):
            p = os.path.join(fdir, fn)
            if not os.path.isfile(p):
                continue
            if not _inside_dir(fdir, p):
                continue
            try:
                os.remove(p)
                n_file += 1
            except Exception:
                pass
    except Exception:
        pass
    return {"records": n_rec, "files": n_file, "kept": n_keep}


def _inside_dir(parent: str, child: str) -> bool:
    """child 是否确实在 parent 目录里（删除前安全校验）。"""
    try:
        p = os.path.abspath(parent).rstrip("\\/").lower()
        c = os.path.abspath(child).lower()
        return c.startswith(p + os.sep) or c == p
    except Exception:
        return False


def db_clear(dev):
    dev = ensure_db(dev)
    with DB_LOCK, _conn(devdb(dev)) as c:
        rows = c.execute("SELECT stored FROM items").fetchall()
        c.execute("DELETE FROM items")
    for (s,) in rows:
        _rm_file(s)


def _copy_into_shared(path: str, name: str) -> dict:
    """把文件复制进「文件」目录一次，返回 {stored, size, name}。"""
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "file"
    size = os.path.getsize(path)
    stored = unique_fname(safe)
    dst = os.path.join(files_dir(), stored)
    tid = TRANSFERS.new(safe, size, "down", "本机 → 手机", "up")
    try:
        with open(path, "rb") as src, open(dst, "wb") as out:
            copied = 0
            while True:
                if TRANSFERS.cancelled(tid):
                    raise _Cancelled()
                b = src.read(CHUNK)
                if not b:
                    break
                out.write(b)
                copied += len(b)
                TRANSFERS.update(tid, copied)
    except _Cancelled:
        try:
            os.remove(dst)
        except Exception:
            pass
        TRANSFERS.finish(tid, "cancelled", "已中止")
        return {"ok": False, "cancelled": True}
    except Exception as e:
        try:
            os.remove(dst)
        except Exception:
            pass
        TRANSFERS.finish(tid, "error", str(e))
        return {"ok": False, "error": str(e)}
    TRANSFERS.finish(tid, "done")
    return {"ok": True, "stored": stored, "size": size, "name": safe}


def dedupe_local_records():
    """清理历史遗留的重复记录。

    老版本「发文件到手机」会同时记两条：本机一条「本机发出」+ 目标设备一条。
    这里把本机里与真实设备重复的那些记录删掉，只保留真实设备那条。
    启动时跑一次即可，幂等。
    """
    me = clean_dev(LOCAL_STORE)
    try:
        devdir = os.path.join(DATA_ROOT, "devices")
        if not os.path.isdir(devdir):
            return 0
        # 收集真实设备里用到的 stored
        real_stored = set()
        for d in os.listdir(devdir):
            if not DEV_RE.match(d) or d == me:
                continue
            try:
                for it in db_list(d, 500):
                    if it.get("stored"):
                        real_stored.add(it["stored"])
            except Exception:
                continue
        if not real_stored:
            return 0
        removed = 0
        with DB_LOCK, _conn(devdb(me)) as c:
            rows = c.execute("SELECT id, stored FROM items").fetchall()
            for iid, stored in rows:
                if stored and stored in real_stored:
                    c.execute("DELETE FROM items WHERE id=?", (iid,))
                    removed += 1
        return removed
    except Exception:
        return 0


def _real_devices(reg=None, me=None):
    """真正的「外部设备」列表：排除本机条目。

    判定：编号 > 0 且不是本机存储。编号 0 是本机，从未配对过的不算。
    """
    reg = reg if reg is not None else load_devices()
    me = me or clean_dev(LOCAL_STORE)
    out = []
    for k, v in reg.items():
        kid = clean_dev(k)
        if kid == me:
            continue
        try:
            no = int(v.get("no", 0))
        except Exception:
            no = 0
        if no <= 0:
            continue
        out.append(kid)
    return out


def push_file(targets, path: str, name=None):
    """发给一台或多台真实设备（"all" = 所有已配对设备，不含本机）。

    **不复制文件**：只登记原路径；手机下载时程序实时读取原文件推过去。
      * 硬盘零额外占用（哪怕 10GB 的电影也不会多占 10GB）
      * 发送瞬间完成（不用先等复制）
      * 内存不受影响（下载时边读边发，不会整个读进内存）
    唯一影响：原文件被移动/删除后，手机就下不到了（会给出明确提示）。
    """
    reg = load_devices()
    me = clean_dev(LOCAL_STORE)
    real = _real_devices(reg, me)
    if not targets or "all" in targets:
        ids = real
    else:
        ids = [clean_dev(t) for t in targets if clean_dev(t) and clean_dev(t) != me]
    if not ids:
        return {"targets": 0, "results": [],
                "error": "还没有已连接的设备，先让手机连一次"}

    if not path or not os.path.isfile(path):
        return {"targets": len(ids), "results": [
            {"ok": False, "error": "文件不存在"}]}

    name = name or os.path.basename(path)
    safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "file"
    size = os.path.getsize(path)
    stored = ext_stored(path)     # 只记路径，不复制

    res = []
    for d in ids:
        try:
            it = db_add(d, "file", fname=safe, stored=stored,
                        size=size, frompc="电脑")
            res.append({"ok": True, "item": it})
        except Exception as e:
            res.append({"ok": False, "error": str(e)})
    return {"targets": len(ids), "results": res, "stored": stored,
            "name": safe, "size": size}


def record_local_send(kind="text", text="", fname="", stored="", size=0) -> dict:
    """在「本机」名下记一条「我发出的」内容，供界面回看。

    与目标设备的记录**分开**，这样界面上「发出去」和「收到的」一眼能分清。
    """
    dev = ensure_local_device()
    return db_add(dev, kind, text=text, fname=fname, stored=stored, size=size,
                  frompc="本机发出")


# ============================================================ 设备登记
def _devreg() -> str:
    return os.path.join(DATA_ROOT, "devices.json")


def load_devices() -> dict:
    try:
        return json.load(open(_devreg(), encoding="utf-8"))
    except Exception:
        return {}


def save_devices(d):
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        json.dump(d, open(_devreg(), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def devices_list():
    """列出所有设备。

    规则：
      * 「本机」（LOCAL_STORE）永远在列表里，编号 0，标签「本机」
      * 只有**真实配对过的**外部设备（手机等）才显示
      * 磁盘上多出来的、来自本机 pcid 的伪设备会被自动清理（老版本遗留）
    """
    me = clean_dev(LOCAL_STORE)
    reg = load_devices()
    changed = False

    # 1) 清掉老版本误建的「本机 pcid」设备（它其实是本机自己）
    fake = hashlib.sha1(("pc:" + str(PC_ID)).encode()).hexdigest()[:16]
    for bad in (fake,):
        if bad in reg and bad != me:
            reg.pop(bad, None)
            changed = True
            # 把它名下的记录并入本机
            try:
                for it in db_list(bad, 500):
                    rec = db_get(bad, it["id"])
                    db_add(me, it["kind"], text=it.get("text", ""),
                           fname=it.get("fname", ""),
                           stored=(rec[0] if rec else ""),
                           size=it.get("size", 0), frompc="本机发出")
            except Exception:
                pass
            try:
                shutil.rmtree(os.path.join(DATA_ROOT, "devices", bad), ignore_errors=True)
            except Exception:
                pass

    # 2) 保证本机条目存在
    if me not in reg:
        reg[me] = {"id": me, "label": "本机", "first": 0, "no": 0}
        changed = True

    # 3) 补齐磁盘上真实存在的设备目录（这类才有意义：真配过对）
    #    注意：必须跳过已经清理掉的伪设备目录，否则会被重新加回来
    ddir = os.path.join(DATA_ROOT, "devices")
    try:
        for name in os.listdir(ddir):
            if not DEV_RE.match(name):
                continue
            if name == me or name == fake or name in reg:
                continue
            used = {v.get("no", 0) for v in reg.values()}
            no = 1
            while no in used:
                no += 1
            reg[name] = {"id": name, "label": "设备", "first": time.time(), "no": no}
            changed = True
    except Exception:
        pass

    if changed:
        save_devices(reg)

    out = []
    for did, info in sorted(reg.items(),
                            key=lambda kv: (kv[1].get("no", 999), kv[1].get("first", 0))):
        try:
            cnt = len(db_list(did, 200))
        except Exception:
            cnt = 0
        label = info.get("label", "")
        if clean_dev(did) == me:
            label = "本机"
        out.append({"id": did, "label": label, "count": cnt,
                    "no": info.get("no", 1), "first": info.get("first", 0),
                    "is_local": clean_dev(did) == me})
    return out


def _guess_label(ua: str) -> str:
    u = (ua or "").lower()
    for kw, name in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "安卓手机"),
                     ("harmony", "华为手机"), ("windows", "Windows 电脑"),
                     ("macintosh", "Mac 电脑"), ("linux", "Linux 电脑")):
        if kw in u:
            return name
    return "未知设备"


def register_device(dev: str, label: str) -> dict:
    reg = load_devices()
    if dev not in reg:
        used = {v.get("no", 0) for v in reg.values()}
        no = 1
        while no in used:
            no += 1
        reg[dev] = {"id": dev, "label": label, "first": time.time(), "no": no}
        save_devices(reg)
    db_init(dev)
    return reg[dev]


# ============================================================ 二维码
_qr_cache = {}


def _png(matrix, scale=7, border=3) -> bytes:
    n = len(matrix)
    size = (n + border * 2) * scale
    white = b"\xff" * (size * 3)
    rows = []
    for y in range(size):
        ry = y // scale - border
        if ry < 0 or ry >= n:
            rows.append(white)
            continue
        line = bytearray()
        for x in range(size):
            cx = x // scale - border
            line += b"\x00\x00\x00" if (0 <= cx < n and matrix[ry][cx]) else b"\xff\xff\xff"
        rows.append(bytes(line))
    raw = b"".join(b"\x00" + r for r in rows)

    def ch(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", ihdr)
            + ch(b"IDAT", zlib.compress(raw, 9)) + ch(b"IEND", b""))


def qr_png(text: str, target_px: int = 0, border: int = 4) -> bytes:
    """生成二维码 PNG。

    target_px > 0 时按「整数倍缩放」精确凑到接近这个边长——二维码必须用整数倍放大
    才清晰可扫；返回的图片尺寸就是最终显示尺寸，调用方不要再去裁它。
    """
    key = (text, target_px, border)
    if key in _qr_cache:
        return _qr_cache[key]
    try:
        import qrcode
        q = qrcode.QRCode(border=border)
        q.add_data(text)
        q.make(fit=True)
        matrix = q.get_matrix()      # 注意：get_matrix() 已经包含 border 静区
        n = len(matrix)
        if target_px and target_px > 0:
            scale = max(1, target_px // max(1, n))
        else:
            scale = 7
        # border=0：静区已经在 matrix 里了，再加会重复
        b = _png(matrix, scale=scale, border=0)
        _qr_cache[key] = b
        return b
    except Exception:
        return b""


# ============================================================ 局域网发现
class Discovery:
    """UDP 广播互相发现。只用来列出「局域网内其它电脑」，不传文件。"""

    def __init__(self):
        self.peers = {}
        self._lock = threading.Lock()
        self.port = 0
        self._stop = False

    def start(self, my_port):
        self.port = my_port
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._broadcast, daemon=True).start()

    def _payload(self):
        return json.dumps({"app": APP_NAME, "v": APP_VERSION, "pc": PC_ID,
                           "port": self.port, "token": TOKEN,
                           "inst": INSTANCE_ID,
                           "user": current_user()}).encode()

    def _broadcast(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while not self._stop:
            try:
                s.sendto(self._payload(), ("<broadcast>", DISCOVERY_PORT))
            except Exception:
                pass
            for _ in range(20):
                if self._stop:
                    break
                time.sleep(0.1)
        s.close()

    def _listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("", DISCOVERY_PORT))
        except Exception:
            return
        s.settimeout(1.0)
        while not self._stop:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                self._gc()
                continue
            except Exception:
                break
            try:
                d = json.loads(data.decode("utf-8", "ignore"))
            except Exception:
                continue
            if d.get("app") != APP_NAME:
                continue
            # 只跳过「我自己」这个进程；同机其它实例要能看到
            if d.get("inst") == INSTANCE_ID:
                continue
            ip = addr[0]
            with self._lock:
                key = "%s:%s" % (ip, d.get("port"))
                self.peers[key] = {"ip": ip, "pc": d.get("pc", "?"), "port": d.get("port"),
                                   "token": d.get("token", ""), "user": d.get("user", ""),
                                   "seen": time.time(), "v": d.get("v", "")}
        s.close()

    def _gc(self):
        now = time.time()
        with self._lock:
            for k in [k for k, v in self.peers.items() if now - v["seen"] > 12]:
                self.peers.pop(k, None)

    def list(self):
        self._gc()
        with self._lock:
            return sorted(self.peers.values(), key=lambda x: (x["ip"], x["port"] or 0))

    def stop(self):
        self._stop = True


DISCOVERY = Discovery()


# ============================================================ 断点续传（电脑 → 电脑 / 电脑 → 手机）
RESUME_LOCK = threading.RLock()      # 可重入：内部函数会在已持锁时再进来
RESUME_TASKS = {}


def _task_dir() -> str:
    d = os.path.join(DATA_ROOT, "resume")
    os.makedirs(d, exist_ok=True)
    return d


def _task_file(tid: str) -> str:
    return os.path.join(_task_dir(), "task_" + tid + ".json")


def _save_resume_task(tid: str):
    with RESUME_LOCK:
        t = RESUME_TASKS.get(tid)
        if not t:
            return
        t["saved"] = time.time()
        data = dict(t)
    try:
        tmp = _task_file(tid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _task_file(tid))
    except Exception:
        pass


def _drop_resume_task(tid: str):
    with RESUME_LOCK:
        RESUME_TASKS.pop(tid, None)
    try:
        p = _task_file(tid)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def list_resumable() -> list:
    """列出还能续传的任务（文件还在 + 没传完）。"""
    out = []
    with RESUME_LOCK:
        for tid, t in RESUME_TASKS.items():
            path = t.get("path")
            if not path or not os.path.exists(path):
                continue
            written = t.get("written") or {}
            done = sum(int(v) for v in written.values())
            if done >= int(t.get("size") or 0) > 0:
                continue
            out.append({
                "tid": tid,
                "name": t.get("name"),
                "size": t.get("size"),
                "done": done,
                "percent": (done / max(1, int(t.get("size") or 1))) * 100.0,
                "peer": t.get("peer", {}),
                "saved": t.get("saved", 0),
            })
    out.sort(key=lambda x: -x["saved"])
    return out


def restore_resume_tasks():
    """进程启动时把未完成的发送任务读回来（标记为「已暂停（可续传）」）。"""
    d = _task_dir()
    n = 0
    try:
        for fn in os.listdir(d):
            if not fn.startswith("task_") or not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    t = json.load(f)
            except Exception:
                continue
            tid = t.get("tid")
            path = t.get("path")
            if not tid or not path or not os.path.exists(path):
                continue
            written = t.get("written") or {}
            done = sum(int(v) for v in written.values())
            size = int(t.get("size") or 0)
            if done >= size > 0:
                continue
            with RESUME_LOCK:
                RESUME_TASKS[tid] = t
            # 显示在进度区，标为「已暂停（可续传）」
            TRANSFERS.restore(tid, t.get("name") or "文件", size, done, "paused")
            n += 1
    except Exception:
        pass
    return n


def resume_task(tid: str):
    """继续一个未完成的发送任务。"""
    with RESUME_LOCK:
        t = dict(RESUME_TASKS.get(tid) or {})
    if not t:
        return {"ok": False, "error": "任务不存在"}
    path = t.get("path")
    if not path or not os.path.exists(path):
        _drop_resume_task(tid)
        return {"ok": False, "error": "源文件已不存在"}
    # 标记回运行中
    TRANSFERS.set_running(tid)
    threading.Thread(target=_run_resume, args=(tid, t), daemon=True).start()
    return {"ok": True}


def _run_resume(tid, t):
    """真正执行续传：只发「还没写过去」的那部分。"""
    peer = t["peer"]
    base = str(peer.get("token", ""))
    upid = t.get("upid") or ""
    size = int(t.get("size") or 0)
    path = t["path"]
    ranges = t.get("ranges") or []
    written = dict(t.get("written") or {})
    try:
        # 第一步：问对方还缺哪些。对方还认得这个会话（upid 有效）就直接续传，
        # **绝不能重新 begin**——否则对方会新建第二个文件。
        resumed = False
        try:
            info = _post_json(peer["ip"], peer["port"], "/%s/api/p2p-offsets" % base,
                              {"id": upid})
            if info.get("ok"):
                remote = info.get("written") or {}
                written = {k: int(v) for k, v in remote.items()}
                resumed = True
                print("[resume] 对方会话仍在，从断点继续 %s" % upid, flush=True)
        except Exception:
            pass

        if not resumed:
            # 对方会话已经没了（重启过 / 清理过）→ 这时才重新 begin，
            # 用**原来那个 upid**，让对方复用已有的半截文件（如果还在）。
            print("[resume] 对方没有这个会话，重新登记: %s" % upid, flush=True)
            try:
                meta = _post_json(peer["ip"], peer["port"], "/%s/api/p2p-begin" % base,
                                  {"name": t.get("name"), "size": size,
                                   "frompc": PC_ID, "upid": upid})
                upid = meta.get("id") or upid
                if meta.get("written"):
                    written = {k: int(v) for k, v in meta["written"].items()}
            except Exception as e:
                TRANSFERS.finish(tid, "error", "对方不在线：%s" % e)
                return
        # 记下最终 upid（可能被对方换过）
        with RESUME_LOCK:
            RESUME_TASKS.setdefault(tid, {})["upid"] = upid

        n = max(1, min(int(t.get("streams") or 8), 16))
        part = size // n
        errs = []
        done = [sum(written.values())]
        lock = threading.Lock()

        def worker(idx, off, ln):
            have = int(written.get(str(off), 0))
            if have >= ln:
                return
            start = off + have
            length = ln - have
            try:
                conn = _new_conn_send(peer["ip"], peer["port"], timeout=120)
                conn.putrequest("POST", "/%s/api/p2p-part?id=%s&offset=%d"
                                % (base, upid, start))
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(length))
                conn.endheaders()
                with open(path, "rb") as f:
                    f.seek(start)
                    remain = length
                    while remain > 0:
                        if TRANSFERS.cancelled(tid):
                            raise _Cancelled()
                        if TRANSFERS.paused(tid):
                            raise _Cancelled()
                        b = f.read(min(CHUNK, remain))
                        if not b:
                            break
                        conn.send(b)
                        remain -= len(b)
                        with lock:
                            done[0] += len(b)
                            cur = done[0]
                            with RESUME_LOCK:
                                RESUME_TASKS.get(tid, {}).setdefault(
                                    "written", {})[str(off)] = ln - remain
                        TRANSFERS.update(tid, cur)
                r = conn.getresponse()
                r.read()
                conn.close()
                if r.status != 200:
                    errs.append("分片%d HTTP %d" % (idx, r.status))
            except _Cancelled:
                errs.append("__cancelled__")
            except Exception as e:
                errs.append("分片%d %s" % (idx, e))

        ths = []
        for i in range(n):
            off = i * part
            ln = part if i < n - 1 else (size - off)
            if ln > 0:
                ths.append(threading.Thread(target=worker, args=(i, off, ln), daemon=True))
        for th in ths:
            th.start()
        for th in ths:
            th.join()

        _save_resume_task(tid)
        if "__cancelled__" in errs:
            if TRANSFERS.paused(tid):
                TRANSFERS.finish(tid, "paused", "已暂停（可续传）")
            else:
                _drop_resume_task(tid)
            return
        if errs:
            TRANSFERS.finish(tid, "error", errs[0])
            return
        _post_json(peer["ip"], peer["port"], "/%s/api/p2p-end" % base,
                   {"id": upid, "frompc": PC_ID})
        TRANSFERS.finish(tid, "done")
        _drop_resume_task(tid)
    except Exception as e:
        TRANSFERS.finish(tid, "error", str(e))


# ============================================================ 并行传输（多连接分片）
P2P_LOCK = threading.Lock()
P2P_INFLIGHT = {}


def _resume_dir() -> str:
    d = os.path.join(DATA_ROOT, "resume")
    os.makedirs(d, exist_ok=True)
    return d


def _resume_path(upid: str) -> str:
    return os.path.join(_resume_dir(), upid + ".json")


def _save_resume(upid: str):
    """把「哪个分片收到了多少」落盘，进程重启后还能接着传。"""
    with P2P_LOCK:
        info = P2P_INFLIGHT.get(upid)
        if not info:
            return
        data = {
            "id": upid, "name": info["name"], "stored": info["stored"],
            "size": info["size"], "owner": info.get("owner", ""),
            "frompc": info.get("frompc", ""),
            "written": dict(info.get("written") or {}),
            "saved": time.time(),
        }
    try:
        tmp = _resume_path(upid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _resume_path(upid))
    except Exception:
        pass


def _load_resume_all() -> list:
    out = []
    d = _resume_dir()
    try:
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _del_resume(upid: str):
    try:
        p = _resume_path(upid)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def p2p_restore():
    """进程启动时调用：把上次中断的接收任务重新登记，等发送方来续传。"""
    restored = 0
    for rec in _load_resume_all():
        upid = rec.get("id")
        if not upid:
            continue
        stored = rec.get("stored") or ""
        path = os.path.join(files_dir(), stored)
        if not stored or not os.path.exists(path):
            _del_resume(upid)          # 文件都没了，记录作废
            continue
        size = int(rec.get("size") or 0)
        written = rec.get("written") or {}
        done = sum(int(v) for v in written.values())
        with P2P_LOCK:
            P2P_INFLIGHT[upid] = {"path": path, "stored": stored,
                                  "name": rec.get("name") or "file", "size": size,
                                  "tid": "", "done": done, "started": time.time(),
                                  "frompc": rec.get("frompc", ""),
                                  "owner": clean_dev(rec.get("owner", "")),
                                  "written": {k: int(v) for k, v in written.items()},
                                  "resumed": True}
        restored += 1
    return restored


def p2p_offsets(upid: str) -> dict:
    """查询某个会话的续传信息：每片已收到多少。"""
    with P2P_LOCK:
        info = P2P_INFLIGHT.get(upid)
        if not info:
            return {"ok": False}
        return {"ok": True, "size": info["size"], "name": info["name"],
                "written": dict(info.get("written") or {})}


def p2p_begin(name: str, size: int, owner: str = "", upid: str = "") -> dict:
    """接收方：为一次并行上传建占位文件。

    upid 非空 → 这是「续传」：
      * 内存里还有这个会话 → 直接复用，返回已有进度
      * 内存没了但磁盘上有续传记录（进程重启过） → 从磁盘恢复，**复用原文件**
      * 都没有 → 用这个 upid 新建（保持 id 不变，方便发送方继续）
    """
    safe = "".join(c for c in (name or "file") if c not in '\\/:*?"<>|').strip() or "file"
    if upid:
        with P2P_LOCK:
            info = P2P_INFLIGHT.get(upid)
        if info:
            return {"id": upid, "tid": info.get("tid", ""), "resume": True,
                    "size": info["size"], "written": dict(info.get("written") or {})}
        # 磁盘上有没有这次上传的续传记录？
        rec = None
        try:
            rp = _resume_path(upid)
            if os.path.exists(rp):
                with open(rp, encoding="utf-8") as f:
                    rec = json.load(f)
        except Exception:
            rec = None
        if rec:
            stored = rec.get("stored") or ""
            oldpath = os.path.join(files_dir(), stored)
            if stored and os.path.exists(oldpath):
                written = {k: int(v) for k, v in (rec.get("written") or {}).items()}
                done = sum(written.values())
                with P2P_LOCK:
                    P2P_INFLIGHT[upid] = {
                        "path": oldpath, "stored": stored,
                        "name": rec.get("name") or safe,
                        "size": int(rec.get("size") or size),
                        "tid": "", "done": done, "started": time.time(),
                        "frompc": rec.get("frompc", ""),
                        "owner": clean_dev(rec.get("owner", "")),
                        "written": written, "resumed": True,
                    }
                return {"id": upid, "tid": "", "resume": True,
                        "size": P2P_INFLIGHT[upid]["size"], "written": written}

    stored = unique_fname(safe)   # 保持原名，重名才加 (1)(2)
    path = os.path.join(files_dir(), stored)
    if not upid:
        upid = uuid.uuid4().hex[:12]
    with open(path, "wb") as f:
        f.truncate(int(size))          # 预分配（NTFS 上是稀疏的，很快）
    tid = TRANSFERS.new(safe, size, "pc", "并行", "down")
    with P2P_LOCK:
        P2P_INFLIGHT[upid] = {"path": path, "stored": stored, "name": safe,
                              "size": int(size), "tid": tid, "done": 0,
                              "started": time.time(), "frompc": "",
                              "owner": clean_dev(owner), "written": {}}
    _save_resume(upid)
    return {"id": upid, "tid": tid, "resume": False, "size": int(size), "written": {}}


def p2p_write(upid: str, offset: int, data: bytes):
    """接收方：把某个分片的一段数据写到文件指定位置。

    分片以「起始 offset」为键记录**已连续写入的字节数**（不用担心断点错位：
    发送方永远从 offset + written 开始续传）。
    """
    with P2P_LOCK:
        info = P2P_INFLIGHT.get(upid)
    if not info:
        return False
    with open(info["path"], "r+b") as f:
        f.seek(offset)
        f.write(data)
        f.flush()
    with P2P_LOCK:
        info["done"] += len(data)
        w = info.setdefault("written", {})
        w[str(offset)] = w.get(str(offset), 0) + len(data)
        cur = info["done"]
        tid = info.get("tid")
    if tid:
        TRANSFERS.update(tid, cur)
    _save_resume(upid)
    return True


def p2p_end(upid: str, frompc: str = ""):
    """接收方：收齐后登记到数据库。

    归属优先级：手机（owner）> 发送方电脑 > 本机兜底。
    """
    with P2P_LOCK:
        info = P2P_INFLIGHT.pop(upid, None)
    if not info:
        return None
    _del_resume(upid)
    size = os.path.getsize(info["path"])
    pcid = (frompc or info.get("frompc") or "").strip()
    owner = info.get("owner") or ""
    if owner:
        dev = ensure_db(owner)
        src = "手机"
    elif pcid:
        dev = device_for_pc(pcid)
        src = "电脑 #" + str(pcid)
    else:
        dev = ensure_db(LOCAL_STORE)
        src = "电脑"
    it = db_add(dev, "file", fname=info["name"], stored=info["stored"],
                size=size, frompc=src)
    tid = info.get("tid")
    if tid:
        TRANSFERS.finish(tid, "done")
    return it


def p2p_abort(upid: str):
    """中止（或暂停）一次并行接收。

    mode="abort"   → 删半截文件、删续传记录
    mode="pause"   → **保留**续传记录，下次可以接着传
    """
    with P2P_LOCK:
        info = P2P_INFLIGHT.get(upid)
    if not info:
        return
    tid = info.get("tid")
    if tid:
        TRANSFERS.cancel(tid)
    with P2P_LOCK:
        P2P_INFLIGHT.pop(upid, None)
    try:
        if os.path.exists(info["path"]):
            os.remove(info["path"])
    except Exception:
        pass
    _del_resume(upid)


def p2p_set_from(upid: str, pcid: str):
    with P2P_LOCK:
        info = P2P_INFLIGHT.get(upid)
        if info:
            info["frompc"] = str(pcid or "")[:8]


# ============================================================ 发文件给别人（电脑互传）
def _new_conn(ip, port, timeout=60):
    conn = http.client.HTTPConnection(ip, int(port), timeout=timeout)
    conn.connect()
    tune_socket(conn.sock)
    return conn


def _new_conn_send(ip, port, timeout=60):
    """上传专用连接：短写超时，让线程能定期醒来检查暂停/中止。"""
    conn = _new_conn(ip, port, timeout)
    try:
        conn.sock.settimeout(2.0)
    except Exception:
        pass
    return conn


def _post_json(ip, port, path, obj, timeout=15):
    """发一个 JSON POST 并读回结果。**必须有超时**，否则对端不理睬会永久挂住。"""
    conn = _new_conn(ip, port, timeout)
    try:
        conn.sock.settimeout(timeout)
    except Exception:
        pass
    try:
        body = json.dumps(obj).encode()
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        if r.status != 200:
            raise RuntimeError("HTTP %d: %s" % (r.status, data[:200]))
        return json.loads(data or b"{}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def send_file_to_peer(peer: dict, path: str, name=None, streams=DEFAULT_STREAMS):
    """把本地文件推给另一台电脑。

    大文件走「多连接并行分片」：把文件切成 N 段，N 条 TCP 同时传。
    单条 WiFi TCP 常因延迟/窗口限制跑不满带宽，多流并发能显著提速。
    """
    name = name or os.path.basename(path)
    size = os.path.getsize(path)
    tid = TRANSFERS.new(name, size, "pc", "%s:%s" % (peer.get("ip"), peer.get("port")), "up")
    base = str(peer.get("token", ""))

    def _fallback(reason):
        """并行失败就退回单连接，保证能传。"""
        try:
            return _send_single(peer, path, name, tid)
        except Exception as e:
            TRANSFERS.finish(tid, "error", "%s / %s" % (reason, e))
            return tid

    if size < MIN_PARALLEL or streams <= 1:
        return _send_single(peer, path, name, tid)

    try:
        meta = _post_json(peer["ip"], peer["port"], "/%s/api/p2p-begin" % base,
                          {"name": name, "size": size, "frompc": PC_ID})
        upid = meta["id"]
    except Exception as e:
        return _fallback("对方不支持并行")

    n = max(1, min(int(streams), 16))
    part = size // n
    ranges = []
    for i in range(n):
        off = i * part
        ln = part if i < n - 1 else (size - off)
        if ln > 0:
            ranges.append((i, off, ln))

    # 记下这个任务，重启后可续传
    RESUME_LOCK.acquire()
    try:
        RESUME_TASKS[tid] = {
            "tid": tid, "kind": "pc", "path": path, "name": name, "size": size,
            "peer": {k: peer.get(k) for k in ("ip", "port", "token", "pc")},
            "upid": upid, "ranges": ranges, "streams": n,
            "written": {}, "saved": time.time(),
        }
    finally:
        RESUME_LOCK.release()
    _save_resume_task(tid)

    done = [0]
    lock = threading.Lock()
    errs = []
    cancelled_flag = [False]
    paused_flag = [False]
    live_conns = []          # 记住所有连接，暂停时直接 shutdown，打断阻塞的 send
    live_lock = threading.Lock()

    def kill_all_conns():
        with live_lock:
            for c in live_conns:
                try:
                    c.sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    c.close()
                except Exception:
                    pass
            live_conns.clear()

    def worker(idx, off, ln):
        conn = None
        try:
            conn = _new_conn_send(peer["ip"], peer["port"], timeout=120)
            with live_lock:
                live_conns.append(conn)
            conn.putrequest("POST", "/%s/api/p2p-part?id=%s&offset=%d" % (base, upid, off))
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(ln))
            conn.endheaders()
            with open(path, "rb") as f:
                f.seek(off)
                remain = ln
                while remain > 0:
                    if TRANSFERS.cancelled(tid) or TRANSFERS.paused(tid):
                        raise _Cancelled()
                    b = f.read(min(CHUNK, remain))
                    if not b:
                        break
                    conn.send(b)
                    remain -= len(b)
                    sent = ln - remain
                    with lock:
                        done[0] += len(b)
                        cur = done[0]
                        RESUME_TASKS.get(tid, {}).setdefault("written", {})[str(off)] = sent
                    TRANSFERS.update(tid, cur)
            r = conn.getresponse()
            r.read()
            if r.status != 200:
                errs.append("分片%d HTTP %d" % (idx, r.status))
        except _Cancelled:
            errs.append("__cancelled__")
        except Exception as e:
            if TRANSFERS.cancelled(tid) or TRANSFERS.paused(tid):
                errs.append("__cancelled__")
            else:
                errs.append("分片%d %s" % (idx, e))
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    ths = [threading.Thread(target=worker, args=r, daemon=True) for r in ranges]
    for t in ths:
        t.start()

    # 暂停/中止信号一到：先强关所有连接（打断阻塞的 send），再让线程收尾
    deadline = time.time() + 60
    seen_pause = seen_cancel = False
    while time.time() < deadline:
        if TRANSFERS.paused(tid):
            seen_pause = True
            break
        if TRANSFERS.cancelled(tid):
            seen_cancel = True
            break
        if not any(t.is_alive() for t in ths):
            break
        time.sleep(0.03)

    if seen_pause or seen_cancel:
        paused_flag[0] = seen_pause
        cancelled_flag[0] = seen_cancel
        kill_all_conns()
        time.sleep(0.1)

    for t in ths:
        t.join(timeout=5)

    # 稍等一下，让服务端把已收到的部分落盘
    time.sleep(0.15)

    with RESUME_LOCK:
        RESUME_TASKS.get(tid, {})["saved"] = time.time()
        _save_resume_task(tid)

    if paused_flag[0]:
        TRANSFERS.finish(tid, "paused", "已暂停（可续传）")
        try:
            _post_json(peer["ip"], peer["port"], "/%s/api/p2p-pause" % base, {"id": upid})
        except Exception:
            pass
        return tid

    if cancelled_flag[0]:
        TRANSFERS.finish(tid, "cancelled", "已中止")
        _drop_resume_task(tid)
        try:
            _post_json(peer["ip"], peer["port"], "/%s/api/p2p-abort" % base, {"id": upid})
        except Exception:
            pass
        return tid

    if errs:
        return _fallback("并行出错:" + errs[0])

    try:
        _post_json(peer["ip"], peer["port"], "/%s/api/p2p-end" % base,
                   {"id": upid, "frompc": PC_ID})
    except Exception as e:
        TRANSFERS.finish(tid, "error", "收尾失败: %s" % e)
        return tid

    TRANSFERS.finish(tid, "done")
    return tid


def _send_single(peer, path, name, tid=None):
    """单连接上传（兜底 & 小文件）。"""
    size = os.path.getsize(path)
    if tid is None:
        tid = TRANSFERS.new(name, size, "pc", "%s:%s" % (peer.get("ip"), peer.get("port")), "up")
    try:
        conn = _new_conn_send(peer["ip"], peer["port"], timeout=120)
        boundary = "----wd" + uuid.uuid4().hex
        head = (
            "--%s\r\n"
            'Content-Disposition: form-data; name="f"; filename="%s"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)
        ).encode("utf-8")
        tail = ("\r\n--%s--\r\n" % boundary).encode()
        total = len(head) + size + len(tail)
        conn.putrequest("POST", "/%s/api/pc-receive" % peer.get("token", ""))
        conn.putheader("Content-Type", "multipart/form-data; boundary=%s" % boundary)
        conn.putheader("Content-Length", str(total))
        conn.putheader("X-PC-From", PC_ID)
        conn.putheader("X-PC-From-User", quote(current_user(), safe=""))
        conn.endheaders()
        conn.send(head)
        sent = 0
        with open(path, "rb") as f:
            while True:
                if TRANSFERS.cancelled(tid):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    TRANSFERS.finish(tid, "cancelled", "已中止")
                    return tid
                if TRANSFERS.paused(tid):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    TRANSFERS.finish(tid, "paused", "已暂停")
                    return tid
                b = f.read(CHUNK)
                if not b:
                    break
                conn.send(b)
                sent += len(b)
                TRANSFERS.update(tid, sent)
        conn.send(tail)
        r = conn.getresponse()
        r.read()
        conn.close()
        if r.status == 200:
            TRANSFERS.finish(tid, "done")
        else:
            TRANSFERS.finish(tid, "error", "对方拒绝：HTTP %d" % r.status)
    except Exception as e:
        TRANSFERS.finish(tid, "error", "%s: %s" % (type(e).__name__, e))
    return tid


# ============================================================ HTTP
def _human(n):
    return human_size(n)


def unique_fname(name: str) -> str:
    """在「文件」目录里找一个不冲突的文件名：保持原名，重名才加 (1)(2)。"""
    safe = "".join(c for c in (name or "file") if c not in '\\/:*?"<>|').strip()
    if not safe:
        safe = "file"
    d = files_dir()
    if not os.path.exists(os.path.join(d, safe)):
        return safe
    stem, ext = os.path.splitext(safe)
    i = 1
    while i < 1000:
        cand = "%s(%d)%s" % (stem, i, ext)
        if not os.path.exists(os.path.join(d, cand)):
            return cand
        i += 1
    # 极端情况：加时间戳兜底
    return "%s_%s%s" % (stem, uuid.uuid4().hex[:6], ext)


def _guess_mime(name: str) -> str:
    """按扩展名给出 Content-Type，让浏览器能预览图片/PDF，而不是一律下载。"""
    ext = os.path.splitext(name or "")[1].lower()
    return {
        # 图片
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml", ".heic": "image/heic",
        # 文档（Word/Excel/PPT 必须写全，否则手机存成 .bin）
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8", ".json": "application/json",
        ".html": "text/html; charset=utf-8", ".rtf": "application/rtf",
        # 音视频
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
        ".aac": "audio/aac", ".flac": "audio/flac",
        # 压缩包 / 安装包
        ".zip": "application/zip", ".rar": "application/vnd.rar",
        ".7z": "application/x-7z-compressed",
        ".apk": "application/vnd.android.package-archive",
    }.get(ext, "application/octet-stream")

class Handler(server.BaseHTTPRequestHandler):
    server_version = APP_NAME + "/" + APP_VERSION
    protocol_version = "HTTP/1.1"
    ui_ctx = {"base_url": "", "hook": None}

    def log_message(self, *a):
        pass

    # ---------- 工具 ----------
    def _parts(self):
        p = urlparse(self.path)
        seg = [x for x in p.path.split("/") if x]
        if seg and seg[0] == TOKEN:
            return seg[1:], parse_qs(p.query)
        return None, parse_qs(p.query)

    def _cookies(self):
        out = {}
        for s in (self.headers.get("Cookie") or "").split(";"):
            if "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = unquote(v.strip())
        return out

    def _dev(self):
        raw = (self.headers.get("X-Device-Id") or "").strip()
        if not raw:
            raw = (self._cookies().get(COOKIE_NAME) or "").strip()
        return clean_dev(raw)

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), headers=headers)

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code)

    def _body(self, limit=None):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if limit and n > limit:
            return None
        return self.rfile.read(n)

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._err(500, "%s: %s" % (type(e).__name__, e))
            except Exception:
                pass

    def _get(self):
        seg, q = self._parts()
        if seg is None:
            return self._send(404, b"not found", "text/plain")
        head = seg[0] if seg else ""

        if head in ("", "index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if head == "devices":
            return self._json({"devices": devices_list()})
        if head == "qr.png":
            b = qr_png(self.ui_ctx.get("base_url") or "")
            return self._send(200, b, "image/png") if b else self._err(404, "no qr")

        # GET 也放行这几个：只靠 token 认证，不需要设备 ID
        if head == "api" and len(seg) > 1:
            if seg[1] == "resumable":
                return self._json({"items": list_resumable()})
            if seg[1] == "version":
                return self._json({"app": APP_NAME, "v": APP_VERSION,
                                   "pc": PC_ID, "port": PORT,
                                   "data": DATA_ROOT})

        # 下载/预览必须在设备校验之前：<img>、新标签页都不带自定义请求头，
        # 强依赖设备 ID 会直接 401（这就是"点开图片显示 device not paired"的原因）。
        # 安全性靠 URL 里的 token + 随机文件 id，已经足够。
        if head == "dl" and len(seg) > 1:
            return self._download(self._dev(), seg[1], q)

        dev = self._dev()
        if not dev:
            return self._err(401, "device not paired")

        if head == "api" and len(seg) > 1:
            act = seg[1]
            if act == "me":
                reg = load_devices()
                return self._json({"id": dev, "no": reg.get(dev, {}).get("no", 1),
                                   "pc": PC_ID, "files": files_dir()})
            if act == "list":
                return self._json({"items": db_list(dev), "now": time.time()})
            if act == "since":
                try:
                    ts = float((q.get("ts") or ["0"])[0])
                except ValueError:
                    ts = 0.0
                return self._json({"items": db_since(dev, ts), "now": time.time()})
            if act == "clip":
                return self._json({"text": clipboard_get_timed()})
            return self._err(404, "not found")

        return self._err(404, "not found")

    def _find_item_anywhere(self, iid):
        """按 id 在所有设备里找文件记录。

        为什么要这样：`<img>`、新标签页、"另存为"这些场景**不会带自定义请求头**，
        cookie 也可能失效，所以下载不能强依赖设备 ID。用文件 id 全局查一次即可
        —— id 是随机 12 位十六进制，本身就足够当能力凭据。
        """
        try:
            for d in devices_list():
                row = db_get(d["id"], iid)
                if row and row[0]:
                    return row
        except Exception:
            pass
        return None

    def _download(self, dev, iid, q=None):
        # 先按当前设备查；查不到就全局查（兼容 <img>/新标签页等不带头的场景）
        row = db_get(dev, iid) if dev else None
        if not row or not row[0]:
            row = self._find_item_anywhere(iid)
        if not row or not row[0]:
            return self._err(404, "not found")
        stored = row[0]
        # 外部文件（没复制进「文件」文件夹）→ 读原路径；
        # 安全性：路径来自数据库里登记过的记录，不接受 URL 传任意路径。
        path = resolve_stored(stored)
        if not os.path.isfile(path):
            return self._err(404, "gone")
        size = os.path.getsize(path)
        name = row[1] or "download"
        # ASCII 回退名：中文会被过滤掉，若只剩扩展名（如 "大文件.bin" → ".bin"）
        # 就退回一个安全的默认名，避免部分浏览器把它存成隐藏文件
        an = name.encode("ascii", "ignore").decode().replace('"', "").strip()
        if not an or an.startswith("."):
            an = "file" + (os.path.splitext(name)[1] or ".bin")
        # 按扩展名给正确的 Content-Type：浏览器才能"预览"而不是一律下载
        ctype = _guess_mime(name)
        # ?dl=1 → 强制下载；默认 inline → 图片/PDF 可直接在手机上看
        q = q or {}
        force_dl = (q.get("dl") or [""])[0] == "1"
        disp = "attachment" if force_dl else "inline"
        peer = self.client_address[0]
        tid = TRANSFERS.new(name, size, "down", peer, "down")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         "%s; filename=\"%s\"; filename*=UTF-8''%s"
                         % (disp, an, quote(name, safe="")))
        self.end_headers()
        sent = 0
        try:
            with open(path, "rb") as f:
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    self.wfile.write(b)
                    sent += len(b)
                    TRANSFERS.update(tid, sent)
            TRANSFERS.finish(tid, "done")
        except (BrokenPipeError, ConnectionResetError):
            TRANSFERS.finish(tid, "error", "对方断开")
        except Exception as e:
            TRANSFERS.finish(tid, "error", str(e))

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            # 出错时请求体可能还没读完。若直接回响应再关连接，Windows 会发 RST，
            # 客户端只能看到「连接被中断」。所以先把剩余请求体吃掉再回错误。
            try:
                self._drain()
            except Exception:
                pass
            try:
                self._err(500, "%s: %s" % (type(e).__name__, e))
            except Exception:
                pass

    def _drain(self, budget=2.0):
        """出错时把内核接收缓冲里剩下的请求体丢掉。

        关键：不能按 Content-Length 硬读——请求体可能已被读掉一部分，硬读会永久阻塞。
        这里用 select 探测「真的有数据可读」才读，并且总时长封顶。
        """
        import select
        t0 = time.time()
        while time.time() - t0 < budget:
            try:
                r, _, _ = select.select([self.connection], [], [], 0.05)
            except Exception:
                return
            if not r:
                return
            try:
                b = self.connection.recv(CHUNK)
            except Exception:
                return
            if not b:
                return

    def _post(self):
        seg, q = self._parts()
        if seg is None:
            return self._send(404, b"not found", "text/plain")
        head = seg[0] if seg else ""

        if head == "pair":
            b = self._body(65536) or b""
            try:
                d = json.loads(b.decode("utf-8") or "{}")
            except Exception:
                return self._err(400, "bad json")
            did = clean_dev(d.get("deviceId"))
            if d.get("newDevice") or not did:
                did = new_dev()
            info = register_device(did, _guess_label(self.headers.get("User-Agent", "")))
            ck = "%s=%s; Path=/; Max-Age=31536000; SameSite=Lax" % (COOKIE_NAME, did)
            return self._json({"ok": True, "id": did, "no": info.get("no", 1)},
                              headers={"Set-Cookie": ck})

        # 电脑互传 / 续传入口：只靠 URL 里的 token 认证，不需要浏览器设备 ID
        if head == "api" and len(seg) > 1:
            act = seg[1]
            if act == "resumable":
                return self._json({"items": list_resumable()})
            if act == "resume-task":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                return self._json(resume_task((d.get("id") or "").strip()))
            if act == "p2p-begin":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    return self._err(400, "bad json")
                try:
                    meta = p2p_begin(d.get("name") or "file", int(d.get("size") or 0),
                                     owner=self._dev(),
                                     upid=(d.get("upid") or "").strip())
                    if d.get("frompc"):
                        p2p_set_from(meta["id"], d.get("frompc"))
                except Exception as e:
                    return self._err(500, "begin failed: %s" % e)
                return self._json({"ok": True, **meta})
            if act == "p2p-part":
                return self._p2p_part(q)
            if act == "p2p-abort":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                p2p_abort(d.get("id") or "")
                return self._json({"ok": True})
            if act == "p2p-pause":
                # 暂停：只标记，**保留**半截文件和续传记录
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                upid = d.get("id") or ""
                with P2P_LOCK:
                    info = P2P_INFLIGHT.get(upid)
                    if info and info.get("tid"):
                        TRANSFERS.cancel(info["tid"], "pause")
                _save_resume(upid)
                return self._json({"ok": True})
            if act == "p2p-offsets":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                return self._json(p2p_offsets(d.get("id") or ""))
            if act == "cancel":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                tid = (d.get("id") or "").strip()
                mode = (d.get("mode") or "abort").strip()
                if mode not in ("abort", "pause", "delete"):
                    mode = "abort"
                n = TRANSFERS.cancel(tid or None, mode)
                if mode == "delete":
                    # 删除源文件（电脑本地发送的那个）。注意：没传 id 也要删，
                    # 因为用户可能就是冲着「删文件」来的。
                    src = (d.get("path") or "").strip()
                    if src and os.path.exists(src):
                        try:
                            os.remove(src)
                        except Exception:
                            pass
                return self._json({"ok": True, "cancelled": n, "mode": mode})
            if act == "resume":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                ok = TRANSFERS.resume((d.get("id") or "").strip())
                return self._json({"ok": ok})
            if act == "push-file":
                # 电脑 → 手机 发文件
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    return self._err(400, "bad json")
                path = (d.get("path") or "").strip()
                if not path or not os.path.exists(path):
                    return self._err(400, "file not found")
                targets = d.get("targets") or ["all"]
                res = push_file(targets, path, d.get("name"))
                return self._json({"ok": True, **res})
            if act == "p2p-end":
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8") or "{}")
                except Exception:
                    d = {}
                it = p2p_end(d.get("id") or "", d.get("frompc") or "")
                if not it:
                    return self._err(404, "unknown upload")
                hook = Handler.ui_ctx.get("hook")
                if hook:
                    try:
                        hook("pc_receive", it)
                    except Exception:
                        pass
                return self._json({"ok": True, "item": it})
            if act == "pc-receive":
                return self._pc_receive()

        dev = self._dev()
        if not dev:
            return self._err(401, "device not paired")

        if head == "api" and len(seg) > 1:
            act = seg[1]
            if act == "text":
                b = self._body(16 * 1024 * 1024) or b""
                try:
                    d = json.loads(b.decode("utf-8", "replace") or "{}")
                except Exception:
                    return self._err(400, "bad json")
                t = (d.get("text") or "").strip()
                if not t:
                    return self._err(400, "empty")
                to_clip = bool(d.get("toClipboard"))
                reg = load_devices()
                it = db_add(dev, "text", text=t, devno=str(reg.get(dev, {}).get("no", "")),
                            frompc=self.client_address[0], clip=1 if to_clip else 0)
                if to_clip:
                    threading.Thread(target=clipboard_set, args=(t,), daemon=True).start()
                return self._json({"ok": True, "item": it})
            if act == "to-clipboard":
                # 把某条已存在的记录（通常是电脑发出去的）重新放进剪贴板
                b = self._body(65536) or b""
                try:
                    d = json.loads(b.decode("utf-8", "replace") or "{}")
                except Exception:
                    d = {}
                t = (d.get("text") or "").strip()
                if not t:
                    return self._err(400, "empty")
                threading.Thread(target=clipboard_set, args=(t,), daemon=True).start()
                return self._json({"ok": True})
            if act == "upload":
                return self._upload(dev)
            if act == "clear":
                db_clear(dev)
                return self._json({"ok": True})
            return self._err(404, "not found")

        return self._err(404, "not found")

    def _p2p_part(self, q):
        """并行分片：把这一整片数据顺序写到文件的指定偏移。

        支持续传：如果这一片的 offset 已经有写入记录，会跳过已收到的部分。

        关键：发送方可能中途暂停/中止并直接断开连接。此时 read 会返回空，
        必须**立即收尾返回**，绝不能再继续等（否则线程卡死、目标端半截文件状态不对）。
        """
        try:
            upid = (q.get("id") or [""])[0]
            off = int((q.get("offset") or ["0"])[0])
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            return self._err(400, "bad params")
        if n <= 0 or n > MAX_UPLOAD_MB * 1024 * 1024:
            return self._err(400, "bad length")

        with P2P_LOCK:
            info = P2P_INFLIGHT.get(upid)
        if not info:
            # 连接还挂着，先把请求体丢掉，避免 RST
            try:
                self._drain()
            except Exception:
                pass
            return self._err(404, "unknown upload")

        tid = info.get("tid") or ""
        with P2P_LOCK:
            already = int((info.get("written") or {}).get(str(off), 0))
        skip = min(already, n)
        if skip:
            left = skip
            while left > 0:
                b = self.rfile.read(min(CHUNK, left))
                if not b:
                    break
                left -= len(b)
            n -= skip
            off += skip
            if n <= 0:
                return self._json({"ok": True, "got": 0, "skipped": skip})

        got = 0
        short = False
        try:
            with open(info["path"], "r+b") as f:
                f.seek(off)
                while got < n:
                    if tid and TRANSFERS.cancelled(tid):
                        raise _Cancelled()
                    try:
                        chunk = self.rfile.read(min(CHUNK, n - got))
                    except Exception:
                        short = True
                        break
                    if not chunk:
                        short = True          # 对方断开了（暂停/中止/关机）
                        break
                    f.write(chunk)
                    got += len(chunk)
                    with P2P_LOCK:
                        info["done"] += len(chunk)
                        w = info.setdefault("written", {})
                        w[str(off)] = w.get(str(off), 0) + len(chunk)
                        cur = info["done"]
                    if tid:
                        TRANSFERS.update(tid, cur)
        except _Cancelled:
            _save_resume(upid)
            return self._err(499, "cancelled")
        except Exception as e:
            _save_resume(upid)
            return self._err(500, "part failed: %s" % e)
        _save_resume(upid)
        if short:
            # 对方断开：已写部分已经落盘并记录，返回 200 让发送方知道进度有效
            return self._json({"ok": True, "got": got, "short": True})
        return self._json({"ok": True, "got": got, "skipped": skip})

    def _pc_receive(self):
        """接收另一台电脑推来的大文件，边收边更新进度。"""
        ctype = self.headers.get("Content-Type", "")
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if "multipart/form-data" not in ctype or total <= 0:
            return self._err(400, "need multipart")
        bnd = ctype.split("boundary=")[-1].strip('"').encode()
        frm = self.headers.get("X-PC-From", "?")
        who = unquote(self.headers.get("X-PC-From-User", "") or "")

        head_len, name = self._parse_multipart_head()
        safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "recv.bin"
        tail_len = 2 + 2 + len(bnd) + 4
        size = max(0, total - head_len - tail_len)

        tid = TRANSFERS.new(safe, size, "pc", "%s(%s)" % (frm, who or "?"), "down")
        stored = unique_fname(safe)   # 保持原名，重名才加 (1)(2)
        dst = os.path.join(files_dir(), stored)
        try:
            with open(dst, "wb") as f:
                self._read_exact(size, sink=f,
                                 on_progress=lambda n: TRANSFERS.update(tid, n))
            try:
                self._read_exact(tail_len)
            except Exception:
                pass
        except Exception as e:
            TRANSFERS.finish(tid, "error", str(e))
            return self._err(500, "receive failed: %s" % e)

        # 单连接来的电脑文件同样归到「发送方电脑」的编号下
        dev = device_for_pc(frm) if frm and frm != "?" else ensure_db(LOCAL_STORE)
        it = db_add(dev, "file", fname=safe, stored=stored, size=os.path.getsize(dst),
                    frompc=("电脑 #" + str(frm)) if frm and frm != "?" else "电脑")
        TRANSFERS.finish(tid, "done")
        hook = Handler.ui_ctx.get("hook")
        if hook:
            try:
                hook("pc_receive", it)
            except Exception:
                pass
        return self._json({"ok": True, "item": it})

    def _read_exact(self, n, sink=None, on_progress=None):
        """从请求体精确读 n 字节。边读边写 sink（文件对象），并回调进度。"""
        got = 0
        while got < n:
            chunk = self.rfile.read(min(CHUNK, n - got))
            if not chunk:
                break
            if sink is not None:
                sink.write(chunk)
            got += len(chunk)
            if on_progress:
                on_progress(got)
        return got

    def _parse_multipart_head(self):
        """读到 \r\n\r\n 为止，解析出文件名。返回 (已读字节数, 文件名)。"""
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            b = self.rfile.read(1)
            if not b:
                break
            buf += b
        hl = buf.decode("utf-8", "ignore")
        name = "file"
        if 'filename="' in hl:
            name = hl.split('filename="', 1)[1].split('"', 1)[0]
        return len(buf), name

    def _upload(self, dev):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._err(400, "need multipart")
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total <= 0:
            return self._err(400, "empty")
        if total > MAX_UPLOAD_MB * 1024 * 1024:
            return self._err(413, "too large")
        bnd = ctype.split("boundary=")[-1].strip('"').encode()

        head_len, name = self._parse_multipart_head()
        safe = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "file"
        # 尾部固定是 \r\n--<boundary>--\r\n
        tail_len = 2 + 2 + len(bnd) + 4
        size = max(0, total - head_len - tail_len)

        peer = self.client_address[0]
        tid = TRANSFERS.new(safe, size, "up", peer, "up")
        stored = unique_fname(safe)   # 保持原名，重名才加 (1)(2)
        dst = os.path.join(files_dir(), stored)
        try:
            with open(dst, "wb") as f:
                self._read_exact(size, sink=f,
                                 on_progress=lambda n: TRANSFERS.update(tid, n))
            try:
                self._read_exact(tail_len)
            except Exception:
                pass
        except Exception as e:
            TRANSFERS.finish(tid, "error", str(e))
            return self._err(500, "upload failed: %s" % e)

        reg = load_devices()
        it = db_add(dev, "file", fname=safe, stored=stored,
                    size=os.path.getsize(dst), devno=str(reg.get(dev, {}).get("no", "")),
                    frompc=peer)
        TRANSFERS.finish(tid, "done")
        hook = Handler.ui_ctx.get("hook")
        if hook:
            try:
                hook("upload", it)
            except Exception:
                pass
        return self._json({"ok": True, "item": it})


class Threaded(server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def process_request(self, request, client_address):
        # 每个连接都放大 TCP 缓冲区并关掉 Nagle，大文件才跑得满
        tune_socket(request)
        super().process_request(request, client_address)


def _probe_token(port, token) -> bool:
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        c.sendall(("GET /%s/devices HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                   % token).encode())
        buf = b""
        while len(buf) < 3000:
            b = c.recv(4096)
            if not b:
                break
            buf += b
        c.close()
        return b"devices" in buf
    except Exception:
        return False


def _free(port) -> bool:
    s = socket.socket()
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        return True
    except OSError:
        s.close()
        return False


def acquire_port():
    for p in range(BASE_PORT, BASE_PORT + PORT_TRIES):
        if not _free(p):
            if _probe_token(p, TOKEN):
                return p, True
            continue
        return p, False
    return -1, False


def lan_ip() -> str:
    cands = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        cands.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["ipconfig"], capture_output=True, timeout=5).stdout
        for line in out.decode("gbk", "ignore").splitlines():
            if "IPv4" in line and ":" in line:
                ip = line.split(":")[-1].strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    cands.append(ip)
    except Exception:
        pass

    def sc(ip):
        if ip.startswith(("169.254.", "127.")):
            return -1
        if ip.startswith("192.168."):
            return 10
        if ip.startswith("10."):
            return 8
        return 5

    best = max(cands or ["127.0.0.1"], key=sc)
    return best if sc(best) >= 0 else "127.0.0.1"


HOST_IP = lan_ip()


def start_server():
    """返回 (httpd, port, already_running)。"""
    port, already = acquire_port()
    if port == -1:
        raise RuntimeError("端口 %d~%d 全被占用" % (BASE_PORT, BASE_PORT + PORT_TRIES - 1))
    if already:
        return None, port, True
    # 断点续传：把上次没传完的接收/发送任务读回来
    try:
        p2p_restore()
    except Exception:
        pass
    try:
        restore_resume_tasks()
    except Exception:
        pass
    try:
        n = dedupe_local_records()
        if n:
            print("（已清理 %d 条重复的历史记录）" % n)
    except Exception:
        pass
    httpd = Threaded(("0.0.0.0", port), Handler)
    Handler.ui_ctx["base_url"] = "http://%s:%d/%s/" % (HOST_IP, port, TOKEN)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, False


# ============================================================ 手机端网页
PAGE = r"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1115"><title>WiFiDrop</title>
<style>
:root{--bg:#0f1115;--card:#181c23;--line:#252b35;--fg:#e9eef5;--mut:#8b96a5;--acc:#3b82f6;--ok:#22c55e;--bad:#ef4444}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:12px 12px 64px}
.top{display:flex;align-items:center;gap:9px;padding:6px 2px 12px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.title{font-weight:600;font-size:16px}
.pill{font-size:11.5px;color:#a9d8b8;background:#13301d;border:1px solid #1e4a2e;padding:2px 8px;border-radius:99px;white-space:nowrap}
.num{font-size:12px;color:#cfe0ff;background:#152238;border:1px solid #24344f;padding:2px 8px;border-radius:99px;font-variant-numeric:tabular-nums}
.spacer{flex:1}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
textarea{width:100%;min-height:96px;background:#0c1015;color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:11px;font:14px/1.5 inherit;resize:vertical;outline:none}
textarea:focus{border-color:var(--acc)}
.row{display:flex;gap:8px;margin-top:9px;flex-wrap:wrap}
button{flex:1;min-width:104px;border:0;border-radius:10px;padding:11px;background:var(--acc);color:#fff;font:600 14px inherit;cursor:pointer}
button.ghost{background:#242c38;color:var(--fg)}
button.tiny{flex:none;min-width:0;padding:6px 11px;font-size:12.5px;font-weight:500}
button:disabled{opacity:.45}
.hint{color:var(--mut);font-size:12.5px;margin:8px 2px 0}
.item{border-top:1px solid var(--line);padding:11px 2px}
.item:first-child{border-top:0}
.ih{display:flex;justify-content:space-between;gap:8px;color:var(--mut);font-size:11.5px}
.msg{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:14px;max-height:260px;overflow:auto;
  -webkit-user-select:text;user-select:text;-webkit-touch-callout:default;cursor:text;
  padding:8px;background:#0c1015;border:1px solid var(--line);border-radius:8px}
.msg::selection{background:#2b4f7a}
.msg::-moz-selection{background:#2b4f7a}
.mini{display:flex;gap:6px;margin-top:8px}
img.thumb{max-width:100%;border-radius:10px;margin-top:8px;display:block}
.filebox{display:flex;align-items:center;gap:9px;margin-top:8px;padding:9px;background:#0c1015;border:1px solid var(--line);border-radius:10px}
.filebox a{color:var(--acc);text-decoration:none;font-weight:600;word-break:break-all}
.bar{height:6px;background:#0c1015;border-radius:99px;overflow:hidden;margin-top:8px;border:1px solid var(--line)}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#60a5fa);width:0;transition:width .2s}
.prog{margin-top:8px;padding:9px;background:#0c1015;border:1px solid var(--line);border-radius:10px}
.prog .t{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
.pair{max-width:430px;margin:0 auto;padding:34px 18px}
.pair h2{font-size:19px;margin:0 0 8px}
.pair p{color:var(--mut);font-size:14px;margin:0 0 16px}
.dev{display:flex;align-items:center;gap:10px;padding:11px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;background:#0c1015;cursor:pointer}
.dev b{font-weight:600;font-size:14px}
.dev .no{color:#cfe0ff;font-variant-numeric:tabular-nums;font-weight:700}
.status{position:fixed;left:0;right:0;bottom:0;background:#181c23e6;backdrop-filter:blur(10px);border-top:1px solid var(--line);padding:9px 12px;text-align:center;color:var(--mut);font-size:12.5px}
.hide{display:none!important}
</style></head><body><div id="app"></div><script>
const TOK = location.pathname.split('/')[1] || '';
const BASE = '/' + TOK;
const $ = (s,r=document)=>r.querySelector(s);
const isPhone = /Android|iPhone|iPad|iPod|Mobile|HarmonyOS/i.test(navigator.userAgent) || Math.min(innerWidth,innerHeight) < 620;
let me = null, lastTs = 0, pollT = null, progT = null;

function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function kb(n){ n=n||0; return n>=1048576?(n/1048576).toFixed(1)+' MB':(n>=1024?(n/1024).toFixed(1)+' KB':n+' B'); }
function hhmm(t){ return new Date(t*1000).toTimeString().slice(0,5); }
function st(t){ const e=$('#stat'); if(e) e.textContent=t; }
function pad(n){ return ('000'+n).slice(-4); }

async function j(path,opt){
  const ctl=new AbortController(); const tm=setTimeout(()=>ctl.abort(),20000);
  const h=Object.assign({},(opt&&opt.headers)||{});
  if(me&&me.id) h['X-Device-Id']=me.id;
  try{
    const r=await fetch(BASE+path,Object.assign({signal:ctl.signal,credentials:'same-origin'},opt||{},{headers:h}));
    let d=null; try{d=await r.json();}catch(e){}
    return {status:r.status,ok:r.ok,data:d};
  } finally { clearTimeout(tm); }
}

async function showPairing(devs){
  document.getElementById('app').innerHTML=`<div class="pair">
    <h2>连接 WiFiDrop</h2>
    <p>第一次连接，点一个设备即可。之后浏览器会记住，不用再选。</p>
    <div id="devs">${devs.map(d=>`<div class="dev" data-id="${esc(d.id)}">
      <span class="no">#${pad(d.no)}</span><b>${esc(d.label||'设备')}</b>
      <span style="margin-left:auto;color:#8b96a5;font-size:12px">${d.count} 条</span></div>`).join('')
      ||'<div class="hint">还没有其它设备</div>'}</div>
    <div class="row" style="margin-top:16px">
      <button id="newdev">我是新设备</button></div>
    <div class="hint" style="margin-top:14px">电脑端窗口里也能看到每台设备的编号。</div></div>`;
  const first=$('#devs .dev');
  if(first) first.onclick=()=>pair(first.dataset.id);
  document.querySelectorAll('#devs .dev').forEach(el=>{ el.onclick=()=>pair(el.dataset.id); });
  $('#newdev').onclick=()=>pair(null);
}
async function pair(id){
  const body = id?{deviceId:id}:{newDevice:true};
  const r=await fetch(BASE+'/pair',{method:'POST',credentials:'same-origin',
    headers:Object.assign({'Content-Type':'application/json'},id?{'X-Device-Id':id}:{}),
    body:JSON.stringify(body)});
  if(r.ok){ const d=await r.json(); try{localStorage.setItem('wd_dev',d.id);}catch(e){} location.reload(); }
  else alert('连接失败 '+r.status);
}

function shell(){
  document.getElementById('app').innerHTML=`<div class="wrap">
  <div class="top"><span class="dot"></span><span class="title">WiFiDrop</span>
    <span class="pill">${isPhone?'手机':'电脑'}</span>
    <span class="num" id="myNo">#${pad(me.no)}</span>
    <span class="spacer"></span><span class="pill" id="stat">已连接</span></div>
  <div class="card">
    <textarea id="txt" placeholder="${isPhone?'粘贴文字，点发送，电脑上 Ctrl+V 就能粘出来…':'输入文字，点发送，手机立刻收到…'}"></textarea>
    <div class="row"><button id="send">发送</button>
      <button class="ghost" id="sendclip">发送并放进电脑剪贴板</button>
    </div>
    <div class="row">
      <button class="ghost" id="grab">取电脑剪贴板</button>
      <button class="ghost" id="gcl">读手机剪贴板</button>
      <button class="ghost tiny" id="qrbtn">二维码</button></div>
    <div id="qrbox" class="hide" style="text-align:center;padding-top:10px">
      <img id="qrimg" style="width:150px;height:150px;background:#fff;border-radius:10px;padding:6px">
      <div class="hint" id="qurl"></div></div>
  </div>
  <div class="card">
    <div class="row" style="margin-top:0">
      <button class="ghost" id="pick">上传图片 / 文件</button>
      <input type="file" id="file" multiple class="hide"></div>
    <div id="upProg" class="hide"></div>
    <div class="hint">上传后存到电脑的「文件」文件夹（只有一层，文件都在里面）</div>
  </div>
  <div class="card">
    <div style="display:flex;align-items:center;gap:8px;padding:0 2px 10px">
      <b style="font-size:13.5px">历史记录</b>
      <span class="hint" style="margin:0">文字 200 条 / 文件 3 天</span>
      <span class="spacer"></span>
      <button class="ghost tiny" id="clear">清空</button></div>
    <div id="list"></div></div>
  </div>
  <div class="status" id="statbar">本机编号 #${pad(me.pc)} · 数据不出局域网</div>`;

  $('#send').onclick=()=>sendText(false);
  $('#sendclip').onclick=()=>sendText(true);
  $('#grab').onclick=grabClip;
  $('#gcl').onclick=grabLocal;
  $('#qrbtn').onclick=toggleQR;
  $('#pick').onclick=()=>$('#file').click();
  $('#file').onchange=upload;
  $('#clear').onclick=async()=>{ if(!confirm('清空本设备全部记录？'))return; await j('/api/clear',{method:'POST'}); refresh(); };
  $('#txt').addEventListener('keydown',e=>{ if((e.ctrlKey||e.metaKey)&&e.key==='Enter') sendText(); });
  ['dragover','drop'].forEach(t=>document.addEventListener(t,ev=>{
    ev.preventDefault();
    if(t==='drop'&&ev.dataTransfer.files.length) upload({target:{files:ev.dataTransfer.files}});
  }));
}

function render(items){
  const L=$('#list'); if(!L) return;
  if(!items.length){ L.innerHTML='<div class="hint" style="padding:10px 2px 16px">还没有内容</div>'; return; }
  L.innerHTML=items.map(it=>{
    const isText = it.kind==='text';
    const mine = (it.frompc||'').indexOf('电脑')>=0 || (it.frompc||'').indexOf('本机')>=0;
    const tag = isText?(mine?'电脑发来':'我发出的文字'):(mine?'电脑发来的文件':'文件');
    const dev = it.devno?`<span style="color:#7f8fa8">#${pad(it.devno)}</span> `:'';
    let body='';
    if(isText){
      body=`<div class="msg" contenteditable="false">${esc(it.text)}</div><div class="mini">
        <button class="ghost tiny" data-a="c">复制</button>
        <button class="ghost tiny" data-a="p">发到电脑剪贴板</button></div>`;
    } else {
      const img=/\.(png|jpe?g|gif|webp|bmp)$/i.test(it.fname||'');
      const url = BASE+'/dl/'+it.id;
      const dlUrl = url+'?dl=1';
      body=(img?`<a href="${url}" target="_blank"><img class="thumb" loading="lazy" src="${url}"></a>`:'')
        +`<div class="filebox">
            <a href="${url}" target="_blank" class="fname">${esc(it.fname||'文件')}</a>
            <span class="hint" style="margin-left:auto">${kb(it.size)}</span>
          </div>
          <div class="mini">
            <button class="ghost tiny" data-a="open" data-url="${url}">打开</button>
            <button class="ghost tiny" data-a="save" data-url="${dlUrl}">保存到手机</button>
            <button class="ghost tiny" data-a="share" data-url="${url}">分享</button>
          </div>
          <div class="hint" style="margin-top:4px">保存后去手机「文件管理 → 下载」找</div>`;
    }
    return `<div class="item" data-id="${it.id}" data-name="${esc(it.fname||'')}"><div class="ih"><span>${dev}${tag}</span><span>${hhmm(it.ts)}</span></div>${body}</div>`;
  }).join('');
  L.querySelectorAll('button[data-a]').forEach(b=>{
    b.onclick=()=>{
      const item=b.closest('.item');
      const id=item.dataset.id;
      const it=items.find(x=>x.id===id)||{};
      const act=b.dataset.a;
      if(act==='c') return copyLocal(it.text,b);
      if(act==='p') return toClip(it.text,b);
      const url=b.dataset.url;
      if(act==='open') window.open(url,'_blank');
      else if(act==='save') downloadFile(url, it.fname||'file');
      else if(act==='share') shareFile(url, it.fname||'file', it.size||0);
    };
  });
}

// 保存到手机：用 a[download] 触发浏览器下载（安卓 Chrome 会存到「下载」目录）
function downloadFile(url, name){
  const a=document.createElement('a');
  a.href=url; a.download=name;
  document.body.appendChild(a); a.click(); a.remove();
  st('已开始下载，见手机「下载」文件夹');
  setTimeout(()=>st('已连接'), 2600);
}

// 分享：优先用系统分享面板（微信/相册/文件）
async function shareFile(url, name, size){
  try{
    const r = await fetch(url);
    const blob = await r.blob();
    const file = new File([blob], name, {type: blob.type || 'application/octet-stream'});
    if(navigator.canShare && navigator.canShare({files:[file]})){
      await navigator.share({files:[file], title: name});
      return;
    }
    st('此浏览器不支持分享，已改为下载');
    downloadFile(url, name);
  }catch(e){
    st('分享失败，已改为下载');
    downloadFile(url, name);
  }
}

async function copyLocal(t,b){
  const o=b.textContent;
  try{ await navigator.clipboard.writeText(t); }
  catch(e){ const ta=document.createElement('textarea'); ta.value=t; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select(); try{document.execCommand('copy');}catch(_){}; ta.remove(); }
  b.textContent='已复制'; setTimeout(()=>b.textContent=o,1200);
}
async function toClip(t,b){
  const o=b.textContent; b.textContent='发送中…';
  const r=await j('/api/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,toClipboard:true})});
  b.textContent=r.ok?'已到电脑剪贴板':('失败 '+r.status);
  setTimeout(()=>b.textContent=o,1500);
}
async function sendText(toClip){
  const el=$('#txt'), text=el.value.trim();
  if(!text){ st('先输入内容'); setTimeout(()=>st('已连接'),1500); return; }
  const btn = toClip ? $('#sendclip') : $('#send');
  btn.disabled=true; st(toClip?'发送并写入剪贴板…':'发送中…');
  const r=await j('/api/text',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text, toClipboard: !!toClip})});
  btn.disabled=false;
  if(r.ok){ el.value=''; st(toClip?'已发送并放进电脑剪贴板':'已发送'); }
  else st('失败 '+r.status);
  refresh(); setTimeout(()=>st('已连接'),2400);
}
async function grabClip(){
  st('读取中…'); const r=await j('/api/clip');
  if(!r.ok) st('读取失败 '+r.status);
  else if(r.data&&r.data.text){ $('#txt').value=r.data.text; st('已取到'); }
  else st('电脑剪贴板为空');
  setTimeout(()=>st('已连接'),1800);
}
async function grabLocal(){
  try{ const t=await navigator.clipboard.readText(); if(t){$('#txt').value=t; st('已粘贴');} else st('手机剪贴板为空'); }
  catch(e){ st('请长按输入框粘贴'); }
  setTimeout(()=>st('已连接'),1800);
}

function upload(ev){
  const fs=[...(ev.target.files||[])];
  if(!fs.length) return;
  const box=$('#upProg'); box.classList.remove('hide');
  let i=0;
  const next=()=>{
    if(i>=fs.length){ box.classList.add('hide'); $('#file').value=''; st('上传完成'); refresh(); setTimeout(()=>st('已连接'),2200); return; }
    const f=fs[i];
    if(f.size >= 4*1024*1024) uploadParallel(f, ()=>{ i++; next(); });
    else uploadSingle(f, ()=>{ i++; next(); });
  };
  next();
}

function progUI(name){
  const box=$('#upProg');
  box.innerHTML=`<div class="prog"><div class="t"><span>${esc(name)}</span>
    <span><span id="pp">0%</span> <a href="#" id="pcancel"
      style="color:#ef8f8f;margin-left:10px;text-decoration:none;font-size:12px">中止</a></span></div>
    <div class="bar"><i id="pb"></i></div>
    <div class="t" style="margin-top:6px"><span id="ps">--</span><span id="pe">--</span></div></div>`;
  const api = {
    set(loaded, total, t0){
      const p = total? loaded/total*100 : 0;
      const sec = (Date.now()-t0)/1000, sp = loaded/Math.max(.25, sec);
      const pb=$('#pb'), pp=$('#pp'), ps=$('#ps'), pe=$('#pe');
      if(pb) pb.style.width=p.toFixed(1)+'%';
      if(pp) pp.textContent=p.toFixed(0)+'%';
      if(ps) ps.textContent=(sp/1048576).toFixed(1)+' MB/s';
      if(pe) pe.textContent='剩余 '+Math.max(0,Math.round((total-loaded)/Math.max(1,sp)))+' 秒';
    },
    setCancel(fn){
      const c = $('#pcancel');
      if(c) c.onclick = ev => { ev.preventDefault(); fn(); };
    }
  };
  api.setCancel(()=>{ st('已中止'); });
  return api;
}

function uploadSingle(f, done){
  const fd=new FormData(); fd.append('f',f,f.name);
  const ui=progUI(f.name), t0=Date.now();
  const xhr=new XMLHttpRequest();
  xhr.upload.onprogress=e=>{ if(e.lengthComputable) ui.set(e.loaded, e.total, t0); };
  xhr.onload=()=>done();
  xhr.onerror=()=>{ st('上传失败'); done(); };
  xhr.open('POST', BASE+'/api/upload');
  if(me&&me.id) xhr.setRequestHeader('X-Device-Id',me.id);
  xhr.send(fd);
}

// 大文件：切成 N 片并行传，多条 TCP 才能填满 WiFi 带宽
function uploadParallel(f, done){
  const N = f.size >= 64*1024*1024 ? 8 : 4;
  const ui = progUI(f.name), t0 = Date.now();
  const xhrs = [];
  const ctl = {aborted:false};
  ui.setCancel(()=>{
    ctl.aborted = true;
    xhrs.forEach(x=>{ try{ x.abort(); }catch(e){} });
    fetch(BASE+'/api/p2p-abort', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json', 'X-Device-Id': me?me.id:''},
        body: JSON.stringify({id: upid})}).catch(()=>{});
    st('已中止上传');
    setTimeout(()=>st('已连接'), 1800);
  });
  let upid = '';
  const body = JSON.stringify({name: f.name, size: f.size});
  fetch(BASE+'/api/p2p-begin', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-Device-Id': me?me.id:''}, body})
    .then(r=>r.json())
    .then(meta=>{
      if(!meta || !meta.id){ uploadSingle(f, done); return; }
      upid = meta.id;
      const size=f.size, per=Math.ceil(size/N);
      let loaded=0, finished=0, failed=false;
      const prog=new Array(N).fill(0);
      const bump=(idx,val)=>{
        prog[idx]=val;
        loaded=prog.reduce((a,b)=>a+b,0);
        ui.set(loaded, size, t0);
      };
      for(let k=0;k<N;k++){
        const start=k*per, end=Math.min(size, start+per);
        if(start>=end){ finished++; continue; }
        const slice=f.slice(start, end);
        const xhr=new XMLHttpRequest();
        xhrs.push(xhr);
        xhr.upload.onprogress=e=>{ if(e.lengthComputable) bump(k, e.loaded); };
        xhr.onload=()=>{
          if(xhr.status!==200) failed=true;
          bump(k, end-start);
          finished++;
          if(finished>=N){ finalize(); }
        };
        xhr.onerror=()=>{ if(!ctl.aborted) failed=true; finished++; if(finished>=N) finalize(); };
        xhr.onabort=()=>{ finished++; };
        xhr.open('POST', BASE+'/api/p2p-part?id='+meta.id+'&offset='+start);
        if(me&&me.id) xhr.setRequestHeader('X-Device-Id', me.id);
        xhr.setRequestHeader('Content-Type','application/octet-stream');
        xhr.send(slice);
      }
      function finalize(){
        if(ctl.aborted) return;
        if(failed){ st('并行上传失败，改单连重试'); uploadSingle(f, done); return; }
        fetch(BASE+'/api/p2p-end', {method:'POST', credentials:'same-origin',
            headers:{'Content-Type':'application/json', 'X-Device-Id': me?me.id:''},
            body: JSON.stringify({id: meta.id})})
          .then(()=>{ ui.set(size, size, t0); done(); })
          .catch(()=>done());
      }
    })
    .catch(()=>uploadSingle(f, done));
}

async function toggleQR(){
  const box=$('#qrbox');
  if(!box.classList.contains('hide')){ box.classList.add('hide'); return; }
  $('#qrimg').src=BASE+'/qr.png?t='+Date.now();
  $('#qurl').textContent=location.origin+BASE+'/';
  box.classList.remove('hide');
}

async function refresh(){
  try{
    const r=await j('/api/list');
    if(r.status===401){ location.reload(); return; }
    if(!r.ok||!r.data) return;
    render(r.data.items||[]);
    // 水位用「服务器当前时间」，不能用最新记录的 ts：
    // 否则时钟偏差 / 同秒多条会导致后面的消息永远轮询不到（文字丢失的主因）
    if(r.data.now) lastTs = Math.max(lastTs, r.data.now - 0.001);
  }catch(e){}
}
async function poll(){
  try{
    const r=await j('/api/since?ts='+lastTs);
    if(r.ok&&r.data){
      if(r.data.now) lastTs = Math.max(lastTs, r.data.now - 0.001);
      if(r.data.items&&r.data.items.length){
        r.data.items.forEach(it=>{ if(it.ts>lastTs) lastTs=it.ts; });
        st('收到 '+r.data.items.length+' 条新内容'); refresh();
        setTimeout(()=>st('已连接'),2400);
      }
    }
  }catch(e){}
}

async function boot(){
  let saved=''; try{saved=localStorage.getItem('wd_dev')||'';}catch(e){}
  const h=saved?{'X-Device-Id':saved}:{};
  let r;
  try{ const x=await fetch(BASE+'/api/me',{credentials:'same-origin',headers:h});
       r={ok:x.ok,status:x.status,data:await x.json().catch(()=>null)}; }
  catch(e){ r={ok:false,status:0,data:null}; }
  if(r.status===401||r.status===404){
    const d=await j('/api/devices'); showPairing((d.data&&d.data.devices)||[]); return;
  }
  if(!r.ok||!r.data){ document.getElementById('app').innerHTML=
    '<div class="pair"><h2>连接中断</h2><p>电脑端的 WiFiDrop 可能已经关闭。</p></div>'; return; }
  me=r.data;
  try{ localStorage.setItem('wd_dev',me.id); }catch(e){}
  shell(); refresh(); pollT=setInterval(poll,2000);
}
boot();
</script></body></html>
"""
