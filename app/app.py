import json, os, shlex
from pathlib import Path
from typing import List, Dict, Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import Button, Static, Footer, Header, TabbedContent, TabPane
from textual_terminal import Terminal  # pip install textual-terminal

SETTINGS_DIR = Path(os.environ.get("OPSDESK_SETTINGS_DIR", "/app/settings"))
DEFAULT_SHELL = os.environ.get("SHELL", "/bin/bash")


def load_tabs() -> List[Dict[str, Any]]:
    tabs: List[Dict[str, Any]] = []
    if SETTINGS_DIR.exists():
        for p in sorted(SETTINGS_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["__file"] = p.name
                tabs.append(data)
            except Exception as e:
                tabs.append({
                    "order": 9999,
                    "title": f"Error in {p.name}",
                    "buttons": [{"label": "Parse error", "cmd": f"echo {str(e)!r}"}],
                    "__file": p.name,
                })
    tabs.sort(key=lambda d: (d.get("order", 9999), d.get("__file", "")))
    return tabs


class TabButtons(ScrollableContainer):
    def __init__(self, tab_data: Dict[str, Any]) -> None:
        super().__init__()
        self.tab_data = tab_data

    def compose(self) -> ComposeResult:
        title = self.tab_data.get("title") or self.tab_data.get("__file", "Untitled")
        yield Static(title, classes="tab-title")
        for btn in self.tab_data.get("buttons", []):
            label = btn.get("label") or btn.get("cmd", "")
            cmd = btn.get("cmd", "")
            # 🔧 store command in tooltip (ID left empty to avoid Textual ID rules)
            yield Button(label, tooltip=cmd)


class OpsDesk(App):
    CSS = """
    Screen { layout: vertical; }
    .body { height: 1fr; }
    #left { width: 38; border-right: solid $panel; }
    .tab-title { padding: 1 1; text-style: bold; border-bottom: solid $panel; }
    Button { margin: 1 1; }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self._tabs_data: List[Dict[str, Any]] = load_tabs()
        self.term: Terminal | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(classes="body"):
            with Vertical(id="left"):
                with TabbedContent():
                    if self._tabs_data:
                        for idx, data in enumerate(self._tabs_data):
                            title = data.get("title") or data.get("__file", f"Tab {idx+1}")
                            with TabPane(title=title, id=f"pane-{idx}"):
                                yield TabButtons(data)
                    else:
                        with TabPane(title="No Tabs"):
                            yield Static("No settings/*.json found.", classes="tab-title")
            self.term = Terminal(command=f"{DEFAULT_SHELL}")
            yield self.term
        yield Footer()

    def on_mount(self) -> None:
        if self.term is not None:
            self.term.start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # 🔧 pull the command from tooltip
        cmd = (event.button.tooltip or "").strip()
        if not cmd:
            return

        wrapped = f'{DEFAULT_SHELL} -lc {shlex.quote(cmd + f"; echo; echo [done] {shlex.quote(cmd)}; exec " + DEFAULT_SHELL)}'

        # Replace terminal for a clean run
        assert self.term is not None
        self.term.remove()
        self.term = Terminal(command=wrapped)
        self.mount(self.term)
        self.term.start()


if __name__ == "__main__":
    OpsDesk().run()
