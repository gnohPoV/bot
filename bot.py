import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
import re
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# ===== CONFIGURATION =====
# Đọc token Discord từ file
try:
    with open('discord_token.txt', 'r') as f:
        DISCORD_TOKEN = f.read().strip()
except FileNotFoundError:
    print("❌ Không tìm thấy file discord_token.txt! Tạo file và paste token vào.")
    exit(1)

# Spotify API credentials (lấy từ https://developer.spotify.com/dashboard)
SPOTIFY_CLIENT_ID = "your_client_id_here"
SPOTIFY_CLIENT_SECRET = "your_client_secret_here"

# Prefix cho bot
PREFIX = "?"

# ===== INIT =====
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Tạo thư mục cache
if not os.path.exists('audio_cache'):
    os.makedirs('audio_cache')

# Khởi tạo Spotify client
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

# Lưu queue
music_queues = {}

def get_queue(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = {'queue': [], 'current': None}
    return music_queues[guild_id]

# ===== UTILITY FUNCTIONS =====
def is_spotify_url(url):
    return 'open.spotify.com' in url or 'spotify.com' in url

def is_youtube_url(url):
    return 'youtube.com' in url.lower() or 'youtu.be' in url.lower()

def get_spotify_track_info(url):
    """Lấy thông tin track từ Spotify URL"""
    try:
        if 'track' in url:
            track = sp.track(url)
            return [{
                'name': f"{track['name']} - {track['artists'][0]['name']}",
                'query': f"{track['name']} {track['artists'][0]['name']} audio"
            }]
        elif 'playlist' in url:
            playlist_id = url.split('/')[-1].split('?')[0]
            results = sp.playlist_tracks(playlist_id)
            tracks = []
            for item in results['items']:
                track = item['track']
                if track:
                    tracks.append({
                        'name': f"{track['name']} - {track['artists'][0]['name']}",
                        'query': f"{track['name']} {track['artists'][0]['name']} audio"
                    })
            return tracks
        elif 'album' in url:
            album_id = url.split('/')[-1].split('?')[0]
            album_info = sp.album(url)
            artist_name = album_info['artists'][0]['name']
            results = sp.album_tracks(album_id)
            tracks = []
            for track in results['items']:
                tracks.append({
                    'name': f"{track['name']} - {artist_name}",
                    'query': f"{track['name']} {artist_name} audio"
                })
            return tracks
    except Exception as e:
        print(f"Spotify error: {e}")
    return []

def search_spotify(query):
    """Tìm kiếm trên Spotify"""
    try:
        results = sp.search(q=query, type='track', limit=5)
        if results['tracks']['items']:
            track = results['tracks']['items'][0]
            return {
                'name': f"{track['name']} - {track['artists'][0]['name']}",
                'query': f"{track['name']} {track['artists'][0]['name']} audio"
            }
    except Exception as e:
        print(f"Spotify search error: {e}")
    return None

def search_youtube(query):
    """Tìm kiếm trên YouTube - dùng cookies từ trình duyệt thay vì file"""
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            # 🔥 Dùng cookies từ trình duyệt Chrome (đổi thành 'firefox' nếu xài Firefox)
            'cookiesfrombrowser': ('chrome',),
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch:{query}", download=False)
            if info and info['entries']:
                return {
                    'name': info['entries'][0]['title'],
                    'url': f"https://www.youtube.com/watch?v={info['entries'][0]['id']}"
                }
    except Exception as e:
        print(f"YouTube search error: {e}")
    return None

async def download_and_cache(url, track_name):
    """Tải nhạc về cache - dùng cookies từ trình duyệt"""
    try:
        safe_name = re.sub(r'[^\w\s-]', '', track_name)[:50]
        filename = f"audio_cache/{safe_name.replace(' ', '_')}.mp3"
        if not os.path.exists(filename):
            print(f"⬇️ Đang cache: {safe_name}")
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': f'audio_cache/{safe_name.replace(" ", "_")}',
                'quiet': True,
                'no_warnings': True,
                # 🔥 Không dùng cookiefile nữa, dùng cookies từ trình duyệt
                'cookiesfrombrowser': ('chrome',),
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
            print(f"✓ Cache xong: {safe_name}")
    except Exception as e:
        print(f"Cache error: {e}")

async def play_audio(ctx, track_name):
    """Phát nhạc từ cache"""
    try:
        safe_name = re.sub(r'[^\w\s-]', '', track_name)[:50]
        filename = f"audio_cache/{safe_name.replace(' ', '_')}.mp3"
        waited = 0
        while not os.path.exists(filename) and waited < 60:
            await asyncio.sleep(0.5)
            waited += 0.5
        if not os.path.exists(filename):
            await ctx.send(f"❌ Không thể tải: {track_name}")
            return
        source = discord.FFmpegOpusAudio(filename)
        def after_playing(error):
            if error:
                print(f"Playback error: {error}")
            asyncio.run_coroutine_threadsafe(play_next_track(ctx), bot.loop)
        ctx.voice_client.play(source, after=after_playing)
    except Exception as e:
        print(f"Playback error: {e}")
        await ctx.send(f"❌ Lỗi phát nhạc: {e}")

async def play_next_track(ctx):
    """Phát bài tiếp theo trong queue"""
    queue = get_queue(ctx.guild.id)
    if not queue['queue']:
        queue['current'] = None
        return
    track = queue['queue'].pop(0)
    queue['current'] = track
    await ctx.send(f"🎵 **Đang phát:** {track['name']}")
    asyncio.create_task(download_and_cache(track['youtube_url'], track['name']))
    await play_audio(ctx, track['name'])

# ===== BOT EVENTS =====
@bot.event
async def on_ready():
    print(f"✅ Bot đã đăng nhập: {bot.user}")
    print(f"📌 Prefix: {PREFIX}")
    print("🎵 Spotify + YouTube mode ready!")
    print("🍪 Sử dụng cookies từ trình duyệt (Chrome) – không cần file cookies.txt")

# ===== COMMANDS =====
@bot.command(name="join")
async def join(ctx):
    """Tham gia voice channel"""
    if ctx.author.voice is None:
        await ctx.send("❌ Bạn phải ở trong voice channel!")
        return
    channel = ctx.author.voice.channel
    try:
        await channel.connect()
        await ctx.send(f"✅ Đã vào kênh: {channel.name}")
    except Exception as e:
        await ctx.send(f"❌ Lỗi: {e}")

@bot.command(name="leave")
async def leave(ctx):
    """Rời voice channel"""
    if ctx.voice_client is None:
        await ctx.send("❌ Bot không ở trong voice channel")
        return
    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    queue = get_queue(ctx.guild.id)
    queue['queue'] = []
    queue['current'] = None
    await ctx.voice_client.disconnect()
    await ctx.send("👋 Đã rời kênh voice")

@bot.command(name="play")
async def play(ctx, *, query: str = None):
    """Phát nhạc từ Spotify hoặc YouTube"""
    if query is None:
        await ctx.send("❌ Cách dùng: ?play <tên bài hát hoặc link>")
        return
    if ctx.voice_client is None:
        if ctx.author.voice is None:
            await ctx.send("❌ Bạn phải ở trong voice channel!")
            return
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.send(f"✅ Đã vào kênh: {channel.name}")
    try:
        if is_spotify_url(query):
            await ctx.send("🟢 Đang tải từ Spotify...")
            spotify_tracks = get_spotify_track_info(query)
            if not spotify_tracks:
                await ctx.send("❌ Không lấy được track từ Spotify")
                return
            for spotify_track in spotify_tracks:
                youtube_result = search_youtube(spotify_track['query'])
                if youtube_result:
                    queue = get_queue(ctx.guild.id)
                    queue['queue'].append({
                        'name': spotify_track['name'],
                        'youtube_url': youtube_result['url']
                    })
            await ctx.send(f"✅ Đã thêm {len(spotify_tracks)} bài từ Spotify!")
        elif is_youtube_url(query):
            await ctx.send("🔴 Đang tải link YouTube...")
            ydl_opts = {'quiet': True, 'no_warnings': True, 'cookiesfrombrowser': ('chrome',)}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                track_name = info['title']
            queue = get_queue(ctx.guild.id)
            queue['queue'].append({
                'name': track_name,
                'youtube_url': query
            })
            await ctx.send(f"✅ Đã thêm: **{track_name}**")
        else:
            await ctx.send("🔍 Đang tìm kiếm...")
            spotify_result = search_spotify(query)
            if spotify_result:
                search_query = spotify_result['query']
                track_name = spotify_result['name']
            else:
                search_query = query
                track_name = None
            youtube_result = search_youtube(search_query)
            if not youtube_result:
                await ctx.send("❌ Không tìm thấy kết quả")
                return
            if not track_name:
                track_name = youtube_result['name']
            queue = get_queue(ctx.guild.id)
            queue['queue'].append({
                'name': track_name,
                'youtube_url': youtube_result['url']
            })
            await ctx.send(f"✅ Đã thêm: **{track_name}**")
        if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
            await play_next_track(ctx)
    except Exception as e:
        await ctx.send(f"❌ Lỗi: {str(e)}")
        print(f"Play error: {e}")

@bot.command(name="spotify")
async def spotify_search(ctx, *, query: str = None):
    """Tìm kiếm trên Spotify"""
    if query is None:
        await ctx.send("❌ Cách dùng: ?spotify <tên bài hát>")
        return
    try:
        results = sp.search(q=query, type='track', limit=5)
        if not results['tracks']['items']:
            await ctx.send("❌ Không tìm thấy trên Spotify")
            return
        response = "🟢 **Kết quả Spotify:**\n"
        for i, track in enumerate(results['tracks']['items'][:5], 1):
            artists = ", ".join([artist['name'] for artist in track['artists']])
            response += f"{i}. **{track['name']}** - {artists}\n"
        response += "\nGõ `?play <tên bài>` để phát"
        await ctx.send(response)
    except Exception as e:
        await ctx.send(f"❌ Lỗi: {e}")

@bot.command(name="queue")
async def queue_cmd(ctx):
    """Xem hàng đợi"""
    queue = get_queue(ctx.guild.id)
    queue_text = ""
    if queue['current']:
        queue_text += f"🎵 **Đang phát:** {queue['current']['name']}\n\n"
    if queue['queue']:
        queue_text += "📋 **Tiếp theo:**\n"
        for i, track in enumerate(queue['queue'][:10], 1):
            queue_text += f"{i}. {track['name']}\n"
        if len(queue['queue']) > 10:
            queue_text += f"... và {len(queue['queue']) - 10} bài nữa"
    else:
        queue_text += "📋 Hàng đợi trống"
    await ctx.send(queue_text)

@bot.command(name="skip")
async def skip(ctx):
    """Bỏ qua bài hiện tại"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Đã bỏ qua")
    else:
        await ctx.send("❌ Không có bài nào đang phát")

@bot.command(name="stop")
async def stop(ctx):
    """Dừng và xóa hàng đợi"""
    queue = get_queue(ctx.guild.id)
    queue['queue'] = []
    queue['current'] = None
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    await ctx.send("⏹️ Đã dừng")

@bot.command(name="pause")
async def pause(ctx):
    """Tạm dừng / Tiếp tục"""
    if not ctx.voice_client:
        await ctx.send("❌ Bot không ở trong voice")
        return
    if ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Đã tạm dừng")
    elif ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Tiếp tục phát")

@bot.command(name="commands")
async def commands_cmd(ctx):
    """Hiển thị danh sách lệnh"""
    help_text = f"""
🎵 **MUSIC BOT COMMANDS**

**🎧 PHÁT NHẠC:**
`{PREFIX}play <bài/URL>` - Phát từ Spotify hoặc YouTube
`{PREFIX}spotify <bài>` - Tìm kiếm trên Spotify
`{PREFIX}pause` - Tạm dừng / Tiếp tục
`{PREFIX}skip` - Bỏ qua bài hiện tại
`{PREFIX}stop` - Dừng & xóa hàng đợi

**🔗 HỖ TRỢ LINK:**
- YouTube videos
- Spotify tracks
- Spotify playlists
- Spotify albums

**📋 HÀNG ĐỢI:**
`{PREFIX}queue` - Xem hàng đợi
`{PREFIX}join` - Vào voice channel
`{PREFIX}leave` - Rời voice channel

**💡 VÍ DỤ:**
`{PREFIX}play Blinding Lights`
`{PREFIX}play https://open.spotify.com/track/...`
`{PREFIX}play https://open.spotify.com/playlist/...`
`{PREFIX}play https://youtube.com/watch?v=...`
"""
    await ctx.send(help_text)

# ===== RUN BOT =====
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)