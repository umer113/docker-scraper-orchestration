import os
import re
import ast
import sys
import json
import time
import shutil
import tempfile
import subprocess
import urllib.request
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI()

# Allow the Vite dev frontend (and anything else) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WORKDIR = "/appy"

# Label stamped on every container we create, so we can list/manage only ours.
LABEL = "scraper-orchestrator"
# Base host ports. Container N gets noVNC = NOVNC_BASE + N and VNC = VNC_BASE + N.
# We encode the chosen noVNC port into the container name (scraper-<port>) so the
# whole registry can be reconstructed from `docker ps` alone — no in-memory state
# to lose across reloads.
NOVNC_BASE = 7900
VNC_OFFSET = 2000  # vnc_port = novnc_port - 2000
NAME_RE = re.compile(r"^scraper-(\d+)$")


def list_containers():
    """Return our containers (running or stopped) as dicts, newest port last."""
    res = subprocess.run(
        [
            "docker", "ps", "-a",
            "--filter", f"label=app={LABEL}",
            "--format", '{{.Names}}\t{{.State}}\t{{.Label "scraper_label"}}',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    items = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        label = parts[2] if len(parts) > 2 else ""
        m = NAME_RE.match(name)
        port = int(m.group(1)) if m else None
        items.append(
            {"name": name, "status": state, "novnc_port": port, "label": label}
        )
    items.sort(key=lambda x: x["novnc_port"] or 0)
    return items


def slugify_label(name: Optional[str]) -> str:
    """Clean a user-supplied code name into a short, display-friendly label."""
    if not name:
        return ""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-").lower()
    return s[:40]


def allocate_novnc_port():
    """Pick the lowest free noVNC host port at/above NOVNC_BASE."""
    used = {c["novnc_port"] for c in list_containers() if c["novnc_port"]}
    port = NOVNC_BASE
    while port in used:
        port += 1
    return port


def valid_name_or_400(name: str):
    """Guard against shell/path injection — names must be scraper-<digits>."""
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid container name")
    return name


def build_dockerfile(data_filename: Optional[str] = None) -> str:
    """Generate the Dockerfile. scraper.py and requirements.txt always have
    fixed names; the optional data file keeps its original name and is copied
    next to scraper.py so the scraper's relative paths resolve, plus an
    INPUT_FILE env var points at it as a clean convention."""
    lines = [
        "FROM selenium/standalone-chrome:latest",
        "",
        "USER root",
        "",
        "RUN apt-get update && apt-get install -y python3 python3-pip && \\",
        "    apt-get clean && rm -rf /var/lib/apt/lists/*",
        "",
        f"WORKDIR {WORKDIR}",
        "",
        "COPY requirements.txt .",
        "RUN pip3 install --break-system-packages -r requirements.txt",
        "",
        "COPY scraper.py .",
    ]
    if data_filename:
        # JSON/exec form so filenames with spaces or odd chars are safe.
        lines.append(f"COPY {json.dumps([data_filename, './'])}")
        lines.append(f"ENV INPUT_FILE={json.dumps(WORKDIR + '/' + data_filename)}")
    # Keep the selenium/noVNC supervisor (entry_point.sh) running in the
    # FOREGROUND so the container — and the live browser view — stays alive
    # even after the scraper finishes or crashes. The scraper runs in the
    # background once the X display has had time to come up; `-u` keeps its
    # stdout unbuffered so `docker logs -f` streams it live.
    cmd = (
        "/opt/bin/entry_point.sh & "
        "sleep 12 && python3 -u scraper.py; "
        "wait"
    )
    lines += [
        "",
        f'CMD ["/bin/bash", "-c", {json.dumps(cmd)}]',
        "",
    ]
    return "\n".join(lines)


# Top-level import name -> PyPI package name, for the common cases where they
# differ. Anything not here is assumed to match its import name.
IMPORT_TO_PKG = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "fitz": "pymupdf",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "serial": "pyserial",
    "win32com": "pywin32",
    "google": "google-api-python-client",
}


def _normalize_pkg(name: str) -> str:
    """PEP 503 name normalization: lowercase, runs of -_. collapse to a dash."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def parse_requirement_names(requirements_text: str):
    """Return the set of normalized package names declared in requirements.txt."""
    names = set()
    for raw in requirements_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # PEP 508 "name @ url" — keep the name before the @
        line = line.split("@", 1)[0]
        m = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if m:
            names.add(_normalize_pkg(m.group(1)))
    return names


def validate_requirements(scraper_code: str, requirements_text: str):
    """Find third-party modules imported by the scraper that aren't covered by
    requirements.txt. Returns a list of (import_name, suggested_package).
    Standard-library and relative imports are ignored."""
    try:
        tree = ast.parse(scraper_code)
    except SyntaxError:
        return []  # can't parse — skip the check rather than guess

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # skip relative imports
                imported.add(node.module.split(".")[0])

    stdlib = set(sys.stdlib_module_names)
    declared = parse_requirement_names(requirements_text)

    missing = []
    for name in sorted(imported):
        if name in stdlib or name.startswith("_"):
            continue
        pkg = IMPORT_TO_PKG.get(name, name)
        if _normalize_pkg(pkg) not in declared:
            missing.append((name, pkg))
    return missing


def adjust_input_path(code: str, filename: str):
    """Rewrite string literals in the scraper that reference the data file via
    an absolute or sub-folder path so they point at the bare filename. The file
    is copied next to scraper.py in the container's WORKDIR, so the basename
    resolves there. Only literals with a path separator before the filename are
    touched, leaving an already-relative "links.csv" untouched.
    Returns (new_code, number_of_replacements)."""
    pattern = re.compile(r'(["\'])([^"\']*[\\/])' + re.escape(filename) + r'\1')
    return pattern.subn(lambda m: m.group(1) + filename + m.group(1), code)


@app.post("/scrapers-upload")
async def upload_scraper(
    scraper: UploadFile = File(...),
    requirements: UploadFile = File(...),
    data: Optional[UploadFile] = File(None),
    name: Optional[str] = Form(None),
):
    # Read the uploaded bytes up front: the request body is closed once this
    # handler returns, but the StreamingResponse generator runs afterwards.
    scraper_bytes = await scraper.read()
    requirements_bytes = await requirements.read()
    scraper_code = scraper_bytes.decode("utf-8", errors="replace")
    requirements_text = requirements_bytes.decode("utf-8", errors="replace")

    # Optional data file (CSV/Excel of links). Keep only the basename to avoid
    # path traversal, and use it both for the Dockerfile COPY and the rewrite.
    data_filename = None
    data_bytes = None
    adjust_note = ""
    if data is not None and data.filename:
        data_filename = os.path.basename(data.filename)
        data_bytes = await data.read()
        scraper_code, n = adjust_input_path(scraper_code, data_filename)
        if n:
            adjust_note = (
                f"[adjust] rewrote {n} path reference(s) to "
                f'"{data_filename}" so the scraper reads the uploaded file\n'
            )

    # Pre-build sanity check: warn about imports not covered by requirements.txt.
    missing = validate_requirements(scraper_code, requirements_text)
    if missing:
        check_note = (
            "[check] these imports are not in requirements.txt — the build may "
            "succeed but the scraper will crash at runtime:\n"
        )
        for import_name, pkg in missing:
            suffix = "" if import_name == pkg else f"  (import: {import_name})"
            check_note += f"        • {pkg}{suffix}\n"
    else:
        check_note = "[check] all imports are covered by requirements.txt\n"

    # Allocate a slot: a unique host port pair + container/image names. Each
    # build creates a NEW container that runs alongside the others.
    novnc_port = allocate_novnc_port()
    vnc_port = novnc_port - VNC_OFFSET
    container_name = f"scraper-{novnc_port}"
    image_tag = f"scraper-img-{novnc_port}"
    # Optional friendly name so different codes are distinguishable in the list.
    code_label = slugify_label(name)

    build_dir = tempfile.mkdtemp(prefix="scraper-build-")
    with open(os.path.join(build_dir, "scraper.py"), "w", encoding="utf-8") as f:
        f.write(scraper_code)
    with open(os.path.join(build_dir, "requirements.txt"), "wb") as f:
        f.write(requirements_bytes)
    if data_filename:
        with open(os.path.join(build_dir, data_filename), "wb") as f:
            f.write(data_bytes)
    with open(os.path.join(build_dir, "Dockerfile"), "w") as f:
        f.write(build_dockerfile(data_filename))

    def stream_build():
        # --progress=plain gives line-oriented output that streams cleanly;
        # stderr is merged into stdout so the client sees one ordered log.
        proc = subprocess.Popen(
            [
                "docker", "build",
                "--progress=plain",
                "-t", image_tag,
                build_dir,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Docker emits UTF-8; force it so Windows doesn't fall back to
            # cp1252 and choke on box-drawing / non-ASCII bytes.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "DOCKER_BUILDKIT": "1"},
        )
        try:
            label_note = f" ({code_label})" if code_label else ""
            yield f"[slot] container={container_name}{label_note} novnc=localhost:{novnc_port}\n"
            if data_filename:
                yield f"[input] using uploaded data file: {data_filename}\n"
            if adjust_note:
                yield adjust_note
            yield check_note
            yield f"$ docker build -t {image_tag} {build_dir}\n"
            for line in proc.stdout:
                yield line
            proc.wait()
            if proc.returncode != 0:
                yield f"\n[error] build failed (exit code {proc.returncode})\n"
                return

            yield f"\n[done] image built successfully: {image_tag}\n"

            # Build succeeded — run a NEW container. The selenium/standalone-chrome
            # image serves a noVNC web client on 7900 (password "secret"); we map
            # it to this slot's unique host port so the frontend can embed it.
            yield f"\n$ starting container {container_name} (noVNC on {novnc_port})...\n"
            # Clear any stale container reusing this exact name (e.g. a crashed
            # leftover); other slots are untouched.
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            run = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name", container_name,
                    "--label", f"app={LABEL}",
                    "--label", f"scraper_label={code_label}",
                    "--shm-size=2g",
                    "-p", f"{novnc_port}:7900",
                    "-p", f"{vnc_port}:5900",
                    image_tag,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if run.returncode != 0:
                yield f"[error] failed to start container:\n{run.stderr}\n"
                return

            yield f"[container] started: {run.stdout.strip()[:12]}\n"

            # docker run -d returns once the container starts, but the noVNC
            # server inside takes a few seconds to come up. Poll its HTTP
            # endpoint so we only signal "ready" once it actually serves —
            # otherwise the iframe loads into a connection refusal.
            yield f"[wait] waiting for noVNC server on port {novnc_port}...\n"
            ready = False
            for _ in range(40):  # ~40s budget
                try:
                    url = f"http://127.0.0.1:{novnc_port}/"
                    with urllib.request.urlopen(url, timeout=2) as r:
                        if r.status == 200:
                            ready = True
                            break
                except Exception:
                    pass
                time.sleep(1)

            if not ready:
                yield "[warn] noVNC did not respond in time — use Reconnect once it's up\n"
            # Marker the frontend watches for to focus the live view on this
            # container: includes the name and port so it knows where to connect.
            yield f"[vnc-ready] name={container_name} port={novnc_port}\n"
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

    return StreamingResponse(stream_build(), media_type="text/plain")


@app.get("/containers")
def get_containers():
    """List all scraper containers (running and stopped) for the sidebar."""
    return list_containers()


@app.post("/containers/{name}/stop")
def stop_container(name: str):
    """Stop and remove a single container, leaving the others running."""
    valid_name_or_400(name)
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "name": name}


@app.get("/scraper-output/{name}")
def scraper_output(name: str):
    """Stream a specific container's stdout/stderr (the scraper's print output,
    progress, results) by following its docker logs."""
    valid_name_or_400(name)

    def stream_logs():
        proc = subprocess.Popen(
            ["docker", "logs", "-f", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                yield line
        finally:
            proc.terminate()

    return StreamingResponse(stream_logs(), media_type="text/plain")
