"""Pictures of a disc and its titles, loaded on a worker thread as they are shown."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GObject, Graphene, Gsk, Gtk  # noqa: E402

from ..models import MediaItem  # noqa: E402
from . import formatting as fmt  # noqa: E402
from .bridge import run_in_thread  # noqa: E402
from .widgets import caption_label, icon_button, link_markup, open_uri  # noqa: E402

RATIO = 16 / 10  # 320 by 200, the usual Amiga and Atari ST low resolution screen
CACHE_SIZE = 40
PIXEL_ART_WIDTH = 800  # smaller pictures are scaled up without smoothing


class PixelPaintable(GObject.Object, Gdk.Paintable):
    """A texture drawn with nearest-neighbour scaling when it is small.

    A 320 by 200 menu screen scaled up with smoothing looks blurred; square
    pixels are how it looked on a monitor.
    """

    __gtype_name__ = "PirateFinderPixelPaintable"

    def __init__(self, texture: Gdk.Texture) -> None:
        super().__init__()
        self.texture = texture
        small = texture.get_width() <= PIXEL_ART_WIDTH
        self._filter = Gsk.ScalingFilter.NEAREST if small else Gsk.ScalingFilter.TRILINEAR

    def do_get_intrinsic_width(self) -> int:
        return self.texture.get_width()

    def do_get_intrinsic_height(self) -> int:
        return self.texture.get_height()

    def do_get_intrinsic_aspect_ratio(self) -> float:
        return self.texture.get_width() / max(1, self.texture.get_height())

    def do_get_flags(self) -> Gdk.PaintableFlags:
        return Gdk.PaintableFlags.SIZE | Gdk.PaintableFlags.CONTENTS

    def do_snapshot(self, snapshot: Gdk.Snapshot, width: float, height: float) -> None:
        rect = Graphene.Rect().init(0, 0, width, height)
        snapshot.append_scaled_texture(self.texture, self._filter, rect)


class RatioFrame(Gtk.Widget):
    """Holds one child at a fixed width to height ratio, as wide as it is given.

    Gtk.AspectFrame centres its child in whatever height the parent box
    offers; this asks for the height that matches its width instead.
    """

    __gtype_name__ = "PirateFinderRatioFrame"

    def __init__(self, ratio: float, child: Gtk.Widget) -> None:
        super().__init__()
        self.ratio = ratio
        self.child = child
        child.set_parent(self)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        minimum, natural, _base, _baseline = self.child.measure(orientation, -1)
        if orientation == Gtk.Orientation.HORIZONTAL:
            return minimum, max(natural, 320), -1, -1
        height = int(for_size / self.ratio) if for_size > 0 else int(320 / self.ratio)
        return max(minimum, height), max(minimum, height), -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        self.child.allocate(width, height, baseline, None)

    def do_dispose(self) -> None:
        if self.child is not None:
            self.child.unparent()
            self.child = None


class MediaView(Gtk.Box):
    """A picture with arrows to step through the pictures, a caption and a credit.

    ``load(item)`` runs on a worker thread and returns a ``Gdk.Texture`` or
    None; ``source_names()`` maps source ids to display names.
    """

    def __init__(
        self,
        load: Callable[[MediaItem], Gdk.Texture | None],
        source_names: Callable[[], dict[str, str]],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._load = load
        self._source_names = source_names
        self.items: list[MediaItem] = []
        self.subjects: dict[int | None, str] = {}
        self.index = 0
        self.enabled = True
        self.off_reason = ""
        self._generation = 0
        self._cache: OrderedDict[str, Gdk.Texture | None] = OrderedDict()
        self.loading = False

        overlay = Gtk.Overlay()
        frame = RatioFrame(RATIO, overlay)
        frame.add_css_class("media-frame")
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.frame = frame
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)
        overlay.set_child(self.stack)

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.stack.add_named(self.picture, "picture")
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner.set_size_request(32, 32)
        self.stack.add_named(spinner, "loading")
        placeholder = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
            margin_start=18,
            margin_end=18,
        )
        self.placeholder_icon = Gtk.Image(pixel_size=48, icon_name="image-missing-symbolic")
        self.placeholder_icon.add_css_class("dim-label")
        placeholder.append(self.placeholder_icon)
        self.placeholder_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.placeholder_title.add_css_class("heading")
        placeholder.append(self.placeholder_title)
        self.placeholder_text = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        placeholder.append(self.placeholder_text)
        self.preferences_button = Gtk.Button.new_with_mnemonic("Open _Preferences")
        self.preferences_button.set_action_name("win.preferences")
        self.preferences_button.add_css_class("pill")
        self.preferences_button.set_halign(Gtk.Align.CENTER)
        self.preferences_button.set_margin_top(6)
        placeholder.append(self.preferences_button)
        self.stack.add_named(placeholder, "placeholder")

        self.previous_button = icon_button(
            "go-previous-symbolic", "Previous Picture", lambda _b: self.step(-1), flat=False
        )
        self.next_button = icon_button(
            "go-next-symbolic", "Next Picture", lambda _b: self.step(1), flat=False
        )
        for button, align in (
            (self.previous_button, Gtk.Align.START),
            (self.next_button, Gtk.Align.END),
        ):
            button.add_css_class("osd")
            button.add_css_class("circular")
            button.set_halign(align)
            button.set_margin_start(8)
            button.set_margin_end(8)
            overlay.add_overlay(button)
        self.append(frame)

        captions = Gtk.Box(spacing=12)
        self.caption = caption_label(hexpand=True)
        self.caption.set_wrap(True)
        captions.append(self.caption)
        self.credit = caption_label(dim=False, xalign=1, use_markup=True)
        self.credit.set_wrap(True)
        self.credit.connect("activate-link", self._on_link)
        captions.append(self.credit)
        self.captions = captions
        self.append(captions)

    # Showing pictures

    def show(
        self,
        items: Sequence[MediaItem],
        subjects: dict[int | None, str],
        *,
        enabled: bool,
        first: int = 0,
        off_reason: str = "",
    ) -> None:
        """Show ``items`` starting at ``first``; ``subjects`` names each title and the disc.

        ``off_reason`` says why pictures are not fetched when ``enabled`` is False.
        """
        self.items = list(items)
        self.subjects = dict(subjects)
        self.enabled = enabled
        self.off_reason = off_reason
        self.index = max(0, min(first, len(self.items) - 1))
        self._render()

    def step(self, offset: int) -> None:
        if len(self.items) > 1:
            self.index = (self.index + offset) % len(self.items)
            self._render()

    @property
    def current(self) -> MediaItem | None:
        if self.enabled and 0 <= self.index < len(self.items):
            return self.items[self.index]
        return None

    def _render(self) -> None:
        self._generation += 1
        many = self.enabled and len(self.items) > 1
        self.previous_button.set_visible(many)
        self.next_button.set_visible(many)
        item = self.current
        if item is None:
            self.loading = False
            self.captions.set_visible(False)
            if not self.enabled:
                self._placeholder(
                    "camera-photo-symbolic",
                    "Pictures Are Off",
                    self.off_reason
                    or "Turn on Download Screenshots and Background Information in Preferences.",
                    button=True,
                )
            else:
                self._placeholder("image-missing-symbolic", "No Pictures", "")
            return
        self.captions.set_visible(True)
        subject = self.subjects.get(item.content_id, self.subjects.get(None, ""))
        self.caption.set_text(fmt.media_caption(item, self.index, len(self.items), subject))
        credit = fmt.media_credit(item, self._source_names())
        self.credit.set_markup(link_markup(credit, item.page_url or item.url))
        self.credit.set_visible(bool(credit))
        self.picture.set_alternative_text(self.caption.get_text())
        if item.url in self._cache:
            self._set_texture(self._cache[item.url])
            return
        self.loading = True
        self.stack.set_visible_child_name("loading")
        generation = self._generation
        load = self._load

        def done(texture: Gdk.Texture | None) -> None:
            self._remember(item.url, texture)
            if generation == self._generation:
                self._set_texture(texture)

        def failed(_error: BaseException) -> None:
            if generation == self._generation:
                self.loading = False
                self._placeholder(
                    "image-missing-symbolic", "The Picture Could Not Be Loaded", "", keep=True
                )

        run_in_thread(lambda: load(item), done, failed, name="media")

    def _remember(self, url: str, texture: Gdk.Texture | None) -> None:
        self._cache[url] = texture
        self._cache.move_to_end(url)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    def _set_texture(self, texture: Gdk.Texture | None) -> None:
        self.loading = False
        if texture is None:
            self._placeholder(
                "image-missing-symbolic", "The Picture Is Not Available", "", keep=True
            )
            return
        self.picture.set_paintable(PixelPaintable(texture))
        self.stack.set_visible_child_name("picture")

    def _placeholder(
        self, icon: str, title: str, description: str, *, button: bool = False, keep: bool = False
    ) -> None:
        self.picture.set_paintable(None)
        self.placeholder_icon.set_from_icon_name(icon)
        self.placeholder_title.set_text(title)
        self.placeholder_text.set_text(description)
        self.placeholder_text.set_visible(bool(description))
        self.preferences_button.set_visible(button)
        self.captions.set_visible(keep and self.captions.get_visible())
        self.stack.set_visible_child_name("placeholder")

    def _on_link(self, _label, uri: str) -> bool:
        open_uri(self, uri)
        return True
