import io
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import webserver


STATE_FILE = Path("leaderboard_state.json")
DEFAULT_FORUM_NAME = "contract-board"
DEFAULT_LEADERBOARD_CHANNEL_NAME = "leaderboard"
DEFAULT_IGNORED_TAGS = {
	"done",
	"in progress",
	"not taken",
	"rejected",
	"ban",
	"overlimit",
	"warning",
}


def _env_int(name: str) -> Optional[int]:
	value = os.getenv(name)
	if value is None or not value.strip():
		return None
	try:
		return int(value)
	except ValueError:
		raise SystemExit(f"Environment variable {name} must be an integer.")


def _read_csv_env(name: str) -> set[str]:
	raw = os.getenv(name, "")
	return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def _get_tz() -> ZoneInfo:
	timezone_name = os.getenv("LEADERBOARD_TIMEZONE", "UTC")
	try:
		return ZoneInfo(timezone_name)
	except Exception as exc:
		raise SystemExit(f"Invalid LEADERBOARD_TIMEZONE '{timezone_name}': {exc}")


def _format_timestamp(value: Optional[datetime]) -> Optional[str]:
	if value is None:
		return None
	return value.isoformat()


def get_previous_month_window(now_local: datetime) -> tuple[datetime, datetime, str]:
	start_current_month_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
	end_previous_month_local = start_current_month_local

	if start_current_month_local.month == 1:
		start_previous_month_local = start_current_month_local.replace(
			year=start_current_month_local.year - 1,
			month=12,
		)
	else:
		start_previous_month_local = start_current_month_local.replace(
			month=start_current_month_local.month - 1,
		)

	start_previous_utc = start_previous_month_local.astimezone(timezone.utc)
	end_previous_utc = end_previous_month_local.astimezone(timezone.utc)
	label = start_previous_month_local.strftime("%B %Y")
	return start_previous_utc, end_previous_utc, label


async def fetch_all_archived_forum_posts(
	forum_channel: discord.ForumChannel,
	page_size: int = 100,
) -> list[discord.Thread]:
	before = None
	all_threads: list[discord.Thread] = []
	seen_ids: set[int] = set()

	while True:
		page: list[discord.Thread] = []
		async for thread in forum_channel.archived_threads(limit=page_size, before=before):
			if thread.id in seen_ids:
				continue
			page.append(thread)
			seen_ids.add(thread.id)

		if not page:
			break

		all_threads.extend(page)

		oldest_archive_timestamp = min(
			(
				thread.archive_timestamp
				or thread.created_at
				or discord.utils.snowflake_time(thread.id)
				for thread in page
			)
		)
		before = oldest_archive_timestamp - timedelta(microseconds=1)

		if len(page) < page_size:
			break

	all_threads.sort(
		key=lambda thread: (
			thread.archive_timestamp
			or thread.created_at
			or discord.utils.snowflake_time(thread.id)
		),
		reverse=True,
	)
	return all_threads


async def fetch_archived_forum_posts_for_created_window(
	forum_channel: discord.ForumChannel,
	start_utc: datetime,
	end_utc: datetime,
	page_size: int = 100,
) -> list[discord.Thread]:
	before = None
	matches: list[discord.Thread] = []
	seen_ids: set[int] = set()

	while True:
		page: list[discord.Thread] = []
		async for thread in forum_channel.archived_threads(limit=page_size, before=before):
			if thread.id in seen_ids:
				continue
			page.append(thread)
			seen_ids.add(thread.id)

		if not page:
			break

		for thread in page:
			created_at = thread.created_at or discord.utils.snowflake_time(thread.id)
			if created_at is None:
				continue
			if start_utc <= created_at < end_utc:
				matches.append(thread)

		oldest_archive_timestamp = min(
			(
				thread.archive_timestamp
				or thread.created_at
				or discord.utils.snowflake_time(thread.id)
				for thread in page
			)
		)
		newest_archive_timestamp = max(
			(
				thread.archive_timestamp
				or thread.created_at
				or discord.utils.snowflake_time(thread.id)
				for thread in page
			)
		)

		before = oldest_archive_timestamp - timedelta(microseconds=1)

		if newest_archive_timestamp < start_utc:
			break

		if len(page) < page_size:
			break

	matches.sort(
		key=lambda thread: thread.created_at or discord.utils.snowflake_time(thread.id),
		reverse=True,
	)
	return matches


@dataclass
class LeaderboardResult:
	month_label: str
	period_start_utc: datetime
	period_end_utc: datetime
	counts: Counter[str]
	total_contracts: int
	considered_threads: int
	unclaimed_threads: int


class ForumArchiveBot(commands.Bot):
	def __init__(
		self,
		guild_id: Optional[int],
		default_forum_channel_id: Optional[int],
		leaderboard_channel_id: Optional[int],
	) -> None:
		intents = discord.Intents.none()
		intents.guilds = True

		super().__init__(command_prefix="!", intents=intents)
		self.guild_id = guild_id
		self.default_forum_channel_id = default_forum_channel_id
		self.leaderboard_channel_id = leaderboard_channel_id
		self.leaderboard_channel_name = os.getenv(
			"LEADERBOARD_CHANNEL_NAME", DEFAULT_LEADERBOARD_CHANNEL_NAME
		)
		self.default_forum_channel_name = os.getenv(
			"CONTRACT_FORUM_CHANNEL_NAME", DEFAULT_FORUM_NAME
		)
		self.ignore_tags = DEFAULT_IGNORED_TAGS.union(_read_csv_env("LEADERBOARD_IGNORE_TAGS"))
		self.local_tz = _get_tz()

	async def setup_hook(self) -> None:
		if self.guild_id:
			guild = discord.Object(id=self.guild_id)
			self.tree.copy_global_to(guild=guild)
			await self.tree.sync(guild=guild)
		else:
			await self.tree.sync()

		self.monthly_leaderboard_loop.start()

	async def on_ready(self) -> None:
		print(f"Logged in as {self.user} (ID: {self.user.id})")

	def _load_state(self) -> dict:
		if not STATE_FILE.exists():
			return {"posted_months": {}}
		try:
			return json.loads(STATE_FILE.read_text(encoding="utf-8"))
		except json.JSONDecodeError:
			return {"posted_months": {}}

	def _save_state(self, state: dict) -> None:
		STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

	def _month_key_for_now(self, now_local: datetime) -> str:
		return now_local.strftime("%Y-%m")

	def has_posted_monthly_leaderboard(self, guild_id: int, now_local: datetime) -> bool:
		state = self._load_state()
		month_key = self._month_key_for_now(now_local)
		return state.get("posted_months", {}).get(str(guild_id)) == month_key

	def mark_monthly_leaderboard_posted(self, guild_id: int, now_local: datetime) -> None:
		state = self._load_state()
		posted_months = state.setdefault("posted_months", {})
		posted_months[str(guild_id)] = self._month_key_for_now(now_local)
		self._save_state(state)

	async def resolve_forum_channel(
		self,
		guild: discord.Guild,
		forum_channel_id: Optional[int] = None,
	) -> Optional[discord.ForumChannel]:
		channel_id = forum_channel_id or self.default_forum_channel_id

		channel: Optional[discord.abc.GuildChannel] = None
		if channel_id:
			candidate = guild.get_channel(channel_id)
			if candidate is None:
				try:
					candidate = await self.fetch_channel(channel_id)
				except discord.NotFound:
					candidate = None
			if isinstance(candidate, discord.ForumChannel):
				return candidate
			return None

		for guild_channel in guild.channels:
			if isinstance(guild_channel, discord.ForumChannel) and guild_channel.name == self.default_forum_channel_name:
				return guild_channel

		return None

	async def resolve_leaderboard_channel(
		self,
		guild: discord.Guild,
		leaderboard_channel_id: Optional[int] = None,
		create_if_missing: bool = True,
	) -> Optional[discord.TextChannel]:
		channel_id = leaderboard_channel_id or self.leaderboard_channel_id

		if channel_id:
			channel = guild.get_channel(channel_id)
			if channel is None:
				try:
					channel = await self.fetch_channel(channel_id)
				except discord.NotFound:
					channel = None
			return channel if isinstance(channel, discord.TextChannel) else None

		for channel in guild.text_channels:
			if channel.name == self.leaderboard_channel_name:
				return channel

		if not create_if_missing:
			return None

		me = guild.me
		if me and me.guild_permissions.manage_channels:
			return await guild.create_text_channel(
				name=self.leaderboard_channel_name,
				reason="Create leaderboard channel for monthly contract competition.",
			)

		return None

	def pick_claimant_from_tags(self, thread: discord.Thread) -> Optional[str]:
		if not thread.applied_tags:
			return None

		candidates = [
			tag.name.strip()
			for tag in thread.applied_tags
			if tag.name and tag.name.strip() and tag.name.strip().lower() not in self.ignore_tags
		]
		if not candidates:
			return None
		return candidates[0]

	async def build_previous_month_leaderboard(
		self,
		forum_channel: discord.ForumChannel,
		page_size: int = 100,
	) -> LeaderboardResult:
		now_local = datetime.now(self.local_tz)
		start_utc, end_utc, month_label = get_previous_month_window(now_local)

		active_threads = []
		for thread in forum_channel.threads:
			created_at = thread.created_at or discord.utils.snowflake_time(thread.id)
			if created_at is None:
				continue
			if start_utc <= created_at < end_utc:
				active_threads.append(thread)

		archived_threads = await fetch_archived_forum_posts_for_created_window(
			forum_channel,
			start_utc=start_utc,
			end_utc=end_utc,
			page_size=page_size,
		)

		all_threads_by_id: dict[int, discord.Thread] = {
			thread.id: thread for thread in [*active_threads, *archived_threads]
		}

		counts: Counter[str] = Counter()
		considered_threads = 0
		unclaimed_threads = 0

		for thread in all_threads_by_id.values():
			created_at = thread.created_at or discord.utils.snowflake_time(thread.id)
			if created_at is None:
				continue
			if not (start_utc <= created_at < end_utc):
				continue

			considered_threads += 1
			claimant = self.pick_claimant_from_tags(thread)
			if claimant is None:
				unclaimed_threads += 1
				continue
			counts[claimant] += 1

		return LeaderboardResult(
			month_label=month_label,
			period_start_utc=start_utc,
			period_end_utc=end_utc,
			counts=counts,
			total_contracts=considered_threads,
			considered_threads=considered_threads,
			unclaimed_threads=unclaimed_threads,
		)

	def render_leaderboard_message(
		self,
		forum_channel: discord.ForumChannel,
		result: LeaderboardResult,
	) -> str:
		header = [
			f"🏁 **Contract Leaderboard — {result.month_label}**",
			f"Forum: {forum_channel.mention}",
			f"Contracts posted: **{result.total_contracts}**",
			f"Unclaimed contracts: **{result.unclaimed_threads}**",
			"",
		]

		if not result.counts:
			body = ["No claimed contracts were found for this period yet."]
		else:
			rankings = result.counts.most_common()
			lines = []
			for index, (member_name, count) in enumerate(rankings, start=1):
				medal = ""
				if index == 1:
					medal = "🥇 "
				elif index == 2:
					medal = "🥈 "
				elif index == 3:
					medal = "🥉 "
				lines.append(f"{medal}**#{index}** {member_name} — **{count}** contracts")
			body = lines

		footer = ["", "Keep going everyone — new month, new race! 🚀"]
		return "\n".join([*header, *body, *footer])

	@tasks.loop(minutes=30)
	async def monthly_leaderboard_loop(self) -> None:
		now_local = datetime.now(self.local_tz)
		if now_local.day != 1:
			return

		guilds_to_check: list[discord.Guild] = []
		if self.guild_id:
			guild = self.get_guild(self.guild_id)
			if guild:
				guilds_to_check.append(guild)
		else:
			guilds_to_check.extend(self.guilds)

		for guild in guilds_to_check:
			if self.has_posted_monthly_leaderboard(guild.id, now_local):
				continue

			forum_channel = await self.resolve_forum_channel(guild)
			if forum_channel is None:
				print(f"[monthly_leaderboard] Could not find forum channel in guild {guild.id}.")
				continue

			leaderboard_channel = await self.resolve_leaderboard_channel(guild)
			if leaderboard_channel is None:
				print(f"[monthly_leaderboard] Could not resolve leaderboard channel in guild {guild.id}.")
				continue

			result = await self.build_previous_month_leaderboard(forum_channel)
			message = self.render_leaderboard_message(forum_channel, result)
			await leaderboard_channel.send(message)
			self.mark_monthly_leaderboard_posted(guild.id, now_local)

	@monthly_leaderboard_loop.before_loop
	async def before_monthly_leaderboard_loop(self) -> None:
		await self.wait_until_ready()


bot = ForumArchiveBot(
	guild_id="1409974092483788872",
	default_forum_channel_id="1409979594257465535",
	leaderboard_channel_id="1496828144982687948",
)


@bot.tree.command(
	name="archived_posts",
	description="Fetch all archived posts from a Discord forum channel.",
)
@app_commands.describe(
	forum_channel_id="Forum channel ID. Uses DISCORD_FORUM_CHANNEL_ID if omitted.",
	page_size="How many archived threads to request per page (1-100).",
)
async def archived_posts(
	interaction: discord.Interaction,
	page_size: app_commands.Range[int, 1, 100] = 100,
	forum_channel_id: Optional[int] = None,
) -> None:
	await interaction.response.defer(thinking=True)

	if interaction.guild is None:
		await interaction.followup.send("This command can only be used inside a server.")
		return

	channel = await bot.resolve_forum_channel(interaction.guild, forum_channel_id=forum_channel_id)
	if channel is None:
		await interaction.followup.send("Could not resolve the forum channel.")
		return

	threads = await fetch_all_archived_forum_posts(channel, page_size=page_size)

	if not threads:
		await interaction.followup.send(
			f"No archived posts found in forum `{channel.name}` (`{channel.id}`)."
		)
		return

	serialized_threads = [
		{
			"id": str(thread.id),
			"name": thread.name,
			"url": thread.jump_url,
			"owner_id": str(thread.owner_id) if thread.owner_id else None,
			"message_count": thread.message_count,
			"created_at": _format_timestamp(thread.created_at),
			"archive_timestamp": _format_timestamp(thread.archive_timestamp),
			"locked": thread.locked,
			"archived": thread.archived,
		}
		for thread in threads
	]

	payload = {
		"forum_channel": {
			"id": str(channel.id),
			"name": channel.name,
			"guild_id": str(channel.guild.id),
		},
		"total_archived_posts": len(serialized_threads),
		"page_size": page_size,
		"posts": serialized_threads,
	}

	json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
	file = discord.File(
		fp=io.BytesIO(json_bytes),
		filename=f"archived_posts_{channel.id}.json",
	)

	preview_count = min(10, len(threads))
	preview_lines = [
		f"{index + 1}. `{thread.name}` ({thread.id})"
		for index, thread in enumerate(threads[:preview_count])
	]
	preview_text = "\n".join(preview_lines)

	await interaction.followup.send(
		(
			f"Fetched **{len(threads)}** archived forum posts from `{channel.name}`.\n"
			f"Pagination size: `{page_size}` per request.\n"
			f"Preview (first {preview_count}):\n{preview_text}"
		),
		file=file,
	)


@bot.tree.command(
	name="post_monthly_leaderboard",
	description="Generate and post the previous-month contract leaderboard now.",
)
@app_commands.describe(
	forum_channel_id="Forum channel ID. Uses DISCORD_FORUM_CHANNEL_ID or CONTRACT_FORUM_CHANNEL_NAME if omitted.",
	leaderboard_channel_id="Text channel ID to post into. Uses LEADERBOARD_CHANNEL_ID or channel name if omitted.",
	page_size="Archived forum pagination size (1-100).",
)
async def post_monthly_leaderboard(
	interaction: discord.Interaction,
	page_size: app_commands.Range[int, 1, 100] = 100,
	forum_channel_id: Optional[int] = None,
	leaderboard_channel_id: Optional[int] = None,
) -> None:
	await interaction.response.defer(thinking=True)

	if interaction.guild is None:
		await interaction.followup.send("This command can only be used inside a server.")
		return

	forum_channel = await bot.resolve_forum_channel(interaction.guild, forum_channel_id=forum_channel_id)
	if forum_channel is None:
		await interaction.followup.send(
			"Could not find the forum channel. Set `DISCORD_FORUM_CHANNEL_ID` or `CONTRACT_FORUM_CHANNEL_NAME`."
		)
		return

	leaderboard_channel = await bot.resolve_leaderboard_channel(
		interaction.guild,
		leaderboard_channel_id=leaderboard_channel_id,
		create_if_missing=True,
	)
	if leaderboard_channel is None:
		await interaction.followup.send(
			"Could not find/create the leaderboard channel. Ensure bot can manage channels or set `LEADERBOARD_CHANNEL_ID`."
		)
		return

	result = await bot.build_previous_month_leaderboard(forum_channel, page_size=page_size)
	message = bot.render_leaderboard_message(forum_channel, result)
	await leaderboard_channel.send(message)

	await interaction.followup.send(
		f"Posted previous-month leaderboard to {leaderboard_channel.mention} for `{result.month_label}`."
	)


def main() -> None:
	token = os.getenv("DISCORD_BOT_TOKEN")
	if not token:
		raise SystemExit("Missing DISCORD_BOT_TOKEN environment variable.")
	bot.run(token)


if __name__ == "__main__":
	webserver.keep_alive()
	main()
