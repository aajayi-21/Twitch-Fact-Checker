# 🛠️ CLAUDE.md - Twitch Live Fact-Checker

## 🎯 Project Overview
This project is a Chrome Extension (Manifest V3) paired with a Python backend. It actively monitors an open Twitch live stream, transcribes the audio, and uses a web-search-grounded LLM (Google Gemini or some other LLM) to automatically detect testable claims. When a claim requires verification, the extension displays a non-intrusive popup over the stream featuring a high-level label (e.g., **TRUE**, **FALSE**, **MISLEADING**, **UNVERIFIED**) and a concise, well-sourced explanation. The popup should be aesthetically pleasing.

## 🏗️ Architecture & Tech Stack

The following architecture is a recommendation that should be used as inspiration. If there is a more effective architecture structure use it.

### Frontend: Chrome Extension (`/extension`)
*   **Core:** Manifest V3, JavaScript (ES6+) or TypeScript, HTML/CSS.
*   **Audio/Text Capture:** Web Speech API (for browser-side transcription) OR parsing Twitch's native closed captions.
*   **UI:** Injected DOM elements (Shadow DOM recommended to prevent CSS conflicts with Twitch).
*   **Communication:** Background Service Workers handling API calls to the Python backend.

### Backend: Python API (`/backend`)
*   **Core:** Python 3.11+, FastAPI (for high-performance, asynchronous REST/WebSocket endpoints).
*   **LLM Integration:** Google GenAI SDK (Gemini Pro/Flash with Google Search Tool enabled). Or another model
*   **Logic Pipeline:** 
    1. Receive transcribed text buffer.
    2. Determine if the buffer contains a factual claim worth checking.
    3. Run grounded web search.
    4. Return structured JSON response (Label, Explanation, Sources).

---

## 💻 Development Commands

### Backend (Python)
*   **Install dependencies:** `cd backend && uv sync` (uv-managed; deps in
    `backend/pyproject.toml`, pinned by `backend/uv.lock`)
*   **Run tests:** `cd backend && uv run pytest -m "not slow"`
*   **GPU speech backend (XPU/CUDA/ROCm):** `./backend/scripts/install_stt_gpu.sh`
The backend should be run using a local server

### Frontend (Chrome Extension)
*   **Load unpacked extension:** Go to `chrome://extensions/`, enable "Developer mode", and select the `/extension` folder.
*   **Reloading:** Click the refresh icon on the extension card in `chrome://extensions/` after making code changes.

---

## 🎨 Coding Style & Standards

As an AI assisting with this project, you must strictly adhere to the following coding guidelines to ensure readability, maintainability, and optimal performance.

### 🐍 Python (Backend)
1.  **Type Hinting:** All functions and methods must use Python type hints (`def process_claim(text: str) -> dict:`).
2.  **Frameworks:** Use `FastAPI` for endpoints and `Pydantic` models for request/response validation. 
3.  **Style:** Strictly adhere to PEP 8. Use `black` formatting styles (max line length 88). 
4.  **Error Handling:** Never swallow exceptions. Use explicit `try/except` blocks and return appropriate HTTP status codes (e.g., 500 for LLM failures, 400 for bad input) with descriptive error messages.
5.  **Asynchronous Code:** Use `async/await` for all network-bound operations (FastAPI endpoints, LLM API calls).
6.  **Documentation:** Provide clear docstrings for complex logic, especially the prompt engineering sections.

### 🟨 JavaScript/Extension (Frontend)
1.  **Modern Syntax:** Use ES6+ syntax (arrow functions, destructuring, template literals, async/await). Avoid `var`.
2.  **Modularity:** Keep Content Scripts, Background Workers, and UI logic in separate files. 
3.  **DOM Manipulation:** When injecting the popup into Twitch, create a container with Shadow DOM to encapsulate our CSS and prevent Twitch's stylesheets from breaking the UI.
4.  **Performance:** Throttle or debounce the text chunks being sent to the backend to prevent API rate-limiting and high server costs. Do not send every single word; send logical semantic chunks (e.g., every 5-10 seconds of speech).
5.  **Permissions:** Request only the absolute minimum permissions required in `manifest.json`.

---

## 🧠 LLM & Prompting Guidelines

Use this section as rough inspiration

When writing the code that interacts with Gemini:
*   **Structured Output:** Force the LLM to return strict JSON using `response_schema` or structured prompt instructions.
*   **JSON Schema:**
    ```json
    {
      "requires_check": boolean,
      "claim": "The exact claim made",
      "label": "TRUE | FALSE | MISLEADING | UNVERIFIED",
      "explanation": "2-3 sentences explaining the truthfulness grounded in sources.",
      "sources": ["url1", "url2"]
    }
    ```
*   **System Prompting:** Ensure the system prompt heavily penalizes hallucinations. The LLM must default to `UNVERIFIED` if the web search does not yield highly reputable, conclusive results.
*   **Filtering:** Build logic to ignore opinions, gaming jargon, and subjective statements (e.g., "This game is terrible", "I'm going to push the left lane").

---

## 🤖 Instructions for AI Assistants (System Rules)
1.  **Think Before Coding:** Briefly outline the architecture of the solution before writing the code block.
2.  **Complete Solutions:** Provide complete, runnable snippets rather than pseudo-code. If a file needs to be modified, provide the full context of the modification.
3.  **Readability Above All:** Prioritize clear, descriptive variable and function names (`evaluate_claim_truthfulness` instead of `eval_clm`). 
4.  **Security:** Never hardcode API keys. Always use environment variables (`.env`) in the backend.
