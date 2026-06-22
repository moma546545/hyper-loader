import os
os.environ.setdefault("QT_API", "pyside6")
from core.i18n import _
import qtawesome as qta
from PySide6.QtWidgets import QFrame, QVBoxLayout, QPushButton, QSizePolicy, QButtonGroup
from PySide6.QtCore import QPropertyAnimation, QEasingCurve, Qt, Signal

class PremiumSidebar(QFrame):
    view_changed = Signal(str)
    EXPANDED_WIDTH = 160
    COLLAPSED_WIDTH = 62

    def __init__(self, theme_colors, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar_nav")
        self.theme = theme_colors
        self.is_expanded = True
        self.setFixedWidth(self.EXPANDED_WIDTH)
        
        self._nav_layout = QVBoxLayout(self)
        self._nav_layout.setContentsMargins(6, 16, 6, 16)
        self._nav_layout.setSpacing(10)
        
        # Hamburger button
        self.toggle_btn = QPushButton("")
        self.toggle_btn.setIcon(qta.icon('fa5s.bars', color=self.theme['muted']))
        self.toggle_btn.setObjectName("nav_toggle_btn")
        self.toggle_btn.setFixedSize(40, 40)
        self.toggle_btn.setStyleSheet("margin-left: 2px;")
        self.toggle_btn.setToolTip(_("Collapse/Expand Sidebar"))
        self.toggle_btn.clicked.connect(self.toggle_sidebar)
        self._nav_layout.addWidget(self.toggle_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        
        self._nav_layout.addSpacing(10)
        
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = {}

        self.entries = [
            ('fa5s.search', "Search", "search"),
            ('fa5s.globe', "Smart Browser", "browser"),
            ('fa5s.download', "Downloads", "downloads"),
            ('fa5s.list', "Playlists", "playlists"),
            ('fa5s.rss', "Subscriptions", "subscriptions"),
            ('fa5s.tools', "Tools", "tools"),
            ('fa5s.chart-bar', "Stats", "stats"),
            ('fa5s.exclamation-triangle', "Errors", "errors"),
            ('fa5s.cog', "Settings", "settings"),
        ]

        for icon, text, key in self.entries:
            btn = self.create_nav_btn(icon, text, key)
            self.nav_buttons[key] = btn

        self._nav_layout.addStretch()
        
        self.animation = QPropertyAnimation(self, b"maximumWidth")
        self.animation.setDuration(250)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutQuart)

    def create_nav_btn(self, icon_name, text, key):
        translated = _(text)
        btn = QPushButton(f"  {translated}")
        btn.setIcon(qta.icon(icon_name, color=self.theme['muted']))
        btn.setObjectName("nav_btn")
        btn.setCheckable(True)
        btn.setFixedHeight(45)
        btn.setProperty("source_text", text)
        btn.setProperty("full_text", translated)
        btn.setProperty("icon_name", icon_name)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.clicked.connect(lambda _, k=key: self.view_changed.emit(k))
        self.nav_group.addButton(btn)
        self._nav_layout.addWidget(btn)
        return btn

    def set_theme(self, theme_colors):
        self.theme = theme_colors
        # Update hamburger icon
        self.toggle_btn.setIcon(qta.icon('fa5s.bars', color=self.theme['muted']))
        # Update nav buttons icons
        for key, btn in self.nav_buttons.items():
            icon_name = btn.property("icon_name")
            btn.setIcon(qta.icon(icon_name, color=self.theme['muted']))
        self.update()

    def toggle_sidebar(self):
        self.is_expanded = not self.is_expanded
        target = self.EXPANDED_WIDTH if self.is_expanded else self.COLLAPSED_WIDTH
        current = self.width()

        # Release min/max constraints so animation can run freely
        self.setMinimumWidth(min(current, target))
        self.setMaximumWidth(max(current, target))

        self.animation.stop()
        try:
            self.animation.finished.disconnect()
        except RuntimeError:
            pass

        self.animation.setStartValue(current)
        self.animation.setEndValue(target)

        # After animation completes: lock width and update button text
        self.animation.finished.connect(lambda: self.setFixedWidth(target))
        self.animation.finished.connect(self._update_button_text)
        self.animation.start()

    def _update_button_text(self):
        for key, btn in self.nav_buttons.items():
            if self.is_expanded:
                btn.setText(f"  {btn.property('full_text')}")
                btn.setToolTip("")
            else:
                btn.setText("")
                btn.setToolTip(btn.property("full_text"))

    def retranslate_ui(self):
        self.toggle_btn.setToolTip(_("Collapse/Expand Sidebar"))
        for btn in self.nav_buttons.values():
            source_text = str(btn.property("source_text") or btn.property("full_text") or "")
            translated = _(source_text)
            btn.setProperty("full_text", translated)
        self._update_button_text()
