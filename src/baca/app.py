import asyncio
import dataclasses
from datetime import datetime
from pathlib import Path
from typing import List, Type
import json
import re

from textual import events
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import LoadingIndicator

from baca.components.contents import Content
from baca.components.events import (
    DoneLoading,
    FollowThis,
    OpenThisImage,
    Screenshot,
    SearchSubmitted,
)
from baca.components.windows import Alert, DictDisplay, SearchInputPrompt, ToC, contains_hebrew
from baca.config import load_config
from baca.ebooks import Ebook
from baca.exceptions import LaunchingFileError
from baca.models import Coordinate, KeyMap, ReadingHistory, SearchMode
from baca.utils.app_resources import get_resource_file
from baca.utils.keys_parser import dispatch_key
from baca.utils.systems import launch_file
from baca.utils.urls import is_url
from baca.models import Coordinate

@dataclasses.dataclass
class ReadingSession:
    ebook: Ebook
    content: Content
    ebook_state: ReadingHistory
    reading_progress: float = 0.0

    def contains_hebrew(self, txt: str):
        return bool(re.search(r'[\u0590-\u05FF]', txt))

    def __post_init__(self):
        """Runs automatically after the dataclass is initialized."""
        ebook_name = self.ebook.get_path().name
        if contains_hebrew(ebook_name):
            self.content.set_rtl_true()


class Baca(App):
    CSS_PATH = str(get_resource_file("style.css"))

    def __init__(self, ebook_paths: List[Path], ebook_class: Type[Ebook]):
        self.config = load_config()
        super().__init__()
        self.ebook_paths = ebook_paths
        self.ebook_class = ebook_class

        self.sessions: List[ReadingSession] = []
        self.current_index: int = 0
        self.reading_progress = 0.0
        self.search_mode = None

    def on_load(self, _: events.Load) -> None:
        # Use create_task for background loading to avoid run_worker error
        assert self._loop is not None
        asyncio.create_task(self.load_all_sessions())

    async def load_all_sessions(self):
        """Loads all books into memory but hides all except the first."""
        for i, path in enumerate(self.ebook_paths):
            # Load book data in a thread to keep UI responsive
            ebook = await asyncio.to_thread(self.ebook_class, path)
            content = Content(self.config, ebook)

            # Hide all books except the first one
            content.display = (i == 0)

            ebook_state, _ = await asyncio.to_thread(
                ReadingHistory.get_or_create,
                filepath=str(ebook.get_path()),
                defaults=dict(reading_progress=0.0)
            )

            session = ReadingSession(
                ebook=ebook,
                content=content,
                ebook_state=ebook_state,
                reading_progress=ebook_state.reading_progress
            )
            self.sessions.append(session)

            # Mount it immediately so it stays in the DOM
            await self.mount(content)

        if self.sessions:
            self.post_message(DoneLoading(self.sessions[0].content))

    async def on_done_loading(self, event: DoneLoading) -> None:
        def restore_initial_state() -> None:
            active = self.sessions[0]
            # Restore title and scroll position
            self.title = f"Baca | {active.ebook.get_meta().title}"
            self.reading_progress = active.reading_progress * self.screen.max_scroll_y
            self.screen.scroll_to(None, self.reading_progress, duration=0, animate=False)

            try:
                self.get_widget_by_id("startup-loader").remove()
            except NoMatches:
                pass

        self.call_after_refresh(restore_initial_state)

    async def action_switch_book(self) -> None:
        def fix_points(text):
            parts = [p.strip() for p in text.split('.') if p.strip()]

            # 2. Fix each sentence and join them back
            # We add the dot back to the logical end of each sentence before flipping
            fixed_sentences = []
            for sentence in parts:
                logical_sentence = sentence + "."
                fixed_sentences.append(logical_sentence)

            # 3. Join. The order of sentences in the list will be LTR
            final_output = " ".join(fixed_sentences)

            return final_output

        """Instantly toggles visibility between open books."""
        if len(self.sessions) < 2:
            return

        # 0. Get visible sentences in current book before switching
        current_session = self.sessions[self.current_index]
        current_y = int(self.screen.scroll_offset.y)
        current_session_lines = current_session.content.get_n_visible_sentences(current_y=current_y, n=4)
        current_session_lines = fix_points(current_session_lines)

        # 1. Save progress of current book
        if self.screen.max_scroll_y > 0:
            self.sessions[self.current_index].reading_progress = self.screen.scroll_y / self.screen.max_scroll_y

        # 2. Hide current book
        current_session.content.display = False

        # 3. Increment index
        self.current_index = (self.current_index + 1) % len(self.sessions)
        next_session = self.sessions[self.current_index]

        # 4. Show the next book
        next_session.content.display = True

        # 5. Alignment
        async def perform_alignment(current_session_lines):
            def get_matching_lines_from_json(lines_to_match, json_pth='matches.json'):
                # Open the file with utf-8 encoding to support Hebrew
                with open(json_pth, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # read lines from json and compare - make sure json's frame size and n are equals
                for lines_json in data:
                    if 'הוא טוען שהוא הרג אינקוויזיטור' in lines_json['heb']:
                        pass
                    if lines_json['eng'] == lines_to_match:
                        return lines_json['heb']
                    if lines_json['heb'] == lines_to_match:
                        return lines_json['eng']

                # no matches
                return None

            # get the lines to search for in next session
            next_session_lines = get_matching_lines_from_json(lines_to_match=current_session_lines)

            # Force focus so the app knows which widget is active
            next_session.content.focus()

            # Calc estimated location to start with
            if self.screen.max_scroll_y > 0:
                target_y = current_session.reading_progress * self.screen.max_scroll_y
                self.screen.scroll_to(None, target_y, duration=0, animate=False)

                # 1. Get current scroll position to use as the starting point
                current_y = int(self.screen.scroll_offset.y)
                start_pos = Coordinate(x=-1, y=current_y)

                # 2. Search in a radius from current line
                if next_session_lines:
                    match = await next_session.content.alignment_search(
                        pattern_str=next_session_lines,
                        current_coord=start_pos,
                        radius=1000
                    )
                else:
                    match = None

                # 3. Scroll result to the first line of the screen
                if match:
                    self.screen.scroll_to(y=match.y, animate=False)
                else:
                    await self.alert(f"Couldn't align books :(")

            self.title = f"Baca | {next_session.ebook.get_meta().title}"

        # We refresh layout first, then run the switch logic
        self.refresh(layout=True)
        self.call_after_refresh(perform_alignment, current_session_lines)

    def on_mount(self):
        def screen_watch_scroll_y_wrapper(old_watcher, screen):
            def new_watcher(old, new):
                result = old_watcher(old, new)
                if screen.max_scroll_y != 0:
                    self.reading_progress = new / screen.max_scroll_y
                return result

            return new_watcher

        screen_scroll_y_watcher = getattr(self.screen, "watch_scroll_y")
        setattr(self.screen, "watch_scroll_y", screen_watch_scroll_y_wrapper(screen_scroll_y_watcher, self.screen))

    @property
    def ebook(self) -> Ebook:
        return self.sessions[self.current_index].ebook

    @property
    def ebook_state(self) -> ReadingHistory:
        return self.sessions[self.current_index].ebook_state

    @property
    def content(self) -> Content:
        """Find the currently visible content widget."""
        return self.sessions[self.current_index].content

    async def on_key(self, event: events.Key) -> None:
        keymaps = self.config.keymaps

        if event.key == "tab":
            await self.action_switch_book()
            return

        await dispatch_key(
            [
                KeyMap(keymaps.close, self.action_cancel_search_or_quit),
                KeyMap(keymaps.scroll_down, self.screen.action_scroll_down),
                KeyMap(keymaps.scroll_up, self.screen.action_scroll_up),
                KeyMap(keymaps.page_up, self.action_page_up),
                KeyMap(keymaps.page_down, self.action_page_down),
                KeyMap(keymaps.home, self.screen.action_scroll_home),
                KeyMap(keymaps.end, self.screen.action_scroll_end),
                KeyMap(keymaps.open_toc, self.action_open_toc),
                KeyMap(keymaps.open_metadata, self.action_open_metadata),
                KeyMap(keymaps.open_help, self.action_open_help),
                KeyMap(keymaps.toggle_dark, self.action_toggle_dark),
                KeyMap(keymaps.screenshot, lambda: self.post_message(Screenshot())),
                KeyMap(keymaps.search_forward, lambda: self.action_input_search(forward=True)),
                KeyMap(keymaps.search_backward, lambda: self.action_input_search(forward=False)),
                KeyMap(keymaps.next_match, self.action_search_next),
                KeyMap(keymaps.prev_match, self.action_search_prev),
                KeyMap(keymaps.confirm, self.action_stop_search),
            ],
            event,
        )

    def compose(self) -> ComposeResult:
        yield LoadingIndicator(id="startup-loader")

    # ... (Keep all alert, metadata, search, and help methods exactly as they were) ...
    async def alert(self, message: str) -> None:
        alert = Alert(self.config, message)
        await self.mount(alert)

    async def action_open_metadata(self) -> None:
        if self.metadata_window is None:
            metadata_window = DictDisplay(
                config=self.config, id="metadata", title="Metadata", data=dataclasses.asdict(self.ebook.get_meta())
            )
            await self.mount(metadata_window)

    def action_page_down(self) -> None:
        if not self.screen.allow_vertical_scroll:
            raise SkipAction()
        self.screen.scroll_page_down(duration=self.config.page_scroll_duration)

    def action_page_up(self) -> None:
        if not self.screen.allow_vertical_scroll:
            raise SkipAction()
        self.screen.scroll_page_up(duration=self.config.page_scroll_duration)

    async def action_input_search(self, forward: bool) -> None:
        await self.mount(SearchInputPrompt(forward=forward))

    async def action_search_next(self) -> bool:
        if self.search_mode is not None:
            new_coord = await self.content.search_next(
                self.search_mode.pattern_str,
                self.search_mode.current_coord,
                self.search_mode.forward,
            )
            if new_coord is not None:
                self.search_mode = dataclasses.replace(self.search_mode, current_coord=new_coord)
                return True
            else:
                await self.alert(f"Found no match: '{self.search_mode.pattern_str}'")
        return False

    async def action_search_prev(self) -> None:
        if self.search_mode is not None:
            new_coord = await self.content.search_next(
                self.search_mode.pattern_str,
                self.search_mode.current_coord,
                not self.search_mode.forward,
            )
            if new_coord is not None:
                self.search_mode = dataclasses.replace(self.search_mode, current_coord=new_coord)

    async def action_stop_search(self) -> None:
        if self.search_mode is not None:
            self.search_mode = None
            await self.content.clear_search()

    async def action_open_help(self) -> None:
        if self.help_window is None:
            keymap_data = {
                k.replace("_", " ").title(): ",".join(v) for k, v in dataclasses.asdict(self.config.keymaps).items()
            }
            help_window = DictDisplay(config=self.config, id="help", title="Keymaps", data=keymap_data)
            await self.mount(help_window)

    async def action_open_toc(self) -> None:
        if self.toc_window is None:
            toc_entries = list(self.ebook.get_toc())
            if len(toc_entries) == 0:
                return await self.alert("No content navigations for this ebook.")
            initial_index = 0
            toc_values = [e.value for e in toc_entries]
            for s in self.content.get_navigables():
                if s.nav_point is not None and s.nav_point in toc_values:
                    if self.screen.scroll_offset.y >= s.virtual_region.y:
                        initial_index = toc_values.index(s.nav_point)
                    else:
                        break
            toc = ToC(self.config, entries=toc_entries, initial_index=initial_index)
            await self.mount(toc)

    async def action_cancel_search_or_quit(self) -> None:
        if self.search_mode is not None:
            self.screen.scroll_to(
                0, self.search_mode.saved_position * self.screen.max_scroll_y, duration=self.config.page_scroll_duration
            )
            await self.action_stop_search()
        else:
            await self.action_quit()

    async def action_link(self, link: str) -> None:
        if is_url(link):
            try:
                await launch_file(link)
            except LaunchingFileError as e:
                await self.alert(str(e))
        elif link in [n.nav_point for n in self.content.get_navigables()]:
            self.content.scroll_to_section(link)
        else:
            await self.alert(f"No nav point found in document: {link}")

    async def on_search_submitted(self, message: SearchSubmitted) -> None:
        self.search_mode = SearchMode(
            pattern_str=message.value,
            current_coord=Coordinate(-1 if message.forward else self.content.size.width, self.screen.scroll_offset.y),
            forward=message.forward,
            saved_position=self.reading_progress,
        )
        is_found = await self.action_search_next()
        if not is_found:
            self.search_mode = None

    async def on_follow_this(self, message: FollowThis) -> None:
        self.content.scroll_to_section(message.nav_point)
        self.call_after_refresh(self.toc_window.remove)

    async def on_open_this_image(self, message: OpenThisImage) -> None:
        try:
            filename, bytestr = self.ebook.get_img_bytestr(message.value)
            tmpfilepath = self.ebook.get_tempdir() / filename
            with open(tmpfilepath, "wb") as img_tmp:
                img_tmp.write(bytestr)
            await launch_file(tmpfilepath, preferred=self.config.preferred_image_viewer)
        except LaunchingFileError as e:
            await self.alert(f"Error opening an image: {e}")

    async def on_screenshot(self, _: Screenshot) -> None:
        self.save_screenshot(f"baca_{datetime.now().isoformat()}.svg")

    def run(self, *args, **kwargs):
        try:
            return super().run(*args, **kwargs)
        finally:
            for session in self.sessions:
                meta = session.ebook.get_meta()
                session.ebook_state.last_read = datetime.now()
                session.ebook_state.title = meta.title
                session.ebook_state.author = meta.creator
                if session == self.sessions[self.current_index]:
                    session.ebook_state.reading_progress = self.reading_progress
                else:
                    session.ebook_state.reading_progress = session.reading_progress
                session.ebook_state.save()
                session.ebook.cleanup()

    def get_css_variables(self):
        original = super().get_css_variables()
        return {
            **original,
            **{
                "text-max-width": self.config.max_text_width,
                "text-justification": self.config.text_justification,
                "dark-bg": self.config.dark.bg,
                "dark-fg": self.config.dark.fg,
                "dark-accent": self.config.dark.accent,
                "light-bg": self.config.light.bg,
                "light-fg": self.config.light.fg,
                "light-accent": self.config.light.accent,
            },
        }

    @property
    def toc_window(self) -> ToC | None:
        try:
            return self.query_one(ToC.__name__, ToC)
        except NoMatches:
            return None

    @property
    def metadata_window(self) -> DictDisplay | None:
        try:
            return self.get_widget_by_id("metadata", DictDisplay)
        except NoMatches:
            return None

    @property
    def help_window(self) -> DictDisplay | None:
        try:
            return self.get_widget_by_id("help", DictDisplay)
        except NoMatches:
            return None