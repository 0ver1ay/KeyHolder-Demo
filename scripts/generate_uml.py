from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
VIEWS_SCREENS_DIR = ROOT / "views" / "screens"
ROOT_KV = ROOT / "views" / "root.kv"
CONTROLLER_PY = ROOT / "controllers" / "app_controller.py"
OUTPUT_DIR = ROOT / "docs" / "uml"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def parse_root_kv_for_screens(root_kv_text: str) -> Dict[str, str]:
    """
    Returns mapping of class name -> screen name.
    Based on structure like:
        <RootManager@ScreenManager>:
            MainScreen:
                name: "main"
            AuthScreen:
                name: "auth"
    """
    mapping: Dict[str, str] = {}
    current_class: str | None = None
    for raw in root_kv_text.splitlines():
        line = raw.rstrip()
        # Match a class instantiation line, e.g. "    MainScreen:"
        m_class = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$", line)
        if m_class:
            current_class = m_class.group(1)
            continue
        # Match name: "..."
        m_name = re.search(r"name\s*:\s*\"([^\"]+)\"|name\s*:\s*'([^']+)'", line)
        if m_name and current_class:
            screen_name = m_name.group(1) or m_name.group(2)
            mapping[current_class] = screen_name
    return mapping


def parse_kv_class_names(kv_text: str) -> List[str]:
    # Looks for <ClassName>:
    return re.findall(r"^\s*<([A-Za-z_][A-Za-z0-9_]*)>\s*:\s*$", kv_text, flags=re.MULTILINE)


def find_kv_actions_and_transitions(kv_text: str) -> Tuple[Set[str], List[str]]:
    """
    Returns (controller_actions, direct_targets) detected in KV file.
    - controller_actions: names used like app.controller.ACTION(...)
    - direct_targets: screen names used in statements like app.root.current = 'name'
    """
    actions = set(re.findall(r"app\.controller\.([A-Za-z_][A-Za-z0-9_]*)\(", kv_text))
    direct_targets = re.findall(r"app\.root\.current\s*=\s*['\"]([^'\"]+)['\"]", kv_text)
    return actions, direct_targets


def parse_controller_targets(controller_text: str) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """
    Parse controller methods for:
    - method_targets: method -> set of screen names it sets via app.root.current = "..."
    - method_calls: method -> set of other controller methods it calls
    """
    method_targets: Dict[str, Set[str]] = {}
    method_calls: Dict[str, Set[str]] = {}

    current_method: str | None = None
    for raw in controller_text.splitlines():
        line = raw.rstrip()
        m_def = re.match(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        if m_def:
            current_method = m_def.group(1)
            method_targets.setdefault(current_method, set())
            method_calls.setdefault(current_method, set())
            continue

        if not current_method:
            continue

        # Collect targets
        m_target = re.search(r"app\.root\.current\s*=\s*['\"]([^'\"]+)['\"]", line)
        if m_target:
            method_targets[current_method].add(m_target.group(1))

        # Collect calls to other methods (simple heuristic)
        m_call = re.findall(r"self\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
        for callee in m_call:
            if callee != current_method:  # avoid self-recursion reporting
                method_calls[current_method].add(callee)

    return method_targets, method_calls


def propagate_targets(method_targets: Dict[str, Set[str]], method_calls: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Propagate one level of calls so that methods inherit targets from directly-called methods."""
    resolved: Dict[str, Set[str]] = {k: set(v) for k, v in method_targets.items()}
    for caller, callees in method_calls.items():
        for cal in callees:
            if cal in method_targets:
                resolved[caller].update(method_targets[cal])
    return resolved


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def generate_global_puml(
    screen_name_by_class: Dict[str, str],
    kv_file_to_classnames: Dict[str, List[str]],
    kv_actions_by_file: Dict[str, Set[str]],
    kv_direct_targets_by_file: Dict[str, List[str]],
    method_targets_resolved: Dict[str, Set[str]],
) -> str:
    # Map kv file to screen name using class name first, then fallback to filename
    file_to_screen: Dict[str, str] = {}
    for fp, class_names in kv_file_to_classnames.items():
        screen_name = None
        for cls in class_names:
            if cls in screen_name_by_class:
                screen_name = screen_name_by_class[cls]
                break
        if not screen_name:
            # fallback to filename stem
            screen_name = Path(fp).stem
        file_to_screen[fp] = screen_name

    # Collect all screens
    all_screens: Set[str] = set(screen_name_by_class.values()) | set(file_to_screen.values())

    lines: List[str] = []
    lines.append("@startuml")
    lines.append("skinparam shadowing false")
    lines.append("hide empty description")

    for s in sorted(all_screens):
        lines.append(f"state \"{s}\" as {s}")

    # Transitions from kv actions via controller methods
    for fp, actions in kv_actions_by_file.items():
        src = file_to_screen.get(fp)
        if not src:
            continue
        for action in sorted(actions):
            targets = method_targets_resolved.get(action, set())
            if targets:
                for tgt in sorted(targets):
                    lines.append(f"{src} --> {tgt} : {action}")
            else:
                # Internal action (no screen change)
                lines.append(f"{src} --> {src} : {action}")

    # Direct kv transitions
    for fp, targets in kv_direct_targets_by_file.items():
        src = file_to_screen.get(fp)
        if not src:
            continue
        for tgt in sorted(set(targets)):
            lines.append(f"{src} --> {tgt} : kv")

    lines.append("@enduml")
    return "\n".join(lines)


def generate_per_screen_puml(
    screen: str,
    outgoing: List[Tuple[str, str]],  # (target, label)
) -> str:
    lines = ["@startuml", "skinparam shadowing false", "hide empty description"]
    # Declare states
    lines.append(f"state \"{screen}\" as {screen}")
    # Declare unique target states
    uniq_targets = sorted({t for t, _ in outgoing if t != screen})
    for t in uniq_targets:
        lines.append(f"state \"{t}\" as {t}")
    # Transitions
    for tgt, label in outgoing:
        lines.append(f"{screen} --> {tgt} : {label}")
    lines.append("@enduml")
    return "\n".join(lines)


def main() -> int:
    ensure_output_dir()

    root_kv_text = read_text(ROOT_KV)
    controller_text = read_text(CONTROLLER_PY)

    screen_name_by_class = parse_root_kv_for_screens(root_kv_text)
    method_targets, method_calls = parse_controller_targets(controller_text)
    method_targets_resolved = propagate_targets(method_targets, method_calls)

    kv_file_to_classnames: Dict[str, List[str]] = {}
    kv_actions_by_file: Dict[str, Set[str]] = {}
    kv_direct_targets_by_file: Dict[str, List[str]] = {}

    for kv_path in sorted(VIEWS_SCREENS_DIR.glob("*.kv")):
        text = read_text(kv_path)
        kv_file_to_classnames[str(kv_path.relative_to(ROOT))] = parse_kv_class_names(text)
        actions, dtargets = find_kv_actions_and_transitions(text)
        kv_actions_by_file[str(kv_path.relative_to(ROOT))] = actions
        kv_direct_targets_by_file[str(kv_path.relative_to(ROOT))] = dtargets

    # Global diagram
    global_puml = generate_global_puml(
        screen_name_by_class,
        kv_file_to_classnames,
        kv_actions_by_file,
        kv_direct_targets_by_file,
        method_targets_resolved,
    )
    (OUTPUT_DIR / "screens_state.puml").write_text(global_puml, encoding="utf-8")

    # Per-screen diagrams
    # Build transitions per source screen
    # Reuse mapping like in global generation
    file_to_screen: Dict[str, str] = {}
    for fp, class_names in kv_file_to_classnames.items():
        screen_name = None
        for cls in class_names:
            if cls in screen_name_by_class:
                screen_name = screen_name_by_class[cls]
                break
        if not screen_name:
            screen_name = Path(fp).stem
        file_to_screen[fp] = screen_name

    per_screen_edges: Dict[str, List[Tuple[str, str]]] = {}

    for fp, actions in kv_actions_by_file.items():
        src = file_to_screen.get(fp)
        if not src:
            continue
        for action in sorted(actions):
            targets = method_targets_resolved.get(action, set())
            if targets:
                for tgt in sorted(targets):
                    per_screen_edges.setdefault(src, []).append((tgt, action))
            else:
                per_screen_edges.setdefault(src, []).append((src, action))

    for fp, targets in kv_direct_targets_by_file.items():
        src = file_to_screen.get(fp)
        if not src:
            continue
        for tgt in sorted(set(targets)):
            per_screen_edges.setdefault(src, []).append((tgt, "kv"))

    for screen, edges in per_screen_edges.items():
        puml = generate_per_screen_puml(screen, edges)
        (OUTPUT_DIR / f"screen_{screen}.puml").write_text(puml, encoding="utf-8")

    print(f"Generated UML in: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


