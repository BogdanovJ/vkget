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

    def display_title(self) -> str:
        return "Playlist"


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
    def test_url_bit_title_displays_untitled_not_id(self):
        video = Video(
            title="-211437014_7",
            external_id="-211437014_7",
            webpage_url="https://vk.com/video-211437014_7",
            channel="Algebra",
            status="QUEUED",
        )
        self.assertEqual(video.display_title(), "Untitled")

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
        self.assertIn("Untitled", html)
        self.assertIn("Algebra", html)
        self.assertNotIn("-211437014_7", html)


if __name__ == "__main__":
    unittest.main()
