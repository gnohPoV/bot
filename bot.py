Hiểu rồi! Bot mới = code mới hoàn toàn, chỉ slash commands, bao gồm cả Spotify Connect v0.1. Không prefix, không lệnh cũ.

---

📄 File slash_bot.py – Spotify Connect Slash-Only

```python
"""
SPOTIFY CONNECT SLASH BOT
==========================
Bot chỉ dùng slash commands (/), không prefix.
Bao gồm Spotify Connect (giống v0.1) + vài lệnh test.

Sync lên guild (nhanh):
    Điền GUILD_ID = ID server của bạn

Chạy:
    py slash_bot.py

Yêu cầu:
    pip install discord.py
    discord_token.txt
    librespot (cùng thư mục hoặc PATH)
    ffmpeg (PATH)
    Spotify Premium
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import sys
import json
import subprocess
import shutil
import threading
import queue
import re
import logging
from pathlib import Path

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# =====================================================================
# CẤU HÌNH
# =====================================================================
# ĐIỀN ID SERVER CỦA BẠN để sync slash nhanh (vài giây).
# Để 0 = sync global (mất tới 1 giờ).
GUILD_ID = 0


# =====================================================================
# ĐỌC TOKEN DISCORD
# =====================================================================
try:
    with open('discord_token.txt', 'r') as f:
        DISCORD_TOKEN = f.read().strip()
        if not DISCORD_TOKEN:
            raise ValueError("Token rỗng")
except FileNotFoundError:
    logger.error("❌ Không tìm thấy discord_token.txt!")
    sys.exit(1)
except ValueError as e:
    logger.error(f"❌ Lỗi token: {e}")
    sys.exit(1)


# =====================================================================
# CONFIG FILE
# =====================================================================
CONFIG_FILE = "config.json"
default_config = {
    "spotify_device_name": "Discord Bot",
    "spotify_bitrate": 320,
    "spotify_cache_dir": "./.spotify-cache",
    "spotify_client_id": "",
    "spotify_client_secret": ""
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(default_config, f, indent=2, ensure_ascii=False)
    logger.info(f"⚠️ Đã tạo {CONFIG_FILE} mặc định.")
    return default_config

config = load_config()
DEVICE_NAME = config.get("spotify_device_name", "Discord Bot")
BITRATE = config.get("spotify_bitrate", 320)
CACHE_DIR = config.get("spotify_cache_dir", "./.spotify-cache")
SPOTIFY_CLIENT_ID = config.get("spotify_client_id", "")
SPOTIFY_CLIENT_SECRET = config.get("spotify_client_secret", "")


# =====================================================================
# TÌM LIBRESPOT
# =====================================================================
def find_librespot():
    for name in ["librespot.exe", "librespot"]:
        local = Path(name)
        if local.exists():
            return str(local.resolve())
    return shutil.which("librespot")

LIBRESPOT_PATH = find_librespot()


# =====================================================================
# SPOTIFY AUDIO SOURCE
# =====================================================================
class SpotifyAudioSource(discord.AudioSource):
    FRAME_SIZE = 3840  # 20ms @ 48kHz stereo s16le

    def __init__(self, device_name: str, bitrate: int, on_auth_url=None):
        self.device_name = device_name
        self.bitrate = bitrate
        self.librespot = None
        self.ffmpeg = None
        self.buffer = queue.Queue(maxsize=200)
        self.running = False
        self.reader_thread = None
        self.auth_thread = None
        self.on_auth_url = on_auth_url
        self.needs_auth = False
        self.last_auth_url = None

    def start(self):
        if not LIBRESPOT_PATH:
            raise RuntimeError("Không tìm thấy librespot.")

        os.makedirs(CACHE_DIR, exist_ok=True)
        creds_file = os.path.join(CACHE_DIR, "credentials.json")
        self.needs_auth = not os.path.exists(creds_file)

        librespot_cmd = [
            LIBRESPOT_PATH,
            "--name", self.device_name,
            "--backend", "pipe",
            "--bitrate", str(self.bitrate),
            "--cache", CACHE_DIR,
            "--disable-discovery"
        ]
        if SPOTIFY_CLIENT_ID:
            librespot_cmd += ["--client-id", SPOTIFY_CLIENT_ID]
        if SPOTIFY_CLIENT_SECRET:
            librespot_cmd += ["--client-secret", SPOTIFY_CLIENT_SECRET]

        logger.info(f"🎵 librespot: {' '.join(librespot_cmd)}")
        self.librespot = subprocess.Popen(
            librespot_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if self.on_auth_url:
            self.auth_thread = threading.Thread(
                target=self._auth_reader_loop, daemon=True
            )
            self.auth_thread.start()

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-ar", "48000", "-ac", "2",
            "pipe:1"
        ]
        self.ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=self.librespot.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        self.running = True
        self.reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True
        )
        self.reader_thread.start()
        logger.info("✅ Spotify source sẵn sàng.")

    def _auth_reader_loop(self):
        url_pattern = re.compile(rb'https://accounts\.spotify\.com/\S+')
        buf = b""
        while self.running:
            try:
                chunk = self.librespot.stderr.read(256)
                if not chunk:
                    break
                buf += chunk
                m = url_pattern.search(buf)
                if m:
                    url = m.group(0).decode('utf-8', errors='ignore').strip()
                    self.last_auth_url = url
                    logger.info(f"🔐 OAuth URL: {url}")
                    if self.on_auth_url:
                        try:
                            self.on_auth_url(url)
                        except Exception as e:
                            logger.error(f"on_auth_url error: {e}")
                    buf = b""
            except Exception as e:
                logger.error(f"Auth reader error: {e}")
                break

    def _reader_loop(self):
        while self.running:
            try:
                data = self.ffmpeg.stdout.read(self.FRAME_SIZE)
                if not data or len(data) < self.FRAME_SIZE:
                    logger.warning("⚠️ ffmpeg stream kết thúc")
                    break
                try:
                    self.buffer.put(data, timeout=0.1)
                except queue.Full:
                    pass
            except Exception as e:
                logger.error(f"Reader error: {e}")
                break

    def read(self) -> bytes:
        if self.librespot and self.librespot.poll() is not None:
            return b''
        if not self.running:
            return b''
        try:
            return self.buffer.get(timeout=0.02)
        except queue.Empty:
            return b'\x00' * self.FRAME_SIZE

    def cleanup(self):
        self.running = False
        if self.reader_thread:
            self.reader_thread.join(timeout=1)
        for proc, name in [(self.ffmpeg, "ffmpeg"), (self.librespot, "librespot")]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                logger.info(f"🛑 Đã dừng {name}")


# =====================================================================
# INIT BOT
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

active_sources = {}  # guild_id -> SpotifyAudioSource


# =====================================================================
# SLASH COMMANDS
# =====================================================================

@tree.command(name="ping", description="Kiểm tra bot có phản hồi không")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{latency}ms`", ephemeral=True)


@tree.command(name="join", description="Bot vào voice channel của bạn")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ Bạn chưa vào voice!", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        await interaction.response.send_message("✅ Bot đã ở trong voice rồi.", ephemeral=True)
        return
    try:
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message(
            f"✅ Đã vào {interaction.user.voice.channel.mention}"
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Lỗi: {e}", ephemeral=True)


@tree.command(name="leave", description="Bot rời voice channel")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("❌ Bot chưa ở voice.", ephemeral=True)
        return
    src = active_sources.pop(interaction.guild.id, None)
    if src:
        src.cleanup()
    if vc.is_playing():
        vc.stop()
    await vc.disconnect()
    await interaction.response.send_message("👋 Đã rời voice.")


@tree.command(name="spotify-start", description="Bật Spotify Connect (link login chỉ bạn thấy)")
async def spotify_start(interaction: discord.Interaction):
    if not LIBRESPOT_PATH:
        await interaction.response.send_message(
            "❌ Không tìm thấy **librespot**.\n"
            "📥 Tải: https://github.com/librespot-org/librespot/releases",
            ephemeral=True
        )
        return

    if not interaction.user.voice:
        await interaction.response.send_message("❌ Bạn phải ở trong voice channel!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Vào voice nếu chưa
    vc = interaction.guild.voice_client
    if not vc:
        try:
            await interaction.user.voice.channel.connect()
        except Exception as e:
            await interaction.followup.send(f"❌ Không vào được voice: {e}", ephemeral=True)
            return

    guild = interaction.guild

    # Dừng stream cũ
    if guild.id in active_sources:
        old = active_sources.pop(guild.id)
        old.cleanup()
        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.5)

    # Callback gửi URL OAuth (ephemeral)
    async def send_auth_url(url: str):
        try:
            await interaction.followup.send(
                "🔐 **Đăng nhập Spotify lần đầu**\n"
                "Mở link sau trong trình duyệt và cấp quyền:\n"
                f"{url}\n\n"
                f"*Sau khi login, chọn device `{DEVICE_NAME}` trong app Spotify.*",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Send auth URL error: {e}")

    def on_auth(url: str):
        asyncio.run_coroutine_threadsafe(send_auth_url(url), bot.loop)

    try:
        source = SpotifyAudioSource(DEVICE_NAME, BITRATE, on_auth_url=on_auth)
        source.start()
        active_sources[guild.id] = source

        def after_playing(error):
            if error:
                logger.error(f"[Spotify] Playback error: {error}")
            s = active_sources.pop(guild.id, None)
            if s:
                s.cleanup()

        vc.play(source, after=after_playing)

        if source.needs_auth:
            await interaction.followup.send(
                "⏳ **Đang chờ đăng nhập Spotify...** Link sẽ hiện trong vài giây.",
                ephemeral=True
            )
        else:
            embed = discord.Embed(
                title="🎵 Spotify Connect đã bật",
                description=(
                    f"**Device:** `{DEVICE_NAME}`\n"
                    f"**Bitrate:** {BITRATE} kbps\n\n"
                    "1. Mở app Spotify\n"
                    "2. Bấm **Connect to a device**\n"
                    f"3. Chọn **{DEVICE_NAME}**\n"
                    "4. Chọn nhạc 🎧"
                ),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info(f"[Spotify] Started in guild {guild.id}")

    except Exception as e:
        logger.error(f"[Spotify] Start error: {e}")
        await interaction.followup.send(f"❌ Lỗi: `{e}`", ephemeral=True)


@tree.command(name="spotify-stop", description="Tắt Spotify Connect")
async def spotify_stop(interaction: discord.Interaction):
    guild = interaction.guild
    if guild.id not in active_sources:
        await interaction.response.send_message("❌ Spotify Connect chưa bật.", ephemeral=True)
        return
    src = active_sources.pop(guild.id)
    src.cleanup()
    vc = guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message("⏹️ Đã tắt Spotify Connect.")


@tree.command(name="spotify-status", description="Xem trạng thái Spotify Connect")
async def spotify_status(interaction: discord.Interaction):
    guild = interaction.guild
    if guild.id not in active_sources:
        await interaction.response.send_message("📭 Spotify Connect chưa bật.", ephemeral=True)
        return

    src = active_sources[guild.id]
    running = src.running and (src.librespot and src.librespot.poll() is None)

    embed = discord.Embed(title="📊 Spotify Connect Status", color=discord.Color.blue())
    embed.add_field(name="Device", value=f"`{DEVICE_NAME}`", inline=True)
    embed.add_field(name="Bitrate", value=f"{BITRATE} kbps", inline=True)
    embed.add_field(name="Running", value="✅" if running else "❌", inline=True)
    if src.librespot:
        embed.add_field(name="librespot PID", value=f"`{src.librespot.pid}`", inline=True)
    if src.ffmpeg:
        embed.add_field(name="ffmpeg PID", value=f"`{src.ffmpeg.pid}`", inline=True)
    embed.add_field(name="Buffer", value=f"{src.buffer.qsize()} frames", inline=True)
    if src.needs_auth:
        embed.add_field(name="⚠️ Cần login", value="Dùng `/spotify-login`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="spotify-login", description="Lấy lại link đăng nhập Spotify (chỉ bạn thấy)")
async def spotify_login(interaction: discord.Interaction):
    guild = interaction.guild
    if guild.id not in active_sources:
        await interaction.response.send_message(
            "❌ Spotify Connect chưa bật. Dùng `/spotify-start` trước.",
            ephemeral=True
        )
        return
    src = active_sources[guild.id]
    if src.last_auth_url:
        await interaction.response.send_message(
            f"🔐 **Link đăng nhập Spotify:**\n{src.last_auth_url}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "ℹ️ Chưa có link OAuth. Có thể bạn đã login rồi.",
            ephemeral=True
        )


@tree.command(name="help", description="Hướng dẫn sử dụng")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎵 SPOTIFY CONNECT BOT",
        description="Bot chỉ dùng slash commands `/`",
        color=discord.Color.green()
    )
    embed.add_field(
        name="🎧 Voice",
        value="/join – Bot vào voice\n/leave – Bot rời voice",
        inline=False
    )
    embed.add_field(
        name="🎵 Spotify Connect",
        value=(
            "/spotify-start – Bật (link login ẩn)\n"
            "/spotify-stop – Tắt\n"
            "/spotify-status – Trạng thái\n"
            "/spotify-login – Lấy lại link login"
        ),
        inline=False
    )
    embed.add_field(
        name="💡 Cách dùng",
        value=(
            "1. Vào voice channel\n"
            "2. Gõ `/spotify-start`\n"
            "3. Login Spotify (lần đầu)\n"
            "4. Mở app Spotify → Connect → chọn **" + DEVICE_NAME + "**"
        ),
        inline=False
    )
    embed.add_field(
        name="📌 Yêu cầu",
        value="• Spotify **Premium**\n• **librespot** + **ffmpeg**",
        inline=False
    )
    embed.set_footer(text="v0.1 slash-only")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================================================================
# ERROR HANDLER
# =====================================================================
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"Slash error: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ Lỗi: `{error}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Lỗi: `{error}`", ephemeral=True)
    except Exception:
        pass


# =====================================================================
# EVENTS
# =====================================================================
@bot.event
async def on_ready():
    print("=" * 60)
    print(f"✅ Bot: {bot.user}")
    print(f"🆔 ID: {bot.user.id}")
    print(f"🌐 Guilds: {len(bot.guilds)}")
    print("=" * 60)

    # ===== SYNC SLASH =====
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"✅ Đã sync {len(synced)} lệnh lên guild {GUILD_ID}")
        else:
            synced = await tree.sync()
            print(f"✅ Đã sync {len(synced)} lệnh lên GLOBAL")
            print("⚠️ Sync global có thể mất tới 1 giờ mới hiện.")
            print("💡 Điền GUILD_ID trong code để sync nhanh (vài giây).")

        print("\n📋 Danh sách lệnh đã đăng ký:")
        for cmd in synced:
            print(f"   /{cmd.name} - {cmd.description}")
    except Exception as e:
        logger.error(f"❌ Sync error: {e}")

    print("=" * 60)
    print(f"🔧 librespot: {LIBRESPOT_PATH or '❌ CHƯA CÀI'}")
    print("💡 Gõ / trên Discord để xem lệnh")
    print()


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and not after.channel:
        vc = discord.utils.get(bot.voice_clients, guild=before.channel.guild)
        if vc and len(vc.channel.members) == 1:
            gid = before.channel.guild.id
            src = active_sources.pop(gid, None)
            if src:
                src.cleanup()
            await vc.disconnect()
            logger.info(f"[VOICE] Bot tự rời guild {gid}")


# =====================================================================
# MAIN
# =====================================================================
if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        logger.warning("⚠️ Không tìm thấy ffmpeg trong PATH.")
    if not LIBRESPOT_PATH:
        logger.warning("⚠️ Không tìm thấy librespot.")
        logger.warning("   https://github.com/librespot-org/librespot/releases")

    logger.info("🚀 Đang khởi động bot...")
    bot.run(DISCORD_TOKEN)
```

---

📦 Setup

1. Cài đặt

```bash
pip install discord.py
```

2. Tải librespot

· Windows: https://github.com/librespot-org/librespot/releases → tải librespot.ex
