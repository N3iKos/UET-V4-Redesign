# Universal Excel Translator v4 (UET-v4)

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![UI Framework](https://img.shields.io/badge/TUI-Textual-green.svg)](https://textual.textualize.io/)
[![License](https://img.shields.io/badge/license-MIT-purple.svg)](LICENSE)
[![Security](https://img.shields.io/badge/vault-AES--CTR%20%2B%20PBKDF2-orange.svg)]()
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()

**Universal Excel Translator v4 (UET-v4)** is a high-performance terminal spreadsheet localization platform featuring a modern Text User Interface (TUI). It is engineered specifically for translating game scripts, visual novels, RPG dialogue tables, localization sheets, and large-scale tabular datasets (from hundreds to tens of thousands of rows) using any external OpenAI-compatible LLM endpoint.

UET-v4 solves the critical failure modes of automated spreadsheet translation: **row desynchronization (row drift)**, **game engine tag/control code corruption**, **API 429 rate limits & HTTP 503 errors**, **crashes caused by files opened in Microsoft Excel**, and the **lack of post-translation quality arbitration**.

---

## Table of Contents
- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Prerequisites & Dependencies](#-prerequisites--dependencies)
- [Installation & Quick Start](#-installation--quick-start)
- [Comprehensive User Guide](#-comprehensive-user-guide)
  - [1. Initial Setup (Master Vault & Providers)](#1-initial-setup-master-vault--providers)
  - [2. Model Groups / Combos](#2-model-groups--combos)
  - [3. Translation Workflow (Translate Wizard)](#3-translation-workflow-translate-wizard)
  - [4. AI Arbiter & Quality Control (The Judge)](#4-ai-arbiter--quality-control-the-judge)
  - [5. Interactive Playground (Prompt & Model Testing)](#5-interactive-playground-prompt--model-testing)
- [Keybindings & Navigation](#-keybindings--navigation)
- [Project Directory Structure](#-project-directory-structure)
- [Security & Data Storage](#-security--data-storage)
- [Troubleshooting & FAQ](#-troubleshooting--faq)
- [License](#-license)

---

## 🚀 Key Features

### 1. Zero Hardcoded Topology & Encrypted Local Vault
- **Provider Agnostic**: No AI provider names, endpoints, or model tables are hardcoded into the codebase. Connect to any service: OpenAI, OpenRouter, DeepSeek, Groq, Anthropic Proxies, Ollama, LM Studio, vLLM, or custom reverse proxies.
- **Encrypted Master Vault**: API credentials and provider configurations are securely stored in `~/.config/excel-translator-v4/vault.enc` using **PBKDF2-HMAC-SHA256 (310,000 iterations)** for key derivation, an **AES-CTR stream cipher (SHA-256 keystream XOR)**, and **HMAC-SHA256** integrity verification.
- **Multi-Key Connection Pooling**: Add multiple API keys per provider with *least-used* load balancing, latency tracking, daily quota exhaustion detection, and automated failover.

### 2. Model Groups / Combos (Fallback & Round-Robin)
- Group multiple models across different providers into a single unified execution target.
- **Fallback Strategy**: Automatically cascades to backup models whenever the primary model encounters rate limits, timeouts, or 5xx server outages.
- **Round-Robin Strategy**: Balances request volume sequentially across multiple models or connections to maximize throughput without exceeding quota ceilings.

### 3. Smart Batch Translation & Anti-Desynchronization Pipeline
- **Auto Column Detection**: Intelligently identifies source columns (*original, source, src, japanese, input*) and target output columns (*translation, translated, target, output*).
- **Format & Game Tag Protection**: Protects game engine control codes and format tags (such as RPG Maker `\v[1]`, `\c[2]`, `\!`, `{0}`, line breaks `\n`, HTML/BBCode) before prompting the LLM and restores them with absolute fidelity after parsing.
- **3-Layer Alignment Validation**: Detects line merging, skipped rows, and row drift using statistical correlation algorithms.
- **Recovery Ladder**: When batch parsing fails, tasks automatically break down into *mini-tasks*, degrading down to *solo-tasks* (1 row per request) which are 100% immune to row shifting.
- **Translation Memory (TM)**: Embedded SQLite storage (`tm.db`) for normalized source strings. Previously translated lines are resolved locally without consuming API tokens.
- **Smart Deduplication**: Automatically detects repeated text (e.g. standard NPC dialogue or repetitive UI terms), translates unique lines once, and broadcasts translations back across all target rows while preserving unique line prefix tags.

### 4. Quality Arbitration (The Judge) & Automated Repair
- **AI-Powered Quality Assurance**: Compares original source rows directly against translated spreadsheet output.
- **Comprehensive Evaluation Criteria**: Detects *Mistranslation*, *Loss of Context*, *Placeholder/Tag Damage*, *Tone/Style Inconsistency*, and *Text Overflow / Fitting*.
- **Severity-Based Triage**: Classifies findings into `Critical`, `Moderate`, and `Minor` categories.
- **Staged Fix All**: Generates batch repair suggestions held in-memory for translator review before any changes are written to disk (*Human-in-the-Loop*).
- **Anti-Oscillation Guard**: Stops infinite re-judging loops if multiple models disagree and contradict each other's edits.

### 5. Multi-File Parallel Tabs
- Translate up to 4 spreadsheet files simultaneously.
- **Leader-Follower Pattern**: Settings configured on the primary file (Leader) are automatically inherited by subsequent files (Followers), saving repetitive setup time.

### 6. Non-Destructive I/O & Sibling Lock Recovery
- **Non-Destructive**: Original source files are **strictly read-only** and never overwritten.
- Translated output is written to a dedicated file named `<filename>_Translated.<ext>`.
- **Atomic Disk Writes**: File saving relies on OS-level atomic file replacement (`.tmp` -> `os.replace`).
- **Sibling Forking**: If the destination file is locked by Microsoft Excel, UET-v4 will not crash; it automatically forks output to a new sibling file (e.g. `<filename>_Translated_1.xlsx`) and continues processing uninterrupted.
- **Sidecar Failed Cache**: Interrupted batches and failed row indexes are recorded atomically in `.uet_cache/`, enabling instant resume and re-translation at any time.

---

## 🏗 System Architecture

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                TEXTUAL TUI LAYER                                       │
│  UETApp (Screen Stack: Vault -> MainMenu -> Wizard -> Run -> Summary / Judge / Play)  │
│  Shared Widgets: ModelPicker | FileTabBar | ActivityPanel | EventsPanel | CompareGrid  │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │ Thread boundary (@work(thread=True))
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│                          CORE PROCESSING ENGINE SUBSYSTEM                              │
│  - ProcessingEngine: Row Classifier, Range Slicer, Resume/Sweep, Repair Phase         │
│  - MultiFileDraftManager: Leader/Follower Inheritance, Worker Allocator               │
│  - DedupManager: Hash Fingerprinting, Unique File Extraction, Tagged Broadcast         │
│  - TranslationMemory (TM): Persistent SQLite (tm.db) Normalized Storage               │
└─────────────────────┬────────────────────────────────────────────────────┬─────────────┘
                      │ Batches (BatchTask)                                │ Raw Text
┌─────────────────────▼────────────────────────────┐ ┌─────────────────────▼─────────────┐
│          SCHEDULING & RESILIENCY ENGINE          │ │      PARSING & ALIGNMENT ENGINE       │
│  - Scheduler: Sequential | Parallel | Wave       │ │  - ParseRecoveryPipeline:             │
│  - Watchdog Monitor: Timeout & Generation Bump   │ │      Strict [N] -> Loose -> Solo      │
│  - RetryExecutor: Priority Failover & Backoff    │ │  - AlignmentValidator:               │
│  - ConnectionPool & ComboPool: Load Balancing    │ │      Statistical Shift & Merge Detect │
│  - KeyPool: Least-Used, Latency, Cooldown        │ │  - PlaceholderEngine: Game Token Enc  │
└─────────────────────┬────────────────────────────┘ └─────────────────────┬─────────────┘
                      │ HTTP Requests (JSON)                               │ Clean Translations
┌─────────────────────▼────────────────────────────┐ ┌─────────────────────▼─────────────┐
│            PROVIDER & SECURITY LAYER             │ │             I/O & PERSISTENCE             │
│  - ProviderClient: Generic OpenAI HTTP / SSE     │ │  - ExcelReader: Stream XLSX / CSV     │
│  - Vault: AES-CTR + PBKDF2 (310k) + HMAC-SHA256  │ │  - ExcelWriter: Async Thread + Atomic │
│  - ProviderRegistry: In-Memory Decrypted Cache   │ │  - Lock Recovery: Sibling Forking     │
│  - ReasoningConfig: User-Defined Meta Paths      │ │  - FailedCache: Sidecar JSON Storage  │
└──────────────────────────────────────────────────┘ └───────────────────────────────────┘
```

---

## 📦 Prerequisites & Dependencies

- **Python**: Version **3.10** or higher (tested on 3.10, 3.11, 3.12, 3.13).
- **Core Python Libraries**:
  - `textual >= 0.50.0` (Terminal UI engine)
  - `requests >= 2.28.0` (HTTP API communication)
  - `openpyxl >= 3.1.0` (XLSX reading & writing)

---

## 🛠 Installation & Quick Start

### Option 1: Windows Automatic Launcher (Recommended)
Simply double-click `Run.bat`. The script automatically checks for Python, installs any missing dependencies, and starts the application:
```cmd
Run.bat
```

### Option 2: Manual Terminal Setup (Windows, macOS, Linux)
1. Clone or download the repository:
   ```bash
   git clone https://github.com/your-username/UET-V4-Redesign.git
   cd UET-V4-Redesign
   ```
2. Install required packages:
   ```bash
   pip install -r requirements.txt
   ```
3. Launch the application:
   ```bash
   python main.py
   ```

---

## 📖 Comprehensive User Guide

### 1. Initial Setup (Master Vault & Providers)
When launching the application for the first time:
1. **Set Master Password**: Create a master password to encrypt your local vault. This password will be required each time you start UET-v4 to decrypt your API keys.
2. **Add a Provider** (`Providers & Models`):
   - Navigate to `Providers & Models` (Press `4` or select from the home menu).
   - Click **Add Provider**.
   - Enter a provider name (e.g. `OpenRouter`, `DeepSeek`, `LocalOllama`, `OpenAI`).
   - Enter the **Base URL** (e.g. `https://openrouter.ai/api/v1` or `http://localhost:11434/v1`).
3. **Add Connections & API Keys**:
   - Select your provider and click **Add Connection**.
   - Enter a connection label (e.g. `Primary Key`, `Backup Key`).
   - Enter the API Key.
   - Configure rate limits: **RPM** (Requests Per Minute) and **TPM** (Tokens Per Minute).
4. **Register Models**:
   - Click **Fetch Models** to automatically populate models from the provider endpoint, or click **Add Model** to specify a model ID manually (e.g. `deepseek/deepseek-chat`, `gpt-4o-mini`, `gemini-2.0-flash`).
   - If the model supports reasoning tokens (thinking budgets), configure its reasoning profile here.

---

### 2. Model Groups / Combos
Model Groups allow you to orchestrate multiple models into a resilient fallback or load-balanced pipeline:
1. Open the `Model Groups` screen (Press `5`).
2. Click **New Group** and give it a name (e.g. `Fast-Fallback-Chain`).
3. Select an execution strategy:
   - **Fallback**: Tries the first model; on failure, rate limit, or timeout, immediately routes to the next model in the group.
   - **Round Robin**: Distributes batches sequentially across configured models/keys to maximize throughput.
4. Add models across any configured provider into the group.

---

### 3. Translation Workflow (Translate Wizard)

To translate a spreadsheet file, select **Translate** (Press `1` or `F1`):

#### Step 1: Select Model
Choose a standalone model or a Model Group.

#### Step 2: File & Columns
- Browse or input the path to your spreadsheet (`.xlsx` or `.csv`).
- UET-v4 displays a table preview and auto-detects the source and target columns.
- Adjust column mappings, specify header rows, and set row ranges (*Start Row* to *End Row*).

#### Step 3: Language & Style
- Select the source language (e.g. `Japanese`, `English`) and target language (e.g. `Indonesian`, `English`).
- Choose a style preset (e.g. Natural Localization, Visual Novel / RPG Dialogue, Formal) or enter a custom localization prompt.

#### Step 4: Processing Settings
- **Strategy**: Select `Parallel` (for standard high-speed runs), `Sequential` (for strict rate-limited tiers), or `Wave` (phased lockstep execution).
- **Batch Size**: Number of rows per LLM request (auto-suggested based on RPM/TPM).
- **Concurrency (Workers)**: Number of concurrent worker threads.
- **Enable Translation Memory (TM)**: Reuses identical previously translated lines without API calls.
- **Enable Deduplication**: Condenses repetitive rows to minimize token consumption.

#### Step 5: Review & Run
- Verify your settings in the review screen.
- Click **Start Translation**.
- The **Run Screen** provides real-time monitoring:
  - Interactive progress bar and completion percentage.
  - Counters for completed, failed, and retrying rows.
  - Processing speed (*rows/min*) and Estimated Time of Arrival (ETA).
  - Live activity feed and row-by-row translation stream.

#### Step 6: Summary & Output
- Output files are saved automatically beside the original file with the `_Translated.<ext>` suffix.
- If any rows encountered errors, click **Retry Failed Rows** to re-process only the affected lines.

---

### 4. AI Arbiter & Quality Control (The Judge)

Use **The Judge** to inspect and audit completed translations:
1. Open **Judge** from the menu (Press `2` or `F2`).
2. Select your translated spreadsheet and designate the source vs. translated columns.
3. Select audit criteria:
   - `Mistranslation`: Meaning contradicts the source text.
   - `Context`: Line lacks narrative cohesion or dialogue flow.
   - `Placeholder`: Missing, altered, or corrupt game engine variables (`\v[n]`, `{name}`, `[BR]`).
   - `Style`: Stiff language, robotic phrasing, or out-of-character tone.
   - `Fitting`: Text exceeds typical visual boundary constraints.
4. Execute the evaluation. Results appear in a triage view categorized by severity (*Critical*, *Moderate*, *Minor*).
5. **Remediation Options**:
   - **Manual Edit**: Directly edit translations in the terminal.
   - **Fix Row**: Send a single line to the model with targeted repair instructions.
   - **Fix All**: Run batch repairs for all flagged rows.
   - **Apply & Save**: Confirms staged fixes and updates the spreadsheet file.

---

### 5. Interactive Playground (Prompt & Model Testing)

Use the **Playground** (Press `3` or `F3`) to test prompts before launching large batch jobs:
- Input sample text or challenging dialogue lines.
- Select multiple models for side-by-side comparison.
- Tweak temperature, system prompts, and reasoning parameters.
- Inspect output speed, token consumption, and translation naturalness in real time.

---

## ⌨ Keybindings & Navigation

| Shortcut | Description |
|---|---|
| `1` / `F1` | Open **Translate Wizard** |
| `2` / `F2` | Open **The Judge (Quality Assurance)** |
| `3` / `F3` | Open **Playground** |
| `4` / `F4` | Open **Providers & Models** |
| `5` / `F5` | Open **Model Groups** |
| `6` / `F6` | Open **Settings** |
| `Esc` / `←` (Left) | Return to previous screen (Back) |
| `Tab` / `Shift+Tab` | Navigate between form fields and buttons |
| `Enter` / `Space` | Activate button / toggle selection |
| `Ctrl+C` | Graceful exit |

---

## 📁 Project Directory Structure

```text
UET-V4-Redesign/
├── main.py                     # Application entry point
├── Run.bat                     # Windows automatic launch script
├── requirements.txt            # Python dependencies
├── .gitignore                  # Git exclusion rules
├── LICENSE                     # MIT License
├── styleprompt.txt             # Reference prompt template for game localization
├── translator/                 # Core engine package
│   ├── config.py               # Global settings, constants, and safety limits
│   ├── core/                   # Processing engine, worker pool, dedup, TM, crash logger
│   ├── data/                   # Style presets and user preferences
│   ├── io/                     # Streaming XLSX (openpyxl) and CSV adapters
│   ├── judge/                  # The Judge QA arbitration engine and prompt templates
│   ├── logging_/               # Thread-safe logging subsystem
│   ├── parsing/                # Placeholder protection, alignment & row shift validators
│   ├── playground/             # Multi-model comparison runner and prompt tester
│   ├── providers/              # HTTP client, dynamic registry, rate limiters, reasoning configs
│   ├── scheduling/             # Sequential, Parallel, and Wave schedulers, key pool, watchdog
│   ├── tui/                    # Textual UI screens, widgets, and themes
│   └── vault/                  # Local encrypted credentials vault (AES-CTR + PBKDF2)
├── tests/                      # Pytest unit and integration test suite
└── Docs/                       # System architecture, benchmarks, and technical specifications
```

---

## 🔒 Security & Data Storage

All sensitive user data is kept strictly inside your operating system user profile directory (never committed to Git or stored in public project folders):
- **Configuration Directory**: `~/.config/excel-translator-v4/`
  - `vault.enc`: Encrypted provider credentials, connection URLs, and API keys.
  - `style_presets.json`: Custom style prompt presets.
  - `combos.json`: Configured Model Groups.
  - `app_prefs.json`: General preferences (theme, token caps, default modes).
- **Temporary Cache**: `.uet_cache/` in the project root for sidecar resume records and deduplication caches.

---

## ❓ Troubleshooting & FAQ

### 1. `PermissionError: [Errno 13] Permission denied` on output file
- **Cause**: The output spreadsheet is currently open in Microsoft Excel or another program.
- **Solution**: Close the file in Excel. UET-v4 includes an automated **Sibling Forking** mechanism that diverts output to a new sibling file (e.g. `<filename>_Translated_1.xlsx`) if the primary file is locked, ensuring translation progress is never lost.

### 2. HTTP 429 (Too Many Requests)
- **Cause**: The request rate exceeds your provider's RPM or TPM tier limits.
- **Solution**:
  - In `Providers & Models`, ensure your connection's RPM and TPM values match your actual account tier.
  - Add multiple API keys to the provider to enable automatic load distribution.
  - In Processing Settings, lower the number of *Workers* or switch the strategy to `Sequential`.

### 3. Forgotten Master Vault Password
- **Security Notice**: Because the vault utilizes standard PBKDF2/AES encryption without any backdoor, passwords cannot be recovered.
- **Solution**: After 5 incorrect password attempts, a reset prompt will appear. Type `DELETE` to clear the vault and create a fresh one.

### 4. Game Control Tags or Variables Damaged
- **Solution**: UET-v4 features an integrated `placeholders.py` module that encodes standard game control sequences (RPG Maker `\v[n]`, `\c[n]`, `{name}`, etc.) into inert tags during translation. Ensure your source column is mapped accurately and avoid prompting the model to translate code syntax.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE). You are free to use, modify, and distribute this software in accordance with the license conditions.
