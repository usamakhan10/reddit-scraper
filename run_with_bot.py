import os
import threading
import time

from dotenv import load_dotenv

import main as reddit_monitor
from discord_bot import RedditDiscordBot


def main():
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel_id_str = os.getenv("DISCORD_CHANNEL_ID", "").strip()

    if not token or not channel_id_str:
        print("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set in the environment.")
        raise SystemExit(1)

    try:
        channel_id = int(channel_id_str)
    except ValueError:
        print("DISCORD_CHANNEL_ID must be an integer.")
        raise SystemExit(1)

    # Init DB and Reddit
    conn = reddit_monitor.init_db(reddit_monitor.DB_PATH)
    reddit = reddit_monitor.reddit_client()

    # Start Discord bot
    bot = RedditDiscordBot(channel_id=channel_id, db_conn=conn)

    # Register bot as notifier so stream threads send matches to the bot
    reddit_monitor.register_notify_handler(bot.enqueue_match)

    # Start keyword refresher and control server for instant reloads
    reddit_monitor.set_keywords(reddit_monitor._ENV_KEYWORDS)
    reddit_monitor.start_keywords_refresher(conn)
    reddit_monitor.start_control_server(conn)

    # Run the bot in a separate thread so we can start monitors once ready
    def _run_bot():
        bot.run(token)

    t_bot = threading.Thread(target=_run_bot, name="discord-bot", daemon=True)
    t_bot.start()

    # Wait for bot to be ready before starting monitor threads
    for _ in range(60):  # up to ~60s
        if bot.is_ready():
            break
        time.sleep(1)

    # Start monitor threads
    t_posts = threading.Thread(target=reddit_monitor.stream_submissions, args=(reddit, conn), daemon=True)
    t_comments = threading.Thread(target=reddit_monitor.stream_comments, args=(reddit, conn), daemon=True)
    t_posts.start()
    t_comments.start()

    reddit_monitor.log.info(
        "Bot ready=%s | Starting watcher | subs=%s | exclude=%s | nsfw=%s | channel_id=%s",
        bot.is_ready(),
        reddit_monitor.subreddit_target(),
        ",".join(sorted(reddit_monitor.EXCLUDE_SUBS)) or "-",
        reddit_monitor.ALLOW_NSFW,
        channel_id,
    )

    # Keep main thread alive; forward Ctrl+C
    try:
        while t_bot.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        reddit_monitor.log.info("Interrupted, shutting down")
    finally:
        reddit_monitor._stop = True  # type: ignore[attr-defined]
        for _ in range(5):
            if not (t_posts.is_alive() or t_comments.is_alive()):
                break
            time.sleep(0.5)


if __name__ == "__main__":
    main()
