from core.qt_compat import (
    QColor, QEvent, QObject, QPainter, QPainterPath, QPen, QSize, QWidget,
    QPushButton, QPropertyAnimation, QEasingCurve, QRect, QGraphicsDropShadowEffect
)


class WheelEventFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            return True
        return super().eventFilter(obj, event)


class AnimatedButton(QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._glow = None
        self._glow_anim = None
        self._ensure_glow_effect()

    def _ensure_glow_effect(self):
        try:
            if self._glow is not None:
                _ = self._glow.blurRadius()
                if self.graphicsEffect() is not self._glow:
                    self.setGraphicsEffect(self._glow)
                if self._glow_anim is not None and self._glow_anim.targetObject() is self._glow:
                    return True
        except RuntimeError:
            pass

        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(0)
        glow.setColor(QColor(0, 229, 255, 0))
        glow.setOffset(0, 0)
        self._glow = glow
        self.setGraphicsEffect(glow)

        self._glow_anim = QPropertyAnimation(glow, b"blurRadius")
        self._glow_anim.setDuration(150)
        self._glow_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        return True

    def enterEvent(self, event):
        # Glow effect (guard against stale/deleted C++ effect objects)
        try:
            self._ensure_glow_effect()
            self._glow.setColor(QColor(0, 229, 255, 150))
            self._glow_anim.stop()
            self._glow_anim.setStartValue(self._glow.blurRadius())
            self._glow_anim.setEndValue(25)
            self._glow_anim.start()
        except RuntimeError:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event):
        # Remove glow effect (guard against stale/deleted C++ effect objects)
        try:
            self._ensure_glow_effect()
            self._glow_anim.stop()
            self._glow_anim.setStartValue(self._glow.blurRadius())
            self._glow_anim.setEndValue(0)
            self._glow_anim.start()
        except RuntimeError:
            pass
        super().leaveEvent(event)


def add_soft_shadow(widget, color="#000000", blur_radius=30, alpha=100):
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(blur_radius)
    shadow.setXOffset(0)
    shadow.setYOffset(6)
    c = QColor(color).toRgb()
    c.setAlpha(max(0, min(255, int(alpha))))
    shadow.setColor(c)
    widget.setGraphicsEffect(shadow)



class SpeedGraphWidget(QWidget):
    def __init__(self, accent_color: str, background_color: str, parent=None):
        super().__init__(parent)
        self._values = []
        self._max_value = 1.0
        self._accent = QColor(accent_color)
        self._background = QColor(background_color)

    def set_values(self, values, max_value: float):
        self._values = list(values or [])
        try:
            mv = float(max_value)
        except Exception:
            mv = 1.0
        self._max_value = mv if mv > 0 else 1.0
        self.update()

    def sizeHint(self):
        return QSize(120, 28)

    def minimumSizeHint(self):
        return QSize(80, 20)

    def paintEvent(self, event):
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, self._background)
        count = len(self._values)
        if count <= 1:
            return
        width = max(1, rect.width())
        height = max(1, rect.height())
        step_x = width / float(count - 1)
        path = QPainterPath()
        for idx, raw in enumerate(self._values):
            try:
                value = float(raw)
            except Exception:
                value = 0.0
            value = min(max(value, 0.0), self._max_value)
            ratio = value / self._max_value if self._max_value > 0 else 0.0
            x = rect.left() + idx * step_x
            y = rect.bottom() - ratio * height
            if idx == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        pen = QPen(self._accent)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawPath(path)
