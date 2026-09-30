from __future__ import annotations

import unittest

from app.main import templates
from app.models import Video
from app.ytdlp import to_vkvideo


STORED = "https://vk.com/playlist/-211437014_7"
DISPLAY = "https://vkvideo.ru/playlist/-211437014_7"


class FakeSub:
    id = 1
    source_url = STORED
    enabled = True
    next_scan_at = None
    last_scan_at = None
    last_scan_result = None
    last_error = None
    initial_last_n = 3
    min_duration_seconds = 600
    extra_stop_words = ""
    watch_future = True
    retention_days = None

    def display_title(self) -> str:
        return "Playlist"

    def newest_video_line(self) -> str:
        return ""


class VkvideoFilterTests(unittest.TestCase):
    def test_filter_is_to_vkvideo(self):
        self.assertIs(templates.env.filters["vkvideo"], to_vkvideo)

    def test_filter_converts_vk_com_playlist(self):
        self.assertEqual(templates.env.filters["vkvideo"](STORED), DISPLAY)

    def test_filter_leaves_vkvideo_url(self):
        self.assertEqual(templates.env.filters["vkvideo"](DISPLAY), DISPLAY)

    def test_home_row_shows_vkvideo_text_without_nested_link(self):
        html = templates.env.get_template("index.html").render(
            counts={
                "subscriptions": 1,
                "queued": 0,
                "downloading": 0,
                "completed": 0,
            },
            recent=[],
            subs=[FakeSub()],
            next_scan={"when": "NONE", "title": None},
            next_download={"when": "NONE", "title": None},
            max_height=720,
        )
        self.assertIn(f'href="/subscriptions/{FakeSub.id}"', html)
        self.assertIn(DISPLAY, html)
        self.assertNotIn(STORED, html)
        self.assertNotIn(f'href="{DISPLAY}"', html)

    def test_subscriptions_list_shows_vkvideo_text_without_nested_link(self):
        html = templates.env.get_template("subscriptions.html").render(
            subs=[FakeSub()]
        )
        self.assertIn(f'href="/subscriptions/{FakeSub.id}"', html)
        self.assertIn(DISPLAY, html)
        self.assertNotIn(STORED, html)
        self.assertNotIn(f'href="{DISPLAY}"', html)

    def test_subscription_detail_links_to_vkvideo(self):
        html = templates.env.get_template("subscription.html").render(
            sub=FakeSub(),
            videos=[],
        )
        self.assertIn(DISPLAY, html)
        self.assertNotIn(STORED, html)
        self.assertIn(f'href="{DISPLAY}"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener"', html)
        self.assertIn('action="/subscriptions/1/delete"', html)
        self.assertIn("DELETE SUBSCRIPTION", html)
        self.assertIn("NEWEST: UNKNOWN", html)
        self.assertNotIn("ALREADY SUBSCRIBED", html)

    def test_subscription_detail_explains_duplicate_channel(self):
        html = templates.env.get_template("subscription.html").render(
            sub=FakeSub(),
            videos=[],
            already=True,
        )
        self.assertIn("ALREADY SUBSCRIBED", html)
        self.assertIn("ONE ROW ON THE BOARD", html)

    def test_catalogue_shows_upload_date(self):
        video = Video(
            title="КСТАТИ #113",
            external_id="-220754053_456246718",
            webpage_url="https://vk.com/video-220754053_456246718",
            channel="VK Видео",
            status="COMPLETED",
            upload_date="20260919",
            duration=6413,
        )
        html = templates.env.get_template("subscription.html").render(
            sub=FakeSub(),
            videos=[video],
        )
        self.assertIn("2026-09-19", html)
        self.assertIn("КСТАТИ #113", html)

    def test_lists_show_newest_video_stamp(self):
        sub = FakeSub()
        sub.newest_video_line = lambda: "19 Sep 2026, 09:00 · КСТАТИ #113"
        home = templates.env.get_template("index.html").render(
            counts={
                "subscriptions": 1,
                "queued": 0,
                "downloading": 0,
                "completed": 0,
            },
            recent=[],
            subs=[sub],
            next_scan={"when": "NONE", "title": None},
            next_download={"when": "NONE", "title": None},
            max_height=720,
        )
        listing = templates.env.get_template("subscriptions.html").render(subs=[sub])
        detail = templates.env.get_template("subscription.html").render(
            sub=sub,
            videos=[],
        )
        self.assertIn("NEWEST VIDEO: 19 Sep 2026, 09:00 · КСТАТИ #113", home)
        self.assertIn("NEWEST VIDEO: 19 Sep 2026, 09:00 · КСТАТИ #113", listing)
        self.assertIn("NEWEST: 19 Sep 2026, 09:00 · КСТАТИ #113", detail)

    def test_lists_drop_repeated_newest_stamp_from_scan_line(self):
        sub = FakeSub()
        sub.newest_video_line = lambda: "19 Sep 2026, 09:00 · КСТАТИ #113"
        sub.last_scan_result = "NO NEW VIDEOS · 114 already known · NEWEST 19 Sep 2026"
        home = templates.env.get_template("index.html").render(
            counts={
                "subscriptions": 1,
                "queued": 0,
                "downloading": 0,
                "completed": 0,
            },
            recent=[],
            subs=[sub],
            next_scan={"when": "NONE", "title": None},
            next_download={"when": "NONE", "title": None},
            max_height=720,
        )
        listing = templates.env.get_template("subscriptions.html").render(subs=[sub])
        self.assertIn("NO NEW VIDEOS · 114 already known", home)
        self.assertNotIn("NEWEST 19 Sep 2026", home)
        self.assertIn("NO NEW VIDEOS · 114 already known", listing)
        self.assertNotIn("NEWEST 19 Sep 2026", listing)

    def test_recent_hides_placeholder_channel_and_shortens_status(self):
        video = Video(
            title="NA",
            external_id="-220754053_456246683",
            webpage_url="https://vk.com/video-220754053_456246683",
            channel="Subscription",
            status="IGNORED_INITIAL_HISTORY",
        )
        html = templates.env.get_template("index.html").render(
            counts={
                "subscriptions": 0,
                "queued": 0,
                "downloading": 0,
                "completed": 0,
            },
            recent=[video],
            subs=[],
            next_scan={"when": "NONE", "title": None},
            next_download={"when": "NONE", "title": None},
            max_height=720,
        )
        self.assertIn("Video -220754053_456246683", html)
        self.assertIn(">HISTORY<", html)
        self.assertNotIn("IGNORED_INITIAL_HISTORY", html)
        self.assertNotIn(">Subscription<", html)


class FakeQueuedVideo:
    channel = "Algebra"
    attempts = 1
    next_attempt_at = None
    last_error = None
    status = "QUEUED"

    def __init__(self, title: str, external_id: str):
        self._video = Video(
            title=title,
            external_id=external_id,
            webpage_url=f"https://vk.com/video{external_id}",
            channel=self.channel,
            status=self.status,
            attempts=self.attempts,
        )

    def display_title(self) -> str:
        return self._video.display_title()


class QueueTitleDisplayTests(unittest.TestCase):
    def test_url_bit_title_displays_channel_not_id(self):
        video = Video(
            title="-211437014_7",
            external_id="-211437014_7",
            webpage_url="https://vk.com/video-211437014_7",
            channel="Algebra",
            status="QUEUED",
        )
        self.assertEqual(video.display_title(), "Algebra")

    def test_real_title_is_shown(self):
        video = Video(
            title="Lecture 4 — Linear maps",
            external_id="-211437014_7",
            webpage_url="https://vk.com/video-211437014_7",
            channel="Algebra",
            status="QUEUED",
        )
        self.assertEqual(video.display_title(), "Lecture 4 — Linear maps")

    def test_queue_row_shows_human_title_and_channel(self):
        html = templates.env.get_template("queue.html").render(
            videos=[
                FakeQueuedVideo("Lecture 4 — Linear maps", "-211437014_7"),
            ]
        )
        self.assertIn("Lecture 4 — Linear maps", html)
        self.assertIn("Algebra", html)
        self.assertNotIn("-211437014_7", html)

    def test_queue_hides_url_bit_title(self):
        html = templates.env.get_template("queue.html").render(
            videos=[FakeQueuedVideo("-211437014_7", "-211437014_7")]
        )
        self.assertIn("Algebra", html)
        self.assertNotIn("-211437014_7", html)
        self.assertNotIn("Untitled", html)


if __name__ == "__main__":
    unittest.main()
