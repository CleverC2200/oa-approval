from base64 import b64decode
from zlib import decompress


def _fixture(encoded):
    return decompress(b64decode(encoded))


FIRST = _fixture(
    'eJwLSixSlGJnZMh9qKHOzcjKzsjGyNjQ0MDwJcn3qByT0FE+lksSS5qZTCUmpjQwMPLkJuZlpqUWl+hV5ObY'
    'eOaV5Wcmp9rZOObml+aV2Jkb6xka2ehDeTa+qbn5do6jYBSMglEwCkbBKMAA3e6BasysLIwMVAIAP5jtqg=='
)


LAST = _fixture(
    'eJwLSixSlGJnZFh56MgpHkZWdmZGNkbGhoYGhgtyT5fKMXH1crFckljSzPSxbPLlBgZGntzEvMy01OISvYrc'
    'HMdRMApGwSgYgcBG3zc1N9/ORt8zryw/MznVTrY8LJCZlYUBAKryXoY='
)


WRONG_LAST = _fixture(
    'eJwLSixSlGJnZFh56MgpHkZWdmZGNkbGhoYGhjv7V8yWY+Lq5WK5JLGkman56z39BgZGntzEvMy01OISvYrc'
    'HKdRMApGwSgYgcBG3zc1N9/ORt8zryw/MznVTrY8LJCZlYUBADh8Y6A='
)


SINGLE = _fixture(
    'eJwLSixSlGJnZPj8sOk1NyMrOwMbI2NDQwND3+nL3+WYmC5JsFySWNLM9LFs8uUGBkae3MS8zLTU4hK9itwc'
    'G8+8svzM5FQ7G8fc/NK8EjtzYz1DIxt9KM/GNzU3385xFIyCUTAKRsEoGAWjYBSMglEwCoDARh/cRbDRh3Uk'
    'ZMvDAplZWRgAurE7Ug=='
)


MEMBER_OVER_LIMIT = (
    _fixture(
        'eJzt3M8rg2EAwPHn2Yx5QxFWS4kkS22LlhtFS7kILbkQtSiW/CgO4kWWH4dJycmBnFArDm6bchBTUwsHBzkJ'
        'hRKR2dAcpN5yVd+n5/I8n+d5vv/B09jeX2BOkcJRfGdLl4ZUaZRyNTiuqiJmqXwt0mVdBb4WSaq6e6yKtUld'
        'xXr2jWqQisft6XD3W4c9PdMHlrJyp2iyDZTn2I1GRRlddXrzCicm6qp770+Pupfzljr9uUN9j5GdtrA/MDIy'
        '+hS/fg9fnIciZ77Kt32ftdX00nUbdSzGzIfxdPeYdyu419J2uel/OG1+6S6NOlxakLjhGlv5vfXz7IkW/PV1'
        'DUjccGkBceLEiRMnTpw4ceLEiRMnTpw4ceLEiRMnTpw4ceLEiRMnTpw4ceLEiRMnTpw4ceLEiRMnTpz4v4yL'
        'mfs0u64qYnoOZd7UTPlnQ3FlI66EtCeKoiiKoiiKoiiKoiiKoiiKoiiKoiiKoiiKoiiKoiiKoui/03z5PJih'
        'T94W4nOqQuoa6j0lC1GrmA9Ky58/65+rbSjWG5Kk+B4fhfNj8w=='
    ),
    _fixture(
        'eJzt288rw2EcwPHn+90vlhYHKeWgFKZJU6477A/QVlZLs6gdlKWtbErylHbYDkspJ7s5bSS7uCEkWUocHE0O'
        'xIHyo7FlpjkovuVo9a7P5XlefXo//8DjHg23t5oUkbxb9FkUQ71OqVOU5WcppTiOWGc6VHP+6fOgl3LnRIqV'
        'edUfOnVLg2IOBoJjgXDvdHAiVzb/nMxvlyhaIyri9w19quOs5SXXdOuMrSf+5StRFEVRFEVRFEVRFEVRFEVR'
        'FEVRFEVRFEVRFEVRFEVRFEVRFP2uIpePmJxDwl6wu0YaO337oQPjwLX/8chxuBcf3/BOPqxuFjzDpR7be/NS'
        'ZWcuFtjezV5cViCtBdUN65oGfG1ktKC6kdYC4sSJEydOvObiqavk7NvreapcLC70e7sKFp0xK0RlpFBU12C0'
        'dLNlE/Gp7j9/Um2Lelw6g158AGzjebo='
    ),
)


TOTAL_OVER_LIMIT = (
    _fixture(
        'eJzt3c9Lk3EcwPHv8+icjYwKljC9rMMkRyYT86aVSdAlZqyQmKPD6IflsAI72Pa4ULIgRahOI4oOYpOhQaPL'
        'jIx++EgeRkr0Y4iHmhMUpJj5Y013KeEJb5G+H57L87yfz/P6Cz58a09fMpv0kvjwbOlJnqTbIuVK0kzEryii'
        'oKLbaZbleLQ1/ZCtKEpYEY+uy12Jab2ik/SeRnfJ1YsXbrzdYyurEcf3XS4zlubmGgzehzXthbv9/qOHPDNj'
        'Iw2BwntnQruam+aiYddoKNLS4v2eii+Nxj6q0fHOyoXXnSX1+cmzicXyO8um4VSe29c+MDhU55oIhmbHTiYb'
        'rIvlDq2QmXD4Hqx99fu377XCev+uETITDq0ADg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4O'
        'Dg4ODg4ODg4ODg7+X+KiY2ZrqVwVzf+h7piqbgvdVFOGxymDqn1TqVQqlUqlUqlUKpVKpVKpVCqVSqVSqVQq'
        'lUqlUqlUKpVKpVKpVCqVSv3XVaiVPfpqh7Albfb67UXOl02vcvZ/dc0NV70Z6jjXX+eZDYaTJ04tWvcuG++m'
        'h31t7sEXA7GJtUFdDb1aITPRqxUyE8V9GgEcHBwcHHyz4YHJ29d+zo8HUgsLXb5PT71meWfPlJI5Dup53+px'
        'UN8O3nesHAd1pdnDcVAbdG8THBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc'
        'HHzT4kINmor+uv65gVdrwDc1bqmdn9yWlRMTIn0rQpLtx/oPuL9YxeeIbCmo6HaaZTkebc2sWinh1VWrrsS0'
        'fmXVytPoXlm1EsbDNcXi3XnL+jazbh2xW7J02ZL48/oFry1vPg=='
    ),
    _fixture(
        'eJwLSixSlGJnZLg6t4qbj5GVk5mRg5HxzomGhgaGHQFl6YpMXIvBHJaGhgNrGxiWNDPVfswoamBlZC8pz9er'
        'yM35o637T3Taf67T9a2p+w9tuv8wMv/Dqu0/wqKxSawBS2itxSEB1bEalwRExypcEqOWj1o+avmo5aOWj1o+'
        'avmo5aOWj1o+avmo5aOWj1o+avmo5aOWj1o+avmo5aOWj1o+avmo5aOWj1o+avmo5aOWj1o+avmo5aOWj1o+'
        'ajl1LJ/7uK/218/rc////j2BXUDAnI+ZbT0DAxA1MDAyBfrfm88lqsVw0kONuEVGsuVhgcysLAwAoUjf2w=='
    ),
)
