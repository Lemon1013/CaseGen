from pathlib import Path


def _dependency_line(path: Path, package: str) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.lower().startswith(package.lower() + "[") or line.lower().startswith(package.lower() + ">"):
            return line
    raise AssertionError(f"{package} requirement missing from {path}")


def test_online_and_offline_requirements_keep_httpx_socks_extra():
    root = Path(__file__).resolve().parents[2]
    online = root / "backend" / "requirements.txt"
    offline = root / "deploy" / "win10" / "requirements-offline.txt"
    assert _dependency_line(online, "httpx") == "httpx[socks]>=0.27.0"
    assert _dependency_line(offline, "httpx") == "httpx[socks]>=0.27.0"
