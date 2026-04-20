# Automator — Dependency & Prerequisites Reference

Python >= 3.10 required.

## Python Packages

These packages are recommended to be installed inside a repo-local `.venv`.

### Core Runtime Dependencies

- **tiktoken>=0.5.0**: Improved token estimation (falls back to `len/3` heuristic if absent).
- **typing_extensions>=4.0.0**: Backports of newer typing features for Python 3.10 compatibility.
- **requests>=2.28.0**: Common dependency for generated delivery scripts (Microsoft Graph API, etc.).
- **pyyaml>=6.0**: YAML frontmatter parsing for Agent Skills (falls back to regex parser if absent).

### Document & Office Formats

- **python-docx>=1.1.0**: Microsoft Word (.docx) generation.
- **openpyxl>=3.1.0**: Excel (.xlsx) manipulation.
- **pandas>=2.0.0**: Data analysis and manipulation.
- **reportlab>=4.0.0**: PDF generation.
- **pdfplumber>=0.11.0**: PDF text and table extraction.
- **pypdf>=4.0.0**: General PDF manipulation.
- **pdf2image>=1.17.0**: PDF to image conversion.

### API Mode Dependencies

Only needed if `config/backends.json` uses `mode: "api"`. Uncomment the vendor SDKs you need in `requirements.txt`:

```text
anthropic>=0.39.0
google-genai>=1.0.0
openai>=1.50.0
```

---

## System Prerequisites

Install these separately—they are not Python packages.

### 1. AI CLI Tools

At least one is required as the engine spawns it via subprocess.

- **Claude Code**: `npm install -g @anthropic-ai/claude-code`
- **Gemini CLI**: `npm install -g @google/gemini-cli`
- **Codex CLI**: `npm install -g @openai/codex` (experimental)

#### Usage

```bash
./automator --cli claude --project new --task "..."
./automator --cli gemini --project new --task "..."
./automator --cli claude --check-runtime
```

> **Runtime Requirement:** If Automator runs under an outer sandboxed launcher, that outer runtime must allow outbound network access for spawned backend CLIs.

### 2. Python Environment

Automator is designed to work with Python 3.10 or higher. Install dependencies directly:

```bash
pip install -r requirements.txt
```

**Preferred entrypoint:** `./automator ...` (uses system `python3` if no repo-local `.venv` is found).

### 3. Node.js / npm

Required to install the AI CLI tools above. [LTS version recommended](https://nodejs.org/).

### 4. Secure Storage Library (Linux/WSL)

Required for the Claude CLI keychain on Linux. Without it the CLI falls back to FileKeychain (functional but prints a warning to stderr on every run):

```bash
sudo apt-get install -y libsecret-1-0
```

- **libsecret-1-0**: GNOME/libsecret shared library used by the Claude CLI for secure credential storage.

### 5. Optional Document/PDF Helper Binaries

Lightweight PDF helpers for advanced workflows:

```bash
sudo apt-get install -y poppler-utils qpdf
```

- **poppler-utils**: `pdftoppm`, `pdftotext`, `pdfinfo` (required for `pdf2image` and PDF inspection).
- **qpdf**: structural PDF merge/rotate/check workflows.

> **Note:** `pandoc` and `libreoffice` are not required for basic document generation.

### 6. Git

Required for project version control.

---

## Platform

Tested on **Linux (Ubuntu/WSL)** and **macOS**. No OS-specific dependencies.
*Windows may work but is untested—WSL is recommended.*

---

## Standard Library Modules

These modules ship with Python—no separate installation needed:

`argparse`, `base64`, `collections`, `csv`, `dataclasses`, `datetime`, `enum`, `io`, `json`, `os`, `pathlib`, `re`, `shlex`, `subprocess`, `sys`, `tempfile`, `time`, `typing`, `unittest`, `urllib`, `uuid`, `xml.etree.ElementTree`, `zipfile`, `zoneinfo`.
