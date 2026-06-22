import logging
import os
from PySide6.QtCore import Qt, Signal, QUrl, QSize, QTimer
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLineEdit, QPushButton,
    QLabel, QStackedWidget, QSplitter, QProgressBar, QListWidget,
    QListWidgetItem, QMenu, QSizePolicy, QTabWidget, QTabBar
)

from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor, QWebEngineProfile, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from ui.views.base_view import BaseView
from core.i18n import _
from ui.widgets import AnimatedButton, add_soft_shadow
import qtawesome as qta

logger = logging.getLogger("SnapDownloader.Browser")

class MediaRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """
    Advanced network sniffer that detects hidden media streams and protected content.
    Integrated with a basic Ad-Blocker.
    """
    media_detected = Signal(dict)
    ad_blocked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Professional Ad-block patterns
        self.ad_patterns = {
            'doubleclick.net', 'googleadservices.com', 'adnxs.com', 
            'facebook.com/tr/', 'amazon-adsystem.com', 'popads.net',
            'adservice.google', 'analytics.google.com', 'googletagmanager.com'
        }

    def interceptRequest(self, info):
        url = info.requestUrl().toString()
        resource_type = info.resourceType()
        
        # 1. Professional Ad-Blocker (Skip common ad domains)
        if any(p in url for p in self.ad_patterns):
            return

        # 2. Deep Content Sniffing with Noise Reduction
        ext = url.split('?')[0].split('.')[-1].lower()
        
        is_media = False
        m_type = "File"
        
        # Detect HLS/DASH (High Priority)
        if ext in {'m3u8', 'mpd'}:
            is_media = True
            m_type = "Live Stream (HLS/DASH)"
        # Detect Subtitles
        elif ext in {'vtt', 'srt', 'ass', 'ssa'}:
            is_media = True
            m_type = "Subtitle"
        # Detect direct files
        elif ext in {'mp4', 'mkv', 'webm', 'ts', 'aac', 'm4a', 'm4s', 'mov', 'avi'}:

            # Ignore very small segments or chunks to avoid noise
            is_media = True
            m_type = "Media File"
        # Intelligent Filter for YouTube/Social segments
        elif 'videoplayback' in url or 'googlevideo' in url:
            # We only catch the first segment to identify the stream, ignoring the rest
            if 'range=0-' in url or 'index=0' in url or 'itag=' in url:
                is_media = True
                m_type = "Streaming Source"
            else:
                return # Silent drop of noisy chunks
        
        elif resource_type == QWebEngineUrlRequestInterceptor.ResourceType.Media:
            is_media = True
            m_type = "Embedded Media"

        if is_media:
            context = {
                "url": url,
                "type": m_type,
                "resource_type": str(resource_type),
            }
            self.media_detected.emit(context)



class PremiumWebView(QWebEngineView):
    analyze_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPage(QWebEnginePage(self))
        # Handle custom schemes like snapdl://
        self.page().urlChanged.connect(self._check_custom_scheme)
        # Enable common features

        settings = self.settings()
        settings.setAttribute(settings.WebAttribute.PluginsEnabled, True)
        settings.setAttribute(settings.WebAttribute.FullScreenSupportEnabled, True)
        settings.setAttribute(settings.WebAttribute.AllowRunningInsecureContent, True)
        settings.setAttribute(settings.WebAttribute.JavascriptCanOpenWindows, True)

    def _check_custom_scheme(self, url):
        url_str = url.toString()
        if url_str.startswith("snapdl://analyze"):
            from urllib.parse import urlparse, parse_qs
            query = urlparse(url_str).query
            params = parse_qs(query)
            media_url = params.get('url', [None])[0]
            if media_url:
                self.analyze_requested.emit(media_url)

    def contextMenuEvent(self, event):


        menu = self.createStandardContextMenu()
        
        # Add custom actions
        menu.addSeparator()
        
        # Action for the current page
        dl_page = QAction(qta.icon("fa5s.download", color="#6366F1"), _("Download This Page"), self)
        dl_page.triggered.connect(lambda: self.analyze_requested.emit(self.url().toString()))
        menu.addAction(dl_page)

        # Action for the link under cursor (if any)
        hit_data = self.page().contextMenuData()
        if hit_data.linkUrl().isValid():
            dl_link = QAction(qta.icon("fa5s.link", color="#10B981"), _("Download Linked Media"), self)
            link_url = hit_data.linkUrl().toString()
            dl_link.triggered.connect(lambda: self.analyze_requested.emit(link_url))
            menu.addAction(dl_link)



        menu.exec(event.globalPos())

class SmartBrowserView(BaseView):
    analyze_requested = Signal(str, dict) # URL, Context(headers, cookies, etc.)

    def __init__(self, main_window=None, parent=None):
        super().__init__(main_window, parent)
        self.detected_links = {} # Tab index -> Set of links
        self._current_page_detected_title = {} # Tab index -> Title
        self.setup_ui()
        self._setup_interceptor()
        # Create first tab
        self._add_new_tab(QUrl("about:blank"), _("New Tab"))


    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- 1. Top Control Bar (Tabs & Nav) ---
        self.top_bar = QFrame()
        self.top_bar.setFixedHeight(95)
        self.top_bar.setStyleSheet("background-color: #1E293B; border-bottom: 1px solid #334155;")
        top_layout = QVBoxLayout(self.top_bar)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # Tab Bar
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        
        # Style the tab widget
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                background: #0F172A; color: #94A3B8; padding: 8px 15px;
                border-top-left-radius: 8px; border-top-right-radius: 8px;
                margin-right: 2px; min-width: 120px; font-weight: bold;
            }
            QTabBar::tab:selected { background: #1E293B; color: white; border-bottom: 2px solid #6366F1; }
            QTabBar::close-button { image: url(close.png); subcontrol-position: right; }
        """)

        # Add "New Tab" button to the right of tabs
        self.new_tab_btn = QPushButton("+")
        self.new_tab_btn.setFixedSize(30, 30)
        self.new_tab_btn.setStyleSheet("background: transparent; color: white; font-size: 20px; font-weight: bold;")
        self.new_tab_btn.clicked.connect(lambda: self._add_new_tab(QUrl("about:blank"), _("New Tab")))
        self.tabs.setCornerWidget(self.new_tab_btn, Qt.Corner.TopRightCorner)

        top_layout.addWidget(self.tabs)

        # Nav & Address Bar
        self.nav_frame = QFrame()
        self.nav_frame.setFixedHeight(50)
        nav_layout = QHBoxLayout(self.nav_frame)
        nav_layout.setContentsMargins(15, 0, 15, 5)
        nav_layout.setSpacing(12)

        self.back_btn = self._create_nav_btn("fa5s.arrow-left", _("Back"), self._go_back)
        self.fwd_btn = self._create_nav_btn("fa5s.arrow-right", _("Forward"), self._go_forward)
        self.reload_btn = self._create_nav_btn("fa5s.redo", _("Reload"), self._reload)
        self.home_btn = self._create_nav_btn("fa5s.home", _("Home"), self._go_home)

        url_container = QFrame()
        url_container.setStyleSheet("background-color: #0F172A; border: 1px solid #334155; border-radius: 10px;")
        url_layout = QHBoxLayout(url_container)
        url_layout.setContentsMargins(10, 0, 10, 0)
        url_layout.setSpacing(5)

        lock_icon = QLabel()
        lock_icon.setPixmap(qta.icon("fa5s.lock", color="#64748B").pixmap(12, 12))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(_("Enter URL or search..."))
        self.url_input.setStyleSheet("background: transparent; border: none; color: #F8FAFC; padding: 6px 0; font-size: 13px;")
        self.url_input.returnPressed.connect(self._on_url_entered)
        url_layout.addWidget(lock_icon)
        url_layout.addWidget(self.url_input, 1)

        self.scan_btn = QPushButton(_("Scan"))
        self.scan_btn.setIcon(qta.icon("fa5s.satellite-dish", color="white"))
        self.scan_btn.setMinimumHeight(34)
        self.scan_btn.setStyleSheet("background-color: #6366F1; color: white; border-radius: 8px; padding: 0 15px; font-weight: bold;")
        self.scan_btn.clicked.connect(self._manual_scan)

        nav_layout.addWidget(self.back_btn)
        nav_layout.addWidget(self.fwd_btn)
        nav_layout.addWidget(self.reload_btn)
        nav_layout.addWidget(self.home_btn)
        nav_layout.addWidget(url_container, 1)
        nav_layout.addWidget(self.scan_btn)

        top_layout.addWidget(self.nav_frame)
        layout.addWidget(self.top_bar)

        # --- 2. AI Discovery Bar ---
        self.discovery_bar = QFrame()
        self.discovery_bar.setFixedHeight(50)
        self.discovery_bar.setStyleSheet("background-color: #6366F1; border-bottom: 2px solid #4F46E5;")
        discovery_layout = QHBoxLayout(self.discovery_bar)
        discovery_layout.setContentsMargins(20, 0, 20, 0)
        self.discovery_lbl = QLabel(_("AI Discovery: Searching for videos..."))
        self.discovery_lbl.setStyleSheet("color: white; font-weight: bold;")
        self.bulk_dl_btn = QPushButton(_("Download All Found"))
        self.bulk_dl_btn.setIcon(qta.icon("fa5s.layer-group", color="#6366F1"))
        self.bulk_dl_btn.setStyleSheet("background: white; color: #6366F1; border-radius: 6px; padding: 5px 15px; font-weight: 800;")
        self.bulk_dl_btn.clicked.connect(self._on_bulk_dl_clicked)
        discovery_layout.addWidget(self.discovery_lbl)
        discovery_layout.addStretch(1)
        discovery_layout.addWidget(self.bulk_dl_btn)
        self.discovery_bar.hide()
        layout.addWidget(self.discovery_bar)

        # --- 3. Main Splitter ---
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setStyleSheet("QSplitter::handle { background: #1E293B; width: 2px; }")

        # Container for the current tab's WebView
        self.browser_stack = QStackedWidget()
        self.splitter.addWidget(self.browser_stack)

        # Sniffer Panel
        self.detect_panel = QFrame()
        self.detect_panel.setStyleSheet("background-color: #0F172A;")
        self.detect_panel.setMinimumWidth(280)
        panel_layout = QVBoxLayout(self.detect_panel)
        panel_layout.setContentsMargins(10, 15, 10, 10)
        panel_layout.setSpacing(15)
        
        status_row = QHBoxLayout()
        sniffer_icon = QLabel()
        sniffer_icon.setPixmap(qta.icon("fa5s.satellite-dish", color="#10B981").pixmap(16, 16))
        self.sniffer_status_lbl = QLabel(_("Sniffer Live"))
        self.sniffer_status_lbl.setStyleSheet("color: #10B981; font-weight: 900; font-size: 13px;")
        self.clear_sniffer_btn = QPushButton()
        self.clear_sniffer_btn.setIcon(qta.icon("fa5s.trash-alt", color="#64748B"))
        self.clear_sniffer_btn.setFixedSize(28, 28)
        self.clear_sniffer_btn.setStyleSheet("background: transparent; border: none;")
        self.clear_sniffer_btn.clicked.connect(self._clear_detected)
        status_row.addWidget(sniffer_icon)
        status_row.addWidget(self.sniffer_status_lbl)
        status_row.addStretch(1)
        status_row.addWidget(self.clear_sniffer_btn)
        panel_layout.addLayout(status_row)

        self.panel_hdr = QLabel(_("Detected Streams"))
        self.panel_hdr.setStyleSheet("color: #475569; font-size: 10px; font-weight: bold; text-transform: uppercase;")
        panel_layout.addWidget(self.panel_hdr)

        self.links_list = QListWidget()
        self.links_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.links_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; }
            QListWidget::item { margin-bottom: 8px; border-radius: 10px; }
            QListWidget::item:selected { background: rgba(99, 102, 241, 0.1); border: 1px solid rgba(99, 102, 241, 0.3); }
        """)
        self.links_list.itemSelectionChanged.connect(self._on_selection_changed)
        panel_layout.addWidget(self.links_list, 1)
        
        self.send_selected_btn = QPushButton(_("Add Selected to Queue"))
        self.send_selected_btn.setMinimumHeight(40)
        self.send_selected_btn.setIcon(qta.icon("fa5s.plus-circle", color="white"))
        self.send_selected_btn.setStyleSheet("background: #6366F1; color: white; border-radius: 8px; font-weight: bold;")
        self.send_selected_btn.clicked.connect(self._on_send_selected_clicked)
        self.send_selected_btn.hide()
        panel_layout.addWidget(self.send_selected_btn)

        self.dl_page_btn = QPushButton(_("Download This Page"))
        self.dl_page_btn.setMinimumHeight(45)
        self.dl_page_btn.setStyleSheet("background: #10B981; color: white; border-radius: 10px; font-weight: bold;")
        self.dl_page_btn.clicked.connect(self._on_download_page_clicked)
        panel_layout.addWidget(self.dl_page_btn)

        self.splitter.addWidget(self.detect_panel)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)

        layout.addWidget(self.splitter, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("QProgressBar { background: transparent; border: none; } QProgressBar::chunk { background: #6366F1; }")
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

    def _create_nav_btn(self, icon_name, tooltip, callback):
        btn = QPushButton()
        btn.setIcon(qta.icon(icon_name, color="#A1A1AA"))
        btn.setToolTip(tooltip)
        btn.setFixedSize(36, 36)
        btn.setStyleSheet("""
            QPushButton { background: transparent; border-radius: 10px; }
            QPushButton:hover { background: rgba(255,255,255,0.1); }
        """)
        btn.clicked.connect(callback)
        return btn

    def _setup_interceptor(self):
        # Use a persistent profile for the browser so cookies/sessions are saved
        profile = QWebEngineProfile.defaultProfile()
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        
        # Determine a path for browser data
        from core.database import get_app_data_dir
        browser_data_path = os.path.join(get_app_data_dir(), "browser_data")
        os.makedirs(browser_data_path, exist_ok=True)
        profile.setPersistentStoragePath(browser_data_path)
        profile.setCachePath(os.path.join(browser_data_path, "cache"))

        # Stage 3: Pro Ad-Blocker setup
        self.interceptor = MediaRequestInterceptor()
        self.interceptor.ad_patterns.update({
            'adnxs.com', 'bidswitch.net', 'criteo.com', 'openx.net',
            'pubmatic.com', 'rubiconproject.com', 'smartadserver.com'
        })
        self.interceptor.media_detected.connect(self._on_media_detected)
        profile.setUrlRequestInterceptor(self.interceptor)

    def _inject_media_detection_script(self, success):
        if not success: return
        view = self.browser_stack.currentWidget()
        if view is None:
            return
        script = """
        (function() {
            const results = [];
            const pageTitle = document.title;

            // 1. Intelligent Video Finder (Look for real titles)
            document.querySelectorAll('video, audio').forEach(el => {
                // Try to find the closest heading or title for this video
                let title = el.getAttribute('title') || el.getAttribute('alt');
                if (!title) {
                    const container = el.closest('div, section, article');
                    title = container?.querySelector('h1, h2, h3, .title')?.innerText || pageTitle;
                }

                if (el.src && el.src.startsWith('http')) {
                    results.push({url: el.src, type: 'Direct', title: title.trim()});
                }
                el.querySelectorAll('source').forEach(s => {
                    if (s.src && s.src.startsWith('http')) {
                        results.push({url: s.src, type: 'Source', title: title.trim()});
                    }
                });
            });

            // 2. Specialized YouTube Support
            if (window.location.hostname.includes('youtube.com')) {
                const ytTitle = document.querySelector('h1.ytd-video-primary-info-renderer')?.innerText 
                             || document.querySelector('.ytp-title-link')?.innerText;
                if (ytTitle) {
                    results.push({url: window.location.href, type: 'YouTube', title: ytTitle.trim()});
                }
            }

            // 3. Floating Download Button Injection (IDM Style)
            if (!document.getElementById('snap-pro-fab')) {
                const fab = document.createElement('div');
                fab.id = 'snap-pro-fab';
                fab.innerHTML = '📥 Download Video';
                fab.style.cssText = `
                    position: fixed; top: 10px; right: 10px; z-index: 999999;
                    background: #6366F1; color: white; padding: 10px 20px;
                    border-radius: 8px; font-weight: bold; cursor: pointer;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.2);
                    font-family: sans-serif; display: none; transition: all 0.3s;
                `;
                fab.onclick = () => { window.location.href = 'snapdl://analyze?url=' + encodeURIComponent(window.location.href); };
                document.body.appendChild(fab);

                // Show button if video exists
                const checkVideo = () => {
                    const hasVideo = document.querySelector('video');
                    fab.style.display = hasVideo ? 'block' : 'none';
                };
                setInterval(checkVideo, 2000);
            }

            return results;
        })();
        """
        view.page().runJavaScript(script, 0, self._handle_js_detection_result)


    def _handle_js_detection_result(self, result):
        if not result or not isinstance(result, list): return
        for item in result:
            if item.get('type') == 'PageMeta':
                self._current_page_detected_title = item.get('title')
                continue
            self._on_media_detected(item)



    def _on_selection_changed(self):
        count = len(self.links_list.selectedItems())
        self.send_selected_btn.setVisible(count > 0)
        self.send_selected_btn.setText(_("Add {count} to Queue").format(count=count))

    def _on_send_selected_clicked(self):
        items = self.links_list.selectedItems()
        for item in items:
            url = item.data(Qt.ItemDataRole.UserRole)
            title = item.data(Qt.ItemDataRole.DisplayRole) or "Media Stream"
            self._request_analysis(url, {"title": title})
        
        self.links_list.clearSelection()
        self._on_selection_changed()

    def _clean_title(self, title):
        if not title: return "Media Stream"
        import re
        # Remove common noise patterns
        noise = [
            r'\(Official Video\)', r'\[Official Video\]', r'Official Music Video',
            r'HD', r'1080p', r'4K', r'720p', r'Lyrics', r'Lyric Video',
            r'\(.*?Audio\)', r'\[.*?Audio\]'
        ]
        clean = title
        for pattern in noise:
            clean = re.sub(pattern, '', clean, flags=re.IGNORECASE)
        
        # Collapse multiple spaces and trim
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean or "Media Stream"

    def _on_media_detected(self, context):
        view = self.sender()
        if not isinstance(view, PremiumWebView):
            view = self.browser_stack.currentWidget()
        
        idx = self.browser_stack.indexOf(view)
        if idx < 0: return

        if idx not in self.detected_links:
            self.detected_links[idx] = set()
        
        url = context['url']
        if url in self.detected_links[idx]:
            return
        
        self.detected_links[idx].add(url)
        m_type = context['type']
        raw_title = context.get('title') or self._current_page_detected_title.get(idx) or "Media Stream"
        title = self._clean_title(raw_title)

        
        # Only update UI if this is the active tab
        if idx == self.tabs.currentIndex():
            self._update_discovery_bar()
            self._add_link_to_list(url, m_type, title)

    def _add_link_to_list(self, url, m_type, title):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, url)
        item.setData(Qt.ItemDataRole.DisplayRole, title)
        
        display_name = title
        if len(display_name) > 35: display_name = display_name[:32] + "..."
        
        widget = QWidget()
        w_layout = QVBoxLayout(widget)
        w_layout.setContentsMargins(8, 8, 8, 8)
        w_layout.setSpacing(4)
        
        t_row = QHBoxLayout()
        t_lbl = QLabel(f" {m_type}")
        t_lbl.setStyleSheet("background: rgba(99, 102, 241, 0.2); color: #818CF8; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold;")
        t_row.addWidget(t_lbl)
        t_row.addStretch(1)
        
        dl_btn = QPushButton()
        dl_btn.setIcon(qta.icon("fa5s.download", color="#10B981"))
        dl_btn.setFixedSize(24, 24)
        dl_btn.setStyleSheet("background: transparent; border: none;")
        dl_btn.clicked.connect(lambda: self._show_download_menu(dl_btn, url, title))
        t_row.addWidget(dl_btn)
        
        n_lbl = QLabel(display_name)
        n_lbl.setStyleSheet("color: #E2E8F0; font-weight: bold; font-size: 13px;")
        
        w_layout.addLayout(t_row)
        w_layout.addWidget(n_lbl)
        
        item.setSizeHint(widget.sizeHint())
        self.links_list.addItem(item)
        self.links_list.setItemWidget(item, widget)
        self.links_list.scrollToBottom()

    def _update_discovery_bar(self):
        idx = self.tabs.currentIndex()
        count = len(self.detected_links.get(idx, []))
        if count > 1:
            self.discovery_bar.show()
            self.discovery_lbl.setText(_("AI Discovery: Found {count} videos on this page!").format(count=count))
        else:
            self.discovery_bar.hide()

    def _show_download_menu(self, parent_btn, url, title):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: #1E293B; border: 1px solid #334155; color: white; padding: 5px; border-radius: 8px; }
            QMenu::item { padding: 8px 25px; border-radius: 4px; }
            QMenu::item:selected { background-color: #6366F1; }
        """)

        # Option 1: Video
        dl_video = QAction(qta.icon("fa5s.film", color="white"), _("Download as Video"), self)
        dl_video.triggered.connect(lambda: self._request_analysis(url, {"mode": "video", "title": title}))
        menu.addAction(dl_video)

        # Option 2: Audio
        dl_audio = QAction(qta.icon("fa5s.music", color="white"), _("Download as Audio"), self)
        dl_audio.triggered.connect(lambda: self._request_analysis(url, {"mode": "audio", "title": title}))
        menu.addAction(dl_audio)

        menu.addSeparator()

        # Option 3: Deep Analyze
        dl_analyze = QAction(qta.icon("fa5s.search-plus", color="white"), _("Deep Analyze"), self)
        dl_analyze.triggered.connect(lambda: self._request_analysis(url, {"title": title}))
        menu.addAction(dl_analyze)

        menu.exec(parent_btn.mapToGlobal(parent_btn.rect().bottomLeft()))


    def _on_url_entered(self):
        text = self.url_input.text().strip()
        if not text: return
        
        view = self.browser_stack.currentWidget()
        if not view: return
        
        if not (text.startswith("http://") or text.startswith("https://")):
            if "." in text and " " not in text:
                text = "https://" + text
            else:
                text = f"https://www.google.com/search?q={text}"
        view.load(QUrl(text))

    def _on_page_url_changed(self, url):
        view = self.sender()
        idx = self.browser_stack.indexOf(view)
        if idx < 0: return
        
        # Update tab title
        self.tabs.setTabText(idx, view.title() or _("Loading..."))
        self.tabs.setTabToolTip(idx, url.toString())
        
        # If this is current tab, update address bar and clear detection for new page
        if idx == self.tabs.currentIndex():
            self.url_input.setText(url.toString())
            self.detected_links[idx] = set()
            self.links_list.clear()
            self.discovery_bar.hide()

    def _on_load_progress(self, p):
        view = self.sender()
        if view == self.browser_stack.currentWidget():
            self.progress_bar.setValue(p)
            self.progress_bar.setVisible(p < 100)


    def _add_new_tab(self, qurl, title):
        web_view = PremiumWebView(self)
        web_view.analyze_requested.connect(self._request_analysis)
        web_view.page().urlChanged.connect(self._on_page_url_changed)
        web_view.page().loadProgress.connect(self._on_load_progress)
        web_view.loadFinished.connect(self._inject_media_detection_script)
        
        # Add to stack and tabs
        idx = self.browser_stack.addWidget(web_view)
        tab_idx = self.tabs.addTab(QWidget(), title)
        self.tabs.setTabToolTip(tab_idx, title)
        
        # Sync tab with stack
        self.tabs.setCurrentIndex(tab_idx)
        self.browser_stack.setCurrentIndex(idx)
        
        if qurl.toString() == "about:blank":
            self._go_home(web_view)
        else:
            web_view.load(qurl)

    def _close_tab(self, index):
        if self.tabs.count() <= 1:
            return
        widget = self.browser_stack.widget(index)
        self.browser_stack.removeWidget(widget)
        widget.deleteLater()
        self.tabs.removeTab(index)
        # Cleanup state for this tab
        self.detected_links.pop(index, None)
        self._current_page_detected_title.pop(index, None)

    def _on_tab_changed(self, index):
        if index < 0: return
        self.browser_stack.setCurrentIndex(index)
        web_view = self.browser_stack.currentWidget()
        if web_view:
            self.url_input.setText(web_view.url().toString())
            self._refresh_detected_list()

    def _refresh_detected_list(self):
        self.links_list.clear()
        idx = self.tabs.currentIndex()
        links = self.detected_links.get(idx, [])
        # Re-add items if needed, but for now we clear on navigation
        # To keep it simple, detection is per-page

    def _go_back(self):
        view = self.browser_stack.currentWidget()
        if view: view.back()

    def _go_forward(self):
        view = self.browser_stack.currentWidget()
        if view: view.forward()

    def _reload(self):
        view = self.browser_stack.currentWidget()
        if view: view.reload()

    def _go_home(self, view=None):
        target = view or self.browser_stack.currentWidget()
        if not target: return
        
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {
                    background: linear-gradient(135deg, #0F172A 0%, #1E293B 100%);
                    color: white;
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    height: 100vh;
                    margin: 0;
                }
                .container { text-align: center; }
                h1 { font-size: 3rem; margin-bottom: 10px; background: linear-gradient(to right, #818CF8, #6366F1); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
                p { color: #94A3B8; font-size: 1.2rem; margin-bottom: 30px; }
                .search-box {
                    background: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    border-radius: 20px;
                    padding: 15px 30px;
                    width: 500px;
                    display: flex;
                    align-items: center;
                    box-shadow: 0 10px 25px rgba(0,0,0,0.3);
                }
                input {
                    background: transparent;
                    border: none;
                    color: white;
                    flex: 1;
                    font-size: 1.1rem;
                    outline: none;
                }
                .sites {
                    display: grid;
                    grid-template-columns: repeat(4, 1fr);
                    gap: 20px;
                    margin-top: 40px;
                }
                .site-card {
                    background: rgba(30, 41, 59, 0.5);
                    padding: 20px;
                    border-radius: 15px;
                    width: 100px;
                    cursor: pointer;
                    transition: all 0.3s;
                    border: 1px solid transparent;
                }
                .site-card:hover {
                    background: rgba(99, 102, 241, 0.1);
                    border-color: #6366F1;
                    transform: translateY(-5px);
                }
                .site-icon { font-size: 2rem; margin-bottom: 10px; }
                .site-name { font-size: 0.9rem; font-weight: bold; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>SnapDownloader</h1>
                <p>Professional Browser & Media Hunter</p>
                <div class="search-box">
                    <input type="text" placeholder="Search Google or enter URL..." onkeydown="if(event.key==='Enter') window.location.href='https://www.google.com/search?q='+this.value">
                </div>
                <div class="sites">
                    <div class="site-card" onclick="window.location.href='https://www.youtube.com'">
                        <div class="site-icon">📺</div>
                        <div class="site-name">YouTube</div>
                    </div>
                    <div class="site-card" onclick="window.location.href='https://www.facebook.com'">
                        <div class="site-icon">👥</div>
                        <div class="site-name">Facebook</div>
                    </div>
                    <div class="site-card" onclick="window.location.href='https://www.instagram.com'">
                        <div class="site-icon">📸</div>
                        <div class="site-name">Instagram</div>
                    </div>
                    <div class="site-card" onclick="window.location.href='https://www.tiktok.com'">
                        <div class="site-icon">🎵</div>
                        <div class="site-name">TikTok</div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        target.setHtml(html)



    def _clear_detected(self):
        self.detected_links.clear()
        self.links_list.clear()

    def _manual_scan(self):
        # Trigger any hidden media loads or just re-emit current URL
        view = self.browser_stack.currentWidget()
        if view is None:
            return
        url = view.url().toString()
        self._request_analysis(url)


    def _on_download_page_clicked(self):
        view = self.browser_stack.currentWidget()
        if view is None:
            return
        url = view.url().toString()
        self._request_analysis(url)

    def _on_bulk_dl_clicked(self):
        # Send all discovered links to the bulk downloader
        for links in self.detected_links.values():
            for url in links:
                self._request_analysis(str(url))
        self.detected_links.clear()
        self.links_list.clear()
        self.discovery_bar.hide()

    def _request_analysis(self, url, context=None):
        # Stage 4: Session Handoff & Context
        view = self.browser_stack.currentWidget()
        referer = view.url().toString() if view is not None else ""
        user_agent = view.page().profile().httpUserAgent() if view is not None else ""
        profile_path = view.page().profile().persistentStoragePath() if view is not None else ""
        handoff = {
            "referer": referer,
            "user_agent": user_agent,
            "browser_profile_path": profile_path,
        }
        if context:
            handoff.update(context)
        self.analyze_requested.emit(url, handoff)


    def refresh_state(self):
        pass

    def retranslate_ui(self):
        self.back_btn.setToolTip(_("Back"))
        self.fwd_btn.setToolTip(_("Forward"))
        self.reload_btn.setToolTip(_("Reload"))
        self.home_btn.setToolTip(_("Home"))
        self.url_input.setPlaceholderText(_("Enter URL or search..."))
        self.scan_btn.setText(_("Scan"))
        self.dl_page_btn.setText(_("Download This Page"))
        self.panel_hdr.setText(_("Detected Streams"))
        self.sniffer_status_lbl.setText(_("Sniffer Live"))
        self.clear_sniffer_btn.setToolTip(_("Clear detected links"))
        self.discovery_lbl.setText(_("AI Discovery: Searching for videos..."))
        self.bulk_dl_btn.setText(_("Download All Found"))


        # We don't refresh detected items names as they are usually URLs or file names

