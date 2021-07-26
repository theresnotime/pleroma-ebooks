#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only

import sys
import anyio
import aiohttp
import platform
import pendulum
import aiosqlite
import contextlib
from yarl import URL
from utils import shield
from pleroma import Pleroma
from bs4 import BeautifulSoup
from functools import partial
from third_party.utils import extract_post_content

USER_AGENT = (
	'fedi-ebooks; '
	f'{aiohttp.__version__}; '
	f'{platform.python_implementation()}/{platform.python_version()}; '
)

UTC = pendulum.timezone('UTC')
JSON_CONTENT_TYPE = 'application/json'
ACTIVITYPUB_CONTENT_TYPE = 'application/activity+json'

class PostFetcher:
	def __init__(self, *, config):
		self.config = config

	async def __aenter__(self):
		stack = contextlib.AsyncExitStack()
		self._fedi = await stack.enter_async_context(
			Pleroma(api_base_url=self.config['site'], access_token=self.config['access_token']),
		)
		self._http = await stack.enter_async_context(
			aiohttp.ClientSession(
				headers={
					'User-Agent': USER_AGENT,
					'Accept': ', '.join([JSON_CONTENT_TYPE, ACTIVITYPUB_CONTENT_TYPE]),
				},
				trust_env=True,
				raise_for_status=True,
			),
		)
		self._db = await stack.enter_async_context(aiosqlite.connect(self.config.get('db_path', 'posts.db')))
		self._db.row_factory = aiosqlite.Row
		self._ctx_stack = stack
		return self

	async def __aexit__(self, *excinfo):
		return await self._ctx_stack.__aexit__(*excinfo)

	async def fetch_all(self):
		await self._fedi.verify_credentials()
		self._completed_accounts = {}
		async with anyio.create_task_group() as tg:
			for acc in await self._fedi.following():
				tg.start_soon(self._do_account, acc)

	async def _do_account(self, acc):
		async with anyio.create_task_group() as tg:
			self._completed_accounts[acc['fqn']] = done_ev = anyio.Event()
			tx, rx = anyio.create_memory_object_stream()
			async with rx, tx:
				tg.start_soon(self._process_pages, rx, acc)
				tg.start_soon(self._fetch_account, tx, acc)
				await done_ev.wait()
			# processing is complete, so halt fetching.
			# processing may complete before fetching if we get caught up on new posts.
			tg.cancel_scope.cancel()

	async def _process_pages(self, stream, account):
		done_ev = self._completed_accounts[account['fqn']]
		try:
			async for activity in stream:
				try:
					await self._insert_activity(activity)
				except aiosqlite.IntegrityError as exc:
					# LOL sqlite error handling is so bad
					if exc.args[0].startswith('UNIQUE constraint failed: '):
						# this means we've encountered an item we already have saved
						done_ev.set()
						break

					raise
		finally:
			print('COMMIT')
			await self._db.commit()

	async def _insert_activity(self, activity):
		if activity['type'] != 'Create':
			# this isn't a post but something else (like, boost, reaction, etc)
			return

		obj = activity['object']

		content = extract_post_content(obj['content'])
		await self._db.execute(
			"""
			INSERT INTO posts (post_id, summary, content, published_at)
			VALUES (?, ?, ?, ?)
			""",
			(
				obj['id'],
				obj['summary'],
				extract_post_content(obj['content']),
				pendulum.parse(obj['published']).astimezone(pendulum.timezone('UTC')).timestamp(),
			),
		)

	@shield
	async def _fetch_account(self, tx, account):
		done_ev = self._completed_accounts[account['fqn']]

		try:
			outbox = await self.fetch_outbox(account['fqn'])
		except Exception as exc:
			import traceback
			traceback.print_exception(type(exc), exc, exc.__traceback__)
			return

		print(f'Fetching posts for {account["acct"]}...')

		next_page_url = outbox['first']
		while True:
			print(f'Fetching {next_page_url}... ', end='', flush=True)
			async with self._http.get(next_page_url) as resp: page = await resp.json()
			print('done.')

			for activity in page['orderedItems']:
				try:
					await tx.send(activity)
				except anyio.BrokenResourceError:
					# already closed means we're already done
					return

			# show progress
			#print('.', end='', flush=True)

			if not (next_page_url := page.get('next')):
				#done_ev.set()
				break

		done_ev.set()

	async def fetch_outbox(self, handle):
		"""finger handle, a fully-qualified ActivityPub actor name, returning their outbox URL"""
		# it's fucking incredible how overengineered ActivityPub is btw
		print('Fingering ', handle, '...', sep='')

		username, at, instance = handle.lstrip('@').partition('@')
		assert at == '@'

		# i was planning on doing /.well-known/host-meta to find the webfinger URL, but
		# 1) honk does not support host-meta
		# 2) WebFinger is always located at the same location anyway

		profile_url = await self._finger_actor(username, instance)

		try:
			async with self._http.get(profile_url) as resp: profile = await resp.json()
		except aiohttp.ContentTypeError:
			# we didn't get JSON, so just guess the outbox URL
			outbox_url = profile_url + '/outbox'
		else:
			outbox_url = profile['outbox']

		async with self._http.get(outbox_url) as resp: outbox = await resp.json()
		assert outbox['type'] == 'OrderedCollection'
		return outbox

	async def _finger_actor(self, username, instance):
		# despite HTTP being a direct violation of the WebFinger spec, assume e.g. Tor instances do not support
		# HTTPS-over-onion
		finger_url = f'http://{instance}/.well-known/webfinger?resource=acct:{username}@{instance}'
		async with self._http.get(finger_url) as resp: finger_result = await resp.json()
		return (profile_url := self._parse_webfinger_result(username, instance, finger_result))

	def _parse_webfinger_result(self, username, instance, finger_result):
		"""given webfinger data, return profile URL for handle"""
		def check_content_type(type, ct): return ct == type or ct.startswith(type+';')
		check_ap = partial(check_content_type, ACTIVITYPUB_CONTENT_TYPE)

		try:
			# note: the server might decide to return multiple links
			# so we need to decide how to prefer one.
			# i'd put "and URL(template).host == instance" here,
			# but some instances have no subdomain for the handle yet use a subdomain for the canonical URL.
			# Additionally, an instance could theoretically serve profile pages over I2P and the clearnet,
			# for example.
			return (profile_url := next(
				link['href']
				for link in finger_result['links']
				if link['rel'] == 'self' and check_ap(link['type'])
			))
		except StopIteration:
			# this should never happen either
			raise RuntimeError(f'fatal: while fingering {username}@{instance}, failed to find a profile URL')

async def amain():
	import json5 as json
	with open('config.json' if len(sys.argv) < 2 else sys.argv[1]) as f: config = json.load(f)
	async with PostFetcher(config=config) as fetcher: await fetcher.fetch_all()

def main():
	anyio.run(amain)

if __name__ == '__main__':
	main()