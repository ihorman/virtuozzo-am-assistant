"""Language knowledge base for deploying ANY application (deploy-anything).

One module answers three questions for every language:

1. **What is this project?** :func:`detect_project` maps manifest/toolchain
   files (package.json, Cargo.toml, mix.exs, *.cabal, dune-project, build.zig,
   CMakeLists.txt, Makefile, *.csproj, shard.yml, pubspec.yaml, Package.swift,
   *.nimble, stack.yaml, ...) to ``{type, version, evidence}`` rows plus a
   suggested stack, runtime image and app port. Unknown languages never
   dead-end: they map to a Docker Hub runtime image by naming convention
   (crystal -> lang/crystal, haskell -> haskell, ocaml -> ocaml/opam, ...) and
   anything without an inferable image leaves the choice to the user
   (``runtime_image`` free text or the Tier 4 VPS bootstrap).
2. **Where does it run?** :data:`LANGUAGES` carries the tiered deploy table
   (docs/PLAN.md "Deploy anything - any language"): Tier 1 native platform
   stacks, Tier 2 runtime-image deploy (build INSIDE the container - no image
   build), Tier 3 user-supplied registry image, Tier 4 VPS bootstrap.
3. **How is it built and started?** :func:`build_runtime_manifest` and
   :func:`build_vps_manifest` generate the JPS manifest: docker/VPS node +
   cmd actions that fetch the source, install deps, build and start the app
   with ``nohup setsid``. Scripts travel base64-encoded inside the manifest:
   the YAML stays ASCII-only and round-trips byte-exact (no f-string YAML
   heredoc quoting bugs).

Everything here is pure (no network, no disk, no execution): archives are
walked in memory with strict caps.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import io
import json
import re
import tarfile
import zipfile
from typing import Any, Mapping, Optional, Tuple

# --- caps (deployment-scoped, docs/SECURITY.md scope confinement) -----------

#: whole source download cap
MAX_SOURCE_BYTES = 25 * 1024 * 1024
#: archive entry cap (zip-bomb / tar-bomb defence)
MAX_ENTRIES = 4000
#: per-entry read cap
MAX_FILE_BYTES = 256 * 1024
#: total bytes read out of the archive
MAX_TOTAL_READ = 8 * 1024 * 1024


# --- language table ---------------------------------------------------------

#: per-language version extractors: (file pattern, regex, group) in priority
#: order; the first file that yields a version is the evidence file.
_VERSION_RULES: dict = {
    "node": [
        (".nvmrc", r"(\d+(?:\.\d+)*)", 1),
        ("package.json", r'"(?:node)"\s*:\s*"([^"]+)"', 1),
    ],
    "python": [
        (".python-version", r"(\d+(?:\.\d+)*)", 1),
        ("pyproject.toml", r'requires-python\s*=\s*"([^"]+)"', 1),
        ("runtime.txt", r"python-(\d+(?:\.\d+)*)", 1),
        ("setup.py", r"python_requires\s*=\s*['\"]([^'\"]+)", 1),
    ],
    "java": [
        ("pom.xml", r"<(?:java\.version|maven\.compiler\.(?:source|target|"
                    r"release)|release)>\s*(\d+(?:\.\d+)*)", 1),
        ("build.gradle", r"(?:JavaVersion\.VERSION_(\d+)|"
                         r"JavaLanguageVersion\.of\((\d+)\)|"
                         r"sourceCompatibility\s*=\s*['\"]?(\d+(?:\.\d+)?))", 0),
        ("build.gradle.kts", r"(?:JavaVersion\.VERSION_(\d+)|"
                             r"JavaLanguageVersion\.of\((\d+)\)|"
                             r"jvmToolchain\((\d+)\))", 0),
    ],
    "php": [
        ("composer.json", r'"php"\s*:\s*"([^"]+)"', 1),
    ],
    "ruby": [
        (".ruby-version", r"(\d+(?:\.\d+)*)", 1),
        ("Gemfile", r"""ruby\s+['\"](\d+(?:\.\d+)*)""", 1),
    ],
    "go": [
        ("go.mod", r"^go\s+(\d+(?:\.\d+)*)", 1),
    ],
    "dotnet": [
        ("*.csproj", r"<TargetFramework>\s*net(\d+(?:\.\d+)?)", 1),
        ("*.fsproj", r"<TargetFramework>\s*net(\d+(?:\.\d+)?)", 1),
    ],
    "rust": [
        ("rust-toolchain.toml", r'channel\s*=\s*"([^"]+)"', 1),
        ("rust-toolchain", r"(\d+(?:\.\d+)*)", 1),
        ("Cargo.toml", r'rust-version\s*=\s*"([^"]+)"', 1),
    ],
    "elixir": [
        ("mix.exs", r'elixir:\s*"([^"]+)"', 1),
    ],
    "crystal": [
        ("shard.yml", r"crystal:\s*['\"]?(\d+(?:\.\d+)*)", 1),
    ],
    "dart": [
        ("pubspec.yaml", r"sdk:\s*['\"]([^'\"]+)", 1),
    ],
    "swift": [
        ("Package.swift", r"swift-tools-version:?\s*(\d+(?:\.\d+)*)", 1),
    ],
}


def _lang(key: str, display: str, tier: int, *, stack: Optional[str] = None,
          image: str = "", image_default: str = "", port: int = 8080,
          markers: Tuple[str, ...] = (), build: str = "true",
          start: str = "true", toolchain: str = "") -> dict:
    return {"key": key, "display": display, "tier": tier, "stack": stack,
            "image": image, "image_default": image_default, "port": port,
            "markers": tuple(markers), "build": build, "start": start,
            "toolchain": toolchain}


#: The tiered deploy table. Order IS the detection priority (first match is
#: the primary suggestion); ``c_cpp`` / ``docker`` / ``static`` sit last and
#: act as fallbacks when no language-specific manifest matched.
LANGUAGES: Tuple[dict, ...] = (
    _lang("node", "Node.js", 1, stack="nodejs", image="node:{v}",
          image_default="20", port=3000,
          markers=("package.json", ".nvmrc", "package-lock.json",
                   "yarn.lock", "pnpm-lock.yaml"),
          build="npm ci || npm install",
          start="npm start || node index.js",
          toolchain="nodejs npm"),
    _lang("python", "Python", 1, stack="python", image="python:{v}-slim",
          image_default="3.12", port=8000,
          markers=("requirements.txt", "pyproject.toml", "Pipfile",
                   "setup.py", ".python-version", "runtime.txt"),
          build="pip install --no-cache-dir -r requirements.txt || "
                "pip install --no-cache-dir .",
          start="python app.py || python main.py || python3 app.py",
          toolchain="python3 python3-pip python3-venv"),
    _lang("java", "Java", 1, stack="java", image="eclipse-temurin:{v}",
          image_default="21", port=8080,
          markers=("pom.xml", "build.gradle", "build.gradle.kts"),
          build="mvn -q -DskipTests package || gradle build -x test",
          start="java -jar target/*.jar || java -jar build/libs/*.jar",
          toolchain="default-jdk maven"),
    _lang("php", "PHP", 1, stack="php", image="php:{v}-apache",
          image_default="8.3", port=80,
          markers=("composer.json", "composer.lock"),
          build="composer install --no-dev --no-interaction || true",
          start="apache2-foreground",
          toolchain="php-cli php-mbstring composer"),
    _lang("ruby", "Ruby", 1, stack="ruby", image="ruby:{v}-slim",
          image_default="3.3", port=3000,
          markers=("Gemfile", "Gemfile.lock", ".ruby-version"),
          build="bundle install",
          start="bundle exec rails server -b 0.0.0.0 -p $PORT || "
                "bundle exec rackup -o 0.0.0.0 -p $PORT",
          toolchain="ruby-full ruby-bundler build-essential"),
    _lang("go", "Go", 1, stack="golang", image="golang:{v}",
          image_default="1.23", port=8080,
          markers=("go.mod", "go.sum"),
          build="go build -o app .",
          start="./app",
          toolchain="golang-go"),
    _lang("dotnet", ".NET", 1, stack="dotnet",
          image="mcr.microsoft.com/dotnet/sdk:{v}", image_default="8.0",
          port=8080, markers=("*.csproj", "*.fsproj", "*.vbproj"),
          build="dotnet publish -c Release -o out",
          start="dotnet out/*.dll --urls http://0.0.0.0:$PORT || "
                "dotnet run --urls http://0.0.0.0:$PORT",
          toolchain=""),
    # --- Tier 2 native runtime-image languages (build inside the container)
    _lang("rust", "Rust", 2, image="rust:{v}-slim", image_default="1.80",
          port=8080,
          markers=("Cargo.toml", "rust-toolchain.toml", "rust-toolchain"),
          build="cargo build --release",
          start="cargo run --release",
          toolchain="curl build-essential; curl --proto '=https' --tlsv1.2 "
                    "-sSf https://sh.rustup.rs | sh -s -- -y; "
                    ". \"$HOME/.cargo/env\""),
    _lang("elixir", "Elixir", 2, image="elixir:{v}-slim",
          image_default="1.17", port=4000,
          markers=("mix.exs", "mix.lock"),
          build="mix deps.get && mix compile",
          start="mix phx.server || mix run --no-halt",
          toolchain="erlang elixir"),
    _lang("zig", "Zig", 2, image="zig", image_default="", port=8080,
          markers=("build.zig", "build.zig.zon"),
          build="zig build -Doptimize=ReleaseSafe || zig build",
          start="exec $(ls zig-out/bin/* 2>/dev/null | head -1) || zig build run",
          toolchain=""),
    _lang("deno", "Deno", 2, image="denoland/deno", image_default="",
          port=8000, markers=("deno.json", "deno.jsonc", "deno.lock"),
          build="deno cache main.ts || deno cache main.js || true",
          start="deno run --allow-net --allow-env main.ts || "
                "deno run --allow-net --allow-env main.js",
          toolchain=""),
    _lang("bun", "Bun", 2, image="oven/bun", image_default="", port=3000,
          markers=("bun.lockb", "bunfig.toml"),
          build="bun install",
          start="bun run start || bun index.ts || bun index.js",
          toolchain=""),
    _lang("haskell", "Haskell", 2, image="haskell", image_default="",
          port=3000, markers=("*.cabal", "stack.yaml", "package.yaml"),
          build="cabal update && cabal build || stack build",
          start="cabal run || stack run",
          toolchain="ghc cabal-install"),
    _lang("ocaml", "OCaml", 2, image="ocaml/opam", image_default="",
          port=8080, markers=("dune-project",),
          build="opam install . --deps-only -y && dune build",
          start="dune exec ./bin/main.exe || dune exec",
          toolchain="ocaml dune"),
    _lang("crystal", "Crystal", 2, image="lang/crystal", image_default="",
          port=3000, markers=("shard.yml",),
          build="shards install && shards build",
          start="exec $(ls bin/* 2>/dev/null | head -1) || "
                "crystal run src/main.cr",
          toolchain=""),
    _lang("dart", "Dart", 2, image="dart", image_default="", port=8080,
          markers=("pubspec.yaml", "pubspec.lock"),
          build="dart pub get",
          start="dart run bin/server.dart || dart run",
          toolchain=""),
    _lang("swift", "Swift", 2, image="swift", image_default="", port=8080,
          markers=("Package.swift",),
          build="swift build -c release",
          start="swift run",
          toolchain=""),
    _lang("nim", "Nim", 2, image="nimlang/nim", image_default="", port=8080,
          markers=("*.nimble", "nim.cfg"),
          build="nimble install -y --depsOnly || true",
          start="nimble run || nim c -r src/*.nim",
          toolchain=""),
    # --- fallbacks (only suggested when nothing above matched)
    _lang("c_cpp", "C/C++", 2, image="gcc", image_default="", port=8080,
          markers=("CMakeLists.txt", "Makefile", "makefile", "configure",
                   "configure.ac"),
          build="cmake -B build && cmake --build build || make",
          start="./app || ./build/app || ./a.out",
          toolchain="build-essential cmake"),
    _lang("docker", "Docker", 3, image="", image_default="", port=8080,
          markers=("Dockerfile", "Containerfile"),
          build="true", start="true", toolchain=""),
    _lang("static", "Static site", 2, image="nginx:alpine", image_default="",
          port=80, markers=("index.html",),
          build="true",
          start="nginx -g 'daemon off;'",
          toolchain=""),
)

_BY_KEY = {lang["key"]: lang for lang in LANGUAGES}

#: runtime-image repo (lowercased) -> language key (for deploy_any when the
#: caller supplies an image instead of a source to inspect).
IMAGE_LANGUAGE = {
    "node": "node", "python": "python", "eclipse-temurin": "java",
    "openjdk": "java", "php": "php", "ruby": "ruby", "golang": "go",
    "mcr.microsoft.com/dotnet/sdk": "dotnet",
    "mcr.microsoft.com/dotnet/aspnet": "dotnet",
    "rust": "rust", "elixir": "elixir", "zig": "zig",
    "denoland/deno": "deno", "oven/bun": "bun", "haskell": "haskell",
    "ocaml/opam": "ocaml", "lang/crystal": "crystal",
    "crystallang/crystal": "crystal", "dart": "dart", "swift": "swift",
    "nimlang/nim": "nim", "gcc": "c_cpp", "nginx": "static",
}

#: framework conventions (spec: "express/fastify 3000, flask/django 8000/8080,
#: spring 8080, rails 3000, phoenix 4000, actix/axum 8080"). Each rule:
#: (language, file pattern, keyword regex, port).
FRAMEWORK_PORTS = (
    ("node", "package.json", r'"(express|fastify|next)"', 3000),
    ("python", "requirements.txt", r"(?i)\bflask\b", 8000),
    ("python", "requirements.txt", r"(?i)\bdjango\b", 8080),
    ("python", "pyproject.toml", r"(?i)\bflask\b", 8000),
    ("python", "pyproject.toml", r"(?i)\bdjango\b", 8080),
    ("java", "pom.xml", r"(?i)spring", 8080),
    ("java", "build.gradle", r"(?i)spring", 8080),
    ("ruby", "Gemfile", r"(?i)\brails\b", 3000),
    ("elixir", "mix.exs", r"(?i)phoenix", 4000),
    ("rust", "Cargo.toml", r'"(actix-web|axum|rocket)"', 8080),
)


def language_display(key: str) -> str:
    return _BY_KEY.get(key, {}).get("display", key)


def language_tier(key: str) -> int:
    return int(_BY_KEY.get(key, {}).get("tier", 2))


def language_port(key: str) -> int:
    return int(_BY_KEY.get(key, {}).get("port", 8080))


def runtime_image(key: str, version: Optional[str] = None) -> Optional[str]:
    """Language key (+ optional version) -> Docker runtime image name."""
    lang = _BY_KEY.get(key)
    if not lang or not lang["image"]:
        return None
    version = (version or "").strip() or lang["image_default"]
    if "{v}" in lang["image"]:
        if not version:
            return None
        return lang["image"].replace("{v}", version)
    return lang["image"]


def image_language(image: str) -> Optional[str]:
    """Runtime image name -> language key (naming convention lookup)."""
    repo = str(image or "").strip()
    if not repo:
        return None
    if "/" in repo:
        # keep the meaningful tail: "mcr.microsoft.com/dotnet/sdk" stays whole
        # (registry-prefixed), "rust:1.80-slim" -> "rust".
        if repo.count("/") >= 2 and not repo.startswith(("http",)):
            head = repo.rsplit(":", 1)[0]
            if head in IMAGE_LANGUAGE:
                return IMAGE_LANGUAGE[head]
        repo = repo.rsplit("/", 1)[-1]
    repo = repo.split(":", 1)[0].lower()
    return IMAGE_LANGUAGE.get(repo)


def image_version(image: str) -> Optional[str]:
    """'rust:1.80-slim' -> '1.80'; 'node:20' -> '20'."""
    if ":" not in str(image or ""):
        return None
    tag = image.rsplit(":", 1)[1]
    match = re.match(r"(\d+(?:\.\d+)*)", tag)
    return match.group(1) if match else None


# --- version extraction -----------------------------------------------------

def _clean_version(raw: str) -> Optional[str]:
    """>=8.2 / '~> 1.17' / '3.0.0 <4.0.0' -> '8.2' / '1.17' / '3.0'."""
    text = str(raw or "").strip()
    match = re.search(r"\d+(?:\.\d+)*", text)
    if not match:
        return None
    parts = match.group(0).split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else parts[0]


def _find(files: Mapping[str, str], pattern: str) -> Optional[str]:
    """First file path whose basename matches ``pattern`` (fnmatch)."""
    for path in files:
        base = path.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(base, pattern):
            return path
    return None


def _version_for(key: str, files: Mapping[str, str]
                 ) -> Tuple[Optional[str], Optional[str]]:
    """(version, evidence file) from the language's version rules."""
    for pattern, regex, group in _VERSION_RULES.get(key, ()):
        path = _find(files, pattern)
        if path is None:
            continue
        text = files.get(path) or ""
        match = re.search(regex, text, re.MULTILINE)
        if match:
            version = _clean_version(match.group(group))
            if version:
                return version, path.rsplit("/", 1)[-1]
    return None, None


def _framework_port(key: str, files: Mapping[str, str]) -> Optional[int]:
    for lang, pattern, regex, port in FRAMEWORK_PORTS:
        if lang != key:
            continue
        path = _find(files, pattern)
        if path is not None and re.search(regex, files.get(path) or ""):
            return port
    return None


def detect_project(files: Mapping[str, str]) -> dict:
    """Manifest/toolchain files -> detection result.

    Returns ``{detected: [{type, version, evidence}], suggested_stack,
    suggested_runtime_image, suggested_port, confidence}``. Every known
    match is listed in ``detected``; the first (priority) one drives the
    suggestions. Marker-less sources return an empty ``detected`` and no
    suggestions - the wizard then asks which runtime image to use (the
    universal-start guarantee never errors out).
    """
    found = []
    for lang in LANGUAGES:
        marker = next((p for p in lang["markers"] if _find(files, p)), None)
        if marker is None:
            continue
        marker_path = _find(files, marker)
        marker_name = marker_path.rsplit("/", 1)[-1] if marker_path else marker
        version, evidence = _version_for(lang["key"], files)
        found.append({"type": lang["key"],
                      "version": version,
                      "evidence": evidence or marker_name})

    primary = found[0] if found else None
    suggested_stack = None
    suggested_image = None
    suggested_port = 8080
    confidence = "low"
    if primary is not None:
        key = primary["type"]
        lang = _BY_KEY[key]
        version = primary["version"] or lang["image_default"]
        if lang["tier"] == 1 and lang["stack"]:
            suggested_stack = f"{lang['stack']}:{version}" if version \
                else lang["stack"]
        else:
            suggested_stack = f"{key}:{version}" if version else key
        suggested_image = runtime_image(key, primary["version"])
        suggested_port = _framework_port(key, files) or language_port(key)
        confidence = "high" if primary["version"] else "medium"
    return {"detected": found,
            "suggested_stack": suggested_stack,
            "suggested_runtime_image": suggested_image,
            "suggested_port": suggested_port,
            "confidence": confidence}


# --- archive walking (in memory, capped) ------------------------------------

class SourceTooLargeError(Exception):
    """Archive exceeds the size/entry caps."""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def read_archive_entries(data: bytes, name: str = "") -> dict:
    """Walk a zip / tar.gz / tar archive entirely in memory.

    Returns ``{path: text}`` for text entries under the caps. Never extracts
    to disk (``BytesIO`` only). Raises :class:`SourceTooLargeError` on the
    entry-count / per-file / total-read caps and ``ValueError`` when the data
    is not a recognized archive.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("source is not archive data")
    entries: dict = {}
    total = 0
    count = 0

    def _take(path: str, blob: bytes) -> None:
        nonlocal total, count
        count += 1
        if count > MAX_ENTRIES:
            raise SourceTooLargeError("archive has too many entries")
        blob = blob[:MAX_FILE_BYTES]
        total += len(blob)
        if total > MAX_TOTAL_READ:
            raise SourceTooLargeError("archive contents exceed the read cap")
        entries[path] = _decode(blob)

    head = bytes(data[:4])
    is_zip = head.startswith(b"PK\x03\x04")
    is_gzip = head.startswith(b"\x1f\x8b")
    lowered = str(name or "").lower()
    if is_zip:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > MAX_FILE_BYTES and \
                        not info.filename.endswith(("/Makefile",)):
                    _take(info.filename, b"")
                    continue
                _take(info.filename, archive.read(info))
        return entries
    if is_gzip or lowered.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
                                    ".tar.xz", ".txz", ".tar")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for info in archive.getmembers():
                if not info.isfile():
                    continue
                if info.size > MAX_FILE_BYTES:
                    _take(info.name, b"")
                    continue
                handle = archive.extractfile(info)
                _take(info.name, handle.read() if handle else b"")
        return entries
    raise ValueError("source is not a zip or tar archive")


def inspect_archive(data: bytes, name: str = "") -> dict:
    """Archive bytes -> detection result (with ``source_format`` added)."""
    entries = read_archive_entries(data, name)
    result = detect_project(entries)
    head = bytes(data[:4])
    if head.startswith(b"PK\x03\x04"):
        result["source_format"] = "zip"
    elif head.startswith(b"\x1f\x8b"):
        result["source_format"] = "tar.gz"
    else:
        result["source_format"] = "tar"
    return result


# --- git URL handling -------------------------------------------------------

_SSH_GIT_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
                     ".tar.xz", ".txz")
_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")


def normalize_git_url(url: str) -> str:
    """>git@github.com:org/repo.git -> https://github.com/org/repo."""
    match = _SSH_GIT_RE.match(str(url or "").strip())
    if match:
        return f"https://{match.group(1)}/{match.group(2)}"
    return str(url or "").strip()


def looks_like_git_url(url: str) -> bool:
    text = normalize_git_url(url)
    lowered = text.lower()
    if lowered.endswith(".git"):
        return True
    if lowered.endswith(_ARCHIVE_SUFFIXES):
        return False
    host = re.sub(r"^https?://", "", text).split("/", 1)[0].lower()
    return host in _GIT_HOSTS


def git_archive_url(url: str) -> Optional[str]:
    """Git repo URL -> downloadable source archive URL for INSPECTION only.

    The deploy path clones inside the runtime container; inspection needs a
    plain archive instead. Known hosts map to their default-branch archive
    endpoints (HEAD resolves to the default branch); unknown hosts return
    None - the caller asks for an archive URL.
    """
    text = normalize_git_url(url)
    match = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", text)
    if not match:
        return None
    host, path = match.group(1).lower(), match.group(2)
    repo = path.rsplit("/", 1)[-1]
    if host == "github.com":
        return f"https://github.com/{path}/archive/HEAD.tar.gz"
    if host == "gitlab.com":
        return f"https://gitlab.com/{path}/-/archive/HEAD/{repo}-HEAD.tar.gz"
    if host == "bitbucket.org":
        return f"https://bitbucket.org/{path}/get/HEAD.tar.gz"
    return None


def git_default_branch(final_url: str) -> Optional[str]:
    """>codeload.github.com/o/r/tar.gz/refs/heads/main -> 'main'."""
    text = str(final_url)
    match = re.search(r"refs/heads/([A-Za-z0-9._/-]+?)(?:\.tar\.gz)?/?$",
                      text)
    if not match:
        match = re.search(r"(?:/-/archive/|/get/|tar\.gz/)"
                          r"([A-Za-z0-9._/-]+?)(?:\.tar\.gz)?/?$", text)
    if match:
        branch = match.group(1)
        return branch if branch.upper() != "HEAD" else None
    return None


# --- JPS manifest generation (ASCII-only, base64-script round-trip) ---------

def _sh_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _b64(script: str) -> str:
    return base64.b64encode(script.encode("utf-8")).decode("ascii")


def _run_action(script: str) -> str:
    """One JPS cmd action: decode the base64 script and run it."""
    return f"echo {_b64(script)} | base64 -d | /bin/sh"


def _start_action(script: str) -> str:
    """Start action: install the generated start script, run detached."""
    return (f"echo {_b64(script)} | base64 -d > /opt/app/.run.sh && "
            f"chmod +x /opt/app/.run.sh && "
            f"nohup setsid /bin/sh /opt/app/.run.sh > /var/log/app.log 2>&1 &")


def fetch_script(source: Mapping[str, str]) -> str:
    """Shell that fetches the source into /opt/app (git or archive)."""
    git_url = source.get("git_url")
    if git_url:
        branch = str(source.get("branch") or "").strip()
        clone = (f"git clone --depth 1 -b {_sh_quote(branch)} "
                 if branch else "git clone --depth 1 ")
        return (
            "set -e\n"
            "rm -rf /opt/app\n"
            "mkdir -p /opt/app\n"
            f"{clone}{_sh_quote(git_url)} /opt/app\n")
    archive_url = str(source.get("archive_url") or "")
    return (
        "set -e\n"
        "rm -rf /opt/app /tmp/src\n"
        "mkdir -p /opt/app /tmp/src\n"
        f"curl -fsSL {_sh_quote(archive_url)} -o /tmp/source-archive\n"
        "case " + _sh_quote(archive_url) + " in\n"
        "  *.zip)\n"
        "    command -v unzip >/dev/null 2>&1 || "
        "(apt-get update -qq && apt-get install -y -qq unzip)\n"
        "    unzip -oq /tmp/source-archive -d /tmp/src ;;\n"
        "  *)\n"
        "    tar -xzf /tmp/source-archive -C /tmp/src ;;\n"
        "esac\n"
        "if [ \"$(ls -A /tmp/src | wc -l)\" -eq 1 ] && "
        "[ -d /tmp/src/$(ls -A /tmp/src) ]; then\n"
        "  cp -a /tmp/src/$(ls -A /tmp/src)/. /opt/app/\n"
        "else\n"
        "  cp -a /tmp/src/. /opt/app/\n"
        "fi\n")


def build_script(language: Optional[str]) -> str:
    lang = _BY_KEY.get(language or "")
    commands = (lang or {}).get("build") or "true"
    return "set -e\ncd /opt/app\n" + commands + "\n"


def start_script(language: Optional[str], port: int) -> str:
    lang = _BY_KEY.get(language or "")
    commands = (lang or {}).get("start") or "true"
    return ("set -e\n"
            "cd /opt/app\n"
            f"export PORT=\"${{PORT:-{int(port)}}}\"\n"
            f"export HOST=\"${{HOST:-0.0.0.0}}\"\n"
            f"exec {commands}\n")


def bootstrap_script(language: Optional[str], source: Mapping[str, str],
                     port: int) -> str:
    """Tier 4 VPS bootstrap: install a toolchain, fetch, build, run."""
    lang = _BY_KEY.get(language or "")
    toolchain = (lang or {}).get("toolchain") or "build-essential"
    return (
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update -qq\n"
        "apt-get install -y -qq curl git ca-certificates "
        + toolchain + "\n"
        + fetch_script(source)
        + build_script(language)
        + "cd /opt/app\n"
        + f"export PORT=\"${{PORT:-{int(port)}}}\"\n"
        + f"nohup setsid /bin/sh -c {_sh_quote((lang or {}).get('start') or 'true')}"
        + " > /var/log/app.log 2>&1 &\n")


def _yaml_quote(value: Any) -> str:
    """JSON string - valid YAML scalar, ASCII-only, never ambiguous."""
    return json.dumps(str(value), ensure_ascii=True)


def _emit_yaml(value: Any, indent: int = 0) -> list:
    pad = "  " * indent
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(pad + "-")
                lines.extend(_emit_yaml(item, indent + 1))
            else:
                lines.append(pad + "- " + _yaml_quote(item))
        return lines
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if item:
                    lines.append(pad + _yaml_quote(key) + ":")
                    lines.extend(_emit_yaml(item, indent + 1))
                else:
                    lines.append(pad + _yaml_quote(key) + ": "
                                 + ("[]" if isinstance(item, list) else "{}"))
            else:
                lines.append(pad + _yaml_quote(key) + ": "
                             + _yaml_quote(item))
        return lines
    return [pad + _yaml_quote(value)]


def render_manifest(manifest: Mapping[str, Any]) -> str:
    """Deterministic ASCII-only YAML (every scalar JSON-quoted)."""
    text = "\n".join(_emit_yaml(dict(manifest))) + "\n"
    if any(ord(char) > 127 for char in text) or "\t" in text:
        raise ValueError("manifest must be ASCII-only without tabs")
    return text


def build_runtime_manifest(*, name: str, image: str, port: int,
                           source: Mapping[str, str],
                           language: Optional[str] = None,
                           cloudlets: int = 8) -> str:
    """Tier 2/3 manifest: docker node from the runtime image + cmd actions.

    The code is built INSIDE the runtime container (fetch -> deps -> build ->
    start with nohup setsid); no image build happens. Scripts travel
    base64-encoded and round-trip byte-exact.
    """
    scripts = [fetch_script(source), build_script(language)]
    actions = [{"cmd [cp]": _run_action(script)} for script in scripts]
    actions.append({"cmd [cp]": _start_action(start_script(language, port))})
    manifest = {
        "type": "install",
        "jpsType": "install",
        "name": name,
        "version": "1.0",
        "description": f"Generated runtime-image deploy ({image})",
        "env": {"PORT": str(int(port))},
        "nodes": {"cp": {"nodeType": "docker", "image": image,
                         "cloudlets": int(cloudlets)}},
        "actions": {"onInstall": actions},
    }
    return render_manifest(manifest)


def build_vps_manifest(*, name: str, port: int, source: Mapping[str, str],
                       language: Optional[str] = None,
                       cloudlets: int = 8) -> str:
    """Tier 4 manifest: VPS node + generated bootstrap script."""
    actions = [{"cmd [cp]": _run_action(
        bootstrap_script(language, source, port))}]
    manifest = {
        "type": "install",
        "jpsType": "install",
        "name": name,
        "version": "1.0",
        "description": "Generated VPS bootstrap deploy (exotic runtime)",
        "env": {"PORT": str(int(port))},
        "nodes": {"cp": {"nodeType": "vps", "cloudlets": int(cloudlets)}},
        "actions": {"onInstall": actions},
    }
    return render_manifest(manifest)


def embedded_scripts(manifest_text: str) -> list:
    """Extract + decode the base64 scripts embedded in a generated manifest.

    Test/verification seam: proves the YAML round-trips the scripts.
    """
    import yaml

    manifest = yaml.safe_load(manifest_text)
    scripts = []
    for action in ((manifest.get("actions") or {}).get("onInstall") or []):
        command = next(iter(action.values()))
        match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d",
                          str(command))
        if match:
            scripts.append(base64.b64decode(match.group(1)).decode("utf-8"))
    return scripts
