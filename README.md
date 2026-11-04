# MORTIS — Local-First AI Web Spider & Knowledge Assistant

**Mortis** is a local-first web crawler and research assistant for Windows. It pairs an explicitly armed Chromium page-capture extension and same-host link crawler with a desktop spider widget and Streamlit dashboard. Ollama provides local embeddings and answers, while SQLite stores the searchable page text and vectors on your machine.

---

## Architecture & Data Flow

```
+---------------------------------------------------------------------------------------+
|                                    WINDOWS DESKTOP                                    |
|                                                                                       |
|  +--------------------+   Native Messaging   +----------------------+                 |
|  | Chromium Extension | -------------------> | MortisNativeHost.exe |                 |
|  | (Brave/Chrome/Edge)|                      +----------+-----------+                 |
|  +--------------------+                                 |                             |
|                                                         v                             |
|                                             +-----------------------+                 |
|                                             |  mortis_native_host   |                 |
|                                             |       (Python)        |                 |
|                                             +-----------+-----------+                 |
|                                                         |                             |
|  +---------------------+    IPC Named Pipe    +---------v-----------+   Ollama HTTP   |
|  |    Mortis Spider    | <==================> |     mortis_ipc      | <-------------> |
|  |   (Desktop Widget)  |  (\\.\pipe\mortis-*) |  (Named Pipe Server)|   (127.0.0.1:   |
|  +----------+----------+                      +---------+-----------+      11434)     |
|             |                                           |                             |
|             | (Ollama RAG & Crawling)                   v                             |
|             |                                 +---------------------+                 |
|             +-------------------------------->|     mortis_core     |                 |
|             |                                 | (Embedding & Search)|                 |
|             v                                 +---------+-----------+                 |
|  +---------------------+                                |                             |
|  | Streamlit Dashboard | <------------------------------+                             |
|  | (http://localhost)  |                                v                             |
|  +---------------------+                      +---------------------+                 |
|                                               | spider_knowledge.db |                 |
|                                               |  (SQLite Database)  |                 |
|                                               +---------------------+                 |
+---------------------------------------------------------------------------------------+
```

### Subsystems:
1. **Desktop Spider (`desktop_spider.py`)**: An always-on-top, draggable desktop widget featuring a procedural vector spider and status display. Hosts the authenticated named-pipe IPC bridge for the browser extension, manages page crawling, and provides instant desktop Q&A.
2. **Mortis Core & Knowledge Store (`mortis_core.py`, `spider_knowledge.db`)**: Handles URL normalization, DOM text extraction, chunking, local vector embedding generation (`nomic-embed-text`), SQLite storage, cosine similarity ranking, and source-grounded RAG query answering (`llama3.2`).
3. **Streamlit Research Dashboard (`app.py`)**: Web-based control interface (`http://localhost:8501`) for manual page crawling, knowledge base browsing, chunk inspection, and semantic search.
4. **Browser Extension & Native Host (`browser_extension/`, `mortis_native_launcher.c`, `mortis_native_host.py`)**: Extension for Brave, Chrome, and Edge to arm tabs, extract readable DOM snapshots, and feed them into the local spider pipeline. The Windows launcher executable is built locally from the included C source and is not stored in Git.

---

## Prerequisites

- **OS**: Windows 10 or Windows 11 (64-bit).
- **Python**: 3.11 or higher.
- **Ollama**: Local Ollama service running with the following models:
  ```bash
  ollama pull llama3.2
  ollama pull nomic-embed-text
  ```
- **Browser**: Brave, Chrome, or Edge with developer mode enabled (for loading unpacked extensions).

---

## How to Launch

### 1. Launch Mortis (Single Command)
Run setup once, then launch from PowerShell or double-click:
```powershell
.\setup.bat
.\launch.bat
```
This launcher:
- Validates the local Python environment.
- Starts the Streamlit dashboard on `http://localhost:8501`.
- Starts the Mortis Desktop Spider widget.
- Initializes the named-pipe IPC listener for browser extension requests.

### 2. Browser Extension Setup
1. Open your Chromium browser's extension settings (e.g., `brave://extensions` or `chrome://extensions`).
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the `browser_extension` directory in this workspace.
4. To build the Windows Native Messaging launcher, open a **Visual Studio Developer Command Prompt** in this project directory and run:
   ```powershell
   .\build_native_host.bat
   ```
   This creates the ignored local file `MortisNativeHost.exe` from `mortis_native_launcher.c`.
5. Run the native host installer once from PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Install-MortisNativeHost.ps1
   ```
   The installer registers the extension IDs you provide and uses the local `.venv` Python interpreter.
6. Click the Mortis extension icon on a web page and choose **Arm this tab** before asking the desktop spider to capture it.

## Local data and configuration

- The SQLite knowledge base is created as `spider_knowledge.db` beside the application. It is local-only and excluded from Git.
- `.venv`, caches, logs, backups, environment files, and compiled binaries are ignored by Git.
- Set `OLLAMA_URL` before launch to use a different local Ollama endpoint; the default is `http://localhost:11434`.
- Install the configured models with `ollama pull llama3.2` and `ollama pull nomic-embed-text`.

## Tests

Run the Python suite from the project directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The browser service worker lifecycle mock also runs with Node.js:

```powershell
node tests/test_native_extension_lifecycle.js
```

---

## Stopping Mortis

- In the console window: Press **Ctrl+C**. The launcher coordinates a graceful shutdown across all child processes and closes active job objects.
- From the desktop spider: Right-click the spider and select **Exit Mortis Spider**, or press **Esc**.
