# ==============================================================================
# UI DESIGN AND STYLING COMPLETE CODE - SnapDownloader (Neon Premium Edition)
# ==============================================================================

from core.theme_extractor import build_accent_tokens

THEMES = {
    "Modern Dark": {
        "bg":         "rgba(15, 15, 20, 0.85)",
        "bg_2":       "rgba(25, 25, 35, 0.65)",
        "panel":      "rgba(35, 35, 50, 0.55)",
        "panel_alt":  "rgba(45, 45, 65, 0.45)",
        "panel_soft": "rgba(45, 45, 65, 0.35)",
        "accent":     "#00D2FF",
        "accent_2":   "#3A7BD5",
        "gold":       "#10B981",
        "text":       "#FAFAFA",
        "muted":      "#A1A1AA",
        "border":     "rgba(255, 255, 255, 0.1)",
        "success":    "#00E676",
        "danger":     "#FF3D00",
        "warning":    "#FFC400",
    },
    "Midnight Neon": {
        "bg":         "rgba(10, 10, 15, 0.90)",
        "bg_2":       "rgba(20, 20, 30, 0.70)",
        "panel":      "rgba(30, 30, 45, 0.60)",
        "panel_alt":  "rgba(40, 40, 60, 0.50)",
        "panel_soft": "rgba(40, 40, 60, 0.40)",
        "accent":     "#00E5FF",
        "accent_2":   "#2979FF",
        "gold":       "#10B981",
        "text":       "#FFFFFF",
        "muted":      "#B0BEC5",
        "border":     "rgba(255, 255, 255, 0.08)",
        "success":    "#00E676",
        "danger":     "#FF1744",
        "warning":    "#FFC400",
    }
}

DEFAULT_THEME = "Midnight Neon"

def get_theme(theme_name: str | None) -> dict:
    name = str(theme_name or "").strip()
    base_theme = THEMES.get(name) if name in THEMES else THEMES.get(DEFAULT_THEME, next(iter(THEMES.values())))
    theme = dict(base_theme)
    accent_tokens = build_accent_tokens()
    theme.update(accent_tokens.to_dict())
    theme.setdefault("accent_soft", f"rgba(99, 102, 241, 0.18)")
    theme.setdefault("accent_border", f"rgba(99, 102, 241, 0.35)")
    theme.setdefault("accent_text", theme["accent"])
    return theme

def get_style_sheet(theme_name: str) -> str:
    t = get_theme(theme_name)

    return f"""
    QMainWindow, QWidget#main_root {{
        background-color: {t['bg']};
        color: {t['text']};
        font-family: 'Segoe UI Variable', 'Inter', 'Segoe UI', system-ui, sans-serif;
        font-size: 14px;
    }}
    QWidget {{
        color: {t['text']};
        font-family: 'Segoe UI Variable', 'Inter', 'Segoe UI', system-ui, sans-serif;
    }}
    QLabel {{
        background-color: transparent;
    }}

    QFrame#page {{
        background-color: transparent;
    }}
    QFrame#sidebar_nav {{
        background-color: {t['bg_2']};
        border-right: 1px solid {t['border']};
    }}
    QFrame#title_bar {{
        background-color: transparent;
        border-bottom: 1px solid {t['border']};
    }}
    
    QPushButton#title_btn {{
        background-color: transparent;
        color: {t['muted']};
        border: none;
        border-radius: 8px;
        font-weight: 800;
    }}
    QPushButton#title_btn:hover {{
        background-color: {t['panel_soft']};
        color: {t['text']};
    }}
    QPushButton#title_btn_close {{
        background-color: transparent;
        color: {t['muted']};
        border: none;
        border-radius: 8px;
        font-weight: 800;
    }}
    QPushButton#title_btn_close:hover {{
        background-color: {t['danger']};
        color: #FFFFFF;
    }}
    QLabel#title_label {{
        color: {t['text']};
        font-weight: 800;
        font-size: 14px;
        letter-spacing: 1px;
    }}

    QPushButton#nav_btn {{
        background-color: transparent;
        color: {t['muted']};
        border: 1px solid transparent;
        padding: 10px 10px;
        font-weight: 700;
        font-size: 15px;
        text-align: left;
        border-radius: 12px;
        margin: 2px 2px;
    }}
    QPushButton#nav_btn:hover {{
        color: {t['text']};
        background-color: {t['panel_soft']};
        border: 1px solid rgba(255,255,255,0.05);
    }}
    QPushButton#nav_btn:checked {{
        color: #FFFFFF;
        background-color: {t['panel']};
        border: 1px solid {t['accent_border']};
        border-left: 4px solid {t['accent']};
        font-weight: 800;
    }}
    QPushButton#nav_toggle_btn {{
        background-color: transparent;
        color: {t['muted']};
        border: 1px solid transparent;
        border-radius: 8px;
    }}
    QPushButton#nav_toggle_btn:hover {{
        background-color: {t['panel_soft']};
        border: 1px solid {t['border']};
    }}

    QFrame#search_bar_container {{
        background-color: {t['bg_2']};
        min-height: 85px;
        max-height: 85px;
        border-bottom: 1px solid {t['border']};
    }}
    QFrame#browser_controls_container {{
        background-color: {t['bg_2']};
        border: 1px solid {t['border']};
        border-radius: 12px;
    }}
    QLineEdit#search_input, QLineEdit#path_input, QSpinBox, QTextEdit, QDateTimeEdit {{
        background-color: {t['panel']};
        border: 1px solid {t['border']};
        border-radius: 14px;
        padding: 12px 16px;
        color: {t['text']};
        font-size: 15px;
    }}
    QTextEdit#browser_detected_text {{
        font-family: 'Cascadia Mono', 'Consolas', monospace;
        font-size: 13px;
        padding: 10px 12px;
    }}
    QLineEdit#search_input:focus, QLineEdit#path_input:focus, QSpinBox:focus, QTextEdit:focus, QDateTimeEdit:focus {{
        border: 1px solid {t['accent']};
        background-color: {t['panel_alt']};
    }}

    QPushButton#btn-accent, QPushButton#trim_btn_accent {{
        background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['accent']}, stop:1 {t['accent_2']});
        color: #FFFFFF;
        border: 1px solid rgba(255, 255, 255, 0.2);
        border-radius: 14px;
        padding: 12px 24px;
        font-weight: 800;
        font-size: 15px;
    }}
    QPushButton#btn-accent:hover, QPushButton#trim_btn_accent:hover {{
        background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['accent_2']}, stop:1 {t['accent']});
        border: 1px solid rgba(255, 255, 255, 0.4);
    }}

    QPushButton#action_download, QPushButton#playlist_download_footer, QPushButton#btn-success {{
        background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['success']}, stop:1 #00C853);
        color: #FFFFFF;
        border: 1px solid rgba(255, 255, 255, 0.2);
        border-radius: 14px;
        padding: 12px 24px;
        font-weight: 800;
        font-size: 15px;
    }}
    QPushButton#action_download:hover {{
        background-color: {t['success']};
        border: 1px solid rgba(255, 255, 255, 0.5);
    }}

    QPushButton#action_button, QPushButton#action_trim, QPushButton#action_schedule {{
        background-color: {t['panel']};
        color: {t['text']};
        border: 1px solid {t['border']};
        border-radius: 14px;
        padding: 10px 16px;
        font-weight: 600;
    }}
    QPushButton#action_button:hover {{
        border: 1px solid {t['accent']};
        color: {t['accent_text']};
        background-color: {t['panel_alt']};
    }}
    QPushButton#action_button:disabled, QPushButton#action_trim:disabled, QPushButton#action_schedule:disabled, QPushButton#action_download:disabled {{
        background-color: {t['panel']};
        color: {t['muted']};
        border: 1px solid {t['border']};
    }}
    QPushButton[browserPrimaryAction="true"] {{
        font-size: 13px;
        font-weight: 700;
        padding: 8px 12px;
        border-radius: 12px;
    }}
    QPushButton[browserToolAction="true"] {{
        font-size: 12px;
        padding: 6px 10px;
        border-radius: 10px;
    }}
    QPushButton[bulkCompactAction="true"] {{
        font-size: 13px;
        font-weight: 700;
        padding: 8px 12px;
        border-radius: 12px;
    }}

    QFrame#SegmentedControl {{
        background-color: {t['bg_2']};
        border-radius: 10px;
        border: 1px solid {t['border']};
    }}
    QPushButton#SegmentedBtn {{
        background-color: transparent;
        color: {t['muted']};
        border: none;
        border-radius: 8px;
        padding: 8px 12px;
        font-weight: 800;
        font-size: 14px;
    }}
    QPushButton#SegmentedBtn:hover {{
        color: {t['text']};
        background-color: rgba(255, 255, 255, 0.05);
    }}
    QPushButton#SegmentedBtn:checked {{
        background-color: {t['accent_soft']};
        color: {t['accent_text']};
        border: 1px solid {t['accent_border']};
    }}

    QFrame#single_card, QFrame#playlist_row, QFrame#playlist_header {{
        background-color: {t['panel']};
        border-radius: 16px;
        border: 1px solid {t['border']};
    }}
    QFrame#single_card:hover {{
        border: 1px solid {t['accent_border']};
        background-color: {t['panel_alt']};
    }}
    QFrame#playlist_row:hover {{
        background-color: {t['panel_alt']};
        border: 1px solid {t['accent_border']};
    }}

    QComboBox {{
        background-color: {t['panel']};
        border: 1px solid {t['border']};
        border-radius: 12px;
        color: {t['text']};
        padding: 8px 12px;
        font-size: 14px;
        font-weight: 600;
    }}
    QComboBox:focus {{
        border: 1px solid {t['accent']};
        background-color: {t['panel_alt']};
    }}
    QComboBox::drop-down {{
        border: none;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t['bg_2']};
        color: {t['text']};
        border: 1px solid {t['border']};
        selection-background-color: {t['accent']};
        border-radius: 10px;
    }}

    QRadioButton {{
        color: {t['text']};
        spacing: 10px;
        font-size: 14px;
        font-weight: 600;
    }}
    QRadioButton::indicator {{
        width: 20px;
        height: 20px;
        border-radius: 10px;
        border: 2px solid {t['muted']};
        background-color: {t['panel']};
    }}
    QRadioButton::indicator:checked {{
        border: 6px solid {t['accent']};
        background-color: {t['bg']};
    }}
    QRadioButton::indicator:hover {{
        border: 2px solid {t['accent_2']};
    }}

    QLabel#section_title {{
        color: {t['accent']};
        font-size: 16px;
        font-weight: 900;
        letter-spacing: 1px;
    }}
    QLabel#single_title, QLabel#playlist_title, QLabel#empty_title {{
        color: {t['text']};
        font-weight: 800;
        font-size: 18px;
    }}
    QLabel#single_sub, QLabel#empty_sub, QLabel#bottom_text {{
        color: {t['muted']};
        font-size: 13px;
        font-weight: 500;
    }}
    QLabel#chip {{
        background-color: {t['accent_soft']};
        color: {t['accent_text']};
        border: 1px solid {t['accent_border']};
        border-radius: 8px;
        padding: 4px 12px;
        font-size: 12px;
        font-weight: bold;
    }}

    QProgressBar {{
        background-color: {t['panel']};
        border-radius: 8px;
        text-align: center;
        color: transparent;
        border: 1px solid {t['border']};
    }}
    QProgressBar::chunk {{
        background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t['accent']}, stop:1 {t['accent_2']});
        border-radius: 8px;
    }}
    """
