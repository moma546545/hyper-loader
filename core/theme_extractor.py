import colorsys
import logging
from dataclasses import dataclass

try:
    import winreg
except ImportError:
    winreg = None


logger = logging.getLogger("SnapDownloader.ThemeExtractor")

_REGISTRY_PATH = r"Software\Microsoft\Windows\DWM"
_ACCENT_NAME = "AccentColor"
_DEFAULT_HEX = "#6366F1"


@dataclass(frozen=True)
class AccentThemeTokens:
    accent: str
    accent_2: str
    accent_soft: str
    accent_border: str
    accent_text: str

    def to_dict(self) -> dict[str, str]:
        return {
            "accent": self.accent,
            "accent_2": self.accent_2,
            "accent_soft": self.accent_soft,
            "accent_border": self.accent_border,
            "accent_text": self.accent_text,
        }


def _clamp_byte(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _parse_windows_accent(raw_value: int) -> tuple[int, int, int]:
    # Windows stores AccentColor as ABGR in many builds.
    blue = raw_value & 0xFF
    green = (raw_value >> 8) & 0xFF
    red = (raw_value >> 16) & 0xFF
    return red, green, blue


def _hex_from_rgb(red: int, green: int, blue: int) -> str:
    return f"#{red:02X}{green:02X}{blue:02X}"


def _rgba(red: int, green: int, blue: int, alpha: float) -> str:
    return f"rgba({red}, {green}, {blue}, {max(0.0, min(1.0, float(alpha))):.3f})"


def _adjust_lightness(red: int, green: int, blue: int, *, saturation_scale: float = 1.0, lightness_shift: float = 0.0) -> tuple[int, int, int]:
    hue, lightness, saturation = colorsys.rgb_to_hls(red / 255.0, green / 255.0, blue / 255.0)
    saturation = max(0.0, min(1.0, saturation * saturation_scale))
    lightness = max(0.0, min(1.0, lightness + lightness_shift))
    next_red, next_green, next_blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return (
        _clamp_byte(next_red * 255.0),
        _clamp_byte(next_green * 255.0),
        _clamp_byte(next_blue * 255.0),
    )


def read_windows_accent_color() -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_PATH) as key:
            raw_value, _ = winreg.QueryValueEx(key, _ACCENT_NAME)
        if not isinstance(raw_value, int):
            return None
        return _hex_from_rgb(*_parse_windows_accent(raw_value))
    except Exception as exc:
        logger.debug("Failed to read Windows accent color: %s", exc)
        return None


def build_accent_tokens(base_hex: str | None = None) -> AccentThemeTokens:
    accent_hex = str(base_hex or "").strip() or read_windows_accent_color() or _DEFAULT_HEX
    try:
        red = int(accent_hex[1:3], 16)
        green = int(accent_hex[3:5], 16)
        blue = int(accent_hex[5:7], 16)
    except Exception:
        red, green, blue = (99, 102, 241)
    accent_2_rgb = _adjust_lightness(red, green, blue, saturation_scale=1.08, lightness_shift=-0.12)
    text_rgb = _adjust_lightness(red, green, blue, saturation_scale=0.45, lightness_shift=0.28)
    return AccentThemeTokens(
        accent=_hex_from_rgb(red, green, blue),
        accent_2=_hex_from_rgb(*accent_2_rgb),
        accent_soft=_rgba(red, green, blue, 0.18),
        accent_border=_rgba(red, green, blue, 0.35),
        accent_text=_hex_from_rgb(*text_rgb),
    )
