#!/usr/bin/env python3
"""Fetch bilibili followed creators' videos and download + extract audio."""

import argparse
import csv
import sqlite3
import subprocess
import os
import re
import sys
from pathlib import Path

import mlx_whisper

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

DB_PATH = "videos.db"
CONFIG_PATH = "config.csv"
VIDEO_DIR = "video"
AUDIO_DIR = "audio"
TRANSCRIPT_DIR = "transcript"
SUMMARY_DIR = "summary"

from dotenv import load_dotenv
load_dotenv()
WECHAT_TARGET = os.environ["WECHAT_TARGET"]
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"


def ensure_mlx_model():
    """Check if the MLX Whisper model is available locally; download if not."""
    # mlx-whisper stores models under ~/.cache/huggingface/hub/
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    model_dir_name = "models--mlx-community--whisper-large-v3-turbo"

    if (cache_dir / model_dir_name).exists():
        print(f"MLX Whisper model already cached.")
        return

    print(f"MLX Whisper model not found. Downloading (this may take a while)...")
    try:
        from huggingface_hub import snapshot_download
        print(f"  Downloading from {MLX_MODEL} ...")
        snapshot_download(MLX_MODEL, repo_type="model")
        print("  Model download complete.")
    except Exception as e:
        print(f"  [WARN] Model pre-download failed ({e}). It will be downloaded on first use.")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            rank INTEGER,
            title TEXT,
            plays INTEGER,
            likes INTEGER,
            date TEXT,
            vid TEXT NOT NULL,
            url TEXT,
            downloaded TEXT DEFAULT 'n',
            download_path TEXT DEFAULT '',
            UNIQUE(username, vid)
        )
    """)
    conn.commit()
    return conn


def read_usernames():
    usernames = []
    with open(CONFIG_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            name = row[0].strip() if row else ""
            if name:
                usernames.append(name)
    return usernames


def fetch_videos(username):
    """Fetch latest 2 videos for a username via opencli."""
    result = subprocess.run(
        ["opencli", "bilibili", "user-videos", username, "--order", "pubdate", "--limit", "2", "-f", "csv"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [ERROR] Failed to fetch videos for '{username}': {result.stderr.strip()}")
        return []

    lines = result.stdout.strip().splitlines()
    if len(lines) <= 1:
        print(f"  No videos found for '{username}'")
        return []

    videos = []
    for row in csv.reader(lines[1:]):  # skip header
        if len(row) < 6:
            continue
        parts = row
        url = parts[5].strip()
        # Extract vid from url
        vid = url.rstrip("/").split("/")[-1]
        videos.append({
            "rank": int(parts[0].strip()),
            "title": parts[1].strip(),
            "plays": int(parts[2].strip()),
            "likes": int(parts[3].strip()),
            "date": parts[4].strip(),
            "vid": vid,
            "url": url,
        })
    return videos


def insert_videos(conn, username, videos):
    """Insert new videos, return list of newly added records."""
    added = []
    for v in videos:
        try:
            conn.execute(
                "INSERT INTO videos (username, rank, title, plays, likes, date, vid, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (username, v["rank"], v["title"], v["plays"], v["likes"], v["date"], v["vid"], v["url"])
            )
            added.append(v)
        except sqlite3.IntegrityError:
            pass  # duplicate username+vid, skip
    conn.commit()
    return added


def download_video(vid):
    """Download video using opencli, return the downloaded file path or None."""
    os.makedirs(VIDEO_DIR, exist_ok=True)

    # Record existing files before download to detect new ones
    before = set(os.listdir(VIDEO_DIR))

    result = subprocess.run(
        ["opencli", "bilibili", "download", vid, "--output", VIDEO_DIR],
        capture_output=True, text=True, timeout=600
    )

    # Find newly added file(s) in VIDEO_DIR
    after = set(os.listdir(VIDEO_DIR))
    new_files = after - before

    # Pick the file matching vid
    for fname in new_files:
        if vid in fname:
            return os.path.join(VIDEO_DIR, fname)

    # Fallback: search all files in VIDEO_DIR
    for fname in os.listdir(VIDEO_DIR):
        if vid in fname:
            return os.path.join(VIDEO_DIR, fname)

    print(f"  [ERROR] Downloaded file not found for {vid}")
    return None


def extract_audio(video_path):
    """Extract audio from video using ffmpeg, return audio path or None."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    basename = os.path.splitext(os.path.basename(video_path))[0]
    audio_path = os.path.join(AUDIO_DIR, f"{basename}.mp3")

    result = subprocess.run(
        ["ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", "-y", audio_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [ERROR] Audio extraction failed for {video_path}: {result.stderr.strip()}")
        return None
    return audio_path


def transcribe_audio(audio_path):
    """Transcribe audio using mlx-whisper (Apple Silicon optimized), return transcript path or None."""
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    basename = os.path.splitext(os.path.basename(audio_path))[0]
    transcript_path = os.path.join(TRANSCRIPT_DIR, f"{basename}.txt")

    try:
        print(f"  Transcribing with MLX Whisper...")
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=MLX_MODEL,
            language="zh",
            verbose=False,
        )
        text = result["text"].strip()
        if not text:
            print(f"  [ERROR] Empty transcription for {audio_path}")
            return None
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"  Transcription done ({len(text)} chars).")
        return transcript_path
    except Exception as e:
        print(f"  [ERROR] Transcription failed for {audio_path}: {e}")
        return None


def summarize_and_send(transcript_path, title, audio_path):
    """Summarize transcript via openclaw agent, save summary, send to WeChat."""
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    basename = os.path.splitext(os.path.basename(transcript_path))[0]
    summary_path = os.path.join(SUMMARY_DIR, f"{basename}.txt")

    # Read transcript content
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_text = f.read()

    # Call Claude to summarize
    prompt = f"请不要有其他想法，直接对我给的文本进行简要综述，用中文回复，控制在300字以内：\n\n{transcript_text}"
    print(f"  Summarizing via Claude...")
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text", "--model", "claude-sonnet-4-20250514"],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(f"  [ERROR] Summarization failed: {result.stderr.strip()}")
        return None

    summary_text = result.stdout.strip()
    if not summary_text:
        print(f"  [ERROR] Empty summary response")
        return None

    # Save summary
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"【{title}】\n\n{summary_text}")
    print(f"  Summary saved: {summary_path}")

    # Send to WeChat: text first, then audio
    audio_abs = os.path.abspath(audio_path)
    # 1) Send text summary
    text_result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "openclaw-weixin",
         "--target", WECHAT_TARGET,
         "--message", f"【{title}】\n{summary_text}"],
        capture_output=True, text=True
    )
    if text_result.returncode != 0:
        print(f"  [ERROR] WeChat text send failed: {text_result.stderr.strip()}")
    else:
        print(f"  Summary text sent to WeChat.")
    # 2) Send audio file
    audio_result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "openclaw-weixin",
         "--target", WECHAT_TARGET,
         "--media", audio_abs],
        capture_output=True, text=True
    )
    if audio_result.returncode != 0:
        print(f"  [ERROR] WeChat audio send failed: {audio_result.stderr.strip()}")
    else:
        print(f"  Audio sent to WeChat.")

    return summary_path


def list_videos():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT username, title, date, plays, likes, downloaded, url FROM videos ORDER BY date DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No videos in database.")
        return

    print(f"{'Date':<12} {'User':<12} {'Downloaded':<4} {'Plays':>7} {'Likes':>6}  Title")
    print("-" * 80)
    for username, title, date, plays, likes, downloaded, url in rows:
        dl = "✓" if downloaded == "y" else "✗"
        print(f"{date:<12} {username:<12} {dl:<4} {plays:>7} {likes:>6}  {title}")
        print(f"{'':12} {'':12} {'':4} {'':7} {'':6}  {url}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Fetch bilibili followed creators' videos and download + extract audio.")
    parser.add_argument("command", nargs="?", default="run", help="'run' to fetch and download, 'list' to show all records (default: run)")
    args = parser.parse_args()

    if args.command == "list":
        list_videos()
        return

    conn = init_db()
    ensure_mlx_model()
    usernames = read_usernames()

    if not usernames:
        print("No usernames found in config.csv")
        conn.close()
        return

    # Phase 1: Fetch and store video info
    all_new = []
    for username in usernames:
        print(f"Fetching videos for: {username}")
        videos = fetch_videos(username)
        added = insert_videos(conn, username, videos)
        for v in added:
            print(f"  [NEW] {v['title']} ({v['date']}) - {v['url']}")
        all_new.extend([(username, v) for v in added])

    if not all_new:
        print("\nNo new videos to download.")
        conn.close()
        return

    # Phase 2: Download new videos and extract audio
    print(f"\nDownloading {len(all_new)} new video(s)...")
    for username, v in all_new:
        vid = v["vid"]
        print(f"  Downloading: {v['title']}")
        video_path = download_video(vid)
        if video_path:
            print(f"    Video saved: {video_path}")
            audio_path = extract_audio(video_path)
            if audio_path:
                print(f"    Audio saved: {audio_path}")
                transcript_path = transcribe_audio(audio_path)
                if transcript_path:
                    print(f"    Transcript saved: {transcript_path}")
                    summarize_and_send(transcript_path, v["title"], audio_path)
            conn.execute(
                "UPDATE videos SET downloaded = 'y', download_path = ? WHERE username = ? AND vid = ?",
                (video_path, username, vid)
            )
            conn.commit()
        else:
            print(f"    Download failed, skipping audio extraction.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
