import os
import discord
from discord.ext import commands
from discord_components import ComponentsBot

from VcControl import VcControl
from ytAPIget import yt_search
import youtube_dl
from YTDLSource import YTDLSource, StaticSource
from youtube_dl.utils import DownloadError

from helper import *
from config import *
from options import ytdl_format_options, ffmpeg_options, ffmpeg_error_log
from DJDynamoDB import DJDB
from DJBannedException import DJBannedException
from YTDLException import YTDLException

class DJ(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.vcControls = {} # guild.id: vcControl object

        # mysql
        # self.djdb = DJDB(mysql_host, mysql_user, mysql_password, mysql_db_name)
        # dynamodb
        self.djdb = DJDB()

        self.djdb.connect()


    # ---------------------------- MESSAGING --------------------------- # 
    async def notify(self, ctx, message, del_sec = 10):
        if str(message) == "": return # prevent err

        m = await ctx.send(message)

        # delete the message if needed
        if del_sec: 
            assert type(del_sec) == int
            await m.delete(delay = del_sec)

    # -------------------------------------------------------------------------------------------- # 
    # ------------------------------------- VOICE CONTROL ---------------------------------------- # 
    # -------------------------------------------------------------------------------------------- # 
    # -------------------- Join voice channel -------------------- #
    @commands.command()
    async def join(self, ctx):
        '''Let bot join the voice channel (caller's channel / most populated channel)'''
        print(ctx.guild.id)
        if ctx.voice_client is None:
            vc = get_channel_to_join(ctx)
            self.djdb.connect()
            await vc.connect()
            # create new playlist instance, send current channel for further messaging
            self.vcControls[ctx.guild.id] = VcControl(ctx.channel, self, ctx.voice_client)
        else: 
            n = ctx.voice_client.channel.name
            await self.notify(ctx, f"I am in voice channel: {n}", del_sec=60)

    # -------------------- Leave voice channel --------------------
    @commands.command()
    async def leave(self, ctx):
        '''Let bot leave voice channel'''
        if ctx.voice_client is None:
            raise Exception("I am not in any voice channel, use join command instead")
        else: 
            await self.vcControls[ctx.guild.id].disconnectVC()
            
    # -------------------- play from youtube url / default if no url -------------------- # 
    # COMMAND: dj
    @commands.command()
    async def dj(self, ctx, type = True):
        '''Turn on DJ'''
        vc = ctx.voice_client
        if type and vc is None:
            await self.join(ctx)

        # set vccontrol and bot status
        await self.vcControls[ctx.guild.id].set_dj_type( type )
        await self.bot_status(dj = type)

    # COMMAND: djoff
    @commands.command()
    async def djoff(self, ctx):
        '''Turn off DJ'''
        await self.dj(ctx, type=None)

    # COMMAND: playsearch
    @commands.command(aliases=['psearch', 'ps'])
    async def playsearch(self, ctx, *kwords):
        '''Search in youtube and play a picked song'''
        s = list(kwords)
        if len(s) <= 0 or "".join(s) == "": # throw error when no arg given 
            raise Exception("No search term(s) given")
        ### scp: 1. search | 2. compile | 3. play 
        # 1. search -> get url
        vid = await self.scp_search_choice(ctx, s,)

        # 2 & 3
        await self.compile_and_play(ctx, vid)


    # COMMAND: play
    @commands.command(aliases=['p'])
    async def play(self, ctx, *kwords):
        '''Play a song (search in youtube / youtube link)'''
        s = list(kwords)
        if len(s) <= 0 or "".join(s) == "": # throw error when no arg given (alternative: play default source)
            raise Exception("No url or search term given")
            # source = StaticSource(discord.FFmpegPCMAudio(source=default_play_dir), volume=default_init_vol)
            # source.url = ''

        ### scp: 1. search | 2. compile | 3. play 
        # 1. search -> get url
        if ("youtu.be" in s[0] or "youtube.com" in s[0]): 
            # case 1: url
            url = s[0]
            vid = yturl_to_vid(url)
            # insert to db if not in db
            if not self.djdb.find_song_match(vid):
                self.yt_search_and_insert(vid, use_vID = True)
        else: 
            # case 2: query yt
            vid = await self.scp_search(ctx, s,)

        # 2 & 3
        await self.compile_and_play(ctx, vid)


    async def compile_and_play(self, ctx, vid):
        '''Step 2 & 3'''
        # DB: INC Qcount
        self.djdb.increment_qcount(vid)

        # 2. compile
        source = await self.scp_compile(vid)
        # 3. play
        await self.scp_play(ctx, source)

    # ---------------------------- SEARCH COMPILE PLAY --------------------------------- #     

    async def scp_search_choice(self, ctx, s):
        '''(ps) scp step 1 (w/choice): search (youtube API only)'''
        vid = None
        return vid

    async def scp_search(self, ctx, s):
        '''scp step 1: search (in db or youtube)'''
        # search for url in youtube API
        search_term = (" ".join(s)).lower()
        await self.notify(ctx, f"Searching: {search_term}")
        
        # fetch vid from either db or youtube api search
        match = self.djdb.find_query_match(search_term)
        if match:
            vid = match
            # insert to db if not in db
            if not self.djdb.find_song_match(vid):
                self.yt_search_and_insert(vid, use_vID = True)
        else:
            # get info by searching youtube API
            info = self.yt_search_and_insert(search_term, insert_after = False)
            vid = info.vID
            # add query to db
            self.djdb.add_query(search_term, info)
        return vid
    
    def yt_search_and_insert(self, search_term, use_vID = False, insert_after = True):
        '''
        [ Helper function for scp_search ]
        youtube search and insert to db
        return: searched song info
        '''
        
        info = yt_search(search_term, use_vID=use_vID)
        # no result from youtube api (by vid)
        if not info: 
            if use_vID: raise Exception(f"No video found: https://youtu.be/{search_term}")
            else: raise Exception(f"Nothing found in video form: {search_term}")

        if insert_after: self.djdb.insert_song(info)
        return info


    async def scp_compile(self, vid, stream = True):
        '''
        scp step 2: compile youtube source
        compile YTDLSource (audio source object) from youtube url
        return: source object
        '''

        url = "https://youtube.com/watch?v=" + vid

        try:
            # search yt url
            data = await self.bot.loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        except DownloadError as e: # youtube dl download error
            self.djdb.remove_song(vid)
            raise YTDLException(f"Unable to download {url}, removed ({str(e)})")

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        # options for baseboosted or normal
        if need_baseboost(data.get('title')):
            ffmpeg_final_options = ffmpeg_options.copy()
            os = "options"
            ffmpeg_final_options[os] = ffmpeg_final_options[os] + " -af bass=g=50"
        else:
            ffmpeg_final_options = ffmpeg_options.copy()
        source = YTDLSource(discord.FFmpegPCMAudio(filename, **ffmpeg_final_options), data=data)
        source.url = url
        source.vid = vid

        # check valid song
        banned_reason = is_banned(source.title)
        if banned_reason:
            raise DJBannedException(f"{source.title} banned: {banned_reason}")
        else:
            return source

    async def scp_play(self, ctx, source):
        '''
        scp step 3: play in voice client
        send source to playlist and play in vc
        '''
        vc = ctx.voice_client
        if vc is None:
            await self.join(ctx)
            vc = ctx.voice_client
        await self.vcControls[ctx.guild.id].add(vc, source)


    # ------------------------------------ CONTROLS --------------------------------------- # 
    # COMMAND: nowplaying
    @commands.command()
    async def nowplaying(self, ctx):
        '''Redisplay nowplaying board w/ controls'''
        await self.vcControls[ctx.guild.id].display_nowplaying()

    # COMMAND: queue
    @commands.command(aliases=['playlist'])
    async def queue(self, ctx):
        '''List current playlist queue'''
        await self.vcControls[ctx.guild.id].list(ctx)

    # COMMAND: skip
    @commands.command()
    async def skip(self, ctx):
        '''Skip the current song'''
        await self.vcControls[ctx.guild.id].skip(ctx.voice_client, ctx.author)

    # COMMAND: remove
    @commands.command()
    async def remove(self, ctx, *args):
        '''Remove a song from playlist'''
        k = " ".join(args)
        await self.vcControls[ctx.guild.id].remove_track(ctx.voice_client, k, ctx.author)

    # COMMAND: clear
    @commands.command()
    async def clear(self, ctx):
        '''Clear playlist'''
        await self.vcControls[ctx.guild.id].clear()

    # COMMAND: stop
    @commands.command()
    async def stop(self, ctx):
        '''Stop player'''
        await self.vcControls[ctx.guild.id].stop()

    # COMMAND: vup (doubled)
    @commands.command()
    async def vup(self, ctx, n=2):
        '''Increase current volume'''
        await self.vset(ctx, n)

    # COMMAND: vdown (half)
    @commands.command()
    async def vdown(self, ctx, n=0.5):
        '''Reduce current volume'''
        await self.vset(ctx, n)

    # volume set
    async def vset(self, ctx, volume):
        vc = ctx.voice_client
        if not vc: raise Exception("I am not in any voice channel")

        vc.source = discord.PCMVolumeTransformer(vc.source)
        vc.source.volume = float(volume)
        await self.notify(ctx, f"Volume multiply by {ctx.voice_client.source.volume}")



    # -------------------------------------------------------------------------------------------- # 
    # --------------------------------------- DB RELATED ----------------------------------------- # 
    # -------------------------------------------------------------------------------------------- # 
    # COMMAND: bind
    @commands.command()
    async def bind(self, ctx, *args):
        '''
        Bind a search query to a url / find what url is binded to a search query
        Usage: =bind [search term (can contain spaces)] [url (optional)]
        '''

        # adding query binding to a vID
        def add_binding(f_q, f_vid):
            # actual binding when url provided
            if not self.djdb.find_song_match(f_vid):
                info = self.yt_search_and_insert(f_vid, use_vID = True)
            else: # song exist
                info = f_vid

            self.djdb.add_query(f_q, info)

        out_message = None
        try:
            # case 1: url provided, delete current binding and bind [query terms] to the url

            # try add query with vid
            vid = yturl_to_vid(args[-1])
            q = " ".join(args[:-1])

            # delete all other query

            # db: add binding
            add_binding(q, vid)

            out_message = f"Added binding \n{q} -> https://youtu.be/{vid}"
        except: 
            # case 2: No url provided, find binded url for the [query terms]
            vid = None
            q = " ".join(args)

            # query binding if url not provided
            vID = self.djdb.find_query_match(q)
            if vID:
                out_message = f"{q} is bind to https://youtu.be/{vID}"
            else: 
                out_message = f"{q} is not bind to anything"

        # final output message to client
        await self.notify(ctx, out_message, del_sec=None)
            

    # COMMAND: listdj
    # list all djable songs
    @commands.command()
    async def listdj(self, ctx, *args):
        '''List 10 djable songs'''
        songs = self.djdb.list_all_songs(dj = True)
        await self.list(ctx, display_list = songs, title = "List 10 djable songs", none_message = "No song found")


    # COMMAND: listnotdj
    @commands.command()
    async def listnotdj(self, ctx, *args):
        '''List 10 not djable songs'''
        songs = self.djdb.list_all_songs(dj = False)
        await self.list(ctx, display_list = songs, title = "List 10 not djable songs", none_message = "No song found")

    # COMMAND: search
    @commands.command()
    async def search(self, ctx, *args):
        '''List all matching title songs'''
        q = " ".join(args)
        songs = self.djdb.search(search_term = q)
        await self.list(ctx, display_list = songs, title = f"Searching: {q}", none_message = "No song found")


    # list: for listing in discord channel (eg: list songs)
    async def list(self, ctx, display_list, title = "", none_message = "Nothing found"):
        if display_list:
            str = title 
            if title != "": str += "\n"
            for i, s in enumerate(display_list):
                str += f"{i+1}: "
                for detail in s:
                    str += f"{detail}\t"
                str += "\n"
            await self.notify(ctx, str, del_sec=None)
        else: 
            await self.notify(ctx, none_message)


    # COMMAND: tag
    # tag "link" "tag"
    @commands.command()
    async def tag(self, ctx, *args):
        pass


    # ------------------------------------------------------------------------------------------------- # 
    # ------------------------------------- EVENT/ERROR HANDLING --------------------------------------- # 
    # ------------------------------------------------------------------------------------------------- # 
    @commands.Cog.listener()		
    async def on_ready(self, ):
        await self.bot_status(False)
        print(f'Logged in as {client.user} (ID: {client.user.id})')
        print('------')

    
    async def bot_status(self, dj):
        if dj: 
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name="DJ"))
        else:
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="play"))



    # for other section: 1. encore | 2. reDJ | 3. del
    @commands.Cog.listener()
    async def on_button_click(self, interaction):
        ctx = await self.bot.get_context(interaction.message)

        id = interaction.component.id

        actions = {
            'encore': self.repeat_btn_handler,
            'reDJ': self.reDJ_btn_handler,
            'del': self.del_btn_handler,
        }
        for action, handler in actions.items():
            if action in id[:len(action)]:
                await handler(ctx, id[len(action)+1:])
                return # maybe break

    # --------- ACTION HANDLERS --------- # 
    # repeat button handler
    async def repeat_btn_handler(self, ctx, vid):
        url = "youtu.be/" + vid
        await self.play(ctx, url)
    # reDJ button handler
    async def reDJ_btn_handler(self, ctx, _):
        await self.dj(ctx)
    # del button handler (delete song from db)
    async def del_btn_handler(self, ctx, vid):
        self.djdb.remove_song(vid)
        await self.notify(ctx, f"Removed song from db ({vid})")


    # handle all (command) error
    @commands.Cog.listener()
    async def on_command_error(self, ctx, e):
        try:
            # send error message to text channel
            await self.notify(ctx, e.original, del_sec=None)
            # log to files
            error_log_e(e.original)
            # print traceback on console
            raise e.original
        except:
            # send error message to text channel
            await self.notify(ctx, e, del_sec=None)
            # log to files
            error_log_e(e)
            # print traceback on console
            raise e

if __name__ == "__main__":
    # set ffmpeg error log file
    os.environ['FFREPORT'] = f'file={ffmpeg_error_log}:level=16'

    # for voice client to work: you need opus and ffmpeg
    discord.opus.load_opus(opus_dir)

    # initialise ytdl from youtube_dl library
    ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

    intents = discord.Intents.default()
    intents.members = True
    client = ComponentsBot(command_prefix="=", case_insensitive=True, 
                    description='DJ', intents=intents)

    client.add_cog(DJ(client))
    client.run(TOKEN)