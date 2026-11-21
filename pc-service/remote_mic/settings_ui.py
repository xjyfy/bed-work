"""设置页（本地弹窗）：绑定二维码、遥控器管理、四栏位应用与发送快捷键。

用 tkinter 实现，作为 pywebview 不可用时的回退，在工作线程里跑自己的 mainloop
（主线程留给 pywebview；托盘已 run_detached，不占主线程）。
所有落盘都走 store.save_config，保存后通知服务热重载，不需要重启进程。
"""

from __future__ import annotations

import logging
import tkinter as tk
import winreg
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import ImageTk

from . import store

logger = logging.getLogger(__name__)

_window_open = False


# ------------------------------------------------------------------ 已安装应用

def installed_apps() -> list[tuple[str, str]]:
    """与 pywebview 设置页共用同一份应用枚举（过滤规则一致）。"""
    from . import applist

    return [(item["name"], item["exe"]) for item in applist.list_app_entries(with_icons=False)]


def _run_pwsh(script: str) -> str:
    """调用 pwsh / powershell 拿 JSON，出错返回空串。"""
    import os
    import shutil
    import subprocess

    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="ignore",
            # GUI 进程没有控制台，不加这个每个 pwsh 调用都会闪一个黑框
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pwsh call failed: %s", exc)
        return ""
    return (out.stdout or "").strip()


def _start_menu_apps() -> list[tuple[str, str]]:
    import json

    script = (
        "$sh = New-Object -ComObject WScript.Shell;"
        "$items = @();"
        "$dirs = @(\"$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\","
        "\"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\");"
        "foreach ($d in $dirs) {"
        "  Get-ChildItem -LiteralPath $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |"
        "  ForEach-Object {"
        "    $t = $sh.CreateShortcut($_.FullName).TargetPath;"
        "    if ($t -and $t.ToLower().EndsWith('.exe')) {"
        "      $items += [pscustomobject]@{ name = $_.BaseName; exe = $t }"
        "    }"
        "  }"
        "};"
        "$items | ConvertTo-Json -Compress"
    )
    raw = _run_pwsh(script)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [(str(d.get("name", "")), str(d.get("exe", ""))) for d in data]


def _store_apps() -> list[tuple[str, str]]:
    import json

    script = (
        "Get-StartApps | Where-Object { $_.AppID -like '*!*' } | "
        "Select-Object Name, AppID | ConvertTo-Json -Compress"
    )
    raw = _run_pwsh(script)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [(str(d.get("Name", "")), str(d.get("AppID", ""))) for d in data]


def _read(key, name: str) -> str:
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return ""


def _exe_from(text: str) -> str:
    import os
    import re

    if not text:
        return ""
    m = re.search(r'"([^"]+\.exe)"', text, re.IGNORECASE) or re.search(r"(\S+\.exe)", text, re.IGNORECASE)
    path = m.group(1) if m else text
    path = path.strip().strip('"')
    if path.lower().endswith(".exe") and os.path.exists(path):
        return path
    return ""


# ------------------------------------------------------------------ 设置窗口

def open_settings_window(service) -> None:
    global _window_open
    if _window_open:
        return
    _window_open = True
    try:
        _build(service)
    finally:
        _window_open = False


def _build(service) -> None:
    root = tk.Tk()
    root.title("手机遥控麦 · 设置")
    root.geometry("820x760")
    root.minsize(760, 640)

    cfg = service.config

    # ---------------- 基本信息
    info = ttk.LabelFrame(root, text="本机")
    info.pack(fill="x", padx=12, pady=(12, 6))

    name_var = tk.StringVar(value=str(cfg.get("computer_name") or store.computer_name()))
    ttk.Label(info, text="电脑名称").grid(row=0, column=0, sticky="w", padx=8, pady=6)
    ttk.Entry(info, textvariable=name_var, width=28).grid(row=0, column=1, sticky="w", pady=6)
    ttk.Label(info, text=f"IP {store.lan_ip()}  端口 {cfg.get('port', 8765)}").grid(
        row=0, column=2, sticky="w", padx=16
    )

    # ---------------- 遥控器
    remotes_box = ttk.LabelFrame(root, text="已绑定遥控器")
    remotes_box.pack(fill="both", expand=False, padx=12, pady=6)

    tree = ttk.Treeview(remotes_box, columns=("name", "ip", "added", "state"), show="headings", height=6)
    for col, text, width in (
        ("name", "名称", 200),
        ("ip", "上次 IP", 140),
        ("added", "绑定时间", 150),
        ("state", "状态", 120),
    ):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w")
    tree.pack(fill="both", expand=True, padx=8, pady=8)

    qr_holder = {"img": None}

    def refresh_remotes() -> None:
        tree.delete(*tree.get_children())
        online = {c[0]: c[2] for c in service.active_remote_names()}
        for r in cfg.get("remotes", []):
            rid = r.get("id", "")
            state = "控制中" if online.get(rid) else ("在线·待命" if rid in online else "离线")
            tree.insert("", "end", iid=rid, values=(r.get("name", ""), r.get("last_ip", ""), r.get("added", ""), state))

    def create_qr() -> None:
        remote = service.create_binding("新遥控器")
        payload = store.qr_payload(cfg, remote["token"])
        try:
            image = store.make_qr_image(payload)
        except ImportError:
            messagebox.showerror("缺少依赖", "未安装 qrcode，请执行 pip install qrcode")
            return
        img = ImageTk.PhotoImage(image.resize((240, 240)))
        qr_holder["img"] = img
        qr_label.configure(image=img, text="")
        qr_hint.configure(
            text=f"用手机扫码绑定：{cfg.get('computer_name')}\n{store.lan_ip()}:{cfg.get('port', 8765)}"
        )
        refresh_remotes()

    def delete_selected() -> None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选中一个遥控器")
            return
        if not messagebox.askyesno("确认", "删除后该遥控器需要重新扫码才能使用，继续？"):
            return
        for rid in sel:
            service.delete_binding(rid)
        cfg.get("remotes", [])[:] = [r for r in cfg.get("remotes", []) if r.get("id") not in sel]
        refresh_remotes()

    btns = ttk.Frame(remotes_box)
    btns.pack(fill="x", padx=8, pady=(0, 8))
    ttk.Button(btns, text="生成绑定二维码", command=create_qr).pack(side="left")
    ttk.Button(btns, text="删除选中", command=delete_selected).pack(side="left", padx=8)
    ttk.Button(btns, text="刷新", command=refresh_remotes).pack(side="left")

    qr_area = ttk.Frame(remotes_box)
    qr_area.pack(fill="x", padx=8, pady=(0, 10))
    qr_label = ttk.Label(qr_area, text="点击「生成绑定二维码」显示")
    qr_label.pack(side="left")
    qr_hint = ttk.Label(qr_area, text="", justify="left")
    qr_hint.pack(side="left", padx=16)

    # ---------------- 应用栏位
    apps_box = ttk.LabelFrame(root, text="四个应用栏位（选择应用 + 发送快捷键，留空则走全局默认）")
    apps_box.pack(fill="both", expand=True, padx=12, pady=6)

    apps_cfg = cfg.setdefault("apps", {})
    slot_hotkey = cfg.setdefault("slot_send_hotkey", {})
    path_vars: dict[str, tk.StringVar] = {}
    hotkey_vars: dict[str, tk.StringVar] = {}

    for i, slot in enumerate(["1", "2", "3", "4"]):
        row = ttk.Frame(apps_box)
        row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text=f"栏位 {slot}", width=8).pack(side="left")

        path_var = tk.StringVar(value=str(apps_cfg.get(slot, "") or ""))
        path_vars[slot] = path_var
        entry = ttk.Entry(row, textvariable=path_var)
        entry.pack(side="left", fill="x", expand=True)

        def pick(s=slot) -> None:
            _pick_app(root, path_vars[s])

        ttk.Button(row, text="选择应用", command=pick).pack(side="left", padx=6)

        hk_var = tk.StringVar(value="+".join(slot_hotkey.get(slot) or []))
        hotkey_vars[slot] = hk_var
        ttk.Label(row, text="发送键").pack(side="left", padx=(8, 2))
        ttk.Entry(row, textvariable=hk_var, width=14).pack(side="left")

    hint = ttk.Label(
        apps_box,
        text="发送键写法：enter / ctrl+enter / shift+enter；留空表示沿用全局规则（按前台程序匹配 send_hotkey，最后回落 Enter）",
        foreground="#666",
        wraplength=760,
        justify="left",
    )
    hint.pack(anchor="w", padx=10, pady=(2, 8))

    # ---------------- 保存
    def save() -> None:
        cfg["computer_name"] = name_var.get().strip() or store.computer_name()
        for slot in ["1", "2", "3", "4"]:
            apps_cfg[slot] = path_vars[slot].get().strip()
            raw = hotkey_vars[slot].get().strip().lower()
            if raw:
                slot_hotkey[slot] = [p.strip() for p in raw.replace("+", " ").split() if p.strip()]
            else:
                slot_hotkey.pop(slot, None)
        store.save_config(cfg)
        service.reload_config()
        messagebox.showinfo("已保存", "配置已写入 config.json 并热重载")

    footer = ttk.Frame(root)
    footer.pack(fill="x", padx=12, pady=(4, 12))
    ttk.Button(footer, text="保存", command=save).pack(side="right")
    ttk.Button(footer, text="关闭", command=root.destroy).pack(side="right", padx=8)

    refresh_remotes()
    root.mainloop()


def _pick_app(parent: tk.Misc, target: tk.StringVar) -> None:
    """已安装应用选择器：列表 + 过滤 + 自定义浏览。"""
    win = tk.Toplevel(parent)
    win.title("选择应用")
    win.geometry("560x520")
    win.transient(parent)

    search = tk.StringVar()
    ttk.Entry(win, textvariable=search).pack(fill="x", padx=10, pady=8)

    listbox = tk.Listbox(win)
    listbox.pack(fill="both", expand=True, padx=10)

    apps = installed_apps()
    shown: list[tuple[str, str]] = []

    def refill(*_args) -> None:
        keyword = search.get().strip().lower()
        listbox.delete(0, tk.END)
        shown.clear()
        for name, exe in apps:
            if keyword and keyword not in name.lower() and keyword not in exe.lower():
                continue
            shown.append((name, exe))
            listbox.insert(tk.END, f"{name}    {exe}")

    search.trace_add("write", lambda *_: refill())

    def choose() -> None:
        sel = listbox.curselection()
        if not sel:
            return
        target.set(shown[sel[0]][1])
        win.destroy()

    def browse() -> None:
        path = filedialog.askopenfilename(title="选择程序", filetypes=[("可执行文件", "*.exe")])
        if path:
            target.set(path.replace("/", "\\"))
            win.destroy()

    bar = ttk.Frame(win)
    bar.pack(fill="x", padx=10, pady=8)
    ttk.Button(bar, text="确定", command=choose).pack(side="right")
    ttk.Button(bar, text="取消", command=win.destroy).pack(side="right", padx=8)
    ttk.Button(bar, text="浏览 exe…", command=browse).pack(side="left")

    refill()
    win.grab_set()
