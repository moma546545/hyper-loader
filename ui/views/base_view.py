
"""
ui/views/base_view.py - Base class for all independent UI Views.
"""
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Signal

class BaseView(QWidget):
    """
    Base class for application views.
    Provides common functionality and enforces a uniform interface.
    """
    
    # Generic signal to request navigating to another view
    navigate_requested = Signal(str)
    
    # Generic signal to show a notification/toast
    notify_requested = Signal(str, str) # title, message

    def __init__(self, main_window=None, parent=None):
        super().__init__(parent)
        self.main_window = main_window # Reference to PremiumWindow or a UI Mediator
        
    def setup_ui(self):
        """Build the UI layout for this view. Should be overridden."""
        pass
        
    def refresh_state(self):
        """Called when the view becomes active."""
        pass
        
    def apply_theme(self, theme_data: dict):
        """Called when the app theme changes."""
        pass

    def retranslate_ui(self):
        """Called when the application language changes."""
        pass



