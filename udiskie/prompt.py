"""
User prompt utility.
"""

from udiskie.depend import has_Gtk, require_Gtk

import asyncio

from distutils.spawn import find_executable
import getpass
import logging
import re
import shlex
import string
import subprocess
import sys

from .async_ import exec_subprocess, run_bg, run_in_executor, serial
from .locale import _
from .common import AttrDictView
from .config import DeviceFilter

Gtk = None

__all__ = ['password', 'browser']


dialog_definition = r"""
<interface>
  <object class="GtkDialog" id="entry_dialog">
    <property name="border_width">5</property>
    <property name="window_position">center</property>
    <property name="type_hint">dialog</property>
    <child internal-child="vbox">
      <object class="GtkBox" id="entry_box">
        <property name="spacing">6</property>
        <property name="border_width">6</property>
        <property name="visible">True</property>
        <child>
          <object class="GtkLabel" id="message">
            <property name="xalign">0</property>
            <property name="visible">True</property>
          </object>
        </child>
        <child>
          <object class="GtkEntry" id="entry">
            <property name="visibility">False</property>
            <property name="activates_default">True</property>
            <property name="visible">True</property>
          </object>
        </child>
        <child>
          <object class="GtkCheckButton" id="show_password">
            <property name="label">Show password</property>
            <property name="active">False</property>
            <property name="visible">True</property>
          </object>
        </child>
        <child>
          <object class="GtkCheckButton" id="remember">
            <property name="label">Remember password</property>
            <property name="visible">False</property>
          </object>
        </child>
        <child internal-child="action_area">
          <object class="GtkButtonBox" id="action_box">
            <property name="visible">True</property>
            <child>
              <object class="GtkButton" id="cancel_button">
                <property name="label">gtk-cancel</property>
                <property name="use_stock">True</property>
                <property name="visible">True</property>
              </object>
            </child>
            <child>
              <object class="GtkButton" id="ok_button">
                <property name="label">gtk-ok</property>
                <property name="use_stock">True</property>
                <property name="can_default">True</property>
                <property name="has_default">True</property>
                <property name="visible">True</property>
              </object>
            </child>
            <child>
              <object class="GtkButton" id="keyfile_button">
                <property name="label">Open keyfile…</property>
                <property name="visible">False</property>
              </object>
            </child>
          </object>
        </child>
      </object>
    </child>
    <action-widgets>
      <action-widget response="-6">cancel_button</action-widget>
      <action-widget response="-5">ok_button</action-widget>
    </action-widgets>
  </object>
</interface>
"""


class Dialog(asyncio.Future):

    def __init__(self, window):
        super().__init__()
        self._enter_count = 0
        self.window = window
        self.window.connect("response", self._result_handler)

    def _result_handler(self, window, response):
        self.set_result(response)

    def __enter__(self):
        self._enter_count += 1
        self._awaken()
        return self

    def __exit__(self, *exc_info):
        self._enter_count -= 1
        if self._enter_count == 0:
            self._cleanup()

    def _awaken(self):
        self.window.present()

    def _cleanup(self):
        self.window.hide()
        self.window.destroy()


class PasswordDialog(Dialog):

    INSTANCES = {}
    content = None

    @classmethod
    def create(cls, key):
        if key in cls.INSTANCES:
            return cls.INSTANCES[key]
        return cls(key)

    def _awaken(self):
        self.INSTANCES[self.key] = self
        super()._awaken()

    def _cleanup(self):
        del self.INSTANCES[self.key]
        super()._cleanup()

    def __init__(self, key):
        self.key = key
        global Gtk
        Gtk = require_Gtk()
        builder = self.builder = Gtk.Builder.new()
        builder.add_from_string(dialog_definition)
        window = self.window = builder.get_object('entry_dialog')
        self.entry = builder.get_object('entry')

        show_password = builder.get_object('show_password')
        show_password.set_label(_('Show password'))
        show_password.connect('clicked', self.on_show_password)

        keyfile_button = self.keyfile_button = builder.get_object('keyfile_button')
        keyfile_button.set_label(_('Open keyfile…'))
        keyfile_button.connect('clicked', run_bg(self.on_open_keyfile))

        self.use_cache = builder.get_object('remember')
        self.label = builder.get_object('message')

        window.set_keep_above(True)
        super(PasswordDialog, self).__init__(window)

    def set_keyfile_support(self, enabled):
        self.keyfile_button.set_visible(enabled)

    def set_cache_hint(self, enabled, checked):
        self.use_cache.set_label(_('Remember password'))
        self.use_cache.set_visible(enabled)
        self.use_cache.set_active(checked)

    def set_message(self, title, message):
        self.window.set_title(title)
        self.label.set_label(message)

    def on_show_password(self, button):
        self.entry.set_visibility(button.get_active())

    async def on_open_keyfile(self, button):
        gtk_dialog = Gtk.FileChooserDialog(
            _("Open a keyfile to unlock the LUKS device"), self.window,
            Gtk.FileChooserAction.OPEN,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
             Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        with Dialog(gtk_dialog) as dialog:
            response = await dialog
            if response == Gtk.ResponseType.OK:
                self.content = await run_in_executor(read_file)(
                    dialog.window.get_filename())
                self.window.response(response)

    def get_text(self):
        if self.content is not None:
            return self.content
        return self.entry.get_text()


def read_file(filename, mode='rb'):
    with open(filename, mode) as f:
        return f.read()


async def unlock_dialog(mounter, device):
    """
    Show a Gtk password dialog.

    :returns: the password or ``None`` if the user aborted the operation
    :raises RuntimeError: if Gtk can not be properly initialized
    """
    key = device.id_uuid
    message = _('Enter password for {0.device_presentation}: ', device)
    title = 'udiskie'
    allow_keyfile = mounter.udisks.keyfile_support
    allow_cache = mounter._cache is not None
    cache_hint = mounter._cache_hint
    with PasswordDialog.create(key) as dialog:
        dialog.set_keyfile_support(allow_keyfile)
        dialog.set_cache_hint(allow_cache, cache_hint)
        dialog.set_message(title, message)
        response = await dialog
        password = dialog.get_text()
        cache_hint = dialog.use_cache.get_active()
    if response == Gtk.ResponseType.OK:
        return await mounter.do_unlock(device, password, cache_hint)
    return None


async def unlock_gui(mounter, device):
    """Unlock a device using password input from GUI."""
    try:
        return await unlock_dialog(mounter, device)
    except RuntimeError:
        return None


@serial
@run_in_executor
def get_password_tty(device):
    """Get the password to unlock a device from terminal."""
    text = _('Enter password for {0.device_presentation}: ', device)
    try:
        return getpass.getpass(text)
    except EOFError:
        print("")
        return None


async def unlock_tty(mounter, device):
    """Unlock device using TTY password prompt."""
    password = await get_password_tty(device)
    return await mounter.do_unlock(device, password)


class DeviceCommand:

    """
    Launcher that starts user-defined password prompts. The command can be
    specified in terms of a command line template.
    """

    def __init__(self, argv, **extra):
        """Create the launcher object from the command line template."""
        if isinstance(argv, str):
            self.argv = shlex.split(argv)
        else:
            self.argv = argv
        self.extra = extra.copy()
        # obtain a list of used fields names
        formatter = string.Formatter()
        self.used_attrs = set()
        for arg in self.argv:
            for text, kwd, spec, conv in formatter.parse(arg):
                if kwd is None:
                    continue
                self.used_attrs.add(kwd)
                if kwd not in DeviceFilter.VALID_PARAMETERS and \
                        kwd not in self.extra:
                    self.extra[kwd] = None
                    logging.getLogger(__name__).error(_(
                        'Unknown device attribute {!r} in format string: {!r}',
                        kwd, arg))

    async def __call__(self, device):
        """
        Invoke the subprocess to ask the user to enter a password for unlocking
        the specified device.
        """
        attrs = {attr: getattr(device, attr) for attr in self.used_attrs}
        attrs.update(self.extra)
        # for backward compatibility provide positional argument:
        fake_dev = AttrDictView(attrs)
        argv = [arg.format(**attrs) for arg in self.argv]
        try:
            stdout = await exec_subprocess(argv)
        except subprocess.CalledProcessError:
            return None
        return stdout.rstrip('\n')

    async def unlock(self, mounter, device):
        """Unlock device using password from external command."""
        password = await self(device)
        return await mounter.do_unlock(device, password)


def unlock(command):
    """Create a password prompt function."""
    gui = lambda: has_Gtk()          and unlock_gui
    tty = lambda: sys.stdin.isatty() and unlock_tty
    if command == 'builtin:gui':
        return gui() or tty()
    elif command == 'builtin:tty':
        return tty() or gui()
    elif command:
        return DeviceCommand(command).unlock
    else:
        return None


def browser(browser_name='xdg-open'):

    """Create a browse-directory function."""

    if not browser_name:
        return None
    executable = find_executable(browser_name)
    if executable is None:
        # Why not raise an exception? -I think it is more convenient (for
        # end users) to have a reasonable default, without enforcing it.
        logging.getLogger(__name__).warn(
            _("Can't find file browser: {0!r}. "
              "You may want to change the value for the '-f' option.",
              browser_name))
        return None

    def browse(path):
        return subprocess.Popen([executable, path])

    return browse


def notify_command(command_format, mounter):
    """
    Command notification tool.

    This works similar to Notify, but will issue command instead of showing
    the notifications on the desktop. This can then be used to react to events
    from shell scripts.

    The command can contain modern pythonic format placeholders like:
    {device_file}. The following placeholders are supported:
    event, device_file, device_id, device_size, drive, drive_label, id_label,
    id_type, id_usage, id_uuid, mount_path, root

    :param str command_format: The command format string to run when an event occurs.
    :param mounter: Mounter object
    """
    udisks = mounter.udisks
    for event in ['device_mounted', 'device_unmounted',
                  'device_locked', 'device_unlocked',
                  'device_added', 'device_removed',
                  'job_failed']:
        udisks.connect(event, run_bg(DeviceCommand(command_format, event=event)))
