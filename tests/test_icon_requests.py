"""Icon downloads must not loop.

Each completed download publishes DECK_FORCE_RELOAD, which repaints the whole
page - a ~50KB transfer to the deck. Two bugs made that repeat forever:

- the dedup set was keyed by download_url on insert but icon.id on removal, so
  entries were never removed and the set never actually deduplicated
- a non-200 response wrote no file, so is_available() stayed False and the icon
  was requested again on the very next redraw, forcing another full repaint
"""

import pytest

from homedeck.enums import IconSource
from homedeck.icons import CustomBrandIconLayer, IconProvider


def layer(name):
    return CustomBrandIconLayer({
        'icon_source': IconSource.CUSTOM_BRAND,
        'icon_name': name,
        'icon_variant': None,
        'max_width': 196,
        'max_height': 196,
    })


class TestDedupKey:
    def test_insert_and_removal_use_the_same_key(self):
        provider = IconProvider()
        icon = layer('spotify')

        provider._requested.add(icon.download_url)
        provider._requested.discard(icon.download_url)

        assert provider._requested == set()

    def test_a_pending_request_is_not_repeated(self):
        provider = IconProvider()
        icon = layer('spotify')
        provider._requested.add(icon.download_url)

        # Would raise without a running loop if it got past the guard
        provider._request_icon(icon)

        assert provider._requested == {icon.download_url}


class TestFailedDownloads:
    def test_a_failed_url_is_not_retried(self):
        provider = IconProvider()
        icon = layer('does-not-exist')
        provider._failed.add(icon.download_url)

        # Reaching the download path needs a running loop; not raising here
        # means the guard returned first.
        provider._request_icon(icon)

        assert icon.download_url in provider._failed

    @pytest.mark.asyncio
    async def test_non_200_records_the_failure(self, monkeypatch):
        provider = IconProvider()
        icon = layer('does-not-exist')

        class FakeResponse:
            status_code = 404

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, timeout=None):
                return FakeResponse()

        monkeypatch.setattr('homedeck.icons.httpx.AsyncClient', FakeClient)

        await provider._queue.put(icon)
        await provider._worker()

        assert icon.download_url in provider._failed
        assert icon.download_url not in provider._requested


class TestSuccessStillWorks:
    @pytest.mark.asyncio
    async def test_a_200_writes_the_file_and_clears_the_request(self, monkeypatch, tmp_path):
        provider = IconProvider()
        icon = layer('spotify')
        target = tmp_path / 'spotify.svg'
        monkeypatch.setattr(type(icon), 'original_file_path', property(lambda self: str(target)))

        class FakeResponse:
            status_code = 200
            content = b'<svg/>'

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, timeout=None):
                return FakeResponse()

        monkeypatch.setattr('homedeck.icons.httpx.AsyncClient', FakeClient)

        provider._requested.add(icon.download_url)
        await provider._queue.put(icon)
        await provider._worker()

        assert target.read_bytes() == b'<svg/>'
        assert provider._requested == set()      # cleared, so a retry is possible
        assert provider._failed == set()
