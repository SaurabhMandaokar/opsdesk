
import asyncio
import json
import os
import shlex
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import glob

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import Button, Static, Footer, Header, TabbedContent, TabPane, Log

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('opsdesk.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ------------------ config ------------------

DEFAULT_SHELL = os.environ.get("SHELL", "/bin/sh")
SETTINGS_DIR = Path(os.environ.get("OPSDESK_SETTINGS_DIR", Path.home() / "documents" / "opsdesk_settings"))


# ---------------- data loading --------------

def _normalize_to_menu(data: Dict[str, Any], file_name: str) -> Dict[str, Any]:
    """
    Convert {buttons:[...]} to nested {items:[...]} if needed.
    Supports:
      - Action: {"label": "...", "cmd": "..."}
      - Submenu: {"label": "...", "items": [...]}
      - Picker: {"label": "...", "picker": {"glob": "...", "env_var": "KUBECONFIG"}, "actions": [...]}
    """
    root: Dict[str, Any] = {
        "title": data.get("title") or file_name,
        "order": data.get("order", 9999),
    }

    if isinstance(data.get("items"), list):
        root["items"] = data["items"]
    elif isinstance(data.get("buttons"), list):
        root["items"] = [
            {"label": (b.get("label") or b.get("cmd", "")), "cmd": b["cmd"]}
            for b in data["buttons"] if isinstance(b, dict) and "cmd" in b
        ]
    else:
        root["items"] = [{"label": "Info", "cmd": f'echo "No actions in {file_name}"'}]

    def _key(x: Dict[str, Any]):
        return (x.get("order", 9999), str(x.get("label", "")))

    root["items"] = sorted(root["items"], key=_key)
    return root


def load_tabs() -> List[Dict[str, Any]]:
    """Return a list of normalized tab menus from *.json in SETTINGS_DIR."""
    logger.info(f"Loading tabs from {SETTINGS_DIR}")
    tabs: List[Dict[str, Any]] = []

    if not SETTINGS_DIR.exists():
        logger.warning(f"Settings directory does not exist: {SETTINGS_DIR}")
        return [{
            "order": 9999,
            "title": "No settings directory",
            "items": [{"label": "Info", "cmd": f'echo "Settings not found: {SETTINGS_DIR}"'}],
            "__file": "(missing)",
        }]

    files = sorted(
        p for p in SETTINGS_DIR.glob("*.json")
        if not p.name.startswith(".") and not p.name.endswith(".example.json")
    )
    logger.info(f"Found {len(files)} JSON files: {[p.name for p in files]}")

    if not files:
        logger.warning(f"No JSON files found in {SETTINGS_DIR}")
        return [{
            "order": 9999,
            "title": "No JSON found",
            "items": [{"label": "Info", "cmd": f'echo "No *.json in {SETTINGS_DIR}"'}],
            "__file": "(empty)",
        }]

    for p in files:
        try:
            logger.debug(f"Parsing {p.name}")
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Top-level JSON must be an object")
            norm = _normalize_to_menu(data, p.name)
            norm["__file"] = p.name
            tabs.append(norm)
            logger.info(f"Successfully loaded {p.name} with {len(norm.get('items', []))} items")
        except Exception as e:
            logger.error(f"Error parsing {p.name}: {e}")
            tabs.append({
                "order": 9999,
                "title": f"Error in {p.name}",
                "items": [{"label": "Parse error", "cmd": f"echo {str(e)!r}"}],
                "__file": p.name,
            })

    tabs.sort(key=lambda d: (d.get("order", 9999), d.get("__file", "")))
    logger.info(f"Loaded {len(tabs)} tabs total")
    return tabs


# --------------- UI widgets ----------------

class MenuPane(Vertical):
    """
    Left-side nested menu with a single-step Back.
    - Submenu items push into self.stack
    - Picker items generate a list of files, then a per-file action list
    - Action items run commands in the right-side Log
    """

    def __init__(self, root_menu: Dict[str, Any], tab_id: str) -> None:
        super().__init__()
        self.root = root_menu
        self.tab_id = tab_id
        self.stack: List[Dict[str, Any]] = [self.root]
        self._body: Optional[ScrollableContainer] = None

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), classes="tab-title")
        with Horizontal():
            yield Button("◀ Back", id=f"{self.tab_id}-back")
        self._body = ScrollableContainer()
        yield self._body

    def on_mount(self) -> None:
        logger.info(f"MenuPane mounted for tab {self.tab_id}")
        self._render_items()

    # ------- helpers

    def _title_text(self) -> str:
        node = self.stack[-1]
        title = node.get("title") or node.get("label") or self.root.get("title") or "Menu"
        logger.debug(f"Title for {self.tab_id}: {title}")
        return title

    def _current_items(self) -> List[Dict[str, Any]]:
        node = self.stack[-1]
        items = node.get("items", [])
        items_list = items if isinstance(items, list) else []
        logger.debug(f"Current items for {self.tab_id}: {len(items_list)} items")
        return items_list

    def _render_items(self) -> None:
        logger.info(f"Rendering items for {self.tab_id}, stack depth: {len(self.stack)}")
        if not self._body:
            logger.warning("No body container found")
            return
        # Clear
        for child in list(self._body.children):
            child.remove()
        # Title for current level
        self._body.mount(Static(self._title_text(), classes="tab-title"))
        # Items
        items = self._current_items()
        logger.info(f"Rendering {len(items)} items")
        for idx, item in enumerate(items):
            label = str(item.get("label", f"Item {idx+1}"))
            logger.debug(f"Item {idx}: {label} - {item}")
            if "items" in item and isinstance(item["items"], list):
                btn = Button(f"📁 {label}", id=f"{self.tab_id}-submenu-{idx}")
                logger.debug(f"Created submenu button: {btn.id}")
                self._body.mount(btn)
            elif "picker" in item and isinstance(item["picker"], dict):
                btn = Button(f"🗂  {label}", id=f"{self.tab_id}-picker-{idx}")
                logger.debug(f"Created picker button: {btn.id}")
                self._body.mount(btn)
            elif "cmd" in item:
                btn = Button(f"▶ {label}", tooltip=str(item["cmd"]))
                logger.debug(f"Created action button: {label} -> {item['cmd']}")
                self._body.mount(btn)
            else:
                btn = Button(f"• {label}", tooltip=f'echo "No action for: {label}"')
                logger.debug(f"Created info button: {label}")
                self._body.mount(btn)
        # Focus first
        first_btn = self._body.query(Button).first()
        if first_btn:
            logger.debug(f"Focusing first button: {first_btn.id}")
            first_btn.focus()
        else:
            logger.warning("No buttons found to focus")

    # ------- dynamic picker expansion

    def _expand_picker_node(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Return a new submenu node produced from a picker item."""
        logger.info(f"Expanding picker node: {item.get('label')}")
        picker = item.get("picker", {})
        glob_pat = str(picker.get("glob", "")).strip()
        logger.debug(f"Picker glob pattern: {glob_pat}")
        if not glob_pat:
            logger.warning("No glob pattern found in picker")
            return {"title": "No files", "items": [{"label": "Info", "cmd": 'echo "picker.glob missing"'}]}

        glob_expanded = os.path.expandvars(os.path.expanduser(glob_pat))
        logger.debug(f"Expanded glob pattern: {glob_expanded}")

        # Works fine with /absolute/path/*.yaml and ./relative/path/*.yaml
        paths = [Path(p) for p in sorted(glob.glob(glob_expanded))]
        logger.info(f"Found {len(paths)} files matching pattern: {[str(p) for p in paths]}")

        if not paths:
            logger.warning(f"No files found matching pattern: {glob_expanded}")
            return {
                "title": f"No matches for {glob_pat}",
                "items": [{"label": "Info", "cmd": f'echo "No files for {glob_pat}"'}],
            }

        env_var = str(picker.get("env_var", "KUBECONFIG"))
        actions = item.get("actions", [])  # template actions for each file
        logger.debug(f"Using env_var: {env_var}, actions: {len(actions)}")

        # Build a per-file submenu entry that shows the actions
        choices: List[Dict[str, Any]] = []
        for p in paths:
            name = p.name
            stem = p.stem
            path_str = str(p.resolve())
            logger.debug(f"Processing file: {name} -> {path_str}")

            # Build concrete actions for this file
            per_file_items: List[Dict[str, Any]] = []
            for act in actions:
                if not isinstance(act, dict) or "label" not in act:
                    continue
                label = str(act.get("label", "Action"))

                # Template command (optional)
                raw_cmd = str(act.get("cmd", "")).format(
                    PATH=path_str, NAME=name, STEM=stem, KUBECONFIG=path_str
                ).strip()

                # If the template didn't reference KUBECONFIG, prefix it
                if env_var and raw_cmd and ("KUBECONFIG=" not in raw_cmd) and ("{KUBECONFIG}" not in act.get("cmd", "")):
                    cmd = f'{env_var}={shlex.quote(path_str)} {raw_cmd}'
                else:
                    cmd = raw_cmd

                if not cmd:
                    # Safe fallback
                    cmd = f'echo "Selected {name}"'

                logger.debug(f"Action for {name}: {label} -> {cmd}")
                per_file_items.append({"label": label, "cmd": cmd})

            # If no actions provided, offer a minimal default
            if not per_file_items:
                logger.debug(f"No actions for {name}, using defaults")
                per_file_items = [
                    {"label": "Current context", "cmd": f'{env_var}={shlex.quote(path_str)} kubectl config current-context'},
                    {"label": "Namespaces", "cmd": f'{env_var}={shlex.quote(path_str)} kubectl get ns'},
                    {"label": "Pods (top 20)", "cmd": f'{env_var}={shlex.quote(path_str)} kubectl get pods -A | head -n 20'},
                ]

            choices.append({
                "label": stem,
                "items": per_file_items
            })

        logger.info(f"Created {len(choices)} file choices for picker")
        return {
            "title": item.get("label", "Choose file"),
            "items": choices
        }

    # ------- navigation

    def go_back(self) -> None:
        logger.info(f"Going back in {self.tab_id}, current stack depth: {len(self.stack)}")
        if len(self.stack) > 1:
            popped = self.stack.pop()
            logger.debug(f"Popped from stack: {popped.get('title', 'Unknown')}")
            self._render_items()
        else:
            logger.debug("Already at root, cannot go back further")

    # ------- events

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        label = event.button.label or ""
        logger.info(f"Button pressed in {self.tab_id}: {bid} ({label})")

        if bid == f"{self.tab_id}-back":
            logger.debug("Back button pressed")
            self.go_back()
            return

        # Submenu push
        if bid.startswith(f"{self.tab_id}-submenu-"):
            logger.debug(f"Submenu button pressed: {bid}")
            try:
                idx = int(bid.rsplit("-", 1)[-1])
                items = self._current_items()
                logger.debug(f"Submenu index: {idx}, available items: {len(items)}")
                if 0 <= idx < len(items):
                    item = items[idx]
                    logger.info(f"Pushing submenu: {item.get('label')}")
                    self.stack.append(item)
                    self._render_items()
                else:
                    logger.error(f"Invalid submenu index: {idx}")
            except Exception as e:
                logger.error(f"Error handling submenu: {e}")
            return

        # Picker → expand to a submenu (file choices), then push
        if bid.startswith(f"{self.tab_id}-picker-"):
            logger.debug(f"Picker button pressed: {bid}")
            try:
                idx = int(bid.rsplit("-", 1)[-1])
                items = self._current_items()
                logger.debug(f"Picker index: {idx}, available items: {len(items)}")
                if 0 <= idx < len(items):
                    item = items[idx]
                    logger.info(f"Expanding picker: {item.get('label')}")
                    expanded = self._expand_picker_node(item)
                    self.stack.append(expanded)
                    self._render_items()
                else:
                    logger.error(f"Invalid picker index: {idx}")
            except Exception as e:
                logger.error(f"Error handling picker: {e}")
            return

        # Action
        cmd = (event.button.tooltip or "").strip()
        logger.info(f"Action button pressed: {label} -> {cmd}")
        if cmd:
            self.app.run_command(cmd)  # type: ignore[attr-defined]
        else:
            fallback_cmd = f'echo "No action bound for {label}"'
            logger.warning(f"No command found, using fallback: {fallback_cmd}")
            self.app.run_command(fallback_cmd)  # type: ignore[attr-defined]


# -------------------- app -------------------

class OpsDesk(App):
    CSS = """
    Screen { layout: vertical; }
    .body { height: 1fr; }
    #left { width: 44; border-right: solid $panel; }
    .tab-title { padding: 1 1; text-style: bold; border-bottom: solid $panel; }
    Button { margin: 1 1; }
    #out { border: solid $panel; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("b", "back", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        logger.info("Initializing OpsDesk app")
        self._tabs_data: List[Dict[str, Any]] = load_tabs()
        logger.info(f"Loaded {len(self._tabs_data)} tabs")
        self.out: Log | None = None

    def compose(self) -> ComposeResult:
        logger.info("Composing app layout")
        yield Header(show_clock=True)
        with Horizontal(classes="body"):
            with Vertical(id="left"):
                with TabbedContent():
                    if self._tabs_data:
                        for idx, data in enumerate(self._tabs_data):
                            title = data.get("title") or data.get("__file", f"Tab {idx+1}")
                            tab_id = f"pane-{idx}"
                            logger.debug(f"Creating tab {idx}: {title} (id: {tab_id})")
                            with TabPane(title=title, id=tab_id):
                                yield MenuPane(root_menu=data, tab_id=tab_id)
                    else:
                        logger.warning("No tabs data available")
                        with TabPane(title="No Tabs"):
                            yield Static("No settings/*.json found.", classes="tab-title")
            self.out = Log(id="out")
            yield self.out
        yield Footer()

    def on_mount(self) -> None:
        logger.info("App mounted")
        # Focus the first actionable button
        first_btn = self.query(Button).first()
        if first_btn:
            logger.debug(f"Focusing first button: {first_btn.id}")
            first_btn.focus()
        else:
            logger.warning("No buttons found to focus")
        self._log_line(f"Shell: {DEFAULT_SHELL}")
        self._log_line(f"Settings: {SETTINGS_DIR}")
        logger.info("App initialization complete")

    # Back action (keyboard 'b')
    def action_back(self) -> None:
        logger.info("Back action triggered via keyboard")
        for pane in self.query(MenuPane):
            if pane.visible:
                logger.debug(f"Found visible pane: {pane.tab_id}")
                pane.go_back()
                break

    # Run a command and stream output to the right panel
    def run_command(self, cmd: str) -> None:
        logger.info(f"Running command: {cmd}")
        quoted = f'{DEFAULT_SHELL} -lc {shlex.quote(cmd)}'
        logger.debug(f"Full shell command: {quoted}")
        self._log_line("")
        self._log_line(f"$ {cmd}")

        async def runner() -> None:
            logger.debug("Starting command execution")
            proc = await asyncio.create_subprocess_shell(
                quoted,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").rstrip("\n")
                self._log_line(text)
            rc = await proc.wait()
            status = "✔ done" if rc == 0 else f"✖ exit {rc}"
            self._log_line(f"{status}  {cmd}")
            logger.info(f"Command completed with exit code: {rc}")

        self.run_worker(runner(), exclusive=True, thread=False, description=f"run:{cmd}")

    # Log helper (works across Textual versions)
    def _log_line(self, text: str) -> None:
        if not self.out:
            logger.warning("No output log widget available")
            return
        if hasattr(self.out, "write_line"):
            self.out.write_line(text)   # Textual Log API
        elif hasattr(self.out, "write"):
            self.out.write(text)        # fallback
        else:
            try:
                self.out.update((self.out.renderable or "") + "\n" + text)  # type: ignore[attr-defined]
            except Exception:
                pass


if __name__ == "__main__":
    logger.info("Starting OpsDesk application")
    os.environ.setdefault("TERM", "xterm-256color")
    logger.info(f"Using settings directory: {SETTINGS_DIR}")
    OpsDesk().run()# app.py — minimal TUI with Back + dynamic file picker for clusters
