from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import textwrap
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
import streamlit as st

try:
    import py7zr
except Exception:  # optional until user imports .7z
    py7zr = None

APP_DIR = Path.home() / ".wa_ollama_chat_analyzer"
WORKSPACE = APP_DIR / "workspace"
CACHE_FILE = APP_DIR / "summary_cache.json"
DB_FILE = APP_DIR / "chat_index.sqlite"

WA_PATTERNS = [
    re.compile(
        r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}(?:\s?[APap][Mm])?)\s+-\s+([^:]+):\s?(.*)$"
    ),
    re.compile(
        r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}(?:\s?[APap][Mm])?)\]\s+([^:]+):\s?(.*)$"
    ),
]
DATE_FORMATS = [
    "%d/%m/%y %H:%M",
    "%d/%m/%Y %H:%M",
    "%d/%m/%y %I:%M %p",
    "%d/%m/%Y %I:%M %p",
    "%m/%d/%y %H:%M",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %I:%M %p",
    "%m/%d/%Y %I:%M %p",
]

TOPIC_PATTERNS = {
    "academic/college": r"\b(exam|assignment|lab|viva|attendance|class|notes?|ppt|project|faculty|semester|cgpa|marks?)\b",
    "tech/coding": r"\b(java|python|react|api|backend|frontend|database|sql|github|leetcode|code|bug|error|compiler|colab|azure|supabase|flask|fastapi)\b",
    "help/request": r"\b(help|send|explain|doubt|need|can u|can you|pls|please|where|how|fix|share|link)\b",
    "appreciation/apology": r"\b(thanks|thank you|thanku|thankyou|sorry|great|goat|nice|good)\b",
    "emotional/personal": r"\b(feel|hurt|sad|bad|tired|sick|okay|miss|love|care|cry|anxious|scared|worried|happy)\b",
    "meet/call": r"\b(call|meet|come|free|voice|vm|hangout|connect)\b",
    "casual": r"\b(bro|dude|lol|lmao|wtf|tf|yo|hey|hi|aight)\b",
    "deleted/media": r"\b(deleted this message|this message was deleted|<media omitted>)\b",
}

POS_WORDS = set(
    "thanks thank thankyou thanku good great nice happy haha lol lmao goat okay cool sure aight love care".split()
)
NEG_WORDS = set(
    "sad bad hurt tired sick sorry cry anxious scared worried angry mad problem issue doubt confused".split()
)
STOPWORDS = set(
    "the a an and or is are was were be been to of in on for with it this that i you me my your we they he she bro dude hi hey can u pls please not no yes ok okay".split()
)


@dataclass
class Message:
    dt: datetime | None
    sender: str
    content: str
    raw_time: str


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def parse_dt(d: str, t: str) -> datetime | None:
    raw = f"{d} {t.strip().upper()}"
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_whatsapp_text(text: str) -> list[Message]:
    messages: list[Message] = []
    current: Message | None = None
    for line in text.splitlines():
        matched = None
        for pat in WA_PATTERNS:
            matched = pat.match(line)
            if matched:
                break
        if matched:
            if current:
                messages.append(current)
            d, t, sender, content = matched.groups()
            current = Message(
                parse_dt(d, t), sender.strip(), content.strip(), f"{d} {t}"
            )
        else:
            if current and line.strip():
                current.content += "\n" + line.strip()
    if current:
        messages.append(current)
    return messages


def read_text_file(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="ignore")


def extract_input(input_path: str, password: str = "") -> Path:
    ensure_dirs()
    src = Path(input_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Input path does not exist: {src}")

    extract_dir = WORKSPACE / stable_hash(str(src) + str(src.stat().st_mtime))
    if extract_dir.exists() and any(extract_dir.iterdir()):
        return extract_dir
    extract_dir.mkdir(parents=True, exist_ok=True)

    suffix = src.suffix.lower()
    if src.is_dir():
        dest = extract_dir / src.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    elif suffix == ".zip":
        with zipfile.ZipFile(src) as zf:
            zf.extractall(extract_dir, pwd=password.encode() if password else None)
    elif suffix == ".7z":
        if py7zr is None:
            raise RuntimeError("py7zr is not installed. Run: pip install py7zr")
        with py7zr.SevenZipFile(src, mode="r", password=password or None) as archive:
            archive.extractall(extract_dir)
    elif suffix == ".txt":
        shutil.copy2(src, extract_dir / src.name)
    else:
        raise ValueError("Use a folder, .txt WhatsApp export, .zip, or .7z archive.")
    return extract_dir


def discover_files(root: Path) -> dict[str, list[Path]]:
    all_files = [p for p in root.rglob("*") if p.is_file()]
    txts = [p for p in all_files if p.suffix.lower() == ".txt"]
    csvs = [p for p in all_files if p.suffix.lower() == ".csv"]
    jsons = [p for p in all_files if p.suffix.lower() == ".json"]
    excels = [p for p in all_files if p.suffix.lower() in {".xlsx", ".xls"}]

    chat_txts = []
    context_txts = []
    for p in txts:
        low = p.name.lower()
        sample = read_text_file(p)[:5000]
        if len(parse_whatsapp_text(sample)) >= 2 or " - " in sample and ":" in sample:
            chat_txts.append(p)
        elif "context" in low or "diary" in low or "analysis" in low:
            context_txts.append(p)
        else:
            context_txts.append(p)
    return {
        "chat_txts": chat_txts,
        "context_txts": context_txts,
        "csvs": csvs,
        "jsons": jsons,
        "excels": excels,
        "all": all_files,
    }


def load_everything(
    root: Path, self_name_hint: str = ""
) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    files = discover_files(root)
    all_messages: list[dict[str, Any]] = []
    context_parts: list[str] = []

    for chat_file in files["chat_txts"]:
        text = read_text_file(chat_file)
        for m in parse_whatsapp_text(text):
            if not m.dt:
                continue
            all_messages.append(
                {
                    "datetime": m.dt,
                    "date": m.dt.date().isoformat(),
                    "time": m.dt.strftime("%H:%M"),
                    "sender": m.sender,
                    "content": m.content,
                    "source_file": str(chat_file.relative_to(root)),
                    "word_count": len(re.findall(r"\b\w+\b", m.content)),
                }
            )

    for p in files["context_txts"]:
        context_parts.append(
            f"\n\n--- SOURCE: {p.relative_to(root)} ---\n" + read_text_file(p)
        )

    for p in files["csvs"]:
        try:
            df = pd.read_csv(p)
            context_parts.append(
                f"\n\n--- CSV SOURCE: {p.relative_to(root)} ---\n"
                + df.head(200).to_csv(index=False)
            )
        except Exception as e:
            context_parts.append(
                f"\n\n--- CSV SOURCE: {p.relative_to(root)} could not be read: {e} ---"
            )

    for p in files["jsons"]:
        try:
            obj = json.loads(read_text_file(p))
            context_parts.append(
                f"\n\n--- JSON SOURCE: {p.relative_to(root)} ---\n"
                + json.dumps(obj, indent=2, ensure_ascii=False)[:30000]
            )
        except Exception as e:
            context_parts.append(
                f"\n\n--- JSON SOURCE: {p.relative_to(root)} could not be read: {e} ---"
            )

    df = pd.DataFrame(all_messages)
    if not df.empty:
        df = df.sort_values("datetime").reset_index(drop=True)
    meta = {
        k: [str(x.relative_to(root)) for x in v] for k, v in files.items() if k != "all"
    }
    return df, "\n".join(context_parts).strip(), meta


def detect_topics(text: str) -> list[str]:
    return [
        name
        for name, pattern in TOPIC_PATTERNS.items()
        if re.search(pattern, text, re.I)
    ]


def simple_sentiment(text: str) -> float:
    words = [w.lower() for w in re.findall(r"\b\w+\b", text)]
    if not words:
        return 0.0
    pos = sum(w in POS_WORDS for w in words)
    neg = sum(w in NEG_WORDS for w in words)
    return round((pos - neg) / max(1, math.sqrt(len(words))), 3)


def make_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows = []
    for day, g in df.groupby("date", sort=True):
        text = "\n".join(f"{r.time} {r.sender}: {r.content}" for r in g.itertuples())
        topic_counts = Counter(
            t for msg in g["content"] for t in detect_topics(str(msg))
        )
        sender_counts = g["sender"].value_counts().to_dict()
        top_terms = Counter(
            w.lower()
            for msg in g["content"]
            for w in re.findall(r"\b[a-zA-Z][a-zA-Z]{2,}\b", str(msg))
            if w.lower() not in STOPWORDS
        ).most_common(12)
        rows.append(
            {
                "date": day,
                "messages": len(g),
                "senders": sender_counts,
                "topics": dict(topic_counts),
                "top_terms": dict(top_terms),
                "mood_score": round(
                    sum(simple_sentiment(str(x)) for x in g["content"])
                    / max(1, len(g)),
                    3,
                ),
                "raw": text,
            }
        )
    return pd.DataFrame(rows)


def load_cache() -> dict[str, Any]:
    ensure_dirs()
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict[str, Any]) -> None:
    ensure_dirs()
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def ollama_generate(
    prompt: str,
    model: str,
    host: str = "http://localhost:11434",
    temperature: float = 0.2,
    timeout: int = 180,
) -> str:
    url = host.rstrip("/") + "/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 8192},
    }
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json().get("response", "").strip()


def summarize_day(row: pd.Series, context: str, model: str, host: str) -> str:
    cache = load_cache()
    key = "day:" + stable_hash(row["date"] + row["raw"][:4000] + context[:2000] + model)
    if key in cache:
        return cache[key]
    context_slice = context[:8000]
    prompt = f"""
You are analyzing a private WhatsApp conversation plus saved context. Be grounded, direct, and cautious.
Do not invent motives. Use only the evidence given.

Saved context excerpt:
{context_slice}

Day: {row['date']}
Detected metrics:
- messages: {row['messages']}
- senders: {row['senders']}
- topics: {row['topics']}
- rough mood score: {row['mood_score']}

Raw chat for this day:
{row['raw'][:12000]}

Return this exact format:
1. What happened:
2. Main topics:
3. Mood / tone:
4. Interaction pattern:
5. Notable lines or signals:
6. What is NOT proven:
""".strip()
    out = ollama_generate(prompt, model=model, host=host)
    cache[key] = out
    save_cache(cache)
    return out


def build_context_chunks(
    df: pd.DataFrame, daily: pd.DataFrame, context: str
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not daily.empty:
        for r in daily.itertuples():
            chunks.append(
                {
                    "kind": "day",
                    "date": r.date,
                    "text": f"Date: {r.date}\nTopics: {r.topics}\nTop terms: {r.top_terms}\nRaw:\n{r.raw}",
                }
            )
    if context:
        parts = re.split(r"\n(?=--- SOURCE:|--- CSV SOURCE:|--- JSON SOURCE:)", context)
        for i, part in enumerate(parts):
            for j in range(0, len(part), 3500):
                chunks.append(
                    {"kind": "saved_context", "date": "", "text": part[j : j + 4500]}
                )
    return chunks


def score_chunk(query: str, chunk: dict[str, Any]) -> float:
    q = [w.lower() for w in re.findall(r"\b\w+\b", query) if w.lower() not in STOPWORDS]
    if not q:
        return 0.0
    text = chunk["text"].lower()
    score = 0.0
    for w in q:
        score += text.count(w) * (2.0 if len(w) > 4 else 1.0)
    for date_like in re.findall(
        r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", query
    ):
        if date_like in text:
            score += 20
    return score / max(1, math.log(len(text) + 10))


def retrieve(
    query: str, chunks: list[dict[str, Any]], k: int = 8
) -> list[dict[str, Any]]:
    ranked = [(score_chunk(query, c), c) for c in chunks]
    ranked = [(s, c) for s, c in ranked if s > 0]
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [{**c, "score": round(s, 3)} for s, c in ranked[:k]]


def ask_question(
    query: str,
    df: pd.DataFrame,
    daily: pd.DataFrame,
    context: str,
    model: str,
    host: str,
) -> tuple[str, list[dict[str, Any]]]:
    chunks = build_context_chunks(df, daily, context)
    found = retrieve(query, chunks, k=10)
    evidence = "\n\n".join(
        f"--- Evidence {i+1} | {c['kind']} | {c.get('date','')} | score {c['score']} ---\n{c['text'][:5000]}"
        for i, c in enumerate(found)
    )
    prompt = f"""
You are a local private chat analyst. Answer the user's question using only the evidence below.
Be useful but cautious. Separate observed facts from interpretations. If asked about feelings, attraction, intent, or mood, say what is suggested, not certain.

User question:
{query}

Evidence:
{evidence if evidence else 'No relevant evidence retrieved.'}

Answer with:
- Direct answer
- Evidence by date / source
- Pattern interpretation
- Caveats / what is not proven
""".strip()
    return ollama_generate(prompt, model=model, host=host), found


def make_overall_report(
    df: pd.DataFrame, daily: pd.DataFrame, context: str, model: str, host: str
) -> str:
    cache = load_cache()
    key = "overall:" + stable_hash(
        str(len(df))
        + str(daily["date"].tolist() if not daily.empty else [])
        + context[:5000]
        + model
    )
    if key in cache:
        return cache[key]
    sender_counts = df["sender"].value_counts().to_dict() if not df.empty else {}
    topic_total = Counter()
    if not daily.empty:
        for d in daily["topics"]:
            topic_total.update(d)
    timeline = (
        daily[["date", "messages", "topics", "mood_score"]].to_dict("records")
        if not daily.empty
        else []
    )
    prompt = f"""
Analyze this WhatsApp + saved-context dataset.
Do not diagnose. Do not claim certainty about romantic interest. Keep it grounded.

Dataset stats:
- date range: {df['date'].min() if not df.empty else 'none'} to {df['date'].max() if not df.empty else 'none'}
- sender counts: {sender_counts}
- topic totals: {dict(topic_total)}
- timeline sample: {json.dumps(timeline[:120], ensure_ascii=False)}

Saved context excerpt:
{context[:12000]}

Return sections:
1. Overall dynamic
2. Timeline phases
3. Recurring topics
4. Mood/tone trends
5. Interaction patterns
6. Possible asymmetries
7. Strong evidence
8. Weak/uncertain evidence
9. Questions worth asking the bot next
""".strip()
    out = ollama_generate(prompt, model=model, host=host)
    cache[key] = out
    save_cache(cache)
    return out


def export_daily_csv(daily: pd.DataFrame) -> Path:
    out = APP_DIR / "daily_summary_index.csv"
    export = daily.drop(columns=["raw"], errors="ignore") if not daily.empty else daily
    export.to_csv(out, index=False)
    return out


def render_ui() -> None:
    st.set_page_config(page_title="WA + Context Ollama Analyzer", layout="wide")
    ensure_dirs()
    st.title("WhatsApp + Saved Context Analyzer")
    st.caption(
        "Local-first browser UI for daily summaries, raw context, patterns, and Ollama-powered Q&A."
    )

    with st.sidebar:
        st.header("Import")
        input_path = st.text_input(
            "Archive / folder / chat.txt path",
            value=(
                "/mnt/data/ChatsAnalyser.7z"
                if Path("/mnt/data/ChatsAnalyser.7z").exists()
                else ""
            ),
        )
        password = st.text_input(
            "Archive password",
            value="",
            type="password",
            help="For your uploaded archive, use the password you mentioned.",
        )
        model = st.text_input("Ollama model", value="llama3.1:8b")
        host = st.text_input("Ollama host", value="http://localhost:11434")
        import_btn = st.button("Load / refresh data", type="primary")

    if import_btn:
        try:
            root = extract_input(input_path, password=password)
            df, context, meta = load_everything(root)
            st.session_state["root"] = str(root)
            st.session_state["df"] = df
            st.session_state["context"] = context
            st.session_state["meta"] = meta
            st.session_state["daily"] = make_daily(df)
            st.success(
                f"Loaded {len(df)} messages and {len(context):,} characters of saved context."
            )
        except Exception as e:
            st.error(str(e))

    df = st.session_state.get("df", pd.DataFrame())
    context = st.session_state.get("context", "")
    daily = st.session_state.get("daily", pd.DataFrame())
    meta = st.session_state.get("meta", {})

    if df.empty and not context:
        st.info(
            "Load your .7z/.zip/folder first. Make sure Ollama is running before generating summaries or asking questions."
        )
        st.code(
            "ollama pull llama3.1:8b\nollama serve\nstreamlit run app.py",
            language="bash",
        )
        return

    tab_overview, tab_day, tab_qa, tab_patterns, tab_raw, tab_files = st.tabs(
        ["Overview", "Day view", "Ask bot", "Trends/patterns", "Raw context", "Files"]
    )

    with tab_overview:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Messages", len(df))
        c2.metric("Days", len(daily))
        c3.metric("Saved context chars", f"{len(context):,}")
        c4.metric("Senders", df["sender"].nunique() if not df.empty else 0)
        if not df.empty:
            st.write("Date range:", df["date"].min(), "→", df["date"].max())
            st.bar_chart(df["sender"].value_counts())
        if st.button("Generate overall Ollama report"):
            try:
                st.markdown(
                    make_overall_report(df, daily, context, model=model, host=host)
                )
            except Exception as e:
                st.error(f"Ollama error: {e}")

    with tab_day:
        if daily.empty:
            st.warning("No daily rows found.")
        else:
            selected = st.selectbox(
                "Pick a date", daily["date"].tolist(), index=len(daily) - 1
            )
            row = daily[daily["date"] == selected].iloc[0]
            c1, c2, c3 = st.columns(3)
            c1.metric("Messages", int(row["messages"]))
            c2.metric("Mood score", row["mood_score"])
            c3.markdown(f"**Topics**  \n{row['topics']}")
            st.subheader("Raw WhatsApp for the day")
            st.text_area("", value=row["raw"], height=320)
            if st.button("Summarize this day with Ollama"):
                try:
                    st.markdown(summarize_day(row, context, model=model, host=host))
                except Exception as e:
                    st.error(f"Ollama error: {e}")

    with tab_qa:
        q = st.text_area(
            "Ask about dates, events, moods, interactions, trends, or context",
            height=100,
            placeholder="Example: What changed around March? Was the interaction mostly help-based or emotional?",
        )
        if st.button("Ask local bot", type="primary") and q.strip():
            try:
                answer, evidence = ask_question(
                    q, df, daily, context, model=model, host=host
                )
                st.markdown(answer)
                with st.expander("Retrieved evidence"):
                    for item in evidence:
                        st.write(
                            f"{item['kind']} | {item.get('date','')} | score {item['score']}"
                        )
                        st.text(item["text"][:3000])
            except Exception as e:
                st.error(f"Ollama error: {e}")

    with tab_patterns:
        if not daily.empty:
            chart_df = daily.copy()
            chart_df["date"] = pd.to_datetime(chart_df["date"])
            st.subheader("Messages per day")
            st.line_chart(chart_df.set_index("date")["messages"])
            st.subheader("Rough mood score per day")
            st.line_chart(chart_df.set_index("date")["mood_score"])
            topic_total = Counter()
            for d in daily["topics"]:
                topic_total.update(d)
            st.subheader("Detected topics")
            st.bar_chart(pd.Series(dict(topic_total)).sort_values(ascending=False))
            st.subheader("Daily table")
            st.dataframe(
                daily.drop(columns=["raw"], errors="ignore"), use_container_width=True
            )
            out = export_daily_csv(daily)
            st.download_button(
                "Download daily index CSV",
                data=out.read_bytes(),
                file_name="daily_summary_index.csv",
            )

    with tab_raw:
        st.subheader("Saved context/raw prior analysis")
        st.text_area("Context", value=context[:250000], height=500)
        if len(context) > 250000:
            st.caption("Showing first 250k characters only in the browser.")

    with tab_files:
        st.json(meta)


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Local WhatsApp + context analyzer with Ollama."
    )
    parser.add_argument(
        "--input", required=False, help="Path to .7z/.zip/folder/chat.txt"
    )
    parser.add_argument("--password", default="", help="Archive password")
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--question", default="", help="Ask one question from CLI")
    parser.add_argument(
        "--summary", action="store_true", help="Generate overall report from CLI"
    )
    parser.add_argument("--run-ui", action="store_true", help="Launch Streamlit UI")
    args = parser.parse_args()

    if args.run_ui:
        subprocess.run(["streamlit", "run", str(Path(__file__).resolve())], check=False)
        return

    if not args.input:
        render_ui()
        return

    root = extract_input(args.input, password=args.password)
    df, context, meta = load_everything(root)
    daily = make_daily(df)
    print(f"Loaded {len(df)} messages, {len(daily)} days, {len(context)} context chars")
    print("Files:", json.dumps(meta, indent=2))

    if args.summary:
        print(make_overall_report(df, daily, context, args.model, args.host))
    if args.question:
        answer, evidence = ask_question(
            args.question, df, daily, context, args.model, args.host
        )
        print(answer)
        print("\nRetrieved evidence:")
        for e in evidence:
            print(f"- {e['kind']} {e.get('date','')} score={e['score']}")


if __name__ == "__main__":
    cli()
