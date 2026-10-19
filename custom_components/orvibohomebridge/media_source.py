"""Read lock archives through Home Assistant's authenticated media API."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    PlayMedia,
    Unresolvable,
)

from .const import DOMAIN

URL_PREFIX = f"/api/{DOMAIN}/media"


async def async_get_media_source(hass) -> LockMediaSource:
    """Register one private archive source, without changing user media_dirs."""
    source = LockMediaSource(hass)
    hass.http.register_view(LockMediaView(source))
    return source


class LockMediaSource(MediaSource):
    """Expose only this integration's existing image and video archives."""

    name = "欧瑞博门锁抓拍"

    def __init__(self, hass):
        super().__init__(DOMAIN)
        self.hass = hass
        self.root = Path(hass.config.path("media")) / DOMAIN

    def _path(self, identifier: str) -> Path:
        directory, _, location = identifier.partition("/")
        if directory != "archive" or "\\" in location:
            raise Unresolvable("Unknown archive path")
        try:
            root = self.root.resolve()
            path = (root / location).resolve()
            path.relative_to(root)
        except (ValueError, OSError, RuntimeError) as err:
            raise Unresolvable("Invalid archive path") from err
        return path

    def _file(self, identifier: str) -> tuple[Path, str]:
        path = self._path(identifier)
        mime_type, _ = mimetypes.guess_type(str(path))
        if not path.is_file() or not mime_type or mime_type.split("/")[0] not in ("image", "video"):
            raise Unresolvable("Archive media not found")
        return path, mime_type

    async def async_resolve_media(self, item) -> PlayMedia:
        if item.domain != DOMAIN:
            raise Unresolvable("Unknown domain")
        _, mime_type = await self.hass.async_add_executor_job(self._file, item.identifier)
        return PlayMedia(f"{URL_PREFIX}/{quote(item.identifier, safe='/')}", mime_type)

    async def async_browse_media(self, item) -> BrowseMediaSource:
        if item.domain != DOMAIN:
            raise BrowseError("Unknown domain")
        return await self.hass.async_add_executor_job(self._browse, item.identifier or "archive")

    def _browse(self, identifier: str) -> BrowseMediaSource:
        try:
            path = self._path(identifier)
        except Unresolvable as err:
            raise BrowseError(str(err)) from err
        if path != self.root.resolve() and not path.is_dir():
            raise BrowseError("Archive directory not found")
        result = BrowseMediaSource(
            domain=DOMAIN, identifier=identifier, media_class=MediaClass.DIRECTORY,
            media_content_type="", title=self.name if identifier == "archive" else path.name,
            can_play=False, can_expand=True, children=[],
        )
        if not path.is_dir():
            return result
        for child in sorted(path.iterdir(), key=lambda candidate: candidate.name):
            if child.name.startswith("."):
                continue
            child_id = f"{identifier.rstrip('/')}/{child.name}"
            try:
                checked = self._path(child_id)
                is_dir = checked.is_dir()
                mime_type = "" if is_dir else self._file(child_id)[1]
            except Unresolvable:
                continue
            result.children.append(BrowseMediaSource(
                domain=DOMAIN, identifier=child_id, title=child.name,
                media_class=MediaClass.DIRECTORY if is_dir else (
                    MediaClass.IMAGE if mime_type.startswith("image/") else MediaClass.VIDEO),
                media_content_type=mime_type, can_play=not is_dir, can_expand=is_dir,
            ))
        result.children.sort(key=lambda child: (child.can_play, child.title))
        return result


class LockMediaView(HomeAssistantView):
    """Serve archive files using HA bearer authentication or signed paths."""

    url = f"{URL_PREFIX}/{{identifier:.*}}"
    name = f"api:{DOMAIN}:media"
    requires_auth = True

    def __init__(self, source: LockMediaSource):
        self.source = source

    async def get(self, request, identifier: str) -> web.FileResponse:
        try:
            path, _ = await self.source.hass.async_add_executor_job(self.source._file, identifier)
        except Unresolvable as err:
            raise web.HTTPNotFound from err
        return web.FileResponse(path)
