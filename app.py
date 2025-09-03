
import asyncio
import json
import os
import shlex
import logging
import signal
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
import glob
import re

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import Button, Static, Footer, Header, TabbedContent, TabPane, Log, Input

# Small helpers
from dsl import dynamic_from_lines, run_and_capture
from config import load_config

# Set up logging
# Avoid printing to the terminal while the Textual TUI runs (causes glitches).
# Defaults: file-only, and ERROR-or-higher level (what you asked for).
# You can override with OPSDESK_LOG_LEVEL (debug|info|warning|error|critical)
# and enable console with OPSDESK_CONSOLE_LOG=1|true|yes|on|debug
handlers: list[logging.Handler] = [logging.FileHandler('opsdesk.log')]
_console_flag = os.environ.get('OPSDESK_CONSOLE_LOG', '').strip().lower()
if _console_flag in ('1', 'true', 'yes', 'on', 'debug'):
    sh = logging.StreamHandler()
    # Let root level control filtering unless explicitly set to debug here
    if _console_flag == 'debug':
        sh.setLevel(logging.DEBUG)
    handlers.append(sh)

_level_name = os.environ.get('OPSDESK_LOG_LEVEL', '').strip().lower()
_level_map = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL,
}
default_level = logging.ERROR  # errors only by default
level = _level_map.get(_level_name, default_level)

logging.basicConfig(
    level=level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers,
)
logger = logging.getLogger(__name__)


# ------------------ config ------------------

DEFAULT_SHELL = os.environ.get("SHELL", "/bin/sh")
CONFIG = load_config()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _read_json_file(p: Path) -> Dict[str, Any]:
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to read {p}: {e}")
    return {}


def _load_app_configs_json(app_dir: Path) -> Dict[str, Any]:
    # Support both configs.json and config.json
    data = {}
    for name in ("configs.json", "config.json"):
        p = app_dir / name
        d = _read_json_file(p)
        if d:
            data.update(d)
    return data


APP_DIR = Path(__file__).resolve().parent
CONFIGS_JSON_APP: Dict[str, Any] = _load_app_configs_json(APP_DIR)

# Resolve kubectl path (env → top-level config → any section → fallback)
def _resolve_kubectl(cfg: Dict[str, Any]) -> str:
    env_val = os.environ.get("KUBECTL")
    if env_val:
        return env_val.strip() or "kubectl"
    if isinstance(cfg, dict):
        top = cfg.get("kubectl_path") or cfg.get("kubectl")
        if isinstance(top, str) and top.strip():
            return top.strip()
        for v in cfg.values():
            if isinstance(v, dict):
                sec = v.get("kubectl_path") or v.get("kubectl")
                if isinstance(sec, str) and sec.strip():
                    return sec.strip()
    return "kubectl"

KUBECTL = _resolve_kubectl(CONFIGS_JSON_APP)


def _tabs_from_configs(configs: Dict[str, Any]) -> List[Dict[str, Any]]:
    tabs: List[Dict[str, Any]] = []
    for key, section in (configs or {}).items():
        if not isinstance(section, dict):
            continue
        title = str(key)
        tab = {
            "order": 100,
            "title": title,
            "items": [
                {
                    "label": "Kubeconfigs",
                    "picker": {},
                    "actions": [],
                }
            ],
            "__file": "configs.json",
        }
        tabs.append(tab)
    return tabs


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
    """Return a list of tabs from config.json/configs.json next to app.py.

    This replaces the old OPSDESK_SETTINGS_DIR design.
    """
    logger.info("Loading tabs from config near app.py")
    cfg_tabs = _tabs_from_configs(CONFIGS_JSON_APP)
    if cfg_tabs:
        logger.info(f"Loaded {len(cfg_tabs)} tabs from configs.json")
        return cfg_tabs
    # No config found
    logger.warning("No configs.json or config.json found next to app.py")
    return [{
        "order": 9999,
        "title": "No config found",
        "items": [{"label": "Info", "cmd": 'echo "Add config.json next to app.py"'}],
        "__file": "(missing)",
    }]


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
        with Horizontal(classes="header-actions"):
            yield Button("◀ Back", id=f"{self.tab_id}-back", classes="header-btn")
            yield Button("⛔ Kill", id=f"{self.tab_id}-kill", classes="header-btn")
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
            elif isinstance(item.get("list_cmd"), str):
                btn = Button(f"🔎 {label}", id=f"{self.tab_id}-dynamic-{idx}")
                logger.debug(f"Created dynamic button: {btn.id}")
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
        # If no glob declared, fall back to config value (kubeconfig_glob)
        if not glob_pat:
            cfg_glob = str(CONFIG.get("kubeconfig_glob", "")).strip()
            if cfg_glob:
                glob_pat = cfg_glob
        logger.debug(f"Picker glob pattern (initial): {glob_pat}")

        # Prefer per-tab entries from config near app.py (for files only)
        # Determine top-level tab name
        top_title = self.root.get("title") or ""
        slug = _slug(str(top_title))
        section = None
        if isinstance(CONFIGS_JSON_APP, dict):
            # Look for exact key or slug key
            section = CONFIGS_JSON_APP.get(top_title) or CONFIGS_JSON_APP.get(slug) or CONFIGS_JSON_APP.get(top_title.replace("_", " "))

        paths: List[Path]
        if isinstance(section, dict):
            file_list = section.get("kubeconfigs") or section.get("kubeconfig_files") or section.get("files")
            kube_dir = section.get("kubeconfig_dir") or section.get("dir")
            kube_glob = section.get("kubeconfig_glob") or section.get("glob")
            if isinstance(file_list, list) and file_list:
                expanded_files = [os.path.expandvars(os.path.expanduser(str(fp))) for fp in file_list]
                paths = [Path(p) for p in expanded_files if p]
                logger.info(f"Using kubeconfigs from app configs.json section '{top_title}': {len(paths)} files")
            else:
                if kube_dir and not kube_glob:
                    kube_glob = str(kube_dir).rstrip("/") + "/*.yaml"
                use_glob = str(kube_glob or glob_pat).strip()
                if not use_glob:
                    logger.warning("No kubeconfigs in configs.json section and no glob available")
                    return {"title": "No files", "items": [{"label": "Info", "cmd": 'echo "Provide kubeconfigs or kubeconfig_dir in configs.json"'}]}
                glob_expanded = os.path.expandvars(os.path.expanduser(use_glob))
                logger.debug(f"Expanded glob from section: {glob_expanded}")
                paths = [Path(p) for p in sorted(glob.glob(glob_expanded))]
        else:
            # No section; use explicit top-level list in app config or the glob
            file_list = None
            if isinstance(CONFIGS_JSON_APP, dict):
                file_list = CONFIGS_JSON_APP.get("kubeconfigs") or CONFIGS_JSON_APP.get("kubeconfig_files") or CONFIGS_JSON_APP.get("files")
            # kubectl path is always global (from top-level config)
            if isinstance(file_list, list) and file_list:
                expanded_files = [os.path.expandvars(os.path.expanduser(str(fp))) for fp in file_list]
                paths = [Path(p) for p in expanded_files if p]
            else:
                if not glob_pat:
                    logger.warning("No glob pattern and no kubeconfigs list available")
                    return {"title": "No files", "items": [{"label": "Info", "cmd": 'echo "Provide config.json section with kubeconfig_dir or kubeconfigs"'}]}
                glob_expanded = os.path.expandvars(os.path.expanduser(glob_pat))
                logger.debug(f"Expanded glob pattern: {glob_expanded}")
                paths = [Path(p) for p in sorted(glob.glob(glob_expanded))]

        logger.info(f"Found {len(paths)} kubeconfig files: {[str(p) for p in paths]}")

        if not paths:
            logger.warning("No kubeconfig files found for this tab")
            return {
                "title": f"No kubeconfigs found",
                "items": [{"label": "Info", "cmd": 'echo "No kubeconfig files found"'}],
            }

        env_var = str(picker.get("env_var", "KUBECONFIG"))
        # Use global kubectl path from top-level config (no per-tab override)
        kubectl_q = shlex.quote(KUBECTL)
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
                    PATH=path_str, NAME=name, STEM=stem, KUBECONFIG=path_str, KUBECTL=KUBECTL
                ).strip()

                # Normalize kubectl invocations to include --kubeconfig right after kubectl
                cmd = raw_cmd
                try:
                    if raw_cmd:
                        stripped = raw_cmd.lstrip()
                        if stripped.startswith("kubectl "):
                            rest = stripped[len("kubectl "):]
                            cmd = f"{kubectl_q} --kubeconfig={shlex.quote(path_str)} {rest}"
                        elif stripped.startswith(f"{KUBECTL} ") or stripped.startswith(f"{kubectl_q} "):
                            if "--kubeconfig" not in stripped:
                                token = kubectl_q
                                rest = stripped[len(token)+1:] if stripped.startswith(token+" ") else stripped[len(KUBECTL)+1:]
                                cmd = f"{kubectl_q} --kubeconfig={shlex.quote(path_str)} {rest}"
                except Exception:
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
                    {"label": "Pods (all)", "cmd": f'{kubectl_q} --kubeconfig={shlex.quote(path_str)} get pods'},
                ]

            # Always add a dynamic Pods chooser unless one already exists
            try:
                has_dynamic = any(isinstance(it, dict) and it.get("list_cmd") for it in per_file_items)
            except Exception:
                has_dynamic = False
            if not has_dynamic:
                # List only pod names; avoid namespaces entirely
                columns = 'NAME:.metadata.name'
                pods_list_cmd = f"{kubectl_q} --kubeconfig={shlex.quote(path_str)} get pods --no-headers -o custom-columns={columns}"
                per_file_items.append({
                    "label": "Pods (choose)",
                    "list_cmd": pods_list_cmd,
                    "entry_label": "{T0}",
                    "actions": [
                        {"label": "Describe", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(path_str)} describe pod {{T0}}"},
                        {"label": "Logs (-f)", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(path_str)} logs -f {{T0}}"},
                        {"label": "Logs (tail 100)", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(path_str)} logs --tail=100 {{T0}}"},
                        {"label": "Exec bash", "cmd": f"interactive: {kubectl_q} --kubeconfig={shlex.quote(path_str)} exec -it {{T0}} -- bash"},
                        {"label": "Exec sh",   "cmd": f"interactive: {kubectl_q} --kubeconfig={shlex.quote(path_str)} exec -it {{T0}} -- /bin/sh"},
                    ],
                })

            choices.append({
                "label": stem,
                "items": per_file_items
            })

        logger.info(f"Created {len(choices)} file choices for picker")
        return {
            "title": item.get("label", "Choose file"),
            "items": choices
        }

    # dynamic menu building moved to dsl.dynamic_from_lines

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

        if bid == f"{self.tab_id}-kill":
            logger.debug("Kill button pressed")
            try:
                self.app.kill_running_command()  # type: ignore[attr-defined]
            except Exception as e:
                logger.error(f"Error sending kill signal: {e}")
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

        # Dynamic list → run command, convert output to submenu, then push
        if bid.startswith(f"{self.tab_id}-dynamic-"):
            logger.debug(f"Dynamic button pressed: {bid}")
            try:
                idx = int(bid.rsplit("-", 1)[-1])
                items = self._current_items()
                logger.debug(f"Dynamic index: {idx}, available items: {len(items)}")
                if 0 <= idx < len(items):
                    item = items[idx]
                    cmd = str(item.get("list_cmd", "")).strip()
                    if not cmd:
                        logger.error("Dynamic item missing 'list_cmd'")
                        self.app._log_line("Dynamic item missing 'list_cmd'")  # type: ignore[attr-defined]
                        return

                    self.app._log_line(f"$ {cmd}")  # type: ignore[attr-defined]

                    async def worker() -> None:
                        lines = await run_and_capture(cmd)
                        expanded = dynamic_from_lines(item, lines)
                        self.stack.append(expanded)
                        self._render_items()

                    self.app.run_worker(worker(), exclusive=True, thread=False, description=f"dyn:{cmd}")  # type: ignore[attr-defined]
                else:
                    logger.error(f"Invalid dynamic index: {idx}")
            except Exception as e:
                logger.error(f"Error handling dynamic: {e}")
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
    #left { width: 60; min-width: 36; max-width: 96; border-right: solid $panel; }
    .tab-title { padding: 0 1; text-style: bold; border-bottom: solid $panel; }
    Button { margin: 0 1; }
    #left Button { margin: 0 1; padding: 0 1; content-align: left middle; width: 100%; min-height: 1; }
    .header-actions Button { width: auto; min-width: 10; padding: 0 1; }
    .header-actions { padding: 0 1; }
    #out { border: solid $panel; height: 1fr; min-height: 8; }
    #cmd_row { layout: horizontal; padding: 0 1; }
    #cmd_input { border: solid $panel; width: 1fr; }
    #history_title { padding: 0 1; text-style: italic; color: $text-muted; }
    #history { border: solid $panel; height: 16; min-height: 8; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("b", "back", "Back"),
        ("/", "focus_cmd", "Command"),
        ("ctrl+p", "history_prev", "Prev Cmd"),
        ("ctrl+n", "history_next", "Next Cmd"),
        ("ctrl+v", "paste_cmd", "Paste"),
    ]

    def __init__(self) -> None:
        super().__init__()
        logger.info("Initializing OpsDesk app")
        self._tabs_data: List[Dict[str, Any]] = load_tabs()
        logger.info(f"Loaded {len(self._tabs_data)} tabs")
        self.out: Log | None = None
        self.cmd_input: Input | None = None
        self.history: ScrollableContainer | None = None
        self._history: List[str] = []
        self._hist_idx: Optional[int] = None  # None means not navigating
        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._was_killed: bool = False

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
            with Vertical(id="right"):
                with Horizontal(id="cmd_row"):
                    self.cmd_input = Input(placeholder="Type a shell command and press Enter…", id="cmd_input")
                    yield self.cmd_input
                    yield Button("📋 Copy", id="copy-input")
                    yield Button("📥 Paste", id="paste-input")
                yield Static("History (click to re-run)", id="history_title")
                self.history = ScrollableContainer(id="history")
                yield self.history
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
        cfg_src = "config.json / configs.json next to app.py"
        self._log_line(f"Config source: {cfg_src}")
        try:
            self._log_line(f"kubectl: {KUBECTL}")
        except Exception:
            pass
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
        # Handle interactive-prefixed commands by suspending the TUI
        INTERACTIVE_PREFIX = "interactive:"
        if cmd.strip().startswith(INTERACTIVE_PREFIX):
            real = cmd.strip()[len(INTERACTIVE_PREFIX):].strip()
            self.run_interactive(real)
            return
        # Track in history for re-run
        self._history_push(cmd)
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
                start_new_session=True,
            )
            self._current_proc = proc
            self._was_killed = False
            assert proc.stdout is not None
            batch: list[str] = []
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore").rstrip("\n")
                batch.append(text)
                if len(batch) >= 50:
                    self._log_lines(batch)
                    batch.clear()
            if batch:
                self._log_lines(batch)
            rc = await proc.wait()
            status = "⏹ killed" if self._was_killed else ("✔ done" if rc == 0 else f"✖ exit {rc}")
            self._log_line(f"{status}  {cmd}")
            logger.info(f"Command completed with exit code: {rc}")
            self._current_proc = None

        self.run_worker(runner(), exclusive=True, thread=False, description=f"run:{cmd}")

    def run_interactive(self, cmd: str) -> None:
        """Suspend TUI and run an interactive command in the user's terminal.

        Use for commands like `kubectl exec -it ... -- bash` that require a TTY.
        """
        self._log_line("")
        self._log_line(f"$ {cmd}")
        try:
            with self.suspend():  # type: ignore[attr-defined]
                rc = subprocess.call(cmd, shell=True, executable=DEFAULT_SHELL)
        except Exception as e:
            logger.error(f"Interactive run failed: {e}")
            self._log_line(f"✖ interactive error: {e}")
            return
        self._log_line(f"✔ returned {rc}  {cmd}")

    # line capture moved to dsl.run_and_capture

    # ------- input + history --------

    def action_focus_cmd(self) -> None:
        if self.cmd_input:
            logger.debug("Focusing command input")
            self.cmd_input.focus()

    def _history_push(self, cmd: str) -> None:
        if not cmd:
            return
        if self._history and self._history[-1] == cmd:
            return
        self._history.append(cmd)
        # Reset navigation when a new command is added
        self._hist_idx = None
        self._history_refresh()

    def _history_refresh(self) -> None:
        if not self.history:
            return
        # Clear existing
        for child in list(self.history.children):
            child.remove()
        # Show newest first as rows with explicit intent in tooltip
        for cmd in reversed(self._history):
            label = cmd if len(cmd) <= 80 else cmd[:77] + "..."
            row = Horizontal()
            self.history.mount(row)
            row.mount(Button(f"↻ {label}", tooltip=f"run:{cmd}"))
            row.mount(Button("📋", tooltip=f"copy:{cmd}"))

    def action_history_prev(self) -> None:
        if not self.cmd_input or not self._history:
            return
        if self._hist_idx is None:
            self._hist_idx = len(self._history) - 1
        else:
            self._hist_idx = max(0, self._hist_idx - 1)
        self.cmd_input.value = self._history[self._hist_idx]
        self.cmd_input.focus()

    def action_history_next(self) -> None:
        if not self.cmd_input or not self._history:
            return
        if self._hist_idx is None:
            return  # nothing to do
        if self._hist_idx < len(self._history) - 1:
            self._hist_idx += 1
            self.cmd_input.value = self._history[self._hist_idx]
        else:
            self._hist_idx = None
            self.cmd_input.value = ""
        self.cmd_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        logger.info(f"Input submitted: {value}")
        if not value:
            return
        if self.cmd_input:
            self.cmd_input.value = ""
        self.run_command(value)

    # ------- kill handling --------

    def kill_running_command(self) -> None:
        if not self._current_proc:
            self._log_line("No running command to kill")
            return
        try:
            pid = self._current_proc.pid
            # Send SIGINT to the process group started by start_new_session=True
            os.killpg(pid, signal.SIGINT)
            self._was_killed = True
            self._log_line("[sent SIGINT]")
            logger.info(f"Sent SIGINT to process group {pid}")
        except ProcessLookupError:
            self._log_line("[process already finished]")
        except Exception as e:
            logger.error(f"Failed to send SIGINT: {e}")
            try:
                self._current_proc.send_signal(signal.SIGINT)
                self._was_killed = True
                self._log_line("[sent SIGINT]")
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Copy current input
        if event.button.id == "copy-input":
            if self.cmd_input:
                self._copy_to_clipboard(self.cmd_input.value)
            return

        # History row buttons
        parent = event.button.parent
        parent_id = getattr(parent, "id", None)
        grand_id = getattr(getattr(parent, "parent", None), "id", None)
        if parent_id == "history" or grand_id == "history":
            tip = str(event.button.tooltip or "")
            if tip.startswith("copy:"):
                cmd = tip[len("copy:"):]
                logger.info(f"History copy clicked: {cmd}")
                self._copy_to_clipboard(cmd)
            else:
                cmd = tip[len("run:"):] if tip.startswith("run:") else tip
                logger.info(f"History re-run clicked: {cmd}")
                if self.cmd_input:
                    self.cmd_input.value = cmd
                self.run_command(cmd)
            return

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

    def _log_lines(self, lines: list[str]) -> None:
        if not self.out:
            return
        try:
            if hasattr(self.out, "write_lines"):
                self.out.write_lines(lines)  # type: ignore[attr-defined]
                return
        except Exception:
            pass
        for ln in lines:
            self._log_line(ln)

    def _copy_to_clipboard(self, text: str) -> None:
        try:
            import shutil, subprocess, sys
            text = text or ""
            # Prefer pbcopy on macOS
            if sys.platform == "darwin" and shutil.which("pbcopy"):
                p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-8"))
                self._log_line("[copied to clipboard]")
                return
            # Try xclip / wl-copy on Linux
            if shutil.which("xclip"):
                p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-8"))
                self._log_line("[copied to clipboard]")
                return
            if shutil.which("wl-copy"):
                p = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
                p.communicate(input=text.encode("utf-8"))
                self._log_line("[copied to clipboard]")
                return
            # OSC52 fallback
            b64 = text.encode("utf-8").hex()
            # Hex is not valid for OSC52; but constructing full OSC52 safely is complex in TUI; fallback to message
            self._log_line("[copy unsupported: install pbcopy/xclip/wl-copy]")
        except Exception:
            self._log_line("[copy failed]")

    def _paste_from_clipboard(self) -> str | None:
        try:
            import shutil, subprocess, sys
            # macOS
            if sys.platform == "darwin" and shutil.which("pbpaste"):
                out = subprocess.check_output(["pbpaste"])  # type: ignore[arg-type]
                text = out.decode("utf-8", errors="ignore")
            elif shutil.which("xclip"):
                out = subprocess.check_output(["xclip", "-selection", "clipboard", "-o"])  # type: ignore[list-item]
                text = out.decode("utf-8", errors="ignore")
            elif shutil.which("wl-paste"):
                out = subprocess.check_output(["wl-paste"])  # type: ignore[arg-type]
                text = out.decode("utf-8", errors="ignore")
            else:
                self._log_line("[paste unsupported: install pbpaste/xclip/wl-paste]")
                return None
            # Use first line for the command input
            first_line = (text or "").splitlines()[0] if text else ""
            return first_line
        except Exception:
            self._log_line("[paste failed]")
            return None

    # Ensure cleanup when quitting: interrupt any running child process
    def action_quit(self) -> None:
        try:
            if self._current_proc and self._current_proc.pid:
                try:
                    os.killpg(self._current_proc.pid, signal.SIGINT)
                except Exception:
                    try:
                        self._current_proc.send_signal(signal.SIGINT)
                    except Exception:
                        pass
                self._was_killed = True
                self._log_line("[cleaned up child process]")
        except Exception:
            pass
        self.exit()


if __name__ == "__main__":
    logger.info("Starting OpsDesk application")
    os.environ.setdefault("TERM", "xterm-256color")
    logger.info("Using config near app.py (config.json / configs.json)")
    OpsDesk().run()# app.py — minimal TUI with Back + dynamic file picker for clusters
