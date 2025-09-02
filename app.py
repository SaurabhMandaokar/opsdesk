import json
import os
import shlex
from pathlib import Path
from typing import List, Dict, Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import Button, Static, Footer, Header, TabbedContent, TabPane
from textual_terminal import Terminal  # pip install textual-terminal


def _resolve_shell() -> str:
    """
    Pick a reasonable interactive shell for the container/host.
    Prefers $DEFAULT_SHELL, then $SHELL, then /bin/bash, then /bin/sh.
    """
    for candidate in (
        os.environ.get("DEFAULT_SHELL"),
        os.environ.get("SHELL"),
        "/bin/bash",
        "/bin/sh",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return "/bin/sh"


SETTINGS_DIR = Path(os.environ.get("OPSDESK_SETTINGS_DIR", "/app/settings"))
DEFAULT_SHELL = _resolve_shell()


def load_tabs() -> List[Dict[str, Any]]:
    """
    Discover all *.json under SETTINGS_DIR.
    Each JSON should look like:
      {
        "title": "GKE",
        "order": 1,
        "buttons": [
          {"label": "Pods", "cmd": "kubectl get pods -A | head"},
          {"label": "Contexts", "cmd": "kubectl config get-contexts"}
        ]
      }
    """
    tabs: List[Dict[str, Any]] = []

    if not SETTINGS_DIR.exists():
        # No settings directory at all
        return [{
            "order": 9999,
            "title": "No settings directory",
            "buttons": [{
                "label": "Info",
                "cmd": f'echo "Settings directory not found: {SETTINGS_DIR}"'
            }],
            "__file": "(missing)"
        }]

    for p in sorted(SETTINGS_DIR.glob("*.json")):
        # Skip dotfiles/backups if any
        if p.name.startswith(".") or p.name.endswith(".example.json"):
            continue

        try:
            text = p.read_text(encoding="utf-8")
            data = json.loads(text)

            # Minimal schema guardrails
            if not isinstance(data, dict):
                raise ValueError("Top-level JSON must be an object")
            if "buttons" in data and not isinstance(data["buttons"], list):
                raise ValueError("'buttons' must be a list")
            for i, b in enumerate(data.get("buttons", [])):
                if not isinstance(b, dict) or "cmd" not in b:
                    raise ValueError(f"buttons[{i}] must be an object with a 'cmd' field")

            data["__file"] = p.name
            tabs.append(data)

        except Exception as e:
            tabs.append({
                "order": 9999,
                "title": f"Error in {p.name}",
                "buttons": [{"label": "Parse error", "cmd": f"echo {str(e)!r}"}],
                "__file": p.name,
            })

    if not tabs:
        # Directory exists but no matching files
        tabs.append({
            "order": 9999,
            "title": "No JSON found",
            "buttons": [{
                "label": "Info",
                "cmd": f'echo "No *.json files found in {SETTINGS_DIR}"'
            }],
            "__file": "(empty)"
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
            # store command in tooltip (ID left empty to avoid Textual ID constraints)
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
                            # (This branch rarely hits now; we create info/error panes above.)
                            yield Static(f"No settings/*.json found in {SETTINGS_DIR}.", classes="tab-title")
            # Right-side terminal
            self.term = Terminal(command=f"{DEFAULT_SHELL}")
            yield self.term
        yield Footer()

    def on_mount(self) -> None:
        if self.term is not None:
            self.term.start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # get the command from tooltip
        cmd = (event.button.tooltip or "").strip()
        if not cmd:
            return

        # After running the command, leave the terminal open and exec the shell again
        # so the user stays in an interactive session.
        trailer = f'; echo; echo [done] {shlex.quote(cmd)}; exec {shlex.quote(DEFAULT_SHELL)}'
        wrapped = f'{DEFAULT_SHELL} -lc {shlex.quote(cmd + trailer)}'

        # Replace terminal for a clean run
        assert self.term is not None
        self.term.remove()
        self.term = Terminal(command=wrapped)
        self.mount(self.term)
        self.term.start()


if __name__ == "__main__":
    OpsDesk().run()
