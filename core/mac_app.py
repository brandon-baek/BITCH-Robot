"""BITCH's own window on the Mac, and its menu bar item.

Closing the window only hides it: BITCH keeps living (your phone, reminders and greetings keep working).
The BITCH item in the menu bar opens the window again or quits BITCH for real.
"""
import os, threading
from . import config

_keep = []          # Objective-C objects that must stay alive


def run(url, on_quit=None):
    """Open the window and the menu bar item, then run the Mac's event loop (blocks)."""
    import webview
    win = webview.create_window('BITCH', url, width=1320, height=900, min_size=(380, 600), background_color='#DFE4E8')

    def closing():
        try:
            win.hide(); return False            # hide instead of closing
        except Exception:
            return True
    win.events.closing += closing
    webview.start(lambda: menubar(win, on_quit))


def menubar(win, on_quit=None):
    """Add a BITCH item to the menu bar with Open and Quit. Called on a background thread by pywebview."""
    try:
        import objc
        from AppKit import NSStatusBar, NSVariableStatusItemLength, NSMenu, NSMenuItem, NSImage, NSApp
        from Foundation import NSObject
        from PyObjCTools import AppHelper
    except Exception as e:
        print(f'  menu bar item unavailable ({e})', flush=True); return

    class BitchMenuTarget(NSObject):
        def openBitch_(self, sender):
            win.show()
            try: NSApp.activateIgnoringOtherApps_(True)
            except Exception: pass

        def quitBitch_(self, sender):
            if on_quit: on_quit()
            os._exit(0)

    def build():
        try:
            item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            img = NSImage.alloc().initWithContentsOfFile_(os.path.join(config.ROOT, 'mac', 'icon.png'))
            if img:
                img.setSize_((18, 18)); item.button().setImage_(img)
            else:
                item.button().setTitle_('BITCH')
            item.button().setToolTip_('BITCH')
            menu = NSMenu.alloc().init(); t = BitchMenuTarget.alloc().init()
            for title, sel in (('Open BITCH', 'openBitch:'), (None, None), ('Quit BITCH', 'quitBitch:')):
                if title is None: menu.addItem_(NSMenuItem.separatorItem()); continue
                mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, '')
                mi.setTarget_(t); menu.addItem_(mi)
            item.setMenu_(menu)
            _keep.extend([item, menu, t])
        except Exception as e:
            print(f'  menu bar item failed ({e})', flush=True)
    AppHelper.callAfter(build)
