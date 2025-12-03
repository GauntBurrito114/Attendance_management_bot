import os
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
from datetime import datetime
import json

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
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
WELCOME_CONFIG_FILE = "welcome_config.json"
DEFAULT_WELCOME_MESSAGE = "ようこそ {mention} さん！\nルールを確認してニックネームを変更してください！"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("attendance-bot")
intents = discord.Intents.default()
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix="/", intents=intents)
app = Flask(__name__)
last_processed = {}
_ready_once = False

# --- JSON設定 ---
def load_welcome_message_config():
    if not os.path.exists(WELCOME_CONFIG_FILE):
        return DEFAULT_WELCOME_MESSAGE
    try:
        with open(WELCOME_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("message", DEFAULT_WELCOME_MESSAGE)
    except Exception as e:
        logger.error(f"設定読み込みエラー: {e}")
        return DEFAULT_WELCOME_MESSAGE

def save_welcome_message_config(message: str):
    try:
        with open(WELCOME_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"message": message}, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        logger.error(f"設定保存エラー: {e}")
        return False

# --- Web Server (UptimeRobot用) ---
@app.route('/')
def home():
    # Botの状態も返すように変更
    if bot.is_ready():
        return "Discord bot is running and connected!", 200
    else:
        return "Discord bot is running but NOT connected to Discord.", 503

def run_web():
    port = int(os.environ.get("PORT", 10000))
    # use_reloader=Falseで二重起動防止
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# daemon=Trueにすることでメインプロセス終了時に道連れにする
threading.Thread(target=run_web, daemon=True).start()

# --- Watchdog (修正版) ---
#本当に死んでいるときだけログを出すように変更
async def watchdog_task():
    while True:
        await asyncio.sleep(600)  # 10分ごとにチェック
        if bot.is_closed():
            logger.warning("Bot is disconnected! (Auto-reconnect should handle this)")
        elif not bot.is_ready():
             logger.warning("Bot is connected but not ready yet.")

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

# --- 絵文字を判別 ---
def is_check_mark(emoji) -> bool:
    return getattr(emoji, "name", str(emoji)) in ("✅", "\u2705")

async def fetch_channel_safe(bot, channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch: return ch
    try: return await bot.fetch_channel(channel_id)
    except: return None

# --- 出席管理 ---
async def mark_user_attendance(member: discord.abc.Snowflake, role: discord.Role, record_channel: discord.TextChannel) -> bool:
    try:
        if isinstance(member, discord.Member) and role in member.roles:
            return False
        
        if isinstance(member, discord.Member):
            await member.add_roles(role, reason="bot出席")
        
        now = datetime.now(TOKYO) if TOKYO else datetime.now()
        timestr = now.strftime("%Y年%m月%d日 %H:%M")
        text = f"{member.mention} が **{timestr}** に出席しました。"
        await record_channel.send(text)
        logger.info(" %s に出席ロールを付与しました", member.id)
        return True
    except Exception:
        logger.exception("出席記録エラー")
        return False

async def handle_attendance_reaction(payload):
    guild = bot.get_guild(payload.guild_id)
    if not guild: return

    now = asyncio.get_event_loop().time()
    if payload.user_id in last_processed and now - last_processed[payload.user_id] < 5:
        return
    last_processed[payload.user_id] = now

    channel = guild.get_channel(payload.channel_id)
    if not channel: return

    try:
        message = channel.get_partial_message(payload.message_id)
        try: message = await message.fetch()
        except: return
    except: return

    if payload.user_id == bot.user.id: return
    member = guild.get_member(payload.user_id)
    if not member: return

    attendance_role = guild.get_role(ATTENDANCE_ROLE_ID)
    record_channel = guild.get_channel(ATTENDANCE_RECORD_CHANNEL_ID)

    if not attendance_role or not record_channel: return
    if attendance_role in member.roles: return

    try:
        await member.add_roles(attendance_role, reason="出席確認")
        now_str = datetime.now(TOKYO).strftime("%Y年%m月%d日 %H:%M:%S") if TOKYO else datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
        await record_channel.send(f"{member.mention} が {now_str} に出席しました。")
        await message.remove_reaction(payload.emoji, member)
        await asyncio.sleep(1) # API制限回避
    except Exception as e:
        logger.exception("出席処理エラー: %s", e)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        if payload.user_id == bot.user.id: return
        if ATTENDANCE_MESSAGE_ID == 0 or payload.message_id != ATTENDANCE_MESSAGE_ID: return
        if not is_check_mark(payload.emoji): return
        await handle_attendance_reaction(payload)
    except Exception:
        logger.exception("例外が発生しました")

async def remove_attendance_roles():
    try:
        if not bot.guilds: return
        guild = bot.guilds[0]
        role = guild.get_role(ATTENDANCE_ROLE_ID)
        if not role: return

        members_with_role = [m for m in guild.members if role in m.roles]
        if not members_with_role: return

        logger.info("%d 人のロール削除開始", len(members_with_role))

        for member in members_with_role:
            try:
                await member.remove_roles(role, reason="日次リセット")
            except Exception as e:
                logger.error("%s ロール削除失敗: %s", member.name, e)
            
            # 10秒は長すぎた
            await asyncio.sleep(1.5)

        logger.info("ロール削除完了")

    except Exception:
        logger.exception("remove_attendance_roles例外")

async def schedule_task():
    # 毎日0時に実行
    schedule.every().day.at("00:00").do(
        lambda: asyncio.create_task(remove_attendance_roles())
    )
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

@bot.event
async def on_member_join(member: discord.Member):
    try:
        if WELCOME_CHANNEL_ID == 0: return
        channel = await fetch_channel_safe(bot, WELCOME_CHANNEL_ID)
        if not channel: return

        raw_message = load_welcome_message_config()
        formatted_message = raw_message.replace("{mention}", member.mention)\
                                       .replace("{name}", member.display_name)\
                                       .replace("{server}", member.guild.name)
        await channel.send(formatted_message)
    except Exception:
        logger.exception("on_member_joinエラー")

# --- コマンド ---
@bot.tree.command(name="test", description="テスト")
async def slash_test(interaction: discord.Interaction):
    await interaction.response.send_message("テストOK")

@bot.tree.command(name="attendance", description="指定したメンバーに出席を付与します（管理者権限が必要です）")
@app_commands.describe(member="出席を付与するメンバー")
async def slash_attendance(interaction: discord.Interaction, member: discord.Member):
    # 実行権限の確認
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("このコマンドを実行する権限がありません", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("ギルドコンテキストで実行してください", ephemeral=True)
        return

    role = guild.get_role(ATTENDANCE_ROLE_ID)
    record_channel = await fetch_channel_safe(bot, ATTENDANCE_RECORD_CHANNEL_ID)

    if role is None:
        await interaction.response.send_message("出席ロールが見つかりません", ephemeral=True)
        return
    if record_channel is None or not isinstance(record_channel, discord.TextChannel):
        await interaction.response.send_message("記録チャンネルが見つかりません", ephemeral=True)
        return

    if role in member.roles:
        await interaction.response.send_message(f"{member.display_name} は既に出席ロールを持っています", ephemeral=True)
        return

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

@bot.tree.command(name="stop", description="Botを停止します（管理者権限が必要です）")
async def slash_stop(interaction: discord.Interaction):
    # 実行権限の確認
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("このコマンドを実行する権限がありません", ephemeral=True)
        return

    # まずユーザーに応答してからシャットダウン予約
    try:
        await interaction.response.send_message("ボットを停止します", ephemeral=True)
        logger.info("Shutdown requested by %s (%s)", interaction.user, interaction.user.id)
        asyncio.create_task(_shutdown_bot_after_delay(2.0))
    except Exception:
        logger.exception("Failed to send shutdown response; forcing shutdown immediately")
        # 最後の手段で即座に停止
        try:
            await bot.close()
        finally:
            os._exit(0)

async def _shutdown_bot_after_delay(delay_seconds: float = 2.0):
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

@bot.tree.command(name="set_welcome_message", description="ウェルカムメッセージを設定します（管理者権限が必要です）")
@app_commands.describe(content="内容")
async def slash_set_welcome(interaction: discord.Interaction, content: str):
    if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
        await interaction.response.send_message("権限がありません")
        return
    if save_welcome_message_config(content):
        await interaction.response.send_message(f"保存しました:\n{content}")
    else:
        await interaction.response.send_message("保存に失敗しました 再度実行してください")

@bot.tree.command(name="test_welcome", description="ウェルカムメッセージテスト")
async def slash_test_welcome(interaction: discord.Interaction):
    if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
        await interaction.response.send_message("このコマンドを実行する権限がありません", ephemeral=True)
        return
    msg = load_welcome_message_config().replace("{mention}", interaction.user.mention).replace("{name}", interaction.user.display_name).replace("{server}", interaction.guild.name)
    await interaction.response.send_message(msg, ephemeral=True)

@bot.event
async def on_ready():
    global _ready_once
    if _ready_once: return
    _ready_once = True
    logger.info("準備完了: %s", bot.user)
    
    create_task_with_logging(watchdog_task())
    create_task_with_logging(schedule_task())
    try:
        await bot.tree.sync()
        logger.info("コマンドツリーを同期しました")
    except Exception:
        logger.exception("コマンドツリーの同期に失敗しました")  

#bot起動
if __name__ == "__main__":
    if not DISCORD_TOKEN: raise SystemExit("No Token")
    bot.run(DISCORD_TOKEN)