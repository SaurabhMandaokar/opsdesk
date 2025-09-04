from typing import List, Dict, Any
import shlex


def _normalize_kubectl_cmd(raw_cmd: str, kubeconfig: str, kubectl_q: str, kubectl_path: str) -> str:
    """Ensure kubectl commands use the resolved binary and --kubeconfig flag.

    Accepts commands starting with 'kubectl' or the absolute kubectl path; if already
    contains '--kubeconfig', it is preserved.
    """
    cmd = raw_cmd.strip()
    if not cmd:
        return cmd

    stripped = cmd.lstrip()
    if stripped.startswith("kubectl "):
        rest = stripped[len("kubectl "):]
        return f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig)} {rest}"
    token = kubectl_q
    if stripped.startswith(f"{token} "):
        if "--kubeconfig" not in stripped:
            rest = stripped[len(token) + 1 :]
            return f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig)} {rest}"
        return stripped
    # Not a kubectl command; return as-is
    return cmd


def build_items_for_kubeconfig(
    kubeconfig_path: str,
    kubectl_q: str,
    actions: List[Dict[str, Any]] | None,
    pod_entry_label: str = "{T0}",
) -> List[Dict[str, Any]]:
    """Return menu items for a single kubeconfig.

    - Normalizes any provided custom actions to include --kubeconfig
    - Adds sensible defaults if no actions provided
    - Adds a Pods (choose) dynamic list with Exec and Logs
    """
    actions = actions or []
    items: List[Dict[str, Any]] = []

    # Normalize provided actions (if any)
    for act in actions:
        if not isinstance(act, dict) or "label" not in act:
            continue
        label = str(act.get("label", "Action"))
        raw_cmd = str(act.get("cmd", "")).format(PATH=kubeconfig_path, KUBECONFIG=kubeconfig_path, KUBECTL=kubectl_q)
        cmd = _normalize_kubectl_cmd(raw_cmd, kubeconfig_path, kubectl_q, kubectl_q)
        if not cmd:
            continue
        items.append({"label": label, "cmd": cmd})

    if not items:
        items.append({"label": "Pods (all)", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} get pods"})

    # Always add the dynamic pods chooser unless already present
    has_dynamic = any(isinstance(it, dict) and it.get("list_cmd") for it in items)
    if not has_dynamic:
        columns = 'NAME:.metadata.name'
        pods_list_cmd = f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} get pods --no-headers -o custom-columns={columns}"
        items.append(
            {
                "label": "Pods (choose)",
                "list_cmd": pods_list_cmd,
                "entry_label": pod_entry_label,
                "actions": [
                    {"label": "Describe", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} describe pod {{T0}}"},
                    {"label": "Logs (-f)", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} logs -f {{T0}}"},
                    {"label": "Logs (tail 100)", "cmd": f"{kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} logs --tail=100 {{T0}}"},
                    {"label": "Generate delete (to input)", "cmd": f"template: {kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} delete pod {{T0}}"},
                    {"label": "Port-forward 4040 (Spark UI)", "cmd": f"( (sleep 1; (command -v open >/dev/null && open http://localhost:4040 || (command -v xdg-open >/dev/null && xdg-open http://localhost:4040) || python3 -m webbrowser http://localhost:4040) >/dev/null 2>&1) & ); {kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} port-forward {{T0}} 4040:4040"},
                    {"label": "Stop port-forward 4040", "cmd": "pids=$(pgrep -f 'kubectl.*port-forward.* {{T0}} .*4040:4040' || true); if [ -n \"$pids\" ]; then kill $pids; fi; pids2=$(lsof -ti tcp:4040 2>/dev/null || true); if [ -n \"$pids2\" ]; then kill $pids2; fi; echo 'Stopped port-forward on 4040'"},
                    {"label": "Run in pod…", "cmd": f"template: {kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} exec -i {{T0}} -- /bin/sh -lc 'YOUR_CMD_HERE'"},
                    {"label": "Exec bash", "cmd": f"interactive: {kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} exec -it {{T0}} -- bash"},
                    {"label": "Exec sh",   "cmd": f"interactive: {kubectl_q} --kubeconfig={shlex.quote(kubeconfig_path)} exec -it {{T0}} -- /bin/sh"},
                ],
            }
        )

    return items
