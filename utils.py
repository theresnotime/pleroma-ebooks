# SPDX-License-Identifier: AGPL-3.0-only

import sys
import anyio
import aiohttp
import contextlib
from functools import wraps

def as_corofunc(f):
	@wraps(f)
	async def wrapped(*args, **kwargs):
		# can't decide if i want an `anyio.sleep(0)` here.
		return f(*args, **kwargs)
	return wrapped

def as_async_cm(cls):
	@wraps(cls, updated=())  # cls.__dict__ doesn't support .update()
	class wrapped(cls, contextlib.AbstractAsyncContextManager):
		__aenter__ = as_corofunc(cls.__enter__)
		__aexit__ = as_corofunc(cls.__exit__)
	return wrapped

suppress = as_async_cm(contextlib.suppress)

def shield(f):
	@wraps(f)
	async def shielded(*args, **kwargs):
		with anyio.CancelScope(shield=True):
			return await f(*args, **kwargs)
	return shielded

def removeprefix(s, prefix):
	try:
		return s.removeprefix(prefix)
	except AttributeError:
		# compatibility for pre-3.9
		return s[len(prefix):] if s.startswith(prefix) else s

def http_session_factory(headers={}, **kwargs):
	py_version = '.'.join(map(str, sys.version_info))
	user_agent = (
		'pleroma-ebooks (https://github.com/ioistired/pleroma-ebooks); '
		f'aiohttp/{aiohttp.__version__}; '
		f'python/{py_version}'
	)
	return aiohttp.ClientSession(
		headers={'User-Agent': user_agent, **headers},
		**kwargs,
	)
