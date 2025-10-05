import os
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
import asyncio
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    TOKYO = ZoneInfo("Asia/Tokyo")
except Exception:
    TOKYO = None
import schedule
import time

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ATTENDANCE_MESSAGE_ID = int(os.getenv("ATTENDANCE_MESSAGE_ID", "0"))
ATTENDANCE_RECORD_CHANNEL_ID = int(os.getenv("ATTENDANCE_RECORD_CHANNEL_ID", "0"))
ATTENDANCE_ROLE_ID = int(os.getenv("ATTENDANCE_ROLE_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("attendance-bot")

intents = discord.Intents.default()
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- ガードフラグ（on_ready が複数回呼ばれる対策） ---
_ready_once = False

# --- keepalive の定義は起動前に ---
async def keepalive_task():
    try:
        while True:
            logger.info("keep alive ok!")
            await asyncio.sleep(5)  # テスト用: 5秒。運用時は 180 に
    except asyncio.CancelledError:
        # シャットダウン時にタスクがキャンセルされるとここに来る
        logger.info("keepalive_taskがキャンセルされました")
        raise
    except Exception:
        logger.exception("keepalive_taskが例外で終了しました")

# --- タスクの作成と例外追跡 ---
def create_task_with_logging(coro):
    task = asyncio.create_task(coro)
    def _on_done(t):
        try:
            exc = t.exception()
            if exc:
                logger.exception("バックグラウンドタスクがクラッシュしました")
        except asyncio.CancelledError:
            pass
    task.add_done_callback(_on_done)
    return task

# --- ✅絵文字か判定する ---
def is_check_mark(emoji) -> bool:
    # payload.emoji は名前を持つ場合と単純な文字列の場合があるので両方対応
    return getattr(emoji, "name", str(emoji)) in ("✅", "\u2705")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        # 0) Bot 自身のリアクションは無視する
        if payload.user_id == bot.user.id:
            logger.debug("Ignoring reaction from the bot itself.")
            return

        # 1) attendance_message_id と一致しないなら無視（早期リターン）
        if ATTENDANCE_MESSAGE_ID == 0:
            logger.warning("ATTENDANCE_MESSAGE_ID not set (0). Set it in .env to enable attendance processing.")
            return

        if payload.message_id != ATTENDANCE_MESSAGE_ID:
            logger.debug("メッセージ %s は出席メッセージ (%s) ではありません",payload.message_id, ATTENDANCE_MESSAGE_ID)
            return

        # 2) ✅ 以外の絵文字は無視
        if not is_check_mark(payload.emoji):
            return
        # 3) 出席処理を実行
        await handle_attendance_reaction(payload)

    except Exception:
        logger.exception("例外が発生しました")

#--- bot から channel を安全に取得する ---
async def fetch_channel_safe(bot, channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        logger.exception("チャンネル %s を取得できませんでした", channel_id)
        return None

#--- guild から member を安全に取得する ---
async def fetch_member_safe(guild: discord.Guild, user_id: int):
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        try:
            return await bot.fetch_user(user_id)
        except Exception:
            logger.exception("ギルド %s のメンバー/ユーザー %s を取得できませんでした", user_id, getattr(guild, "id", None))
            return None

async def mark_user_attendance(member: discord.abc.Snowflake, role: discord.Role, record_channel: discord.TextChannel) -> bool:
    """
    member に role を付与し、記録チャンネルにタイムスタンプ付きで投稿する。
    既に role がある場合はFalse を返す。
    """
    try:
        if isinstance(member, discord.Member) and role in member.roles:
            return False
        #ロールを追加
        if isinstance(member, discord.Member):
            await member.add_roles(role, reason="botによって登録された出席")
        else:
            logger.warning("メンバーではないユーザーにロールを追加しようとしました: %s", getattr(member, "id", None))
            return False

        now = datetime.now(TOKYO) if TOKYO else datetime.now()
        timestr = now.strftime("%Y年%m月%d日 %H:%M")
        text = f"{member.mention} が **{timestr}** に出席しました。"
        await record_channel.send(text)
        logger.info(" %s に出席ロールを付与しました", member.id)
        return True
    except discord.Forbidden:
        logger.exception("ロール %s を %s に追加するための権限がありません", getattr(role, "id", None), getattr(member, "id", None))
        return False
    except Exception:
        logger.exception("不明な原因により %s の出席を記録できませんでした", getattr(member, "id", None))
        return False
# payload の発火を受け、attendance message の ✅ を付けている全ユーザー（bot を除く）に対してまだロールがなければロールを付与記録チャンネルに「@ユーザーがYYYY年MM月DD日 HH:MMに出席しました。」を送信を行い、最後に payload を発火させた本人（payload.user_id）のリアクションを削除します。
async def handle_attendance_reaction(payload: discord.RawReactionActionEvent):
    try:
        # --- 基本チェック（冗長でも安全） ---
        if ATTENDANCE_MESSAGE_ID == 0:
            logger.warning("ATTENDANCE_MESSAGE_IDが設定されていません。.")
            return

        if payload.message_id != ATTENDANCE_MESSAGE_ID:
            logger.debug("出席メッセージ %s ではありません。",
                         payload.message_id, ATTENDANCE_MESSAGE_ID)
            return

        if not is_check_mark(payload.emoji):
            logger.debug("絵文字 %s はチェックマークではありません", payload.emoji)
            return

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            logger.error("ギルド %s がキャッシュ内に見つかりません", payload.guild_id)
            return

        role = guild.get_role(ATTENDANCE_ROLE_ID)
        if role is None:
            logger.error("出席ロール ID %s がギルド %s に見つかりません", ATTENDANCE_ROLE_ID, guild.id)
            return

        record_channel = await fetch_channel_safe(bot, ATTENDANCE_RECORD_CHANNEL_ID)
        if record_channel is None or not isinstance(record_channel, discord.TextChannel):
            logger.error("出席記録チャンネルID %sが見つからないか、テキストチャンネルではありません", ATTENDANCE_RECORD_CHANNEL_ID)
            return

        channel = await fetch_channel_safe(bot, payload.channel_id)
        if channel is None:
            logger.error("チャンネル %s が見つかりません", payload.channel_id)
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            logger.exception("チャネル %s のメッセージ %s を取得できませんでした", payload.message_id, channel.id)
            return

        target_reaction = None
        for react in message.reactions:
            if is_check_mark(react.emoji):
                target_reaction = react
                break
        # もしリアクションが見つからなければ終了
        if target_reaction is None:
            return
        # --- ✅リアクションを付けているユーザーを列挙 ---
        try:
            users = [u async for u in target_reaction.users()]
        except Exception:
            logger.exception("メッセージ %s へのユーザー応答の反復に失敗しました", message.id)
            users = []

        # 各ユーザーを処理する
        for user in users:
            if getattr(user, "bot", False):
                continue

            # メンバーを取得
            member = await fetch_member_safe(guild, user.id)
            if member is None:
                logger.warning("ギルド %s のメンバー %s を取得できませんでした。", user.id, guild.id)
                continue

            # すでにロールがある場合はスキップ
            if isinstance(member, discord.Member) and role in member.roles:
                logger.debug("メンバー %s はすでに出席ロールを付与されています", member.id)
                continue
            # 出席を記録
            ok = await mark_user_attendance(member, role, record_channel)
            # APIのレートリミット対策
            await asyncio.sleep(1)
        # リアクションの削除
        try:
            reactor_member = await fetch_member_safe(guild, payload.user_id)
            if reactor_member is None:
                reactor_user = await bot.fetch_user(payload.user_id)
            else:
                reactor_user = reactor_member

            await message.remove_reaction(payload.emoji, reactor_user)
        except discord.Forbidden:
            logger.exception("チャンネル %s のリアクションを削除する権限がありません", channel.id)
        except Exception:
            logger.exception("メッセージ %s に対するユーザー %s のリアクションを削除できませんでした", payload.user_id, message.id)

    except Exception:
        logger.exception("handle_attendance_reaction で例外が発生しました")

# 毎日深夜0時に出席ロールを全員からはく奪する
async def remove_attendance_roles():
    try:
        # Botが参加している最初のギルドを取得（1つだけ運用想定）
        if not bot.guilds:
            logger.warning("botはどのギルドにも所属していません")
            return

        guild = bot.guilds[0]
        role = guild.get_role(ATTENDANCE_ROLE_ID)

        if role is None:
            logger.error("ロール %s が見つかりません", ATTENDANCE_ROLE_ID)
            return

        members_with_role = [m for m in guild.members if role in m.roles]

        if not members_with_role:
            logger.info("出席ロールを持つメンバーがいませんでした")
            return

        logger.info("%d 人のメンバーの出席記録の削除を開始", len(members_with_role))

        for member in members_with_role:
            try:
                await member.remove_roles(role, reason="毎日の出席リセット")
                logger.info("%s から出席役割を削除しました", member.name)
            except discord.Forbidden:
                logger.error("%s からロールを削除する権限がありません", member.name)
            except Exception as e:
                logger.exception("%s からロールを削除中にエラーが発生しました: %s", member.name, e)

            # API制限回避のための5秒スリープ
            await asyncio.sleep(5)

        logger.info("出席ロールの削除が完了しました")

    except Exception:
        logger.exception("remove_attendance_rolesで例外が発生しました")

async def schedule_task():
    schedule.every().day.at("00:00").do(
        lambda: asyncio.create_task(remove_attendance_roles())
    )

    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

@bot.event
async def on_ready():
    global _ready_once
    if _ready_once:
        logger.info("on_readyが再度呼び出されたためスキップします")
        return
    _ready_once = True
    logger.info("Bot is ready: %s (id=%s)", bot.user, bot.user.id)

    # バックグラウンドタスクを起動（create_task_with_logging を使って例外追跡）
    create_task_with_logging(keepalive_task())
    create_task_with_logging(schedule_task())

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set")
        raise SystemExit("Set DISCORD_TOKEN in .env")
    bot.run(DISCORD_TOKEN)
