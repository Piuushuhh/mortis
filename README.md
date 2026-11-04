# MORTIS — Local-First AI Web Spider & Knowledge Assistant

**Mortis** is an offline, privacy-first web spider and AI research companion for Windows. It pairs a Chromium page-capture extension and multi-depth link crawler with a desktop spider widget and Streamlit research dashboard, backed entirely by local Ollama intelligence and SQLite vector storage.

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
4. **Browser Extension & Native Host (`browser_extension/`, `MortisNativeHost.exe`, `mortis_native_host.py`)**: Extension for Brave, Chrome, and Edge to arm tabs, extract readable DOM snapshots, and feed them directly into the local spider pipeline.

---

## Prerequisites

- **OS**: Windows 10 or Windows 11 (64-bit).
- **Python**: 3.11 or higher (configured in project `.venv/`).
- **Ollama**: Local Ollama service running with the following models:
  ```bash
  ollama pull llama3.2
  ollama pull nomic-embed-text
  ```
- **Browser**: Brave, Chrome, or Edge with developer mode enabled (for loading unpacked extensions).

---

## How to Launch

### 1. Launch Mortis (Single Command)
Run from PowerShell or double-click:
```powershell
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
4. Run the native host installer once:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Install-MortisNativeHost.ps1
   ```
5. Click the Mortis extension icon on any web page to "Arm this tab" and capture content into your local knowledge base.

---

## Stopping Mortis

- In the console window: Press **Ctrl+C**. The launcher coordinates a graceful shutdown across all child processes and closes active job objects.
- From the desktop spider: Right-click the spider and select **Exit Mortis Spider**, or press **Esc**.
