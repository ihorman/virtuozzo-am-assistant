"""Fixture application sources for the deploy-anything tests.

Each fixture is a tiny but REAL project layout per language (manifest +
entrypoint) built into an in-memory zip - exactly what ``project_inspect``
consumes. Used by the tests and available for offline demos.
"""

from __future__ import annotations

import io
import zipfile

#: language -> {path: text} fixture project files
FIXTURES: dict = {
    "node": {
        "package.json": (
            '{\n'
            '  "name": "node-demo",\n'
            '  "version": "1.0.0",\n'
            '  "engines": { "node": ">=20" },\n'
            '  "scripts": { "start": "node index.js" },\n'
            '  "dependencies": { "express": "^4.18.0" }\n'
            '}\n'),
        "index.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "app.get('/', (_req, res) => res.send('hello'));\n"
            "app.listen(process.env.PORT || 3000, '0.0.0.0');\n"),
    },
    "python": {
        "requirements.txt": "flask==3.0.0\n",
        ".python-version": "3.12\n",
        "app.py": (
            "from flask import Flask\n"
            "app = Flask(__name__)\n\n"
            "@app.get('/')\n"
            "def index():\n"
            "    return 'hello'\n"),
    },
    "rust": {
        "Cargo.toml": (
            '[package]\n'
            'name = "rust-demo"\n'
            'version = "0.1.0"\n'
            'edition = "2021"\n'
            'rust-version = "1.80"\n\n'
            '[dependencies]\n'
            'axum = "0.7"\n'),
        "src/main.rs": (
            "#[tokio::main]\n"
            "async fn main() {\n"
            "    println!(\"hello\");\n"
            "}\n"),
    },
    "docker": {
        "Dockerfile": (
            "FROM node:20-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "CMD [\"node\", \"index.js\"]\n"),
        "index.js": "console.log('hello');\n",
    },
    "go": {
        "go.mod": "module example.com/go-demo\n\ngo 1.23\n",
        "main.go": "package main\n\nfunc main() {}\n",
    },
    "java": {
        "pom.xml": (
            "<project>\n"
            "  <properties>\n"
            "    <java.version>21</java.version>\n"
            "  </properties>\n"
            "</project>\n"),
    },
    "elixir": {
        "mix.exs": (
            "defmodule Demo.MixProject do\n"
            "  use Mix.Project\n"
            "  def project, do: [app: :demo, version: \"0.1.0\","
            " elixir: \"~> 1.17\"]\n"
            "end\n"),
    },
    # marker-less source: no toolchain file the wizard can map -> the
    # universal-start guarantee kicks in (ask for a runtime image / VPS).
    "unknown": {
        "main.weirdlang": "print 'hello'\n",
        "README.md": "An exotic-language demo app.\n",
    },
}


def source_zip(language: str) -> bytes:
    """Build the fixture project for ``language`` as zip bytes."""
    files = FIXTURES[language]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(f"{language}-demo/{path}", text)
    return buffer.getvalue()
