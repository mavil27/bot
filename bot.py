import asyncio
from collections import deque
from dataclasses import dataclass

import discord
from discord.ext import commands
from discord import app_commands
import wavelink

import os

LAVALINK_HOST = os.getenv("LAVALINK_HOST")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
IDLE_TIMEOUT_SEC = int(os.getenv("IDLE_TIMEOUT_SEC", "120"))

# -------------------------
# 길드별 상태(큐/idle/lock)
# -------------------------
@dataclass
class GuildMusicState:
    queue: deque[wavelink.Playable]
    idle_task: asyncio.Task | None
    lock: asyncio.Lock

states: dict[int, GuildMusicState] = {}

def get_state(guild_id: int) -> GuildMusicState:
    st = states.get(guild_id)
    if st is None:
        st = GuildMusicState(queue=deque(), idle_task=None, lock=asyncio.Lock())
        states[guild_id] = st
    return st

async def cancel_idle(st: GuildMusicState):
    if st.idle_task and not st.idle_task.done():
        st.idle_task.cancel()
    st.idle_task = None

async def schedule_idle_disconnect(player: wavelink.Player, st: GuildMusicState):
    await cancel_idle(st)

    async def _idle():
        try:
            await asyncio.sleep(IDLE_TIMEOUT_SEC)
            # 타이머 후에도 재생 없고 큐 비었으면 퇴장
            if (not player.playing) and (not st.queue):
                await player.disconnect()
        except asyncio.CancelledError:
            pass

    st.idle_task = asyncio.create_task(_idle())

async def resolve_track(query: str) -> wavelink.Playable:
    # 링크면 그대로, 아니면 유튜브 검색
    if query.startswith("http://") or query.startswith("https://"):
        tracks = await wavelink.Playable.search(query)
    else:
        tracks = await wavelink.Playable.search(f"ytsearch:{query}")

    if not tracks:
        raise app_commands.AppCommandError("트랙을 찾지 못했습니다.")
    return tracks[0]


# -------------------------
# Discord / Wavelink setup
# -------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not wavelink.Pool.nodes:
        await wavelink.Pool.connect(
            client=bot,
            nodes=[
                wavelink.Node(
                    uri=f"http://{LAVALINK_HOST}:{LAVALINK_PORT}",
                    password=LAVALINK_PASSWORD,
                )
            ],
        )
        print("Connected to Lavalink")

    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash commands")


# -------------------------
# 곡 종료 이벤트: 다음 곡 재생 / idle 시작
# -------------------------
@bot.listen("on_wavelink_track_end")
async def on_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if not player.guild:
        return

    st = get_state(player.guild.id)
    async with st.lock:
        if st.queue:
            nxt = st.queue.popleft()
            await player.play(nxt)
        else:
            await schedule_idle_disconnect(player, st)


# -------------------------
# 유틸: 음성채널 연결 보장
# -------------------------
async def ensure_player(interaction: discord.Interaction) -> wavelink.Player:
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise app_commands.AppCommandError("멤버 정보를 확인할 수 없습니다.")

    if not interaction.user.voice or not interaction.user.voice.channel:
        raise app_commands.AppCommandError("먼저 음성채널에 들어가 주세요.")

    vc = interaction.user.voice.channel

    player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
    if player is None:
        player = await vc.connect(cls=wavelink.Player)
    else:
        if player.channel and player.channel.id != vc.id:
            await player.move_to(vc)

    return player


# -------------------------
# Commands
# -------------------------
@bot.tree.command(name="play", description="유튜브 링크/검색어를 재생하거나 대기열에 추가합니다.")
@app_commands.describe(query="유튜브 링크 또는 검색어")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    player = await ensure_player(interaction)
    st = get_state(interaction.guild.id)

    async with st.lock:
        await cancel_idle(st)

        track = await resolve_track(query)

        if player.playing:
            st.queue.append(track)
            await interaction.followup.send(f"✅ 대기열 추가: **{track.title}** (총 {len(st.queue)}곡)")
        else:
            await player.play(track)
            await interaction.followup.send(f"▶️ 재생 시작: **{track.title}**")


@bot.tree.command(name="queue", description="대기열을 보여줍니다.")
async def queue_cmd(interaction: discord.Interaction):
    st = get_state(interaction.guild.id)
    if not st.queue:
        await interaction.response.send_message("대기열이 비어있습니다.")
        return

    lines = []
    for i, t in enumerate(list(st.queue)[:20], start=1):
        lines.append(f"{i}. {t.title}")
    msg = "🎶 **대기열**\n" + "\n".join(lines)
    if len(st.queue) > 20:
        msg += f"\n... (총 {len(st.queue)}곡)"
    await interaction.response.send_message(msg)


@bot.tree.command(name="remove", description="대기열에서 특정 번호의 곡을 삭제합니다.")
@app_commands.describe(index="삭제할 곡 번호(1부터)")
async def remove(interaction: discord.Interaction, index: int):
    st = get_state(interaction.guild.id)
    async with st.lock:
        if index < 1 or index > len(st.queue):
            await interaction.response.send_message("인덱스가 범위를 벗어났습니다.", ephemeral=True)
            return
        q = list(st.queue)
        removed = q.pop(index - 1)
        st.queue = deque(q)

    await interaction.response.send_message(f"🗑️ 삭제됨: **{removed.title}**")


@bot.tree.command(name="skip", description="현재 곡을 스킵합니다.")
async def skip(interaction: discord.Interaction):
    player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
    if player is None or not player.playing:
        await interaction.response.send_message("재생 중인 곡이 없습니다.", ephemeral=True)
        return

    # stop() -> on_track_end에서 다음곡 처리됨
    await player.stop()
    await interaction.response.send_message("⏭️ 스킵했습니다.")


@bot.tree.command(name="stop", description="재생을 중지하고 대기열을 비웁니다(자동퇴장 타이머 시작).")
async def stop(interaction: discord.Interaction):
    player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
    if player is None:
        await interaction.response.send_message("봇이 음성채널에 없습니다.", ephemeral=True)
        return

    st = get_state(interaction.guild.id)
    async with st.lock:
        st.queue.clear()
        if player.playing:
            await player.stop()
        await schedule_idle_disconnect(player, st)

    await interaction.response.send_message(
        f"⏹️ 중지 & 대기열 초기화. {IDLE_TIMEOUT_SEC}초 동안 명령 없으면 자동 퇴장합니다."
    )


@bot.tree.command(name="leave", description="봇을 음성채널에서 내보냅니다.")
async def leave(interaction: discord.Interaction):
    player: wavelink.Player | None = interaction.guild.voice_client  # type: ignore
    if player is None:
        await interaction.response.send_message("봇이 음성채널에 없습니다.", ephemeral=True)
        return

    st = get_state(interaction.guild.id)
    async with st.lock:
        st.queue.clear()
        await cancel_idle(st)
        await player.disconnect()

    await interaction.response.send_message("👋 음성채널에서 나갔습니다.")


# 에러 핸들러(유저에게 깔끔하게)
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = str(error)
    if interaction.response.is_done():
        await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # 사람이 음성채널을 옮기거나 나갈 때마다 호출됨

    # 봇 자신의 상태 변화는 무시(무한루프/불필요 트리거 방지)
    if member.bot:
        return

    guild = member.guild
    player: wavelink.Player | None = guild.voice_client  # type: ignore
    if player is None or player.channel is None:
        return

    vc = player.channel  # 봇이 현재 붙어있는 음성채널

    # 이번 업데이트가 "봇이 있는 채널"과 무관하면 무시
    # (예: 다른 채널에서 나간 것)
    if before.channel != vc and after.channel != vc:
        return

    # 봇이 있는 채널에 남아있는 "사람(봇 제외)" 수 체크
    humans_left = sum(1 for m in vc.members if not m.bot)

    if humans_left == 0:
        st = get_state(guild.id)
        async with st.lock:
            st.queue.clear()
            await cancel_idle(st)   # idle 타이머 있으면 취소
            # 재생중이면 멈추고 나가기(선호에 따라 stop 생략 가능)
            try:
                if player.playing:
                    await player.stop()
            finally:
                await player.disconnect()


bot.run(DISCORD_TOKEN)
