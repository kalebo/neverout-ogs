#!/usr/bin/env python

## TODO
## 1. make rudimentary call to api authenticated
## 2. architect a daemon that polls for games
## 3. Datastructure that updates time remaining
## 4. push notification to phone
## 5. More complex notifications (e.g., still 10 hours left but getting close to bed time...)
##

import arrow
import requests
from string import Template

import config

## Consider using the realtime api with websockets
import websocket
from socketIO_client import SocketIO
import logging
logging.getLogger('socketIO-client').setLevel(logging.DEBUG)
logging.basicConfig()

class Client:
    baseurl = "https://online-go.com"

    def __init__(self, username, password):
        self.s = requests.Session()
        self.s.post(self.baseurl + "/api/v0/login", \
                {'username': usrn, 'password': passw})
        config = self.get_config()
        self.cid = "wherewouldthiscomefrom" # idk
        self.uid = config['user']['id']
        self.notification_auth = config['notification_auth']
        self.chat_auth = config['chat_auth']

    def _request(url, verb, data=None):
        pass


    def get_config(self):
        r = self.s.get(self.baseurl + "/api/v1/ui/config")
        return r.json()

    def curr_games(self):
        r = self.s.get(self.baseurl + "/api/v1/me/games", \
                       params = {"started__isnull": False, "ended__isnull": True})
        return r.json()

    def curr_game_ids(self):
        games = self.curr_games()
        return [i['id'] for i in games['results']]

    def get_game(self, gid):
        r = self.s.get(self.baseurl + "/api/v1/games/{}".format(gid))
        return r.json()

    @staticmethod
    def calc_remaining(gamejson):
        clock = gamejson['gamedata']['clock']
        return arrow.get(clock['expiration']/1000)

    def get_time_remaining(self):
        results = []
        for gid in self.curr_game_ids():
            game = self.get_game(gid)
            if game['gamedata']['clock']['current_player'] == self.uid:
                results.append({'title': game['name'], \
                                'remaining': self.calc_remaining(game).humanize()})
        return results

    def _setup_ws(self):
        self.ws = websocket.create_connection("wss://online-go.com/socket.io/?transport=websocket")

    def _setup_sio(self):
        self.sio = SocketIO("https://online-go.com", transports=['websocket'])

    ping_template = Template('42["net/ping",{"client": "$client", "drift":0, "latency":0 }]')
    def ping(self):
        if self.ws:
            self.ws.send(bytes(self.ping_template.substitute(client=0), 'utf8'))

    def push(self, message, priority=0, retry=60, expire=60*3, sound=None):
        params = {"user": config.Pushover.user,
                  "token": config.Pushover.token,
                  "message": message,
                  "priority": priority}

        if priority >= 2:
            params.update({"retry": retry, "expire": expire})
        if sound:
            params.update({"sound": sound})

        requests.post("https://api.pushover.net/1/messages.json", params)



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
        moves = game['gamedata']['moves']
        seq = []

        for i, m in enumerate(moves):
            s = "B[{}{}]" if i %2 == 0 else "W[{}{}]"
            seq.append(s.format(*self.convert_coord(m[0], m[1])))

        return self.heading + ";".join(seq) + ")"






if __name__ == "__main__":
    c = Client(config.OGS.user, config.OGS.password)
