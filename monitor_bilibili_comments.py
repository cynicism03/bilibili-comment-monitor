#!/usr/bin/env python3
"""Monitor a Bilibili video's reply count and alert at target counts."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


DEFAULT_URL = (
    "https://www.bilibili.com/video/BV19zj16cExw/"
    "?spm_id_from=333.1365.list.card_archive.click"
    "&vd_source=f285dbef2d1227ce659f79932b8cbf2b"
)
DEFAULT_TARGETS = (519, 665)


class FetchError(RuntimeError):
    pass


class PushError(RuntimeError):
    pass


def parse_bvid(text: str) -> str:
    match = re.search(r"BV[0-9A-Za-z]{10}", text)
    if not match:
        raise ValueError("Could not find a BV id in the URL/text.")
    return match.group(0)


def fetch_reply_count(bvid: str, referer: str, timeout: float = 10.0) -> tuple[int, str]:
    query = urllib.parse.urlencode({"bvid": bvid})
    api_url = f"https://api.bilibili.com/x/web-interface/view?{query}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    }

    cookie = os.environ.get("BILIBILI_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie

    request = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"Network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise FetchError("Bilibili returned a non-JSON response.") from exc

    if payload.get("code") != 0:
        message = payload.get("message") or payload.get("msg") or "unknown error"
        raise FetchError(f"Bilibili API error {payload.get('code')}: {message}")

    data = payload.get("data") or {}
    stat = data.get("stat") or {}
    reply = stat.get("reply")
    if not isinstance(reply, int):
        raise FetchError("Could not find data.stat.reply in the API response.")

    return reply, str(data.get("title") or bvid)


def push_serverchan(sendkey: str, title: str, desp: str, timeout: float = 10.0) -> None:
    api_url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "User-Agent": "BilibiliCommentMonitor/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PushError(f"ServerChan HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise PushError(f"ServerChan network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise PushError("ServerChan returned a non-JSON response.") from exc

    if payload.get("code") not in (0, None):
        message = payload.get("message") or payload.get("msg") or "unknown error"
        raise PushError(f"ServerChan API error {payload.get('code')}: {message}")


def load_state(path: str) -> dict[str, object]:
    if not os.path.exists(path):
        return {"alerted_targets": []}

    with open(path, "r", encoding="utf-8") as state_file:
        state = json.load(state_file)

    if not isinstance(state, dict):
        raise ValueError(f"State file must contain a JSON object: {path}")
    return state


def save_state(path: str, state: dict[str, object]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=True, indent=2, sort_keys=True)
        state_file.write("\n")
    os.replace(temp_path, path)


def alert_user(
    count: int,
    target: int,
    title: str,
    video_url: str,
    open_page: bool,
    serverchan_key: str = "",
) -> None:
    message = (
        f"Reply count is {count}. Target {target} was reached or passed.\n\n"
        f"Go comment on:\n{title}"
    )
    wechat_title = f"Bilibili comment alert: {count}"
    wechat_desp = (
        f"Video: {title}\n\n"
        f"Current reply count: {count}\n\n"
        f"Target reply count: {target}\n\n"
        f"Link: {video_url}"
    )

    if serverchan_key:
        try:
            push_serverchan(serverchan_key, wechat_title, wechat_desp)
            print("WeChat push sent through ServerChan.", flush=True)
        except Exception as exc:
            print(f"Failed to send WeChat push: {exc}", file=sys.stderr, flush=True)

    print("\a", end="", flush=True)

    if sys.platform.startswith("win"):
        try:
            import winsound

            for _ in range(3):
                winsound.Beep(1200, 350)
                time.sleep(0.12)
        except Exception:
            pass

    if open_page:
        webbrowser.open(video_url)

    if sys.platform.startswith("win"):
        try:
            ctypes.windll.user32.MessageBoxW(0, message, "Bilibili comment alert", 0x40)
            return
        except Exception:
            pass

    print("=" * 72)
    print(message)
    print("=" * 72)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Alert when a Bilibili video reply count reaches target values."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help="Bilibili video URL. Defaults to the URL from this request.",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        type=int,
        default=list(DEFAULT_TARGETS),
        help="Reply counts to alert at. Default: 519 665.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds. Default: 5.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Alert only when the count is exactly a target, not when it jumps past one.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not automatically open the video page when alerting.",
    )
    parser.add_argument(
        "--serverchan-key",
        default=os.environ.get("SERVERCHAN_SENDKEY", "").strip(),
        help=(
            "ServerChan SendKey for WeChat push. "
            "You can also set the SERVERCHAN_SENDKEY environment variable."
        ),
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Keep monitoring even after all targets are reached or passed.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch the current reply count once, then exit without alerting.",
    )
    parser.add_argument(
        "--cloud-check",
        action="store_true",
        help=(
            "Fetch once, alert for newly reached targets, and persist state. "
            "This is intended for scheduled jobs such as GitHub Actions."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=".bilibili_comment_state.json",
        help="State file used by --cloud-check. Default: .bilibili_comment_state.json.",
    )
    return parser.parse_args()


def run_cloud_check(
    args: argparse.Namespace,
    bvid: str,
    video_url: str,
    targets: list[int],
) -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    count, title = fetch_reply_count(bvid, video_url)
    state = load_state(args.state_file)

    alerted_targets = {
        int(target)
        for target in state.get("alerted_targets", [])
        if isinstance(target, int) or str(target).isdigit()
    }
    newly_reached = [
        target for target in targets if target not in alerted_targets and count >= target
    ]

    print(f"Cloud check at {now}")
    print(f"reply count = {count} | {title}")
    print(f"newly reached targets = {newly_reached or 'none'}")

    if newly_reached and not args.serverchan_key:
        print(
            "SERVERCHAN_SENDKEY is not set, so WeChat push is disabled.",
            file=sys.stderr,
            flush=True,
        )

    for target in newly_reached:
        alert_user(
            count=count,
            target=target,
            title=title,
            video_url=video_url,
            open_page=False,
            serverchan_key=args.serverchan_key,
        )
        alerted_targets.add(target)

    if newly_reached:
        save_state(
            args.state_file,
            {
                "alerted_targets": sorted(alerted_targets),
                "bvid": bvid,
                "last_alert_count": count,
                "last_alerted_at": now,
                "title": title,
            },
        )

    return 0


def main() -> int:
    args = parse_args()
    video_url = args.url
    bvid = parse_bvid(video_url)
    targets = sorted(set(args.targets))
    alerted: set[int] = set()
    last_count: int | None = None

    if args.cloud_check:
        return run_cloud_check(args, bvid, video_url, targets)

    print(f"Monitoring {bvid}")
    print(f"Targets: {', '.join(map(str, targets))}")
    print(f"Interval: {args.interval:g}s")
    print(f"WeChat push: {'enabled' if args.serverchan_key else 'disabled'}")
    print("Press Ctrl+C to stop.")

    while True:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            count, title = fetch_reply_count(bvid, video_url)
            print(f"[{now}] reply count = {count} | {title}", flush=True)

            if args.once:
                return 0

            for target in targets:
                if target in alerted:
                    continue

                exact_hit = count == target
                crossed_hit = (
                    not args.strict
                    and last_count is not None
                    and last_count < target <= count
                )
                if exact_hit or crossed_hit:
                    alerted.add(target)
                    alert_user(
                        count=count,
                        target=target,
                        title=title,
                        video_url=video_url,
                        open_page=not args.no_open,
                        serverchan_key=args.serverchan_key,
                    )

            last_count = count

            if (
                not args.keep_running
                and targets
                and count >= max(targets)
                and all(target <= count for target in targets)
            ):
                print("All targets have been reached or passed. Exiting.")
                return 0

        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:
            print(f"[{now}] {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1

        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
