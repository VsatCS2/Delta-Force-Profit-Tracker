"""
bundled_assets.py - works out which data files the app loads at run time, so the
build can bundle them. Used by DFTracker.spec (to bundle) and build_release.py (to
check before and after building).

Why it exists: a frozen app only has the files the build put inside it. A file the code
loads through resource_path("delta_force_items.json") works when you run the .py (it's
sitting in the folder) but is simply absent from the installed app unless the build
bundles it. This reads the code, finds those names, and bundles them.

  * resource_path("x")        -> REQUIRED: the build stops if x is missing
  * read_bundled_text("x")    -> optional: a warning if x is missing (it only affects one screen)
  * LICENSE, THIRD_PARTY_NOTICES.txt are always bundled when present
Files whose names look like saved credentials or user data are never bundled.
"""
import ast
import re
from pathlib import Path

KIT_VERSION = 8      # must match build_release.py, DFTracker.spec and DFTracker.iss
LOADERS = {"resource_path": True, "read_bundled_text": False}      # function name -> required?
ALWAYS = ("LICENSE", "THIRD_PARTY_NOTICES.txt")
DENY = re.compile(r"(session|creds|credential|token|secret|cookie|password|passwd|cache|account|history|"
                  r"settings|\.env$|\.pem$|\.key$|\.pfx$|\.p12$)", re.I)
SKIP_SCRIPTS = {"build_release.py", "tidy_folder.py", "apply_cleanup.py", "bundled_assets.py"}


class Result:
    def __init__(self):
        self.datas = []                 # [(source path str, destination folder str)]
        self.required = []              # names the code needs
        self.optional = []
        self.missing_required = []
        self.missing_optional = []
        self.refused = []               # names we will not bundle (look like secrets / unsafe)

    @property
    def ok(self):
        return not self.missing_required and not self.refused


def _call_name(node):
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)


def find_references(app_dir):
    """-> {name: required_bool} for every literal passed to resource_path / read_bundled_text."""
    found = {}
    for p in sorted(Path(app_dir).glob("*.py")):
        if p.name in SKIP_SCRIPTS:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                required = LOADERS.get(_call_name(node))
                if required is None:
                    continue
                name = node.args[0].value
                found[name] = found.get(name, False) or required
    return found


def _safe(name):
    parts = Path(name).parts
    return bool(parts) and not Path(name).is_absolute() and ".." not in parts


def resolve_assets(app_dir):
    app_dir = Path(app_dir)
    res = Result()
    refs = find_references(app_dir)
    for name in ALWAYS:
        refs.setdefault(name, False)
    for name, required in sorted(refs.items()):
        (res.required if required else res.optional).append(name)
        if not _safe(name) or DENY.search(name):
            res.refused.append(name)
            continue
        src = app_dir / name
        if not src.is_file():
            (res.missing_required if required else res.missing_optional).append(name)
            continue
        dest = str(Path(name).parent).replace("\\", "/")
        res.datas.append((str(src), "." if dest in ("", ".") else dest))
    return res


def verify_in_bundle(dist_dir, res):
    """After building: is each bundled file really inside the built app folder? -> list of missing names."""
    dist = Path(dist_dir)
    have = {str(p.relative_to(dist)).replace("\\", "/") for p in dist.rglob("*") if p.is_file()}
    missing = []
    for src, dest in res.datas:
        name = Path(src).name
        rel = name if dest == "." else f"{dest}/{name}"
        if not any(h == rel or h.endswith("/" + rel) for h in have):
            missing.append(rel)
    return missing


def describe_problems(res):
    lines = []
    for n in res.missing_required:
        lines.append(f"{n} is loaded by the app (resource_path) but isn't in the app folder. "
                     "An app built now would be missing it. Put the file next to delta_force_gui.py.")
    for n in res.refused:
        lines.append(f"{n} is referenced by the code but looks like user data or a credential, so it will NOT be bundled. "
                     "Rename it, or tell me if it really is a shipped file.")
    return lines
