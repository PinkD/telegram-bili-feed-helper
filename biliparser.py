import asyncio
from datetime import datetime
import html
import json
import os
import re
from functools import cached_property, lru_cache
from io import BytesIO

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag
from telegraph import Telegraph
from tortoise import Tortoise, timezone
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from database import (
    audio_cache,
    bangumi_cache,
    dynamic_cache,
    live_cache,
    read_cache,
    reply_cache,
    video_cache,
)
from utils import BILI_API, compress, headers, logger

CACHES = {
    "audio": audio_cache,
    "bangumi": bangumi_cache,
    "dynamic": dynamic_cache,
    "live": live_cache,
    "read": read_cache,
    "reply": reply_cache,
    "video": video_cache,
}


def escape_markdown(text):
    return (
        re.sub(r"([_*\[\]()~`>\#\+\-=|{}\.!\\])", r"\\\1", html.unescape(text))
        if text
        else str()
    )


class ParserException(Exception):
    def __init__(self, msg, url, res=str()):
        self.msg = msg
        self.url = url
        self.res = res

    def __str__(self):
        return f"{self.msg}: {self.url} ->\n{self.res}"


class feed:
    user: str = ""
    uid: str = ""
    __content: str = ""
    __mediaurls: list = []
    mediaraws: bool = False
    mediatype: str = ""
    mediathumb: str = ""
    mediaduration: int = 0
    mediatitle: str = ""
    extra_markdown: str = ""
    replycontent: dict

    def __init__(self, rawurl):
        self.rawurl = rawurl

    @staticmethod
    def make_user_markdown(user, uid):
        return (
            f"[@{escape_markdown(user)}](https://space.bilibili.com/{uid})"
            if user and uid
            else str()
        )

    @staticmethod
    def shrink_line(text):
        return (
            re.sub(r"\n*\n", r"\n", re.sub(r"\r\n", r"\n", text.strip()))
            if text
            else str()
        )

    @cached_property
    def user_markdown(self):
        return self.make_user_markdown(self.user, self.uid)

    @property
    @lru_cache(maxsize=1)
    def content(self):
        return self.shrink_line(self.__content)

    @content.setter
    def content(self, content):
        self.__content = content

    @cached_property
    def content_markdown(self):
        content_markdown = escape_markdown(self.content)
        if not content_markdown.endswith("\n"):
            content_markdown += "\n"
        # if self.extra_markdown:
        #     content_markdown += self.extra_markdown
        return self.shrink_line(content_markdown)

    @cached_property
    def has_comment(self):
        if not hasattr(self, "replycontent"):
            return False
        return bool(self.replycontent.get("data"))

    @cached_property
    def comment(self):
        comment = str()
        if self.has_comment:
            if top := self.replycontent["data"].get("top"):
                for item in top.values():
                    if item:
                        comment += f'🔝> @{item["member"]["uname"]}:\n{item["content"]["message"]}\n'
        return self.shrink_line(comment)

    @cached_property
    def comment_markdown(self):
        comment_markdown = str()
        if self.has_comment:
            if top := self.replycontent["data"].get("top"):
                for item in top.values():
                    if item:
                        comment_markdown += f'🔝\\> {self.make_user_markdown(item["member"]["uname"], item["member"]["mid"])}:\n{escape_markdown(item["content"]["message"])}\n'
        return self.shrink_line(comment_markdown)

    @property
    @lru_cache(maxsize=1)
    def mediaurls(self):
        return self.__mediaurls

    @mediaurls.setter
    def mediaurls(self, content):
        if isinstance(content, list):
            self.__mediaurls = content
        else:
            self.__mediaurls = [content]

    @cached_property
    def mediafilename(self):
        def get_filename(url) -> str:
            target = re.search(r"\/([^\/]*\.\w{3,4})(?:$|\?)", url)
            if target:
                return target.group(1)
            return str()

        return (
            [get_filename(i) for i in self.__mediaurls] if self.__mediaurls else list()
        )

    @cached_property
    def url(self):
        return self.rawurl


class dynamic(feed):
    detailcontent: dict = {}
    dynamic_id: int = 0
    rid: int = 0
    __user: str = ""
    __content: str = ""
    forward_user: str = ""
    forward_uid: int = 0
    forward_content: str = ""

    @cached_property
    def forward_card(self):
        return json.loads(self.detailcontent["data"]["card"]["card"])

    @cached_property
    def has_forward(self):
        return bool(self.detailcontent["data"]["card"]["desc"]["orig_type"])

    @cached_property
    def forward_type(self):
        return self.detailcontent["data"]["card"]["desc"]["type"]

    @cached_property
    def origin_type(self):
        return (
            self.detailcontent["data"]["card"]["desc"]["orig_type"]
            if self.has_forward
            else self.forward_type
        )

    @cached_property
    def reply_type(self):
        if self.forward_type == 2:
            return 11
        if self.forward_type == 16:
            return 5
        if self.forward_type == 64:
            return 12
        if self.forward_type == 256:
            return 14
        if self.forward_type in [8, 512, *range(4000, 4200)]:
            return 1
        if self.forward_type in [1, 4, *range(4200, 4300), *range(2048, 2100)]:
            return 17

    @cached_property
    def oid(self):
        if self.forward_type in [1, 4, *range(4200, 4300), *range(2048, 2100)]:
            return self.dynamic_id
        else:
            return self.rid

    @cached_property
    def card(self):
        return (
            json.loads(self.forward_card.get("origin"))
            if self.has_forward
               and self.forward_card.get(
                "origin"
            )  # forwared deleted content (workaround, not implemented yet)
            else self.forward_card
        )

    @cached_property
    def add_on_card(self):
        display = self.detailcontent["data"]["card"]["display"]
        if "add_on_card_info" in display:
            return display["add_on_card_info"]
        return []

    @property
    @lru_cache(maxsize=1)
    def user(self):
        return self.forward_user if self.has_forward else self.__user

    @user.setter
    def user(self, user):
        self.__user = user

    @cached_property
    def user_markdown(self):
        return (
            self.make_user_markdown(self.forward_user, self.forward_uid)
            if self.has_forward
            else self.make_user_markdown(self.__user, self.uid)
        )

    @property
    @lru_cache(maxsize=1)
    def content(self):
        content = str()
        if self.has_forward:
            content = self.forward_content
            if self.__user:
                content += f"//@{self.__user}:\n"
        content += self.__content
        return self.shrink_line(content)

    @content.setter
    def content(self, content):
        self.__content = content

    @cached_property
    def content_markdown(self):
        content_markdown = str()
        if self.has_forward:
            content_markdown += escape_markdown(self.forward_content)
            if self.uid:
                content_markdown += (
                    f"//{self.make_user_markdown(self.__user, self.uid)}:\n"
                )
            elif self.__user:
                content_markdown += f"//@{escape_markdown(self.__user)}:\n"
        content_markdown += escape_markdown(self.__content)
        if not content_markdown.endswith("\n"):
            content_markdown += "\n"
        if self.extra_markdown:
            content_markdown += self.extra_markdown
        return self.shrink_line(content_markdown)

    @cached_property
    def url(self):
        return f"https://t.bilibili.com/{self.dynamic_id}"


class audio(feed):
    infocontent: dict = {}
    mediacontent: str = ""
    audio_id: int = 0
    reply_type: int = 14

    @cached_property
    def url(self):
        return f"https://www.bilibili.com/audio/au{self.audio_id}"


class live(feed):
    rawcontent: dict = {}
    room_id: int = 0

    @cached_property
    def url(self):
        return f"https://live.bilibili.com/{self.room_id}"


class video(feed):
    aid: int = 0
    cid: int = 0
    sid: int = 0
    cidcontent: dict = {}
    infocontent: dict = {}
    mediacontent: dict = {}
    reply_type: int = 1

    @cached_property
    def url(self):
        return f"https://www.bilibili.com/video/av{self.aid}"


class read(feed):
    rawcontent: str = ""
    read_id: int = 0
    reply_type: int = 12

    @cached_property
    def url(self):
        return f"https://www.bilibili.com/read/cv{self.read_id}"


def safe_parser(func):
    async def inner_function(*args, **kwargs):
        try:
            try:
                return await func(*args, **kwargs)
            except IntegrityError:
                ## try again with SQL race condition
                return await func(*args, **kwargs)
        except Exception as err:
            if err.__class__ == ParserException:
                logger.error(err)
            else:
                logger.exception(err)
            return err

    return inner_function


@safe_parser
async def reply_parser(client, oid, reply_type):
    if cache := await reply_cache.get_or_none(
        query := Q(Q(oid=oid), Q(reply_type=reply_type)),
        Q(created__gte=timezone.now() - reply_cache.timeout),
    ):
        logger.info(f"拉取评论缓存: {cache.created}")
        r = cache.content
    else:
        r = (
            await client.get(
                BILI_API + "/x/v2/reply/main",
                params={"oid": oid, "type": reply_type},
            )
        ).json()
        if not r.get("data"):
            raise ParserException("评论解析错误", r.url, r)
    logger.info(f"评论ID: {oid}, 评论类型: {reply_type}")
    if not cache:
        logger.info(f"评论缓存: {oid}")
        if cache := await reply_cache.get_or_none(query):
            cache.content = r
            await cache.save(update_fields=["content", "created"])
        else:
            await reply_cache(oid=oid, reply_type=reply_type, content=r).save()
    return r


@safe_parser
async def dynamic_parser(client: httpx.AsyncClient, url: str):
    if not (match := re.search(r"[th]\.bilibili\.com[\/\w]*\/(\d+)", url)):
        raise ParserException("动态链接错误", url, match)
    f = dynamic(url)
    query = (
        Q(rid=match.group(1))
        if "type=2" in match.group(0)
        else Q(dynamic_id=match.group(1))
    )
    if cache := await dynamic_cache.get_or_none(
        query,
        Q(created__gte=timezone.now() - dynamic_cache.timeout),
    ):
        logger.info(f"拉取动态缓存: {cache.created}")
        f.detailcontent = cache.content
    else:
        r = await client.get(
            "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/get_dynamic_detail",
            params={"rid": match.group(1), "type": 2}
            if "type=2" in match.group(0) or "h.bilibili.com" in match.group(0)
            else {"dynamic_id": match.group(1)},
        )
        f.detailcontent = r.json()
        if not f.detailcontent.get("data").get("card"):
            raise ParserException("动态解析错误", r.url, f.detailcontent)
    f.dynamic_id = f.detailcontent["data"]["card"]["desc"]["dynamic_id"]
    f.rid = f.detailcontent["data"]["card"]["desc"]["rid"]
    logger.info(f"动态ID: {f.dynamic_id}")
    if not cache:
        logger.info(f"动态缓存: {f.dynamic_id}")
        if cache := await dynamic_cache.get_or_none(query):
            cache.content = f.detailcontent
            await cache.save(update_fields=["content", "created"])
        else:
            await dynamic_cache(
                dynamic_id=f.dynamic_id, rid=f.rid, content=f.detailcontent
            ).save()
    # extract from detail.js
    detail_types_list = {
        # REPOST WORD
        "WORD": [1, 4],
        "PIC": [2],
        "VIDEO": [8, 512, *range(4000, 4200)],
        "CLIP": [16],
        "ARTICLE": [64],
        "MUSIC": [256],
        # LIVE LIVE_ROOM
        "LIVE": range(4200, 4300),
        # H5_SHARE COMIC_SHARE
        "SHARE": range(2048, 2100),
        # BANGUMI PGC_BANGUMI FILM TV GUOCHUANG DOCUMENTARY
        "EPS": [512, *range(4000, 4200)],
        # NONE MEDIA_LIST CHEESE_SERIES CHEESE_UPDATE
        "NONE": [2024, *range(4300, 4400)],
    }

    # extra parsers
    if f.origin_type in [
        *detail_types_list.get("MUSIC", []),
        *detail_types_list.get("VIDEO", []),
        *detail_types_list.get("LIVE", []),
        *detail_types_list.get("ARTICLE", []),
    ]:
        # au audio
        if f.origin_type in detail_types_list.get("MUSIC"):
            fu = await audio_parser(client, f'bilibili.com/audio/au{f.card.get("id")}')
            f.content = fu.content
        # live
        elif f.origin_type in detail_types_list.get("LIVE"):
            fu = await live_parser(client, f'live.bilibili.com/{f.card.get("roomid")}')
            f.content = fu.content
        # bv video
        elif f.origin_type in detail_types_list.get("VIDEO"):
            fu = await video_parser(client, f'b23.tv/av{f.card.get("aid")}')
            f.content = f.card.get("new_desc") if f.card.get("new_desc") else fu.content
        # article
        elif f.origin_type in detail_types_list.get("ARTICLE"):
            fu = await read_parser(client, f'bilibili.com/read/cv{f.card.get("id")}')
            f.content = fu.content
        else:
            fu = None
        if fu:
            f.user = fu.user
            f.uid = fu.uid
            f.extra_markdown = fu.extra_markdown
            f.mediathumb = fu.mediathumb
            f.mediatitle = fu.mediatitle
            f.mediaduration = fu.mediaduration
            f.mediaurls = fu.mediaurls
            f.mediatype = fu.mediatype
            f.mediaraws = fu.mediaraws
    # dynamic images/videos
    elif f.origin_type in [
        *detail_types_list.get("PIC", []),
        *detail_types_list.get("CLIP", []),
    ]:
        f.user = f.card.get("user").get("name")
        f.uid = f.card.get("user").get("uid")
        f.content = f'{f.card.get("item").get("title", str())}\n{f.card.get("item").get("description", str())}'
        add_on_card = f.add_on_card
        if len(add_on_card) != 0:
            f.extra_markdown = add_on_card[0].get("reserve_attach_card").get("title")
        if f.origin_type in detail_types_list.get("PIC"):
            f.mediaurls = [t.get("img_src") for t in f.card.get("item").get("pictures")]
            f.mediatype = "image"
        elif f.origin_type in detail_types_list.get("CLIP"):
            f.mediaurls = f.card.get("item").get("video_playurl")
            f.mediathumb = f.card.get("item").get("cover").get("unclipped")
            f.mediatype = "video"
    # dynamic text
    elif f.origin_type in detail_types_list.get("WORD"):
        f.user = f.card.get("user").get("uname")
        f.uid = f.card.get("user").get("uid")
        f.content = f.card.get("item").get("content")
    # share images
    elif f.origin_type in detail_types_list.get("SHARE"):
        f.user = f.card.get("user").get("uname")
        f.uid = f.card.get("user").get("uid")
        f.content = f.card.get("vest").get("content")
        f.extra_markdown = f"[{escape_markdown(f.card.get('sketch').get('title'))}\n{escape_markdown(f.card.get('sketch').get('desc_text'))}]({f.card.get('sketch').get('target_url')})"
        f.mediaurls = f.card.get("sketch").get("cover_url")
        f.mediatype = "image"
    else:
        logger.warning(ParserException(f"未知动态模板{f.origin_type}", f.url, f.card))
    # forward text
    if f.has_forward:
        f.forward_user = f.forward_card.get("user").get("uname")
        f.forward_uid = f.forward_card.get("user").get("uid")
        f.forward_content = f.forward_card.get("item").get("content")
    f.replycontent = await reply_parser(client, f.oid, f.reply_type)
    return f


@safe_parser
async def audio_parser(client: httpx.AsyncClient, url: str):
    if not (match := re.search(r"bilibili\.com\/audio\/au(\d+)", url)):
        raise ParserException("音频链接错误", url, match)
    f = audio(url)
    f.audio_id = int(match.group(1))
    if cache := await audio_cache.get_or_none(
        query := Q(audio_id=f.audio_id),
        Q(created__gte=timezone.now() - audio_cache.timeout),
    ):
        logger.info(f"拉取音频缓存: {cache.created}")
        f.infocontent = cache.content
        detail = f.infocontent["data"]
    else:
        r = await client.get(
            BILI_API + "/audio/music-service-c/songs/playing",
            params={"song_id": f.audio_id},
        )
        f.infocontent = r.json()
        if not (detail := f.infocontent.get("data")):
            raise ParserException("音频解析错误", r.url, f.infocontent)
    logger.info(f"音频ID: {f.audio_id}")
    if not cache:
        logger.info(f"音频缓存: {f.audio_id}")
        if cache := await audio_cache.get_or_none(query):
            cache.content = f.infocontent
            await cache.save(update_fields=["content", "created"])
        else:
            await audio_cache(audio_id=f.audio_id, content=f.infocontent).save()
    f.uid = detail.get("mid")
    r = await client.get(
        BILI_API + "/audio/music-service-c/url",
        params={
            "songid": f.audio_id,
            "mid": f.uid,
            "privilege": 2,
            "quality": 3,
            "platform": "",
        },
    )
    f.mediacontent = r.json()
    f.user = detail.get("author")
    f.content = detail.get("intro")
    f.extra_markdown = f"[{escape_markdown(detail.get('title'))}]({f.url})"
    f.mediathumb = detail.get("cover_url")
    f.mediatitle = detail.get("title")
    f.mediaduration = detail.get("duration")
    f.mediaurls = f.mediacontent.get("data").get("cdns")
    f.mediatype = "audio"
    f.mediaraws = True
    f.replycontent = await reply_parser(client, f.audio_id, f.reply_type)
    return f


@safe_parser
async def live_parser(client: httpx.AsyncClient, url: str):
    if not (match := re.search(r"live\.bilibili\.com[\/\w]*\/(\d+)", url)):
        raise ParserException("直播链接错误", url, match)
    f = live(url)
    f.room_id = int(match.group(1))
    if cache := await live_cache.get_or_none(
        query := Q(room_id=f.room_id),
        Q(created__gte=timezone.now() - live_cache.timeout),
    ):
        logger.info(f"拉取直播缓存: {cache.created}")
        f.rawcontent = cache.content
        detail = f.rawcontent.get("data")
    else:
        r = await client.get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
            params={"room_id": f.room_id},
        )
        f.rawcontent = r.json()
        if not (detail := f.rawcontent.get("data")):
            raise ParserException("直播解析错误", r.url, f.rawcontent)
    logger.info(f"直播ID: {f.room_id}")
    if not cache:
        logger.info(f"直播缓存: {f.room_id}")
        if cache := await live_cache.get_or_none(query):
            cache.content = f.rawcontent
            await cache.save(update_fields=["content", "created"])
        else:
            await live_cache(room_id=f.room_id, content=f.rawcontent).save()
    if not detail:
        raise ParserException("直播内容获取错误", f.url)
    f.user = detail["anchor_info"]["base_info"]["uname"]
    roominfo = detail.get("room_info")
    f.uid = roominfo.get("uid")
    f.content = f"{roominfo.get('title')} - {roominfo.get('area_name')} - {roominfo.get('parent_area_name')}"
    f.extra_markdown = f"[{escape_markdown(f.user)}的直播间]({f.url})"
    f.mediaurls = roominfo.get("keyframe")
    f.mediatype = "image"
    return f


@safe_parser
async def video_parser(client: httpx.AsyncClient, url: str):
    if not (
        match := re.search(
            r"(?i)(?:bilibili\.com/(?:video|bangumi/play)|b23\.tv|acg\.tv)/(?:(?P<bvid>bv\w+)|av(?P<aid>\d+)|ep(?P<epid>\d+)|ss(?P<ssid>\d+))",
            url,
        )
    ):
        raise ParserException("视频链接错误", url, match)
    f = video(url)
    if epid := match.group("epid"):
        params = {"ep_id": epid}
    elif bvid := match.group("bvid"):
        params = {"bvid": bvid}
    elif aid := match.group("aid"):
        params = {"aid": aid}
    elif ssid := match.group("ssid"):
        params = {"season_id": ssid}
    else:
        params = {}
    if "ep_id" in params or "season_id" in params:
        if cache := await bangumi_cache.get_or_none(
            query := Q(
                Q(epid=params.get("ep_id")),
                Q(ssid=params.get("season_id")),
                join_type="OR",
            ),
            Q(created__gte=timezone.now() - video_cache.timeout),
        ):
            logger.info(f"拉取番剧缓存: {cache.created}")
            f.infocontent = cache.content
        else:
            r = await client.get(
                BILI_API + "/pgc/view/web/season",
                params=params,
            )
            f.infocontent = r.json()
        if not (detail := f.infocontent.get("result")):
            # Anime detects non-China IP
            raise ParserException("番剧解析错误", url, f.infocontent)
        f.sid = detail.get("season_id")
        if epid:
            for episode in detail.get("episodes"):
                if str(episode.get("id")) == epid:
                    f.aid = episode.get("aid")
        if not f.aid:
            f.aid = detail.get("episodes")[-1].get("aid")
            epid = detail.get("episodes")[-1].get("id")
        logger.info(f"番剧ID: {epid}")
        if not cache:
            logger.info(f"番剧缓存: {epid}")
            if cache := await bangumi_cache.get_or_none(query):
                cache.content = f.infocontent
                await cache.save(update_fields=["content", "created"])
            else:
                await bangumi_cache(epid=epid, ssid=f.sid, content=f.infocontent).save()
        params = {"aid": f.aid}
    # elif "aid" in params or "bvid" in params:
    if cache := await video_cache.get_or_none(
        query := Q(
            Q(aid=params.get("aid")), Q(bvid=params.get("bvid")), join_type="OR"
        ),
        Q(created__gte=timezone.now() - video_cache.timeout),
    ):
        logger.info(f"拉取视频缓存: {cache.created}")
        f.infocontent = cache.content
        detail = f.infocontent.get("data")
    else:
        r = await client.get(
            BILI_API + "/x/web-interface/view",
            params=params,
        )
        # Video detects non-China IP
        f.infocontent = r.json()
        if not (detail := f.infocontent.get("data")):
            raise ParserException("视频解析错误", r.url, f.infocontent)
    if not detail:
        raise ParserException("视频内容获取错误", f.url)
    bvid = detail.get("bvid")
    f.aid = detail.get("aid")
    f.cid = detail.get("cid")
    logger.info(f"视频ID: {f.aid}")
    if not cache:
        logger.info(f"视频缓存: {f.aid}")
        if cache := await video_cache.get_or_none(query):
            cache.content = f.infocontent
            await cache.save(update_fields=["content", "created"])
        else:
            await video_cache(aid=f.aid, bvid=bvid, content=f.infocontent).save()
    f.user = detail.get("owner").get("name")
    f.uid = detail.get("owner").get("mid")
    f.content = detail.get("dynamic")
    f.extra_markdown = f"[{escape_markdown(detail.get('title'))}]({f.url})"
    f.mediatitle = detail.get("title")
    f.mediaurls = detail.get("pic")
    f.mediatype = "image"
    f.replycontent = await reply_parser(client, f.aid, f.reply_type)
    # r = await client.get(
    #     BILI_API+"/x/player/playurl",
    #     params={"avid": f.aid, "cid": f.cid, "fnval": 16},
    # )
    # f.mediacontent = r.json()
    # f.mediaurls = f.mediacontent.get("data").get("dash").get("video")[0].get("base_url")
    # f.mediathumb = detail.get("pic")
    # f.mediatype = "video"
    # f.mediaraws = True
    return f


@safe_parser
async def read_parser(client: httpx.AsyncClient, url: str):
    async def relink(img):
        src = img.attrs.pop("data-src")
        img.attrs = {}
        logger.info(f"下载图片: {src}")
        async with httpx.AsyncClient(
            headers=headers, http2=True, timeout=None, verify=False
        ) as client:
            r = await client.get(f"https:{src}")
            media = BytesIO(r.read())
            content_length = int(r.headers.get("content-length"))
            if content_length > 1024 * 1024 * 5:
                mediatype = r.headers.get("content-type")
                if mediatype in ["image/jpeg", "image/png"]:
                    logger.info(f"图片大小: {content_length} 压缩: {src} {mediatype}")
                    media = compress(media)
            r = await client.post(
                "https://telegra.ph/upload", files={"upload-file": media.getvalue()}
            )
            resp = r.json()
            if isinstance(resp, list):
                img.attrs["src"] = f"https://telegra.ph{resp[0].get('src')}"
            else:
                logger.warning(f"{src} -> {resp}")

    if not (match := re.search(r"bilibili\.com\/read\/(?:cv|mobile\/)(\d+)", url)):
        raise ParserException("文章链接错误", url, match)
    f = read(url)
    f.read_id = int(match.group(1))
    r = await client.get(f"https://www.bilibili.com/read/cv{f.read_id}")
    soup = BeautifulSoup(r.text, "lxml")
    uid_content = soup.find("a", class_="up-name")
    if not isinstance(uid_content, Tag) or not uid_content.attrs.get("href"):
        raise ParserException("文章uid解析错误", url, uid_content)
    f.uid = uid_content.attrs["href"].split("/")[-1]
    user_content = soup.find("meta", attrs={"name": "author"})
    if not isinstance(user_content, Tag) or not user_content.attrs.get("content"):
        raise ParserException("文章user解析错误", url, user_content)
    f.user = user_content.attrs["content"]
    content_content = soup.find("meta", attrs={"name": "description"})
    if not isinstance(content_content, Tag) or not content_content.attrs.get("content"):
        raise ParserException("文章content解析错误", url, content_content)
    f.content = content_content.attrs.get("content")
    mediaurls_content = soup.find("meta", property="og:image")
    if not isinstance(mediaurls_content, Tag):
        raise ParserException("文章mediaurls解析错误", url, mediaurls_content)
    mediaurls = mediaurls_content.attrs.get("content")
    if mediaurls:
        logger.info(f"文章mediaurls: {mediaurls}")
        f.mediaurls = mediaurls
        f.mediatype = "image"
    title_content = soup.find("meta", property="og:title")
    if not isinstance(title_content, Tag) or not title_content.attrs.get("content"):
        raise ParserException("文章title解析错误", url, title_content)
    title = title_content.attrs.get("content")
    logger.info(f"文章ID: {f.read_id}")
    if cache := await read_cache.get_or_none(
        query := Q(read_id=f.read_id),
        Q(created__gte=timezone.now() - audio_cache.timeout),
    ):
        logger.info(f"拉取文章缓存: {cache.created}")
        graphurl = cache.graphurl
    else:
        article = soup.find("div", class_="read-article-holder")
        if not isinstance(article, Tag):
            raise ParserException("文章article解析错误", url)
        imgs = article.find_all("img")
        task = list(relink(img) for img in imgs)  ## data-src -> src
        for _ in article.find_all("h1"):  ## h1 -> h3
            _.name = "h3"
        for _ in article.find_all("span"):  ## remove span
            _.unwrap()
        for s in ["p", "figure", "figcaption"]:  ## clean tags
            for _ in article.find_all(s):
                _.attrs = {}
        telegraph = Telegraph()
        await asyncio.gather(*task)
        result = "".join(
            [i.__str__() for i in article.contents]
        )  ## div rip off, cannot unwrap div so use str directly
        telegraph.create_account("bilifeedbot")
        graphurl = telegraph.create_page(
            title=title,
            html_content=result,
            author_name=f.user,
            author_url=f"https://space.bilibili.com/{f.uid}",
        ).get("url")
        logger.info(f"生成页面: {graphurl}")
        logger.info(f"文章缓存: {f.read_id}")
        if cache := await read_cache.get_or_none(query):
            cache.graphurl = graphurl
            await cache.save(update_fields=["graphurl", "created"])
        else:
            await read_cache(read_id=f.read_id, graphurl=graphurl).save()
    f.extra_markdown = f"[{escape_markdown(title)}]({graphurl})"
    f.replycontent = await reply_parser(client, f.read_id, f.reply_type)
    return f


@safe_parser
async def feed_parser(client: httpx.AsyncClient, url: str):
    r = await client.get(url)
    url = str(r.url)
    logger.debug(f"URL: {url}")
    # API link
    if re.search(r"api\..*\.bilibili", url):
        pass
    # dynamic
    elif re.search(r"[th]\.bilibili\.com", url):
        return await dynamic_parser(client, url)
    # live image
    elif re.search(r"live\.bilibili\.com", url):
        return await live_parser(client, url)
    # au audio
    elif re.search(r"bilibili\.com/audio", url):
        return await audio_parser(client, url)
    # au audio
    elif re.search(r"bilibili\.com/read", url):
        return await read_parser(client, url)
    # main video
    elif re.search(r"bilibili\.com/(?:video|bangumi/play)", url):
        return await video_parser(client, url)
    raise ParserException("URL错误", url)


def db_init(func):
    async def inner_function(*args, **kwargs):
        await Tortoise.init(
            db_url=os.environ.get("DATABASE_URL", "sqlite://cache.db"),
            modules={"models": ["database"]},
            use_tz=True,
        )
        await Tortoise.generate_schemas()
        result = await func(*args, **kwargs)
        await Tortoise.close_connections()
        return result

    return inner_function


@db_init
async def biliparser(urls):
    logger.debug(BILI_API)
    if isinstance(urls, str):
        urls = [urls]
    elif isinstance(urls, tuple):
        urls = list(urls)
    async with httpx.AsyncClient(
        headers=headers, http2=True, timeout=None, verify=False, follow_redirects=True
    ) as client:
        tasks = list(
            feed_parser(
                client,
                f"http://{url}" if not url.startswith(("http:", "https:")) else url,
            )
            for url in urls
        )
        callbacks = await asyncio.gather(*tasks)
    for num, f in enumerate(callbacks):
        if isinstance(f, Exception):
            logger.warning(f"排序: {num}\n异常: {f}\n")
        else:
            logger.debug(
                f"排序: {num}\n"
                f"类型: {type(f)}\n"
                f"链接: {f.url}\n"
                f"用户: {f.user_markdown}\n"
                f"内容: {f.content_markdown}\n"
                f"附加内容: {f.extra_markdown}\n"
                f"评论: {f.comment_markdown}\n"
                f"媒体: {f.mediaurls}\n"
                f"媒体种类: {f.mediatype}\n"
                f"媒体预览: {f.mediathumb}\n"
                f"媒体标题: {f.mediatitle}\n"
                f"媒体文件名: {f.mediafilename}"
            )
    return callbacks


async def __db_status():
    tasks = [item.all().count() for item in CACHES.values()]
    result = await asyncio.gather(*tasks)
    ans = ""
    for key, item in zip(CACHES.keys(), await asyncio.gather(*tasks)):
        ans += f"{key}: {item}\n"
    ans += f"总计: {sum(result)}"
    return ans


@db_init
async def db_status():
    return await __db_status()


@db_init
async def db_clear(target):
    if CACHES.get(target):
        await CACHES[target].filter(created__lt=datetime.today()).delete()
    return await __db_status()
