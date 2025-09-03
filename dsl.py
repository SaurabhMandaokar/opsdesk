import asyncio
import os
import shlex
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def dynamic_from_lines(item: Dict[str, Any], lines: List[str]) -> Dict[str, Any]:
    """Build a submenu spec from command output lines.

    Templating variables available in actions:
      - {LINE}: full line
      - {NAME}: first token (or full line)
      - {T0}..{T9}: tokens by index
    Optional item["env_var"] may prefix commands with ENV=LINE when not present.
    """
    logger.info(f"Building dynamic node for: {item.get('label')}")
    actions = item.get("actions", [])
    env_var = str(item.get("env_var", ""))
    logger.debug(f"Actions: {len(actions)}, env_var: {env_var!r}")

    entries: List[Dict[str, Any]] = []
    for line in [l.strip() for l in lines if l.strip()]:
        tokens = line.split()
        vars_map: Dict[str, str] = {"LINE": line, "NAME": (tokens[0] if tokens else line)}
        for i in range(10):
            vars_map[f"T{i}"] = tokens[i] if i < len(tokens) else ""

        per_items: List[Dict[str, Any]] = []
        for act in actions:
            if not isinstance(act, dict) or "label" not in act:
                continue
            label = str(act.get("label", "Action"))
            raw_tmpl = str(act.get("cmd", ""))
            try:
                raw_cmd = raw_tmpl.format(**vars_map).strip()
            except Exception as e:
                logger.error(f"Template error in dynamic action '{label}': {e}")
                raw_cmd = f'echo "Template error: {e}"'

            cmd = raw_cmd
            if env_var and raw_cmd and (f"{env_var}=" not in raw_cmd):
                # Prefix env only if not already present
                cmd = f'{env_var}={shlex.quote(vars_map.get("LINE", ""))} {raw_cmd}'

            per_items.append({"label": label, "cmd": cmd})

        if not per_items:
            per_items = [{"label": "Echo", "cmd": f'echo {shlex.quote(line)}'}]

        # Allow caller to override entry label via template, e.g. "{T1}" or "{T1} ({T0})"
        label_tmpl = str(item.get("entry_label", "{NAME}"))
        try:
            entry_label = label_tmpl.format(**vars_map)
        except Exception:
            entry_label = vars_map["NAME"]

        entries.append({"label": entry_label, "items": per_items})

    title = item.get("label", "Choose item")
    logger.info(f"Dynamic node built with {len(entries)} entries")
    return {"title": title, "items": entries}


async def run_and_capture(cmd: str, max_lines: int = 500) -> List[str]:
    """Run a shell command and return its stdout lines (decoded)."""
    shell = os.environ.get("SHELL", "/bin/sh")
    quoted = f'{shell} -lc {shlex.quote(cmd)}'
    logger.debug(f"Capture shell command: {quoted}")
    proc = await asyncio.create_subprocess_shell(
        quoted,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    lines: List[str] = []
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="ignore").rstrip("\n")
        lines.append(text)
        if len(lines) >= max_lines:
            logger.warning(f"Line capture limit reached: {max_lines}")
            break
    rc = await proc.wait()
    logger.info(f"Capture complete with exit code: {rc}, lines: {len(lines)}")
    return lines
