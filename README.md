# Viva Engage News Curator

A Streamlit dashboard that fetches the latest tech news from RSS feeds and Google News, then uses a free OpenRouter LLM to draft ready-to-paste Viva Engage posts.

## Features
- Pulls from configurable RSS feeds (TechCrunch, The Verge, Hacker News, etc.) and Google News queries
- Deduplicates articles and excludes already-posted ones
- Generates a full Viva Engage post with headline, takeaways, "why it matters", and a discussion question
- Editable post preview before publishing
- Tracks posted/dismissed articles in `posted_history.json`

## Setup

```bash
pip install streamlit feedparser beautifulsoup4 python-dateutil openai requests
streamlit run app.py
```

## Model
Uses `nvidia/nemotron-3.5-lightning:free` via [OpenRouter](https://openrouter.ai) (free tier).
