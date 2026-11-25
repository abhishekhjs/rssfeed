import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

import feedparser
import streamlit as st
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from openai import OpenAI

# --- CREDENTIALS & FILES ---
OPENROUTER_API_KEY = ""  # Set your key in the sidebar or as env var OPENROUTER_API_KEY
# Best free model on OpenRouter as of Nov 2026 — fast, reliable JSON output
FREE_MODEL = "nvidia/nemotron-3.5-lightning:free"

CONFIG_FILE = Path("curator_config.json")
HISTORY_FILE = Path("posted_history.json")

# --- PAGE CONFIG (must be first Streamlit call) ---
st.set_page_config(
    page_title="Viva Engage News Curator",
    page_icon="📢",
    layout="wide",
)

DEFAULT_CONFIG = {
    "api_key": OPENROUTER_API_KEY,
    "model": FREE_MODEL,
    "office_context": (
        "Enterprise technology and operations team interested in practical AI adoption, "
        "Microsoft 365 workflows, cybersecurity, and major industry shifts."
    ),
    "direct_feeds": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.feedburner.com/TheHackersNews",
        "https://news.ycombinator.com/rss",
    ],
    "gnews_queries": [
        "enterprise AI productivity",
        "Microsoft 365 Copilot updates",
    ],
}


# ── STORAGE ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            merged = {**DEFAULT_CONFIG, **saved}
            if not merged.get("api_key"):
                merged["api_key"] = OPENROUTER_API_KEY
            return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_history() -> set:
    if HISTORY_FILE.exists():
        try:
            return set(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_history(seen: set) -> None:
    HISTORY_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


# ── FEED HELPERS ───────────────────────────────────────────────────────────────

def clean_html(raw_html: str, max_len: int = 300) -> str:
    if not raw_html:
        return ""
    text = BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[: max_len - 3] + "…") if len(text) > max_len else text


def parse_utc(entry) -> datetime:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return datetime.now(timezone.utc)
    dt = date_parser.parse(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_age(dt_utc: datetime) -> str:
    delta = datetime.now(timezone.utc) - dt_utc
    h = int(delta.total_seconds() // 3600)
    if h < 1:
        return f"{max(1, int(delta.total_seconds() // 60))}m ago"
    if h < 24:
        return f"{h}h ago"
    return f"{h // 24}d ago"


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_article_text(url: str, max_len: int = 1800) -> str:
    """Best-effort extraction of readable article paragraphs from a URL."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                return ""
            html = resp.read(300_000).decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        container = soup.find("article") or soup.find("main") or soup.body
        if not container:
            return ""
        paras = [
            re.sub(r"\s+", " ", p.get_text(" ", strip=True))
            for p in container.find_all("p")
        ]
        body = "\n\n".join(p for p in paras if len(p) > 40)
        return (body[: max_len - 3] + "…") if len(body) > max_len else body
    except Exception:
        return ""


def fetch_candidates(feed_urls: list[str], gnews_queries: list[str]) -> list[dict]:
    seen_history = load_history()
    raw_items: list[dict] = []
    errors: list[str] = []

    for url in feed_urls:
        url = url.strip()
        if not url:
            continue
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                errors.append(f"⚠️ Could not parse feed: {url}")
                continue
            feed_title = parsed.feed.get("title", url)
            for entry in parsed.entries:
                link = entry.get("link", "").strip()
                guid = entry.get("id", link).strip()
                if link:
                    raw_body = (entry.get("content") or [{}])[0].get("value") or entry.get("summary", "")
                    raw_items.append({
                        "id": guid,
                        "title": entry.get("title", "Untitled").strip(),
                        "source": feed_title,
                        "url": link,
                        "published_utc": parse_utc(entry),
                        "summary": clean_html(entry.get("summary", "")),
                        "content": clean_html(raw_body, max_len=1800),
                    })
        except Exception as e:
            errors.append(f"⚠️ Error fetching {url}: {e}")

    for query in gnews_queries:
        query = query.strip()
        if not query:
            continue
        try:
            encoded_q = quote_plus(f"{query} when:1d")
            gnews_url = (
                f"https://news.google.com/rss/search?q={encoded_q}"
                f"&hl=en-IN&gl=IN&ceid=IN:en"
            )
            parsed = feedparser.parse(gnews_url)
            for entry in parsed.entries:
                raw_title = entry.get("title", "Untitled").strip()
                if " - " in raw_title:
                    title, source = raw_title.rsplit(" - ", 1)
                else:
                    title, source = raw_title, "Google News"
                link = entry.get("link", "").strip()
                guid = entry.get("id", link).strip()
                if link:
                    raw_items.append({
                        "id": guid,
                        "title": title.strip(),
                        "source": source.strip(),
                        "url": link,
                        "published_utc": parse_utc(entry),
                        "summary": clean_html(entry.get("summary", "")),
                        "content": clean_html(entry.get("summary", ""), max_len=1800),
                    })
        except Exception as e:
            errors.append(f"⚠️ Google News error for '{query}': {e}")

    # Deduplicate by URL and normalised title, exclude already-posted
    unique: list[dict] = []
    seen_keys: set[str] = set()
    for item in sorted(raw_items, key=lambda x: x["published_utc"], reverse=True):
        norm = re.sub(r"\W+", "", item["title"].lower())
        if (
            item["id"] in seen_history
            or item["url"] in seen_history
            or item["url"] in seen_keys
            or norm in seen_keys
        ):
            continue
        seen_keys.update([item["url"], norm])
        item["age"] = format_age(item["published_utc"])
        unique.append(item)
        if len(unique) == 20:
            break

    return unique, errors


# ── LLM HELPER ─────────────────────────────────────────────────────────────────

def call_openrouter_json(api_key: str, model: str, prompt: str, temperature: float = 0.3) -> dict:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "http://localhost:8501",
            "X-Title": "Viva-Engage-News-Curator",
        },
    )
    last_err = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            raw_text = resp.choices[0].message.content.strip()
            # Extract first JSON object (non-greedy, dotall)
            match = re.search(r"\{.*?\}", raw_text, flags=re.DOTALL)
            # Non-greedy can be too tight for nested JSON; fall back to greedy if needed
            if not match or not match.group(0).count('"'):
                match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
            if match:
                raw_text = match.group(0)
            return json.loads(raw_text)
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"OpenRouter request failed after 3 attempts: {last_err}")


def build_prompt(items: list[dict], office_context: str) -> str:
    articles_block = "\n\n".join(
        f"Article {i}:\n"
        f"  Title: {item['title']}\n"
        f"  Source: {item['source']}\n"
        f"  URL: {item['url']}\n"
        f"  Summary: {item.get('summary', '(no summary)')}"
        for i, item in enumerate(items, 1)
    )
    merge_note = (
        "Synthesize all selected articles below into ONE cohesive Viva Engage digest post, "
        "connecting common themes and including all source links.\n\n"
        if len(items) > 1
        else ""
    )
    return f"""You are an enterprise communications specialist curating tech news for a Microsoft Viva Engage community.

Audience: {office_context}

{merge_note}{articles_block}

Return ONLY a valid JSON object with these exact keys (no extra text before or after):
{{
  "headline": "catchy headline with 1 emoji",
  "takeaways": ["point 1", "point 2", "point 3"],
  "why_it_matters": "1-2 sentences linking this to our team",
  "discussion_question": "one engaging question for employees",
  "formatted_post": "full ready-to-paste Viva Engage post in markdown (including all article URLs)"
}}"""


def build_post_from_parts(res: dict, items: list[dict]) -> str:
    """Fallback: stitch a post from individual fields if formatted_post is missing."""
    headline = res.get("headline", items[0]["title"])
    takeaways = res.get("takeaways", [])
    why = res.get("why_it_matters", "")
    question = res.get("discussion_question", "")
    bullets = "\n".join(f"• {t}" for t in takeaways)
    links = "\n".join(f"🔗 [{item['title']}]({item['url']})" for item in items)
    return (
        f"{headline}\n\n"
        f"**Key Takeaways:**\n{bullets}\n\n"
        f"**Why It Matters:**\n{why}\n\n"
        f"💬 {question}\n\n"
        f"{links}"
    )


def archive_selected(items: list[dict], selected_indices: list[int]) -> None:
    """Record selected articles in posted_history.json and remove them from candidates."""
    h = load_history()
    for art in items:
        h.update([art["id"], art["url"]])
    save_history(h)
    selected_set = set(selected_indices)
    st.session_state.candidates = [
        c for i, c in enumerate(st.session_state.candidates) if i not in selected_set
    ]
    st.session_state.selected_indices = []
    st.session_state.active_idx = None
    st.session_state.curated_post = ""
    st.session_state.ai_parts = {}


# ── STREAMLIT UI ───────────────────────────────────────────────────────────────

def main():
    # ── State init
    for key, default in [
        ("candidates", []),
        ("fetch_errors", []),
        ("selected_indices", []),
        ("active_idx", None),
        ("curated_post", ""),
        ("ai_parts", {}),
        ("last_fetched", None),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    cfg = load_config()
    history = load_history()

    # ── Header
    st.title("📢 Viva Engage News Curator")
    st.caption("Fetch the latest tech news, generate AI-drafted Viva Engage posts, and track what you've shared.")

    # ── Sidebar
    with st.sidebar:
        st.header("⚙️ Settings")

        api_key = st.text_input(
            "OpenRouter API Key",
            value=cfg.get("api_key", ""),
            type="password",
            help="Get a free key at openrouter.ai",
        )
        model = st.text_input(
            "Model",
            value=cfg.get("model", FREE_MODEL),
            help=f"Default: {FREE_MODEL} (free tier)",
        )
        office_context = st.text_area(
            "Audience / Office Context",
            value=cfg.get("office_context", ""),
            height=110,
            help="Describe your team's interests to personalise the AI commentary.",
        )

        with st.expander("📡 Feed Sources", expanded=False):
            feeds_text = st.text_area(
                "Direct RSS Feeds (one per line)",
                value="\n".join(cfg.get("direct_feeds", [])),
                height=110,
            )
            gnews_text = st.text_area(
                "Google News Queries (one per line)",
                value="\n".join(cfg.get("gnews_queries", [])),
                height=80,
            )

        if st.button("💾 Save Settings", use_container_width=True):
            cfg["api_key"] = api_key.strip() or OPENROUTER_API_KEY
            cfg["model"] = model.strip() or FREE_MODEL
            cfg["office_context"] = office_context.strip()
            cfg["direct_feeds"] = [f.strip() for f in feeds_text.splitlines() if f.strip()]
            cfg["gnews_queries"] = [q.strip() for q in gnews_text.splitlines() if q.strip()]
            save_config(cfg)
            st.success("Settings saved!")

        st.divider()
        st.metric("Posted history", len(history), help="Articles marked as posted or dismissed")
        if history and st.button("🗑️ Clear History", use_container_width=True):
            save_history(set())
            st.success("History cleared!")
            st.rerun()

    # ── Step 1: Fetch
    st.subheader("Step 1 — Fetch Latest News")
    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        fetch_clicked = st.button("🔄 Fetch News", type="primary", use_container_width=True)

    if fetch_clicked:
        with st.spinner("Fetching RSS feeds & Google News…"):
            feed_urls = [f.strip() for f in feeds_text.splitlines() if f.strip()]
            gnews_queries = [q.strip() for q in gnews_text.splitlines() if q.strip()]
            candidates, errors = fetch_candidates(feed_urls, gnews_queries)
        st.session_state.candidates = candidates
        st.session_state.fetch_errors = errors
        st.session_state.selected_indices = []
        st.session_state.active_idx = None
        st.session_state.curated_post = ""
        st.session_state.ai_parts = {}
        st.session_state.last_fetched = datetime.now().strftime("%H:%M:%S")

    with col_info:
        if st.session_state.last_fetched:
            n = len(st.session_state.candidates)
            st.info(f"Last fetched at **{st.session_state.last_fetched}** — **{n}** new articles ready.")

    for err in st.session_state.fetch_errors:
        st.warning(err)

    candidates = st.session_state.candidates
    if not candidates:
        st.info("👆 Click **Fetch News** to pull articles from your configured feeds.")
        return

    # ── Step 2: Article cards (multi-select)
    st.divider()
    selected_indices = [i for i in st.session_state.selected_indices if i < len(candidates)]
    st.session_state.selected_indices = selected_indices

    hdr_col, clr_col = st.columns([4, 1])
    with hdr_col:
        st.subheader(f"Step 2 — Pick Articles ({len(selected_indices)} selected / {len(candidates)} available)")
    with clr_col:
        if selected_indices and st.button("Clear selection", use_container_width=True):
            st.session_state.selected_indices = []
            st.session_state.active_idx = None
            st.session_state.curated_post = ""
            st.session_state.ai_parts = {}
            st.rerun()

    # Inject CSS for card styling
    st.markdown("""
    <style>
    div[data-testid="stButton"] > button {
        white-space: normal !important;
        height: auto !important;
    }
    .article-card {
        border: 1.5px solid #e0e0e0;
        border-radius: 10px;
        padding: 14px 16px;
        margin-bottom: 4px;
        background: #fafafa;
        transition: border-color 0.15s;
    }
    .article-card.selected {
        border-color: #ff4b4b;
        background: #fff5f5;
    }
    .card-source { font-size: 11px; color: #888; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .card-title  { font-size: 15px; font-weight: 600; color: #111; margin: 4px 0 6px; line-height: 1.4; }
    .card-meta   { font-size: 12px; color: #aaa; }
    </style>
    """, unsafe_allow_html=True)

    COLS = 3  # cards per row
    for row_start in range(0, len(candidates), COLS):
        row_items = candidates[row_start:row_start + COLS]
        cols = st.columns(COLS)
        for offset, (col, art) in enumerate(zip(cols, row_items)):
            idx = row_start + offset
            is_selected = idx in selected_indices
            card_class = "article-card selected" if is_selected else "article-card"
            with col:
                st.markdown(
                    f"""<div class="{card_class}">
                        <div class="card-source">{art['source']}</div>
                        <div class="card-title">{art['title']}</div>
                        <div class="card-meta">🕐 {art['age']}</div>
                    </div>""",
                    unsafe_allow_html=True,
                )
                btn_label = "✅ Selected" if is_selected else "Select"
                if col.button(btn_label, key=f"card_{idx}", use_container_width=True):
                    if is_selected:
                        st.session_state.selected_indices.remove(idx)
                        if st.session_state.active_idx == idx:
                            sel = st.session_state.selected_indices
                            st.session_state.active_idx = sel[-1] if sel else None
                    else:
                        st.session_state.selected_indices.append(idx)
                        st.session_state.active_idx = idx
                    st.session_state.curated_post = ""
                    st.session_state.ai_parts = {}
                    st.rerun()

        active_idx = st.session_state.active_idx
        if active_idx is not None and row_start <= active_idx < row_start + len(row_items):
            active_art = candidates[active_idx]
            with st.container(border=True):
                h_col, link_col = st.columns([4, 1])
                with h_col:
                    st.markdown(f"#### {active_art['title']}")
                    st.caption(f"{active_art['source']} · {active_art['age']}")
                with link_col:
                    st.markdown(f"[Open article ↗]({active_art['url']})")
                body = active_art.get("content") or active_art.get("summary") or ""
                if len(body) < 200 or body.strip().lower() == "comments":
                    fetched = fetch_article_text(active_art["url"])
                    if fetched:
                        body = fetched
                st.write(body or "No preview content available for this link — click Open article above.")

    selected_items = [candidates[i] for i in st.session_state.selected_indices]
    if not selected_items:
        st.info("👆 Select one or more article cards above to generate a post.")
        return

    # Inline preview of selected articles
    st.divider()
    st.markdown(f"**📰 Selected Articles ({len(selected_items)}):**")
    for i, sel in enumerate(selected_items, 1):
        with st.container():
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**{i}. {sel['title']}**")
                if sel.get("summary"):
                    st.caption(sel["summary"])
            with c2:
                st.markdown(f"**{sel['source']}** · {sel['age']} · [Open ↗]({sel['url']})")

    # ── Step 3: Generate post
    st.divider()
    st.subheader("Step 3 — Generate Viva Engage Post")

    btn_text = f"✨ Merge {len(selected_items)} Articles & Generate" if len(selected_items) > 1 else "✨ Generate with AI"
    gen_col, _ = st.columns([1, 3])
    with gen_col:
        generate_clicked = st.button(btn_text, type="primary", use_container_width=True)

    if generate_clicked:
        api_key_to_use = cfg.get("api_key") or OPENROUTER_API_KEY
        model_to_use = cfg.get("model") or FREE_MODEL
        if not api_key_to_use:
            st.error("No API key set. Please enter your OpenRouter API key in the sidebar.")
        else:
            with st.spinner(f"Calling {model_to_use} via OpenRouter…"):
                try:
                    prompt = build_prompt(selected_items, cfg.get("office_context", ""))
                    res = call_openrouter_json(api_key_to_use, model_to_use, prompt)
                    post = res.get("formatted_post") or build_post_from_parts(res, selected_items)
                    st.session_state.curated_post = post
                    st.session_state.ai_parts = res
                    st.success("✅ Post generated!")
                except Exception as e:
                    st.error(f"AI generation failed: {e}")

    # Show AI breakdown if available
    parts = st.session_state.ai_parts
    if parts:
        with st.expander("🔍 AI Analysis Breakdown", expanded=False):
            if parts.get("headline"):
                st.markdown(f"**Headline:** {parts['headline']}")
            if parts.get("takeaways"):
                st.markdown("**Key Takeaways:**")
                for t in parts["takeaways"]:
                    st.markdown(f"- {t}")
            if parts.get("why_it_matters"):
                st.markdown(f"**Why It Matters:** {parts['why_it_matters']}")
            if parts.get("discussion_question"):
                st.markdown(f"**Discussion Question:** {parts['discussion_question']}")

    # ── Step 4: Edit & publish
    if st.session_state.curated_post:
        st.divider()
        st.subheader("Step 4 — Review, Edit & Publish")

        edited_post = st.text_area(
            "Edit your post before publishing:",
            value=st.session_state.curated_post,
            height=280,
            key="post_editor",
        )
        st.session_state.curated_post = edited_post

        # Preview
        with st.expander("👁️ Preview (rendered)", expanded=False):
            st.markdown(edited_post)

        # Copy helper
        st.caption("💡 Select all text in the box above and copy it into Viva Engage.")

        c_post, c_dismiss, c_regen = st.columns(3)
        with c_post:
            if st.button("✅ Mark as Posted", use_container_width=True, type="primary"):
                archive_selected(selected_items, st.session_state.selected_indices)
                st.success("Marked as posted! Removed from queue.")
                st.rerun()

        with c_dismiss:
            if st.button("⏭️ Dismiss Selected", use_container_width=True):
                archive_selected(selected_items, st.session_state.selected_indices)
                st.rerun()

        with c_regen:
            if st.button("🔁 Regenerate Post", use_container_width=True):
                st.session_state.curated_post = ""
                st.session_state.ai_parts = {}
                st.rerun()






if __name__ == "__main__":
    main()
