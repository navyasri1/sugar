# Copyright (C) 2006-2007 Red Hat, Inc.
# Copyright (C) 2008 One Laptop Per Child
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import logging
from gettext import gettext as _
import math

import gobject
import gconf
import gtk
import hippo
import dbus

from sugar.graphics import style
from sugar.graphics.icon import Icon, CanvasIcon
from sugar.graphics.menuitem import MenuItem
from sugar.graphics.alert import Alert
from sugar.graphics.xocolor import XoColor
from sugar.activity import activityfactory
from sugar.activity.activityhandle import ActivityHandle
from sugar.presence import presenceservice
from sugar import dispatch

from jarabe.view.palettes import JournalPalette
from jarabe.view.palettes import CurrentActivityPalette, ActivityPalette
from jarabe.view.buddymenu import BuddyMenu
from jarabe.view import launcher
from jarabe.model.buddy import BuddyModel
from jarabe.model import shell
from jarabe.model import bundleregistry
from jarabe import journal

from jarabe.desktop import schoolserver
from jarabe.desktop.schoolserver import RegisterError
from jarabe.desktop.myicon import MyIcon
from jarabe.desktop import favoriteslayout

_logger = logging.getLogger('FavoritesView')

_ICON_DND_TARGET = ('activity-icon', gtk.TARGET_SAME_WIDGET, 0)

LAYOUT_MAP = {favoriteslayout.RingLayout.key: favoriteslayout.RingLayout,
        #favoriteslayout.BoxLayout.key: favoriteslayout.BoxLayout,
        #favoriteslayout.TriangleLayout.key: favoriteslayout.TriangleLayout,
        #favoriteslayout.SunflowerLayout.key: favoriteslayout.SunflowerLayout,
        favoriteslayout.RandomLayout.key: favoriteslayout.RandomLayout}
"""Map numeric layout identifiers to uninstantiated subclasses of
`FavoritesLayout` which implement the layouts.  Additional information
about the layout can be accessed with fields of the class."""

class FavoritesView(hippo.Canvas):
    __gtype_name__ = 'SugarFavoritesView'

    __gsignals__ = {
        'erase-activated' : (gobject.SIGNAL_RUN_FIRST,
                             gobject.TYPE_NONE, ([str]))
    }

    def __init__(self, **kwargs):
        logging.debug('STARTUP: Loading the favorites view')

        gobject.GObject.__init__(self, **kwargs)

        # DND stuff
        self._pressed_button = None
        self._press_start_x = None
        self._press_start_y = None
        self._hot_x = None
        self._hot_y = None
        self._last_clicked_icon = None

        self._box = hippo.CanvasBox()
        self._box.props.background_color = style.COLOR_WHITE.get_int()
        self.set_root(self._box)

        self._my_icon = _MyIcon(style.XLARGE_ICON_SIZE)
        self._my_icon.connect('register-activate', self.__register_activate_cb)
        self._box.append(self._my_icon)

        self._current_activity = CurrentActivityIcon()
        self._box.append(self._current_activity)

        self._layout = None
        self._alert = None
        self._datastore_listener = DatastoreListener()

        # More DND stuff
        self.add_events(gtk.gdk.BUTTON_PRESS_MASK |
                        gtk.gdk.POINTER_MOTION_HINT_MASK)
        self.connect('motion-notify-event', self.__motion_notify_event_cb)
        self.connect('button-press-event', self.__button_press_event_cb)
        self.connect('drag-begin', self.__drag_begin_cb)
        self.connect('drag-motion', self.__drag_motion_cb)
        self.connect('drag-drop', self.__drag_drop_cb)
        self.connect('drag-data-received', self.__drag_data_received_cb)

        gobject.idle_add(self.__connect_to_bundle_registry_cb)

        favorites_settings = get_settings()
        favorites_settings.changed.connect(self.__settings_changed_cb)
        self._set_layout(favorites_settings.layout)

    def __settings_changed_cb(self, **kwargs):
        favorites_settings = get_settings()
        self._set_layout(favorites_settings.layout)        

    def __connect_to_bundle_registry_cb(self):
        registry = bundleregistry.get_registry()

        for info in registry:
            if registry.is_bundle_favorite(info.get_bundle_id(),
                                           info.get_activity_version()):
                self._add_activity(info)

        registry.connect('bundle-added', self.__activity_added_cb)
        registry.connect('bundle-removed', self.__activity_removed_cb)
        registry.connect('bundle-changed', self.__activity_changed_cb)

    def _add_activity(self, activity_info):
        if activity_info.get_bundle_id() == 'org.laptop.JournalActivity':
            return
        icon = ActivityIcon(activity_info, self._datastore_listener)
        icon.connect('erase-activated', self.__erase_activated_cb)
        icon.props.size = style.STANDARD_ICON_SIZE
        self._box.insert_sorted(icon, 0, self._layout.compare_activities)
        self._layout.append(icon)

    def __erase_activated_cb(self, activity_icon, bundle_id):
        self.emit('erase-activated', bundle_id)

    def __activity_added_cb(self, activity_registry, activity_info):
        registry = bundleregistry.get_registry()
        if registry.is_bundle_favorite(activity_info.get_bundle_id(),
                activity_info.get_activity_version()):
            self._add_activity(activity_info)

    def _find_activity_icon(self, bundle_id, version):
        for icon in self._box.get_children():
            if isinstance(icon, ActivityIcon) and \
                    icon.bundle_id == bundle_id and icon.version == version:
                return icon
        return None

    def __activity_removed_cb(self, activity_registry, activity_info):
        icon = self._find_activity_icon(activity_info.get_bundle_id(),
                activity_info.get_activity_version())
        if icon is not None:
            self._layout.remove(icon)
            self._box.remove(icon)

    def __activity_changed_cb(self, activity_registry, activity_info):
        if activity_info.get_bundle_id() == 'org.laptop.JournalActivity':
            return
        icon = self._find_activity_icon(activity_info.get_bundle_id(),
                activity_info.get_activity_version())
        if icon is not None:
            self._box.remove(icon)

        registry = bundleregistry.get_registry()
        if registry.is_bundle_favorite(activity_info.get_bundle_id(),
                                       activity_info.get_activity_version()):
            self._add_activity(activity_info)

    def do_size_allocate(self, allocation):
        width = allocation.width        
        height = allocation.height

        min_w_, my_icon_width = self._my_icon.get_width_request()
        min_h_, my_icon_height = self._my_icon.get_height_request(my_icon_width)
        x = (width - my_icon_width) / 2
        y = (height - my_icon_height - style.GRID_CELL_SIZE) / 2
        self._layout.move_icon(self._my_icon, x, y, locked=True)

        min_w_, icon_width = self._current_activity.get_width_request()
        min_h_, icon_height = \
                self._current_activity.get_height_request(icon_width)
        x = (width - icon_width) / 2
        y = (height - my_icon_height - style.GRID_CELL_SIZE) / 2 + \
                my_icon_height + style.DEFAULT_PADDING
        self._layout.move_icon(self._current_activity, x, y, locked=True)

        hippo.Canvas.do_size_allocate(self, allocation)

    # TODO: Dnd methods. This should be merged somehow inside hippo-canvas.
    def __button_press_event_cb(self, widget, event):
        if event.button == 1 and event.type == gtk.gdk.BUTTON_PRESS:
            self._last_clicked_icon = self._get_icon_at_coords(event.x, event.y)
            if self._last_clicked_icon is not None:
                self._pressed_button = event.button
                self._press_start_x = event.x
                self._press_start_y = event.y

        return False

    def _get_icon_at_coords(self, x, y):
        for icon in self._box.get_children():
            icon_x, icon_y = icon.get_context().translate_to_widget(icon)
            icon_width, icon_height = icon.get_allocation()

            if (x >= icon_x ) and (x <= icon_x + icon_width) and \
                    (y >= icon_y ) and (y <= icon_y + icon_height) and \
                    isinstance(icon, ActivityIcon):
                return icon
        return None

    def __motion_notify_event_cb(self, widget, event):
        if not self._pressed_button:
            return False
        
        # if the mouse button is not pressed, no drag should occurr
        if not event.state & gtk.gdk.BUTTON1_MASK:
            self._pressed_button = None
            return False

        if event.is_hint:
            x, y, state_ = event.window.get_pointer()
        else:
            x = event.x
            y = event.y

        if widget.drag_check_threshold(int(self._press_start_x),
                                       int(self._press_start_y),
                                       int(x),
                                       int(y)):
            context_ = widget.drag_begin([_ICON_DND_TARGET],
                                         gtk.gdk.ACTION_MOVE,
                                         1,
                                         event)
        return False

    def __drag_begin_cb(self, widget, context):
        icon_file_name = self._last_clicked_icon.props.file_name
        # TODO: we should get the pixbuf from the widget, so it has colors, etc
        pixbuf = gtk.gdk.pixbuf_new_from_file(icon_file_name)
        
        self._hot_x = pixbuf.props.width / 2
        self._hot_y = pixbuf.props.height / 2
        context.set_icon_pixbuf(pixbuf, self._hot_x, self._hot_y)

    def __drag_motion_cb(self, widget, context, x, y, time):
        if self._last_clicked_icon is not None:
            context.drag_status(context.suggested_action, time)
            return True
        else:
            return False

    def __drag_drop_cb(self, widget, context, x, y, time):
        if self._last_clicked_icon is not None:
            self.drag_get_data(context, _ICON_DND_TARGET[0])

            self._layout.move_icon(self._last_clicked_icon,
                                   x - self._hot_x, y - self._hot_y)

            self._pressed_button = None
            self._press_start_x = None
            self._press_start_y = None
            self._hot_x = None
            self._hot_y = None
            self._last_clicked_icon = None

            return True
        else:
            return False

    def __drag_data_received_cb(self, widget, context, x, y, selection_data,
                                info, time):
        context.drop_finish(success=True, time=time)

    def _set_layout(self, layout):
        if layout not in LAYOUT_MAP:
            raise ValueError('Unknown favorites layout: %r' % layout)

        if type(self._layout) == LAYOUT_MAP[layout]:
            return

        self._layout = LAYOUT_MAP[layout]()
        self._box.set_layout(self._layout)

        #TODO: compatibility hack while sort() gets added to the hippo python
        # bindings
        if hasattr(self._box, 'sort'):
            self._box.sort(self._layout.compare_activities)

        for icon in self._box.get_children():
            if icon not in [self._my_icon, self._current_activity]:
                self._layout.append(icon)

        self._layout.append(self._my_icon, locked=True)
        self._layout.append(self._current_activity, locked=True)

        if self._layout.allow_dnd():
            self.drag_source_set(0, [], 0)
            self.drag_dest_set(0, [], 0)
        else:
            self.drag_source_unset()
            self.drag_dest_unset()

    layout = property(None, _set_layout)

    def add_alert(self, alert):
        if self._alert is not None:
            self.remove_alert()
        alert.set_size_request(gtk.gdk.screen_width(), -1)
        self._alert = hippo.CanvasWidget(widget=alert)
        self._box.append(self._alert, hippo.PACK_FIXED)

    def remove_alert(self):
        self._box.remove(self._alert)
        self._alert = None

    def __register_activate_cb(self, icon):
        alert = Alert()
        try:
            schoolserver.register_laptop()
        except RegisterError, e:
            alert.props.title = _('Registration Failed')
            alert.props.msg = _('%s') % e
        else:    
            alert.props.title = _('Registration Successful')
            alert.props.msg = _('You are now registered ' \
                                'with your school server.')
            self._my_icon.remove_register_menu()

        ok_icon = Icon(icon_name='dialog-ok')
        alert.add_button(gtk.RESPONSE_OK, _('Ok'), ok_icon)

        self.add_alert(alert)
        alert.connect('response', self.__register_alert_response_cb)            
            
    def __register_alert_response_cb(self, alert, response_id):
        self.remove_alert()

DS_DBUS_SERVICE = 'org.laptop.sugar.DataStore'
DS_DBUS_INTERFACE = 'org.laptop.sugar.DataStore'
DS_DBUS_PATH = '/org/laptop/sugar/DataStore'

class DatastoreListener(object):
    def __init__(self):
        bus = dbus.SessionBus()
        remote_object = bus.get_object(DS_DBUS_SERVICE, DS_DBUS_PATH)
        self._datastore = dbus.Interface(remote_object, DS_DBUS_INTERFACE)
        self._datastore.connect_to_signal('Created',
                                          self.__datastore_created_cb)
        self._datastore.connect_to_signal('Updated',
                                          self.__datastore_updated_cb)
        self._datastore.connect_to_signal('Deleted',
                                          self.__datastore_deleted_cb)

        self.updated = dispatch.Signal()
        self.deleted = dispatch.Signal()

    def __datastore_created_cb(self, object_id):
        metadata = self._datastore.get_properties(object_id, byte_arrays=True)
        self.updated.send(self, metadata=metadata)

    def __datastore_updated_cb(self, object_id):
        metadata = self._datastore.get_properties(object_id, byte_arrays=True)
        self.updated.send(self, metadata=metadata)

    def __datastore_deleted_cb(self, object_id):
        self.deleted.send(self, object_id=object_id)

    def get_last_activity_async(self, bundle_id, properties, callback_cb):
        query = {'activity': bundle_id,
                 'limit': 5,
                 'order_by': ['-mtime']}

        reply_handler = lambda entries, total_count: self.__reply_handler_cb(
                entries, total_count, callback_cb)

        error_handler = lambda error: self.__error_handler_cb(
                error, callback_cb)

        self._datastore.find(query, properties, byte_arrays=True,
                               reply_handler=reply_handler,
                               error_handler=error_handler)

    def __reply_handler_cb(self, entries, total_count, callback_cb):
        logging.debug('__reply_handler_cb')
        callback_cb(entries)

    def __error_handler_cb(self, error, callback_cb):
        logging.debug('__error_handler_cb')
        callback_cb(None, error)

class ActivityIcon(CanvasIcon):
    __gtype_name__ = 'SugarFavoriteActivityIcon'

    _BORDER_WIDTH = style.zoom(3)

    __gsignals__ = {
        'erase-activated' : (gobject.SIGNAL_RUN_FIRST,
                             gobject.TYPE_NONE, ([str]))
    }

    def __init__(self, activity_info, datastore_listener):
        CanvasIcon.__init__(self, cache=True,
                            file_name=activity_info.get_icon())

        self._activity_info = activity_info
        self._journal_entries = []
        self._hovering = False

        self.connect('hovering-changed', self.__hovering_changed_event_cb)
        self.connect('button-release-event', self.__button_release_event_cb)

        self._datastore_listener = datastore_listener
        datastore_listener.updated.connect(self.__datastore_listener_updated_cb)
        datastore_listener.deleted.connect(self.__datastore_listener_deleted_cb)

        self._refresh()
        self._update()

    def _refresh(self):
        bundle_id = self._activity_info.get_bundle_id()
        properties = ['uid', 'title', 'icon-color', 'activity', 'activity_id',
                      'mime_type', 'mountpoint']
        self._datastore_listener.get_last_activity_async(bundle_id, properties,
                self.__get_last_activity_async_cb)

    def __datastore_listener_updated_cb(self, **kwargs):
        bundle_id = self._activity_info.get_bundle_id()
        if kwargs['metadata'].get('activity', '') == bundle_id:
            self._refresh()

    def __datastore_listener_deleted_cb(self, **kwargs):
        for entry in self._journal_entries:
            if entry['uid'] == kwargs['object_id']:
                self._refresh()
                break

    def __get_last_activity_async_cb(self, entries, error=None):
        if error is not None:
            logging.error('Error retrieving most recent activities: %r' % error)

        # If there's a problem with the DS index, we may get entries not related
        # to this activity.
        checked_entries = []
        for entry in entries:
            if entry['activity'] == self.bundle_id:
                checked_entries.append(entry)

        self._journal_entries = checked_entries
        self._update()

    def _update(self):
        self.palette = None
        if not self._journal_entries:
            self.props.stroke_color = style.COLOR_BUTTON_GREY.get_svg()
            self.props.fill_color = style.COLOR_TRANSPARENT.get_svg()
        else:
            first_entry = self._journal_entries[0]
            self.props.xo_color = XoColor(first_entry['icon-color'])

    def create_palette(self):
        palette = FavoritePalette(self._activity_info, self._journal_entries)
        palette.connect('activate', self.__palette_activate_cb)
        palette.connect('erase-activated', self.__erase_activated_cb)
        return palette

    def __erase_activated_cb(self, palette):
        self.emit('erase-activated', self._activity_info.get_bundle_id())

    def __palette_activate_cb(self, palette):
        self._activate()

    def __hovering_changed_event_cb(self, icon, hovering):
        self._hovering = hovering
        self.emit_paint_needed(0, 0, -1, -1)

    def do_paint_above_children(self, cr, damaged_box):
        if not self._hovering:
            return

        width, height = self.get_allocation()

        x = ActivityIcon._BORDER_WIDTH / 2
        y = ActivityIcon._BORDER_WIDTH / 2
        width -= ActivityIcon._BORDER_WIDTH
        height -= ActivityIcon._BORDER_WIDTH
        radius = width / 10

        cr.move_to(x + radius, y)
        cr.arc(x + width - radius, y + radius, radius, math.pi * 1.5,
               math.pi * 2)
        cr.arc(x + width - radius, x + height - radius, radius, 0,
               math.pi * 0.5)
        cr.arc(x + radius, y + height - radius, radius, math.pi * 0.5, math.pi)
        cr.arc(x + radius, y + radius, radius, math.pi, math.pi * 1.5)

        color = style.COLOR_SELECTION_GREY.get_int()
        hippo.cairo_set_source_rgba32(cr, color)
        cr.set_line_width(ActivityIcon._BORDER_WIDTH)
        cr.stroke()

    def do_get_content_height_request(self, for_width):
        height, height = CanvasIcon.do_get_content_height_request(self, 
                                                                  for_width)
        height += ActivityIcon._BORDER_WIDTH * 2
        return height, height

    def do_get_content_width_request(self):
        width, width = CanvasIcon.do_get_content_width_request(self)
        width += ActivityIcon._BORDER_WIDTH * 2
        return width, width

    def __button_release_event_cb(self, icon, event):
        self._activate()

    def _activate(self):
        self.palette.popdown(immediate=True)
        if self._journal_entries:
            entry = self._journal_entries[0]

            shell_model = shell.get_model()
            activity = shell_model.get_activity_by_id(entry['activity_id'])
            if activity:
                activity.get_window().activate(gtk.get_current_event_time())
                return

            launcher.add_launcher(entry['activity_id'],
                                  self._activity_info.get_icon(),
                                  XoColor(entry.get('icon-color', '')))
            journal.misc.resume(entry, self._activity_info.get_bundle_id())
        else:
            client = gconf.client_get_default()
            xo_color = XoColor(client.get_string('/desktop/sugar/user/color'))

            activity_id = activityfactory.create_activity_id()
            launcher.add_launcher(activity_id,
                                  self._activity_info.get_icon(),
                                  xo_color)

            handle = ActivityHandle(activity_id)
            activityfactory.create(self._activity_info, handle)

    def get_bundle_id(self):
        return self._activity_info.get_bundle_id()
    bundle_id = property(get_bundle_id, None)

    def get_version(self):
        return self._activity_info.get_activity_version()
    version = property(get_version, None)

    def _get_installation_time(self):
        return self._activity_info.get_installation_time()
    installation_time = property(_get_installation_time, None)

    def _get_fixed_position(self):
        registry = bundleregistry.get_registry()
        return registry.get_bundle_position(self.bundle_id, self.version)
    fixed_position = property(_get_fixed_position, None)

class FavoritePalette(ActivityPalette):
    __gtype_name__ = 'SugarFavoritePalette'

    def __init__(self, activity_info, journal_entries):
        ActivityPalette.__init__(self, activity_info)

        if journal_entries and journal_entries[0].get('icon-color', ''):
            color = XoColor(journal_entries[0]['icon-color'])
        else:
            color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                       style.COLOR_WHITE.get_svg()))

        self.props.icon = Icon(file=activity_info.get_icon(),
                               xo_color=color,
                               icon_size=gtk.ICON_SIZE_LARGE_TOOLBAR)

        if journal_entries:
            self.props.secondary_text = journal_entries[0]['title']

            menu_items = []
            for entry in journal_entries:
                icon_file_name = journal.misc.get_icon_name(entry)
                color = XoColor(entry.get('icon-color', None))

                menu_item = MenuItem(text_label=entry['title'],
                                     file_name=icon_file_name,
                                     xo_color=color)
                menu_item.connect('activate', self.__resume_entry_cb, entry)
                menu_items.append(menu_item)
                menu_item.show()

            if journal_entries:
                separator = gtk.SeparatorMenuItem()
                menu_items.append(separator)
                separator.show()

            for i in range(0, len(menu_items)):
                self.menu.insert(menu_items[i], i)

    def __resume_entry_cb(self, menu_item, entry):
        if entry is not None:
            activityfactory.create_with_object_id(self._bundle, entry['uid'])

class CurrentActivityIcon(CanvasIcon, hippo.CanvasItem):
    def __init__(self):
        CanvasIcon.__init__(self, cache=True)
        self._home_model = shell.get_model()
        self._home_activity = self._home_model.get_active_activity()

        if self._home_activity is not None:
            self._update()

        self._home_model.connect('active-activity-changed',
                                 self.__active_activity_changed_cb)

        self.connect('button-release-event', self.__button_release_event_cb)

    def __button_release_event_cb(self, icon, event):
        self._home_model.get_active_activity().get_window().activate(1)

    def _update(self):
        self.props.file_name = self._home_activity.get_icon_path()
        self.props.xo_color = self._home_activity.get_icon_color()
        self.props.size = style.STANDARD_ICON_SIZE

        if self.palette is not None:
            self.palette.destroy()
            self.palette = None

    def create_palette(self):
        if self._home_activity.is_journal():
            palette = JournalPalette(self._home_activity)
        else:
            palette = CurrentActivityPalette(self._home_activity)
        return palette

    def __active_activity_changed_cb(self, home_model, home_activity):
        self._home_activity = home_activity
        self._update()

class _MyIcon(MyIcon):
    __gtype_name__ = 'SugarFavoritesMyIcon'

    __gsignals__ = {
        'register-activate' : (gobject.SIGNAL_RUN_FIRST,
                                gobject.TYPE_NONE, ([]))
    }
    def __init__(self, scale):
        MyIcon.__init__(self, scale)

        self._power_manager = None
        self._palette_enabled = False
        self._register_menu = None

    def create_palette(self):
        if not self._palette_enabled:
            self._palette_enabled = True
            return

        presence_service = presenceservice.get_instance()
        owner = BuddyModel(buddy=presence_service.get_owner())
        palette = BuddyMenu(owner)

        client = gconf.client_get_default()
        backup_url = client.get_string('/desktop/sugar/backup_url')
        if not backup_url:
            self._register_menu = MenuItem(_('Register'), 'media-record')
            self._register_menu.connect('activate', self.__register_activate_cb)
            palette.menu.append(self._register_menu)
            self._register_menu.show()

        return palette

    def get_toplevel(self):
        return hippo.get_canvas_for_item(self).get_toplevel()

    def __register_activate_cb(self, menuitem):
        self.emit('register-activate')

    def remove_register_menu(self):
        self.palette.remove(self._register_menu)

class FavoritesSetting(object):

    _FAVORITES_KEY = "/desktop/sugar/desktop/favorites_layout"

    def __init__(self):
        client = gconf.client_get_default() 
        self._layout = client.get_string(self._FAVORITES_KEY)
        logging.debug('FavoritesSetting layout %r' % (self._layout))

        self._mode = None

        self.changed = dispatch.Signal()

    def get_layout(self):
        return self._layout

    def set_layout(self, layout):
        logging.debug('set_layout %r %r' % (layout, self._layout))
        if layout != self._layout:
            self._layout = layout

            client = gconf.client_get_default()
            client.set_string(self._FAVORITES_KEY, layout)

            self.changed.send(self)

    layout = property(get_layout, set_layout)

_favorites_settings = None

def get_settings():
    global _favorites_settings
    if _favorites_settings is None:
        _favorites_settings = FavoritesSetting()
    return _favorites_settings
