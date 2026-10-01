# WhatsApp + Saved Context Ollama Analyzer

Local-first Streamlit app for browsing WhatsApp exports + saved context, generating day-by-day summaries, and asking an Ollama model questions about dates, events, moods, interaction patterns, topics, and trends.

## What it supports

- `.7z`, `.zip`, folder, or raw `.txt` WhatsApp export input
- Password-protected `.7z` archives
- WhatsApp raw chat parsing by date/sender/message
- Existing `context.txt`, CSV, JSON, and Excel-adjacent analysis files as additional context
- Day-by-day raw view
- Day-by-day Ollama summaries
- Overall relationship/context report
- Local Q&A with evidence retrieval
- Trend charts for message volume, rough mood score, detected topics, and sender counts
- Cache for generated summaries so the same day/report is not regenerated every time

## Install

```bash
cd wa_ollama_chat_analyzer
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## Install and run Ollama

Install Ollama, then pull a model:

```bash
ollama pull llama3.1:8b
ollama serve
```

Other good local models: `qwen2.5:7b`, `mistral:7b`, `llama3.2:3b` for weaker hardware.

## Run the UI

```bash
streamlit run app.py
```

In the sidebar, set:

- archive/folder path: path to your `.7z`, `.zip`, folder, or `chat.txt`
- archive password: your archive password
- Ollama model: for example `llama3.1:8b`
- Ollama host: `http://localhost:11434`

Then click **Load / refresh data**.

## Run from CLI

```bash
python app.py --input "ChatsAnalyser.7z" --password "ananya" --summary
```

Ask one question from terminal:

```bash
python app.py --input "ChatsAnalyser.7z" --password "ananya" --question "What happened around March and what mood pattern is visible?"
```

## Privacy notes

The app is designed for local Ollama use. It does not send chats to cloud AI APIs unless you modify the code. Your parsed workspace and summary cache are stored under:

```text
~/.wa_ollama_chat_analyzer/
```

Delete that folder to clear imported data and generated summaries.
