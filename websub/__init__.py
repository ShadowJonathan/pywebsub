import asyncio
import datetime
import hashlib
import logging
import os
from dataclasses import dataclass
from functools import partial
from typing import Dict, Callable, Optional, Awaitable

import bs4
import httpx
import sanic
from anyio import create_task_group
from sanic.request import Request
from sanic.response import text

logger = logging.getLogger("websub")


class HubNotFoundException(Exception):
    pass


@dataclass
class Subscription:
    hub: str
    topic: str
    hex_id: str

    handler: Callable[[bytes], Awaitable[None]]

    verified_at: Optional[datetime.datetime] = None
    lease: Optional[datetime.timedelta] = None

    cold: bool = True

    def __repr__(self) -> str:
        return f"<Subscription {self.topic} (0x{self.hex_id.upper()} {self.lease})>"


class WebSubClient:
    def __init__(self, https: bool, app: sanic.Sanic, server: str = None, _from: str = None):
        self.server = server
        self.https = https
        self.app = app
        self._from = f"{_from or 'default'} (pywebsub)"
        self.running = False
        self.subscriptions: Dict[str, Subscription] = {}
        self.seen_hashes = set()

    async def subscribe(self, hub: str, topic: str, handler: Callable[[bytes], Awaitable[None]]):
        s = Subscription(hub=hub,
                         topic=topic,
                         hex_id=os.urandom(16).hex(),
                         handler=handler)
        self.subscriptions[topic] = s
        if self.running:
            s.cold = False
            await self.make_request(s)

    async def unsubscribe(self, topic: str):
        sub = self.subscriptions.get(topic)
        if sub is None:
            logger.warning(f"Unsubscribe requested for {topic}, but subscription does not exist.")
        else:
            if self.running:
                await self.make_request(sub, "unsubscribe")
            del self.subscriptions[topic]

    async def discover(self, topic: str) -> str:
        try:
            resp = await httpx.get(topic, headers=self.headers)
        except httpx.exceptions.HTTPError as e:
            logger.exception(f"{topic} discovery request failed with exception:")
            raise HubNotFoundException(e)

        if resp.status_code != 202:
            raise HubNotFoundException(f"{topic} discovery request failed, status is {resp.status_code}")
        else:
            logger.debug(f"{topic} discovery request succeeded")
            soup = bs4.BeautifulSoup(resp.content, "xml")
            link = soup.find("link", rel="hub")
            if link is None:
                raise HubNotFoundException(f"rel=hub link not present in {topic}")

            return link.attrs['href']

    async def discover_and_sub(self, topic: str, handler: Callable[[bytes], Awaitable[None]]):
        hub = await self.discover(topic)
        await self.subscribe(hub, topic, handler)

    async def make_request(self, sub: Subscription, mode: str = "subscribe"):
        callback_url = self.make_callback_url(sub.hex_id)
        logger.info(f"Subscribing to {sub.topic}, calling back to {callback_url}")

        values = {
              "hub.callback": callback_url,
              "hub.topic":    sub.topic,
              "hub.mode":     "subscribe"
        }

        try:
            resp = await httpx.post(sub.hub, data=values, headers=self.headers)
        except httpx.exceptions.HTTPError:
            logger.exception(f"{mode} request failed with exception, {sub} at {callback_url}:")
            return

        if resp.status_code != 202:
            logger.error(f"{mode} request failed, {sub} at {callback_url}, status is {resp.status_code}")
        else:
            logger.info(f"{mode} request succeeded, {sub} at {callback_url}")

    def make_callback_url(self, hex_id: str) -> str:
        return self.app.url_for('websub.callback', hex_id=hex_id,
                                _scheme='https' if self.https else 'http',
                                _external=True, _server=self.server)

    def get_sub_by_hex(self, hex_id: str) -> Optional[Subscription]:
        for s in self.subscriptions.values():
            if s.hex_id.lower() == hex_id.lower():
                return s

    async def broadcast(self, sub: Subscription, req: Request):
        body: bytes = req.body
        h: bytes = hashlib.md5(req.body).digest()
        if h not in self.seen_hashes:
            self.seen_hashes.add(h)
            await sub.handler(body)
        else:
            logger.info(f"Update for {sub} already handled, hash is {h.hex()}")

    @property
    def bp(self) -> sanic.Blueprint:
        bp = sanic.Blueprint("websub")

        @bp.route("/push-callback/<hex_id>")
        async def callback(request: Request, hex_id: str) -> sanic.response.HTTPResponse:
            topic = request.args.get("hub.topic")
            mode = request.args.get("hub.mode")

            if mode == "subscribe":
                sub = self.subscriptions.get(topic)
                if sub is None:
                    logger.warning(f"Unexpected subscription for {topic} on {hex_id}")
                    return text("Unexpected subscription", status=400)
                else:
                    lease_s = request.args.get('hub.lease_seconds')
                    if not lease_s:
                        logger.error(f"Lease is not resolved for sub {sub}: {lease_s!r}")
                        return text("No lease", status=400)
                    sub.verified_at = datetime.datetime.now()
                    sub.lease = datetime.timedelta(seconds=int(lease_s))
                    logger.info(f"Subscription verified for {sub}, lease is {sub.lease}")
                    if hex_id != sub.hex_id:
                        logger.warning(f"Subscription hex does not match path hex: {sub.hex_id} != {hex_id}")
                    return text(request.args.get("hub.challenge"))

            elif mode == "unsubscribe":
                sub = self.subscriptions.get(topic)
                if sub is None:
                    logger.warning(f"Unexpected unsubscribe for {topic} on {hex_id}")
                    return text("Unexpected unsubscribe", status=400)
                else:
                    logger.info(f"Unsubscribe confirmed for {topic} on {hex_id}")
                    if hex_id != sub.hex_id:
                        logger.warning(f"Subscription hex does not match path hex: {sub.hex_id} != {hex_id}")
                    return text(request.args.get("hub.challenge"))

            elif mode == "denied":
                logger.warning(f"Subscription denied for {topic}, reason was {request.args.get('hub.reason')}")
                return sanic.response.HTTPResponse()
                # TODO: Don't do anything for now, should probably mark the subscription.
            else:
                sub = self.get_sub_by_hex(hex_id)
                if sub is None:
                    logger.warning("Got unknown message for unknown subscription:")
                    logger.warning(f"{hex_id}: {request}")
                    return text("Unknown subscription", status=400)
                else:
                    logger.info(f"Update for {sub}")
                    await self.broadcast(sub, request)
                    return sanic.response.HTTPResponse()

        return bp

    @property
    def headers(self):
        return {
              "From": self._from
        }

    def install(self):
        bp = self.bp
        if bp.name not in self.app.blueprints:
            self.app.blueprint(self.bp)

    async def boot(self, *args, **kwargs):
        create_server = partial(self.app.create_server, *args, **kwargs, return_asyncio_server=True)

        async with create_task_group() as tg:
            await tg.spawn(create_server)
            await tg.spawn(self.start)

    async def start(self):
        self.running = True
        self.install()
        logger.debug("waiting 30 seconds before starting ensure_subbed_loop...")
        await asyncio.sleep(30)
        await self.ensure_subbed_loop()

    async def ensure_subbed_loop(self):
        while True:
            try:
                await self.ensure_subbed()
            except Exception:
                logger.exception("ensure_subbed gave an exception")

            await asyncio.sleep(60)

    async def ensure_subbed(self):
        logger.debug("ensure_subbed called")
        in_one_hour = datetime.datetime.now() + datetime.timedelta(hours=1)
        for s in self.subscriptions.values():
            logger.debug(f"ensure_subbed: checking {s}")
            if not s.cold and (s.verified_at is None or s.lease is None):
                logger.debug(f"ensure_subbed: {s} not yet verified, skipping...")
                continue
            if s.cold or (s.verified_at + s.lease) < in_one_hour:
                s.cold = False
                await self.make_request(s)
            else:
                logger.debug(f"ensure_subbed: {s} does not need new lease, verified at {s.verified_at}")
