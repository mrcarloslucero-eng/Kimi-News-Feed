#!/usr/bin/env python3
"""
AI News Automation Pipeline - GitHub Actions Compatible
"""

import feedparser
import requests
import hashlib
import re
import smtplib
import os
from dotenv import load_dotenv
load_dotenv()  # This loads variables from .env file into os.environ
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# CONFIGURATION
# =============================================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "sk-ant-your-anthropic-key-here")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-6")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "your.email@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "your-app-password-16-chars")
EMAIL_TO = os.environ.get("EMAIL_TO", "your.email@gmail.com")

HOURS_BACK = int(os.environ.get("HOURS_BACK", "24"))
MAX_STORIES_PER_CATEGORY = int(os.environ.get("MAX_STORIES_PER_CATEGORY", "3"))
REQUEST_TIMEOUT = 30
CACHE_FILE = os.environ.get("CACHE_FILE", "story_cache.json")

ENABLE_EDITOR = os.environ.get("ENABLE_EDITOR", "1") == "1"
ENABLE_RESEARCHER = os.environ.get("ENABLE_RESEARCHER", "1") == "1"
ENABLE_CRITIC = os.environ.get("ENABLE_CRITIC", "1") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

FEEDS = {
    "datacenter": [
        "https://www.datacenterknowledge.com/feed",
        "https://www.theregister.com/data_centre/headlines.atom",
    ],
    "regulation": [
        "https://www.politico.com/rss/technology.xml",
        "https://www.euractiv.com/section/artificial-intelligence/feed/",
    ],
    "jobs": [
        "https://www.vox.com/recode/rss.xml",
        "https://www.theguardian.com/technology/artificialintelligence/rss",
    ],
    "general": [
        "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
        "https://arstechnica.com/tag/ai/feed/",
        "https://hnrss.org/newest?q=AI",
        "https://www.anthropic.com/news/rss.xml",
        "https://openai.com/news/rss.xml",
    ],
}


def get_requests_session():
    """Create a requests session with retries and backoff, and a legitimate User-Agent."""
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[403, 429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Spoof a standard browser User-Agen to prevent basic bot blocking
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    return session


REQUESTS_SESSION = get_requests_session()


def fetch_feed(url):
    try:
        logger.info(f"  Fetching: {url[:60]}...")
        response = REQUESTS_SESSION.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        return feed.entries
    except requests.exceptions.RequestException as e:
        logger.error(f"  ERROR fetching feed {url}: {e}")
        return []
    except Exception as e:
        logger.error(f"  ERROR parsing feed {url}: {e}")
        return []


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def get_story_hash(title, link):
    content = title.lower().strip() + "|" + urlparse(link).netloc
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def is_recent(entry, hours=HOURS_BACK):
    published = entry.get('published_parsed') or entry.get('updated_parsed')
    if not published:
        return True
    pub_time = datetime(*published[:6], tzinfo=timezone.utc)
    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    return pub_time > cutoff_time


def categorize_story(title, summary):
    text = (title + " " + summary).lower()
    keywords = {
        "datacenter": [
            "datacenter", "data center", "gpu", "server", "infrastructure",
            "facility", "power", "cooling", "nvidia", "cluster", "compute"
        ],
        "regulation": [
            "regulation", "regulatory", "law", "policy", "government",
            "eu", "congress", "senate", "bill", "act", "legal", "sue", "lawsuit",
            "fine", "antitrust", "oversight"
        ],
        "jobs": [
            "job", "employment", "layoff", "layoffs", "firing", "hiring",
            "workforce", "labor", "career", "worker", "replace", "unemployment",
            "wage", "union", "strike"
        ],
    }
    for category, words in keywords.items():
        if any(word in text for word in words):
            return category
    return "general"


def deduplicate_stories(stories):
    seen_hashes = set()
    unique_stories = []
    for story in stories:
        story_hash = story['hash']
        if story_hash not in seen_hashes:
            seen_hashes.add(story_hash)
            unique_stories.append(story)
    logger.info(f"  Deduplication: {len(stories)} raw -> {len(unique_stories)} unique")
    return unique_stories


def summarize_with_ai(title, content, category):
    content = content[:3500]
    prompt = ("You are a scriptwriter for an AI news YouTube channel aimed at smart everyday people who are NOT tech experts.\n\n"
              f"STORY CATEGORY: {category.upper()}\n"
              f"TITLE: {title}\n"
              f"SOURCE CONTENT: {content}\n\n"
              "Write a 60-90 second script segment with EXACTLY these sections:\n\n"
              "**HOOK:** One punchy sentence that makes a normal person care about this story. No jargon.\n\n"
              "**THE DRAMA:** What happened, in plain English. If there is conflict, explain both sides simply.\n\n"
              "**WHY IT MATTERS:** Real-world impact on money, privacy, jobs, or daily life. Be specific.\n\n"
              "**THE ANGLE:** What makes this story interesting, controversial, or different from the usual hype?\n\n"
              "RULES:\n"
              "- Conversational, punchy, no corporate speak\n"
              "- Write as if you are talking to a friend at a coffee shop\n"
              "- If numbers are mentioned, put them in context\n"
              "- Avoid phrases like 'In the ever-evolving landscape of AI...'\n"
              "- End with a subtle question or forward-looking statement")

    if AI_PROVIDER == "openai":
        return call_openai_api(prompt)
    else:
        return call_anthropic_api(prompt)


def call_openai_api(prompt):
    headers = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 600
    }
    try:
        response = REQUESTS_SESSION.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except requests.exceptions.RequestException as e:
        return "[ERROR: API request failed - " + str(e) + "]"
    except (KeyError, IndexError) as e:
        return "[ERROR: Unexpected API response format - " + str(e) + "]"


def call_anthropic_api(prompt):
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    payload = {
        "model": AI_MODEL,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        response = REQUESTS_SESSION.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()['content'][0]['text']
    except requests.exceptions.RequestException as e:
        return "[ERROR: API request failed - " + str(e) + "]"
    except (KeyError, IndexError) as e:
        return "[ERROR: Unexpected API response format - " + str(e) + "]"


def select_top_stories_with_ai(stories, category, max_n):
    """Editor agent: score all candidates and pick the best max_n for the brief."""
    if len(stories) <= max_n:
        return stories

    candidates = "\n".join(
        f"{i+1}. {s['title']} — {s['raw_summary'][:200]}"
        for i, s in enumerate(stories)
    )
    prompt = ("You are the editor of a daily AI news briefing for smart everyday people who are NOT tech experts.\n\n"
              f"CATEGORY: {category.upper()}\n\n"
              f"Here are {len(stories)} candidate stories from the last 24 hours:\n\n{candidates}\n\n"
              f"Pick the {max_n} most newsworthy stories for a general audience. Prioritize real-world impact "
              "(money, privacy, jobs, daily life), genuine novelty, and conflict or controversy. "
              "Avoid picking multiple stories about the same event.\n\n"
              "Reply with ONLY a JSON object like: {\"picks\": [3, 1, 7]}")

    raw = call_anthropic_api(prompt) if AI_PROVIDER != "openai" else call_openai_api(prompt)
    picks = []
    match = re.search(r'"picks"\s*:\s*\[([^\]]*)\]', raw)
    if match:
        for num in re.findall(r'\d+', match.group(1)):
            i = int(num) - 1
            if 0 <= i < len(stories) and i not in picks:
                picks.append(i)
    if not picks:
        logger.warning(f"  Editor agent returned unparseable picks for {category}, using first {max_n}")
        return stories[:max_n]
    logger.info(f"  Editor agent picked {len(picks)}/{len(stories)} for {category}")
    return [stories[i] for i in picks[:max_n]]


def fetch_full_text(url):
    """Researcher agent: pull the full article body, falling back to the RSS snippet."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded)
            if text and len(text) > 400:
                return text[:8000]
    except Exception as e:
        logger.warning(f"  Could not fetch full text for {url[:60]}: {e}")
    return ""


def critique_with_ai(title, content, summary, category):
    """Critic agent: tighten the summary, cut the cheese, verify against the source.
    Returns the revised summary, or "SKIP" if the story is too thin to include."""
    prompt = ("You are a ruthless editor reviewing a segment for a daily AI news briefing.\n\n"
              f"CATEGORY: {category.upper()}\n"
              f"TITLE: {title}\n"
              f"SOURCE CONTENT: {content[:3500]}\n\n"
              f"DRAFT SEGMENT:\n{summary}\n\n"
              "Review the draft against the source. Fix anything the source does not support, "
              "remove cheesy or hypey phrasing, and tighten the writing while keeping the "
              "HOOK / THE DRAMA / WHY IT MATTERS / THE ANGLE section format.\n\n"
              "If the underlying story is too thin or boring to be worth including, reply with exactly: SKIP\n"
              "Otherwise reply with ONLY the revised segment.")
    return call_anthropic_api(prompt) if AI_PROVIDER != "openai" else call_openai_api(prompt)


def build_html_email(stories_by_category):
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    html_content = ("<!DOCTYPE html>\n<html>\n<head>\n"
            "    <style>\n"
            "        body { font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 700px; margin: 0 auto; padding: 20px; }\n"
            "        h1 { color: #1a1a1a; border-bottom: 3px solid #6366f1; padding-bottom: 10px; }\n"
            "        h2 { color: #4f46e5; margin-top: 30px; text-transform: uppercase; font-size: 14px; letter-spacing: 1px; }\n"
            "        h3 { margin-bottom: 5px; font-size: 18px; }\n"
            "        h3 a { color: #1a1a1a; text-decoration: none; }\n"
            "        h3 a:hover { color: #6366f1; text-decoration: underline; }\n"
            "        .story { background: #f8fafc; border-left: 4px solid #6366f1; padding: 15px 20px; margin-bottom: 25px; border-radius: 0 8px 8px 0; }\n"
            "        .meta { color: #64748b; font-size: 13px; margin-bottom: 10px; }\n"
            "        .summary { background: white; padding: 15px; border-radius: 6px; border: 1px solid #e2e8f0; white-space: pre-wrap; font-family: Georgia, serif; font-size: 15px; }\n"
            "        .footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #e2e8f0; color: #94a3b8; font-size: 12px; text-align: center; }\n"
            "    </style>\n"
            "</head>\n<body>\n"
            "    <h1>AI News Daily Brief</h1>\n"
            f"    <p style=\"color: #64748b;\"><em>{html.escape(now)}</em></p>\n")

    emojis = {"datacenter": "DC", "regulation": "REG", "jobs": "JOBS", "general": "NEWS"}
    for category in ["datacenter", "regulation", "jobs", "general"]:
        stories = stories_by_category.get(category, [])
        if not stories:
            continue
        emoji = emojis.get(category, "NEWS")
        html_content += f"    <h2>{html.escape(emoji)} {html.escape(category.upper())}</h2>\n"
        for story in stories:
            title = html.escape(story['title'])
            link = html.escape(story['link'])
            source = html.escape(story['source'])
            published = html.escape(story['published'])
            summary = html.escape(story['summary'])
            html_content += (f"    <div class=\"story\">\n"
                     f"        <h3><a href=\"{link}\">{title}</a></h3>\n"
                     f"        <div class=\"meta\">{source} | {published}</div>\n"
                     f"        <div class=\"summary\">{summary}</div>\n"
                     f"    </div>\n")

    html_content += ("    <div class=\"footer\">\n"
             "        Generated by AI News Pipeline | Edit and publish to your CMS\n"
             "    </div>\n"
             "</body>\n"
             "</html>")
    return html_content


def build_text_email(stories_by_category):
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    lines = [
        "AI News Daily Brief",
        now,
        "=" * 40,
        ""
    ]
    for category in ["datacenter", "regulation", "jobs", "general"]:
        stories = stories_by_category.get(category, [])
        if not stories:
            continue
        lines.append(f"[{category.upper()}]")
        lines.append("-" * 40)
        for story in stories:
            lines.append(story['title'])
            lines.append(f"Source: {story['source']} | {story['published']}")
            lines.append(f"Link: {story['link']}")
            lines.append("")
            lines.append(story['summary'])
            lines.append("")
    lines.append("Generated by AI News Pipeline | Edit and publish to your CMS")
    return "\n".join(lines)


def send_email(subject, html_body, text_body):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = EMAIL_USERNAME
    msg['To'] = EMAIL_TO
    msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)
        logger.info(f"Email sent successfully to {EMAIL_TO}")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("AUTH ERROR: Check your email password. For Gmail, use an App Password.")
        return False
    except Exception as e:
        logger.error(f"Email failed: {e}")
        return False


def load_summary_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Could not load summary cache: {e}")
        return {}


def save_summary_cache(cache):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save summary cache: {e}")


def main():
    logger.info("=" * 60)
    logger.info("AI NEWS PIPELINE STARTING")
    logger.info(f"Provider: {AI_PROVIDER} | Model: {AI_MODEL} | Lookback: {HOURS_BACK}h")
    logger.info("=" * 60)

    all_raw_stories = []
    for category, urls in FEEDS.items():
        logger.info(f"\nCategory: {category}")
        for url in urls:
            entries = fetch_feed(url)
            for entry in entries:
                if not is_recent(entry):
                    continue
                title = entry.get('title', 'Untitled')
                link = entry.get('link', '')
                raw_summary = entry.get('summary', entry.get('description', ''))
                story = {
                    'title': title,
                    'link': link,
                    'raw_summary': clean_text(raw_summary),
                    'source': urlparse(url).netloc.replace('www.', ''),
                    'hash': get_story_hash(title, link),
                    'published': entry.get('published', 'Recent'),
                    'category': category if category != "general" else categorize_story(title, clean_text(raw_summary))
                }
                all_raw_stories.append(story)
            time.sleep(0.5)

    logger.info(f"\nTotal raw stories collected: {len(all_raw_stories)}")

    logger.info("\nDeduplicating...")
    unique_stories = deduplicate_stories(all_raw_stories)

    logger.info("\nOrganizing by category...")
    by_category = defaultdict(list)
    for story in unique_stories:
        by_category[story['category']].append(story)

    stories_to_summarize = []
    if ENABLE_EDITOR:
        logger.info("\nSelecting top stories (editor agent)...")
        for category in ["datacenter", "regulation", "jobs", "general"]:
            pool = by_category[category]
            logger.info(f"  {category}: choosing {min(MAX_STORIES_PER_CATEGORY, len(pool))} of {len(pool)}")
            stories_to_summarize.extend(
                select_top_stories_with_ai(pool, category, MAX_STORIES_PER_CATEGORY)
            )
            time.sleep(1)
    else:
        for category in ["datacenter", "regulation", "jobs", "general"]:
            stories_to_summarize.extend(by_category[category][:MAX_STORIES_PER_CATEGORY])

    if ENABLE_RESEARCHER:
        logger.info("\nFetching full article text (researcher agent)...")
        for story in stories_to_summarize:
            full_text = fetch_full_text(story['link'])
            if len(full_text) > len(story['raw_summary']) + 200:
                logger.info(f"  Full text: {story['title'][:55]}...")
                story['raw_summary'] = full_text
            time.sleep(0.5)

    logger.info("\nGenerating AI summaries...")
    logger.info(f"Using {AI_PROVIDER} - this may take a minute")

    summary_cache = load_summary_cache()

    for i, story in enumerate(stories_to_summarize, 1):
        cached_summary = summary_cache.get(story['hash'])
        if cached_summary:
            logger.info(f"[{i}/{len(stories_to_summarize)}] Using cached summary: {story['title'][:55]}...")
            story['summary'] = cached_summary
        else:
            logger.info(f"[{i}/{len(stories_to_summarize)}] Summarizing: {story['title'][:55]}...")
            summary = summarize_with_ai(story['title'], story['raw_summary'], story['category'])
            skip = False
            if ENABLE_CRITIC and not summary.startswith("[ERROR"):
                logger.info(f"  Critic agent reviewing...")
                critique = critique_with_ai(story['title'], story['raw_summary'], summary, story['category'])
                if critique.strip() == "SKIP":
                    logger.info(f"  Critic agent cut this story: {story['title'][:55]}...")
                    skip = True
                elif not critique.startswith("[ERROR"):
                    summary = critique
            if skip:
                story['summary'] = ""
            else:
                story['summary'] = summary
                summary_cache[story['hash']] = summary
            time.sleep(1)

    stories_to_summarize = [s for s in stories_to_summarize if s.get('summary')]
    save_summary_cache(summary_cache)

    logger.info("\nBuilding email...")
    email_groups = defaultdict(list)
    for story in stories_to_summarize:
        email_groups[story['category']].append(story)

    html_body = build_html_email(email_groups)
    text_body = build_text_email(email_groups)
    subject = "AI News Brief - " + datetime.now().strftime('%A, %B %d')

    logger.info("\nSending email...")
    success = send_email(subject, html_body, text_body)

    logger.info("\n" + "=" * 60)
    if success:
        logger.info("PIPELINE COMPLETE")
        logger.info(f"Stories summarized: {len(stories_to_summarize)}")
        logger.info(f"Categories covered: {list(email_groups.keys())}")
    else:
        logger.info("PIPELINE COMPLETE WITH ERRORS")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
