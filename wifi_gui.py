#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
传输助手 桌面界面（tkinter，暗色主题）。

* 无黑色控制台窗口（打包时 console=False）
* 顶部：本机编号、访问地址、二维码
* 中部：进行中的传输（百分比 / 速度 / 剩余时间）
* 下部：已收到的文件（双击打开、右键删除、「打开文件夹」）
* 右侧：局域网其它电脑（自动发现），选中后可发大文件
"""

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import wifi_core as core

# ---------------------------------------------------------------- 配色
BG = "#0f1115"
CARD = "#181c23"
LINE = "#252b35"
FG = "#e9eef5"
MUT = "#8b96a5"
ACC = "#3b82f6"
OK = "#22c55e"
WARN = "#f59e0b"
BAD = "#ef4444"


def _inside(parent: str, child: str) -> bool:
    """判断 child 是否在 parent 目录内（用于删除前做安全校验）。"""
    try:
        p = os.path.abspath(parent).rstrip("\\/").lower()
        c = os.path.abspath(child).lower()
        return c.startswith(p + os.sep) or c == p
    except Exception:
        return False


def pad4(n):
    try:
        return "%04d" % int(n)
    except Exception:
        return "----"


def enable_dpi_awareness():
    """高 DPI 屏幕（125%/150% 缩放）下必须声明 DPI 感知，否则窗口会被放大到超出屏幕。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        # 2 = PER_MONITOR_AWARE_V2（Win8.1+），失败逐级回退
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def fit_size(win, want_w, want_h):
    """按屏幕可用区裁剪窗口尺寸，保证在 1366x768 这类小屏上也能完整显示。"""
    try:
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
    except Exception:
        sw, sh = 1366, 768
    w = min(want_w, max(760, int(sw * 0.94)))
    h = min(want_h, max(560, int(sh * 0.90)))
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))
    return w, h


def human_size(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0
    return "%.1f TB" % n


def human_speed(n):
    return human_size(n) + "/s"


def human_eta(sec):
    if sec is None:
        return "--"
    s = int(sec)
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return "%d 分 %d 秒" % (s // 60, s % 60)
    return "%d 时 %d 分" % (s // 3600, (s % 3600) // 60)


def _short_path(p, keep=3):
    """只保留末尾几段，避免界面上显示超长路径。"""
    parts = [x for x in str(p).replace("/", "\\").split("\\") if x]
    if len(parts) <= keep:
        return p
    return "…\\" + "\\".join(parts[-keep:])


class App:
    def __init__(self, root):
        self.root = root
        self.httpd = None
        self.port = 0
        self.already = False
        self.qr_img = None
        self._last_sig = None

        root.title("传输助手 v" + core.APP_VERSION)
        root.configure(bg=BG)
        fit_size(root, 1180, 800)
        root.minsize(820, 600)

        self._style()
        self._build()

        ok = self._start()
        if ok:
            self._refresh_qr()
            self._refresh_targets()
            self._load_files()
        self.root.after(200, self._dark_titlebar)
        self.root.after(300, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 样式
    def _dark_titlebar(self, tries=3):
        """让 Windows 标题栏也变深色。必须在窗口映射之后再调用才生效。"""
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            val = ctypes.c_int(1)
            ok = False
            for attr in (20, 19):
                hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd), ctypes.c_uint(attr),
                    ctypes.byref(val), ctypes.sizeof(val))
                if hr == 0:
                    ok = True
            # 顺便把标题栏背景刷一次，强制重绘
            if ok:
                ctypes.windll.user32.SetWindowPos(
                    wintypes.HWND(hwnd), None, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004)
                return True
            if tries > 0:
                self.root.after(120, lambda: self._dark_titlebar(tries - 1))
        except Exception:
            pass
        return False

    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=CARD,
                     bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=CARD, foreground=FG)
        st.configure("Mut.TLabel", background=CARD, foreground=MUT)
        st.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 13, "bold"))
        st.configure("Big.TLabel", background=CARD, foreground=ACC,
                     font=("Consolas", 18, "bold"))
        st.configure("No.TLabel", background=CARD, foreground="#cfe0ff",
                     font=("Consolas", 18, "bold"))
        st.configure("TButton", background="#242c38", foreground=FG, borderwidth=0,
                     focusthickness=0, padding=(10, 6))
        st.map("TButton",
               background=[("active", "#2e3846"), ("disabled", "#1b2028")],
               foreground=[("disabled", MUT)])
        st.configure("Accent.TButton", background=ACC, foreground="#ffffff")
        st.map("Accent.TButton", background=[("active", "#2f6fd0")])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                     bordercolor=LINE, rowheight=24, borderwidth=0)
        st.configure("Treeview.Heading", background="#1f262f", foreground=MUT,
                     borderwidth=0, relief="flat")
        st.map("Treeview", background=[("selected", "#24334a")])
        st.configure("TProgressbar", background=ACC, troughcolor="#0c1015",
                     bordercolor=LINE, lightcolor=ACC, darkcolor=ACC)
        # 下拉框走暗色（clam 主题下需要显式配 fieldbackground）
        st.configure("TCombobox", fieldbackground="#0c1015", background="#242c38",
                     foreground=FG, arrowcolor=FG, bordercolor=LINE,
                     selectbackground="#24334a", selectforeground=FG, padding=(6, 4))
        st.map("TCombobox",
               fieldbackground=[("readonly", "#0c1015"), ("focus", "#0c1015")],
               foreground=[("readonly", FG)],
               background=[("active", "#2e3846")])
        self.root.option_add("*TCombobox*Listbox.background", "#0c1015")
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#24334a")
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG)

    # ------------------------------------------------------------ 布局
    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=2)   # 中部
        outer.rowconfigure(2, weight=3)   # 底部文件列表

        # ---- 顶栏：本机编号 / 地址 / 二维码
        head = ttk.Frame(outer, style="Card.TFrame", padding=12)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)

        left = ttk.Frame(head, style="Card.TFrame")
        left.grid(row=0, column=0, sticky="nsew")

        row1 = ttk.Frame(left, style="Card.TFrame")
        row1.pack(anchor="w")
        ttk.Label(row1, text="本机编号", style="Mut.TLabel").pack(side="left", pady=(3, 0))
        self.lbl_no = ttk.Label(row1, text="#----", style="No.TLabel")
        self.lbl_no.pack(side="left", padx=(8, 0))
        self.lbl_state = ttk.Label(row1, text="● 已启动", style="Card.TLabel",
                                   foreground=OK)
        self.lbl_state.pack(side="left", padx=(14, 0))

        ttk.Label(left, text="手机扫右侧二维码，或在浏览器打开：",
                  style="Mut.TLabel").pack(anchor="w", pady=(8, 2))
        self.lbl_url = tk.Entry(left, bg="#0c1015", fg=FG, insertbackground=FG,
                                relief="flat", font=("Consolas", 10))
        self.lbl_url.pack(fill="x", ipady=5)

        rowbtn = ttk.Frame(left, style="Card.TFrame")
        rowbtn.pack(anchor="w", pady=(8, 0))
        ttk.Button(rowbtn, text="复制地址", command=self._copy_url).pack(side="left")
        ttk.Button(rowbtn, text="打开文件夹", command=self._open_folder).pack(side="left", padx=6)
        ttk.Button(rowbtn, text="在浏览器打开", command=self._open_browser).pack(side="left")

        right = ttk.Frame(head, style="Card.TFrame")
        right.grid(row=0, column=1, sticky="ne", padx=(14, 0))
        # 不给固定宽高：让图片自己撑开（图片尺寸由 _refresh_qr 精确控制）
        self.qr_label = tk.Label(right, bg="white", bd=0)
        self.qr_label.pack()
        ttk.Label(right, text="手机扫码连接", style="Mut.TLabel").pack(pady=(3, 0))

        # ---- 中部左右分栏
        mid = ttk.Frame(outer)
        mid.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)

        leftc = ttk.Frame(mid, style="Card.TFrame", padding=10)
        leftc.grid(row=0, column=0, sticky="nsew")

        # 发文字
        tbar = ttk.Frame(leftc, style="Card.TFrame")
        tbar.pack(fill="x")
        ttk.Label(tbar, text="发文字", style="Card.TLabel",
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Label(tbar, text="发给：", style="Mut.TLabel").pack(side="left", padx=(10, 0))
        self.txt_target = ttk.Combobox(tbar, state="readonly", width=18,
                                       values=["所有设备"])
        self.txt_target.pack(side="left")
        self.txt_target.current(0)

        trow = ttk.Frame(leftc, style="Card.TFrame")
        trow.pack(fill="x", pady=(6, 0))
        self.txt_box = tk.Text(trow, height=2, bg="#0c1015", fg=FG, insertbackground=FG,
                               relief="flat", font=("Segoe UI", 10), wrap="word",
                               padx=8, pady=6)
        self.txt_box.pack(side="left", fill="both", expand=True)
        tbtns = ttk.Frame(trow, style="Card.TFrame")
        tbtns.pack(side="right", padx=(6, 0))
        ttk.Button(tbtns, text="发送", style="Accent.TButton",
                   command=self._send_text).pack(fill="x")
        ttk.Button(tbtns, text="粘贴", command=self._paste_text).pack(fill="x", pady=(4, 0))
        # Ctrl+Enter 发送
        self.txt_box.bind("<Control-Return>", lambda e: (self._send_text(), "break")[1])

        # 传输进度
        hdr = ttk.Frame(leftc, style="Card.TFrame")
        hdr.pack(fill="x", pady=(10, 0))
        ttk.Label(hdr, text="传输进度", style="Card.TLabel",
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        self.lbl_active = ttk.Label(hdr, text="", style="Mut.TLabel")
        self.lbl_active.pack(side="left", padx=(8, 0))
        ttk.Button(hdr, text="全部中止", command=self._cancel_all).pack(side="right")
        ttk.Button(hdr, text="清除已完成", command=self._clear_done).pack(side="right", padx=(0, 6))
        ttk.Button(hdr, text="继续未完成", command=self._resume_all).pack(
            side="right", padx=(0, 6))

        self.prog_canvas = tk.Canvas(leftc, bg=CARD, highlightthickness=0, height=100)
        self.prog_canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.prog_canvas.bind("<Configure>", lambda e: self._draw_progress())
        self.prog_canvas.bind("<Button-1>", self._on_progress_click)
        self.prog_canvas.bind("<Button-3>", self._on_progress_rightclick)

        # ---- 底部：文件列表
        bot = ttk.Frame(outer, style="Card.TFrame", padding=10)
        bot.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        h3 = ttk.Frame(bot, style="Card.TFrame")
        h3.pack(fill="x")
        ttk.Label(h3, text="记录", style="Card.TLabel",
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(h3, text="打开文件夹", command=self._open_folder).pack(side="right")
        ttk.Button(h3, text="清除所有记录",
                   command=self._clear_all_records).pack(side="right", padx=(0, 6))
        ttk.Button(h3, text="发文件到手机",
                   command=self._send_file_to_phone).pack(side="right", padx=(0, 6))

        cols = ("no", "time", "name", "size", "from")
        self.tree = ttk.Treeview(bot, columns=cols, show="headings", height=5)
        for c, t, w, a in (("no", "设备", 64, "center"), ("time", "时间", 118, "center"),
                           ("name", "内容", 320, "w"), ("size", "大小", 78, "e"),
                           ("from", "来源", 130, "center")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c == "name"),
                             minwidth=50)
        self.tree.pack(fill="both", expand=True, pady=(6, 0))
        self.tree.bind("<Double-1>", self._open_selected_file)
        self.tree.bind("<Button-3>", self._file_menu)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # 选中记录后，这里显示全文 —— 可以直接用鼠标拖选、Ctrl+C 复制
        detail = ttk.Frame(bot, style="Card.TFrame")
        detail.pack(fill="x", pady=(6, 0))
        dhead = ttk.Frame(detail, style="Card.TFrame")
        dhead.pack(fill="x")
        ttk.Label(dhead, text="详细信息（可拖选复制）",
                  style="Mut.TLabel").pack(side="left")
        self._sel_all = False
        ttk.Button(dhead, text="全选", command=self._toggle_select_all).pack(side="right")
        self.detail_text = tk.Text(detail, height=3, bg="#0c1015", fg=FG,
                                   insertbackground=FG, relief="flat", wrap="word",
                                   font=("Segoe UI", 10), padx=8, pady=6)
        self.detail_text.pack(fill="x", pady=(3, 0))
        # 只读但允许选中/复制
        self.detail_text.configure(state="disabled")
        self.detail_text.bind("<Control-c>", lambda e: (self.detail_text.event_generate("<<Copy>>"), "break")[1])
        self.detail_text.bind("<Control-a>", lambda e: (self._toggle_select_all(), "break")[1])
        self.detail_text.bind("<Button-1>", lambda e: self._reset_sel_flag())

        # 右键只保留三项：打开 / 打开文件所在位置 / 删除
        self._menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=FG,
                             activebackground="#24334a", activeforeground=FG)
        self._menu.add_command(label="打开", command=self._open_selected_file)
        self._menu.add_command(label="打开文件所在位置", command=self._reveal_selected)
        self._menu.add_separator()
        self._menu.add_command(label="删除", command=self._delete_selected)

        self.files = []

    # ------------------------------------------------------------ 启动
    def _start(self):
        try:
            self.httpd, self.port, self.already = core.start_server()
        except Exception as e:
            messagebox.showerror("启动失败", "无法启动服务：\n\n%s" % e)
            self.root.after(200, self.root.destroy)
            return False

        self.lbl_no.config(text="#" + pad4(core.PC_ID))
        url = "http://%s:%d/%s/" % (core.HOST_IP, self.port, core.TOKEN)
        self.url = url
        self.lbl_url.delete(0, "end")
        self.lbl_url.insert(0, url)

        if self.already:
            self.lbl_state.config(text="● 已在运行", foreground=WARN)
            self.lbl_url.config(state="readonly")
        else:
            core.Handler.ui_ctx["hook"] = self._on_server_event
        return True

    def _refresh_qr(self):
        """二维码按目标像素精确生成，label 尺寸跟着图片走 —— 绝不裁剪，否则扫不出。"""
        target = 152
        png = core.qr_png(self.url, target_px=target)
        if not png:
            self.qr_label.config(text="二维码不可用", fg=MUT, font=("Segoe UI", 9))
            return
        try:
            img = tk.PhotoImage(data=png)   # Tk 8.6 原生支持 PNG
            self.qr_img = img
            w, h = img.width(), img.height()
            # 关键：让 label 正好等于图片大小（之前写死 126 会把 287px 的图裁掉）
            self.qr_label.config(image=img, width=w, height=h, bg="white")
        except Exception as e:
            self.qr_label.config(text="二维码不可用\n(%s)" % type(e).__name__,
                                 fg=MUT, font=("Segoe UI", 8))

    # ------------------------------------------------------------ 事件
    def _on_server_event(self, kind, item):
        # 服务端线程 → 主线程
        self.root.after(0, self._load_files)

    def _copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self.lbl_state.config(text="● 地址已复制", foreground=OK)
        self.root.after(1800, lambda: self.lbl_state.config(
            text="● 已启动" if not self.already else "● 已在运行",
            foreground=OK if not self.already else WARN))

    def _open_folder(self):
        try:
            os.startfile(core.files_dir())
        except Exception as e:
            messagebox.showwarning("打不开", str(e))

    def _open_browser(self):
        import webbrowser
        webbrowser.open(self.url)

    def _open_selected_file(self, ev=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if not (0 <= idx < len(self.files)):
            return
        it = self.files[idx]
        if it.get("kind") == "text":
            # 文字：双击复制到剪贴板
            t = it.get("text") or ""
            if t:
                self.root.clipboard_clear()
                self.root.clipboard_append(t)
                try:
                    core.clipboard_set(t)
                except Exception:
                    pass
                self._flash("文字已复制到剪贴板")
            return
        path = core.resolve_stored(it.get("stored", ""))
        if path and os.path.exists(path):
            try:
                os.startfile(path)
            except Exception as e:
                messagebox.showwarning("打不开", str(e))
        elif it.get("stored"):
            # 文件记录但原文件已经不见了（比如桌面那个被删了/挪了）
            self._open_folder()
            self._flash("原文件已不存在：%s" % _short_path(path or "", 2))
        else:
            # 文字记录没有独立文件，直接打开「文件」文件夹
            self._open_folder()
            self._flash("该记录是文字，没有对应文件")

    def _send_file_to_phone(self):
        """电脑 → 手机 发文件。"""
        name = self.txt_target.get() or "所有设备"
        target = getattr(self, "_targets", {}).get(name, "all")
        path = filedialog.askopenfilename(title="选择要发到手机的文件")
        if not path:
            return
        size = os.path.getsize(path)
        # 记下源文件路径，供「中止并删除源文件」使用
        self._src_paths = getattr(self, "_src_paths", {})
        self._src_paths[os.path.basename(path)] = path

        def work():
            res = core.push_file([target], path)
            okn = sum(1 for r in res.get("results", []) if r.get("ok"))
            if okn:
                msg = "已发送到 %s（%d 台）" % (name, okn)
            elif res.get("error"):
                msg = res["error"]
            else:
                msg = "发送失败，看 error.log"
            self.root.after(0, lambda: self._flash(msg))
            self.root.after(0, self._load_files)
        threading.Thread(target=work, daemon=True).start()
        self._flash("正在发送 %s（%s）…" % (os.path.basename(path), human_size(size)))

    def _copy_record_text(self):
        """把选中记录的文字内容复制到剪贴板。"""
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if not (0 <= idx < len(self.files)):
            return
        it = self.files[idx]
        if it.get("kind") == "text":
            t = it.get("text") or ""
            if t:
                self.root.clipboard_clear()
                self.root.clipboard_append(t)
                self._flash("文字已复制")
        else:
            self._copy_path()

    def _on_tree_select(self, ev=None):
        """选中列表某行 → 在下方文本框显示全文，方便拖选复制。"""
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if not (0 <= idx < len(self.files)):
            return
        it = self.files[idx]
        if it.get("kind") == "text":
            content = it.get("text") or ""
        else:
            stored = it.get("stored", "")
            p = core.resolve_stored(stored) if stored else core.files_dir()
            content = "文件：%s\n大小：%s\n位置：%s" % (
                it.get("fname", ""), human_size(it.get("size", 0)), p)
            if core.stored_is_ext(stored):
                content += "\n（原文件，未复制副本）"
            if stored and not os.path.exists(p):
                content += "\n⚠ 文件已不存在（可能被移动或删除了）"
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", content)
        self.detail_text.configure(state="disabled")

    def _reset_sel_flag(self, ev=None):
        """用户手动点进文本框 → 认为"不是全选状态"。"""
        self._sel_all = False

    def _toggle_select_all(self):
        """全选 / 取消全选（点一次全选，再点一次取消）。"""
        self.detail_text.configure(state="normal")
        try:
            self.detail_text.tag_remove("sel", "1.0", "end")
            if not getattr(self, "_sel_all", False):
                self.detail_text.tag_add("sel", "1.0", "end-1c")
                self._sel_all = True
                self._flash("已全选，按 Ctrl+C 复制")
            else:
                self._sel_all = False
                self._flash("已取消全选")
            # 让选择区高亮可见
            self.detail_text.focus_set()
        finally:
            self.detail_text.configure(state="disabled")

    def _select_all_detail(self, ev=None):
        self._toggle_select_all()
        return "break"

    def _copy_path(self):
        """复制选中文件的完整路径（方便贴到资源管理器）。"""
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if not (0 <= idx < len(self.files)):
            return
        it = self.files[idx]
        if it.get("stored"):
            p = core.resolve_stored(it["stored"])
        else:
            p = core.files_dir()
        self.root.clipboard_clear()
        self.root.clipboard_append(p)
        self._flash("路径已复制：%s" % _short_path(p, 2))

    def _reveal_selected(self):
        """打开文件所在位置（在资源管理器里选中该文件）。"""
        sel = self.tree.selection()
        if not sel:
            self._open_folder()
            return
        idx = self.tree.index(sel[0])
        if not (0 <= idx < len(self.files)):
            self._open_folder()
            return
        it = self.files[idx]
        stored = it.get("stored", "")
        if stored:
            path = core.resolve_stored(stored)
            if os.path.exists(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                return
            self._flash("原文件已不存在：%s" % _short_path(path, 2))
            return
        # 文字记录没有对应文件
        self._open_folder()
        self._flash("该记录是文字，没有对应文件")

    def _cur_record(self):
        """取当前选中的记录。

        用 tree item 的 iid（= 记录id@设备id）直接反查，
        **不再靠 index 推算** —— 列表每 2.4 秒会后台刷新一次，
        用 index 取会在刷新瞬间错位，导致"点了没反应"。
        """
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        by_iid = getattr(self, "_by_iid", {})
        if iid in by_iid:
            return by_iid[iid]
        if "@" in iid:
            rid, dev = iid.split("@", 1)
            for f in self.files:
                if f.get("id") == rid and f.get("dev") == dev:
                    return f
        return None

    def _delete_selected(self):
        it = self._cur_record()
        if not it:
            messagebox.showinfo("未选中", "请先在列表里选一条记录。")
            return
        stored = it.get("stored", "")
        kind = it.get("kind")
        label = it.get("fname") or (("文字：" + (it.get("text") or "")[:20])
                                    if kind == "text" else "记录")
        is_ext = core.stored_is_ext(stored)
        in_share = bool(stored) and not is_ext

        ask = "确定删除这条记录？\n\n%s" % label
        if in_share:
            ask += "\n\n（文件在程序文件夹里，会一并删除）"
        elif is_ext:
            ask += "\n\n（源文件在：%s）\n只删记录，不会动你的源文件。" % core.resolve_stored(stored)
        if not messagebox.askyesno("删除", ask):
            return

        try:
            core.db_delete(it.get("dev"), it.get("id"))
        except Exception as e:
            messagebox.showwarning("删除失败", "记录删不掉：%s" % e)
            return

        if in_share:
            path = core.resolve_stored(stored)
            if _inside(core.files_dir(), path):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    messagebox.showwarning("文件删不掉",
                                           "记录已删除，但文件被占用：\n%s" % e)
            else:
                self._flash("路径不在程序文件夹内，已跳过删文件")
        elif is_ext:
            self._flash("已删除记录（源文件未动）")
        else:
            self._flash("已删除记录")
        self._load_files()

    def _clear_all_records(self):
        """清除所有记录：清空数据库 + 删除「文件」文件夹里的文件。
        外部源文件（桌面等）一律不动。
        """
        n_rec = len(getattr(self, "files", []))
        fdir = core.files_dir()
        n_dir = len(os.listdir(fdir)) if os.path.isdir(fdir) else 0
        ask = ("确定清除所有记录？\n\n"
               "• 记录列表会全部清空（当前 %d 条）\n"
               "• 程序文件夹里的文件会一起删除（当前 %d 个）\n"
               "• 桌面等位置的源文件不会被动\n\n"
               "此操作不可撤销。" % (n_rec, n_dir))
        if not messagebox.askyesno("清除所有记录", ask):
            return
        try:
            core.clear_all_records()
        except Exception as e:
            messagebox.showwarning("清除失败", str(e))
            return
        try:
            self.detail_text.configure(state="normal")
            self.detail_text.delete("1.0", "end")
            self.detail_text.configure(state="disabled")
        except Exception:
            pass
        self._load_files()
        self._flash("已清除所有记录")

    def _file_menu(self, ev):
        row = self.tree.identify_row(ev.y)
        if row:
            self.tree.selection_set(row)
            try:
                self._menu.tk_popup(ev.x_root, ev.y_root)
            finally:
                self._menu.grab_release()

    def _resume_all(self):
        """继续所有未完成的传输（支持关软件后重开接着传）。"""
        lst = core.list_resumable()
        if not lst:
            self._flash("没有未完成的传输")
            return
        n = 0
        for t in lst:
            r = core.resume_task(t["tid"])
            if r.get("ok"):
                n += 1
        self._flash("已继续 %d 个传输" % n)
        self._draw_progress()

    def _clear_done(self):
        core.TRANSFERS.clear_done()
        self._draw_progress()

    # ------------------------------------------------------------ 发文字
    def _refresh_targets(self):
        """下拉框列出可发送的目标设备。"""
        try:
            devs = core.devices_list()
        except Exception:
            devs = []
        self._targets = {"所有设备": "all"}
        vals = ["所有设备"]
        for d in devs:
            label = "#%s %s" % (pad4(d.get("no", 0)), d.get("label") or "设备")
            if label in self._targets:
                label += " " + d["id"][:4]
            self._targets[label] = d["id"]
            vals.append(label)
        cur = self.txt_target.get()
        self.txt_target["values"] = vals
        if cur in vals:
            self.txt_target.set(cur)
        else:
            self.txt_target.current(0)

    def _send_text(self):
        text = self.txt_box.get("1.0", "end").strip()
        if not text:
            self._flash("先输入内容")
            return
        name = self.txt_target.get() or "所有设备"
        target = getattr(self, "_targets", {}).get(name, "all")
        try:
            got = core.push_text([target], text)
        except Exception as e:
            self._flash("发送失败：%s" % e)
            return
        self.txt_box.delete("1.0", "end")
        if got:
            self._flash("已发送（%d 条记录）" % len(got))
        else:
            self._flash("没有可发送的设备（先让手机连一次）")
        self._load_files()

    def _paste_text(self):
        try:
            t = core.clipboard_get_timed(1.0)
        except Exception:
            t = ""
        if t:
            # 覆盖还是追加？这里用「清空后填入」，符合直觉
            self.txt_box.delete("1.0", "end")
            self.txt_box.insert("1.0", t)
            self._flash("已粘贴剪贴板")
        else:
            self._flash("电脑剪贴板为空")

    # ------------------------------------------------------------ 中止
    def _cancel_all(self):
        n = core.TRANSFERS.cancel(None, "abort")
        self._flash("已中止 %d 个传输" % n if n else "没有进行中的传输")
        self._draw_progress()

    def _cancel_one(self, tid, mode="abort"):
        n = core.TRANSFERS.cancel(tid, mode)
        if n:
            txt = {"abort": "已中止", "pause": "已暂停", "delete": "已中止并删除源文件"}.get(
                mode, "已中止")
            self._flash(txt)
        self._draw_progress()

    def _resume_one(self, tid):
        """恢复：暂停中的「电脑→对方」任务走断点续传，其它走普通恢复。"""
        task = None
        try:
            task = core.RESUME_TASKS.get(tid)
        except Exception:
            task = None
        if task:
            res = core.resume_task(tid)
            self._flash("继续传输…" if res.get("ok") else ("续传失败：%s" % res.get("error")))
        else:
            core.TRANSFERS.resume(tid)
        self._draw_progress()

    def _task_menu(self, ev, tid, path=""):
        """进度条上的右键菜单：暂停 / 中止 / 中止并删除源文件。"""
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=FG,
                    activebackground="#24334a", activeforeground=FG)
        m.add_command(label="暂停", command=lambda: self._cancel_one(tid, "pause"))
        m.add_command(label="恢复", command=lambda: self._resume_one(tid))
        m.add_separator()
        m.add_command(label="中止", command=lambda: self._cancel_one(tid, "abort"))
        if path and os.path.exists(path):
            m.add_command(label="中止并删除源文件",
                          command=lambda: (self._cancel_one(tid, "delete"),
                                           self._del_path(path)))
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _del_path(self, path):
        try:
            os.remove(path)
            self._flash("源文件已删除")
        except Exception as e:
            self._flash("删除失败：%s" % e)

    def _on_progress_click(self, ev):
        """点进度条右侧的按钮：进行中 → 中止；已暂停 → 恢复。"""
        for rect, tid, state, path in getattr(self, "_cancel_hits", []):
            x1, y1, x2, y2 = rect
            if x1 <= ev.x <= x2 and y1 <= ev.y <= y2:
                if state == "paused":
                    self._resume_one(tid)
                else:
                    self._cancel_one(tid, "abort")
                return

    def _on_progress_rightclick(self, ev):
        """右键进度区域任一行 → 弹出暂停/中止菜单。"""
        for rect, tid, state, path in getattr(self, "_cancel_hits", []):
            x1, y1, x2, y2 = rect
            # 命中整行（含左侧名称区）
            if y1 - 2 <= ev.y <= y2 + 26:
                self._task_menu(ev, tid, path)
                return

    def _flash(self, msg):
        self.lbl_state.config(text="● " + msg, foreground=OK)
        self.root.after(2000, lambda: self.lbl_state.config(
            text="● 已启动" if not self.already else "● 已在运行",
            foreground=OK if not self.already else WARN))

    # ------------------------------------------------------------ 刷新
    def _load_files(self):
        """刷新记录列表 —— 文字和文件**都要**显示（之前只显示文件，导致文字记录看不到）。"""
        try:
            devs = core.devices_list()
            rows = []
            for d in devs:
                for it in core.db_list(d["id"], 200):
                    rows.append({
                        "kind": it["kind"], "no": d.get("no", ""), "ts": it["ts"],
                        "fname": it["fname"], "size": it["size"],
                        "stored": it.get("stored", ""), "dev": d["id"],
                        "id": it["id"],
                        "devlabel": d.get("label", ""),
                        "is_local": d.get("is_local", False),
                        "frompc": it.get("frompc", "") or "",
                        "text": it.get("text", "") or "",
                        "clip": it.get("clip", 0),
                    })
            rows.sort(key=lambda x: -x["ts"])
            self.files = rows[:300]
            # iid -> 记录，删除/详情都用它反查，避免 index 错位
            self._by_iid = {f["id"] + "@" + f["dev"]: f for f in self.files}

            for r in self.tree.get_children():
                self.tree.delete(r)

            nfile = 0
            for f in self.files:
                ts = time.strftime("%m-%d %H:%M", time.localtime(f["ts"]))
                is_local = f.get("is_local")
                label = f.get("devlabel") or ""
                if is_local:
                    devno = "本机"
                    src = "我发出的" if f["frompc"] in ("本机发出",) else label or "本机"
                else:
                    devno = "#" + pad4(f["no"])
                    src = label or "设备"
                if f["kind"] == "text":
                    preview = f["text"].replace("\n", " ").strip()
                    if len(preview) > 60:
                        preview = preview[:57] + "..."
                    if f["clip"]:
                        preview = "[已进剪贴板] " + preview
                    self.tree.insert("", "end", iid=f["id"] + "@" + f["dev"],
                                    values=(devno, ts, preview, "文字", src))
                else:
                    nfile += 1
                    self.tree.insert("", "end", iid=f["id"] + "@" + f["dev"],
                                    values=(devno, ts, f["fname"],
                                           human_size(f["size"]), src))
# 刷新后同步详情框（保持原选中行）
            sel = self.tree.selection()
            if sel:
                self._on_tree_select()
        except Exception:
            pass

    def _draw_progress(self):
        c = self.prog_canvas
        c.delete("all")
        w = max(c.winfo_width(), 100)
        ch = max(c.winfo_height(), 100)
        items = core.TRANSFERS.snapshot()
        running = [x for x in items if x["state"] == "running"]
        self.lbl_active.config(text=("进行中 %d 个" % len(running)) if running else "空闲")
        if not items:
            c.create_text(w // 2, min(ch // 2, 60), text="暂无传输任务", fill=MUT,
                          font=("Segoe UI", 10))
            return
        y = 4
        self._cancel_hits = []
        for it in items[:6]:
            if y > ch - 18:
                break
            name = it["name"]
            if len(name) > 30:
                name = name[:27] + "..."
            state = it["state"]
            if state == "running":
                arrow = "↑ 发送" if it["dir"] == "up" else "↓ 接收"
            elif state == "done":
                arrow = "✓ 完成"
            elif state == "paused":
                arrow = "⏸ 已暂停（可继续）"
            elif state == "cancelled":
                arrow = "⊘ 已中止"
            else:
                arrow = "✗ 失败"
            color = OK if state == "done" else (
                BAD if state == "error" else (WARN if state in ("cancelled", "paused") else FG))
            c.create_text(2, y, anchor="nw", text="%s  %s" % (arrow, name),
                          fill=color, font=("Segoe UI", 9, "bold"))

            # 右侧：速度/剩余时间/错误
            if state == "running":
                right = "%.0f%%  %s · 剩 %s" % (it["percent"], human_speed(it["speed"]),
                                                human_eta(it["eta"]))
            elif state == "paused":
                right = "%.0f%%  已暂停" % it["percent"]
            elif state == "cancelled":
                right = "%.0f%%  已中止" % it["percent"]
            elif state == "error":
                right = (it.get("error", "") or "失败")[:28]
            else:
                right = human_size(it["done"])
            c.create_text(w - 26, y, anchor="ne", text=right, fill=MUT, font=("Consolas", 9))

            # 按钮：进行中→中止 ×；已暂停→继续 ▶
            if state in ("running", "paused"):
                bx2, by2 = w - 2, y + 13
                if state == "running":
                    bg, bc, sym = "#3a2222", "#5a2f2f", "×"
                else:
                    bg, bc, sym = "#223a24", "#2f5a33", "▶"
                c.create_rectangle(w - 22, y, bx2, by2, fill=bg, outline=bc)
                c.create_text((w - 22 + bx2) // 2, (y + by2) // 2, text=sym,
                              fill="#ef8f8f" if state == "running" else "#8fef9f",
                              font=("Segoe UI", 9, "bold"))
                self._cancel_hits.append(
                    ((w - 22, y, bx2, by2), it["id"], state, it.get("path", "")))

            # 进度条
            bar_y = y + 18
            bar_w = w - 4
            c.create_rectangle(2, bar_y, 2 + bar_w, bar_y + 8, fill="#0c1015",
                               outline=LINE)
            frac = it["percent"] / 100.0
            if frac > 0:
                bw = max(2, int(bar_w * frac))
                fill = OK if state == "done" else (WARN if state == "cancelled" else ACC)
                c.create_rectangle(2, bar_y, 2 + bw, bar_y + 8, fill=fill, outline="")
            y = bar_y + 20

    def _tick(self):
        try:
            self._draw_progress()
            if getattr(self, "_file_tick", 0) % 6 == 0:
                self._load_files()
            if getattr(self, "_file_tick", 0) % 15 == 0:
                self._refresh_targets()
            self._file_tick = getattr(self, "_file_tick", 0) + 1
        except Exception:
            pass
        self.root.after(400, self._tick)

    def _on_close(self):
        try:
            core.DISCOVERY.stop()
        except Exception:
            pass
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        self.root.destroy()


LOG_FILE = os.path.join(core.DATA_ROOT, "error.log")


def _log(msg):
    """把异常写进文件 —— 无控制台模式下这是唯一能找到错误的地方。"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def install_crash_handler():
    import traceback

    def hook(exc_type, exc, tb):
        txt = "".join(traceback.format_exception(exc_type, exc, tb))
        _log("UNCAUGHT:\n" + txt)
        try:
            messagebox.showerror("传输助手出错了",
                                 "程序遇到错误，已记录到：\n%s\n\n%s"
                                 % (LOG_FILE, str(exc)[:300]))
        except Exception:
            pass

    sys.excepthook = hook
    try:
        import threading as _t

        def thook(args):
            txt = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
            _log("THREAD:\n" + txt)

        _t.excepthook = thook
    except Exception:
        pass


def main():
    install_crash_handler()
    _log("---- 启动 v%s ----" % core.APP_VERSION)
    _log("DATA_ROOT=%s" % core.DATA_ROOT)
    _log("frozen=%s" % getattr(sys, "frozen", False))
    try:
        enable_dpi_awareness()
        root = tk.Tk()
        App(root)
        root.mainloop()
        _log("正常退出")
        return 0
    except Exception:
        import traceback
        _log("STARTUP FAIL:\n" + traceback.format_exc())
        try:
            messagebox.showerror("传输助手启动失败",
                                 "详情见：\n%s\n\n%s"
                                 % (LOG_FILE, traceback.format_exc()[-500:]))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
