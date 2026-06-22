
"""
core/i18n.py — Internationalization (i18n) Manager
Provides translation support for the UI.
"""
import json
import os
import logging
import threading

from .qt_dispatch import run_on_qt_main_thread
from .utils import get_resource_path

logger = logging.getLogger("SnapDownloader.i18n")

LANG_DIR = get_resource_path("lang")

class I18nManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.current_lang = "ar"  # Default to Arabic
        self.translations = {}
        self.language_labels = {
            "ar": "Arabic",
            "en": "English",
            "es": "Spanish",
            "fr": "French",
        }
        self.load_translations()

    def load_translations(self):
        if not os.path.exists(LANG_DIR):
            os.makedirs(LANG_DIR, exist_ok=True)
        self._ensure_builtin_languages()

        with self._lock:
            lang_code = self.current_lang

        path = os.path.join(LANG_DIR, f"{lang_code}.json")
        translations = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    translations = payload
            except Exception as exc:
                logger.error(f"Failed to load translations: {exc}")
        else:
            logger.warning(f"Translation file not found: {path}")
        with self._lock:
            self.translations = translations

    def set_language(self, lang_code: str):
        target = str(lang_code or "").strip().lower()
        if target not in self.language_labels:
            target = "en"
        with self._lock:
            self.current_lang = target
        self.load_translations()

        def _apply_layout_direction():
            try:
                from core.qt_compat import QApplication, Qt

                app = QApplication.instance()
                if app:
                    if target == "ar":
                        app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
                    else:
                        app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            except Exception as exc:
                logger.error(f"Failed to set layout direction: {exc}")

        queued = run_on_qt_main_thread(_apply_layout_direction)
        if not queued and threading.current_thread() is threading.main_thread():
            _apply_layout_direction()

    def tr(self, text: str) -> str:
        with self._lock:
            return self.translations.get(text, text)

    def available_languages(self) -> dict:
        return {code: self.tr(label) for code, label in self.language_labels.items()}

    def _ensure_builtin_languages(self):
        builtins = {
            "ar": self._ar_payload(),
            "en": self._en_payload(),
            "es": self._es_payload(),
            "fr": self._fr_payload(),
        }
        for code, payload in builtins.items():
            path = os.path.join(LANG_DIR, f"{code}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing_payload = json.load(f)
                    if not isinstance(existing_payload, dict):
                        existing_payload = {}
                except Exception as exc:
                    logger.error(f"Failed to read language file {path}: {exc}")
                    existing_payload = {}
                merged_payload = dict(payload)
                # Keep user/custom overrides while backfilling any newly added keys.
                merged_payload.update(existing_payload)
                if merged_payload == existing_payload:
                    continue
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(merged_payload, f, indent=2, ensure_ascii=False)
                except Exception as exc:
                    logger.error(f"Failed to update language file {path}: {exc}")
                continue
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                logger.error(f"Failed to create default language {code}: {exc}")



    def _ar_payload(self):
        return {
            "Ready": "جاهز",
            "Analyzing...": "جاري التحليل...",
            "Analyze Playlist": "تحليل قائمة التشغيل",
            "Download Selected": "تحميل المختار",
            "Fetch full playlists, select specific videos, and download in batch": "اجلب قوائم التشغيل كاملة، واختر مقاطع محددة، ثم حمّلها دفعة واحدة",
            "Fetching playlist data...": "جارِ جلب بيانات قائمة التشغيل...",
            "Total Size: --": "الحجم الكلي: --",
            "Playlist is empty or invalid.": "قائمة التشغيل فارغة أو غير صالحة.",
            "Playlist loaded successfully": "تم تحميل قائمة التشغيل بنجاح",
            "Toggle Theme": "تبديل المظهر",
            "Toggle Dark/Light": "تبديل الداكن/الفاتح",
            "Settings": "إعدادات",
            "Downloads": "تحميلات",
            "Search": "بحث",
            "Browser": "المتصفح",
            "Add Selected to Queue": "إضافة المختار للطابور",
            "Add {count} to Queue": "إضافة {count} للطابور",
            "Download Options": "خيارات التحميل",
            "Download as Video": "تحميل كفيديو",
            "Download as Audio": "تحميل كصوت",
            "Deep Analyze": "تحليل عميق",
            "AI Discovery: Found {count} videos on this page!": "اكتشاف ذكي: تم العثور على {count} فيديو في هذه الصفحة!",
            "Download All Found": "تحميل الكل",
            "AI Discovery: Searching for videos...": "اكتشاف ذكي: جاري البحث عن فيديوهات...",
            "Live Stream (HLS/DASH)": "بث مباشر (HLS/DASH)",
            "Media File": "ملف وسائط",
            "Streaming Chunk": "قطعة بث",
            "Embedded Media": "وسائط مدمجة",
            "Download Now": "تحميل الآن",
            "Clear detected links": "مسح الروابط المكتشفة",
            "Detected Streams": "المقاطع المكتشفة",
            "Download This Page": "تحميل هذه الصفحة",
            "Download Linked Media": "تحميل الرابط المختار",
            "Enter URL or search...": "اكتب رابط أو ابحث...",
            "Scan": "فحص",
            "File": "ملف",
            "Stream": "بث",
            "Live Browser": "متصفح حي",
            "Media Queue": "طابور الوسائط",
            "Ready to browse, scan, and queue media links.": "جاهز للتصفح والفحص وإضافة روابط الوسائط للطابور.",
            "Bulk Download": "تحميل متعدد",
            "Browse pages, detect media links, and send them straight to the queue": "تصفح الصفحات، واكتشف روابط الوسائط، وأرسلها مباشرة إلى الطابور",
            "Detected Media": "الوسائط المكتشفة",
            "Browser Workspace": "مساحة عمل المتصفح",
            "Browser Inspector": "لوحة المتصفح",
            "Back": "رجوع",
            "Forward": "تقدم",
            "Reload": "إعادة تحميل",
            "Home": "الرئيسية",
            "Add Current Page": "إضافة الصفحة الحالية",
            "Add Page": "إضافة الصفحة",
            "Scan Media": "فحص الوسائط",
            "Scan": "فحص",
            "Add Detected": "إرسال للمجمع",
            "Collect Detected": "إرسال للمجمع",
            "Analyze Detected": "تحليل المكتشف",
            "Send To Bulk Download": "إرسال إلى التحميل المتعدد",
            "Copy URL": "نسخ الرابط",
            "Open": "فتح",
            "Enter a page URL and press Enter": "اكتب رابط الصفحة واضغط Enter",
            "Embedded browser is ready.": "المتصفح المدمج جاهز.",
            "Qt WebEngine is not available in this build, so the smart browser runs in link collection mode only.": "Qt WebEngine غير متاح في هذه النسخة، لذلك يعمل المتصفح الذكي في وضع تجميع الروابط فقط.",
            "Detected media links will appear here...": "روابط الوسائط المكتشفة ستظهر هنا...",
            "Keep browsing here, then hand off detected links to Bulk Download when ready.": "واصل التصفح هنا، ثم أرسل الروابط المكتشفة إلى التحميل المتعدد عندما تصبح جاهزة.",
            "Page URL": "رابط الصفحة",
            "No page loaded yet.": "لا توجد صفحة محملة بعد.",
            "Current Page": "الصفحة الحالية",
            "No page URL available yet.": "لا يوجد رابط صفحة متاح بعد.",
            "Current page URL copied to clipboard.": "تم نسخ رابط الصفحة الحالية إلى الحافظة.",
            "Detected": "المكتشف",
            "{} links ready": "{} روابط جاهزة",
            "{} media candidates": "{} مرشح وسائط",
            "Current Title": "عنوان الصفحة",
            "No page title yet.": "لا يوجد عنوان صفحة بعد.",
            "Current Host": "المضيف الحالي",
            "No host": "لا يوجد مضيف",
            "Mode": "الوضع",
            "Live Session": "جلسة حية",
            "Quick Actions": "إجراءات سريعة",
            "Recent Pages": "الصفحات الأخيرة",
            "Saved Pages": "الصفحات المحفوظة",
            "Save Page": "حفظ الصفحة",
            "Download This Page": "تحميل هذه الصفحة",
            "Current page was sent for analysis.": "تم إرسال الصفحة الحالية للتحليل.",
            "Current page was saved to favorites.": "تم حفظ الصفحة الحالية في المفضلة.",
            "Network Sniffer": "ملتقط الشبكة",
            "Sniffer": "الملتقط",
            "Sniffer Idle": "الملتقط خامل",
            "Sniffer Off": "الملتقط متوقف",
            "Sniffer Live": "الملتقط نشط",
            "Network sniffer unavailable in this build.": "ملتقط الشبكة غير متاح في هذه النسخة.",
            "Watching media requests while pages load.": "تتم مراقبة طلبات الوسائط أثناء تحميل الصفحات.",
            "Link Collector Mode": "وضع تجميع الروابط",
            "Waiting": "في الانتظار",
            "Loading {progress}%": "جارٍ التحميل {progress}%",
            "P:{playlists} V:{videos} A:{audio}": "ق:{playlists} ف:{videos} ص:{audio}",
            "Enter a valid http(s) URL first.": "اكتب رابط http(s) صالح أولاً.",
            "Embedded browser is unavailable, so the URL was added as a candidate link.": "المتصفح المدمج غير متاح، لذلك أُضيف الرابط كمرشح.",
            "Opening page...": "جارٍ فتح الصفحة...",
            "The page failed to load.": "فشل تحميل الصفحة.",
            "Page loaded. You can scan media or add the current page to the queue.": "تم تحميل الصفحة. يمكنك فحص الوسائط أو إضافة الصفحة الحالية للطابور.",
            "Current page URL added to bulk links.": "تمت إضافة رابط الصفحة الحالية.",
            "Embedded browser is unavailable, so only the typed link can be collected here.": "المتصفح المدمج غير متاح، لذلك لا يمكن جمع إلا الرابط المكتوب هنا.",
            "Scan completed: {count} media links detected.": "اكتمل الفحص: تم اكتشاف {count} رابط وسائط.",
            "{} links": "{} روابط",
            "Detected media list cleared.": "تم مسح قائمة الوسائط المكتشفة.",
            "No detected media links to collect yet.": "لا توجد روابط وسائط مكتشفة لإرسالها بعد.",
            "Detected media links were sent to Bulk Download.": "تم إرسال روابط الوسائط المكتشفة إلى التحميل المتعدد.",
            "No detected media links to analyze yet.": "لا توجد روابط وسائط مكتشفة لتحليلها بعد.",
            "Detected media links were sent for analysis.": "تم إرسال روابط الوسائط المكتشفة للتحليل.",
            "No media loaded yet": "لا يوجد ملف محمل بعد",
            "Volume": "الصوت",
            "Fullscreen": "ملء الشاشة",
            "Pause": "إيقاف مؤقت",
            "Playing": "قيد التشغيل",
            "Unknown file": "ملف غير معروف",
            "Previous": "السابق",
            "Next": "التالي",
            "Tools": "أدوات",
            "Import Links": "استيراد الروابط",
            "Export CSV": "تصدير CSV",
            "Export JSON": "تصدير JSON",
            "Theme": "المظهر",
            "Language": "اللغة",
            "Arabic": "العربية",
            "English": "الإنجليزية",
            "Spanish": "الإسبانية",
            "French": "الفرنسية",
            "Video": "فيديو",
            "Audio": "صوت",
            "Manage multiple links and playlists efficiently": "إدارة الروابط المتعددة وقوائم التشغيل بكفاءة عالية",
            "Paste one or more video links below. Add them to the queue, then start downloading.": "الصق رابط فيديو أو أكثر بالأسفل. أضفهم للطابور، ثم ابدأ التحميل.",
            "Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All": "اكتب أكثر من لغة مفصولة بفاصلة مثل: English,ar أو اختر All",
            "Mode:": "الوضع:",
            "Format:": "الصيغة:",
            "Quality:": "الجودة:",
            "Subtitles:": "الترجمة:",
            "Paste multiple links here, one per line...": "الصق عدة روابط هنا، رابط في كل سطر...",
            "Analyze and Add to Queue": "تحليل وإضافة للطابور",
            "Add Links to Queue": "إضافة الروابط للطابور",
            "Run Queue": "تشغيل الطابور",
            "Start Downloading": "بدء التحميل",
            "Queue first, then download": "أضف للطابور أولاً، ثم حمّل",
            "0 links ready": "لا توجد روابط جاهزة",
            "1 link ready": "رابط واحد جاهز",
            "{count} links ready": "{count} روابط جاهزة",
            "Resume": "استكمال",
            "Browse": "استعراض",
            "Accelerate (aria2)": "تسريع (aria2)",
            "No action (Default)": "لا تفعل شيئًا (افتراضي)",
            "Open download folder": "فتح مجلد الحفظ",
            "Play notification sound": "تشغيل صوت تنبيه",
            "Run custom script": "تشغيل سكربت مخصص",
            "Transcribe audio to text": "تفريغ الصوت إلى نص",
            "Unlimited": "غير محدود",
            "Script path (.py/.ps1/.bat/.cmd)": "مسار السكربت (.py/.ps1/.bat/.cmd)",
            "Download speed per file (KB/s):": "سرعة التحميل لكل ملف (KB/s):",
            "Post-download script (optional):": "سكريبت ما بعد التحميل (اختياري):",
            "Speed:": "السرعة:",
            "Size:": "الحجم:",
            "ETA:": "الوقت المتبقي:",
            "Downloading": "جاري التحميل",
            "Completed": "مكتمل",
            "Queued": "في الانتظار",
            "Completed downloads": "المكتمل",
            "Active": "نشط",
            "Scheduled": "مجدول",
            "Clear completed": "مسح المكتمل",
            "All": "الكل",
            "Paused": "متوقف مؤقتًا",
            "Failed": "فشل",
            "Cancelled": "ملغى",
            "Search downloads...": "ابحث في التحميلات...",
            "Download completed successfully!": "اكتمل التحميل بنجاح!",
            "Download failed!": "فشل التحميل!",
            "Download paused": "تم الإيقاف مؤقتًا",
            "Downloading...": "جاري التحميل...",
            "By channel": "بواسطة القناة",
            "Settings saved for UI language: {language}": "تم حفظ لغة الواجهة: {language}",
            "Language switched to {language}": "تم تغيير اللغة إلى: {language}",
            "Invalid URL": "الرابط غير صالح",
            "Failed to open link: {error}": "تعذر فتح الرابط: {error}",
            "No subscriptions available to export right now": "لا توجد اشتراكات لتصديرها حاليًا",
            "Subscriptions exported successfully": "تم تصدير الاشتراكات بنجاح",
            "Failed to export subscriptions: {error}": "تعذر تصدير الاشتراكات: {error}",
            "Subscriptions imported successfully": "تم استيراد الاشتراكات بنجاح",
            "Failed to import subscriptions: {error}": "تعذر استيراد الاشتراكات: {error}",
        }

    def _en_payload(self):
        return {
            "Ready": "Ready",
            "Analyzing...": "Analyzing...",
            "Analyze Playlist": "Analyze Playlist",
            "Download Selected": "Download Selected",
            "Fetch full playlists, select specific videos, and download in batch": "Fetch full playlists, select specific videos, and download in batch",
            "Fetching playlist data...": "Fetching playlist data...",
            "Total Size: --": "Total Size: --",
            "Playlist is empty or invalid.": "Playlist is empty or invalid.",
            "Playlist loaded successfully": "Playlist loaded successfully",
            "Toggle Theme": "Toggle Theme",
            "Toggle Dark/Light": "Toggle Dark/Light",
            "Settings": "Settings",
            "Downloads": "Downloads",
            "Search": "Search",
            "Browser": "Browser",
            "Add Selected to Queue": "Add Selected to Queue",
            "Add {count} to Queue": "Add {count} to Queue",
            "Download Options": "Download Options",
            "Download as Video": "Download as Video",
            "Download as Audio": "Download as Audio",
            "Deep Analyze": "Deep Analyze",
            "AI Discovery: Found {count} videos on this page!": "AI Discovery: Found {count} videos on this page!",
            "Download All Found": "Download All Found",
            "AI Discovery: Searching for videos...": "AI Discovery: Searching for videos...",
            "Live Stream (HLS/DASH)": "Live Stream (HLS/DASH)",
            "Media File": "Media File",
            "Streaming Chunk": "Streaming Chunk",
            "Embedded Media": "Embedded Media",
            "Download Now": "Download Now",
            "Clear detected links": "Clear detected links",
            "Detected Streams": "Detected Streams",
            "Download This Page": "Download This Page",
            "Download Linked Media": "Download Linked Media",
            "Enter URL or search...": "Enter URL or search...",
            "Scan": "Scan",
            "File": "File",
            "Stream": "Stream",
            "Live Browser": "Live Browser",
            "Media Queue": "Media Queue",
            "Ready to browse, scan, and queue media links.": "Ready to browse, scan, and queue media links.",
            "Bulk Download": "Bulk Download",
            "Browse pages, detect media links, and send them straight to the queue": "Browse pages, detect media links, and send them straight to the queue",
            "Detected Media": "Detected Media",
            "Browser Workspace": "Browser Workspace",
            "Browser Inspector": "Browser Inspector",
            "Back": "Back",
            "Forward": "Forward",
            "Reload": "Reload",
            "Home": "Home",
            "Add Current Page": "Add Current Page",
            "Add Page": "Add Page",
            "Scan Media": "Scan Media",
            "Scan": "Scan",
            "Add Detected": "Add Detected",
            "Collect Detected": "Collect Detected",
            "Analyze Detected": "Analyze Detected",
            "Send To Bulk Download": "Send To Bulk Download",
            "Copy URL": "Copy URL",
            "Open": "Open",
            "Enter a page URL and press Enter": "Enter a page URL and press Enter",
            "Embedded browser is ready.": "Embedded browser is ready.",
            "Qt WebEngine is not available in this build, so the smart browser runs in link collection mode only.": "Qt WebEngine is not available in this build, so the smart browser runs in link collection mode only.",
            "Detected media links will appear here...": "Detected media links will appear here...",
            "Keep browsing here, then hand off detected links to Bulk Download when ready.": "Keep browsing here, then hand off detected links to Bulk Download when ready.",
            "Page URL": "Page URL",
            "No page loaded yet.": "No page loaded yet.",
            "Current Page": "Current Page",
            "No page URL available yet.": "No page URL available yet.",
            "Current page URL copied to clipboard.": "Current page URL copied to clipboard.",
            "Detected": "Detected",
            "{} links ready": "{} links ready",
            "{} media candidates": "{} media candidates",
            "Current Title": "Current Title",
            "No page title yet.": "No page title yet.",
            "Current Host": "Current Host",
            "No host": "No host",
            "Mode": "Mode",
            "Live Session": "Live Session",
            "Quick Actions": "Quick Actions",
            "Recent Pages": "Recent Pages",
            "Saved Pages": "Saved Pages",
            "Save Page": "Save Page",
            "Download This Page": "Download This Page",
            "Current page was sent for analysis.": "Current page was sent for analysis.",
            "Current page was saved to favorites.": "Current page was saved to favorites.",
            "Network Sniffer": "Network Sniffer",
            "Sniffer": "Sniffer",
            "Sniffer Idle": "Sniffer Idle",
            "Sniffer Off": "Sniffer Off",
            "Sniffer Live": "Sniffer Live",
            "Network sniffer unavailable in this build.": "Network sniffer unavailable in this build.",
            "Watching media requests while pages load.": "Watching media requests while pages load.",
            "Link Collector Mode": "Link Collector Mode",
            "Waiting": "Waiting",
            "Loading {progress}%": "Loading {progress}%",
            "P:{playlists} V:{videos} A:{audio}": "P:{playlists} V:{videos} A:{audio}",
            "Enter a valid http(s) URL first.": "Enter a valid http(s) URL first.",
            "Embedded browser is unavailable, so the URL was added as a candidate link.": "Embedded browser is unavailable, so the URL was added as a candidate link.",
            "Opening page...": "Opening page...",
            "The page failed to load.": "The page failed to load.",
            "Page loaded. You can scan media or add the current page to the queue.": "Page loaded. You can scan media or add the current page to the queue.",
            "Current page URL added to bulk links.": "Current page URL added to bulk links.",
            "Embedded browser is unavailable, so only the typed link can be collected here.": "Embedded browser is unavailable, so only the typed link can be collected here.",
            "Scan completed: {count} media links detected.": "Scan completed: {count} media links detected.",
            "{} links": "{} links",
            "Detected media list cleared.": "Detected media list cleared.",
            "No detected media links to collect yet.": "No detected media links to collect yet.",
            "Detected media links were sent to Bulk Download.": "Detected media links were sent to Bulk Download.",
            "No detected media links to analyze yet.": "No detected media links to analyze yet.",
            "Detected media links were sent for analysis.": "Detected media links were sent for analysis.",
            "No media loaded yet": "No media loaded yet",
            "Volume": "Volume",
            "Fullscreen": "Fullscreen",
            "Pause": "Pause",
            "Playing": "Playing",
            "Unknown file": "Unknown file",
            "Previous": "Previous",
            "Next": "Next",
            "Tools": "Tools",
            "Import Links": "Import Links",
            "Export CSV": "Export CSV",
            "Export JSON": "Export JSON",
            "Theme": "Theme",
            "Language": "Language",
            "Arabic": "Arabic",
            "English": "English",
            "Spanish": "Spanish",
            "French": "French",
            "Video": "Video",
            "Audio": "Audio",
            "Manage multiple links and playlists efficiently": "Manage multiple links and playlists efficiently",
            "Paste one or more video links below. Add them to the queue, then start downloading.": "Paste one or more video links below. Add them to the queue, then start downloading.",
            "Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All": "Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All",
            "Mode:": "Mode:",
            "Format:": "Format:",
            "Quality:": "Quality:",
            "Subtitles:": "Subtitles:",
            "Paste multiple links here, one per line...": "Paste multiple links here, one per line...",
            "Analyze and Add to Queue": "Analyze and Add to Queue",
            "Add Links to Queue": "Add Links to Queue",
            "Run Queue": "Run Queue",
            "Start Downloading": "Start Downloading",
            "Queue first, then download": "Queue first, then download",
            "0 links ready": "0 links ready",
            "1 link ready": "1 link ready",
            "{count} links ready": "{count} links ready",
            "Resume": "Resume",
            "Browse": "Browse",
            "Accelerate (aria2)": "Accelerate (aria2)",
            "No action (Default)": "No action (Default)",
            "Open download folder": "Open download folder",
            "Play notification sound": "Play notification sound",
            "Run custom script": "Run custom script",
            "Transcribe audio to text": "Transcribe audio to text",
            "Unlimited": "Unlimited",
            "Script path (.py/.ps1/.bat/.cmd)": "Script path (.py/.ps1/.bat/.cmd)",
            "Download speed per file (KB/s):": "Download speed per file (KB/s):",
            "Post-download script (optional):": "Post-download script (optional):",
            "Speed:": "Speed:",
            "Size:": "Size:",
            "ETA:": "ETA:",
            "Downloading": "Downloading",
            "Completed": "Completed",
            "Queued": "Queued",
            "Completed downloads": "Completed",
            "Active": "Active",
            "Scheduled": "Scheduled",
            "Clear completed": "Clear completed",
            "All": "All",
            "Paused": "Paused",
            "Failed": "Failed",
            "Cancelled": "Cancelled",
            "Search downloads...": "Search downloads...",
            "Download completed successfully!": "Download completed successfully!",
            "Download failed!": "Download failed!",
            "Download paused": "Download paused",
            "Downloading...": "Downloading...",
            "By channel": "By channel",
            "Settings saved for UI language: {language}": "Settings saved for UI language: {language}",
            "Language switched to {language}": "Language switched to {language}",
            "Invalid URL": "Invalid URL",
            "Failed to open link: {error}": "Failed to open link: {error}",
            "No subscriptions available to export right now": "No subscriptions available to export right now",
            "Subscriptions exported successfully": "Subscriptions exported successfully",
            "Failed to export subscriptions: {error}": "Failed to export subscriptions: {error}",
            "Subscriptions imported successfully": "Subscriptions imported successfully",
            "Failed to import subscriptions: {error}": "Failed to import subscriptions: {error}",
            "الإعدادات": "Settings",
            "تطبيق الإعدادات": "Apply Settings",
            "اختيار مجلد": "Choose Folder",
            "اكتب أكثر من لغة مفصولة بفاصلة مثل: English,ar أو اختر All": "Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All",
            "الإعدادات والصيغ": "Formats and settings",
            "لصق الرابط": "Paste link",
            "مسح السجل": "Clear history",
            "عنوان الفيديو": "Video title",
            "by القناة": "By channel",
            "لا تفعل شيء (Default)": "No action (Default)",
            "فتح مجلد الحفظ": "Open download folder",
            "تشغيل صوت تنبيه": "Play notification sound",
            "تشغيل سكربت مخصص": "Run custom script",
            "تفريغ صوت إلى نص (Transcribe)": "Transcribe audio to text",
            "غير محدود": "Unlimited",
            "مسار سكربت .py/.ps1/.bat/.cmd": "Script path (.py/.ps1/.bat/.cmd)",
            "سرعة التحميل لكل ملف (KB/s):": "Download speed per file (KB/s):",
            "سكريبت ما بعد التحميل (اختياري):": "Post-download script (optional):",
            "جاهز": "Ready",
            "الكل": "All",
            "في الانتظار": "Queued",
            "جاري التحميل": "Downloading",
            "متوقف مؤقتاً": "Paused",
            "فشل": "Failed",
            "ملغى": "Cancelled",
            "ابحث في التحميلات...": "Search downloads...",
        }

    def _es_payload(self):
        return {
            "Ready": "Listo",
            "Analyzing...": "Analizando...",
            "Analyze Playlist": "Analizar lista",
            "Download Selected": "Descargar seleccionados",
            "Fetch full playlists, select specific videos, and download in batch": "Obtén listas completas, selecciona videos específicos y descarga en lote",
            "Fetching playlist data...": "Obteniendo datos de la lista...",
            "Total Size: --": "Tamaño total: --",
            "Playlist is empty or invalid.": "La lista está vacía o no es válida.",
            "Playlist loaded successfully": "Lista cargada correctamente",
            "Toggle Theme": "Cambiar tema",
            "Toggle Dark/Light": "Alternar oscuro/claro",
            "Settings": "Configuración",
            "Downloads": "Descargas",
            "Search": "Buscar",
            "Browser": "Navegador",
            "Bulk Download": "Descarga masiva",
            "No media loaded yet": "Aun no hay contenido cargado",
            "Volume": "Volumen",
            "Fullscreen": "Pantalla completa",
            "Pause": "Pausa",
            "Playing": "Reproduciendo",
            "Unknown file": "Archivo desconocido",
            "Previous": "Anterior",
            "Next": "Siguiente",
            "Tools": "Herramientas",
            "Import Links": "Importar enlaces",
            "Export CSV": "Exportar CSV",
            "Export JSON": "Exportar JSON",
            "Theme": "Tema",
            "Language": "Idioma",
            "Arabic": "Árabe",
            "English": "Inglés",
            "Spanish": "Español",
            "French": "Francés",
        }

    def _fr_payload(self):
        return {
            "Ready": "Prêt",
            "Analyzing...": "Analyse en cours...",
            "Analyze Playlist": "Analyser la playlist",
            "Download Selected": "Télécharger la sélection",
            "Fetch full playlists, select specific videos, and download in batch": "Récupérez des playlists complètes, sélectionnez des vidéos et téléchargez en lot",
            "Fetching playlist data...": "Récupération des données de la playlist...",
            "Total Size: --": "Taille totale : --",
            "Playlist is empty or invalid.": "La playlist est vide ou invalide.",
            "Playlist loaded successfully": "Playlist chargée avec succès",
            "Toggle Theme": "Basculer le thème",
            "Toggle Dark/Light": "Mode sombre/clair",
            "Settings": "Paramètres",
            "Downloads": "Téléchargements",
            "Search": "Recherche",
            "Browser": "Navigateur",
            "Bulk Download": "Telechargement en lot",
            "No media loaded yet": "Aucun media charge pour le moment",
            "Volume": "Volume",
            "Fullscreen": "Plein ecran",
            "Pause": "Pause",
            "Playing": "Lecture",
            "Unknown file": "Fichier inconnu",
            "Previous": "Precedent",
            "Next": "Suivant",
            "Tools": "Outils",
            "Import Links": "Importer des liens",
            "Export CSV": "Exporter CSV",
            "Export JSON": "Exporter JSON",
            "Theme": "Thème",
            "Language": "Langue",
            "Arabic": "Arabe",
            "English": "Anglais",
            "Spanish": "Espagnol",
            "French": "Français",
        }

# Singleton
i18n = I18nManager()

def _(text: str) -> str:
    return i18n.tr(text)



