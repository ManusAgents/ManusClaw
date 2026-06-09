"""
ManusClaw Desktop GUI — built with Flet.
Provides a terminal-style chat interface running ManusClaw locally.
Launch: python -m app.desktop.main
"""
from __future__ import annotations
import asyncio
import sys
import threading
from pathlib import Path
from typing import Optional

try:
    import flet as ft
except ImportError:
    sys.exit(
        "Flet not installed. Run:  pip install flet\n"
        "Then relaunch:           python -m app.desktop.main"
    )

# ─── colours ────────────────────────────────────────────────────────────────
BG        = "#0d0d0d"
PANEL     = "#111111"
ACCENT    = "#00ff88"
ACCENT2   = "#00ccff"
TEXT      = "#e0e0e0"
MUTED     = "#555555"
USER_CLR  = "#00ccff"
AI_CLR    = "#00ff88"
ERR_CLR   = "#ff4444"
WARN_CLR  = "#ffaa00"

BANNER = r"""
  __  __                      ______ _               
 |  \/  |                    |  ____| |              
 | \  / | __ _ _ __  _   _ _| |__  | | _____      __
 | |\/| |/ _` | '_ \| | | / /  __| | |/ _ \ \ /\ / /
 | |  | | (_| | | | | |_| / /| |____| | (_) \ V  V / 
 |_|  |_|\__,_|_| |_|\__,/_/ |______|_|\___/ \_/\_/  
              Autonomous AI Ecosystem  v3.2
"""


# ─── agent runner ────────────────────────────────────────────────────────────

def _run_agent(task: str, on_token, on_done, on_error):
    """Run ManusClaw agent in a background thread."""
    try:
        # Lazy import so desktop works even if some deps are missing
        from app.flow.planning import PlanningFlow
        import asyncio as _aio

        async def _go():
            flow = PlanningFlow()
            result = await flow.run(task)
            return result

        loop = _aio.new_event_loop()
        result = loop.run_until_complete(_go())
        loop.close()
        on_done(str(result))
    except Exception as e:
        on_error(str(e))


# ─── message bubble ──────────────────────────────────────────────────────────

def _bubble(role: str, text: str) -> ft.Container:
    is_user = role == "user"
    colour  = USER_CLR if is_user else AI_CLR
    label   = "YOU" if is_user else "MANUSCLAW"
    return ft.Container(
        content=ft.Column(
            [
                ft.Text(label, size=10, weight=ft.FontWeight.BOLD,
                        color=colour, font_family="monospace"),
                ft.SelectionArea(
                    content=ft.Text(
                        text, size=13, color=TEXT,
                        selectable=True, font_family="monospace",
                    )
                ),
            ],
            spacing=2,
        ),
        bgcolor=PANEL,
        border=ft.border.only(left=ft.border.BorderSide(2, colour)),
        border_radius=4,
        padding=ft.padding.only(left=10, top=8, right=10, bottom=8),
        margin=ft.margin.only(bottom=8),
    )


def _status_line(msg: str, color: str = MUTED) -> ft.Text:
    return ft.Text(f"› {msg}", size=11, color=color, font_family="monospace")


# ─── main app ────────────────────────────────────────────────────────────────

def main(page: ft.Page):
    page.title       = "ManusClaw — Autonomous AI Ecosystem"
    page.bgcolor     = BG
    page.window_width    = 960
    page.window_height   = 700
    page.window_min_width  = 600
    page.window_min_height = 400
    page.fonts = {"monospace": "Courier New"}
    page.padding = 0

    # ── chat log ──
    chat_col = ft.Column(
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        spacing=0,
    )

    # ── banner ──
    chat_col.controls.append(
        ft.Container(
            ft.Text(BANNER, size=10, color=ACCENT,
                    font_family="monospace"),
            padding=ft.padding.only(left=12, top=12, bottom=4),
        )
    )
    chat_col.controls.append(
        _status_line("System ready. Type a task and press Enter.", MUTED)
    )

    status_text = ft.Text("● Idle", size=11, color=MUTED,
                          font_family="monospace")

    def _scroll_bottom():
        chat_col.scroll_to(offset=-1, duration=200)

    def _add(ctrl):
        chat_col.controls.append(ctrl)
        page.update()
        _scroll_bottom()

    # ── input ──
    task_input = ft.TextField(
        hint_text="Enter your task…",
        hint_style=ft.TextStyle(color=MUTED),
        bgcolor="#0a0a0a",
        border_color=ACCENT,
        focused_border_color=ACCENT2,
        color=TEXT,
        text_style=ft.TextStyle(font_family="monospace", size=13),
        multiline=True,
        min_lines=1,
        max_lines=5,
        expand=True,
        border_radius=6,
        on_submit=lambda e: _send(),
    )

    running = {"active": False}

    def _send():
        task = task_input.value.strip()
        if not task or running["active"]:
            return
        task_input.value = ""
        running["active"] = True
        status_text.value = "● Running…"
        status_text.color = WARN_CLR
        page.update()

        _add(_bubble("user", task))
        _add(_status_line("Agent thinking…", WARN_CLR))

        def _on_done(result):
            _add(_bubble("agent", result))
            _add(_status_line("Task complete.", ACCENT))
            status_text.value = "● Idle"
            status_text.color = MUTED
            running["active"] = False
            page.update()

        def _on_error(err):
            _add(ft.Container(
                ft.Text(f"ERROR: {err}", color=ERR_CLR, size=12,
                        font_family="monospace"),
                bgcolor=PANEL,
                border=ft.border.only(left=ft.border.BorderSide(2, ERR_CLR)),
                border_radius=4,
                padding=10,
                margin=ft.margin.only(bottom=8),
            ))
            status_text.value = "● Idle"
            status_text.color = MUTED
            running["active"] = False
            page.update()

        threading.Thread(
            target=_run_agent,
            args=(task, None, _on_done, _on_error),
            daemon=True,
        ).start()

    send_btn = ft.ElevatedButton(
        text="RUN",
        bgcolor=ACCENT,
        color="#000000",
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=6),
        ),
        on_click=lambda e: _send(),
    )

    # ── config panel (collapsible) ──
    cfg_model   = ft.TextField(label="Model", value="llama3.2:3b",
                               bgcolor="#0a0a0a", color=TEXT,
                               label_style=ft.TextStyle(color=MUTED),
                               border_color=MUTED, height=40)
    cfg_base    = ft.TextField(label="Base URL", value="http://localhost:11434/v1",
                               bgcolor="#0a0a0a", color=TEXT,
                               label_style=ft.TextStyle(color=MUTED),
                               border_color=MUTED, height=40)
    cfg_key     = ft.TextField(label="API Key", value="none",
                               bgcolor="#0a0a0a", color=TEXT,
                               label_style=ft.TextStyle(color=MUTED),
                               border_color=MUTED, height=40,
                               password=True, can_reveal_password=True)

    cfg_panel = ft.Column([
        ft.Row([cfg_model, cfg_base, cfg_key], spacing=8),
    ], visible=False)

    def _toggle_cfg(e):
        cfg_panel.visible = not cfg_panel.visible
        page.update()

    cfg_btn = ft.TextButton(
        "⚙ Settings", on_click=_toggle_cfg,
        style=ft.ButtonStyle(color=MUTED),
    )

    # ── layout ──
    page.add(
        ft.Column([
            # top bar
            ft.Container(
                ft.Row([
                    ft.Text("MANUSCLAW", size=14, weight=ft.FontWeight.BOLD,
                            color=ACCENT, font_family="monospace"),
                    ft.Text("Autonomous AI Ecosystem", size=11, color=MUTED,
                            font_family="monospace"),
                    ft.Container(expand=True),
                    status_text,
                    cfg_btn,
                ]),
                bgcolor=PANEL,
                padding=ft.padding.symmetric(horizontal=14, vertical=8),
                border=ft.border.only(bottom=ft.border.BorderSide(1, MUTED)),
            ),
            # config panel
            ft.Container(cfg_panel,
                         padding=ft.padding.symmetric(horizontal=14, vertical=6),
                         bgcolor=PANEL, visible=True),
            # chat area
            ft.Container(chat_col, expand=True,
                         padding=ft.padding.symmetric(horizontal=14, vertical=8)),
            # input bar
            ft.Container(
                ft.Row([task_input, send_btn], spacing=8, vertical_alignment="end"),
                bgcolor=PANEL,
                padding=ft.padding.symmetric(horizontal=14, vertical=10),
                border=ft.border.only(top=ft.border.BorderSide(1, MUTED)),
            ),
        ], expand=True, spacing=0)
    )


def main_entry() -> None:
    """Packaged entry point used by pyproject.toml [project.scripts]."""
    ft.app(target=main)


if __name__ == "__main__":
    ft.app(target=main)
