#!/usr/bin/env python

## TODO
## 1. make rudimentary call to api authenticated
## 2. architect a daemon that polls for games
## 3. Datastructure that updates time remaining
## 4. push notification to phone
## 5. More complex notifications (e.g., still 10 hours left but getting close to bed time...)
### Next actions
## [ ] make update alert only trigger when it is your turn
## [ ] handle pagination better
##

import arrow
import requests
import sched
import time
import atexit
import sys
import structlog
import signal
from string import Template

import config

## Consider using the realtime api with websockets
import websocket
from socketIO_client import SocketIO
import logging

logging.getLogger("socketIO-client").setLevel(logging.DEBUG)
logging.basicConfig()


class Client:
    baseurl = "https://online-go.com"

    def __init__(self, username, password, ratelimit=1):
        self.log = structlog.getLogger()

        # Rate limiting functionality
        self.lastreq = None
        self.ratelimit = ratelimit

        # Scheduling functionality
        self.games = {}
        self.timers = {}  # gid: sched_handle
        self.sched = sched.scheduler(time.time, time.sleep)

        # Api session
        self.s = requests.Session()
        self.login(username, password)
        config = self.get_config()

        # Class variables
        self.cid = "wherewouldthiscomefrom"  # idk
        self.uid = config["user"]["id"]
        self.notification_auth = config["notification_auth"]
        self.chat_auth = config["chat_auth"]

    def login(self, username, password):
        self._request(
            self.baseurl + "/api/v0/login",
            "POST",
            {"username": username, "password": password},
        )

    def _request(self, url, verb="GET", data=None, retries=3, **kwargs):
        if (
            self.lastreq
            and arrow.now().timestamp - self.lastreq.timestamp <= self.ratelimit
        ):
            # We should be good netizens and limit ourselves unless using the RT API
            time.sleep(self.ratelimit)
        
        # Adaptive backoff
        for attempt in range(retries + 1):
            try:
                self.lastreq = arrow.now()
                return self.s.request(verb, url, data=data, **kwargs)

            except Exception as e:
                # with max retries just give up
                if attempt == retries:
                    raise e

                time.sleep(self.ratelimit * attempt)
        

    def get_config(self):
        r = self._request(self.baseurl + "/api/v1/ui/config", "GET")
        return r.json()

    def curr_games(self):
        r = self._request(
            self.baseurl + "/api/v1/me/games",
            "GET",
            params={"started__isnull": False, "ended__isnull": True},
        )
        return r.json()

    def curr_game_ids(self):  # WARN: the json result from curr_games may be paginated!
        games = self.curr_games()
        if games["next"] or games["previous"]:
            self.log.warning("curr_games returned paginated results!")
        return [i["id"] for i in games["results"]]

    def get_game(self, gid):
        r = self._request(self.baseurl + "/api/v1/games/{}".format(gid), "GET")
        return r.json()

    def _get_opponent_name(self, playersjson):
        if playersjson["black"]["id"] == self.uid:
            return playersjson["white"]["username"]
        else:
            return playersjson["black"]["username"]

    def get_status(self, gid):
        game = self.get_game(gid)
        return {
            "title": game["name"],
            "myturn": game["gamedata"]["clock"]["current_player"] == self.uid,
            "turn": len(game["gamedata"]["moves"]),
            "opponent": self._get_opponent_name(game["players"]),
            "url": "https://online-go.com/game/{}".format(game["id"]),
            "timeout": arrow.get(game["gamedata"]["clock"]["expiration"] / 1000),
        }

    def get_statuses(self):
        results = {}
        for gid in self.curr_game_ids():
            results.update({gid: self.get_status(gid)})
        return results

    def update(self):
        gids = self.curr_game_ids()
        self.games.update({gid: self.get_status(gid) for gid in gids})

    def alert(self, status):
        priority = 1 if (status["timeout"].timestamp - time.time() < 12 * 3600) else 0
        self.push(
            f"Your game with {status['opponent']} will end in {status['timeout'].humanize()}!\n{status['url']}",
            priority,
        )

    def _setup_ws(self):
        self.ws = websocket.create_connection(
            "wss://online-go.com/socket.io/?transport=websocket"
        )

    def _setup_sio(self):
        self.sio = SocketIO("https://online-go.com", transports=["websocket"])

    ping_template = Template(
        '42["net/ping",{"client": "$client", "drift":$drift, "latency":$latency }]'
    )

    def ping(self):
        if self.ws:
            self.ws.send(
                bytes(
                    self.ping_template.substitute(client=0, drift=0, latency=0), "utf8"
                )
            )

    def push(self, message, priority=0, retry=60, expire=60 * 3, sound=None):
        params = {
            "user": config.Pushover.user,
            "token": config.Pushover.token,
            "message": message,
            "priority": priority,
        }

        if priority >= 2:
            params.update({"retry": retry, "expire": expire})
        if sound:
            params.update({"sound": sound})

        requests.post("https://api.pushover.net/1/messages.json", params)

    def watchdog_alert(self):
        self.push("NeverOut-OGS shutdown unexpectedly!", priority=1, sound="alien")

    def enable_watchdog(self):
        atexit.register(self.watchdog_alert)

    def disable_watchdog(self):
        atexit.unregister(self.watchdog_alert)

    def update_games(self):
        self.log.info("Updating self.games ...")
        self.games = {gid: self.get_status(gid) for gid in self.curr_game_ids()}

    def set_game_timer(self, gid, status, fraction=1 / 2):
        delta = (status["timeout"].timestamp - time.time()) * fraction
        task = self.sched.enter(delta, 1, self.notify_task, (gid,))
        self.log.info(
            "Alert for game {} set to run in {:.0f} seconds at {:.0f}".format(
                gid, delta, time.time() + delta
            )
        )
        self._replace_timer(gid, task)

    def _replace_timer(self, gid, task):
        if gid in self.timers:
            try:
                self.sched.cancel(self.timers[gid])
                self.log.info("Timer was replaced for game: {}".format(gid))
            except ValueError:
                pass
        self.timers[gid] = task

    def update_task(self, delay=30 * 60.0):
        self.update_games()
        for gid, status in self.games.items():
            if status["myturn"] and gid not in self.timers:
                # only set the timer once in update_task. After notify task should handle it
                self.set_game_timer(gid, status)
        self.log.info(
            "Next update will occur in {} seconds at {:.0f}".format(
                delay, time.time() + delay
            )
        )
        self.sched.enter(delay, 1, self.update_task)

    def notify_task(self, gid):
        self.log.info("Checking status of game {}...".format(gid))
        status = self.get_status(gid)

        if gid not in self.games:
            self.log.info("Game {} has ended.".format(gid))
            self.games.pop(gid, None)
            return

        # only notify if there hasn't been any moves since the timer was set
        if status["turn"] == self.games[gid]["turn"] and status["myturn"]:
            self.alert(status)
            self.log.info("Alerting!")

        self.set_game_timer(gid, status)
        self.games[gid].update(status)

    def run(self):
        self.log.info("Running...")

        self.enable_watchdog()
        self.update_task()
        self.sched.run()

    coords = "abcdefghijklmnopqrs"

    @classmethod
    def convert_coord(cls, col, row):
        return cls.coords[col], cls.coords[row]

    heading = """(;FF[4]GM[1]SZ[19]
PB[Black]
PW[White]
KM[6.5]
;"""

    def dump_sgf(self, gid):
        game = self.get_game(gid)

        tc = game["gamedata"]["time_control"]
        tc_max = tc["max_time"]
        tc_inc = tc["time_increment"]

        assert tc["system"] == "fischer"  # only fischer timing is supported right now

        moves = game["gamedata"]["moves"]
        start = game["gamedata"]["start_time"]
        clock = [tc["initial_time"]] * 2
        time_elapsed = 0
        seq = []

        for i, m in enumerate(moves):
            player = i % 2  # even black -- odd white

            s = "B[{}{}]C[{}]" if player == 0 else "W[{}{}]C[{}]"
            time_elapsed += m[2] / 1000
            seq.append(
                s.format(
                    *self.convert_coord(m[0], m[1]),
                    "Played at {}\n{:.1f} seconds remain".format(
                        arrow.get(start + time_elapsed), clock[player] - m[2] / 1000
                    ),
                )
            )
            # advance clock
            clock[player] = min(clock[player] - m[2] / 1000 + tc_inc, tc_max)

        return self.heading + ";".join(seq) + ")"


if __name__ == "__main__":
    c = Client(config.OGS.user, config.OGS.password)
    try:    
        c.run()
    except:
        c.watchdog_alert()
