from __future__ import annotations

from core.qt_compat import QEasingCurve, QGraphicsOpacityEffect, QPropertyAnimation, QTimer


class UIController:
    def __init__(self, window):
        self.window = window

    def set_status(self, value: str):
        search_view = getattr(self.window, "search_view", None)
        if search_view is None:
            return
        label = getattr(search_view, "state_label", None)
        if label is not None:
            label.setText(str(value or ""))

    def switch_view(self, key: str):
        w = self.window
        view = str(key or "").strip()
        if not view:
            return
        stack = getattr(w, "main_stack", None) or getattr(w, "stack", None)
        target = self._resolve_target_view(view)
        if stack is not None and target is not None:
            stack.setCurrentWidget(target)
        w.active_view = view
        self._sync_nav_selection(view)
        if view == "downloads":
            refresh = getattr(w, "_refresh_downloads_list", None)
            if callable(refresh):
                QTimer.singleShot(0, refresh)

    def animate_view_change(self):
        stack = getattr(self.window, "main_stack", None) or getattr(self.window, "stack", None)
        if stack is None:
            return
        current = stack.currentWidget()
        if current is None:
            return
        self.fade_widget(current, delay_ms=0, duration_ms=180)

    def _resolve_target_view(self, view: str):
        w = self.window
        aliases = {
            "playlists": "playlist_view",
            "playlist": "playlist_view",
            "subscriptions": "subscriptions_view",
            "subscription": "subscriptions_view",
            "errors": "error_dashboard",
            "error": "error_dashboard",
            "stats": "stats_view",
        }
        attr = aliases.get(view, f"{view}_view")
        return getattr(w, attr, None)

    def _sync_nav_selection(self, view: str):
        sidebar = getattr(self.window, "sidebar", None)
        nav = getattr(sidebar, "nav_buttons", None) if sidebar else getattr(self.window, "nav_buttons", None)
        if not isinstance(nav, dict):
            return
        for key, btn in nav.items():
            try:
                btn.setChecked(str(key) == view)
            except Exception:
                continue

    def fade_widget(self, widget, delay_ms=0, duration_ms=280):
        if widget is None:
            return

        def _run():
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(widget)
                widget.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", widget)
            anim.setDuration(max(80, int(duration_ms)))
            anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.start()
            self._keep_anim(anim)

        if delay_ms and int(delay_ms) > 0:
            QTimer.singleShot(int(delay_ms), _run)
        else:
            _run()

    def pulse_widget(self, widget):
        if widget is None:
            return
        anim = QPropertyAnimation(widget, b"windowOpacity", widget)
        anim.setDuration(380)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.setKeyValueAt(0.0, 1.0)
        anim.setKeyValueAt(0.5, 0.65)
        anim.setKeyValueAt(1.0, 1.0)
        anim.start()
        self._keep_anim(anim)

    def _keep_anim(self, anim):
        bucket = getattr(self.window, "_ui_animations", None)
        if bucket is None:
            bucket = []
            setattr(self.window, "_ui_animations", bucket)
        bucket.append(anim)
        anim.finished.connect(lambda: self._prune_anim(anim))

    def _prune_anim(self, anim):
        bucket = getattr(self.window, "_ui_animations", None) or []
        try:
            bucket.remove(anim)
        except ValueError:
            pass

    def toggle_mini_mode(self):
        w = self.window
        mini = getattr(w, "mini_window", None)
        if mini is not None:
            if mini.isVisible():
                mini.hide()
                w.showNormal()
                try:
                    w.raise_()
                except Exception:
                    pass
                w.activateWindow()
            else:
                mini.show()
                w.hide()

    def show_from_mini(self):
        w = self.window
        mini = getattr(w, "mini_window", None)
        if mini is not None:
            mini.hide()
        w.showNormal()
        try:
            w.raise_()
        except Exception:
            pass
        w.activateWindow()
