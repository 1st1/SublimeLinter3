#
# persist.py
# Part of SublimeLinter3, a code checking framework for Sublime Text 3
#
# Written by Ryan Hileman and Aparajita Fishman
#
# Project: https://github.com/SublimeLinter/SublimeLinter3
# License: MIT
#

from collections import defaultdict
import json
import os
from queue import Queue, Empty
import threading
import traceback
import time
import sublime
from xml.etree import ElementTree

from . import util

plugin_name = 'SublimeLinter'

# Get the name of the plugin directory, which is the parent of this file's directory
plugin_directory = os.path.basename(os.path.dirname(os.path.dirname(__file__)))


class Daemon:
    MIN_DELAY = 0.1
    running = False
    callback = None
    q = Queue()
    last_runs = {}

    def __init__(self):
        self.settings = {}
        self.sub_settings = None

    def load_settings(self, force=False):
        if force or not self.settings:
            if self.sub_settings:
                self.sub_settings.clear_on_change('sublimelinter-persist-settings')

            self.sub_settings = sublime.load_settings('SublimeLinter.sublime-settings')
            self.sub_settings.add_on_change('sublimelinter-persist-settings', self.update_settings)
            self.update_settings()

            self.observe_prefs_changes()

    def update_settings(self):
        settings = util.merge_user_settings(self.sub_settings)
        self.settings.clear()
        self.settings.update(settings)

        # Clear the path-related caches in case the paths list has changed
        util.create_environment.cache_clear()
        util.which.cache_clear()

        # Update the gutter marks in case that setting changed
        self.update_gutter_marks()

        # Reattach settings objects to linters
        from . import linter
        linter.Linter.reload()

    def update_user_settings(self, view=None):
        load_settings()

        # Fill in default linter settings
        linters = settings.pop('linters', {})

        for name, language in languages.items():
            default = language.get_settings().copy()
            default.update(linters.pop(name, {}))
            linters[name] = default

        settings['linters'] = linters

        user_prefs_path = os.path.join(sublime.packages_path(), 'User', '{}.sublime-settings'.format(plugin_name))

        if view is None:
            # See if any open views are the user prefs
            for w in sublime.windows():
                for v in w.views():
                    if v.file_name() == user_prefs_path:
                        view = v
                        break

                if view is not None:
                    break

        if view is not None:
            def replace(edit):
                if not view.is_dirty():
                    j = json.dumps({'user': settings}, indent=4, sort_keys=True)
                    j = j.replace(' \n', '\n')
                    view.replace(edit, sublime.Region(0, view.size()), j)

            edits[view.id()].append(replace)
            view.run_command('sublimelinter_edit')
            view.run_command('save')
        else:
            user_settings = sublime.load_settings('SublimeLinter.sublime-settings')
            user_settings.set('user', settings)
            sublime.save_settings('SublimeLinter.sublime-settings')

    def update_gutter_marks(self):
        theme = settings.get('gutter_theme', 'Default')
        theme_path = None

        # User themes override built in themes, check them first
        paths = (
            ('User', 'SublimeLinter-gutter-themes', theme),
            (plugin_directory, 'gutter-themes', theme),
            (plugin_directory, 'gutter-themes', 'Default')
        )

        for path in paths:
            sub_path = os.path.join(*path)
            full_path = os.path.join(sublime.packages_path(), sub_path)

            if os.path.isdir(full_path):
                theme_path = sub_path
                break

        if theme_path:
            if theme != 'Default' and os.path.basename(theme_path) == 'Default':
                printf('cannot find the gutter theme \'{}\', using the default'.format(theme))

            for error_type in ('warning', 'error'):
                gutter_marks[error_type] = os.path.join('Packages', theme_path, '{}.png'.format(error_type))

            gutter_marks['colorize'] = os.path.exists(os.path.join(sublime.packages_path(), theme_path, 'colorize'))
        else:
            sublime.error_message('SublimeLinter: cannot find the gutter theme "{}", and the default is also not available. No gutter marks will display.'.format(theme))
            gutter_marks['warning'] = gutter_marks['error'] = ''

    def observe_prefs_changes(self):
        prefs = sublime.load_settings('Preferences.sublime-settings')
        prefs.add_on_change('sublimelinter-pref-settings', self.update_color_scheme)

    def update_color_scheme(self):
        '''
        Checks to see if the current color scheme has our colors, and if not,
        adds them and writes the result to Packages/User/<scheme>.
        '''
        prefs = sublime.load_settings('Preferences.sublime-settings')
        scheme = prefs.get('color_scheme')

        if scheme is None:
            return

        # Structure of color scheme is:
        #
        # plist
        #    dict (name, settings)
        #       array (settings)
        #          dict (style)
        #
        # A style dict contains a 'scope' <key> followed by a <string>
        # with the scopes the style should apply to. So we will search
        # style dicts for a <string> of 'sublimelinter.mark.warning',
        # which is one of our scopes.

        plist = ElementTree.XML(sublime.load_resource(scheme))
        hasColors = False

        for element in plist.iterfind('./dict/array/dict/string'):
            if element.text == 'sublimelinter.mark.warning':
                hasColors = True
                break

        if not hasColors:
            # Append style dicts with our styles to the style array
            styles = plist.find('./dict/array')

            for style in MARK_STYLES:
                styles.append(ElementTree.XML(MARK_STYLES[style]))

            # Write the amended color scheme to Packages/User
            name = os.path.splitext(os.path.basename(scheme))[0] + ' - SublimeLinter'
            scheme_path = os.path.join(sublime.packages_path(), 'User', name + '.tmTheme')

            with open(scheme_path, 'w', encoding='utf8') as f:
                f.write(COLOR_SCHEME_PREAMBLE)
                f.write(ElementTree.tostring(plist, encoding='unicode'))

                # Set the amended color scheme to the current color scheme
                prefs.clear_on_change('sublimelinter-pref-settings')
                prefs.set('color_scheme', os.path.join('Packages', 'User', os.path.basename(scheme_path)))
                sublime.save_settings('Preferences.sublime-settings')

                sublime.message_dialog('SublimeLinter copied and amended the color scheme "{}" and switched to the amended scheme.'.format(os.path.splitext(os.path.basename(scheme))[0]))

                # Just to be sure the main thread has a chance to fully reload the color scheme,
                # don't start observing changes on it right away.
                sublime.set_timeout(self.observe_prefs_changes, 1000)

    def start(self, callback):
        self.callback = callback

        if self.running:
            self.q.put('reload')
            return
        else:
            self.running = True
            threading.Thread(target=self.loop).start()

    def reenter(self, view_id, timestamp):
        self.callback(view_id, timestamp)

    def loop(self):
        last_runs = {}

        while True:
            try:
                try:
                    item = self.q.get(block=True, timeout=self.MIN_DELAY)
                except Empty:
                    for view_id, timestamp in last_runs.copy().items():
                        # If more than the minimum delay has elapsed since the last run, update the view
                        if time.monotonic() > timestamp + self.MIN_DELAY:
                            self.last_runs[view_id] = time.monotonic()
                            del last_runs[view_id]
                            self.reenter(view_id, timestamp)

                    continue

                if isinstance(item, tuple):
                    view_id, timestamp = item

                    if view_id in self.last_runs and timestamp < self.last_runs[view_id]:
                        continue

                    last_runs[view_id] = timestamp

                elif isinstance(item, (int, float)):
                    time.sleep(item)

                elif isinstance(item, str):
                    if item == 'reload':
                        self.printf('daemon detected a reload')
                else:
                    self.printf('unknown message sent to daemon:', item)
            except:
                self.printf('error in SublimeLinter daemon:')
                self.printf('-' * 20)
                self.printf(traceback.format_exc())
                self.printf('-' * 20)

    def hit(self, view):
        timestamp = time.monotonic()
        self.q.put((view.id(), timestamp))
        return timestamp

    def delay(self, milliseconds=100):
        self.q.put(milliseconds / 1000.0)

    def debug(self, *args):
        if self.settings.get('debug'):
            self.printf(*args)

    def printf(self, *args):
        print(plugin_name + ': ', end='')

        for arg in args:
            print(arg, end=' ')

        print()

if not 'queue' in globals():
    queue = Daemon()
    debug = queue.debug
    printf = queue.printf
    settings = queue.settings

    # A mapping between view ids and errors, which are line:(col, message) dicts
    errors = {}

    # A mapping between view ids and HighlightSets
    highlights = {}

    # A mapping between language names and linter classes
    languages = {}

    # A mapping between view ids and a set of linter instances
    linters = {}

    # A mapping between view ids and views
    views = {}

    edits = defaultdict(list)

    # Info about the gutter mark icons
    gutter_marks = {'warning': 'dot', 'error': 'dot', 'colorize': True}

    # Set to true when the plugin is loaded at startup
    plugin_is_loaded = False


def load_settings(force=False):
    queue.load_settings(force)


def update_user_settings(view=None):
    queue.update_user_settings(view=view)


def update_gutter_marks():
    queue.update_gutter_marks()


def update_color_scheme():
    queue.update_color_scheme()


def edit(vid, edit):
    callbacks = edits.pop(vid, [])

    for c in callbacks:
        c(edit)


def register_linter(linter_class, name, attrs):
    '''Add a linter class to our mapping of languages <--> linter classes.'''
    if name:
        linter_settings = settings.get('linters', {})
        linter_class.lint_settings = linter_settings.get(name, {})
        linter_class.name = name
        languages[name] = linter_class

        # The sublime plugin API is not available until plugin_loaded is executed
        if plugin_is_loaded:
            load_settings(force=True)

            # If a linter is reloaded, we have to reassign linters to all views
            from . import linter

            for view in views.values():
                linter.Linter.assign(view, reassign=True)

            printf('{} linter reloaded'.format(name))
        else:
            printf('{} linter loaded'.format(name))


COLOR_SCHEME_PREAMBLE = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
'''

MARK_STYLES = {
    'warning': '''
        <dict>
            <key>name</key>
            <string>SublimeLinter Warning</string>
            <key>scope</key>
            <string>sublimelinter.mark.warning</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#EDBA00</string>
            </dict>
        </dict>
    ''',

    'error': '''
        <dict>
            <key>name</key>
            <string>SublimeLinter Error</string>
            <key>scope</key>
            <string>sublimelinter.mark.error</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#DA2000</string>
            </dict>
        </dict>
    ''',

    'gutter': '''
        <dict>
            <key>name</key>
            <string>SublimeLinter Gutter Mark</string>
            <key>scope</key>
            <string>sublimelinter.gutter-mark</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#FFFFFF</string>
            </dict>
        </dict>
    '''
}
