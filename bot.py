import os
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    TOKYO = ZoneInfo("Asia/Tokyo")
except Exception:
    TOKYO = None
import schedule
from flask import Flask
import threading
import sys

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
app = Flask(__name__)
last_processed = {}

@app.route('/')
def home():
    return "Discord bot is running on Render!"

def run_web():
    port = int(os.environ.get("PORT", 10000))  # Renderが環境変数PORTを設定する
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web).start()

# --- ガードフラグ（on_ready が複数回呼ばれる対策） ---
_ready_once = False

# --- keepalive の定義は起動前に ---
async def watchdog_task():
    while True:
        await asyncio.sleep(300)  # 5分ごとに監視
        if not bot.is_closed():
            continue
        try:
            logger.warning("Bot seems disconnected. Restarting process.")
            os._exit(1)  # Renderが自動再起動
        except Exception:
            pass

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
async def handle_attendance_reaction(payload):
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        logger.warning("Guildが見つかりませんでした: %s", payload.guild_id)
        return

    # 5秒以内の同一ユーザーの反応は無視してAPI負荷を軽減
    now = asyncio.get_event_loop().time()
    if payload.user_id in last_processed and now - last_processed[payload.user_id] < 5:
        logger.info("短時間での再反応をスキップ: user_id=%s", payload.user_id)
        return
    last_processed[payload.user_id] = now

    # 各オブジェクトを取得
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        logger.error("チャンネルが見つかりません: %s", payload.channel_id)
        return

    # キャッシュにある場合はfetchせず使用
    try:
        message = channel.get_partial_message(payload.message_id)
        try:
            message = await message.fetch()
        except discord.errors.HTTPException as e:
            logger.warning("メッセージ取得をスキップ: %s", e)
            return
    except Exception as e:
        logger.error("メッセージオブジェクトの取得に失敗: %s", e)
        return

    # bot自身のリアクションは無視
    if payload.user_id == bot.user.id:
        return

    # ユーザー・ロール・チャンネルの取得
    member = guild.get_member(payload.user_id)
    if not member:
        logger.warning("メンバーが見つかりません: %s", payload.user_id)
        return

    attendance_role_id = int(os.getenv("ATTENDANCE_ROLE_ID", "0"))
    record_channel_id = int(os.getenv("ATTENDANCE_RECORD_CHANNEL_ID", "0"))
    attendance_role = guild.get_role(attendance_role_id)
    record_channel = guild.get_channel(record_channel_id)

    if not attendance_role or not record_channel:
        logger.error("ロールまたは記録チャンネルが見つかりません")
        return

    # すでにロールを持っていたら処理をスキップ
    if attendance_role in member.roles:
        logger.info("%s はすでに出席ロールを持っています。処理をスキップします。", member.name)
        return

    try:
        # 出席ロール付与
        await member.add_roles(attendance_role, reason="出席確認")
        now = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
        await record_channel.send(f"{member.mention} が {now} に出席しました。")
        logger.info("出席を記録: %s", member.name)

        # ✅リアクションを削除して次の処理へ
        await message.remove_reaction(payload.emoji, member)

        # 少し待って次の処理へ（Discord API 負荷軽減）
        await asyncio.sleep(1)

    except discord.Forbidden:
        logger.error("権限が不足しています。ロールを付与できません。")
    except discord.HTTPException as e:
        logger.error("出席処理中にHTTPエラー: %s", e)
    except Exception as e:
        logger.exception("出席処理中に予期せぬエラーが発生しました: %s", e)

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
            await asyncio.sleep(10)

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



# --- botのコマンドたち　---


# --- /testコマンド ---
@bot.tree.command(name="test", description="テストメッセージを送ります")
async def slash_test(interaction: discord.Interaction):
    try:
        await interaction.response.send_message("テストメッセージです。")
        logger.info("/test が %s によって実行されました (%s)", interaction.user, interaction.user.id)
    except Exception:
        logger.exception("/testの実行に失敗しました")


# --- /attendance コマンド ---
@bot.tree.command(name="attendance", description="指定したメンバーに出席を付与します（管理者権限が必要）")
@app_commands.describe(member="出席を付与するメンバー")
async def slash_attendance(interaction: discord.Interaction, member: discord.Member):
    # 実行権限の確認
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("このコマンドを実行する権限がありません（Manage Roles が必要）", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("ギルドコンテキストで実行してください。", ephemeral=True)
        return

    # ATTENDANCE_ROLE_ID / ATTENDANCE_RECORD_CHANNEL_ID は .env から読み込んである前提
    role = guild.get_role(ATTENDANCE_ROLE_ID)
    record_channel = await fetch_channel_safe(bot, ATTENDANCE_RECORD_CHANNEL_ID)

    if role is None:
        await interaction.response.send_message("出席ロールが見つかりません", ephemeral=True)
        return
    if record_channel is None or not isinstance(record_channel, discord.TextChannel):
        await interaction.response.send_message("記録チャンネルが見つかりません", ephemeral=True)
        return

    # 既にロールがあるかチェック
    if role in member.roles:
        await interaction.response.send_message(f"{member.display_name} は既に出席ロールを持っています", ephemeral=True)
        return

    # 実際の処理：mark_user_attendance を再利用
    try:
        ok = await mark_user_attendance(member, role, record_channel)
        if ok:
            await interaction.response.send_message(f"{member.display_name} に出席を付与しました", ephemeral=True)
            logger.info("/attendance: %s に出席付与を実行しました by %s", member.id, interaction.user.id)
        else:
            await interaction.response.send_message("ロール付与に失敗しました", ephemeral=True)
    except Exception:
        logger.exception("Exception in /attendance command")
        await interaction.response.send_message("エラーが発生しました", ephemeral=True)


#--- /stop コマンド ---
async def _shutdown_bot_after_delay(delay_seconds: float = 2.0):
    """内部で使うシャットダウンヘルパー（少し遅らせて応答を送らせてから停止）"""
    await asyncio.sleep(delay_seconds)
    try:
        logger.info("Shutting down bot (closing)...")
        await bot.close()
    except Exception:
        logger.exception("Error while closing bot")
    finally:
        logger.info("Exiting process now")
        # os._exit や sys.exit を使ってプロセスを強制終了
        try:
            os._exit(0)
        except Exception:
            sys.exit(0)

@bot.tree.command(name="stop", description="Bot を停止します（管理者のみ）")
async def slash_stop(interaction: discord.Interaction):
    # 実行権限の確認
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("このコマンドを実行する権限がありません（Manage Roles が必要）", ephemeral=True)
        return

    # まずユーザーに応答してからシャットダウン予約
    try:
        await interaction.response.send_message("ボットを停止します。数秒後にプロセスを終了します。", ephemeral=True)
        logger.info("Shutdown requested by %s (%s)", interaction.user, interaction.user.id)
        asyncio.create_task(_shutdown_bot_after_delay(2.0))
    except Exception:
        logger.exception("Failed to send shutdown response; forcing shutdown immediately")
        # 最後の手段で即座に停止
        try:
            await bot.close()
        finally:
            os._exit(0)

@bot.event
async def on_ready():
    global _ready_once
    if _ready_once:
        logger.info("on_readyが再度呼び出されたためスキップします")
        return
    _ready_once = True
    logger.info("Bot is ready: %s (id=%s)", bot.user, bot.user.id)

    # バックグラウンドタスクを起動（create_task_with_logging を使って例外追跡）
    create_task_with_logging(watchdog_task())  
    create_task_with_logging(schedule_task()) 
    try:
        await bot.tree.sync()
        logger.info("コマンドツリーを同期しました")
    except Exception:
        logger.exception("コマンドツリーの同期に失敗しました")        
        
           
# --- Botの起動 ---
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN is not set")
        raise SystemExit("Set DISCORD_TOKEN in .env")
    bot.run(DISCORD_TOKEN)
