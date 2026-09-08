from __future__ import annotations

import unittest

from app.main import templates
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


if __name__ == "__main__":
    unittest.main()
