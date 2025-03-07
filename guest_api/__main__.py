import json
import logging
import re
from typing import Optional

import aiohttp
from aiohttp import web
import aioredis

logger = logging.getLogger(__name__)

ALEPH_API_SERVER = "https://api2.aleph.im"
ALEPH_VM_CONNECTOR = "http://localhost:4021"
CACHE_EXPIRES_AFTER = 7 * 24 * 3600  # Seconds


async def proxy(request: web.Request):
    path = request.match_info.get('tail').lstrip('/')
    query_string = request.rel_url.query_string
    url = f"{ALEPH_API_SERVER}/{path}?{query_string}"

    async with aiohttp.ClientSession() as session:
        async with session.request(method=request.method, url=url) as response:
            data = await response.read()
            return web.Response(body=data,
                                status=response.status,
                                content_type=response.content_type)


async def repost(request: web.Request):
    logger.debug("REPOST")
    data_raw = await request.json()
    topic, message = data_raw["topic"], json.loads(data_raw["data"])

    content = json.loads(message["item_content"])
    content["address"] = "VM on executor"
    message["item_content"] = json.dumps(content)

    new_data = {"topic": topic, "data": json.dumps(message)}

    path = request.path
    if request.rel_url.query_string:
        query_string = request.rel_url.query_string
        url = f"{ALEPH_VM_CONNECTOR}{path}?{query_string}"
    else:
        url = f"{ALEPH_VM_CONNECTOR}{path}"

    async with aiohttp.ClientSession() as session:
        async with session.post(url=url, json=new_data) as response:
            data = await response.read()
            return web.Response(body=data,
                                status=response.status,
                                content_type=response.content_type)


# async def decrypt_secret(request: web.Request):
#     Not implemented...


async def properties(request: web.Request):
    logger.debug("Forwarding signing properties")

    url = f"{ALEPH_VM_CONNECTOR}/properties"
    async with aiohttp.ClientSession() as session:
        async with session.get(url=url) as response:
            data = await response.read()
            return web.Response(body=data,
                                status=response.status,
                                content_type=response.content_type)


async def sign(request: web.Request):
    vm_hash = request.app.meta_vm_hash
    message = await request.json()

    # Ensure that the hash of the VM is used as sending address
    content = json.loads(message["item_content"])
    if content["address"] != vm_hash:
        raise web.HTTPBadRequest(reason="Message address does not match VM item_hash")

    logger.info("Forwarding signing request to VM Connector")

    url = f"{ALEPH_VM_CONNECTOR}/sign"
    async with aiohttp.ClientSession() as session:
        async with session.post(url=url, json=message) as response:
            signed_message = await response.read()
            return web.Response(body=signed_message,
                                status=response.status,
                                content_type=response.content_type)


async def get_from_cache(request: web.Request):
    prefix: str = request.app.meta_vm_hash
    key: str = request.match_info.get('key')
    if not re.match(r'^\w+$', key):
        return web.HTTPBadRequest(text="Invalid key")

    redis: aioredis.Redis = await aioredis.create_redis(address="redis://localhost")
    body = await redis.get(f"{prefix}:{key}")
    if body:
        return web.Response(body=body, status=200)
    else:
        return web.Response(text="No such key in cache", status=404)


async def put_in_cache(request: web.Request):
    prefix: str = request.app.meta_vm_hash
    key: str = request.match_info.get('key')
    if not re.match(r'^\w+$', key):
        return web.HTTPBadRequest(text="Invalid key")

    value: bytes = await request.read()

    redis: aioredis.Redis = await aioredis.create_redis(address="redis://localhost")
    return web.json_response(await redis.set(f"{prefix}:{key}", value,
                                             expire=CACHE_EXPIRES_AFTER))


async def delete_from_cache(request: web.Request):
    prefix: str = request.app.meta_vm_hash
    key: str = request.match_info.get('key')
    if not re.match(r'^\w+$', key):
        return web.HTTPBadRequest(text="Invalid key")

    redis: aioredis.Redis = await aioredis.create_redis(address="redis://localhost")
    result = await redis.delete(f"{prefix}:{key}")
    return web.json_response(result)


async def list_keys_from_cache(request: web.Request):
    prefix: str = request.app.meta_vm_hash
    pattern: str = request.rel_url.query.get('pattern', '*')
    if not re.match(r'^[\w?*^\-]+$', pattern):
        return web.HTTPBadRequest(text="Invalid key")

    redis: aioredis.Redis = await aioredis.create_redis(address="redis://localhost")
    result = await redis.keys(f"{prefix}:{pattern}")
    keys = [
        key.decode()[len(prefix)+1:]
        for key in result
    ]
    return web.json_response(keys)


def run_guest_api(unix_socket_path, vm_hash: Optional[str] = None):
    app = web.Application()
    app.meta_vm_hash = vm_hash or '_'

    app.router.add_route(method='GET', path='/properties', handler=properties)
    app.router.add_route(method='POST', path='/sign', handler=sign)

    app.router.add_route(method='GET', path='/cache/', handler=list_keys_from_cache)
    app.router.add_route(method='GET', path='/cache/{key:.*}', handler=get_from_cache)
    app.router.add_route(method='PUT', path='/cache/{key:.*}', handler=put_in_cache)
    app.router.add_route(method='DELETE', path='/cache/{key:.*}', handler=delete_from_cache)

    app.router.add_route(method='GET', path='/{tail:.*}', handler=proxy)
    app.router.add_route(method='HEAD', path='/{tail:.*}', handler=proxy)
    app.router.add_route(method='OPTIONS', path='/{tail:.*}', handler=proxy)

    app.router.add_route(method='POST', path='/api/v0/ipfs/pubsub/pub', handler=repost)
    app.router.add_route(method='POST', path='/api/v0/p2p/pubsub/pub', handler=repost)

    # web.run_app(app=app, port=9000)
    web.run_app(app=app, path=unix_socket_path)


if __name__ == '__main__':
    run_guest_api("/tmp/guest-api", vm_hash='vm')
