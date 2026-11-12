"""Keyboard selection for finite provider/model lists."""

from collections.abc import Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.completion import CompleteEvent, FuzzyCompleter, WordCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from src.utils.interrupt import terminal_input_lock


def select_menu(title: str, choices: Sequence[str], current: str | None = None) -> str | None:
    """Type to fuzzy-filter, navigate matches with arrows, or cancel."""
    values = list(choices)
    if not values:
        return None
    selected = values.index(current) if current in values else 0
    initial_selection = selected
    matches = list(values)
    bindings = KeyBindings()
    search = TextArea(height=1, prompt="Search: ", multiline=False, wrap_lines=False)
    completer = FuzzyCompleter(WordCompleter(values, sentence=True), pattern=r"^.+")

    def filter_choices(buffer) -> None:
        nonlocal matches, selected
        query = buffer.text
        if query:
            matches = [item.text for item in completer.get_completions(
                Document(query, cursor_position=len(query)), CompleteEvent()
            )]
            selected = 0
        else:
            matches = list(values)
            selected = initial_selection

    search.buffer.on_text_changed += filter_choices

    def rows():
        fragments = []
        if not matches:
            return [("class:empty", "  No matches. Edit search or press Ctrl+U to clear.")]
        for index, value in enumerate(matches):
            style = "class:selected" if index == selected else ""
            marker = "> " if index == selected else "  "
            fragments.append((style, marker + value))
            if index + 1 < len(matches):
                fragments.append(("", "\n"))
        return fragments

    def move(delta: int) -> None:
        nonlocal selected
        selected = max(0, min(len(matches) - 1, selected + delta))

    @bindings.add("up")
    def up(event):
        move(-1)

    @bindings.add("down")
    def down(event):
        move(1)

    @bindings.add("pageup")
    def page_up(event):
        move(-10)

    @bindings.add("pagedown")
    def page_down(event):
        move(10)

    @bindings.add("home")
    def home(event):
        move(-len(matches))

    @bindings.add("end")
    def end(event):
        move(len(matches))

    @bindings.add("enter")
    def confirm(event):
        if matches:
            event.app.exit(result=matches[selected])

    @bindings.add("c-u")
    def clear_search(event):
        search.buffer.reset()
        filter_choices(search.buffer)

    @bindings.add("escape", eager=True)
    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    control = FormattedTextControl(
        rows, focusable=True, show_cursor=False,
        get_cursor_position=lambda: Point(x=0, y=selected),
    )
    window = Window(
        control, height=min(12, len(values)), wrap_lines=False,
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )
    application: Application[str | None] = Application(
        layout=Layout(HSplit([
            Window(FormattedTextControl(title), height=1),
            search,
            window,
            Window(FormattedTextControl(lambda: f"Type to search · ↑↓ Select · Enter Confirm · Esc Cancel · Ctrl+U Clear · {selected + 1 if matches else 0}/{len(matches)} of {len(values)}"), height=1),
        ]), focused_element=search),
        key_bindings=bindings,
        style=Style.from_dict({"selected": "reverse bold", "empty": "italic"}),
        full_screen=False, erase_when_done=True,
    )
    try:
        with terminal_input_lock:
            return application.run()
    except (EOFError, KeyboardInterrupt):
        return None
