"""
SPOTIFY CONNECT BOT - Single File
==================================
Bot giả làm 1 thiết bị Spotify Connect. Khi bạn mở app Spotify và
bấm "Connect to a device" → chọn "Discord Bot", audio sẽ phát lên voice.

Setup:
1. Cài librespot:
   - Windows: tải librespot.exe từ
     https://github.com/librespot-org/librespot/releases
     đặt cùng thư mục bot
   - Linux:   cargo install librespot  (hoặc tải binary)
   - Mac:     brew install librespot
2. Có Spotify Premium (bắt buộc cho Connect API)
3. pip install discord.py

Chạy:
    py bot.py

Sử dụng trên Discord:
    ?join             - Bot vào voice channel
    ?spotify-start    - Bật Spotify Connect (lần đầu cần login)
    ?spotify-stop     - Tắt
    ?spotify-status   - Trạng thái
    ?help             - Hướng dẫn

Lần đầu chạy ?spotify-start:
    - Terminal sẽ hiển thị link đăng nhập Spotify
    - Mở link, đăng nhập, cấp quyền
    - Credentials được cache vào ./.spotify-cache
    - Các lần sau tự đăng nhập
"""

import discord
from discord.ext import commands
import asyncio
import os
import sys
import json
import subprocess
import shutil
import threading
import queue
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
# CONFIG
# =====================================================================
CONFIG_FILE = "config.json"
default_config = {
    "default_prefix": "?",
    "admin_ids": [],
    "spotify_device_name": "Discord Bot",
    "spotify_bitrate": 320,
    "spotify_cache_dir": "./.spotify-cache"
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(default_config, f, indent=2, ensure_ascii=False)
    logger.info(f"⚠️ Đã tạo file {CONFIG_FILE} mặc định.")
    return default_config

config = load_config()
DEFAULT_PREFIX = config.get("default_prefix", "?")
ADMIN_IDS = config.get("admin_ids", [])
DEVICE_NAME = config.get("spotify_device_name", "Discord Bot")
BITRATE = config.get("spotify_bitrate", 320)
CACHE_DIR = config.get("spotify_cache_dir", "./.spotify-cache")


# ===== ĐỌC TOKEN DISCORD =====
try:
    with open('discord_token.txt', 'r') as f:
        DISCORD_TOKEN = f.read().strip()
        if not DISCORD_TOKEN:
            raise ValueError("Token rỗng")
except FileNotFoundError:
    logger.error("❌ Không tìm thấy discord_token.txt!")
    sys.exit(1)
except ValueError as e:
    logger.error(f"❌ Lỗi token Discord: {e}")
    sys.exit(1)


# =====================================================================
# TÌM LIBRESPOT
# =====================================================================
def find_librespot():
    """Tìm librespot trong PATH hoặc cùng thư mục bot."""
    # Cùng thư mục bot
    for name in ["librespot.exe", "librespot"]:
        local = Path(name)
        if local.exists():
            return str(local.resolve())
    # Trong PATH
    found = shutil.which("librespot")
    if found:
        return found
    return None

LIBRESPOT_PATH = find_librespot()


# =====================================================================
# SPOTIFY AUDIO SOURCE
# =====================================================================
class SpotifyAudioSource(discord.AudioSource):
    """
    Đọc PCM từ librespot → chuyển qua ffmpeg → đẩy lên Discord.
    Discord cần PCM 16-bit stereo 48kHz, mỗi frame 20ms = 3840 bytes.
    """
    FRAME_SIZE = 3840  # 0.02s * 48000Hz * 2ch * 2bytes

    def __init__(self, device_name: str, bitrate: int):
        self.device_name = device_name
        self.bitrate = bitrate
        self.librespot = None
        self.ffmpeg = None
        self.buffer = queue.Queue(maxsize=100)
        self.running = False
        self.reader_thread = None

    def start(self):
        """Khởi động librespot + ffmpeg + reader thread."""
        if not LIBRESPOT_PATH:
            raise RuntimeError("Không tìm thấy librespot. Cài đặt và đặt cùng thư mục bot.")

        os.makedirs(CACHE_DIR, exist_ok=True)

        logger.info(f"🎵 Khởi động librespot (device: {self.device_name}, bitrate: {self.bitrate})")

        # ----- librespot: output PCM ra stdout -----
        librespot_cmd = [
            LIBRESPOT_PATH,
            "--name", self.device_name,
            "--backend", "pipe",
            "--bitrate", str(self.bitrate),
            "--cache", CACHE_DIR,
            "--disable-discovery"  # tắt mDNS (tuỳ chọn, giảm nhiễu)
        ]
        self.librespot = subprocess.Popen(
            librespot_cmd,
            stdout=subprocess.PIPE,
            stderr=None  # để auth prompt hiện trong terminal
        )
        logger.info(f"   librespot PID: {self.librespot.pid}")

        # ----- ffmpeg: PCM in → PCM S16LE 48kHz stereo out -----
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le",
            "-ar", "48000",
            "-ac", "2",
            "pipe:1"
        ]
        self.ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=self.librespot.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        logger.info(f"   ffmpeg PID: {self.ffmpeg.pid}")

        # ----- Reader thread: đọc ffmpeg stdout → buffer -----
        self.running = True
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name="SpotifyReader",
            daemon=True
        )
        self.reader_thread.start()

        logger.info("✅ Spotify source sẵn sàng. Mở app Spotify → Connect to a device!")

    def _reader_loop(self):
        """Đọc liên tục từ ffmpeg stdout, đẩy vào queue."""
        while self.running:
            try:
                data = self.ffmpeg.stdout.read(self.FRAME_SIZE)
                if not data or len(data) < self.FRAME_SIZE:
                    logger.warning("⚠️ ffmpeg stream kết thúc")
                    break
                try:
                    self.buffer.put(data, timeout=0.1)
                except queue.Full:
                    # Bỏ frame nếu buffer đầy (tránh tích tụ)
                    pass
            except Exception as e:
                logger.error(f"Reader error: {e}")
                break

    def read(self) -> bytes:
        """Discord.py gọi liên tục để lấy 20ms PCM."""
        # Nếu librespot đã chết → kết thúc
        if self.librespot and self.librespot.poll() is not None:
            return b''
        if not self.running:
            return b''

        try:
            return self.buffer.get(timeout=0.02)
        except queue.Empty:
            # Không có data (Spotify paused hoặc chưa phát) → trả silence
            return b'\x00' * self.FRAME_SIZE

    def cleanup(self):
        """Dọn dẹp khi dừng."""
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
# BOT SETUP
# =====================================================================
def get_prefix(bot, message):
    return DEFAULT_PREFIX

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)
bot.remove_command('help')

# Lưu source theo guild để cleanup
active_sources = {}  # guild_id -> SpotifyAudioSource


def is_admin(ctx):
    return ctx.author.id in ADMIN_IDS


# =====================================================================
# COMMANDS
# =====================================================================

@bot.command(name="join")
async def join(ctx):
    """Bot vào voice channel."""
    if not ctx.author.voice:
        await ctx.send("❌ Bạn chưa vào voice!")
        return
    if ctx.voice_client:
        await ctx.send("✅ Bot đã ở trong voice rồi.")
        return
    try:
        await ctx.author.voice.channel.connect()
        await ctx.send(f"✅ Đã vào {ctx.author.voice.channel.mention}")
    except Exception as e:
        await ctx.send(f"❌ Lỗi: {e}")


@bot.command(name="leave")
async def leave(ctx):
    """Bot rời voice (tự dừng stream)."""
    if not ctx.voice_client:
        await ctx.send("❌ Bot chưa ở voice.")
        return

    # Dọn source
    src = active_sources.pop(ctx.guild.id, None)
    if src:
        src.cleanup()

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    await ctx.voice_client.disconnect()
    await ctx.send("👋 Đã rời voice.")


@bot.command(name="spotify-start", aliases=["spotifystart", "sstart"])
async def spotify_start(ctx):
    """Bật Spotify Connect device và stream lên Discord voice."""
    if not LIBRESPOT_PATH:
        await ctx.send(
            "❌ Không tìm thấy **librespot**.\n"
            "📥 Tải tại: https://github.com/librespot-org/librespot/releases\n"
            "📁 Đặt `librespot.exe` (Windows) hoặc `librespot` (Linux/Mac) cùng thư mục bot."
        )
        return

    # Cần ở trong voice
    if not ctx.author.voice:
        await ctx.send("❌ Bạn phải ở trong voice channel trước!")
        return

    # Vào voice nếu chưa
    if not ctx.voice_client:
        try:
            await ctx.author.voice.channel.connect()
            await ctx.send(f"✅ Đã vào {ctx.author.voice.channel.mention}")
        except Exception as e:
            await ctx.send(f"❌ Không vào được voice: {e}")
            return

    # Nếu đã có stream → dừng trước
    if ctx.guild.id in active_sources:
        old = active_sources.pop(ctx.guild.id)
        old.cleanup()
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await asyncio.sleep(0.5)

    try:
        # Tạo source
        source = SpotifyAudioSource(DEVICE_NAME, BITRATE)
        source.start()
        active_sources[ctx.guild.id] = source

        def after_playing(error):
            if error:
                logger.error(f"[Spotify] Playback error: {error}")
            # Cleanup khi stream kết thúc
            src = active_sources.pop(ctx.guild.id, None)
            if src:
                src.cleanup()

        ctx.voice_client.play(source, after=after_playing)

        embed = discord.Embed(
            title="🎵 Spotify Connect đã bật",
            description=(
                f"**Device:** `{DEVICE_NAME}`\n"
                f"**Bitrate:** {BITRATE} kbps\n\n"
                "**Bước tiếp theo:**\n"
                "1. Mở app Spotify (điện thoại/PC)\n"
                "2. Bấm biểu tượng **Connect to a device** (góc dưới phải)\n"
                f"3. Chọn **{DEVICE_NAME}**\n"
                "4. Chọn bài → nhạc phát lên Discord 🎧"
            ),
            color=discord.Color.green()
        )
        if not os.path.exists(os.path.join(CACHE_DIR, "credentials.json")):
            embed.add_field(
                name="⚠️ Lần đầu chạy",
                value="Terminal sẽ hiển thị link đăng nhập Spotify. Mở link và cấp quyền.",
                inline=False
            )
        embed.set_footer(text="Dùng ?spotify-stop để tắt")
        await ctx.send(embed=embed)

        logger.info(f"[Spotify] {ctx.author} started in guild {ctx.guild.id}")

    except Exception as e:
        logger.error(f"[Spotify] Start error: {e}")
        await ctx.send(f"❌ Lỗi khởi động: `{e}`")


@bot.command(name="spotify-stop", aliases=["spotifystop", "sstop"])
async def spotify_stop(ctx):
    """Tắt Spotify Connect."""
    if ctx.guild.id not in active_sources:
        await ctx.send("❌ Spotify Connect chưa bật.")
        return

    src = active_sources.pop(ctx.guild.id)
    src.cleanup()

    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()

    await ctx.send("⏹️ Đã tắt Spotify Connect.")


@bot.command(name="spotify-status", aliases=["sstatus"])
async def spotify_status(ctx):
    """Xem trạng thái Spotify Connect."""
    if ctx.guild.id not in active_sources:
        await ctx.send("📭 Spotify Connect chưa bật.")
        return

    src = active_sources[ctx.guild.id]
    running = src.running and (src.librespot and src.librespot.poll() is None)

    embed = discord.Embed(
        title="📊 Spotify Connect Status",
        color=discord.Color.blue()
    )
    embed.add_field(name="Device", value=f"`{DEVICE_NAME}`", inline=True)
    embed.add_field(name="Bitrate", value=f"{BITRATE} kbps", inline=True)
    embed.add_field(name="Running", value="✅" if running else "❌", inline=True)
    embed.add_field(name="librespot PID", value=f"`{src.librespot.pid}`", inline=True)
    embed.add_field(name="ffmpeg PID", value=f"`{src.ffmpeg.pid}`", inline=True)
    embed.add_field(name="Buffer", value=f"{src.buffer.qsize()} frames", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="help", aliases=["commands"])
async def help_cmd(ctx):
    """Hiển thị danh sách lệnh."""
    help_text = f"""
🎵 **SPOTIFY CONNECT BOT**

**🎧 VOICE:**
`{DEFAULT_PREFIX}join` – Bot vào voice channel
`{DEFAULT_PREFIX}leave` – Bot rời voice

**🎵 SPOTIFY CONNECT:**
`{DEFAULT_PREFIX}spotify-start` – Bật Spotify Connect device
`{DEFAULT_PREFIX}spotify-stop` – Tắt
`{DEFAULT_PREFIX}spotify-status` – Trạng thái

**💡 CÁCH DÙNG:**
1. Gõ `{DEFAULT_PREFIX}spotify-start`
2. Mở app Spotify → **Connect to a device** → chọn **{DEVICE_NAME}**
3. Chọn nhạc → phát lên Discord

**📌 YÊU CẦU:**
• Spotify **Premium**
• Đã cài **librespot** + **ffmpeg**

**🔗 Lấy librespot:**
https://github.com/librespot-org/librespot/releases
"""
    await ctx.send(help_text)


# =====================================================================
# EVENTS
# =====================================================================

@bot.event
async def on_ready():
    print("=" * 60)
    print(f"✅ Bot: {bot.user} | Prefix: {DEFAULT_PREFIX}")
    print(f"🎵 Device name: {DEVICE_NAME}")
    print(f"🎧 Bitrate: {BITRATE} kbps")
    print(f"🔧 librespot: {LIBRESPOT_PATH or '❌ CHƯA CÀI'}")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    print("=" * 60)
    print("💡 Gõ ?help trên Discord để xem lệnh")
    print()


@bot.event
async def on_voice_state_update(member, before, after):
    """Tự động dừng stream khi bot bị đẩy ra khỏi voice (không còn ai)."""
    if before.channel and not after.channel:
        vc = discord.utils.get(bot.voice_clients, guild=before.channel.guild)
        if vc and len(vc.channel.members) == 1:
            gid = before.channel.guild.id
            src = active_sources.pop(gid, None)
            if src:
                src.cleanup()
            await vc.disconnect()
            logger.info(f"[VOICE] Bot tự rời guild {gid} (không còn ai)")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(f"❌ Lệnh không tồn tại. Dùng `{DEFAULT_PREFIX}help`.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Thiếu tham số. Dùng `{DEFAULT_PREFIX}help`.")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(f"❌ Lỗi: {error}")


# =====================================================================
# MAIN
# =====================================================================

def main():
    # Kiểm tra ffmpeg
    if not shutil.which("ffmpeg"):
        logger.warning("⚠️ Không tìm thấy ffmpeg trong PATH. Cài đặt và thêm vào PATH.")

    # Cảnh báo librespot
    if not LIBRESPOT_PATH:
        logger.warning("⚠️ Không tìm thấy librespot. Tải tại:")
        logger.warning("   https://github.com/librespot-org/librespot/releases")
        logger.warning("   Đặt file librespot(.exe) cùng thư mục bot")

    logger.info("🚀 Đang khởi động bot...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
