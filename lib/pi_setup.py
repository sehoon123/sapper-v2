import json
from pathlib import Path
import os
import tempfile

PACKAGE = str(Path(__file__).resolve().parents[1])


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".sapper-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_settings(project):
    path = project / ".pi/settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("packages", []), list):
        raise ValueError("Pi settings must be an object with a packages array")
    return settings


def configure(project, remove=False):
    settings = read_settings(project)
    path = project / ".pi/settings.json"
    owner = project / ".pi/sapper-integration.json"
    owned = json.loads(owner.read_text()) if owner.exists() else []
    if not isinstance(owned, list) or any(not isinstance(p, str) for p in owned):
        raise ValueError("invalid Sapper integration ownership file")
    packages = settings.get("packages", [])
    if remove:
        settings["packages"] = [p for p in packages if p not in owned]
        if not settings["packages"]:
            settings.pop("packages")
        if path.exists():
            atomic_json(path, settings)
        owner.unlink(missing_ok=True)
    elif PACKAGE not in packages:
        atomic_json(owner, list(dict.fromkeys([*owned, PACKAGE])))
        settings["packages"] = [*packages, PACKAGE]
        atomic_json(path, settings)
